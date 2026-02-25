"""
Risk & Position Sizing.
Berechnet Ordergrößen dynamisch basierend auf:
  - Aktuelle Gesamtbalance
  - Anzahl aktiver Bot-Instanzen (gezählt via db/*.db)
  - Sicherheitspuffer (safety_buffer_pct)
"""
import logging
from glob import glob
from bot.config import RiskConfig

log = logging.getLogger("tradingbot.risk")


def _count_active_bots(db_dir: str) -> int:
    """Zählt laufende Bot-Instanzen anhand der DB-Dateien."""
    dbs = glob(f"{db_dir}/*.db")
    count = max(len(dbs), 1)  # mindestens 1 damit keine Division durch 0
    log.debug(f"Aktive Bot-Instanzen: {count} (aus {db_dir}/*.db)")
    return count


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check_guardrails(self, open_orders: list, balance: dict) -> tuple[bool, str]:
        if len(open_orders) >= self.cfg.max_open_orders:
            return False, f"Zu viele offene Orders ({len(open_orders)}/{self.cfg.max_open_orders})"

        quote_free  = balance.get("quote", 0.0)
        num_bots    = _count_active_bots(self.cfg.db_dir)
        usable      = quote_free * (1 - self.cfg.safety_buffer_pct)
        per_bot     = usable / num_bots
        trade_quote = per_bot * self.cfg.quote_risk_fraction

        if trade_quote < self.cfg.min_order_quote:
            return False, (
                f"Trade-Größe zu klein: {trade_quote:.2f} EUR "
                f"(Balance={quote_free:.2f}, Bots={num_bots}, "
                f"Puffer={self.cfg.safety_buffer_pct*100:.0f}%)"
            )

        return True, "ok"

    def calc_buy_amount(self, balance: dict, last_price: float, exchange) -> float:
        quote_free  = balance.get("quote", 0.0)
        num_bots    = _count_active_bots(self.cfg.db_dir)

        # Verfügbares Kapital nach Sicherheitspuffer, gleichmäßig auf alle Bots verteilt
        usable      = quote_free * (1 - self.cfg.safety_buffer_pct)
        per_bot     = usable / num_bots
        trade_quote = per_bot * self.cfg.quote_risk_fraction

        log.info(
            f"Sizing: Balance={quote_free:.2f}€ | Puffer={self.cfg.safety_buffer_pct*100:.0f}% "
            f"| Bots={num_bots} | pro Bot={per_bot:.2f}€ | Trade={trade_quote:.2f}€"
        )
        return trade_quote / last_price

    def calc_sell_amount(self, balance: dict) -> float:
        return balance.get("base", 0.0)
