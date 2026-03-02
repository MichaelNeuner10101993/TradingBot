"""
Indikatoren-Library (portiert aus APEX).
Alle Indikatoren als reine numpy-Funktionen — keine Klassen, kein State.
Input: numpy arrays oder Listen. Output: float oder numpy array.

Verwendung:
  from bot.indicators import rsi_current, atr_current, ema_current
  from bot.indicators import sma, rsi, atr, ema, bollinger_bands, adx, vwap
"""
import numpy as np
from numpy.typing import NDArray


# ─── RSI ──────────────────────────────────────────────────────────────────────

def rsi(closes: NDArray, period: int = 14) -> NDArray:
    """
    Relative Strength Index (Wilder's EMA-Smoothing).
    Returns array gleicher Länge; erste (period) Werte sind NaN.
    """
    closes = np.asarray(closes, dtype=float)
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    result = np.full(len(closes), np.nan)

    # Erster Durchschnitt
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return result


def rsi_current(closes: NDArray, period: int = 14) -> float:
    """Aktueller RSI-Wert (letzter Wert). NaN wenn nicht genug Daten."""
    r = rsi(closes, period)
    vals = r[~np.isnan(r)]
    return float(vals[-1]) if len(vals) > 0 else float("nan")


# ─── EMA ──────────────────────────────────────────────────────────────────────

def ema(closes: NDArray, period: int) -> NDArray:
    """
    Exponential Moving Average (k = 2/(n+1)).
    Returns array gleicher Länge; erste (period-1) Werte sind NaN.
    """
    closes = np.asarray(closes, dtype=float)
    result = np.full(len(closes), np.nan)
    k = 2.0 / (period + 1)

    if len(closes) < period:
        return result
    result[period - 1] = np.mean(closes[:period])

    for i in range(period, len(closes)):
        result[i] = closes[i] * k + result[i - 1] * (1 - k)

    return result


def ema_current(closes: NDArray, period: int) -> float:
    """Aktueller EMA-Wert. NaN wenn nicht genug Daten."""
    e = ema(closes, period)
    vals = e[~np.isnan(e)]
    return float(vals[-1]) if len(vals) > 0 else float("nan")


# ─── SMA ──────────────────────────────────────────────────────────────────────

def sma(closes: NDArray, period: int) -> NDArray:
    """Simple Moving Average. Returns array gleicher Länge; erste (period-1) Werte sind NaN."""
    closes = np.asarray(closes, dtype=float)
    result = np.full(len(closes), np.nan)
    for i in range(period - 1, len(closes)):
        result[i] = np.mean(closes[i - period + 1 : i + 1])
    return result


# ─── MACD ─────────────────────────────────────────────────────────────────────

def macd(
    closes: NDArray,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[NDArray, NDArray, NDArray]:
    """
    MACD Line, Signal Line, Histogram.
    Returns: (macd_line, signal_line, histogram)
    """
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = fast_ema - slow_ema

    valid_macd = np.where(np.isnan(macd_line), 0.0, macd_line)
    signal_line = ema(valid_macd, signal_period)
    signal_line = np.where(np.isnan(macd_line), np.nan, signal_line)

    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def macd_current(closes: NDArray) -> tuple[float, float, float]:
    """Aktuelle MACD-Werte: (macd, signal, histogram)."""
    ml, sl, hist = macd(closes)
    def last_valid(arr):
        vals = arr[~np.isnan(arr)]
        return float(vals[-1]) if len(vals) > 0 else float("nan")
    return last_valid(ml), last_valid(sl), last_valid(hist)


# ─── Bollinger Bands ──────────────────────────────────────────────────────────

def bollinger_bands(
    closes: NDArray,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[NDArray, NDArray, NDArray]:
    """
    Bollinger Bands: (upper, middle, lower)
    middle = SMA(period), upper/lower = middle ± std_dev × StdDev
    """
    closes = np.asarray(closes, dtype=float)
    middle = sma(closes, period)
    result_upper = np.full(len(closes), np.nan)
    result_lower = np.full(len(closes), np.nan)

    for i in range(period - 1, len(closes)):
        std = np.std(closes[i - period + 1 : i + 1], ddof=0)
        result_upper[i] = middle[i] + std_dev * std
        result_lower[i] = middle[i] - std_dev * std

    return result_upper, middle, result_lower


def bb_width(closes: NDArray, period: int = 20, std_dev: float = 2.0) -> float:
    """BB-Width = (upper - lower) / middle × 100. NaN wenn nicht genug Daten."""
    upper, middle, lower = bollinger_bands(closes, period, std_dev)
    valid = ~(np.isnan(upper) | np.isnan(middle) | np.isnan(lower))
    if not np.any(valid) or middle[valid][-1] == 0:
        return float("nan")
    u, m, l = upper[valid][-1], middle[valid][-1], lower[valid][-1]
    return (u - l) / m * 100.0


def bb_current(
    closes: NDArray, period: int = 20, std_dev: float = 2.0
) -> tuple[float, float, float]:
    """Aktuelle BB-Werte: (upper, middle, lower)."""
    upper, middle, lower = bollinger_bands(closes, period, std_dev)
    def last_valid(arr):
        vals = arr[~np.isnan(arr)]
        return float(vals[-1]) if len(vals) > 0 else float("nan")
    return last_valid(upper), last_valid(middle), last_valid(lower)


# ─── ATR ──────────────────────────────────────────────────────────────────────

def atr(highs: NDArray, lows: NDArray, closes: NDArray, period: int = 14) -> NDArray:
    """
    Average True Range (Wilder's Smoothing).
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    highs  = np.asarray(highs,  dtype=float)
    lows   = np.asarray(lows,   dtype=float)
    closes = np.asarray(closes, dtype=float)

    n  = len(closes)
    tr = np.full(n, np.nan)

    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i]  - closes[i - 1])
        lc = abs(lows[i]   - closes[i - 1])
        tr[i] = max(hl, hc, lc)

    result   = np.full(n, np.nan)
    valid_tr = tr[~np.isnan(tr)]
    if len(valid_tr) < period:
        return result

    result[period] = np.mean(valid_tr[:period])
    for i in range(period + 1, n):
        if not np.isnan(tr[i]):
            result[i] = (result[i - 1] * (period - 1) + tr[i]) / period

    return result


def atr_current(
    highs: NDArray, lows: NDArray, closes: NDArray, period: int = 14
) -> float:
    """Aktueller ATR-Wert. NaN wenn nicht genug Daten."""
    a = atr(highs, lows, closes, period)
    vals = a[~np.isnan(a)]
    return float(vals[-1]) if len(vals) > 0 else float("nan")


# ─── VWAP ─────────────────────────────────────────────────────────────────────

def vwap(
    highs: NDArray, lows: NDArray, closes: NDArray, volumes: NDArray
) -> NDArray:
    """
    Volume Weighted Average Price (kumulativ).
    VWAP = Σ(typical_price × volume) / Σ(volume)
    """
    highs   = np.asarray(highs,   dtype=float)
    lows    = np.asarray(lows,    dtype=float)
    closes  = np.asarray(closes,  dtype=float)
    volumes = np.asarray(volumes, dtype=float)

    typical_price = (highs + lows + closes) / 3.0
    cum_tp_vol    = np.cumsum(typical_price * volumes)
    cum_vol       = np.cumsum(volumes)

    return np.where(cum_vol > 0, cum_tp_vol / cum_vol, np.nan)


def vwap_current(
    highs: NDArray, lows: NDArray, closes: NDArray, volumes: NDArray
) -> float:
    """Aktueller VWAP-Wert."""
    v = vwap(highs, lows, closes, volumes)
    vals = v[~np.isnan(v)]
    return float(vals[-1]) if len(vals) > 0 else float("nan")


# ─── ADX ──────────────────────────────────────────────────────────────────────

def adx(highs: NDArray, lows: NDArray, closes: NDArray, period: int = 14) -> NDArray:
    """
    Average Directional Index.
    Werte < 20 = kein Trend, > 25 = starker Trend.
    """
    highs  = np.asarray(highs,  dtype=float)
    lows   = np.asarray(lows,   dtype=float)
    closes = np.asarray(closes, dtype=float)
    n = len(closes)

    tr_arr   = np.full(n, np.nan)
    plus_dm  = np.full(n, np.nan)
    minus_dm = np.full(n, np.nan)

    for i in range(1, n):
        tr_arr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        up_move   = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i]  = up_move   if up_move   > down_move and up_move   > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move   and down_move > 0 else 0.0

    smooth_tr    = np.full(n, np.nan)
    smooth_plus  = np.full(n, np.nan)
    smooth_minus = np.full(n, np.nan)

    if n < period + 1:
        return np.full(n, np.nan)

    smooth_tr[period]    = np.nansum(tr_arr[1:period + 1])
    smooth_plus[period]  = np.nansum(plus_dm[1:period + 1])
    smooth_minus[period] = np.nansum(minus_dm[1:period + 1])

    for i in range(period + 1, n):
        smooth_tr[i]    = smooth_tr[i - 1]    - smooth_tr[i - 1]    / period + tr_arr[i]
        smooth_plus[i]  = smooth_plus[i - 1]  - smooth_plus[i - 1]  / period + plus_dm[i]
        smooth_minus[i] = smooth_minus[i - 1] - smooth_minus[i - 1] / period + minus_dm[i]

    di_plus  = np.where(smooth_tr > 0, smooth_plus  / smooth_tr * 100, 0.0)
    di_minus = np.where(smooth_tr > 0, smooth_minus / smooth_tr * 100, 0.0)

    denom = np.maximum(di_plus + di_minus, 1e-10)
    dx = np.where(
        (di_plus + di_minus) > 0,
        np.abs(di_plus - di_minus) / denom * 100,
        0.0,
    )

    result = np.full(n, np.nan)
    if 2 * period < n:
        result[2 * period] = np.mean(dx[period:2 * period + 1])
        for i in range(2 * period + 1, n):
            result[i] = (result[i - 1] * (period - 1) + dx[i]) / period

    return result


def adx_current(
    highs: NDArray, lows: NDArray, closes: NDArray, period: int = 14
) -> float:
    """Aktueller ADX-Wert. NaN wenn nicht genug Daten."""
    a = adx(highs, lows, closes, period)
    vals = a[~np.isnan(a)]
    return float(vals[-1]) if len(vals) > 0 else float("nan")


# ─── Volume Delta ─────────────────────────────────────────────────────────────

def volume_delta(volumes: NDArray, lookback: int = 5) -> float:
    """
    Volume-Trend über letzte `lookback` Perioden.
    Positiv = steigendes Volumen, Negativ = fallendes Volumen.
    Rückgabe: % Änderung vom ersten zum letzten Wert.
    """
    volumes = np.asarray(volumes, dtype=float)
    if len(volumes) < lookback:
        return float("nan")
    window = volumes[-lookback:]
    if window[0] == 0:
        return float("nan")
    return (window[-1] - window[0]) / window[0] * 100.0


# ─── Price Momentum ───────────────────────────────────────────────────────────

def price_momentum_pct(closes: NDArray, periods: int = 3) -> float:
    """Prozentuales Momentum über letzte `periods` Candles (absolut)."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < periods + 1:
        return float("nan")
    start = closes[-(periods + 1)]
    end   = closes[-1]
    if start == 0:
        return float("nan")
    return abs((end - start) / start * 100.0)
