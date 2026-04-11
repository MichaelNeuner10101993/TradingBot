"""
Stop-Loss / Take-Profit Monitor (synthetisch).

Logik:
- Nach jedem BUY wird ein Trade in der DB mit entry_price, sl_price, tp_price gespeichert.
- In jeder Hauptschleifen-Iteration prüft SlTpMonitor alle offenen Trades gegen den
  aktuellen Preis.
- Wird SL oder TP erreicht, gibt check() den Trade + Grund zurück → Executor führt SELL aus.

SL/TP-Berechnung:
- Primär: ATR-basiert (entry ± Multiplikator × ATR) → passt sich der Volatilität an.
- Fallback: feste Prozentsätze aus RiskConfig (wenn zu wenig Candles für ATR).
"""
import logging
from bot.config import RiskConfig
from bot.strategy import atr as _calc_atr

log = logging.getLogger("tradingbot.sl_tp")


def calc_levels(
    entry_price: float,
    cfg: RiskConfig,
    candles: list[list] | None = None,
) -> tuple[float, float]:
    """
    Berechnet SL- und TP-Preis aus Entry-Preis.
    Gibt (sl_price, tp_price) zurück.

    Wenn candles übergeben werden, wird ATR genutzt (dynamisch).
    Sonst Fallback auf feste Prozentsätze aus RiskConfig.
    """
    if candles:
        atr_val = _calc_atr(candles, cfg.atr_period)
        if atr_val and atr_val > 0:
            sl_price = entry_price - cfg.atr_sl_mult * atr_val
            tp_price = entry_price + cfg.atr_tp_mult * atr_val
            sl_pct   = (entry_price - sl_price) / entry_price * 100
            tp_pct   = (tp_price - entry_price) / entry_price * 100
            # TP-Cap: ATR-TP nie über MAX_TP_PCT (verhindert unrealistische Ziele)
            MAX_TP_PCT = 0.05  # 5% Maximum
            if tp_pct / 100 > MAX_TP_PCT:
                tp_price = entry_price * (1 + MAX_TP_PCT)
                tp_pct   = MAX_TP_PCT * 100
                log.info(f"ATR-TP gecappt auf {MAX_TP_PCT*100:.0f}% Maximum")
            log.info(
                f"ATR-Level: ATR={atr_val:.6f} "
                f"| SL={sl_price:.6f} (-{sl_pct:.2f}%) "
                f"| TP={tp_price:.6f} (+{tp_pct:.2f}%)"
            )
            # Wenn ATR-basierter SL zu eng ist (Markt zu ruhig für Fee-Gate),
            # auf feste Prozentsätze zurückfallen
            if sl_pct < cfg.stop_loss_pct * 100:
                log.info(
                    f"ATR-SL ({sl_pct:.2f}%) enger als Fixed-SL "
                    f"({cfg.stop_loss_pct*100:.1f}%) – Fallback auf feste Prozentsätze"
                )
            else:
                return sl_price, tp_price

    # Fallback: feste Prozentsätze (TP gecappt auf max 5%)
    _tp_pct  = min(cfg.take_profit_pct, 0.05)
    sl_price = entry_price * (1 - cfg.stop_loss_pct)
    tp_price = entry_price * (1 + _tp_pct)
    log.info(
        f"Feste Level (ATR nicht verfügbar): "
        f"SL={sl_price:.6f} (-{cfg.stop_loss_pct*100:.1f}%) "
        f"TP={tp_price:.6f} (+{cfg.take_profit_pct*100:.1f}%)"
    )
    return sl_price, tp_price


def update_trailing_sl(
    trade: dict,
    current_price: float,
    trailing_sl_pct: float,
) -> float | None:
    """
    Berechnet den neuen Trailing-SL-Preis.
    Gibt den neuen sl_price zurück wenn er den aktuellen überschreitet, sonst None.
    SL wird nur angehoben, nie abgesenkt.
    """
    trail_price = current_price * (1 - trailing_sl_pct)
    if trail_price > float(trade["sl_price"]):
        log.debug(
            f"Trailing-SL: {float(trade['sl_price']):.6f} → {trail_price:.6f} "
            f"(Preis={current_price:.6f} -{trailing_sl_pct*100:.1f}%)"
        )
        return trail_price
    return None


def check_breakeven(trade: dict, current_price: float, cfg: RiskConfig) -> bool:
    """
    Prüft ob der SL auf Entry-Preis angehoben werden soll (Breakeven-SL).
    Gibt True zurück wenn Gewinn >= breakeven_trigger_pct und SL noch unter Entry.
    """
    if not cfg.breakeven_enabled:
        return False
    entry = float(trade["entry_price"])
    sl    = float(trade["sl_price"])
    if sl >= entry:      # bereits auf/über Entry
        return False
    return (current_price - entry) / entry >= cfg.breakeven_trigger_pct


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
            sl    = trade["sl_price"]
            tp    = trade["tp_price"]
            entry = trade["entry_price"]

            if current_price <= sl:
                pct = (current_price - entry) / entry * 100
                log.warning(
                    f"STOP-LOSS ausgelöst | entry={entry:.6f} SL={sl:.6f} "
                    f"aktuell={current_price:.6f} ({pct:+.2f}%)"
                )
                triggered.append({"trade": trade, "reason": "sl_hit"})

            elif current_price >= tp:
                pct = (current_price - entry) / entry * 100
                log.info(
                    f"TAKE-PROFIT ausgelöst | entry={entry:.6f} TP={tp:.6f} "
                    f"aktuell={current_price:.6f} ({pct:+.2f}%)"
                )
                triggered.append({"trade": trade, "reason": "tp_hit"})

        return triggered
