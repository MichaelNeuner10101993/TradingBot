"""
Marktregime-Erkennung via ADX + relative ATR.

Klassifiziert den Markt als TREND / SIDEWAYS / VOLATILE
und liefert passende Strategie-Parameter pro Regime.
"""
import logging

log = logging.getLogger("tradingbot.regime")

# Strategie-Parameter pro Regime
REGIME_TEMPLATES: dict[str, dict] = {
    "TREND": {
        # Trend bestätigt → RSI-Filter entspannter, normales Risk/Reward
        "rsi_buy_max":  68.0,
        "rsi_sell_min": 32.0,
        "atr_sl_mult":  1.5,
        "atr_tp_mult":  2.5,
    },
    "SIDEWAYS": {
        # Kein klarer Trend → selektiverer RSI, früherer TP (Range-Ziel)
        "rsi_buy_max":  60.0,
        "rsi_sell_min": 40.0,
        "atr_sl_mult":  1.2,
        "atr_tp_mult":  1.8,
    },
    "VOLATILE": {
        # Hohe Volatilität → sehr selektiv, weites SL damit Rauschen nicht triggert
        "rsi_buy_max":  55.0,
        "rsi_sell_min": 45.0,
        "atr_sl_mult":  2.0,
        "atr_tp_mult":  3.5,
    },
}

# Schwellwerte
ADX_TREND_MIN    = 22.0   # ADX > 22 → Trend vorhanden
VOLATILE_ATR_PCT = 3.0    # ATR > 3% des Preises → volatil


def adx(candles: list[list], period: int = 14) -> float | None:
    """
    Average Directional Index (Wilder's DMI).
    candles: [ts, open, high, low, close, volume]
    Mindestbedarf: 2 × period + 1 Candles.
    Gibt ADX-Wert (0–100) oder None zurück.
    """
    n = len(candles)
    if n < 2 * period + 1:
        log.debug(f"ADX: Zu wenig Candles ({n} < {2 * period + 1})")
        return None

    # Schritt 1: True Range, +DM, -DM
    trs, pdms, ndms = [], [], []
    for i in range(1, n):
        high, low                     = candles[i][2],   candles[i][3]
        prev_high, prev_low, prev_close = candles[i-1][2], candles[i-1][3], candles[i-1][4]

        tr   = max(high - low, abs(high - prev_close), abs(low - prev_close))
        up   = high - prev_high
        down = prev_low - low
        pdm  = up   if (up   > down and up   > 0) else 0.0
        ndm  = down if (down > up   and down > 0) else 0.0

        trs.append(tr)
        pdms.append(pdm)
        ndms.append(ndm)

    # Schritt 2: Wilder-Smoothing – Summe der ersten `period` Werte, danach gleitendes Mittel
    def _wilder(vals: list[float]) -> list[float]:
        smoothed = [sum(vals[:period])]
        for v in vals[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    s_tr  = _wilder(trs)
    s_pdm = _wilder(pdms)
    s_ndm = _wilder(ndms)

    # Schritt 3: DX berechnen
    dx_vals = []
    for str_, spdm, sndm in zip(s_tr, s_pdm, s_ndm):
        if str_ == 0:
            dx_vals.append(0.0)
            continue
        di_plus  = 100.0 * spdm / str_
        di_minus = 100.0 * sndm / str_
        di_sum   = di_plus + di_minus
        dx_vals.append(100.0 * abs(di_plus - di_minus) / di_sum if di_sum else 0.0)

    # Schritt 4: ADX = Wilder-Smoothing über DX-Werte
    if len(dx_vals) < period:
        return None
    adx_val = sum(dx_vals[:period]) / period
    for dx in dx_vals[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period

    return adx_val


def classify_regime(
    candles: list[list],
    adx_period: int = 14,
    atr_period: int = 14,
) -> tuple[str, float, float]:
    """
    Klassifiziert das Marktregime für einen Coin.

    Gibt (regime, adx_val, atr_pct) zurück:
      regime:  "TREND" | "SIDEWAYS" | "VOLATILE"
      adx_val: ADX-Wert oder -1.0 wenn nicht berechenbar
      atr_pct: ATR als Prozent des aktuellen Preises
    """
    from bot.strategy import atr as _calc_atr

    closes     = [c[4] for c in candles]
    last_close = closes[-1] if closes else 1.0
    atr_val    = _calc_atr(candles, atr_period) or 0.0
    atr_pct    = (atr_val / last_close * 100) if last_close else 0.0

    # Volatile überschreibt alles
    if atr_pct > VOLATILE_ATR_PCT:
        log.info(f"Regime: VOLATILE | ATR%={atr_pct:.2f}% > {VOLATILE_ATR_PCT}%")
        return "VOLATILE", -1.0, atr_pct

    adx_val = adx(candles, adx_period)
    if adx_val is None:
        log.debug("ADX nicht berechenbar – Fallback TREND")
        return "TREND", -1.0, atr_pct

    regime = "TREND" if adx_val > ADX_TREND_MIN else "SIDEWAYS"
    log.info(f"Regime: {regime} | ADX={adx_val:.1f} | ATR%={atr_pct:.2f}%")
    return regime, adx_val, atr_pct
