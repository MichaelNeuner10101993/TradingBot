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
from datetime import datetime, timezone
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
    return p.parse_args()


def main():
    # SIGTERM (von systemctl stop) → KeyboardInterrupt → finally-Block setzt status="stopped"
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    _signal.signal(_signal.SIGTERM, _on_sigterm)

    args = parse_args()

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
    if args.trailing_sl:                    risk_cfg.use_trailing_sl    = True
    if args.trailing_sl_pct is not None:    risk_cfg.trailing_sl_pct    = args.trailing_sl_pct
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
                    ("pending_fast_period",         bot_cfg,  "fast_period",           int),
                    ("pending_slow_period",         bot_cfg,  "slow_period",           int),
                ]:
                    _v = _sv.get(_ok, "")
                    if _v:
                        try:
                            setattr(_obj, _attr, _cast(_v))
                            db.del_state(_ok)
                            log.info(f"Laufzeit-Override: {_attr} = {getattr(_obj, _attr)}")
                        except (ValueError, TypeError) as _e:
                            log.warning(f"Override {_ok} ungültig: {_e}")
                            db.del_state(_ok)

                if _sv.get("supervisor_regime"):
                    try:
                        risk_cfg.rsi_buy_max  = float(_sv.get("supervisor_rsi_buy_max",  risk_cfg.rsi_buy_max))
                        risk_cfg.rsi_sell_min = float(_sv.get("supervisor_rsi_sell_min", risk_cfg.rsi_sell_min))
                        risk_cfg.atr_sl_mult  = float(_sv.get("supervisor_atr_sl_mult",  risk_cfg.atr_sl_mult))
                        risk_cfg.atr_tp_mult  = float(_sv.get("supervisor_atr_tp_mult",  risk_cfg.atr_tp_mult))
                        if _sv.get("supervisor_fast"):
                            bot_cfg.fast_period = int(_sv.get("supervisor_fast", bot_cfg.fast_period))
                            bot_cfg.slow_period = int(_sv.get("supervisor_slow", bot_cfg.slow_period))
                        # Feature-Flags nur übernehmen wenn NICHT via CLI gesetzt
                        if not _cli_trailing_set:
                            sv_trail = _sv.get("supervisor_use_trailing_sl", "")
                            if sv_trail.lower() in ("true", "false"):
                                risk_cfg.use_trailing_sl = sv_trail.lower() == "true"
                        if not _cli_vol_set:
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
                )
                rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "–"

                # Feature 3: HTF-Trend-Filter (BUY nur wenn höherer Timeframe bullish)
                if signal == "BUY" and candles_htf is not None:
                    if not is_htf_bullish(candles_htf, bot_cfg.htf_fast, bot_cfg.htf_slow):
                        log.info(f"HTF-Filter ({bot_cfg.htf_timeframe}): BUY → HOLD")
                        signal = "HOLD"

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
                        executor.buy(risk.calc_buy_amount(balance, last_price, exchange), last_price, candles)
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
                db.set_state("status",                 "paused" if _paused else "running")

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
