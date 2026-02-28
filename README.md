# TradingBot

Automatisierter Krypto-Trading-Bot für Kraken auf dem Raspberry Pi.
Mehrere Bot-Instanzen laufen parallel – je eine pro Coin, je eine SQLite-DB.
Ein Supervisor erkennt das Marktregime und passt die Strategie dynamisch an.
Ein News-Agent überwacht Krypto-News und sendet Telegram-Alerts mit Bot-Steuerung.

---

## Architektur-Überblick

```
┌─────────────────────────────────────────────────────────┐
│                    Raspberry Pi                         │
│                                                         │
│  supervisor.py  ──→  alle db/*.db (Regime schreiben)   │
│                           ↑                             │
│  main.py (×N)   ←──  bot_state lesen + anwenden        │
│  BTC/EUR                  │                             │
│  ETH/EUR          SQLite  │  db/BTC_EUR.db              │
│  XRP/EUR   ...            │  db/ETH_EUR.db  ...         │
│                           ↓                             │
│  web/app.py     ──→  Dashboard :5001                    │
│  news_agent.py  ──→  Telegram                           │
└─────────────────────────────────────────────────────────┘
```

---

## Projektstruktur

```
bot/
├── main.py                  # Entry Point – eine Instanz pro Coin
├── supervisor.py            # Supervisor – Regime-Erkennung (alle 5 min)
├── news_agent.py            # Entry Point News-Agent
│
├── bot/
│   ├── config.py            # Alle Konfigurationsparameter (dataclasses)
│   ├── data_feed.py         # Marktdaten via CCXT (OHLCV, Balance, Orders)
│   ├── strategy.py          # SMA-Crossover + RSI-Filter + ATR
│   ├── regime.py            # ADX-basierte Regime-Erkennung (TREND/SIDEWAYS/VOLATILE)
│   ├── risk.py              # Dynamisches Position Sizing
│   ├── execution.py         # Order-Submit, Dry-Run, Post-Trade-Verify
│   ├── sl_tp.py             # Stop-Loss / Take-Profit Monitor (ATR-basiert)
│   ├── pyramid.py           # Pyramid-Nachkauf-Logik (News + Profit-Check)
│   ├── notify.py            # Telegram-Benachrichtigungen (Kauf/Verkauf/Pyramid)
│   ├── persistence.py       # SQLite (orders, trades, errors, bot_state, supervisor_log)
│   └── ops.py               # Logging, Retry/Backoff, Circuit Breaker
│
├── news/
│   ├── config.py            # NewsAgentConfig (dataclass)
│   ├── fetcher.py           # CryptoPanic, RSS, Google News, Twitter
│   ├── sentiment.py         # VADER + TextBlob Sentiment-Analyse
│   ├── agent.py             # Orchestrator: fetch → filter → score → alert
│   └── telegram_bot.py      # Telegram Bot mit Inline-Buttons
│
├── web/
│   ├── app.py               # Flask Dashboard (Port 5001)
│   └── templates/index.html # Dark-Theme UI, Multi-Bot, Live-Refresh
│
├── db/                      # SQLite-DBs (eine pro Bot + news.db)
├── db/archive/              # Archivierte DBs gelöschter Bots
├── logs/                    # Log-Dateien pro Bot + Supervisor + News
├── bot.conf.d/              # Konfiguration pro Bot-Instanz (systemd)
├── systemd/                 # Service-Dateien + install.sh
├── .env                     # API-Keys (nicht committen!)
├── .env.example             # Vorlage
└── requirements.txt
```

---

## Setup

```bash
# 1. Virtuelle Umgebung + Abhängigkeiten
python -m venv botvenv
source botvenv/bin/activate
pip install -r requirements.txt

# 2. API-Keys eintragen
cp .env.example .env
nano .env
```

**`.env` Variablen:**

| Variable | Pflicht | Beschreibung |
|----------|---------|--------------|
| `KRAKEN_API_KEY` | ✅ | Kraken API Key (nur Trade-Rechte, kein Withdraw!) |
| `KRAKEN_API_SECRET` | ✅ | Kraken API Secret |
| `TELEGRAM_BOT_TOKEN` | News-Agent | Token von @BotFather |
| `TELEGRAM_CHAT_ID` | News-Agent | Eigene Telegram User-ID |
| `CRYPTOPANIC_API_KEY` | optional | Kostenlos auf cryptopanic.com |
| `TWITTER_BEARER_TOKEN` | optional | Twitter/X Basic API (~$100/Monat) |

**Kraken API-Key Berechtigungen** (nur diese aktivieren):
- ✅ Query Funds
- ✅ Query Open Orders & Trades
- ✅ Create & Modify Orders
- ✅ Cancel/Close Orders
- ❌ Withdraw Funds – niemals!

---

## Strategie

Die Bots kombinieren drei Schichten:

### 1. SMA-Crossover (Signal-Generierung)
```
BUY  → Fast SMA (9) kreuzt Slow SMA (21) von unten nach oben
SELL → Fast SMA (9) kreuzt Slow SMA (21) von oben nach unten
HOLD → kein Crossover
```

### 2. RSI-Filter (Signal-Qualität)
Signale werden gefiltert wenn der Markt bereits überhitzt ist:
```
BUY  wird blockiert wenn RSI > rsi_buy_max  (Standard: 65 – überkauft)
SELL wird blockiert wenn RSI < rsi_sell_min (Standard: 35 – überverkauft)
```

### 3. ATR-basiertes SL/TP (Risikomanagement)
Stop-Loss und Take-Profit passen sich der aktuellen Volatilität an:
```
SL = entry − 1.5 × ATR(14)
TP = entry + 2.5 × ATR(14)
```
Bei zu wenig Daten: Fallback auf feste Prozentsätze (`--sl` / `--tp`).

### 4. Trailing Stop-Loss (optional)
Mit `--trailing-sl` folgt der Stop-Loss dem steigenden Kurs nach oben – Gewinne werden automatisch abgesichert:
```
trail = aktueller_preis × (1 − trailing_sl_pct)   # Standard: 2%
SL wird nur angehoben, nie abgesenkt
```

### 5. Volumen-Filter (optional)
Mit `--volume-filter` werden Crossover-Signale ignoriert, wenn das Handelsvolumen unterdurchschnittlich ist:
```
Signal nur wenn: letztes_volumen ≥ Avg(letzte 20 Candles) × volume_factor
```
Verhindert Fehlsignale in dünnen Märkten ohne Marktbewegung.

### 6. SL-Cooldown (optional)
Nach einem Stop-Loss wartet der Bot N Candles (`--sl-cooldown 3`, Standard: 3 = 15min bei 5m) bevor er wieder kauft.
Verhindert sofortigen Wiedereinstieg in einen weiter fallenden Markt.

### Positionsgröße
Das Kapital wird gleichmäßig auf alle aktiven Bots verteilt:
```
usable    = balance_EUR × (1 − safety_buffer)
per_bot   = usable / anzahl_aktive_bots
trade_EUR = per_bot × quote_risk_fraction  (0.95)
amount    = trade_EUR / aktueller_preis
```

---

## Supervisor – Marktregime-Erkennung

Der Supervisor läuft als separater Prozess und analysiert alle 5 Minuten
das Marktregime jedes Coins via **ADX** (Trendstärke) und **relative ATR** (Volatilität).
Die Bots übernehmen die angepassten Parameter beim nächsten Loop-Durchlauf **ohne Neustart**.

### Regime-Klassifikation

| Regime | Bedingung | RSI-Fenster | SL-Mult | TP-Mult |
|--------|-----------|-------------|---------|---------|
| **TREND** | ADX > 22, ATR% ≤ 3% | buy < 68, sell > 32 | 1.5× | 2.5× |
| **SIDEWAYS** | ADX ≤ 22, ATR% ≤ 3% | buy < 60, sell > 40 | 1.2× | 1.8× |
| **VOLATILE** | ATR% > 3% | buy < 55, sell > 45 | 2.0× | 3.5× |

### Multi-Varianten-Optimierung

Pro Supervisor-Durchlauf werden **24 Varianten** getestet (6 SMA-Kombinationen × 4 Feature-Kombos):

| Trailing SL | Volumen-Filter | SMA-Varianten |
|-------------|----------------|---------------|
| ❌ | ❌ | Scalp, Agile, Standard, MACD, Mittel, Swing |
| ✅ | ❌ | … × 6 |
| ❌ | ✅ | … × 6 |
| ✅ | ✅ | … × 6 |

Die Variante mit dem höchsten simulierten P&L gewinnt.
Wenn die optimale Kombo von der aktuellen Bot-Konfiguration abweicht, sendet der Supervisor
eine **Telegram-Empfehlung** – der Bot übernimmt sie aber nur, wenn kein CLI-Flag gesetzt ist.

```
🔬 Supervisor-Empfehlung: BTC/EUR
Strategie: Agile 7/18  Sim-P&L: +3.2% (5 Trades)
Trailing SL: ✅ empfohlen  (aktuell: ❌)
Volumen-Filter: ❌ empfohlen  (aktuell: ❌)
→ Neustart mit: --trailing-sl  um zu übernehmen
```

### Cross-Bot-Learning

Nach jedem Optimierungsdurchlauf prüft der Supervisor, ob die beste Strategie eines Coins
auch auf anderen Coins im **gleichen Regime** besser abschneidet.
Wenn ja, wird die Strategie übernommen und als `"Agile→BTC"` gekennzeichnet.

```
BTC (TREND): Agile 7/18 → +3.2%
XRP (TREND): Swing 21/55 → +0.8%
→ XRP übernimmt "Agile" von BTC  (validiert auf XRP-Candles: +2.1% > +0.8%)
```

### Supervisor-Erfahrung (supervisor_log)

Jeder Durchlauf wird append-only in `supervisor_log` persistiert (eine Tabelle pro Bot-DB):

| Spalte | Inhalt |
|--------|--------|
| `regime` | TREND / SIDEWAYS / VOLATILE |
| `adx` | ADX-Wert zum Zeitpunkt |
| `strategy_name` | z.B. `Agile`, `Swing`, `Agile→BTC` |
| `fast / slow` | Gewählte SMA-Parameter |
| `sim_pnl` | Simulierter P&L (Backtest) |
| `source` | `own` oder `cross:BTC` |
| `use_trailing_sl` | Trailing SL aktiv bei dieser Variante |
| `volume_filter` | Volumen-Filter aktiv bei dieser Variante |

Abfrage via Telegram: `/supervisor` (Übersicht) oder `/supervisor BTC/EUR` (Verlauf).

### Supervisor starten

```bash
# Testen (liest DBs, kein Schreiben)
botvenv/bin/python supervisor.py --dry-run

# Live (alle 5 Minuten)
botvenv/bin/python supervisor.py

# oder via systemd:
sudo systemctl start tradingbot-supervisor
journalctl -u tradingbot-supervisor -f
```

| Option | Standard | Beschreibung |
|--------|----------|--------------|
| `--interval` | `300` | Sekunden zwischen Durchläufen |
| `--timeframe` | `5m` | Candle-Timeframe für ADX/ATR |
| `--candles` | `100` | Anzahl Candles (min. 30 für ADX) |
| `--dry-run` | – | Nur loggen, nicht in DB schreiben |

---

## Bot starten

```bash
# Dry-Run – kein echter Handel, zum Testen
botvenv/bin/python main.py --symbol BTC/EUR --dry-run

# Live
botvenv/bin/python main.py --symbol BTC/EUR
botvenv/bin/python main.py --symbol ETH/EUR --sl 0.025 --tp 0.05
```

### CLI-Optionen

| Option | Standard | Beschreibung |
|--------|----------|--------------|
| `--symbol` | – | Coin-Paar, z.B. `BTC/EUR` (Pflicht) |
| `--timeframe` | `5m` | Kerzen-Intervall |
| `--fast` | `9` | Fast-SMA-Periode |
| `--slow` | `21` | Slow-SMA-Periode |
| `--sl` | `0.03` | Stop-Loss Fallback (3%), wenn ATR nicht berechenbar |
| `--tp` | `0.06` | Take-Profit Fallback (6%) |
| `--safety-buffer` | `0.10` | Anteil des Kapitals der nie angefasst wird |
| `--startup-delay` | `0` | Verzögerter Start in Sekunden (Rate-Limit staffeln) |
| `--trailing-sl` | – | Trailing Stop-Loss aktivieren |
| `--trailing-sl-pct` | `0.02` | Abstand des Trailing-SL (2% = 2% unter aktuellem Kurs) |
| `--sl-cooldown` | `3` | Candles Wartezeit nach SL-Hit bevor nächster Kauf |
| `--volume-filter` | – | Volumen-Filter aktivieren |
| `--volume-factor` | `1.2` | Signal nur bei ≥ 1.2× Durchschnittsvolumen |
| `--dry-run` | – | Kein echter Handel |

---

## Web-Dashboard

```bash
botvenv/bin/python web/app.py
# → http://<ip>:5001
```

- Zeigt alle Bot-Instanzen automatisch (liest alle `db/*.db`)
- **Auto-Refresh**: 60s (Seite), 5s (Cards live via `/api/bots`)
- **Regime-Badge** pro Bot: TREND / SIDEWAYS / VOLATILE mit Farbe
- **Bot-Verwaltung**: Hinzufügen / Starten / Stoppen / Löschen im Browser
- **SL/TP editierbar**: ± Buttons mit adaptiver Schrittweite (~1.50€ P&L pro Klick)
- **P&L-Anzeige**: Netto nach Kraken-Gebühren (0.26% pro Order)
- **RSI-Anzeige**: Farbe je nach Überkauft/Überverkauft-Status

---

## systemd (Raspberry Pi – empfohlen)

```bash
# Einmalig einrichten
bash systemd/install.sh

# Alles starten (Bots + Web + Supervisor + News-Agent)
sudo systemctl start tradingbot.target

# Einzelne Services
sudo systemctl start tradingbot-supervisor
sudo systemctl start news-agent

# Status
sudo systemctl status 'tradingbot@*'
sudo systemctl status tradingbot-supervisor

# Logs
journalctl -u tradingbot@BTC_EUR -f
journalctl -u tradingbot-web -f
journalctl -u tradingbot-supervisor -f
journalctl -u news-agent -f
```

### Bot-Konfiguration (`bot.conf.d/`)

Jede Datei `SYMBOL.conf` aktiviert eine Bot-Instanz beim systemd-Start:

```ini
# bot.conf.d/BTC_EUR.conf
BOT_SYMBOL=BTC/EUR
BOT_ARGS=--timeframe 5m --fast 9 --slow 21 --sl 0.02 --tp 0.04 --safety-buffer 0.10 --startup-delay 20
```

Die `--startup-delay` Werte staffeln API-Calls beim gleichzeitigen Start:

| Symbol | Delay |
|--------|-------|
| BTC/EUR | 20s |
| XRP/EUR | 80s |
| ETH/EUR | 100s |

---

## Pyramid-Nachkaufen

Der Bot kann eine offene Position automatisch aufstocken (Pyramiding), wenn alle Bedingungen gleichzeitig erfüllt sind:

| Bedingung | Schwelle |
|-----------|---------|
| Position im Gewinn | ≥ 1.5% |
| Marktregime | TREND oder SIDEWAYS (nicht VOLATILE) |
| Nachrichten-Sentiment (letzte 4h) | Score ≥ 0.4 (bullish) |
| Bisherige Nachkäufe im Trade | 0 (max. 1 Nachkauf pro Trade) |

**Nachkauf-Größe:** 25% der normalen Positionsgröße.
**Nach dem Kauf:** Gewichteter Avg-Entry, neue SL/TP werden berechnet und in der DB aktualisiert.

```
Offene Position: 0.01 BTC @ 85.000 EUR  (+2.3%)
News-Sentiment:  +0.62 (bullish)
Regime:          TREND
→ Pyramid-Kauf: +0.0025 BTC @ 86.950 EUR
→ Neuer Avg-Entry: 85.390 EUR  SL: 84.100  TP: 88.200
```

---

## Trade-Benachrichtigungen

Alle Kauf-/Verkaufs-Events werden automatisch per Telegram gemeldet:

| Event | Nachricht |
|-------|-----------|
| Kauf | 🟢 KAUF BTC/EUR – Menge @ Preis · SL / TP mit % |
| Verkauf (Signal) | 📉 VERKAUF BTC/EUR – Menge @ Preis |
| Stop-Loss | 🛑 STOP-LOSS BTC/EUR – P&L netto |
| Take-Profit | 💰 TAKE-PROFIT BTC/EUR – P&L netto |
| Pyramid | 🔺 NACHKAUF BTC/EUR – neuer Avg-Entry |

---

## News-Agent

Überwacht Krypto-News aus 10+ Quellen, berechnet Sentiment-Scores
und sendet bei relevanten Ereignissen Telegram-Alerts mit Inline-Buttons zur Bot-Steuerung.

### Filter-Pipeline (in Reihenfolge)

```
gefetcht → [Qualität] → [Alter] → [URL-Dedup] → [Titel-Dedup] → [Relevanz] → [Schwelle] → Alert
```

| Filter | Standard | Beschreibung |
|--------|----------|--------------|
| Qualität | ≥ 5 Wörter | Reddit-Posts / Platzhalter rausfiltern |
| Alter | ≤ 48h | `published_at` muss aktuell sein |
| URL-Dedup | 24h | Gleiche URL nicht erneut alerten |
| Titel-Dedup | 4h / 50% | Gleiche Story von anderen Outlets unterdrücken (Jaccard) |
| Relevanz | – | Muss Coin-Keyword oder Watchword enthalten |
| Schwelle | 0.5 | `|sentiment_score|` muss Schwelle überschreiten |

### Quellen

**RSS-Feeds (kostenlos):**
CoinTelegraph · Decrypt · CoinDesk · Bitcoin Magazine · Crypto Slate ·
Blockworks · NewsBTC · CryptoNews · Reddit r/CryptoCurrency · Reddit r/Bitcoin

**Google News RSS (kostenlos):**
bitcoin · crypto regulation · cryptocurrency hack · ethereum · trump crypto ·
XRP ripple SEC · crypto ETF approval · DeFi exploit · bitcoin whale

**API (optional):**
CryptoPanic (`CRYPTOPANIC_API_KEY`) · Twitter/X (`TWITTER_BEARER_TOKEN`)

### Sentiment-Scoring

- **VADER** (70%) + **TextBlob** (30%) → kombinierter Score −1.0 bis +1.0
- `bearish` < −0.3 · `neutral` −0.3…+0.3 · `bullish` > +0.3

### News-Agent starten

```bash
# Testen
botvenv/bin/python news_agent.py --dry-run --once

# Telegram-Verbindung testen
botvenv/bin/python news_agent.py --test-telegram

# Live
botvenv/bin/python news_agent.py
# oder:
sudo systemctl start news-agent
```

### Telegram-Befehle

| Befehl | Beschreibung |
|--------|--------------|
| `/status` | Alle Bots: Status, Signal, Regime + Gesamt-Balance (Frei/Coins/Total) |
| `/portfolio` | Offene Positionen: Entry, Jetzt-Preis, P&L EUR+%, SL/TP mit Abstand |
| `/rendite` | Rentabilität: Win-Rate, Gesamt-P&L, beste/schlechteste Trades, Sim-P&L |
| `/holdings` | Alle Coins auf Kraken (Menge × Preis, sortiert nach EUR-Wert) |
| `/supervisor` | Supervisor-Übersicht: letztes Regime/Strategie/Sim-P&L pro Bot |
| `/supervisor BTC/EUR` | Detailverlauf: Regime-Verteilung, Top-Strategien, Cross-Bot-Events |
| `/params BTC/EUR` | Parameter eines Bots: SMA, RSI, ATR, Regime, Fallback SL/TP |
| `/start_bot BTC/EUR` | Bot starten |
| `/stop_bot BTC/EUR` | Bot stoppen |
| `/stop_all` | Alle laufenden Bots sofort stoppen |
| `/buy BTC/EUR` | Force-BUY beim nächsten Loop (manueller Kauf) |
| `/sell BTC/EUR` | Force-SELL beim nächsten Loop (Position schließen) |
| `/set_sl BTC/EUR 2.0` | Stop-Loss auf 2% unter Entry-Preis setzen |
| `/set_tp BTC/EUR 4.0` | Take-Profit auf 4% über Entry-Preis setzen |

Alle Befehle funktionieren auch als **Freitext** ohne `/` – der Bot erkennt Intents per Regex:
`status` · `portfolio` · `rendite` · `holdings` · `erfahrung` · `stoppe BTC` · `starte ETH` · `kauf BTC` · `sl BTC 2`

### Alert-Inline-Buttons

| Button | Wann | Aktion |
|--------|------|--------|
| `🛑 BTC/EUR stoppen` | Bearish-Alert, Bot läuft | POST /api/bot/stop |
| `▶ ETH/EUR starten` | Bullish-Alert, Bot gestoppt | POST /api/bot/start |
| `✅ Ignorieren` | Immer | Alert als dismissed markieren (24h Cooldown) |

---

## Remote-Zugriff via WireGuard VPN

```bash
# VPN-Client hinzufügen
pivpn add
pivpn -qr <Name>   # QR-Code für Handy

# Status
sudo wg show
```

Nach VPN-Verbindung: `http://<pi-vpn-ip>:5001` im Browser.

---

## Sicherheit

- Kraken API-Keys mit minimalen Rechten (kein Withdraw)
- `.env` ist gitignored – niemals committen
- **Circuit Breaker**: Bot stoppt nach 5 konsekutiven Fehlern automatisch
- `NoNewPrivileges=true` in Bot-Services (`tradingbot@.service`); im Web-Service entfernt (sonst blockiert polkit/sudo)
- `/etc/sudoers.d/tradingbot`: User `xxx` darf `systemctl stop|start|restart tradingbot@*` ohne Passwort (für Dashboard-Steuerung)
- Supervisor schreibt nur `supervisor_*`-Keys in Bot-DBs, greift nie in Orders ein
