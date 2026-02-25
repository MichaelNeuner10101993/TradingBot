# TradingBot

Automatisierter Krypto-Trading-Bot für Kraken mit Web-Dashboard und News-Agent.
Strategie: SMA-Crossover mit synthetischem Stop-Loss / Take-Profit.
Mehrere Bot-Instanzen parallel, je eine pro Coin, je eine SQLite-DB.

---

## Projektstruktur

```
bot/
├── main.py                  # Entry Point – eine Instanz pro Coin
├── bot/
│   ├── config.py            # Alle Konfigurationsparameter (dataclasses)
│   ├── data_feed.py         # Marktdaten via CCXT (OHLCV, Balance, Orders)
│   ├── strategy.py          # SMA-Crossover → BUY / SELL / HOLD
│   ├── risk.py              # Dynamisches Position Sizing
│   ├── execution.py         # Order-Submit, Dry-Run, Post-Trade-Verify
│   ├── sl_tp.py             # Stop-Loss / Take-Profit Monitor
│   ├── persistence.py       # SQLite (orders, trades, errors, bot_state)
│   └── ops.py               # Logging, Retry/Backoff, Circuit Breaker
├── news/
│   ├── config.py            # NewsAgentConfig (dataclass)
│   ├── fetcher.py           # CryptoPanic, RSS, Google News, Twitter
│   ├── sentiment.py         # VADER + TextBlob Sentiment-Analyse
│   ├── agent.py             # Orchestrator: fetch → dedupe → score → alert
│   └── telegram_bot.py      # Telegram Bot mit Inline-Buttons
├── web/
│   ├── app.py               # Flask Dashboard (Port 5001)
│   └── templates/index.html # Dark-Theme UI, Multi-Bot, Auto-Refresh
├── db/                      # SQLite-DBs (eine pro Bot + news.db)
├── db/archive/              # Archivierte DBs
├── logs/                    # Log-Dateien pro Bot
├── bot.conf.d/              # Konfiguration pro Bot-Instanz (systemd)
├── systemd/                 # Service-Dateien + install.sh
├── news_agent.py            # Entry Point News-Agent
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

## Bot starten

```bash
# Dry-Run (kein echter Handel, zum Testen)
python main.py --symbol BTC/EUR --dry-run

# Live
python main.py --symbol BTC/EUR
python main.py --symbol ETH/EUR --sl 0.025 --tp 0.05
```

### CLI-Optionen

| Option | Standard | Beschreibung |
|--------|----------|--------------|
| `--symbol` | – | Coin-Paar, z.B. `BTC/EUR` (Pflicht) |
| `--timeframe` | `5m` | Kerzen-Intervall |
| `--fast` | `9` | Fast-SMA-Periode |
| `--slow` | `21` | Slow-SMA-Periode |
| `--sl` | `0.03` | Stop-Loss (3%) |
| `--tp` | `0.06` | Take-Profit (6%) |
| `--safety-buffer` | `0.10` | Anteil des Kapitals der nie angefasst wird |
| `--startup-delay` | `0` | Verzögerter Start in Sekunden (Kraken Rate-Limit) |
| `--dry-run` | – | Kein echter Handel |

---

## Web-Dashboard

```bash
python web/app.py
# → http://<ip>:5001
```

- Zeigt alle Bot-Instanzen automatisch (liest alle `db/*.db`)
- Auto-Refresh: 60s (Seite), 5s (Cards via API)
- **Bot-Verwaltung**: Hinzufügen / Starten / Stoppen / Löschen direkt im Browser
- **SL/TP editierbar**: ± Buttons mit adaptiver Schrittweite (~1.50€ P&L pro Klick)
- P&L-Anzeige: Netto nach Kraken-Gebühren (0.26% pro Order)

---

## systemd (Raspberry Pi – empfohlen)

```bash
# Einmalig einrichten (ersetzt DEIN_USER/DEIN_BOTDIR)
bash systemd/install.sh

# Starten
sudo systemctl start tradingbot.target
sudo systemctl start news-agent

# Status
sudo systemctl status 'tradingbot@*'
sudo systemctl status news-agent

# Logs
journalctl -u tradingbot@BTC_EUR -f
journalctl -u tradingbot-web -f
journalctl -u news-agent -f
```

### Bot-Konfiguration (`bot.conf.d/`)

Jede Datei `bot.conf.d/SYMBOL.conf` aktiviert eine Bot-Instanz beim Start:

```ini
# bot.conf.d/BTC_EUR.conf
BOT_SYMBOL=BTC/EUR
BOT_ARGS=--timeframe 5m --fast 9 --slow 21 --sl 0.02 --tp 0.04 --safety-buffer 0.10 --startup-delay 20
```

---

## News-Agent

Überwacht Krypto-News (RSS, Google News, CryptoPanic, optional Twitter),
berechnet Sentiment-Scores und sendet bei relevanten Ereignissen Telegram-Alerts
mit Inline-Buttons zur Bot-Steuerung.

```bash
# Testen (kein Telegram)
python news_agent.py --dry-run --once

# Telegram-Verbindung testen
python news_agent.py --test-telegram

# Dauerhaft starten
python news_agent.py
# oder via systemd:
sudo systemctl start news-agent
```

### CLI-Optionen

| Option | Beschreibung |
|--------|--------------|
| `--dry-run` | Fetch + Log, kein Telegram |
| `--once` | Einmaliger Cycle, dann Exit |
| `--test-telegram` | Sendet Test-Nachricht, dann Exit |
| `--interval MINUTEN` | Poll-Interval (Standard: 10) |
| `--threshold SCORE` | Sentiment-Schwelle 0.0–1.0 (Standard: 0.5) |

### Telegram-Buttons

| Button | Wann | Aktion |
|--------|------|--------|
| `🛑 BTC/EUR stoppen` | Bearish-Alert, Bot läuft | POST /api/bot/stop |
| `▶ ADA/EUR starten` | Bullish-Alert, Bot läuft nicht | POST /api/bot/start |
| `✅ Ignorieren` | Immer | Alert als dismissed markieren |
| `/status` | Jederzeit | Zeigt alle Bot-Stati |

### Sentiment-Scoring

- **VADER** (70%) + **TextBlob** (30%) → kombinierter Score −1.0 bis +1.0
- `bearish` < −0.5 | `neutral` −0.5…+0.5 | `bullish` > +0.5
- Quellen: CryptoPanic API, RSS (CoinTelegraph, Decrypt, CoinDesk), Google News, Twitter (optional)
- Deduplizierung: gleiche URL löst 24h keinen zweiten Alert aus

---

## Strategie – SMA-Crossover

```
BUY  → Fast SMA (9) kreuzt Slow SMA (21) von unten nach oben
SELL → Fast SMA (9) kreuzt Slow SMA (21) von oben nach unten
HOLD → kein Crossover
```

Nach jedem BUY: synthetischer Stop-Loss + Take-Profit wird gesetzt und
in jeder Loop-Iteration gegen den aktuellen Preis geprüft.

### Positionsgröße

```
usable    = balance_EUR × (1 − safety_buffer)   # z.B. × 0.90
per_bot   = usable / anzahl_aktive_bots
trade_EUR = per_bot × quote_risk_fraction        # z.B. × 0.95
amount    = trade_EUR / aktueller_preis
```

---

## Remote-Zugriff via WireGuard VPN

```bash
# VPN-Client hinzufügen
pivpn add

# QR-Code für Handy anzeigen
pivpn -qr <Name>

# Status
sudo wg show
```

Nach VPN-Verbindung: `http://10.244.199.1:5001` im Browser.

---

## Sicherheit

- Kraken API-Keys mit minimalen Rechten (kein Withdraw)
- `.env` ist gitignored
- Circuit Breaker: Bot stoppt nach 5 konsekutiven Fehlern
- `NoNewPrivileges=true` in allen systemd-Services
