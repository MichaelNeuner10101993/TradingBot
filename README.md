# Trading Bot

Automatisierter Krypto-Trading-Bot für Kraken mit Web-Dashboard.
Strategie: SMA-Crossover mit synthetischem Stop-Loss / Take-Profit.

---

## Projektstruktur

```
TradingBot/
├── main.py                  # Entry Point – eine Instanz pro Coin
├── bot/
│   ├── config.py            # Alle Konfigurationsparameter
│   ├── data_feed.py         # Marktdaten via CCXT (OHLCV, Balance, Orders)
│   ├── strategy.py          # SMA-Crossover → BUY / SELL / HOLD
│   ├── risk.py              # Position Sizing, Guardrails
│   ├── execution.py         # Order-Submit, Dry-Run, Post-Trade-Verify
│   ├── sl_tp.py             # Stop-Loss / Take-Profit Monitor
│   ├── persistence.py       # SQLite (orders, trades, errors, bot_state)
│   └── ops.py               # Logging, Retry/Backoff, Circuit Breaker
├── web/
│   ├── app.py               # Flask Dashboard (Multi-Bot)
│   └── templates/
│       └── index.html       # Dark-Theme UI, Auto-Refresh 15s
├── db/                      # SQLite-DBs (eine pro Instanz, auto-erstellt)
├── logs/                    # Log-Dateien (eine pro Instanz, auto-erstellt)
├── .env                     # API-Keys (nicht committen!)
├── .env.example             # Vorlage
└── requirements.txt
```

---

## Setup

```bash
# 1. Abhängigkeiten installieren
pip install -r requirements.txt

# 2. API-Keys eintragen
cp .env.example .env
# .env bearbeiten: KRAKEN_API_KEY und KRAKEN_API_SECRET setzen
```

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
python main.py --symbol SNX/EUR --dry-run

# Live
python main.py --symbol SNX/EUR
python main.py --symbol BTC/EUR --timeframe 1m --fast 5 --slow 13
python main.py --symbol ETH/EUR --sl 0.025 --tp 0.05
```

### Alle CLI-Optionen

| Option | Beispiel | Beschreibung |
|--------|----------|--------------|
| `--symbol` | `SNX/EUR` | Coin-Paar (Pflicht) |
| `--timeframe` | `5m` | Kerzen-Intervall (default: 5m) |
| `--fast` | `9` | Fast-SMA-Periode (default: 9) |
| `--slow` | `21` | Slow-SMA-Periode (default: 21) |
| `--sl` | `0.03` | Stop-Loss 3% (default: 0.03) |
| `--tp` | `0.06` | Take-Profit 6% (default: 0.06) |
| `--dry-run` | – | Kein echter Handel |
| `--live` | – | Echter Handel |

---

## Web-Dashboard

```bash
python web/app.py
```

Erreichbar unter `http://<ip>:5000`
Zeigt alle laufenden Instanzen automatisch (liest alle `db/*.db`).

---

## Konfiguration (`bot/config.py`)

```python
class BotConfig:
    symbol:       "SNX/EUR"   # Coin-Paar
    timeframe:    "5m"        # Kerzen-Intervall
    fast_period:  9           # Fast SMA
    slow_period:  21          # Slow SMA
    poll_seconds: 20          # Polling-Intervall
    dry_run:      False       # True = kein echter Handel

class RiskConfig:
    quote_risk_fraction: 0.95  # 95% des Bestands pro Trade
    max_open_orders:     1
    min_order_quote:     10.0  # Mindestorder in Quote-Währung (€)
    max_consecutive_errors: 5  # Circuit Breaker
    stop_loss_pct:       0.03  # 3% Stop-Loss
    take_profit_pct:     0.06  # 6% Take-Profit
```

---

## Als systemd-Service (Raspberry Pi)

Datei: `/etc/systemd/system/tradingbot@.service`

```ini
[Unit]
Description=Trading Bot – %i
After=network.target

[Service]
User=xxx
WorkingDirectory=/home/xxx/bot
EnvironmentFile=/home/xxx/bot/.env
ExecStart=/home/xxx/bot/botvenv/bin/python /home/xxx/bot/main.py --symbol %i
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
TimeoutStopSec=20
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
```

```bash
# Mehrere Instanzen
sudo systemctl enable --now tradingbot@SNX_EUR
sudo systemctl enable --now tradingbot@BTC_EUR
sudo systemctl enable --now tradingbot@ETH_EUR

# Logs
journalctl -u tradingbot@SNX_EUR -f
journalctl -u "tradingbot@*" -f
```

Web-Dashboard-Service: `/etc/systemd/system/tradingbot-web.service`

```ini
[Unit]
Description=Trading Bot Web Dashboard
After=network.target

[Service]
User=xxx
WorkingDirectory=/home/xxx/bot
EnvironmentFile=/home/xxx/bot/.env
ExecStart=/home/xxx/bot/botvenv/bin/python /home/xxx/bot/web/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tradingbot-web
```

---

## Strategie – SMA-Crossover

```
BUY  → Fast SMA (9) kreuzt Slow SMA (21) von unten nach oben
SELL → Fast SMA (9) kreuzt Slow SMA (21) von oben nach unten
HOLD → kein Crossover
```

Nach jedem BUY wird automatisch ein synthetischer Stop-Loss und Take-Profit gesetzt.
Der Bot prüft diese in jeder Iteration gegen den aktuellen Preis.

---

## Sicherheit

- API-Keys mit minimalen Rechten (kein Withdraw)
- Separate Keys pro Bot-Umgebung empfohlen
- Circuit Breaker stoppt den Bot nach 5 konsekutiven Fehlern
- `.env` ist gitignored
