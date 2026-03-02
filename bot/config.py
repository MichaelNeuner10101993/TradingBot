"""
Zentrale Konfiguration des Trading Bots.
Alle Parameter werden hier definiert – keine Magic Numbers in anderen Modulen.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ExchangeConfig:
    api_key: str = field(default_factory=lambda: os.getenv("KRAKEN_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("KRAKEN_API_SECRET", ""))
    exchange_id: str = "kraken"
    enable_rate_limit: bool = True


@dataclass
class BotConfig:
    symbol: str = "SNX/EUR"
    timeframe: str = "5m"       # 1m wäre zu viel Noise für SNX
    fast_period: int = 9        # kürzer als 12 – SNX bewegt sich schneller
    slow_period: int = 21       # kürzer als 26 – passt besser zur SNX-Volatilität
    poll_seconds: int = 60
    dry_run: bool = False
    # Multi-Timeframe-Filter: BUY nur wenn HTF bullish (fast SMA >= slow SMA)
    htf_timeframe: str = ""     # z.B. "1h", leer = deaktiviert
    htf_fast: int = 9
    htf_slow: int = 21


@dataclass
class RiskConfig:
    quote_risk_fraction: float = 0.95    # Anteil des Bot-Anteils der eingesetzt wird
    safety_buffer_pct: float = 0.10      # 10% der Gesamtbalance werden nie angefasst
    max_open_orders: int = 1
    min_order_quote: float = 15.0        # Sicherheitspuffer über Kraken-Mindestmengen
    db_dir: str = "db"                   # Verzeichnis mit allen Bot-DBs (für Bot-Zählung)
    # Circuit Breaker: Bot stoppt nach N konsekutiven Fehlern
    max_consecutive_errors: int = 5
    # Stop-Loss / Take-Profit (Fallback wenn ATR nicht berechenbar)
    stop_loss_pct: float = 0.03          # 3% unter Entry → SL
    take_profit_pct: float = 0.06        # 6% über Entry → TP
    # RSI-Filter: verhindert Käufe bei Überkauft und Verkäufe bei Überverkauft
    rsi_period: int = 14
    rsi_buy_max: float = 65.0            # Kein BUY wenn RSI > 65
    rsi_sell_min: float = 35.0           # Kein SELL wenn RSI < 35
    # ATR-basiertes SL/TP (überschreibt stop_loss_pct / take_profit_pct)
    atr_period: int = 14
    atr_sl_mult: float = 1.5             # SL = entry - 1.5 × ATR
    atr_tp_mult: float = 2.5             # TP = entry + 2.5 × ATR
    # Trailing Stop-Loss: SL zieht mit steigendem Kurs nach oben
    use_trailing_sl: bool = False
    trailing_sl_pct: float = 0.02        # 2% Abstand unter aktuellem Kurs
    # Cooldown nach SL-Hit: verhindert sofortigen Wiederkauf
    sl_cooldown_candles: int = 3         # 3 Candles (= 15min bei 5m-Timeframe)
    # Volumen-Filter: Signal nur bei überdurchschnittlichem Volumen
    volume_filter: bool = False          # default aus (konservativ)
    volume_factor: float = 1.2           # Crossover-Candle muss 1.2× Avg(20) haben
    # Breakeven-SL: SL automatisch auf Entry heben wenn Gewinn >= Trigger
    breakeven_enabled: bool = False
    breakeven_trigger_pct: float = 0.01  # 1% Gewinn → SL auf Entry
    # Partial Take-Profit: bei erstem TP-Hit nur Anteil verkaufen
    partial_tp_enabled: bool = False
    partial_tp_fraction: float = 0.50   # 50% verkaufen, 50% als Remainder weiterführen
    # Auto-Cleanup: alte orders/errors-Einträge regelmäßig löschen (trades nie)
    cleanup_days: int = 30
    # Sentiment-Filter (News-Agent → Supervisor → bot_state)
    sentiment_buy_enabled:    bool  = False   # BUY nur wenn Score ≥ sentiment_buy_min
    sentiment_buy_min:        float = 0.1     # Mindestscore für BUY
    sentiment_sell_enabled:   bool  = False   # Reaktion wenn Score < sentiment_sell_max
    sentiment_sell_max:       float = -0.3    # Trigger-Schwelle für SELL-Reaktion
    sentiment_sell_mode:      str   = "block" # "block" | "close" | "both"
    sentiment_stop_enabled:   bool  = False   # Bot pausieren wenn Score < sentiment_stop_threshold
    sentiment_stop_threshold: float = -0.5    # Stopp-Schwelle


@dataclass
class OpsConfig:
    log_level: str = "INFO"
    log_dir: str = "logs"
    db_path: str = "db/state.db"
