"""
Marktregime-Erkennung via ADX + EMA50/200 + BB-Width + RSI.

5 Regimes:
  BULL     — ADX > 22, EMA50 > EMA200 um ≥ 0.5%  → Aufwärtstrend bestätigt
  BEAR     — ADX > 22, EMA50 < EMA200 um ≥ 0.5%  → Abwärtstrend (BUY selektiv)
  SIDEWAYS — ADX < 22, BB-Width < Schwelle        → Range, kein klarer Trend
  VOLATILE — ATR% > 3% oder BB-Width > 4%         → Hohe Volatilität
  EXTREME  — RSI < 25 oder RSI > 75               → Extreme Überverkauft/Überkauft

Prioritätsreihenfolge (höchste zuerst):
  EXTREME → VOLATILE → (BULL | BEAR | SIDEWAYS anhand ADX + EMA-Abstand)
"""
import logging
import math
import numpy as np

log = logging.getLogger("tradingbot.regime")

# ─── Strategie-Parameter pro Regime ──────────────────────────────────────────
REGIME_TEMPLATES: dict[str, dict] = {
    "BULL": {
        # Aufwärtstrend bestätigt → RSI-Filter locker, normales Risk/Reward
        "rsi_buy_max":  68.0,
        "rsi_sell_min": 32.0,
        "atr_sl_mult":  1.5,
        "atr_tp_mult":  2.5,
    },
    "BEAR": {
        # Abwärtstrend → BUY nur bei starker Überverkauftheit, weites SL
        "rsi_buy_max":  45.0,
        "rsi_sell_min": 30.0,
        "atr_sl_mult":  2.0,
        "atr_tp_mult":  3.0,
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
    "EXTREME": {
        # Extremes RSI-Niveau → Kapitalschutz, BUY nur bei starker Überverkauftheit
        "rsi_buy_max":  30.0,
        "rsi_sell_min": 70.0,
        "atr_sl_mult":  1.2,
        "atr_tp_mult":  2.0,
    },
    # Legacy-Key für Rückwärtskompatibilität (ältere bot_state-Einträge / Peer-Instanzen)
    "TREND": {
        "rsi_buy_max":  68.0,
        "rsi_sell_min": 32.0,
        "atr_sl_mult":  1.5,
        "atr_tp_mult":  2.5,
    },
}

# ─── Schwellwerte ─────────────────────────────────────────────────────────────
ADX_TREND_MIN      = 22.0   # ADX > 22 → Trend vorhanden
VOLATILE_ATR_PCT   = 3.0    # ATR% > 3% → volatil
BB_VOLATILE_WIDTH  = 4.0    # BB-Width > 4% → volatil
EMA_BULL_THRESHOLD = +0.5   # EMA50 > EMA200 um ≥ 0.5% → BULL
EMA_BEAR_THRESHOLD = -0.5   # EMA50 < EMA200 um ≥ 0.5% → BEAR
RSI_EXTREME_LOW    = 25.0   # RSI < 25 → EXTREME
RSI_EXTREME_HIGH   = 75.0   # RSI > 75 → EXTREME


def classify_regime(
    candles: list[list],
    adx_period: int = 14,
    atr_period: int = 14,
) -> tuple[str, float, float]:
    """
    Klassifiziert das Marktregime für einen Coin.

    Gibt (regime, adx_val, atr_pct) zurück:
      regime:  "BULL" | "BEAR" | "SIDEWAYS" | "VOLATILE" | "EXTREME"
      adx_val: ADX-Wert oder -1.0 wenn nicht berechenbar
      atr_pct: ATR als Prozent des aktuellen Preises

    Mindestbedarf: 30 Candles (Fallback: SIDEWAYS).
    Für zuverlässige EMA200-Erkennung: ≥ 210 Candles empfohlen.
    """
    from bot.indicators import (
        adx_current    as _adx,
        atr_current    as _atr,
        ema_current    as _ema,
        rsi_current    as _rsi,
        bb_width       as _bb_width,
    )

    n = len(candles)
    if n < 30:
        log.debug(f"Regime: Zu wenig Candles ({n}) – Fallback SIDEWAYS")
        return "SIDEWAYS", -1.0, 0.0

    highs  = np.asarray([c[2] for c in candles], dtype=float)
    lows   = np.asarray([c[3] for c in candles], dtype=float)
    closes = np.asarray([c[4] for c in candles], dtype=float)
    last_close = float(closes[-1]) if closes[-1] > 0 else 1.0

    # ── Indikatoren berechnen ─────────────────────────────────────────────────
    adx_val  = _adx(highs, lows, closes, adx_period)
    atr_val  = _atr(highs, lows, closes, atr_period)
    rsi_val  = _rsi(closes, 14)
    bbw      = _bb_width(closes, 20, 2.0)

    # EMA50 / EMA200 mit Fallback auf kürzere Perioden bei wenig Daten
    ema50    = _ema(closes, 50)     if n >= 60  else _ema(closes, max(10, n // 4))
    ema200   = _ema(closes, 200)    if n >= 210 else _ema(closes, max(20, n // 2))

    # NaN-Safety
    if math.isnan(adx_val):  adx_val = 20.0
    if math.isnan(rsi_val):  rsi_val = 50.0
    if math.isnan(bbw):      bbw     = 2.0
    if math.isnan(ema50):    ema50   = last_close
    if math.isnan(ema200):   ema200  = last_close

    atr_pct = (atr_val / last_close * 100) if (not math.isnan(atr_val) and last_close > 0) else 0.0

    # EMA-Abstand (%) — positiv = bullish, negativ = bearish
    ema_diff_pct = (ema50 - ema200) / ema200 * 100 if ema200 > 0 else 0.0

    # ── Regime-Klassifikation (Priorität von oben nach unten) ─────────────────

    # 1. EXTREME: RSI-Extremwert (höchste Priorität — Kapitalschutz)
    if rsi_val < RSI_EXTREME_LOW or rsi_val > RSI_EXTREME_HIGH:
        log.info(
            f"Regime: EXTREME | RSI={rsi_val:.1f} | ADX={adx_val:.1f} "
            f"| EMA-Δ={ema_diff_pct:+.2f}% | ATR%={atr_pct:.2f}%"
        )
        return "EXTREME", adx_val, atr_pct

    # 2. VOLATILE: Hohe Volatilität überschreibt Trendrichtung
    if atr_pct > VOLATILE_ATR_PCT or bbw > BB_VOLATILE_WIDTH:
        log.info(
            f"Regime: VOLATILE | ATR%={atr_pct:.2f}% BB-Width={bbw:.2f}% "
            f"| ADX={adx_val:.1f} | EMA-Δ={ema_diff_pct:+.2f}%"
        )
        return "VOLATILE", adx_val, atr_pct

    # 3. SIDEWAYS: Kein klarer Trend
    if adx_val < ADX_TREND_MIN:
        log.info(
            f"Regime: SIDEWAYS | ADX={adx_val:.1f} < {ADX_TREND_MIN} "
            f"| BB-Width={bbw:.2f}% | ATR%={atr_pct:.2f}%"
        )
        return "SIDEWAYS", adx_val, atr_pct

    # 4. Trend vorhanden → BULL vs BEAR anhand EMA50/200-Abstand
    if ema_diff_pct >= EMA_BULL_THRESHOLD:
        log.info(
            f"Regime: BULL | ADX={adx_val:.1f} | EMA-Δ={ema_diff_pct:+.2f}% "
            f"(≥+{EMA_BULL_THRESHOLD}%) | ATR%={atr_pct:.2f}%"
        )
        return "BULL", adx_val, atr_pct

    if ema_diff_pct <= EMA_BEAR_THRESHOLD:
        log.info(
            f"Regime: BEAR | ADX={adx_val:.1f} | EMA-Δ={ema_diff_pct:+.2f}% "
            f"(≤{EMA_BEAR_THRESHOLD}%) | ATR%={atr_pct:.2f}%"
        )
        return "BEAR", adx_val, atr_pct

    # 5. ADX > Schwelle aber EMA50 ≈ EMA200 (Übergangsbereich) → SIDEWAYS
    log.info(
        f"Regime: SIDEWAYS (EMA-Übergang) | ADX={adx_val:.1f} "
        f"| EMA-Δ={ema_diff_pct:+.2f}% | ATR%={atr_pct:.2f}%"
    )
    return "SIDEWAYS", adx_val, atr_pct
