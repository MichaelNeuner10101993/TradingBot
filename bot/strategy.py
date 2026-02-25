"""
Strategy Engine: erzeugt Signale (BUY / SELL / HOLD) aus OHLCV-Daten.
Aktuell: SMA-Crossover (fast/slow).
"""
import logging
from typing import Literal

log = logging.getLogger("tradingbot.strategy")

Signal = Literal["BUY", "SELL", "HOLD"]


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def sma_crossover(closes: list[float], fast: int, slow: int) -> Signal:
    """
    SMA-Crossover-Signal.
    Vergleicht vorletzte und letzte Candle, um einen Crossover zu erkennen.
    """
    if len(closes) < slow + 1:
        log.debug("Zu wenig Candles für Signal")
        return "HOLD"

    prev = closes[:-1]
    curr = closes

    f_prev = sma(prev, fast)
    s_prev = sma(prev, slow)
    f_curr = sma(curr, fast)
    s_curr = sma(curr, slow)

    if None in (f_prev, s_prev, f_curr, s_curr):
        return "HOLD"

    if f_prev <= s_prev and f_curr > s_curr:
        log.info(f"Signal: BUY (fast={f_curr:.2f} kreuzt slow={s_curr:.2f} nach oben)")
        return "BUY"

    if f_prev >= s_prev and f_curr < s_curr:
        log.info(f"Signal: SELL (fast={f_curr:.2f} kreuzt slow={s_curr:.2f} nach unten)")
        return "SELL"

    return "HOLD"


def get_signal(candles: list[list], fast: int, slow: int) -> tuple[Signal, float]:
    """
    Gibt (Signal, letzter Close-Preis) zurück.
    candles: Liste von [timestamp, open, high, low, close, volume]
    """
    closes = [c[4] for c in candles]
    last_price = closes[-1] if closes else 0.0
    signal = sma_crossover(closes, fast, slow)
    return signal, last_price
