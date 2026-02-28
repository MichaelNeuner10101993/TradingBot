"""
Risk & Position Sizing.
Berechnet Ordergrößen dynamisch basierend auf:
  - Aktuelle Gesamtbalance
  - Anzahl aktiver Bot-Instanzen mit gleicher Quote-Currency (Pool)
  - Sicherheitspuffer (safety_buffer_pct)

Kapital-Pools: EUR-Bots und USDT-Bots konkurrieren NICHT um dasselbe Kapital.
_count_active_bots() zählt nur Bots im gleichen Quote-Pool (erkannt via DB-Dateiname).
"""
import logging
import os
from glob import glob
from bot.config import RiskConfig

log = logging.getLogger("tradingbot.risk")

# DBs die keine Bot-Instanzen sind
_SKIP_DBS = {"candles.db", "news.db"}


def _quote_from_filename(filename: str) -> str:
    """
    Extrahiert Quote-Currency aus DB-Dateiname.
    BTC_EUR.db → EUR  |  BTC_USDT.db → USDT  |  TRUMP_EUR.db → EUR
    """
    name = os.path.basename(filename).replace(".db", "")
    parts = name.split("_")
    return parts[-1].upper() if len(parts) >= 2 else "EUR"


def _count_active_bots(db_dir: str, quote_currency: str = "") -> int:
    """
    Zählt Bot-Instanzen im gleichen Quote-Currency-Pool.
    Wenn quote_currency angegeben: nur Bots mit gleicher Quote (z.B. nur EUR-Bots).
    Sonst: alle Bot-DBs.
    """
    dbs = [
        p for p in glob(f"{db_dir}/*.db")
        if os.path.basename(p) not in _SKIP_DBS
    ]

    if not quote_currency:
        count = max(len(dbs), 1)
        log.debug(f"Aktive Bot-Instanzen gesamt: {count}")
        return count

    count = sum(1 for p in dbs if _quote_from_filename(p) == quote_currency.upper())
    count = max(count, 1)
    log.debug(f"Aktive Bot-Instanzen im {quote_currency}-Pool: {count}")
    return count


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check_guardrails(self, open_orders: list, balance: dict) -> tuple[bool, str]:
        if len(open_orders) >= self.cfg.max_open_orders:
            return False, f"Zu viele offene Orders ({len(open_orders)}/{self.cfg.max_open_orders})"

        quote_free     = balance.get("quote", 0.0)
        quote_currency = balance.get("quote_currency", "")
        num_bots       = _count_active_bots(self.cfg.db_dir, quote_currency)
        usable         = quote_free * (1 - self.cfg.safety_buffer_pct)
        per_bot        = usable / num_bots
        trade_quote    = per_bot * self.cfg.quote_risk_fraction

        if trade_quote < self.cfg.min_order_quote:
            return False, (
                f"Trade-Größe zu klein: {trade_quote:.2f} {quote_currency} "
                f"(Balance={quote_free:.2f}, Pool={num_bots} {quote_currency}-Bots, "
                f"Puffer={self.cfg.safety_buffer_pct*100:.0f}%)"
            )

        return True, "ok"

    def calc_buy_amount(self, balance: dict, last_price: float, exchange) -> float:
        quote_free     = balance.get("quote", 0.0)
        quote_currency = balance.get("quote_currency", "")
        num_bots       = _count_active_bots(self.cfg.db_dir, quote_currency)

        usable      = quote_free * (1 - self.cfg.safety_buffer_pct)
        per_bot     = usable / num_bots
        trade_quote = per_bot * self.cfg.quote_risk_fraction

        log.info(
            f"Sizing: Balance={quote_free:.2f} {quote_currency} | "
            f"Puffer={self.cfg.safety_buffer_pct*100:.0f}% | "
            f"Pool={num_bots} {quote_currency}-Bots | "
            f"pro Bot={per_bot:.2f} | Trade={trade_quote:.2f} {quote_currency}"
        )
        return trade_quote / last_price

    def calc_sell_amount(self, balance: dict) -> float:
        return balance.get("base", 0.0)
