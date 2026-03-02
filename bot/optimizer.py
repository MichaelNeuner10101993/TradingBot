"""
Strategie-Optimierer: Simuliert mehrere SMA-Varianten auf historischen Candles
und wählt die profitabelste aus.

Wird vom Supervisor alle 5 Minuten aufgerufen – Ergebnisse landen in bot_state.
"""
import logging
import math

logger = logging.getLogger(__name__)

KRAKEN_FEE = 0.0026  # 0.26% pro Order
MIN_TRADES  = 5       # Varianten mit weniger Trades werden nicht gewertet

RSI_ATR_COMBOS: dict[str, list[dict]] = {
    "TREND": [
        {"rsi_buy_max": 65, "rsi_sell_min": 35, "atr_sl_mult": 1.2, "atr_tp_mult": 2.0},
        {"rsi_buy_max": 68, "rsi_sell_min": 32, "atr_sl_mult": 1.5, "atr_tp_mult": 2.5},  # Template
        {"rsi_buy_max": 72, "rsi_sell_min": 28, "atr_sl_mult": 2.0, "atr_tp_mult": 3.0},
    ],
    "SIDEWAYS": [
        {"rsi_buy_max": 57, "rsi_sell_min": 43, "atr_sl_mult": 1.0, "atr_tp_mult": 1.5},
        {"rsi_buy_max": 60, "rsi_sell_min": 40, "atr_sl_mult": 1.2, "atr_tp_mult": 1.8},  # Template
        {"rsi_buy_max": 63, "rsi_sell_min": 37, "atr_sl_mult": 1.5, "atr_tp_mult": 2.2},
    ],
    "VOLATILE": [
        {"rsi_buy_max": 52, "rsi_sell_min": 48, "atr_sl_mult": 1.8, "atr_tp_mult": 3.0},
        {"rsi_buy_max": 55, "rsi_sell_min": 45, "atr_sl_mult": 2.0, "atr_tp_mult": 3.5},  # Template
        {"rsi_buy_max": 58, "rsi_sell_min": 42, "atr_sl_mult": 2.5, "atr_tp_mult": 4.0},
    ],
}

STRATEGY_VARIANTS = [
    {"name": "Scalp",    "fast": 5,  "slow": 15},
    {"name": "Agile",    "fast": 7,  "slow": 18},
    {"name": "Standard", "fast": 9,  "slow": 21},
    {"name": "MACD",     "fast": 9,  "slow": 26},
    {"name": "Mittel",   "fast": 13, "slow": 34},
    {"name": "Swing",    "fast": 21, "slow": 55},
]


# ---------------------------------------------------------------------------
# Interne Zeitreihen-Indikatoren
# ---------------------------------------------------------------------------

def _sma_series(values: list, period: int) -> list:
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1:i + 1]) / period
    return result


def _rsi_series(closes: list, period: int = 14) -> list:
    """RSI-Zeitreihe (Simple Average, konsistent mit strategy.py:rsi())."""
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    for i in range(period, len(closes)):
        window = closes[i - period:i]
        deltas = [closes[i - period + j + 1] - closes[i - period + j] for j in range(period)]
        gains  = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        avg_g  = sum(gains)  / period
        avg_l  = sum(losses) / period
        if avg_l == 0:
            result[i] = 100.0
        else:
            result[i] = 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    return result


def _atr_series(highs: list, lows: list, closes: list, period: int = 14) -> list:
    """ATR-Zeitreihe (Simple Average über letzte `period` TRs)."""
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    trs = []
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i]  - closes[i - 1])
        trs.append(max(hl, hc, lc))
    for i in range(period, len(closes)):
        result[i] = sum(trs[i - period:i]) / period
    return result


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _net_pnl(entry: float, exit_price: float) -> float:
    """Netto P&L eines Roundtrips nach Gebühren."""
    return (exit_price / entry - 1) - 2 * KRAKEN_FEE


def simulate(
    candles: list,
    fast: int,
    slow: int,
    rsi_buy_max: float,
    rsi_sell_min: float,
    atr_sl_mult: float,
    atr_tp_mult: float,
    rsi_period: int = 14,
    atr_period: int = 14,
    use_trailing_sl: bool = False,
    trailing_sl_pct: float = 0.02,
    volume_filter: bool = False,
    volume_factor: float = 1.2,
) -> dict | None:
    """
    Simuliert eine Strategie auf historischen Candles.
    Candle-Format: [ts, open, high, low, close, volume]
    Gibt {pnl_pct, num_trades, win_rate} zurück oder None bei zu wenig Daten.
    """
    min_needed = slow + max(rsi_period, atr_period) + 2
    if len(candles) < min_needed:
        return None

    closes  = [c[4] for c in candles]
    highs   = [c[2] for c in candles]
    lows    = [c[3] for c in candles]
    volumes = [c[5] for c in candles] if volume_filter else None

    sma_f = _sma_series(closes, fast)
    sma_s = _sma_series(closes, slow)
    rsi_v = _rsi_series(closes, rsi_period)
    atr_v = _atr_series(highs, lows, closes, atr_period)

    capital  = 1000.0
    position = None
    trades   = []
    start    = max(slow, rsi_period, atr_period) + 1

    for i in range(start, len(candles)):
        sf_cur  = sma_f[i]
        ss_cur  = sma_s[i]
        sf_prv  = sma_f[i - 1]
        ss_prv  = sma_s[i - 1]
        rsi_cur = rsi_v[i]
        atr_cur = atr_v[i]

        if None in (sf_cur, ss_cur, sf_prv, ss_prv, rsi_cur, atr_cur):
            continue

        price = closes[i]
        high  = highs[i]
        low   = lows[i]

        if position:
            # Trailing-SL-Update (vor SL-Check – SL wird nur angehoben)
            if use_trailing_sl:
                trail = price * (1 - trailing_sl_pct)
                if trail > position["sl"]:
                    position["sl"] = trail

            # SL/TP prüfen
            if low <= position["sl"]:
                pnl = _net_pnl(position["entry"], position["sl"])
                capital *= (1 + pnl)
                trades.append(pnl)
                position = None
                continue
            if high >= position["tp"]:
                pnl = _net_pnl(position["entry"], position["tp"])
                capital *= (1 + pnl)
                trades.append(pnl)
                position = None
                continue
            # Signal-Close: SELL-Crossover
            if sf_cur < ss_cur and sf_prv >= ss_prv and rsi_cur > rsi_sell_min:
                pnl = _net_pnl(position["entry"], price)
                capital *= (1 + pnl)
                trades.append(pnl)
                position = None
        else:
            # Volumen-Filter: BUY nur bei überdurchschnittlichem Volumen
            vol_ok = True
            if volume_filter and volumes and i >= 21:
                avg_vol = sum(volumes[i - 20:i]) / 20
                vol_ok  = volumes[i] >= avg_vol * volume_factor

            # BUY-Crossover
            if vol_ok and sf_cur > ss_cur and sf_prv <= ss_prv and rsi_cur < rsi_buy_max:
                sl = price - atr_sl_mult * atr_cur
                tp = price + atr_tp_mult * atr_cur
                position = {"entry": price, "sl": sl, "tp": tp}

    # Offene Position zum letzten Kurs schließen
    if position:
        pnl = _net_pnl(position["entry"], closes[-1])
        capital *= (1 + pnl)
        trades.append(pnl)

    if not trades:
        return {"pnl_pct": 0.0, "num_trades": 0, "win_rate": 0.0, "sqn": 0.0}

    if len(trades) >= 2:
        mean_t   = sum(trades) / len(trades)
        variance = sum((t - mean_t) ** 2 for t in trades) / len(trades)
        std_t    = math.sqrt(max(variance, 1e-8))
        sqn      = (mean_t / std_t) * math.sqrt(len(trades))
    else:
        sqn = 0.0

    return {
        "pnl_pct":    round((capital - 1000.0) / 1000.0 * 100, 2),
        "num_trades": len(trades),
        "win_rate":   round(sum(1 for t in trades if t > 0) / len(trades), 2),
        "sqn":        round(sqn, 3),
    }


# ---------------------------------------------------------------------------
# Beste Variante wählen
# ---------------------------------------------------------------------------

def best_variant(
    candles: list,
    rsi_atr_variants: list | None = None,   # Liste von dicts mit rsi_buy_max etc.
    rsi_buy_max: float = 65.0,              # Backward-Compat: Einzel-Combo
    rsi_sell_min: float = 35.0,
    atr_sl_mult: float = 1.5,
    atr_tp_mult: float = 2.5,
    variants: list | None = None,
    use_trailing_sl: bool = False,
    trailing_sl_pct: float = 0.02,
    volume_filter: bool = False,
    volume_factor: float = 1.2,
) -> dict:
    """
    Testet alle SMA-Varianten × RSI/ATR-Kombos und gibt die beste zurück.
    Sortierung: SQN primär, P&L sekundär.
    Varianten mit < MIN_TRADES werden herausgefiltert (Fallback: alle, wenn keine qualifiziert).
    """
    if rsi_atr_variants is None:
        rsi_atr_variants = [{"rsi_buy_max": rsi_buy_max, "rsi_sell_min": rsi_sell_min,
                              "atr_sl_mult": atr_sl_mult, "atr_tp_mult": atr_tp_mult}]

    results = []
    for ra in rsi_atr_variants:
        for v in (variants or STRATEGY_VARIANTS):
            r = simulate(
                candles, v["fast"], v["slow"],
                ra["rsi_buy_max"], ra["rsi_sell_min"],
                ra["atr_sl_mult"],  ra["atr_tp_mult"],
                use_trailing_sl=use_trailing_sl,
                trailing_sl_pct=trailing_sl_pct,
                volume_filter=volume_filter,
                volume_factor=volume_factor,
            )
            if r is None:
                continue
            results.append({**v, **ra, **r,
                             "use_trailing_sl": use_trailing_sl,
                             "volume_filter":   volume_filter})
            logger.debug(
                "Variante %-8s (f=%2d s=%2d) rsi_buy=%.0f trailing=%s vol=%s: "
                "P&L=%+.2f%% Trades=%d SQN=%.2f",
                v["name"], v["fast"], v["slow"], ra["rsi_buy_max"],
                use_trailing_sl, volume_filter,
                r["pnl_pct"], r["num_trades"], r.get("sqn", 0),
            )

    # MIN_TRADES-Filter: Varianten mit zu wenig Trades nicht werten
    valid = [r for r in results if r["num_trades"] >= MIN_TRADES]
    pool  = valid if valid else results

    if not pool:
        fallback = next(v for v in STRATEGY_VARIANTS if v["name"] == "Standard")
        logger.warning("Kein Ergebnis (zu wenig Daten) – Fallback: Standard")
        return {**fallback, **RSI_ATR_COMBOS["TREND"][1],
                "pnl_pct": 0.0, "num_trades": 0, "win_rate": 0.0, "sqn": 0.0,
                "use_trailing_sl": use_trailing_sl, "volume_filter": volume_filter}

    pool.sort(key=lambda x: (x.get("sqn", 0), x["pnl_pct"]), reverse=True)
    best = pool[0]
    logger.info(
        "Beste Variante: %s (f=%d/s=%d) rsi_buy=%.0f trailing=%s vol=%s "
        "P&L=%+.2f%% Trades=%d SQN=%.2f",
        best["name"], best["fast"], best["slow"], best["rsi_buy_max"],
        best["use_trailing_sl"], best["volume_filter"],
        best["pnl_pct"], best["num_trades"], best.get("sqn", 0),
    )
    return best
