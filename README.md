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
│   ├── indicators.py        # numpy-Indikator-Library (RSI/EMA/ATR/ADX/BB/VWAP/MACD)
│   ├── strategy.py          # SMA-Crossover + RSI-Filter + ATR + HTF-Filter
│   ├── regime.py            # 5-Regime-Erkennung (BULL/BEAR/SIDEWAYS/VOLATILE/EXTREME)
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
│   └── templates/index.html # Dark-Theme UI, Logo, Hover-Tooltips, Live-Refresh
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
| `ANTHROPIC_API_KEY` | empfohlen | Für KI-Freitext im Telegram-Bot (console.anthropic.com) |
| `CRYPTOPANIC_API_KEY` | optional | Kostenlos auf cryptopanic.com |
| `TWITTER_BEARER_TOKEN` | optional | Twitter/X Basic API (~$100/Monat) |
| `PEERS` | optional | Peer-Pi-Instanzen für Cross-Instance Learning, z.B. `http://10.8.0.2:5001,http://10.8.0.3:5001` |

**Kraken API-Key Berechtigungen** (nur diese aktivieren):
- ✅ Query Funds
- ✅ Query Open Orders & Trades
- ✅ Create & Modify Orders
- ✅ Cancel/Close Orders
- ❌ Withdraw Funds – niemals!

---

## Strategie

Die Bots kombinieren mehrere Filter-Schichten die nacheinander geprüft werden.
Ein Signal muss **alle** aktivierten Filter passieren um ausgeführt zu werden.

```
OHLCV-Daten
    │
    ▼
SMA-Crossover (BUY / SELL / HOLD)
    │
    ▼  ← RSI-Filter (blockiert Käufe bei überkauftem Markt)
    │
    ▼  ← HTF-Trend-Filter (blockiert Käufe gegen übergeordneten Trend)
    │
    ▼  ← BEAR-Regime (Supervisor erkennt Abwärtstrend → BUY unterdrückt)
    │
    ▼  ← Sentiment Auto-Stop (Score < Schwelle → Bot pausiert)
    │
    ▼  ← Sentiment SELL-Trigger (Score < Schwelle → SELL / BUY sperren / beides)
    │
    ▼  ← Sentiment BUY-Gate (Score < Min → BUY → HOLD)
    │
    ▼  ← Volumen-Filter (blockiert Signale bei unterdurchschnittlichem Volumen)
    │
    ▼  ← SL-Cooldown (blockiert Käufe kurz nach einem Stop-Loss)
    │
    ▼  ← Fee-Gate (kein Trade wenn TP-Abstand < 1.5× Roundtrip-Gebühr, ~0.78%)
    │
    ▼
ORDER
    │
    ▼  ← Trailing-SL (SL folgt steigendem Kurs nach oben)
    │
    ▼  ← Breakeven-SL (SL auf Entry heben sobald genug Gewinn)
    │
    ▼  ← Partial-TP (bei erstem TP-Hit nur Teil verkaufen, Rest läuft weiter)
    │
    ▼  ← Drawdown-Stopp (≥10%: Position-Sizing halbiert; ≥15%: alle Käufe pausiert)
```

---

### 1. SMA-Crossover (Signal-Generierung)

```
BUY  → Fast SMA (9) kreuzt Slow SMA (21) von unten nach oben
SELL → Fast SMA (9) kreuzt Slow SMA (21) von oben nach unten
HOLD → kein Crossover
```

**`--fast N`** – Periode des schnellen SMA (Standard: `9`)
- **Kleiner (z.B. 5):** Sehr reaktiv – erkennt Trends früher, erzeugt aber mehr Fehlsignale in seitwärts bewegenden Märkten.
- **Größer (z.B. 15–20):** Träger – weniger Fehlsignale, steigt aber später ein und aus.
- Faustregel: `fast` sollte ¼ bis ½ von `slow` betragen.

**`--slow N`** – Periode des langsamen SMA (Standard: `21`)
- **Kleiner (z.B. 12–15):** Kurzer Betrachtungszeitraum, mehr Crossovers, passt zu volatilen Coins.
- **Größer (z.B. 50–200):** Klassische Trendfolge (50/200 = „Golden Cross"), sehr wenige aber zuverlässigere Signale. Für 5m-Timeframe eher ungeeignet.
- Bekannte Kombinationen: `9/21` (Standard), `5/15` (Scalp), `7/18` (Agile), `12/26` (MACD-ähnlich), `21/55` (Swing).

---

### 2. RSI-Filter (Signal-Qualität)

Signale werden gefiltert wenn der Markt bereits überhitzt ist. Verhindert Käufe an Hochpunkten und Verkäufe an Tiefpunkten.

```
BUY  wird blockiert wenn RSI > rsi_buy_max  (Standard: 65 – überkauft)
SELL wird blockiert wenn RSI < rsi_sell_min (Standard: 35 – überverkauft)
```

**`rsi_buy_max`** – Obere RSI-Grenze für BUY-Signale (vom Supervisor gesetzt)
- **50–55:** Sehr konservativ – kauft nur bei klar nicht-überkauftem Markt. Viele Signale werden geblockt, aber die verbleibenden sind qualitativ hochwertig.
- **65 (Standard):** Ausgewogene Mitte – lässt moderate Aufwärtsmomente durch, blockiert klare Überhitzung.
- **70–75:** Permissiv – kauft auch in überkauften Momenten. Nützlich in starken Trend-Märkten, riskant in Seitwärtsphasen.

**`rsi_sell_min`** – Untere RSI-Grenze für SELL-Signale (vom Supervisor gesetzt)
- **25–30:** Konservativ – verkauft kaum wenn der Markt bereits überverkauft ist (gut, um Panikverkäufe zu vermeiden).
- **35 (Standard):** Standard-Schwelle.
- **45–50:** Aggressiv – verkauft auch bei neutralem RSI.

> Der RSI-Filter kann **nicht direkt** per CLI gesetzt werden. Der Supervisor passt ihn automatisch je nach Regime an — siehe Regime-Erkennung unten.

---

### 3. ATR-basiertes SL/TP

Stop-Loss und Take-Profit passen sich der aktuellen Volatilität an.
ATR (Average True Range) misst die durchschnittliche Kursbewegung der letzten 14 Candles.

```
SL = entry − atr_sl_mult × ATR(14)
TP = entry + atr_tp_mult × ATR(14)
```

**`--sl N` / `--tp N`** – Fallback-Prozentsätze wenn ATR nicht berechenbar ist (zu wenig Daten beim Start)
- `--sl 0.03` = 3% unter Entry → Stop-Loss
- `--tp 0.06` = 6% über Entry → Take-Profit
- Als Faustregel: TP sollte mindestens das 1.5-fache von SL sein (Chance:Risiko ≥ 1.5:1).

**`atr_sl_mult`** – ATR-Multiplikator für Stop-Loss (Standard: `1.5`, vom Supervisor angepasst)
- **0.8–1.0:** Sehr enger SL – wird in volatilen Märkten häufig ausgestoppt. Geeignet für ruhige, gut-trendende Märkte.
- **1.5 (Standard):** Gibt dem Trade genug Raum für normale Kursschwankungen.
- **2.0–3.0:** Weiter SL – der Verlust pro Trade ist größer, aber der Trade wird seltener vorzeitig gestoppt.

**`atr_tp_mult`** – ATR-Multiplikator für Take-Profit (Standard: `2.5`, vom Supervisor angepasst)
- **1.5–2.0:** Nimmt Gewinne schnell mit – gut in Seitwärtsmärkten, da der Kurs oft wieder zurückkommt.
- **2.5 (Standard):** Ausgewogen. Risk:Reward = 2.5/1.5 ≈ 1.67:1.
- **3.0–5.0:** Wartet auf große Bewegungen – gut in starken Trendbewegungen (VOLATILE-Regime), aber TP wird seltener erreicht.

> Supervisor passt beide Multiplikatoren automatisch je nach Regime an — siehe Regime-Erkennung unten.

---

### 4. Trailing Stop-Loss (optional)

```bash
--trailing-sl [--trailing-sl-pct 0.02]
```

Der SL wird automatisch nach oben gezogen wenn der Kurs steigt.
**Wichtig:** Der SL wird nur angehoben, nie abgesenkt.

```
trail = aktueller_preis × (1 − trailing_sl_pct)
Beispiel: Kurs steigt auf 100 EUR, pct=2% → SL wandert auf 98 EUR
          Kurs fällt danach auf 98 → Stop-Loss ausgelöst
```

**`--trailing-sl-pct N`** – Abstand des Trailing-SL vom aktuellen Kurs (Standard: `0.02` = 2%)
- **0.005–0.01 (0.5–1%):** Sehr enger Trailing-SL – der Bot sichert Gewinne sehr früh ab. Bei normaler Volatilität wird man häufig ausgestoppt bevor der Trend endet. Geeignet für sehr schnelle Scalp-Strategien.
- **0.02 (2%, Standard):** Guter Mittelwert für 5m-Coins wie BTC/ETH – gibt dem Kurs Raum für normale Schwankungen.
- **0.03–0.05 (3–5%):** Weiter Abstand – der Trade läuft länger durch, gibt aber mehr Gewinn zurück bevor der SL auslöst. Für volatile Coins (XRP, SNX) sinnvoll.
- **> 0.05:** Zu weit – kein wesentlicher Unterschied zum festen SL.

> **Tipp:** Trailing-SL kombiniert sich gut mit Breakeven-SL: Erst SL auf Entry schieben (Breakeven), dann mit Trailing-SL den Gewinn schützen.

---

### 5. Breakeven-SL (optional)

```bash
--breakeven [--breakeven-pct 0.01]
```

Sobald ein offener Trade einen Mindestgewinn erreicht, wird der Stop-Loss automatisch auf den Entry-Preis angehoben.
Der Trade kann dann im schlimmsten Fall **nicht mehr mit Verlust** enden.

```
Beispiel: Entry bei 100 EUR, breakeven_pct=1%
→ Kurs steigt auf 101 EUR (+1%) → SL wird auf 100 EUR gesetzt
→ Selbst wenn der Kurs zurückfällt: kein Verlust
```

**`--breakeven-pct N`** – Mindestgewinn (als Dezimalzahl) der den Breakeven-SL auslöst (Standard: `0.01` = 1%)
- **0.003–0.005 (0.3–0.5%):** Sehr früher Breakeven – der SL wird schon bei minimalem Gewinn auf Entry gesetzt. Viele Trades enden mit 0% statt kleinem Gewinn, aber das Verlustrisiko ist minimal.
- **0.01 (1%, Standard):** Auslösung nach 1% Gewinn. Gibt dem Trade kurz Luft, setzt dann aber schnell die Absicherung.
- **0.02–0.03 (2–3%):** Breakeven erst nach größerem Gewinn – der Trade kann zwischenzeitlich noch ins Minus fallen bevor der SL angepasst wird. Geeignet für volatile Coins mit weitem ATR-SL.
- **> 0.05:** Zu hoch – der Kurs könnte den TP erreichen bevor der Breakeven ausgelöst wird.

> **Kombination mit Trailing-SL:** Breakeven schützt vor Verlust, Trailing-SL sichert zusätzlich wachsende Gewinne. Empfehlung: `--breakeven --breakeven-pct 0.01 --trailing-sl --trailing-sl-pct 0.02`

---

### 6. Partial Take-Profit (optional)

```bash
--partial-tp [--partial-tp-fraction 0.5]
```

Beim ersten TP-Hit wird nur ein Teil der Position verkauft. Der Rest läuft als neuer Trade weiter mit:
- **SL = Original-Entry** (Breakeven – kann nicht mehr mit Verlust enden)
- **TP = Original-TP + gleicher Abstand** (nächste Zielstufe)

```
Beispiel: 1 BTC, Entry 90.000, SL 88.000, TP 93.000, Fraction 50%
→ Kurs erreicht 93.000:
  · 0.5 BTC werden verkauft (+3.000 EUR gesichert)
  · 0.5 BTC laufen weiter: SL=90.000 (Breakeven), TP=96.000
→ Kurs erreicht 96.000:
  · restliche 0.5 BTC verkauft (+6.000 EUR aus dem Rest)
```

**`--partial-tp-fraction N`** – Anteil der Position der beim ersten TP verkauft wird (Standard: `0.5` = 50%)
- **0.25–0.33 (25–33%):** Kleiner Teilverkauf – der Großteil der Position läuft weiter. Höheres Potential, aber du sicherst wenig ab wenn der Kurs danach dreht.
- **0.5 (50%, Standard):** Ausgewogene Mischung – Hälfte gesichert, Hälfte läuft weiter.
- **0.67–0.75 (67–75%):** Großteil verkauft – konservativ, kleiner Rest als "Free Trade" ohne Verlustrisiko.
- **> 0.8:** Kaum sinnvoll – der Rest ist zu klein für eine sinnvolle weitere Position.

> **Hinweis:** Der Remainder-Trade wird nur geöffnet wenn der Restbetrag über dem Mindestorderwert (15 EUR) liegt.

---

### 7. Multi-Timeframe HTF-Filter (optional)

```bash
--htf-timeframe 1h [--htf-fast 9] [--htf-slow 21]
```

BUY-Signale werden nur ausgeführt wenn der **übergeordnete Timeframe** (HTF = Higher TimeFrame) bullish ist.
Bullish = Fast-SMA ≥ Slow-SMA im HTF-Chart.

**SELL-Signale werden nicht gefiltert** – eine Position kann immer geschlossen werden.

```
Beispiel: Bot läuft auf 5m-Candles, htf_timeframe=1h
→ 5m zeigt BUY-Signal
→ 1h: Fast-SMA(9) < Slow-SMA(21) → übergeordneter Trend ist bearish
→ BUY wird zu HOLD umgewandelt – kein Kauf
```

**`--htf-timeframe TF`** – Zeitrahmen für den Trendfilter
- **`15m`:** Filterung gegen 15-Minuten-Trend. Sehr reaktiv, leichte Filterung. Sinnvoll wenn der Bot auf 1m oder 3m läuft.
- **`1h` (empfohlen für 5m-Bots):** Gut ausbalanciert – filtert Käufe gegen den Stunden-Trend heraus. Reduziert Signale deutlich, erhöht aber die Trefferquote.
- **`4h`:** Starke Filterung – kauft nur wenn der 4-Stunden-Chart im Aufwärtstrend ist. Sehr wenige, aber zuverlässigere Signale. Für moderate Haltezeiten.
- **`1d`:** Maximale Filterung – kauft nur im täglichen Aufwärtstrend. Wenige Signale, ideal für Swing-Strategien.

**`--htf-fast N / --htf-slow N`** – SMA-Perioden für die HTF-Trendbeurteilung (Standard: `9/21`)
- Dieselbe Logik wie beim Haupt-SMA: Kleinere Werte = reaktiver, größere Werte = stabiler.
- Standard `9/21` passt gut zu `1h` HTF. Für `4h` oder `1d` HTF kann `21/55` sinnvoller sein.

> **Tipp:** Den HTF-Filter aktivieren wenn viele Fehlkäufe in Korrekturphasen auftreten. Er reduziert die Anzahl der Trades, verbessert aber das Verhältnis gewinnender zu verlierender Trades.

---

### 8. Volumen-Filter (optional)

```bash
--volume-filter [--volume-factor 1.2]
```

Ein Crossover-Signal wird nur dann ausgeführt wenn das Volumen der Crossover-Candle über dem Durchschnitt liegt.
Verhindert Fehlsignale in Phasen mit geringer Marktbeteiligung.

```
Signal nur wenn: letztes_volumen ≥ Avg(letzte 20 Candles) × volume_factor
```

**`--volume-factor N`** – Wie viel höher als der Durchschnitt das Volumen sein muss (Standard: `1.2`)
- **1.0:** Minimale Anforderung – jedes Volumen über dem 20-Candle-Durchschnitt ist ausreichend. Sehr permissiv.
- **1.2 (Standard):** Volumen muss 20% über Durchschnitt liegen. Filtert ruhige, bedeutungslose Crossovers heraus.
- **1.5–2.0:** Streng – nur Signale mit deutlich erhöhtem Volumen. Weniger Trades, aber höhere Überzeugung dass eine echte Bewegung stattfindet.
- **> 2.5:** Zu restriktiv – die meisten Signale werden blockiert, viele gute Einstiege werden verpasst.

---

### 9. SL-Cooldown (optional)

```bash
--sl-cooldown 3
```

Nach einem Stop-Loss wartet der Bot N Candles bevor er wieder ein BUY-Signal ausführt.
Verhindert sofortigen Wiedereinstieg in einen weiter fallenden Markt.

**`--sl-cooldown N`** – Anzahl Candles Wartezeit nach SL-Hit (Standard: `3`)
- **0:** Kein Cooldown – sofortiger Wiederkauf möglich. Maximale Nutzung von Bounces, aber Gefahr von Mehrfach-SLs in Folge.
- **3 (Standard):** 15 Minuten Pause bei 5m-Candles. Gibt dem Markt Zeit zu stabilisieren.
- **5–10:** 25–50 Minuten Pause. Konservativer, verpasst ggf. schnelle Rebounds.
- **20+:** Sehr langer Cooldown – sinnvoll nach starken Kurseinbrüchen um den Ausbruch zu warten.
- Die Wartezeit in Minuten = `sl_cooldown × timeframe_minuten` (bei 5m: `3 × 5 = 15 min`).

---

### 10. Safety Buffer (Kapitalschutz)

```bash
--safety-buffer 0.10
```

**`--safety-buffer N`** – Anteil des Gesamtkapitals das niemals in Trades eingesetzt wird (Standard: `0.10` = 10%)
- **0.05 (5%):** Aggressiver – fast das gesamte Kapital wird genutzt. Riskant wenn mehrere Bots gleichzeitig kaufen.
- **0.10 (10%, Standard):** 10% Reserve bleiben immer übrig. Deckt Kraken-Gebühren und unerwartete Situationen ab.
- **0.15–0.20:** Konservativ – weniger Kapital im Einsatz, geringere Rendite aber höherer Puffer.
- Der Safety Buffer wird einmal auf die Gesamt-Balance angewendet, dann wird der Rest gleichmäßig auf alle aktiven Bots aufgeteilt.

---

### 11. Sentiment-Filter (optional, per Bot konfigurierbar)

Der Supervisor schreibt alle 5 Minuten den aktuellen Sentiment-Score (`current_sentiment_score`) in jede Bot-DB –
als gewichteten Durchschnitt der letzten 4 Stunden aus `news.db`. Jeder Bot kann daraufhin unabhängig reagieren.

**Score-Berechnung:** VADER + TextBlob kombiniert (−1.0 bis +1.0), gewichtet nach Quelle:

| Quelle | Gewicht | Begründung |
|--------|---------|------------|
| Fear & Greed Index | 2.5× | Direkter Marktsentiment-Indikator |
| CryptoPanic | 2.0× | Kuratierte Krypto-News |
| RSS-Feeds | 1.0× | Standard-Basis |
| Google News | 0.7× | Allgemein, wenig Krypto-spezifisch |
| CoinGecko Trending | 0.0× | Ausgeschlossen (kein echter Sentiment) |

**Drei unabhängige Filter pro Bot:**

**BUY-Gate** – Nur kaufen wenn Sentiment ausreichend positiv:
```
BUY-Signal + score < buy_min → Signal wird zu HOLD umgewandelt
```
Standard-Schwelle: `0.1` (leicht positives Sentiment erforderlich)

**SELL-Trigger** – Reaktion wenn Sentiment sehr negativ wird:

| Modus | Verhalten |
|-------|-----------|
| `block` (Standard) | BUY-Signale werden gesperrt (HOLD) |
| `close` | Offene Position wird sofort geschlossen (SELL) |
| `both` | Position schließen + weitere BUYs sperren |

Standard-Schwelle: `−0.3` (klar negativer Score)

**Auto-Stop** – Bot automatisch pausieren bei extremem Negativsentiment:
```
score < stop_threshold → Bot pausiert (wie ⏸ PAU im Dashboard)
```
Standard-Schwelle: `−0.5`. Bot muss manuell (Dashboard, Telegram) wieder fortgesetzt werden.

**Konfiguration** per Dashboard (⚙-Button → Erweiterte Einstellungen → 📰 Sentiment-Filter)
oder per Telegram (`/start_bot BTC/EUR sentiment_buy=0.1 sentiment_sell=-0.3 sell_mode=block`).

---

### Positionsgröße

Das Kapital wird gleichmäßig auf alle aktiven Bots verteilt:

```
usable    = balance_EUR × (1 − safety_buffer)
per_bot   = usable / anzahl_aktive_bots
trade_EUR = per_bot × quote_risk_fraction  (0.95)
amount    = trade_EUR / aktueller_preis
```

Je mehr Bots aktiv sind, desto kleiner jede einzelne Position.

---

## Supervisor – Marktregime-Erkennung

Der Supervisor läuft als separater Prozess und analysiert alle 5 Minuten
das Marktregime jedes Coins via **7 Indikatoren** (numpy-basiert).
Die Bots übernehmen die angepassten Parameter beim nächsten Loop-Durchlauf **ohne Neustart**.

### Regime-Klassifikation (5 Regimes, Priorität von oben)

| Regime | Bedingung | RSI-Fenster | SL-Mult | TP-Mult | BUY |
|--------|-----------|-------------|---------|---------|-----|
| **EXTREME** | RSI < 25 oder RSI > 75 | buy < 30, sell > 70 | 1.2× | 2.0× | Nur Gegenpositionen |
| **VOLATILE** | ATR% > 3% oder BB-Width > 4% | buy < 55, sell > 45 | 2.0× | 3.5× | Selektiv |
| **BULL** | ADX > 22, EMA50 > EMA200 (+0.5%) | buy < 68, sell > 32 | 1.5× | 2.5× | ✅ Normal |
| **BEAR** | ADX > 22, EMA50 < EMA200 (−0.5%) | buy < 45, sell > 30 | 2.0× | 3.0× | ❌ Unterdrückt |
| **SIDEWAYS** | ADX < 22, kein klarer Trend | buy < 60, sell > 40 | 1.2× | 1.8× | Enger RSI |

**Indikatoren:** ADX · EMA50/200 · ATR% · BB-Width · RSI(14) — alle via `bot/indicators.py` (numpy, Wilder's Smoothing)

### Multi-Varianten-Optimierung (bis zu 90 Varianten)

Pro Supervisor-Durchlauf werden Varianten getestet:

```
5 RSI/ATR-Kombos × 6 SMA-Varianten × 4 Feature-Kombos = bis zu 120 Varianten
(nach MIN_TRADES-Filter und SQN-Sortierung)
```

**RSI/ATR-Kombos pro Regime** (Supervisor wählt die beste):

| Regime | RSI Buy / Sell | ATR SL-Mult | ATR TP-Mult |
|--------|---------------|-------------|-------------|
| BULL (3 Kombos) | 65/35 · 68/32 · 72/28 | 1.2 · 1.5 · 2.0 | 2.0 · 2.5 · 3.0 |
| BEAR (3 Kombos) | 40/30 · 45/30 · 50/35 | 1.8 · 2.0 · 1.5 | 2.5 · 3.0 · 2.8 |
| SIDEWAYS (3 Kombos) | 57/43 · 60/40 · 63/37 | 1.0 · 1.2 · 1.5 | 1.5 · 1.8 · 2.2 |
| VOLATILE (3 Kombos) | 52/48 · 55/45 · 58/42 | 1.8 · 2.0 · 2.5 | 3.0 · 3.5 · 4.0 |
| EXTREME (3 Kombos) | 28/72 · 30/70 · 35/65 | 1.0 · 1.2 · 1.5 | 1.8 · 2.0 · 2.5 |

**Feature-Kombos** (× 6 SMA × 3 RSI/ATR = 72):

| Trailing SL | Volumen-Filter |
|-------------|----------------|
| ❌ | ❌ |
| ✅ | ❌ |
| ❌ | ✅ |
| ✅ | ✅ |

**Scoring: SQN** (System Quality Number) statt reinem P&L:
```
SQN = (Ø Trade-P&L / Stdabw) × √Anzahl_Trades
```
SQN bevorzugt konsistente Strategien gegenüber Einzel-Lucky-Trades.
Varianten mit weniger als 5 Trades werden nicht gewertet.

**Walk-Forward-Validation** verhindert Overfitting:
```
2000 Candles (~7 Tage) aufgeteilt in:
  Training:   80% (~1600 Candles) → Optimierung
  Validation: 20% (~400 Candles)  → Out-of-Sample-Test
```
`supervisor_val_pnl` in DB: Kennzahl ob die Strategie auch auf ungesehenen Daten funktioniert.

**Historischer Candle-Cache:**
- Max. **8640 Candles** pro Symbol (30 Tage bei 5m)
- Beim Start: automatischer **Backfill** auf 2000 Candles (bis zu 5 Batches via API)

**Proaktive Telegram-Nachrichten** wenn der Supervisor etwas Neues lernt:
```
📈 Gelernte Strategie: BTC/EUR
Regime: TREND
Strategie: Swing 21/55  ⬆SL
Sim-P&L: +4.1%  val=+1.8%  SQN=1.84
Δ SQN: +0.62
```
Wird gesendet wenn: Regime wechselt **oder** SQN-Sprung ≥ 0.5 zum Vorgänger.

Wenn die optimale Feature-Kombo von der aktuellen Bot-Konfiguration abweicht, kommt zusätzlich:
```
🔬 Supervisor-Empfehlung: BTC/EUR
Strategie: Agile 7/18  Sim-P&L: +3.2% (5 Trades)
Trailing SL: ✅ empfohlen  (aktuell: ❌)
```

### Regime-Persistenz / Warmstart

Nach jedem Supervisor-Durchlauf speichert der Bot die tatsächlich verwendeten Parameter
als `effective_*`-Keys in der DB. Bei einem Neustart werden diese sofort geladen,
ohne auf den nächsten Supervisor-Zyklus (bis zu 5 Minuten) warten zu müssen.

### Cross-Bot-Learning

Wenn BTC im Trend-Regime eine bessere Strategie findet als XRP (ebenfalls Trend), wird
die Strategie auf XRP übertragen und dort validiert bevor sie übernommen wird.

### Peer Learning (mehrere Pi-Instanzen)

Mehrere Freunde mit eigenem Pi und Kraken-Account können ihre **gelernten Strategien
untereinander teilen** – über WireGuard VPN, ohne zentralen Server.

```
Pi-A (10.8.0.1) ←── WireGuard ───→ Pi-B (10.8.0.2)
     ↑                                    ↑
  PEERS=http://10.8.0.2:5001         PEERS=http://10.8.0.1:5001
```

**Setup (jeder Pi, einmalig):**
```bash
# WireGuard-Gruppe: ein Pi als Hub, andere als Clients
pivpn add          # für jeden Freund einen VPN-Client anlegen
pivpn -qr <Name>   # QR-Code zum Scannen schicken

# Peers in .env eintragen
echo "PEERS=http://10.8.0.2:5001,http://10.8.0.3:5001" >> .env
sudo systemctl restart tradingbot-supervisor.service
```

**Wie es funktioniert:**
1. Jeder Pi exposed `GET /api/peer/strategies` (Port 5001, nur VPN-erreichbar)
2. Supervisor fragt alle Peers alle 5 Minuten ab
3. Peer-Strategie wird **lokal auf eigenen Candles** getestet bevor sie übernommen wird
4. Übernahme nur wenn SQN **und** P&L auf eigenen Candles besser
5. Telegram-Nachricht bei Übernahme: `🌐 Peer-Learning: BTC/EUR – Strategie von 10.8.0.2 übernommen`

**Privacy:** Der Endpunkt gibt nur Strategie-Parameter + Scoring zurück.
Kein Kontostand, keine Orders, keine persönlichen Daten. Nur über WireGuard erreichbar.

### Supervisor starten

```bash
botvenv/bin/python supervisor.py --dry-run   # Testen (kein Schreiben)
botvenv/bin/python supervisor.py             # Live
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

# Standard Live
botvenv/bin/python main.py --symbol BTC/EUR

# Konservativ (enger SL, Breakeven-Absicherung, HTF-Filter)
botvenv/bin/python main.py --symbol BTC/EUR \
  --sl 0.02 --tp 0.04 \
  --breakeven --breakeven-pct 0.01 \
  --htf-timeframe 1h

# Aggressiv (Trending-Markt, Trailing + Partial-TP)
botvenv/bin/python main.py --symbol ETH/EUR \
  --trailing-sl --trailing-sl-pct 0.02 \
  --partial-tp --partial-tp-fraction 0.5

# Alles kombiniert
botvenv/bin/python main.py --symbol XRP/EUR \
  --trailing-sl --trailing-sl-pct 0.02 \
  --breakeven --breakeven-pct 0.01 \
  --partial-tp --partial-tp-fraction 0.5 \
  --htf-timeframe 1h \
  --volume-filter --volume-factor 1.2 \
  --sl-cooldown 3
```

### Alle CLI-Optionen

**Basis-Strategie:**

| Option | Standard | Beschreibung |
|--------|----------|--------------|
| `--symbol` | – | Coin-Paar, z.B. `BTC/EUR` (Pflicht) |
| `--timeframe` | `5m` | Kerzen-Intervall: `1m` `3m` `5m` `15m` `1h` |
| `--fast N` | `9` | Fast-SMA-Periode (empfohlen: 5–21) |
| `--slow N` | `21` | Slow-SMA-Periode (empfohlen: 15–55, muss > fast) |
| `--sl N` | `0.03` | Fallback Stop-Loss wenn ATR nicht berechenbar (3%) |
| `--tp N` | `0.06` | Fallback Take-Profit wenn ATR nicht berechenbar (6%) |
| `--safety-buffer N` | `0.10` | Kapital-Reserve die nie eingesetzt wird (10%) |
| `--startup-delay N` | `0` | Sekunden vor erstem API-Call (Rate-Limit bei Mehrfach-Start) |
| `--dry-run` | – | Kein echter Handel – Orders werden simuliert |

**Verlustschutz:**

| Option | Standard | Wert-Effekt |
|--------|----------|-------------|
| `--trailing-sl` | – | Trailing Stop-Loss aktivieren |
| `--trailing-sl-pct N` | `0.02` | Abstand: kleiner = enger am Kurs (mehr Ausstopp-Risiko), größer = mehr Spielraum |
| `--sl-cooldown N` | `3` | Candles Pause nach SL: `0`=keiner, `3`=15min, `10`=50min (bei 5m) |
| `--breakeven` | – | Breakeven-SL aktivieren |
| `--breakeven-pct N` | `0.01` | Trigger: kleiner = SL früher auf Entry (konservativer), größer = erst nach mehr Gewinn |

**Gewinnoptimierung:**

| Option | Standard | Wert-Effekt |
|--------|----------|-------------|
| `--partial-tp` | – | Partial Take-Profit aktivieren |
| `--partial-tp-fraction N` | `0.5` | Anteil: `0.25`=25% verkaufen, `0.5`=50/50, `0.75`=75% sichern |

**Signal-Filter:**

| Option | Standard | Wert-Effekt |
|--------|----------|-------------|
| `--volume-filter` | – | Volumen-Filter aktivieren |
| `--volume-factor N` | `1.2` | Schwelle: `1.0`=nur über Avg, `1.5`=50% über Avg, `2.0`=doppeltes Avg |
| `--htf-timeframe TF` | – | HTF-Timeframe: `15m` `1h` `4h` `1d` |
| `--htf-fast N` | `9` | SMA-Periode für HTF-Trend-Beurteilung (schnell) |
| `--htf-slow N` | `21` | SMA-Periode für HTF-Trend-Beurteilung (langsam) |

---

## Web-Dashboard

```bash
botvenv/bin/python web/app.py
# → http://<ip>:5001
```

- Zeigt alle Bot-Instanzen automatisch (liest alle `db/*.db`)
- **Auto-Refresh**: 60s (Seite via JS, pausiert automatisch wenn ein Dialog offen ist), 5s (Cards live via `/api/bots`)
- **Regime-Badge** pro Bot: TREND / SIDEWAYS / VOLATILE mit Farbe
- **Status-Badge**: `● ON` (laufend) · `⏸ PAU` (pausiert) · `■ OFF` (gestoppt)
- **⏸ Pause / ▶ Fortsetzen**: Bot läuft weiter, führt aber keine Orders aus – nützlich z.B. bei bekannten News-Events
- **SL/TP editierbar**: ± Buttons mit adaptiver Schrittweite (~1.50€ P&L pro Klick)
- **P&L-Anzeige**: Netto nach Kraken-Gebühren (0.26% pro Order)

### Parameter zur Laufzeit ändern (⚙-Button)

Jede Bot-Card hat einen **⚙**-Button der einen Dialog mit allen änderbaren Parametern öffnet.
Änderungen werden beim **nächsten Bot-Loop (~60s)** übernommen – **kein Neustart nötig**.
Gleichzeitig wird `bot.conf.d/SYMBOL.conf` aktualisiert → Werte bleiben nach Neustart erhalten.

| Parameter | Beschreibung |
|-----------|--------------|
| Fast MA / Slow MA | SMA-Perioden für Signal-Generierung |
| Stop-Loss % / Take-Profit % | Fallback SL/TP für neue Trades |
| RSI Kauf-Max / RSI Verkauf-Min | Filter-Grenzen für Signal-Qualität |
| Safety Buffer % | Kapital-Reserve die nie investiert wird |
| Trailing SL | Ein/Aus + Abstand % |
| Breakeven SL | Ein/Aus + Trigger % |
| Volumen-Filter | Ein/Aus + Faktor |
| Partial Take-Profit | Ein/Aus + Anteil % |
| Sentiment BUY-Gate | Ein/Aus + Min-Score (z.B. 0.1) |
| Sentiment SELL-Trigger | Ein/Aus + Max-Score (z.B. −0.3) + Modus (block/close/both) |
| Sentiment Auto-Stop | Ein/Aus + Schwelle (z.B. −0.5) |

### Bot hinzufügen

Im Dialog **+ Bot hinzufügen** gibt es zwei Bereiche:

**Basis-Einstellungen** (immer sichtbar):
- Symbol (Markt-Suche mit Autocomplete)
- Timeframe, Safety Buffer
- Fast MA, Slow MA
- Stop-Loss %, Take-Profit %
- Dry Run

**⚙ Erweiterte Einstellungen** (ausklappbar):

| Feld | Standard | Beschreibung |
|------|----------|--------------|
| Trailing-SL ☑ | aus | Checkbox aktiviert den Trailing-SL |
| Abstand % | 2% | Wird aktiv wenn Checkbox gesetzt |
| SL-Cooldown | 3 | Candles Pause nach Stop-Loss |
| Volumen-Filter ☑ | aus | Checkbox aktiviert den Filter |
| Faktor | 1.2 | Wird aktiv wenn Checkbox gesetzt |
| Breakeven-SL ☑ | aus | Checkbox aktiviert Breakeven-SL |
| Trigger % | 1% | Wird aktiv wenn Checkbox gesetzt |
| Partial-TP ☑ | aus | Checkbox aktiviert Partial Take-Profit |
| Anteil % | 50% | Wird aktiv wenn Checkbox gesetzt |
| HTF-Timeframe | – | Dropdown: deaktiviert / 15m / 1h / 4h / 1d |
| HTF Fast SMA | 9 | Periode für HTF-Trend-Beurteilung |
| HTF Slow SMA | 21 | Periode für HTF-Trend-Beurteilung |
| Sentiment BUY-Gate ☑ | aus | Nur kaufen wenn Score ≥ Min |
| Min-Score | 0.1 | Wird aktiv wenn Checkbox gesetzt |
| Sentiment SELL-Trigger ☑ | aus | Reaktion wenn Score < Max |
| Max-Score | −0.3 | Wird aktiv wenn Checkbox gesetzt |
| Modus | block | block / close / both |
| Sentiment Auto-Stop ☑ | aus | Bot pausieren wenn Score < Schwelle |
| Schwelle | −0.5 | Wird aktiv wenn Checkbox gesetzt |

Beim **▶ Starten** (Wiederstart eines gestoppten Bots aus der Card) werden alle gespeicherten Feature-Flags automatisch wiederhergestellt.

### Bestände-Tabelle

Zeigt alle auf Kraken gehaltenen Coins mit Menge, EUR-Wert, aktuellem Kurs und Zielkurs-Rechner.

| Spalte | Beschreibung |
|--------|--------------|
| Zielkurs | ± Buttons verschieben den Zielkurs (~0.25 € P&L pro Klick) |
| Verkaufen | Immer aktiv: Force-SELL wenn Bot läuft, sonst Direktverkauf via `POST /api/direct_sell` |
| Bot | **▶ Bot**: öffnet „Bot hinzufügen"-Dialog mit Symbol vorausgefüllt · **● läuft**: Bot ist aktiv |

**Direktverkauf ohne Bot**: Sofortiger Marktverkauf direkt über Kraken – kein laufender Bot nötig. Offene Trades in der DB werden automatisch geschlossen.

### Collapse-Zustand nach Reload

Eingeklappte Bot-Sections und die Bestände-Sektion merken sich ihren Zustand über Seiten-Reloads hinweg (gespeichert in `localStorage`). Einmal zugeklappt = bleibt zugeklappt bis manuell aufgeklappt.

---

## systemd (Raspberry Pi – empfohlen)

```bash
bash systemd/install.sh
sudo systemctl start tradingbot.target
sudo systemctl status 'tradingbot@*'
journalctl -u tradingbot@BTC_EUR -f
```

### Bot-Konfiguration (`bot.conf.d/`)

```ini
# bot.conf.d/BTC_EUR.conf
BOT_SYMBOL=BTC/EUR
BOT_ARGS=--timeframe 5m --fast 9 --slow 21 --sl 0.02 --tp 0.04 \
         --safety-buffer 0.10 --startup-delay 20 \
         --trailing-sl --trailing-sl-pct 0.02 \
         --breakeven --breakeven-pct 0.01
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

```
Offene Position: 0.01 BTC @ 85.000 EUR  (+2.3%)
News-Sentiment:  +0.62 (bullish)
Regime:          TREND
→ Pyramid-Kauf: +0.0025 BTC @ 86.950 EUR
→ Neuer Avg-Entry: 85.390 EUR  SL: 84.100  TP: 88.200
```

---

## Trade-Benachrichtigungen

| Event | Nachricht |
|-------|-----------|
| Kauf | 🟢 KAUF BTC/EUR – Menge @ Preis · SL / TP mit % |
| Verkauf (Signal) | 📉 VERKAUF BTC/EUR – Menge @ Preis |
| Stop-Loss | 🛑 STOP-LOSS BTC/EUR – P&L netto |
| Take-Profit | 💰 TAKE-PROFIT BTC/EUR – P&L netto |
| Partial TP | 💰 TAKE-PROFIT (Partial) – Teilbetrag @ Preis, Rest läuft weiter |
| Pyramid | 🔺 NACHKAUF BTC/EUR – neuer Avg-Entry |

---

## News-Agent

Überwacht Krypto-News aus 10+ Quellen, berechnet Sentiment-Scores
und sendet bei relevanten Ereignissen Telegram-Alerts mit Inline-Buttons.

### Filter-Pipeline

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
| Schwelle | 0.5 | `\|sentiment_score\|` muss Schwelle überschreiten |

### Sentiment-Scoring

- **VADER** (70%) + **TextBlob** (30%) → kombinierter Score −1.0 bis +1.0
- `bearish` < −0.3 · `neutral` −0.3…+0.3 · `bullish` > +0.3
- **Crypto-Lexikon**: VADER wurde um 56 Krypto-spezifische Begriffe erweitert (`bullish` +2.5, `rugpull` −3.5, `hack` −2.5, `mooning` +2.5, …)
- **Quellen-Gewichtung**: Fear & Greed 2.5× · CryptoPanic 2.0× · RSS 1.0× · Google 0.7× · CoinGecko 0.0× (ausgeschlossen)
- **Full-Body-Crawling**: Artikel-Text wird vollständig extrahiert (trafilatura), nicht nur Titel
- **Supervisor-Integration**: Alle 5 min schreibt der Supervisor den gewichteten 4h-Durchschnitt
  als `current_sentiment_score` in jede Bot-DB (Basis für Sentiment-Filter in `main.py`)

### News-Agent starten

```bash
botvenv/bin/python news_agent.py --dry-run --once   # Testen
botvenv/bin/python news_agent.py --test-telegram     # Verbindung testen
botvenv/bin/python news_agent.py                     # Live
sudo systemctl start news-agent
```

### Telegram-Befehle

| Befehl | Beschreibung |
|--------|--------------|
| `/status` | Alle Bots: Status, Signal, Regime + Gesamt-Balance (Frei/Coins/Total) |
| `/portfolio` | Offene Positionen: Entry, Jetzt-Preis, P&L EUR+%, SL/TP mit Abstand |
| `/rendite` | Rentabilität: Win-Rate, Gesamt-P&L, beste/schlechteste Trades, Sim-P&L |
| `/holdings` | Alle Coins auf Kraken (Menge × Preis, sortiert nach EUR-Wert) |
| `/supervisor` | Supervisor-Übersicht: letztes Regime/Strategie/Sim-P&L/val-P&L/SQN pro Bot |
| `/supervisor BTC/EUR` | Detailverlauf: Regime-Verteilung, Top-Strategien, Ø SQN, Ø val-P&L, Cross-Bot-Events |
| `/sentiment BTC/EUR` | Aktueller News-Sentiment für einen Coin |
| `/news` | 10 stärkste News der letzten 48h |
| `/news BTC/EUR` | 5 neueste News für diesen Coin |
| `/params BTC/EUR` | Parameter: SMA, RSI, ATR, Regime, Fallback SL/TP, alle Feature-Flags, Sentiment-Filter + aktueller Score |
| `/start_bot BTC/EUR [params]` | Bot starten (ohne params: gespeicherte Werte; mit params: Override) |
| `/stop_bot BTC/EUR` | Bot stoppen |
| `/stop_all` | Alle laufenden Bots sofort stoppen |
| Freitext: `pausier BTC` | Handel pausieren (Bot läuft, führt aber keine Orders aus, Status: ⏸ PAU) |
| Freitext: `fortsetzen BTC` | Pausierten Handel fortsetzen |
| `/buy BTC/EUR` | Force-BUY beim nächsten Loop |
| `/sell BTC/EUR` | Force-SELL beim nächsten Loop |
| `/set_sl BTC/EUR 2.0` | Stop-Loss auf 2% setzen |
| `/set_tp BTC/EUR 4.0` | Take-Profit auf 4% setzen |

### `/start_bot` mit Inline-Parametern

```
/start_bot BTC/EUR                     → startet mit gespeicherten Werten aus DB
/start_bot BTC/EUR sl=2 tp=4           → SL 2%, TP 4%
/start_bot BTC/EUR trailing breakeven  → Trailing-SL + Breakeven aktivieren
/start_bot BTC/EUR trailing=1.5        → Trailing-SL mit 1.5% Abstand
/start_bot BTC/EUR htf=1h              → HTF-Filter auf 1h aktivieren
/start_bot BTC/EUR partial=60          → Partial-TP, 60% beim ersten Hit verkaufen
/start_bot BTC/EUR volume=1.5          → Volumen-Filter, Faktor 1.5
/start_bot BTC/EUR cooldown=5          → 5 Candles SL-Cooldown
/start_bot BTC/EUR notrailing nohtf    → Features deaktivieren
```

**Parametersyntax:**

| Schlüsselwort | Beispiel | Beschreibung |
|---------------|---------|--------------|
| `sl=N` | `sl=2` | Stop-Loss Fallback in % |
| `tp=N` | `tp=4` | Take-Profit Fallback in % |
| `fast=N` / `slow=N` | `fast=7 slow=18` | SMA-Perioden |
| `tf=TF` | `tf=15m` | Timeframe |
| `trailing` / `trailing=N` | `trailing=2` | Trailing-SL (mit Abstand in %) |
| `breakeven` / `breakeven=N` | `breakeven=1` | Breakeven-SL (mit Trigger in %) |
| `partial` / `partial=N` | `partial=50` | Partial-TP (mit Anteil in %) |
| `htf=TF` | `htf=1h` | HTF-Timeframe |
| `volume` / `volume=N` | `volume=1.3` | Volumen-Filter (mit Faktor) |
| `cooldown=N` | `cooldown=5` | SL-Cooldown in Candles |
| `notrailing` | – | Trailing-SL deaktivieren |
| `nobreakeven` | – | Breakeven-SL deaktivieren |
| `nopartial` | – | Partial-TP deaktivieren |
| `novol` | – | Volumen-Filter deaktivieren |
| `nohtf` | – | HTF-Filter deaktivieren |
| `sentiment_buy=N` / `sbuy=N` | `sbuy=0.1` | BUY-Gate Min-Score (aktiviert automatisch) |
| `sentiment_sell=N` / `ssell=N` | `ssell=-0.3` | SELL-Trigger Max-Score (aktiviert automatisch) |
| `sell_mode=X` / `sent_mode=X` | `sell_mode=block` | SELL-Modus: `block` / `close` / `both` |
| `sentiment_stop=N` / `sstop=N` | `sstop=-0.5` | Auto-Stop Schwelle (aktiviert automatisch) |

Bestätigung zeigt alle aktiven Features:
```
✅ BTC/EUR gestartet.
5m | Fast 9 | Slow 21 | SL 2.0% | TP 4.0%
Features: Trailing 1.5% · Breakeven 1.0% · HTF 1h
```

### KI-Freitext (Claude Haiku)

Wenn `ANTHROPIC_API_KEY` gesetzt ist, versteht der Bot **beliebigen Freitext** auf Deutsch und Englisch –
auch mit Tippfehlern, anderen Wortstellungen oder komplexen Kombinationen:

```
"kannst du bei eth den fast ma auf 7 setzen und volume filter an?"
"stopp mal den ada bot kurz"
"wie viel ist mein portfolio gerade wert?"
"setze bei bitcoin den stop loss auf 2.5 prozent"
"übernimm die supervisor empfehlung für btc"
```

**Ohne `ANTHROPIC_API_KEY`** funktioniert nur ein eingeschränkter Regex-basierter Parser:

| Freitext-Beispiel (Regex) | Aktion |
|--------------------------|--------|
| `status` · `portfolio` · `rendite` · `holdings` | Übersicht |
| `stoppe BTC` · `starte ETH` | Bot stop/start |
| `kauf BTC` · `verkauf ETH` | Force-Signal |
| `sl BTC 2` · `tp BTC 4` | SL/TP setzen |
| `fast BTC 7` · `slow ETH 21` | MA-Perioden |
| `volumen BTC 1.5` · `partial BTC 50` | Filter-Parameter |
| `empfehlung BTC übernehmen` | Supervisor-Empfehlung |
| `BTC fast=7 slow=18 trailing=2` | Multi-Param (key=value) |

Alle Befehle funktionieren auch als **Freitext** ohne `/`:
`status` · `portfolio` · `rendite` · `holdings` · `stoppe BTC` · `starte ETH` · `kauf BTC` · `sl BTC 2`

### Alert-Inline-Buttons

| Button | Wann | Aktion |
|--------|------|--------|
| `🛑 BTC/EUR stoppen` | Bearish-Alert, Bot läuft | POST /api/bot/stop |
| `▶ ETH/EUR starten` | Bullish-Alert, Bot gestoppt | POST /api/bot/start (gespeicherte Params) |
| `✅ Ignorieren` | Immer | Alert als dismissed markieren (24h Cooldown) |

---

## Auto-Cleanup

Beim Start und täglich löscht der Bot automatisch alte Einträge aus der Datenbank:
- **`orders`-Tabelle:** Einträge älter als `cleanup_days` (Standard: 30 Tage) werden gelöscht.
- **`errors`-Tabelle:** Einträge älter als `cleanup_days` werden gelöscht.
- **`trades`-Tabelle:** Wird **niemals** gelöscht (vollständige Trade-Historie).

Der `cleanup_days`-Wert ist in `RiskConfig` gesetzt (Standard: `30`). Bei sehr aktiven Bots und begrenztem SD-Karten-Speicher kann der Wert auf 14 oder 7 reduziert werden.

---

## Remote-Zugriff via WireGuard VPN

```bash
pivpn add          # VPN-Client hinzufügen
pivpn -qr <Name>   # QR-Code für Handy anzeigen
sudo wg show       # Verbindungsstatus
```

Nach VPN-Verbindung: `http://<pi-vpn-ip>:5001` im Browser.

### WireGuard-Port über install.sh setzen

`install.sh` kann den WireGuard-Port direkt in `/etc/wireguard/wg0.conf` setzen und `wg-quick@wg0` neu starten:

**Interaktiv** (wird beim Ausführen nachgefragt):
```bash
bash systemd/install.sh
# → WireGuard-Port setzen? [Enter = überspringen, sonst Port eingeben]: 51820
```

**Als Argument** (nicht-interaktiv / für Scripts):
```bash
bash systemd/install.sh --wg-port=51820
```

Wird kein Port angegeben (Enter), bleibt die bestehende WireGuard-Konfiguration unverändert.
Anschließend Router-Port-Forwarding auf den neuen Port anpassen.

---

## Troubleshooting

### `git pull` – „There is no tracking information for the current branch"

Tritt auf wenn der Branch kein Upstream-Tracking hat (z.B. nach manuellem Klonen oder frischer Installation).

```bash
git branch --set-upstream-to=origin/main main
git pull && sudo systemctl restart tradingbot-web.service
```

### Dashboard-Update – „git fetch fehlgeschlagen: dubious ownership"

Tritt auf wenn das Repo mit `sudo` geklont wurde und der Flask-Prozess als anderer User läuft.
Einmalig beheben:

```bash
git config --global --add safe.directory ~/bot
```

### Bots starten nach Reboot nicht automatisch

Tritt auf wenn `tradingbot.target` nicht aktiviert ist (z.B. nach Neuinstallation oder manuellem `disable`).

```bash
sudo systemctl is-enabled tradingbot.target   # sollte "enabled" ausgeben
sudo systemctl enable tradingbot.target       # falls "disabled"
sudo systemctl daemon-reload
```

`install.sh` setzt das `enable` normalerweise automatisch. Wenn es trotzdem fehlt, einmalig manuell ausführen.

### `curl … | bash` bricht mit Fehler 23 ab

Passiert wenn das Installationsskript via Pipe gestartet wird und `read`-Aufrufe fehlschlagen.
Abhilfe: Skript erst herunterladen, dann ausführen:

```bash
curl -fsSL https://raw.githubusercontent.com/MichaelNeuner10101993/TradingBot/main/install.sh -o /tmp/install.sh
bash /tmp/install.sh
```

---

## Sicherheit

- Kraken API-Keys mit minimalen Rechten (kein Withdraw)
- `.env` ist gitignored – niemals committen
- **Circuit Breaker**: Bot stoppt nach 5 konsekutiven Fehlern automatisch
- `NoNewPrivileges=true` in Bot-Services
- `/etc/sudoers.d/tradingbot`: User darf `systemctl stop|start|restart tradingbot@*` ohne Passwort
- Supervisor schreibt nur `supervisor_*`-Keys in Bot-DBs, greift nie in Orders ein
