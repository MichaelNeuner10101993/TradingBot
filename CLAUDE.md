# TradingBot – Claude Memory

## Überblick
Kraken Trading Bot (Python, CCXT) auf Raspberry Pi.
Strategie: SMA-Crossover mit synthetischem Stop-Loss / Take-Profit.
Mehrere Bot-Instanzen parallel, je eine pro Coin, je eine SQLite-DB.

## Projektstruktur
```
TradingBot/
├── main.py                  # Entry Point, Hauptschleife, argparse CLI
├── bot/
│   ├── config.py            # BotConfig + RiskConfig (dataclasses)
│   ├── ops.py               # retry_backoff, CircuitBreaker, Logging
│   ├── data_feed.py         # CCXT: OHLCV, Balance, Open Orders
│   ├── strategy.py          # SMA-Crossover → BUY/SELL/HOLD
│   ├── risk.py              # Dynamische Positionsgröße (Balance / num_bots × risk)
│   ├── execution.py         # Order-Submit, Dry-Run, Fallback-Preis-Injektion
│   ├── persistence.py       # SQLite (orders, trades, errors, bot_state)
│   └── sl_tp.py             # calc_levels(), SlTpMonitor
├── web/
│   ├── app.py               # Flask Dashboard (Port 5001)
│   └── templates/index.html # Multi-Bot Dashboard
├── db/                      # SQLite DBs: SNX_EUR.db, BTC_EUR.db etc.
├── run/                     # PID-Dateien (UI-gestartete Bots)
├── logs/                    # Log-Dateien pro Bot + web.log
├── bot.conf.d/              # Konfiguration pro Bot-Instanz (systemd)
├── systemd/                 # Service-Dateien + install.sh
└── requirements.txt
```

## Aktive Coins (bot.conf.d/)
| Symbol    | SL   | TP    | Startup-Delay |
|-----------|------|-------|---------------|
| SNX/EUR   | 3%   | 6%    | 0s            |
| BTC/EUR   | 2%   | 4%    | 20s           |
| TRUMP/EUR | 5%   | 10%   | 40s           |
| PEPE/EUR  | 5%   | 10%   | 60s           |
| XRP/EUR   | 3%   | 6%    | 80s           |
| ETH/EUR   | 2.5% | 5%    | 100s          |

Alle mit: `--timeframe 5m --fast 9 --slow 21 --safety-buffer 0.10`
Startup-Delay staffelt API-Calls beim gleichzeitigen Start (Kraken Rate-Limit).

## Wichtige Konfiguration (bot/config.py)
```python
BotConfig:     symbol, timeframe, fast_period, slow_period, poll_seconds=60, dry_run
RiskConfig:    quote_risk_fraction=0.95, safety_buffer_pct=0.10, max_open_orders=1,
               min_order_quote=15.0, stop_loss_pct, take_profit_pct, db_dir="db",
               max_consecutive_errors (→ CircuitBreaker)
OpsConfig:     db_path, log_dir, log_level
ExchangeConfig: exchange_id, api_key, api_secret, enable_rate_limit
```
`poll_seconds=60`: bei 5m-Candles reicht 1×/Minute; reduziert Kraken-API-Last.

## Kritische Bugs & Fixes (bereits gelöst)
- **Kaufpreis NULL in DB**: Kraken archiviert Market-Orders sofort → fetch_order schlägt fehl.
  Fix: In `execution.py` wird `last_price` als `average` ins Order-Dict injiziert **vor** `upsert_order()`.
- **PEPE Preis 0.0000**: Zu wenig Dezimalstellen. Fix: `_price_fmt()` in app.py (dynamisch bis 8 Stellen).
- **ETH Staub-Position**: `_meets_exchange_minimum()` fehlte im sell(). Jetzt in buy() + sell().
- **Web-Dashboard DB nicht gefunden**: Pfad war relativ. Fix: `PROJECT_ROOT` in web/app.py.
- **chart.js CDN nicht erreichbar auf Pi**: Lokal gespeichert unter `web/static/chart.min.js`.
- **systemd ExecStart word-splitting**: `$BOT_ARGS` wurde als ein Argument übergeben (exit-code 2).
  Fix: `ExecStart=/bin/sh -c '... --symbol "$BOT_SYMBOL" $BOT_ARGS'` — Shell macht das word-splitting.
- **Kraken Rate-Limit bei Mehrfach-Start**: 6 Bots gleichzeitig = 12+ private API-Calls auf einmal.
  Fix: `poll_seconds=60` + `--startup-delay` pro Bot (0/20/40/60/80/100s) in `bot.conf.d/`.
- **data-cfg JSON-Parse-Fehler**: `| tojson | e` escapet nicht (Markup-Objekt). HTML-Attribut mit
  einfachen Anführungszeichen + nur `| tojson`: `data-cfg='{{ cfg | tojson }}'`.

## Datenbank-Schema (SQLite pro Bot)
```sql
orders    (client_id, exchange_id, symbol, side, amount, price, status, raw, created_at)
trades    (client_id, symbol, amount, entry_price, sl_price, tp_price, status, opened_at, closed_at)
          status: 'open' | 'sl_hit' | 'tp_hit' | 'signal_close'
errors    (context, message, occurred_at)
bot_state (key, value)  -- last_signal, last_price, status, balance_quote/base, sl_pct, tp_pct ...
```

## Web-Dashboard (web/app.py + index.html)
- Port 5001, Flask, liest alle `db/*.db`
- **Auto-Refresh**: 60s (Seite), 5s (Cards live via /api/bots)
- **Gebühren**: `KRAKEN_FEE = 0.0026` (0.26% pro Order), alle P&L-Anzeigen sind Netto
- **Endpunkte**:
  - `GET /` – Dashboard
  - `GET /api/bots` – JSON aller Bot-Zustände
  - `GET /api/markets` – Alle EUR-Spotmärkte auf Kraken mit Preis (5-Min-Cache)
  - `POST /api/trade/<symbol>/<client_id>/sltp` – SL/TP manuell setzen
  - `POST /api/bot/start` – Bot starten (symbol, timeframe, fast, slow, sl, tp, safety_buffer, dry_run)
  - `POST /api/bot/stop` – Bot stoppen (symbol); via PID-Datei → pgrep → pkill (3-stufig)
  - `POST /api/bot/delete` – Bot löschen (symbol, keep_db); stoppt Prozess, entfernt conf + PID,
    archiviert DB nach `db/archive/` oder löscht sie → andere Bots teilen Kapital neu auf

## Bot-Verwaltung via UI
- **Hinzufügen**: Dialog mit Markt-Suche (Dropdown, /api/markets, 5-Min-Cache), startet via subprocess
- **Starten** (gestoppt): `▶ Starten`-Button mit gespeicherter Konfiguration aus DB
- **Stoppen** (laufend): `■ Stoppen`-Button, 3-stufig: PID-Datei → pgrep → pkill
- **Löschen** (gestoppt): `✕`-Button → Dialog mit "DB archivieren"-Checkbox (Standard: ja)
  - Entfernt `bot.conf.d/SYMBOL.conf` → kein Autostart mehr bei systemd-Reboot
  - DB → `db/archive/SYMBOL_YYYYMMDD_HHMMSS.db` (oder löschen)
  - Andere laufende Bots teilen Kapital beim nächsten Loop (60s) automatisch neu auf

## SL/TP in UI editierbar
- Offene Trades: − / + Buttons mit adaptiver Schrittweite: `step = 1.5 / amount`
  → 1 Klick ≈ 1.5 EUR P&L-Änderung, unabhängig vom Coin
- „Speichern" schreibt direkt in SQLite, Bot übernimmt beim nächsten Loop
- **Abstand zum aktuellen Kurs**: `dist_to_sl_pct` / `dist_to_tp_pct` werden in `_load_bot()` berechnet
  und in der Trades-Tabelle angezeigt (farblich: rot < 1%, orange < 2% für SL; grün < 0.5% für TP)

## Bot-Start via systemd (empfohlen)
```bash
bash systemd/install.sh          # einmalig einrichten (ersetzt DEIN_BOTDIR/DEIN_USER)
sudo systemctl start tradingbot.target
sudo systemctl status 'tradingbot@*'
journalctl -u tradingbot@SNX_EUR -f
```
- Template-Service: `tradingbot@.service`
- Konfiguration pro Bot: `bot.conf.d/SNX_EUR.conf` (BOT_SYMBOL + BOT_ARGS inkl. --startup-delay)
- Kein PID-File bei systemd → Stop-Button im Dashboard nutzt pkill als Fallback
- **WICHTIG ExecStart**: Muss `/bin/sh -c '...'` verwenden, sonst kein word-splitting von `$BOT_ARGS`
  ```ini
  ExecStart=/bin/sh -c 'DEIN_BOTDIR/botvenv/bin/python DEIN_BOTDIR/main.py --symbol "$BOT_SYMBOL" $BOT_ARGS'
  ```
- Nach Änderungen: `scp` Dateien auf Pi → `bash systemd/install.sh` → `daemon-reload` → `restart`

## Positionsgröße (risk.py)
```
usable = balance_EUR × (1 - safety_buffer)   # z.B. × 0.90
per_bot = usable / num_active_bots            # aufgeteilt auf alle DBs in db/
trade_EUR = per_bot × quote_risk_fraction     # z.B. × 0.95
amount = trade_EUR / last_price
```

## Pi-Netzwerk: PiVPN + WireGuard

### Installation (einmalig auf dem Pi)
```bash
curl -L https://install.pivpn.io | bash
# → WireGuard wählen, Port 51820 UDP
# → DNS: 1.1.1.1 oder eigener Pi-hole
# → öffentliche IP oder DynDNS-Hostname eingeben
```

### Router-Konfiguration
- **Port-Forwarding**: UDP 51820 → interne IP des Pi
- **Statische lokale IP** für den Pi empfohlen (DHCP-Reservierung im Router oder `/etc/dhcpcd.conf`)
- Falls keine statische öffentliche IP: DynDNS-Dienst (z.B. DuckDNS) einrichten

### Client-Verwaltung
```bash
pivpn add              # neuen Client erstellen (fragt nach Name)
pivpn -qr <Name>       # QR-Code für Handy/Tablet anzeigen
pivpn -l               # alle Clients auflisten
pivpn -r <Name>        # Client entfernen
pivpn -d               # Diagnose / Verbindungstest
sudo wg show           # WireGuard-Status (aktive Verbindungen, Datentransfer)
```

### Konfigurationsdatei (für Desktop/Laptop)
```bash
cat ~/configs/<Name>.conf   # Inhalt direkt in WireGuard-App importieren
scp pi@<pi-ip>:~/configs/<Name>.conf .  # Datei auf PC kopieren
```

### Systemd-Service
```bash
sudo systemctl status wg-quick@wg0
sudo systemctl restart wg-quick@wg0
```

### Zugriff auf Dashboard über VPN
Nach VPN-Verbindung: `http://<pi-vpn-ip>:5001` im Browser öffnen.

---

## Nutzer-Präferenzen
- Sprache: Deutsch (Kommentare, UI-Texte, Antworten)
- Deployment: Raspberry Pi (Linux/bash), Entwicklung auf Windows 11
- Kein Auto-Commit
