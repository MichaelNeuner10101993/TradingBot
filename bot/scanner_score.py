"""
scanner_score.py — Scoring-Modul für den Trend Scanner.

Pure-Logic Modul: keine Side-Effects, keine I/O.
Bewertet Kraken EUR-Paare nach Momentum und Trend-Qualität.

Score-Tabelle (1h-Candles, 250 Stück):
  BULL      +3 | ADX>30  +2 | RSI 35-60  +1
  VOLATILE  +1 | ADX>25  +1 | SMA50>200  +1
  SIDEWAYS  -1 |             | Vol-Surge  +1
  EXTREME   -2
  BEAR      -3

Min-Score zum Starten: 4 (konfigurierbar)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from bot.regime import classify_regime
from bot.strategy import sma, rsi

# ─── Konstanten ───────────────────────────────────────────────────────────────

REGIME_SCORES: dict[str, int] = {
    "BULL":     +3,
    "VOLATILE": +1,
    "SIDEWAYS": -1,
    "EXTREME":  -2,
    "BEAR":     -3,
}

MIN_CANDLES_FOR_SMA200 = 210   # Mindest-Candles für zuverlässige SMA200

# Regimes, die einen Neustart BLOCKIEREN (unabhängig vom Score)
BLOCKED_START_REGIMES = {"BEAR", "EXTREME"}

# Regimes, bei denen Stopp-Kandidatur gilt
STOP_ELIGIBLE_REGIMES = {"BEAR", "SIDEWAYS"}


# ─── Datenklasse ──────────────────────────────────────────────────────────────

@dataclass
class PairScore:
    symbol: str
    total: int
    regime: str
    adx: float
    atr_pct: float
    rsi_val: Optional[float]
    sma50: Optional[float]
    sma200: Optional[float]
    sma50_above_sma200: bool
    volume_surge: bool
    # Einzelpunkte
    regime_pts: int
    adx_pts: int
    rsi_pts: int
    trend_pts: int
    volume_pts: int
    atr_pts: int = 0
    # Disqualifikation
    disqualified: bool = False
    disqualify_reason: Optional[str] = None


# ─── Haupt-Scoring-Funktion ────────────────────────────────────────────────────

def score_pair(symbol: str, candles_1h: list) -> PairScore:
    """
    Berechnet den Trend-Score für ein EUR-Paar.

    candles_1h: OHLCV-Liste [ts, open, high, low, close, volume], neueste Candle zuletzt.
                Mindestens 30 Einträge (besser 250+).

    Gibt immer ein PairScore zurück. Bei Problemen wird disqualified=True gesetzt.
    """
    # Basiswerte
    n = len(candles_1h)

    # Guard: zu wenig Candles für SMA200
    if n < MIN_CANDLES_FOR_SMA200:
        return _disqualified(symbol, f"nur {n} Candles, min {MIN_CANDLES_FOR_SMA200} nötig")

    closes  = [c[4] for c in candles_1h]
    volumes = [c[5] for c in candles_1h]

    # ── Indikatoren ────────────────────────────────────────────────────────────
    try:
        regime, adx_val, atr_pct = classify_regime(candles_1h)
    except Exception as e:
        return _disqualified(symbol, f"regime error: {e}")

    rsi_val  = rsi(closes, period=14)
    sma50_v  = sma(closes, 50)
    sma200_v = sma(closes, 200)

    # ── Volume-Surge: letzte 5 Candles avg vs. vorherige 20 ───────────────────
    if len(volumes) >= 25:
        recent_avg   = sum(volumes[-5:]) / 5
        baseline_avg = sum(volumes[-25:-5]) / 20
        vol_surge    = (baseline_avg > 0) and (recent_avg > baseline_avg)
    else:
        vol_surge = False

    # ── Trend-Alignment ────────────────────────────────────────────────────────
    sma50_above = (sma50_v is not None and sma200_v is not None and sma50_v > sma200_v)

    # ── Punkte ────────────────────────────────────────────────────────────────
    regime_pts = REGIME_SCORES.get(regime, 0)

    if adx_val >= 30:
        adx_pts = 2
    elif adx_val >= 25:
        adx_pts = 1
    else:
        adx_pts = 0

    rsi_pts   = 1 if (rsi_val is not None and 35 < rsi_val < 60) else 0
    trend_pts = 1 if sma50_above else 0
    vol_pts   = 1 if vol_surge else 0

    # ATR%-Punkte: zu ruhige Märkte sind ungeeignet (TP nicht erreichbar)
    if atr_pct >= 1.5:
        atr_pts = 2   # sehr aktiv → TP gut erreichbar
    elif atr_pct >= 0.7:
        atr_pts = 1   # aktiv → gut
    elif atr_pct < 0.3:
        atr_pts = -2  # zu ruhig → SL sicher, TP nie
    else:
        atr_pts = 0

    total = regime_pts + adx_pts + rsi_pts + trend_pts + vol_pts + atr_pts

    return PairScore(
        symbol              = symbol,
        total               = total,
        regime              = regime,
        adx                 = adx_val,
        atr_pct             = atr_pct,
        rsi_val             = rsi_val,
        sma50               = sma50_v,
        sma200              = sma200_v,
        sma50_above_sma200  = sma50_above,
        volume_surge        = vol_surge,
        regime_pts          = regime_pts,
        adx_pts             = adx_pts,
        rsi_pts             = rsi_pts,
        trend_pts           = trend_pts,
        volume_pts          = vol_pts,
        atr_pts             = atr_pts,
    )


# ─── Entscheidungs-Helfer ─────────────────────────────────────────────────────

def is_eligible_to_start(score: PairScore, min_score: int) -> tuple[bool, str]:
    """
    Prüft ob für diesen Score ein neuer Bot gestartet werden darf.

    Returns: (eligible, reason)
    """
    if score.disqualified:
        return False, f"disqualified: {score.disqualify_reason}"
    if score.regime in BLOCKED_START_REGIMES:
        return False, f"regime {score.regime} blockiert Start"
    if score.total < min_score:
        return False, f"score {score.total} < minimum {min_score}"
    return True, "ok"


def is_candidate_for_stop(
    regime: str,
    consecutive_sl: int,
    has_open_trade: bool,
    threshold: int,
) -> tuple[bool, str]:
    """
    Prüft ob ein laufender Bot gestoppt werden sollte.

    Stopp-Kriterien:
      - Kein offener Trade
      - Regime ist BEAR oder SIDEWAYS
      - consecutive_sl >= threshold

    Returns: (should_stop, reason)
    """
    if has_open_trade:
        return False, "offener Trade aktiv"
    if regime not in STOP_ELIGIBLE_REGIMES:
        return False, f"regime {regime} löst keinen Stopp aus"
    if consecutive_sl < threshold:
        return False, f"consecutive_sl {consecutive_sl} < {threshold}"
    return True, f"{consecutive_sl}× SL in Folge bei Regime {regime}"


# ─── Interner Helfer ──────────────────────────────────────────────────────────

def _disqualified(symbol: str, reason: str) -> PairScore:
    return PairScore(
        symbol=symbol, total=0, regime="?", adx=0.0, atr_pct=0.0,
        rsi_val=None, sma50=None, sma200=None,
        sma50_above_sma200=False, volume_surge=False,
        regime_pts=0, adx_pts=0, rsi_pts=0, trend_pts=0, volume_pts=0,
        disqualified=True, disqualify_reason=reason,
    )
