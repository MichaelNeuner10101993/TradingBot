"""
Stop-Loss / Take-Profit Monitor (synthetisch).

Logik:
- Nach jedem BUY wird ein Trade in der DB mit entry_price, sl_price, tp_price gespeichert.
- In jeder Hauptschleifen-Iteration prüft SlTpMonitor alle offenen Trades gegen den
  aktuellen Preis.
- Wird SL oder TP erreicht, gibt check() den Trade + Grund zurück → Executor führt SELL aus.
"""
import logging
from bot.config import RiskConfig

log = logging.getLogger("tradingbot.sl_tp")


def calc_levels(entry_price: float, cfg: RiskConfig) -> tuple[float, float]:
    """
    Berechnet SL- und TP-Preis aus Entry-Preis und RiskConfig.
    Gibt (sl_price, tp_price) zurück.
    """
    sl_price = entry_price * (1 - cfg.stop_loss_pct)
    tp_price = entry_price * (1 + cfg.take_profit_pct)
    return sl_price, tp_price


class SlTpMonitor:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check(self, current_price: float, open_trades: list) -> list[dict]:
        """
        Prüft alle offenen Trades gegen den aktuellen Preis.
        Gibt Liste von {trade, reason} zurück für Trades die geschlossen werden sollen.
        reason: 'sl_hit' | 'tp_hit'
        """
        triggered = []
        for trade in open_trades:
            sl = trade["sl_price"]
            tp = trade["tp_price"]
            entry = trade["entry_price"]

            if current_price <= sl:
                pct = (current_price - entry) / entry * 100
                log.warning(
                    f"STOP-LOSS ausgelöst | entry={entry:.2f} SL={sl:.2f} "
                    f"aktuell={current_price:.2f} ({pct:+.2f}%)"
                )
                triggered.append({"trade": trade, "reason": "sl_hit"})

            elif current_price >= tp:
                pct = (current_price - entry) / entry * 100
                log.info(
                    f"TAKE-PROFIT ausgelöst | entry={entry:.2f} TP={tp:.2f} "
                    f"aktuell={current_price:.2f} ({pct:+.2f}%)"
                )
                triggered.append({"trade": trade, "reason": "tp_hit"})

        return triggered
