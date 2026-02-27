"""
Strategy Engine: erzeugt Signale (BUY / SELL / HOLD) aus OHLCV-Daten.
Enthält: SMA-Crossover, RSI-Filter, ATR-Berechnung.
"""
import logging
from typing import Literal

log = logging.getLogger("tradingbot.strategy")

Signal = Literal["BUY", "SELL", "HOLD"]


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Relative Strength Index (Wilder-Methode, vereinfacht als Simple-Average)."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains  = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[list], period: int = 14) -> float | None:
    """Average True Range aus OHLCV-Candles [ts, open, high, low, close, vol]."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high       = candles[i][2]
        low        = candles[i][3]
        prev_close = candles[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / period


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
        log.info(f"Signal: BUY (fast={f_curr:.4f} kreuzt slow={s_curr:.4f} nach oben)")
        return "BUY"

    if f_prev >= s_prev and f_curr < s_curr:
        log.info(f"Signal: SELL (fast={f_curr:.4f} kreuzt slow={s_curr:.4f} nach unten)")
        return "SELL"

    return "HOLD"


def get_signal(
    candles: list[list],
    fast: int,
    slow: int,
    rsi_period: int = 14,
    rsi_buy_max: float = 65.0,
    rsi_sell_min: float = 35.0,
) -> tuple[Signal, float, float | None]:
    """
    Gibt (Signal, letzter Close-Preis, RSI-Wert) zurück.
    RSI filtert überkaufte BUY- und überverkaufte SELL-Signale heraus.
    candles: Liste von [timestamp, open, high, low, close, volume]
    """
    closes     = [c[4] for c in candles]
    last_price = closes[-1] if closes else 0.0
    signal     = sma_crossover(closes, fast, slow)
    rsi_val    = rsi(closes, rsi_period)

    if rsi_val is not None and signal != "HOLD":
        if signal == "BUY" and rsi_val > rsi_buy_max:
            log.info(f"BUY gefiltert: RSI={rsi_val:.1f} > {rsi_buy_max} (überkauft)")
            signal = "HOLD"
        elif signal == "SELL" and rsi_val < rsi_sell_min:
            log.info(f"SELL gefiltert: RSI={rsi_val:.1f} < {rsi_sell_min} (überverkauft)")
            signal = "HOLD"

    return signal, last_price, rsi_val
