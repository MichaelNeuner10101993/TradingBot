"""
Strategy Engine: erzeugt Signale (BUY / SELL / HOLD) aus OHLCV-Daten.
Indikatoren kommen aus bot.indicators (numpy, Wilder's EMA-Smoothing).

Öffentliche API (rückwärtskompatibel):
  sma(values, n) → float | None
  rsi(closes, period) → float | None          ← jetzt Wilder's Smoothing
  atr(candles, period) → float | None         ← jetzt Wilder's Smoothing
  sma_crossover(closes, fast, slow) → Signal
  get_signal(candles, ...) → (Signal, price, rsi_val)
  is_htf_bullish(candles, fast, slow) → bool
"""
import logging
import numpy as np
from typing import Literal

from bot.indicators import (
    sma  as _sma_arr,
    rsi_current  as _rsi_current,
    atr_current  as _atr_current,
)

log = logging.getLogger("tradingbot.strategy")

Signal = Literal["BUY", "SELL", "HOLD"]


def sma(values: list[float], n: int) -> float | None:
    """SMA-Wrapper: gibt letzten gültigen Wert zurück oder None."""
    arr   = _sma_arr(np.asarray(values, dtype=float), n)
    valid = arr[~np.isnan(arr)]
    return float(valid[-1]) if len(valid) > 0 else None


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Relative Strength Index (Wilder's EMA-Smoothing). None wenn nicht genug Daten."""
    result = _rsi_current(np.asarray(closes, dtype=float), period)
    return None if np.isnan(result) else result


def atr(candles: list[list], period: int = 14) -> float | None:
    """Average True Range aus OHLCV-Candles [ts, open, high, low, close, vol]."""
    if len(candles) < period + 1:
        return None
    highs  = np.asarray([c[2] for c in candles], dtype=float)
    lows   = np.asarray([c[3] for c in candles], dtype=float)
    closes = np.asarray([c[4] for c in candles], dtype=float)
    result = _atr_current(highs, lows, closes, period)
    return None if np.isnan(result) else result


def is_htf_bullish(candles: list[list], fast: int, slow: int) -> bool:
    """True wenn fast SMA >= slow SMA im höheren Timeframe (Aufwärtstrend). Zu wenig Daten → nicht filtern."""
    closes = [c[4] for c in candles]
    if len(closes) < slow:
        return True
    f = sma(closes, fast)
    s = sma(closes, slow)
    if f is None or s is None:
        return True
    return f >= s


def sma_crossover(closes: list[float], fast: int, slow: int) -> Signal:
    """
    SMA-Crossover-Signal.
    Vergleicht vorletzte und letzte Candle, um einen Crossover zu erkennen.
    """
    if len(closes) < slow + 1:
        log.debug("Zu wenig Candles für Signal")
        return "HOLD"

    arr      = np.asarray(closes, dtype=float)
    fast_arr = _sma_arr(arr, fast)
    slow_arr = _sma_arr(arr, slow)

    f_prev, f_curr = fast_arr[-2], fast_arr[-1]
    s_prev, s_curr = slow_arr[-2], slow_arr[-1]

    if any(np.isnan(x) for x in (f_prev, s_prev, f_curr, s_curr)):
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
    volume_filter: bool = False,
    volume_factor: float = 1.2,
) -> tuple[Signal, float, float | None]:
    """
    Gibt (Signal, letzter Close-Preis, RSI-Wert) zurück.
    RSI filtert überkaufte BUY- und überverkaufte SELL-Signale heraus.
    Optionaler Volumen-Filter: Signal nur wenn letztes Volumen > volume_factor × Avg(20).
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

    # Volumen-Filter: Signal nur bei überdurchschnittlichem Volumen (kein Look-Ahead)
    if volume_filter and signal != "HOLD" and len(candles) >= 21:
        volumes    = [c[5] for c in candles]
        last_vol   = volumes[-1]
        avg_vol_20 = sum(volumes[-21:-1]) / 20
        threshold  = avg_vol_20 * volume_factor
        if last_vol < threshold:
            log.info(
                f"{signal} gefiltert: Volumen={last_vol:.2f} < {threshold:.2f} "
                f"(Avg20={avg_vol_20:.2f} × {volume_factor})"
            )
            signal = "HOLD"
        else:
            log.debug(f"Volumen-Filter OK: {last_vol:.2f} >= {threshold:.2f}")

    return signal, last_price, rsi_val
