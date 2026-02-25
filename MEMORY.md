# Memory – Claude Code Session
# Dieses File wird auf dem Pi automatisch geladen. Für Windows-Instanz: manuell als Kontext einfügen.

## Allgemeines
- Benutzer: Michael Neuner, spricht Deutsch, kein Auto-Commit
- Pi-Hostname: CAT, SSH-User: xxx
- Arbeitsverzeichnis Pi: /home/xxx
- Shell: bash, Python 3.13 in `/home/xxx/bot/botvenv/`
- Entwicklung auf Windows 11 (C:\Users\Michael Neuner\), Deployment via scp auf Pi

---

## Hauptprojekt: TradingBot (/home/xxx/bot/)

**Beschreibung:** Kraken Trading Bot (Python 3.13, CCXT-Library), Raspberry Pi (aarch64).
Strategie: SMA-Crossover mit synthetischem Stop-Loss / Take-Profit.
Mehrere Bot-Instanzen parallel, je eine pro Coin, je eine SQLite-DB.

### Projektstruktur
```
/home/xxx/bot/
├── main.py                  # Entry Point, argparse CLI
├── bot/
│   ├── config.py            # BotConfig + RiskConfig (dataclasses)
│   ├── ops.py               # retry_backoff, CircuitBreaker, Logging
│   ├── data_feed.py         # CCXT: OHLCV, Balance, Open Orders
│   ├── strategy.py          # SMA-Crossover → BUY/SELL/HOLD
│   ├── risk.py              # Dynamische Positionsgröße
│   ├── execution.py         # Order-Submit, Dry-Run, Fallback-Preis-Injektion
│   ├── persistence.py       # SQLite (orders, trades, errors, bot_state)
│   └── sl_tp.py             # calc_levels(), SlTpMonitor
├── web/
│   ├── app.py               # Flask Dashboard (Port 5001)
│   └── templates/index.html # Multi-Bot Dashboard, Dark-Theme
├── db/                      # SQLite DBs: SNX_EUR.db, BTC_EUR.db etc.
├── db/archive/              # Archivierte DBs (SYMBOL_YYYYMMDD_HHMMSS.db)
├── logs/                    # Log-Dateien pro Bot + web.log
├── bot.conf.d/              # Konfiguration pro Bot-Instanz (systemd)
├── systemd/                 # Service-Dateien + install.sh
├── CLAUDE.md                # Detaillierte technische Doku & Bug-History
└── requirements.txt
```

### Aktive Coins (bot.conf.d/)
| Symbol    | SL   | TP    | Startup-Delay |
|-----------|------|-------|---------------|
| SNX/EUR   | 3%   | 6%    | 0s            |
| BTC/EUR   | 2%   | 4%    | 20s           |
| TRUMP/EUR | 5%   | 10%   | 40s           |
| PEPE/EUR  | 5%   | 10%   | 60s           |
| XRP/EUR   | 3%   | 6%    | 80s           |
| ETH/EUR   | 2.5% | 5%    | 100s          |

Alle mit: `--timeframe 5m --fast 9 --slow 21 --safety-buffer 0.10`

### Strategie (aktuell)
```
BUY  → Fast SMA (9) kreuzt Slow SMA (21) von unten nach oben
SELL → Fast SMA (9) kreuzt Slow SMA (21) von oben nach unten
HOLD → kein Crossover
```
Synthetischer SL/TP wird nach jedem BUY gesetzt und in jeder Loop-Iteration geprüft.

### Geplante Strategie-Verbesserungen (noch nicht implementiert)
- **RSI-Filter**: Kaufsignal nur wenn RSI < 70, Verkauf nur wenn RSI > 30
- **Volume-Filter**: Signal nur gültig bei überdurchschnittlichem Volumen
- **ATR-basierte SL/TP**: Dynamisch statt fester Prozentsätze
→ Warten auf Trade-Daten (DB neu aufgesetzt am 23.02.2026) zum Analysieren

### Positionsgröße (risk.py)
```
usable   = balance_EUR × (1 - safety_buffer)   # z.B. × 0.90
per_bot  = usable / num_active_bots            # aufgeteilt auf alle DBs in db/
trade_EUR = per_bot × quote_risk_fraction      # z.B. × 0.95
amount   = trade_EUR / last_price
```

### Web-Dashboard
- Port 5001, Flask, liest alle db/*.db
- Auto-Refresh: 60s (Seite), 5s (Cards live via /api/bots)
- Gebühren: KRAKEN_FEE = 0.0026 (0.26% pro Order), P&L Netto
- Bot-Verwaltung: Start/Stop/Löschen, SL/TP manuell editierbar
- Endpunkte: GET /, /api/bots, /api/markets, POST /api/trade/.../sltp, /api/bot/start, /api/bot/stop, /api/bot/delete

### Bekannte Eigenheiten & Fixes
- ExecStart muss `/bin/sh -c '...'` nutzen (word-splitting für $BOT_ARGS)
- poll_seconds=60 + startup-delay pro Bot (Kraken Rate-Limit bei Mehrfach-Start)
- Kraken archiviert Market-Orders sofort → last_price als average injizieren (execution.py)
- chart.js lokal unter web/static/chart.min.js (CDN auf Pi nicht erreichbar)
- Vollständige Bug-History → /home/xxx/bot/CLAUDE.md

### Systemd
```bash
bash systemd/install.sh          # einmalig einrichten
sudo systemctl start tradingbot.target
sudo systemctl status 'tradingbot@*'
journalctl -u tradingbot@SNX_EUR -f
```

---

## Netzwerk / Remote-Zugriff

### WireGuard VPN (PiVPN)
- PiVPN + WireGuard installiert auf Pi (CAT)
- VPN-Subnetz: 10.244.199.0/24
- Client "OnePlus12": IP 10.244.199.2, Config: `/home/xxx/configs/OnePlus12.conf`
- WireGuard-Port: 51820 UDP (Router-Port-Forwarding eingerichtet ✓)
- Dashboard von außen: http://10.244.199.1:5001 (via VPN-Verbindung) ✓
- QR-Code anzeigen: `pivpn -qr OnePlus12`
- Config auf Windows kopieren: `scp xxx@CAT:/home/xxx/configs/OnePlus12.conf "C:\Users\Michael Neuner\Desktop\OnePlus12.conf"`

---

## README-Stand (bot/README.md)
Das README ist veraltet – beschreibt noch alten Stand (Port 5000 statt 5001, kein systemd-Target, kein Bot-Management via UI).
Muss aktualisiert werden mit: Port 5001, systemd-Target, bot.conf.d/, UI-Features (Start/Stop/Delete/SL-TP), WireGuard-Zugriff.

---

## Offene TODOs
- [ ] Trade-Daten sammeln (DB neu ab 23.02.2026) → dann Strategie analysieren
- [ ] RSI/Volume/ATR-Filter in strategy.py einbauen (nach Datenanalyse)
- [ ] README.md aktualisieren (Port, systemd, UI-Features, VPN-Zugriff)
- [ ] Git-History aufräumen / sinnvolle Commits
