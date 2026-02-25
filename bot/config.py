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


@dataclass
class RiskConfig:
    quote_risk_fraction: float = 0.95    # Anteil des Bot-Anteils der eingesetzt wird
    safety_buffer_pct: float = 0.10      # 10% der Gesamtbalance werden nie angefasst
    max_open_orders: int = 1
    min_order_quote: float = 15.0        # Sicherheitspuffer über Kraken-Mindestmengen
    db_dir: str = "db"                   # Verzeichnis mit allen Bot-DBs (für Bot-Zählung)
    # Circuit Breaker: Bot stoppt nach N konsekutiven Fehlern
    max_consecutive_errors: int = 5
    # Stop-Loss / Take-Profit – SNX ist volatiler, 2% SL wird durch normales Rauschen getriggert
    stop_loss_pct: float = 0.03          # 3% unter Entry → SL
    take_profit_pct: float = 0.06        # 6% über Entry → TP  (2:1 R:R beibehalten)


@dataclass
class OpsConfig:
    log_level: str = "INFO"
    log_dir: str = "logs"
    db_path: str = "db/state.db"
