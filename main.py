"""
Trading Bot – Entry Point
Startet die Hauptschleife: Data -> Signal -> Risk -> Execute -> Persist

Verwendung (eine Instanz pro Coin):
  python main.py --symbol SNX/EUR
  python main.py --symbol BTC/EUR --timeframe 1m --dry-run
"""
import argparse
import random
import signal as _signal
import time
import uuid
import ccxt

import os
from datetime import datetime, timedelta, timezone
from bot.config import ExchangeConfig, BotConfig, RiskConfig, OpsConfig
from bot.ops import setup_logging, CircuitBreaker
from bot.data_feed import DataFeed, build_exchange
from bot.strategy import get_signal, is_htf_bullish
from bot.risk import RiskManager
from bot.execution import Executor
from bot.persistence import StateDB, utcnow
from bot.sl_tp import SlTpMonitor, calc_levels, update_trailing_sl, check_breakeven
from bot import pyramid, notify


def _timeframe_to_seconds(tf: str) -> int:
    _map = {"m": 60, "h": 3600, "d": 86400}
    return int(tf[:-1]) * _map.get(tf[-1].lower(), 60)


def parse_args():
    p = argparse.ArgumentParser(description="Trading Bot")
    p.add_argument("--symbol",     default=None,  help="z.B. SNX/EUR")
    p.add_argument("--timeframe",  default=None,  help="z.B. 1m, 5m, 15m")
    p.add_argument("--fast",       type=int, default=None, help="Fast-SMA-Periode")
    p.add_argument("--slow",       type=int, default=None, help="Slow-SMA-Periode")
    p.add_argument("--sl",         type=float, default=None, help="Stop-Loss %% (z.B. 0.03)")
    p.add_argument("--tp",         type=float, default=None, help="Take-Profit %% (z.B. 0.06)")
    p.add_argument("--safety-buffer", type=float, default=None, help="Sicherheitspuffer 0-1 (default: 0.10 = 10%%)")
    p.add_argument("--dry-run",       action="store_true", default=None)
    p.add_argument("--live",          action="store_true", help="Setzt dry_run=False")
    p.add_argument("--db",            default=None, help="Pfad zur SQLite-DB")
    p.add_argument("--log-dir",       default=None, help="Log-Verzeichnis")
    p.add_argument("--startup-delay",  type=int,   default=0,    help="Sekunden warten vor erster API-Anfrage (API-Rate-Limit bei mehreren Bots staffeln)")
    p.add_argument("--trailing-sl",    action="store_true",       help="Trailing Stop-Loss aktivieren")
    p.add_argument("--trailing-sl-pct", type=float, default=None, help="Trailing-SL-Abstand in Dezimal (z.B. 0.02 = 2%%)")
    p.add_argument("--sl-cooldown",    type=int,   default=None,  help="Cooldown-Candles nach SL-Hit (default: 3)")
    p.add_argument("--volume-filter",  action="store_true",       help="Volumen-Filter aktivieren")
    p.add_argument("--volume-factor",  type=float, default=None,  help="Volumen-Faktor (default: 1.2)")
    p.add_argument("--breakeven",         action="store_true",        help="Breakeven-SL aktivieren")
    p.add_argument("--breakeven-pct",     type=float, default=None,   help="Breakeven-Trigger %% (default: 0.01 = 1%%)")
    p.add_argument("--partial-tp",        action="store_true",        help="Partial Take-Profit aktivieren")
    p.add_argument("--partial-tp-fraction", type=float, default=None, help="Anteil für Partial-TP (default: 0.5 = 50%%)")
    p.add_argument("--htf-timeframe",     default="",                 help="HTF-Timeframe für Trendfilter (z.B. 1h), leer = deaktiviert")
    p.add_argument("--htf-fast",          type=int,   default=None,   help="HTF Fast-SMA-Periode (default: 9)")
    p.add_argument("--htf-slow",          type=int,   default=None,   help="HTF Slow-SMA-Periode (default: 21)")
    p.add_argument("--sma200-filter",     action="store_true",        help="BUY nur wenn Preis über SMA200 (langfristiger Aufwärtstrend)")
    p.add_argument("--slope-filter",      action="store_true",        help="BUY nur wenn Slow-SMA nicht stark fällt")
    p.add_argument("--slope-lookback",    type=int,   default=None,   help="Lookback-Candles für Slope-Filter (default: 20)")
    p.add_argument("--slope-min-pct",     type=float, default=None,   help="Min. Slope %% für BUY (default: -0.15)")
    return p.parse_args()


def main():
    # SIGTERM (von systemctl stop) → KeyboardInterrupt → finally-Block setzt status="stopped"
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    _signal.signal(_signal.SIGTERM, _on_sigterm)

    args = parse_args()
    _startup_warnings = []  # Gesammelt vor log-Initialisierung

    # --- Konfiguration (CLI überschreibt Defaults) ---
    exchange_cfg = ExchangeConfig()
    bot_cfg      = BotConfig()
    risk_cfg     = RiskConfig()
    ops_cfg      = OpsConfig()

    # SNX_EUR → SNX/EUR (systemd erlaubt kein / im Instanznamen)
    if args.symbol:    bot_cfg.symbol    = args.symbol.replace("_", "/", 1)
    if args.timeframe: bot_cfg.timeframe = args.timeframe
    if args.fast:      bot_cfg.fast_period = args.fast
    if args.slow:      bot_cfg.slow_period = args.slow
    if args.sl:        risk_cfg.stop_loss_pct   = args.sl
    if args.tp:        risk_cfg.take_profit_pct  = args.tp
    if args.safety_buffer is not None: risk_cfg.safety_buffer_pct = args.safety_buffer

    # CLI-Feature-Flags (haben Vorrang vor Supervisor-Empfehlung)
    _cli_trailing_set = args.trailing_sl
    _cli_vol_set      = args.volume_filter
    # Laufzeit-Flags: werden gesetzt wenn User via Dashboard/Telegram überschreibt
    _rt_trailing_set  = False
    _rt_vol_set       = False
    _rt_rsi_set       = False
    _rt_sma_set       = False
    if args.trailing_sl:                    risk_cfg.use_trailing_sl    = True
    if args.trailing_sl_pct is not None:
        _tsp = args.trailing_sl_pct
        if _tsp > 1.0:  # > 100%: Prozentwert statt Fraktion in bot.conf.d
            _startup_warnings.append(
                f"--trailing-sl-pct {_tsp} wirkt wie Prozentwert (>1.0=100%!) — "
                f"korrigiert zu {round(_tsp/100,6):.6f} (3% = 0.03, nicht 3.0)"
            )
            _tsp = round(_tsp / 100, 6)
        risk_cfg.trailing_sl_pct = _tsp
    if args.sl_cooldown is not None:        risk_cfg.sl_cooldown_candles = args.sl_cooldown
    if args.volume_filter:                  risk_cfg.volume_filter      = True
    if args.volume_factor is not None:      risk_cfg.volume_factor      = args.volume_factor
    if args.breakeven:                      risk_cfg.breakeven_enabled  = True
    if args.breakeven_pct is not None:      risk_cfg.breakeven_trigger_pct = args.breakeven_pct
    if args.partial_tp:                     risk_cfg.partial_tp_enabled = True
    if args.partial_tp_fraction is not None: risk_cfg.partial_tp_fraction = args.partial_tp_fraction
    if args.htf_timeframe:                  bot_cfg.htf_timeframe       = args.htf_timeframe
    if args.htf_fast is not None:           bot_cfg.htf_fast            = args.htf_fast
    if args.htf_slow is not None:           bot_cfg.htf_slow            = args.htf_slow
    if args.sma200_filter:                  risk_cfg.sma200_filter      = True
    if args.slope_filter:                   risk_cfg.slope_filter       = True
    if args.slope_lookback is not None:     risk_cfg.slope_lookback     = args.slope_lookback
    if args.slope_min_pct is not None:      risk_cfg.slope_min_pct      = args.slope_min_pct

    # DB-Verzeichnis für Bot-Zählung
    risk_cfg.db_dir = os.path.dirname(ops_cfg.db_path) or "db"
    if args.live:      bot_cfg.dry_run = False
    if args.dry_run:   bot_cfg.dry_run = True

    # DB und Logs pro Symbol trennen
    symbol_safe = bot_cfg.symbol.replace("/", "_")
    ops_cfg.db_path  = args.db      or f"db/{symbol_safe}.db"
    ops_cfg.log_dir  = args.log_dir or f"logs/{symbol_safe}"

    # --- Initialisierung ---
    log = setup_logging(ops_cfg)
    for _w in _startup_warnings:
        log.error(f"KONFIGURATIONSFEHLER korrigiert: {_w}")
    if args.startup_delay > 0:
        log.info(f"Startup-Delay: {args.startup_delay}s warten …")
        time.sleep(args.startup_delay)
    log.info(
        f"Bot startet | symbol={bot_cfg.symbol} | tf={bot_cfg.timeframe} "
        f"| SMA {bot_cfg.fast_period}/{bot_cfg.slow_period} "
        f"| RSI({risk_cfg.rsi_period}) buy<{risk_cfg.rsi_buy_max} sell>{risk_cfg.rsi_sell_min} "
        f"| ATR({risk_cfg.atr_period}) SL×{risk_cfg.atr_sl_mult} TP×{risk_cfg.atr_tp_mult} "
        f"| Fallback SL={risk_cfg.stop_loss_pct*100:.1f}% TP={risk_cfg.take_profit_pct*100:.1f}% "
        f"| dry_run={bot_cfg.dry_run} | db={ops_cfg.db_path}"
    )

    db       = StateDB(ops_cfg.db_path)

    # Feature 5: Warmstart – letzte effektive Parameter aus DB laden wenn Supervisor noch nicht lief
    _warmstart = db.get_all_state()
    if not _warmstart.get("supervisor_regime") and _warmstart.get("effective_fast"):
        log.info("Warmstart: übernehme letzte effektive Parameter aus DB")
        if not _cli_trailing_set:
            bot_cfg.fast_period      = int(_warmstart.get("effective_fast",  bot_cfg.fast_period))
            bot_cfg.slow_period      = int(_warmstart.get("effective_slow",  bot_cfg.slow_period))
            risk_cfg.rsi_buy_max     = float(_warmstart.get("effective_rsi_buy_max",  risk_cfg.rsi_buy_max))
            risk_cfg.rsi_sell_min    = float(_warmstart.get("effective_rsi_sell_min", risk_cfg.rsi_sell_min))
            risk_cfg.atr_sl_mult     = float(_warmstart.get("effective_atr_sl_mult",  risk_cfg.atr_sl_mult))
            risk_cfg.atr_tp_mult     = float(_warmstart.get("effective_atr_tp_mult",  risk_cfg.atr_tp_mult))

    # Feature 4: Initialer Cleanup beim Start
    db.cleanup_old_records(days=risk_cfg.cleanup_days)
    db.set_state("last_cleanup", datetime.now(timezone.utc).isoformat())

    exchange = build_exchange(exchange_cfg)
    feed     = DataFeed(exchange, bot_cfg)
    risk     = RiskManager(risk_cfg)
    executor = Executor(exchange, bot_cfg, risk_cfg, db)
    sl_tp    = SlTpMonitor(risk_cfg)
    breaker  = CircuitBreaker(risk_cfg.max_consecutive_errors, logger=log)

    # --- Hauptschleife ---
    try:
        while True:
            try:
                # 1) Marktdaten holen
                candles     = feed.fetch_ohlcv()
                balance     = feed.fetch_balance()
                open_orders = feed.fetch_open_orders()
                # HTF-Candles für Multi-Timeframe-Filter (Feature 3)
                candles_htf = None
                if bot_cfg.htf_timeframe:
                    candles_htf = feed.fetch_ohlcv(
                        timeframe=bot_cfg.htf_timeframe,
                        limit=bot_cfg.htf_slow + 10,
                    )

                # 2) Supervisor-Anpassungen einlesen (falls Supervisor läuft)
                _sv = db.get_all_state()
                _paused = _sv.get("paused", "false").lower() == "true"

                # 2a) Laufzeit-Overrides aus Telegram/API (pending_* Schlüssel, einmalig)
                for _ok, _obj, _attr, _cast in [
                    ("pending_breakeven_enabled",   risk_cfg, "breakeven_enabled",     lambda v: v.lower() == "true"),
                    ("pending_breakeven_pct",       risk_cfg, "breakeven_trigger_pct", float),
                    ("pending_trailing_sl",         risk_cfg, "use_trailing_sl",       lambda v: v.lower() == "true"),
                    ("pending_trailing_sl_pct",     risk_cfg, "trailing_sl_pct",       float),
                    ("pending_sl_pct",              risk_cfg, "stop_loss_pct",         float),
                    ("pending_tp_pct",              risk_cfg, "take_profit_pct",       float),
                    ("pending_safety_buffer",       risk_cfg, "safety_buffer_pct",     float),
                    ("pending_rsi_buy_max",         risk_cfg, "rsi_buy_max",           float),
                    ("pending_rsi_sell_min",        risk_cfg, "rsi_sell_min",          float),
                    ("pending_volume_filter",       risk_cfg, "volume_filter",         lambda v: v.lower() == "true"),
                    ("pending_volume_factor",       risk_cfg, "volume_factor",         float),
                    ("pending_partial_tp",          risk_cfg, "partial_tp_enabled",    lambda v: v.lower() == "true"),
                    ("pending_partial_tp_fraction", risk_cfg, "partial_tp_fraction",   float),
                    ("pending_fast_period",              bot_cfg,  "fast_period",            int),
                    ("pending_slow_period",              bot_cfg,  "slow_period",            int),
                    ("pending_sentiment_buy_enabled",    risk_cfg, "sentiment_buy_enabled",    lambda v: v.lower() == "true"),
                    ("pending_sentiment_buy_min",        risk_cfg, "sentiment_buy_min",        float),
                    ("pending_sentiment_sell_enabled",   risk_cfg, "sentiment_sell_enabled",   lambda v: v.lower() == "true"),
                    ("pending_sentiment_sell_max",       risk_cfg, "sentiment_sell_max",       float),
                    ("pending_sentiment_sell_mode",      risk_cfg, "sentiment_sell_mode",      str),
                    ("pending_sentiment_stop_enabled",   risk_cfg, "sentiment_stop_enabled",   lambda v: v.lower() == "true"),
                    ("pending_sentiment_stop_threshold", risk_cfg, "sentiment_stop_threshold", float),
                ]:
                    _v = _sv.get(_ok, "")
                    if _v:
                        try:
                            setattr(_obj, _attr, _cast(_v))
                            db.del_state(_ok)
                            log.info(f"Laufzeit-Override: {_attr} = {getattr(_obj, _attr)}")
                            # Supervisor darf diesen Parameter nicht mehr überschreiben
                            if _ok == "pending_trailing_sl":
                                _rt_trailing_set = True
                            elif _ok == "pending_volume_filter":
                                _rt_vol_set = True
                            elif _ok in ("pending_rsi_buy_max", "pending_rsi_sell_min"):
                                _rt_rsi_set = True
                            elif _ok in ("pending_fast_period", "pending_slow_period"):
                                _rt_sma_set = True
                        except (ValueError, TypeError) as _e:
                            log.warning(f"Override {_ok} ungültig: {_e}")
                            db.del_state(_ok)

                if _sv.get("supervisor_regime"):
                    try:
                        if not _rt_rsi_set:
                            risk_cfg.rsi_buy_max  = float(_sv.get("supervisor_rsi_buy_max",  risk_cfg.rsi_buy_max))
                            risk_cfg.rsi_sell_min = float(_sv.get("supervisor_rsi_sell_min", risk_cfg.rsi_sell_min))
                        risk_cfg.atr_sl_mult  = float(_sv.get("supervisor_atr_sl_mult",  risk_cfg.atr_sl_mult))
                        risk_cfg.atr_tp_mult  = float(_sv.get("supervisor_atr_tp_mult",  risk_cfg.atr_tp_mult))
                        if _sv.get("supervisor_fast") and not _rt_sma_set:
                            bot_cfg.fast_period = int(_sv.get("supervisor_fast", bot_cfg.fast_period))
                            bot_cfg.slow_period = int(_sv.get("supervisor_slow", bot_cfg.slow_period))
                        # Feature-Flags nur übernehmen wenn NICHT via CLI oder Laufzeit gesetzt
                        if not _cli_trailing_set and not _rt_trailing_set:
                            sv_trail = _sv.get("supervisor_use_trailing_sl", "")
                            if sv_trail.lower() in ("true", "false"):
                                risk_cfg.use_trailing_sl = sv_trail.lower() == "true"
                        if not _cli_vol_set and not _rt_vol_set:
                            sv_vol = _sv.get("supervisor_volume_filter", "")
                            if sv_vol.lower() in ("true", "false"):
                                risk_cfg.volume_filter = sv_vol.lower() == "true"
                        log.debug(
                            f"Regime={_sv['supervisor_regime']} | "
                            f"Strategie={_sv.get('supervisor_strategy_name','?')} "
                            f"f={bot_cfg.fast_period}/s={bot_cfg.slow_period} | "
                            f"RSI<{risk_cfg.rsi_buy_max} RSI>{risk_cfg.rsi_sell_min} | "
                            f"SL×{risk_cfg.atr_sl_mult} TP×{risk_cfg.atr_tp_mult} | "
                            f"trailing={risk_cfg.use_trailing_sl} vol={risk_cfg.volume_filter}"
                        )
                    except (ValueError, TypeError) as e:
                        log.warning(f"Supervisor-State ungültig: {e}")

                # Feature 5: effektiv genutzte Parameter persistieren (Regime-Persistenz / Warmstart)
                db.set_state("effective_fast",         str(bot_cfg.fast_period))
                db.set_state("effective_slow",         str(bot_cfg.slow_period))
                db.set_state("effective_regime",       _sv.get("supervisor_regime", ""))
                db.set_state("effective_rsi_buy_max",  str(risk_cfg.rsi_buy_max))
                db.set_state("effective_rsi_sell_min", str(risk_cfg.rsi_sell_min))
                db.set_state("effective_atr_sl_mult",  str(risk_cfg.atr_sl_mult))
                db.set_state("effective_atr_tp_mult",  str(risk_cfg.atr_tp_mult))

                # 3) Signal berechnen (inkl. RSI- + Volumen-Filter)
                signal, last_price, rsi_val = get_signal(
                    candles,
                    bot_cfg.fast_period,
                    bot_cfg.slow_period,
                    risk_cfg.rsi_period,
                    risk_cfg.rsi_buy_max,
                    risk_cfg.rsi_sell_min,
                    volume_filter=risk_cfg.volume_filter,
                    volume_factor=risk_cfg.volume_factor,
                    sma200_filter=risk_cfg.sma200_filter,
                    slope_filter=risk_cfg.slope_filter,
                    slope_lookback=risk_cfg.slope_lookback,
                    slope_min_pct=risk_cfg.slope_min_pct,
                )
                rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "–"

                # --- Drawdown-Schutz ---
                _total_value = balance['quote'] + balance['base'] * last_price
                _peak_str    = db.get_state("peak_balance", "")
                _peak_bal    = float(_peak_str) if _peak_str else _total_value
                if _total_value > _peak_bal:
                    _peak_bal = _total_value
                    db.set_state("peak_balance", str(_peak_bal))
                _drawdown_pct    = (_peak_bal - _total_value) / _peak_bal if _peak_bal > 0 else 0.0
                _drawdown_reduce = False
                _dd_level        = db.get_state("drawdown_alert_level", "0")
                if _drawdown_pct >= 0.15:
                    if not _paused:
                        db.set_state("paused", "true")
                        _paused = True
                        log.critical(
                            f"DRAWDOWN-STOPP: {_drawdown_pct * 100:.1f}% >= 15% – "
                            f"Handel automatisch pausiert"
                        )
                    if _dd_level != "15":
                        notify.send_drawdown_alert(bot_cfg.symbol, _drawdown_pct, True)
                        db.set_state("drawdown_alert_level", "15")
                elif _drawdown_pct >= 0.10:
                    _drawdown_reduce = True
                    log.warning(
                        f"DRAWDOWN-WARNUNG: {_drawdown_pct * 100:.1f}% >= 10% – "
                        f"Position-Sizing auf 50% reduziert"
                    )
                    if _dd_level not in ("10", "15"):
                        notify.send_drawdown_alert(bot_cfg.symbol, _drawdown_pct, False)
                        db.set_state("drawdown_alert_level", "10")
                else:
                    if _dd_level != "0":
                        db.set_state("drawdown_alert_level", "0")  # Reset nach Erholung
                # --- Ende Drawdown-Schutz ---

                # Feature 3: HTF-Trend-Filter (BUY nur wenn höherer Timeframe bullish)
                if signal == "BUY" and candles_htf is not None:
                    if not is_htf_bullish(candles_htf, bot_cfg.htf_fast, bot_cfg.htf_slow):
                        log.info(f"HTF-Filter ({bot_cfg.htf_timeframe}): BUY → HOLD")
                        signal = "HOLD"

                # BEAR-Regime: BUY unterdrücken (Trend läuft gegen uns)
                if signal == "BUY" and _sv.get("supervisor_regime") == "BEAR":
                    log.info("BEAR-Regime: BUY → HOLD (Abwärtstrend erkannt, kein neuer Kauf)")
                    signal = "HOLD"

                # SIDEWAYS mit niedrigem ADX: BUY unterdrücken (kein klarer Trend)
                if signal == "BUY" and _sv.get("supervisor_regime") == "SIDEWAYS":
                    _sv_adx = float(_sv.get("supervisor_adx", "25") or "25")
                    if _sv_adx < 25.0:
                        log.info(
                            f"SIDEWAYS-Filter: BUY → HOLD (ADX={_sv_adx:.1f} < 25 – "
                            f"kein klarer Trend, Einstieg blockiert)"
                        )
                        signal = "HOLD"

                # Auto-Unpause prüfen (24h-Pause nach konsekutiven SL-Hits)
                _pause_until = _sv.get("pause_until", "")
                if _paused and _pause_until:
                    try:
                        if datetime.now(timezone.utc) > datetime.fromisoformat(_pause_until):
                            db.set_state("paused", "false")
                            db.set_state("pause_until", "")
                            db.set_state("consecutive_sl", "0")
                            _paused = False
                            log.info("Auto-Unpause: 24h SL-Schutzpause abgelaufen – Handel fortgesetzt")
                            notify.send_telegram(f"▶ {bot_cfg.symbol}: Auto-Unpause – 24h SL-Pause abgelaufen")
                    except Exception as _ue:
                        log.warning(f"Auto-Unpause Fehler: {_ue}")

                # --- Sentiment-Filter ---
                _s_score_raw = _sv.get("current_sentiment_score", "")
                _s_score = float(_s_score_raw) if _s_score_raw else None

                # Auto-Stop: Bot pausieren wenn Score zu negativ
                if (not _paused
                        and _s_score is not None
                        and risk_cfg.sentiment_stop_enabled
                        and _s_score < risk_cfg.sentiment_stop_threshold):
                    db.set_state("paused", "true")
                    _paused = True
                    log.critical(
                        f"Sentiment-Auto-Stop: Score {_s_score:+.3f} < "
                        f"Schwelle {risk_cfg.sentiment_stop_threshold:+.3f} – Handel pausiert"
                    )
                    notify.send_telegram(
                        f"⛔ {bot_cfg.symbol}: Sentiment-Auto-Stop "
                        f"(Score {_s_score:+.3f} < {risk_cfg.sentiment_stop_threshold:+.3f})"
                    )

                # SELL-Trigger: Score unter Schwelle → je nach Modus reagieren
                if (_s_score is not None
                        and risk_cfg.sentiment_sell_enabled
                        and _s_score < risk_cfg.sentiment_sell_max):
                    mode = risk_cfg.sentiment_sell_mode
                    if mode in ("close", "both") and signal != "SELL":
                        log.info(
                            f"Sentiment-SELL: Score {_s_score:+.3f} < "
                            f"{risk_cfg.sentiment_sell_max:+.3f} → SELL (Modus: {mode})"
                        )
                        signal = "SELL"
                    elif mode == "block" and signal == "BUY":
                        log.info(
                            f"Sentiment blockiert BUY: Score {_s_score:+.3f} < "
                            f"{risk_cfg.sentiment_sell_max:+.3f} → HOLD"
                        )
                        signal = "HOLD"

                # BUY-Gate: Score unter Mindestschwelle → kein BUY
                if (signal == "BUY"
                        and _s_score is not None
                        and risk_cfg.sentiment_buy_enabled
                        and _s_score < risk_cfg.sentiment_buy_min):
                    log.info(
                        f"Sentiment-Gate: BUY → HOLD "
                        f"(Score {_s_score:+.3f} < Min {risk_cfg.sentiment_buy_min:+.3f})"
                    )
                    signal = "HOLD"
                # --- Ende Sentiment-Filter ---

                # Force-Signal vom Telegram / Dashboard? (überschreibt berechnetes Signal)
                _force = _sv.get("force_signal", "")
                _is_forced = _force in ("BUY", "SELL")
                if _is_forced:
                    log.info(f"⚡ Force-Signal: {_force} (manuell gesetzt, überschreibt {signal})")
                    signal = _force
                    db.set_state("force_signal", "")  # einmalig verbrauchen

                log.info(
                    f"Signal={signal}{'⚡' if _is_forced else ''} | Preis={last_price:.4f} | RSI={rsi_str} | "
                    f"{balance['quote_currency']}={balance['quote']:.2f} "
                    f"{balance['base_currency']}={balance['base']:.6f}"
                )

                # 3b) Trailing-SL-Update (SL folgt steigendem Kurs nach oben)
                if risk_cfg.use_trailing_sl:
                    for trade in db.get_open_trades(bot_cfg.symbol):
                        new_sl = update_trailing_sl(trade, last_price, risk_cfg.trailing_sl_pct)
                        if new_sl is not None:
                            db.update_trade_sltp(trade["client_id"], new_sl, float(trade["tp_price"]))
                            log.info(f"Trailing-SL: {float(trade['sl_price']):.6f} → {new_sl:.6f}")

                # 3c) Breakeven-SL (SL auf Entry heben wenn Gewinn >= Trigger)
                if risk_cfg.breakeven_enabled:
                    for trade in db.get_open_trades(bot_cfg.symbol):
                        if check_breakeven(trade, last_price, risk_cfg):
                            entry = float(trade["entry_price"])
                            db.update_trade_sltp(trade["client_id"], entry, float(trade["tp_price"]))
                            log.info(f"Breakeven-SL gesetzt: {trade['client_id'][:12]}… → {entry:.6f}")

                # 4) SL/TP prüfen (höchste Priorität)
                open_trades = db.get_open_trades(bot_cfg.symbol)

                # Zeitbasierter Exit: Trade > 24h alt UND Kurs unter Entry → schließen
                _TIME_STOP_H   = 24    # Stunden bis zum Time-Stop
                _TIME_STOP_PNL = 0.0   # Exit wenn Preis <= Entry (kein Gewinn)
                for _ot in open_trades:
                    try:
                        _opened = datetime.fromisoformat(_ot["opened_at"]).replace(tzinfo=timezone.utc)
                        _age_h  = (datetime.now(timezone.utc) - _opened).total_seconds() / 3600
                        _entry  = float(_ot["entry_price"])
                        if _age_h >= _TIME_STOP_H and last_price <= _entry * (1 + _TIME_STOP_PNL):
                            log.warning(
                                f"Time-Stop: Trade {_ot['client_id'][:12]} "
                                f"ist {_age_h:.0f}h alt | Kurs {last_price:.4f} <= Entry {_entry:.4f} → Schliessen"
                            )
                            db.update_trade_sltp(_ot["client_id"], last_price * 0.9999, float(_ot["tp_price"]))
                    except Exception as _te:
                        log.debug(f"Time-Stop Fehler: {_te}")

                triggered   = sl_tp.check(last_price, open_trades)
                for hit in triggered:
                    trade  = hit["trade"]
                    reason = hit["reason"]

                    # Feature 2: Partial TP – bei erstem TP-Hit nur Anteil verkaufen
                    if (reason == "tp_hit"
                            and risk_cfg.partial_tp_enabled
                            and not int(trade.get("is_remainder") or 0)):
                        partial   = float(trade["amount"]) * risk_cfg.partial_tp_fraction
                        remainder = float(trade["amount"]) - partial
                        tp_price  = float(trade["tp_price"])
                        entry     = float(trade["entry_price"])
                        executor.sell(
                            amount=risk.calc_sell_amount(balance),
                            last_price=last_price,
                            trade_client_id=trade["client_id"],
                            reason="tp_partial_closed",
                            override_amount=partial,
                        )
                        if remainder * last_price >= risk_cfg.min_order_quote:
                            new_tp = tp_price + (tp_price - entry)
                            db.open_trade(str(uuid.uuid4()), bot_cfg.symbol, remainder,
                                          tp_price, entry, new_tp, is_remainder=1)
                            log.info(
                                f"Partial TP: {partial:.6f} verkauft, "
                                f"{remainder:.6f} als Remainder-Trade (SL={entry:.6f} TP={new_tp:.6f})"
                            )
                        pnl_eur = (tp_price - entry) * partial
                        notify.send_trade_sell(
                            bot_cfg.symbol, partial, tp_price,
                            "tp_partial_closed", pnl_eur, bot_cfg.dry_run,
                        )
                    else:
                        executor.sell(
                            amount=risk.calc_sell_amount(balance),
                            last_price=last_price,
                            trade_client_id=trade["client_id"],
                            reason=reason,
                        )
                        if reason == "sl_hit":
                            db.set_state("last_sl_at", utcnow())
                            # Konsekutive SL-Zählung: 3 in Folge → 24h Pause
                            _consec_sl = int(db.get_state("consecutive_sl", "0") or "0") + 1
                            db.set_state("consecutive_sl", str(_consec_sl))
                            log.info(f"Konsekutive SL-Hits: {_consec_sl}")
                            if _consec_sl >= 3:
                                _pause_ts = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
                                db.set_state("paused", "true")
                                db.set_state("pause_until", _pause_ts)
                                db.set_state("pause_reason", f"{_consec_sl}× konsekutive SL-Hits")
                                log.warning(
                                    f"SL-SCHUTZ: {_consec_sl} konsekutive SL-Hits → "
                                    f"24h Pause bis {_pause_ts[:16]}"
                                )
                                notify.send_telegram(
                                    f"⛔ {bot_cfg.symbol}: {_consec_sl}× SL in Folge → "
                                    f"24h Pause bis {_pause_ts[11:16]} UTC"
                                )
                        else:
                            # Gewinn oder Breakeven → Zähler zurücksetzen
                            db.set_state("consecutive_sl", "0")
                        exit_price = trade["tp_price"] if reason == "tp_hit" else trade["sl_price"]
                        pnl_eur    = (exit_price - float(trade["entry_price"])) * float(trade["amount"])
                        notify.send_trade_sell(
                            bot_cfg.symbol, float(trade["amount"]), exit_price,
                            reason, pnl_eur, bot_cfg.dry_run,
                        )
                if triggered:
                    balance = feed.fetch_balance()

                # 4b) Pyramid-Nachkauf prüfen (News + Profit → Nachkauf)
                active_trades = db.get_open_trades(bot_cfg.symbol)
                if active_trades and not triggered:
                    trade   = active_trades[0]
                    regime  = _sv.get("supervisor_regime", "")
                    news_db = os.path.join(risk_cfg.db_dir, "news.db")
                    ok_pyr, pyr_reason = pyramid.should_pyramid(
                        trade, last_price, regime, news_db, bot_cfg.symbol
                    )
                    if ok_pyr:
                        pyr_amount = risk.calc_buy_amount(balance, last_price, exchange) * pyramid.PYRAMID_SIZE_FRACTION
                        log.info(f"🔺 Pyramid-Bedingung erfüllt ({pyr_reason}) – kaufe {pyr_amount:.6f}")
                        pyr_order = executor.pyramid_buy(pyr_amount, last_price)
                        if pyr_order:
                            actual_price = pyr_order.get("average") or pyr_order.get("price") or last_price
                            old_amount   = float(trade["amount"])
                            new_amount   = old_amount + pyr_amount
                            new_entry    = (trade["entry_price"] * old_amount + actual_price * pyr_amount) / new_amount
                            new_sl, new_tp = calc_levels(new_entry, risk_cfg, candles)
                            db.update_trade_pyramid(trade["client_id"], new_amount, new_entry, new_sl, new_tp)
                            log.info(
                                f"🔺 Pyramid-Kauf: +{pyr_amount:.6f} @ {actual_price:.4f} | "
                                f"Avg-Entry={new_entry:.4f} SL={new_sl:.4f} TP={new_tp:.4f}"
                            )
                            notify.send_pyramid_buy(bot_cfg.symbol, pyr_amount, actual_price, new_entry, bot_cfg.dry_run)
                            balance = feed.fetch_balance()
                    else:
                        log.debug(f"Pyramid nicht ausgelöst: {pyr_reason}")

                # 5) Guardrails
                ok, reason = risk.check_guardrails(open_orders, balance)
                active_trades = db.get_open_trades(bot_cfg.symbol)
                if active_trades:
                    # Force-Sell darf trotz offenem Trade ausgeführt werden (schließt Position)
                    if not (_is_forced and signal == "SELL"):
                        ok     = False
                        reason = f"Offener Trade ({active_trades[0]['client_id'][:12]}…)"

                if not ok:
                    log.debug(f"Kein Trade: {reason}")
                elif _paused:
                    log.info("⏸ Handel pausiert – kein Trade ausgeführt")
                else:
                    # 6) Order ausführen
                    if signal == "BUY" and risk_cfg.sl_cooldown_candles > 0:
                        last_sl_at = db.get_state("last_sl_at", "")
                        if last_sl_at:
                            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sl_at)).total_seconds()
                            cooldown_sec = risk_cfg.sl_cooldown_candles * _timeframe_to_seconds(bot_cfg.timeframe)
                            if elapsed < cooldown_sec:
                                signal = "HOLD"
                                log.info(f"SL-Cooldown aktiv: {int(cooldown_sec - elapsed)}s verbleibend")

                    if signal == "BUY":
                        _buy_amount = risk.calc_buy_amount(balance, last_price, exchange)
                        if _drawdown_reduce:
                            _buy_amount *= 0.5
                            log.warning(
                                f"Position-Sizing: 50% wegen Drawdown "
                                f"{_drawdown_pct * 100:.1f}%"
                            )

                        # ATR-basiertes Position-Sizing: volatile Coins kleiner handeln
                        _atr_pct_sv = float(_sv.get("supervisor_atr_pct", "0") or "0")
                        if _atr_pct_sv > 0:
                            # Referenz-ATR: 1.5% → 1.0x, 3% → 0.5x, 0.5% → 1.0x (cap bei 1.0)
                            _atr_size_mult = min(1.0, 1.5 / _atr_pct_sv)
                            _atr_size_mult = max(0.3, _atr_size_mult)
                            if _atr_size_mult < 0.95:
                                _buy_amount *= _atr_size_mult
                                log.info(
                                    f"ATR-Sizing: {_atr_pct_sv:.2f}% ATR → "
                                    f"{_atr_size_mult:.2f}× Position ({_buy_amount * last_price:.2f}€)"
                                )

                        # Konsekutive SL-Hits: Position bei 2 Hits halbieren
                        _consec_now = int(db.get_state("consecutive_sl", "0") or "0")
                        if _consec_now >= 2:
                            _buy_amount *= 0.5
                            log.warning(
                                f"SL-Schutz-Sizing: {_consec_now}× konsekutive SL-Hits → "
                                f"50% Position-Größe ({_buy_amount * last_price:.2f}€)"
                            )

                        executor.buy(_buy_amount, last_price, candles)
                    elif signal == "SELL":
                        executor.sell(risk.calc_sell_amount(balance), last_price)
                        notify.send_trade_sell(
                            bot_cfg.symbol, balance["base"], last_price,
                            "signal_close", dry_run=bot_cfg.dry_run,
                        )

                # 7) Zustand für Web-Interface persistieren
                db.set_state("symbol",         bot_cfg.symbol)
                db.set_state("timeframe",      bot_cfg.timeframe)
                db.set_state("dry_run",        str(bot_cfg.dry_run))
                db.set_state("last_signal",    signal)
                db.set_state("last_price",     f"{last_price:.6f}")
                db.set_state("last_update",    utcnow())
                db.set_state("balance_quote",  f"{balance['quote']:.2f}")
                db.set_state("balance_base",   f"{balance['base']:.6f}")
                db.set_state("quote_currency", balance['quote_currency'])
                db.set_state("base_currency",  balance['base_currency'])
                db.set_state("sl_pct",         str(risk_cfg.stop_loss_pct))
                db.set_state("tp_pct",         str(risk_cfg.take_profit_pct))
                db.set_state("fast_period",    str(bot_cfg.fast_period))
                db.set_state("slow_period",    str(bot_cfg.slow_period))
                db.set_state("rsi",            rsi_str)
                db.set_state("regime",         _sv.get("supervisor_regime", "–"))
                db.set_state("strategy_name",  _sv.get("supervisor_strategy_name", "Standard"))
                db.set_state("sim_pnl",        _sv.get("supervisor_sim_pnl", "–"))
                db.set_state("use_trailing_sl",        str(risk_cfg.use_trailing_sl))
                db.set_state("trailing_sl_pct",        str(risk_cfg.trailing_sl_pct))
                db.set_state("sl_cooldown_candles",    str(risk_cfg.sl_cooldown_candles))
                db.set_state("volume_filter",          str(risk_cfg.volume_filter))
                db.set_state("volume_factor",          str(risk_cfg.volume_factor))
                db.set_state("breakeven_enabled",      str(risk_cfg.breakeven_enabled))
                db.set_state("breakeven_trigger_pct",  str(risk_cfg.breakeven_trigger_pct))
                db.set_state("partial_tp_enabled",     str(risk_cfg.partial_tp_enabled))
                db.set_state("partial_tp_fraction",    str(risk_cfg.partial_tp_fraction))
                db.set_state("safety_buffer_pct",      str(risk_cfg.safety_buffer_pct))
                db.set_state("htf_timeframe",          bot_cfg.htf_timeframe)
                db.set_state("htf_fast",               str(bot_cfg.htf_fast))
                db.set_state("htf_slow",               str(bot_cfg.htf_slow))
                db.set_state("sma200_filter",          str(risk_cfg.sma200_filter))
                db.set_state("slope_filter",           str(risk_cfg.slope_filter))
                db.set_state("sentiment_buy_enabled",    str(risk_cfg.sentiment_buy_enabled))
                db.set_state("sentiment_buy_min",        str(risk_cfg.sentiment_buy_min))
                db.set_state("sentiment_sell_enabled",   str(risk_cfg.sentiment_sell_enabled))
                db.set_state("sentiment_sell_max",       str(risk_cfg.sentiment_sell_max))
                db.set_state("sentiment_sell_mode",      risk_cfg.sentiment_sell_mode)
                db.set_state("sentiment_stop_enabled",   str(risk_cfg.sentiment_stop_enabled))
                db.set_state("sentiment_stop_threshold", str(risk_cfg.sentiment_stop_threshold))
                db.set_state("status",                 "paused" if _paused else "running")
                db.set_state("consecutive_sl",         db.get_state("consecutive_sl", "0"))
                db.set_state("pause_until",            db.get_state("pause_until", ""))

                # Feature 4: Täglicher Cleanup (orders + errors älter als cleanup_days)
                _last_cleanup = db.get_state("last_cleanup", "")
                if _last_cleanup:
                    _elapsed = (datetime.now(timezone.utc)
                                - datetime.fromisoformat(_last_cleanup)).total_seconds()
                    if _elapsed > 86400:
                        db.cleanup_old_records(days=risk_cfg.cleanup_days)
                        db.set_state("last_cleanup", datetime.now(timezone.utc).isoformat())

                breaker.success()

            except ccxt.DDoSProtection as e:
                wait = 60 + random.uniform(0, 60)
                log.warning(f"[RATELIMIT] {e} – warte {wait:.0f}s")
                db.set_state("status", "error: rate limit")
                time.sleep(wait)
                continue  # Circuit Breaker nicht inkrementieren – kein Bot-Fehler

            except ccxt.NetworkError as e:
                log.warning(f"[NET] {e}")
                db.set_state("status", f"error: network")
                breaker.failure(e)
                time.sleep(5)
                continue

            except ccxt.ExchangeError as e:
                log.error(f"[EXCHANGE] {e}")
                db.log_error("ExchangeError", str(e))
                db.set_state("status", f"error: exchange")
                breaker.failure(e)
                time.sleep(15)
                continue

            except RuntimeError as e:
                log.critical(str(e))
                db.log_error("CircuitBreaker", str(e))
                db.set_state("status", "stopped: circuit breaker")
                break

            except Exception as e:
                log.exception(f"[FATAL] {e}")
                db.log_error("Fatal", str(e))
                db.set_state("status", f"error: {str(e)[:60]}")
                breaker.failure(e)
                time.sleep(30)
                continue

            time.sleep(bot_cfg.poll_seconds)

    except KeyboardInterrupt:
        log.info("Bot gestoppt (SIGINT).")

    finally:
        db.set_state("status", "stopped")
        log.info("Bot beendet.")
        db.close()


if __name__ == "__main__":
    main()
