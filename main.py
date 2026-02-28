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
import ccxt

import os
from datetime import datetime, timezone
from bot.config import ExchangeConfig, BotConfig, RiskConfig, OpsConfig
from bot.ops import setup_logging, CircuitBreaker
from bot.data_feed import DataFeed, build_exchange
from bot.strategy import get_signal
from bot.risk import RiskManager
from bot.execution import Executor
from bot.persistence import StateDB, utcnow
from bot.sl_tp import SlTpMonitor


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
    p.add_argument("--startup-delay", type=int, default=0, help="Sekunden warten vor erster API-Anfrage (API-Rate-Limit bei mehreren Bots staffeln)")
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

                # 2) Supervisor-Anpassungen einlesen (falls Supervisor läuft)
                _sv = db.get_all_state()
                if _sv.get("supervisor_regime"):
                    try:
                        risk_cfg.rsi_buy_max  = float(_sv.get("supervisor_rsi_buy_max",  risk_cfg.rsi_buy_max))
                        risk_cfg.rsi_sell_min = float(_sv.get("supervisor_rsi_sell_min", risk_cfg.rsi_sell_min))
                        risk_cfg.atr_sl_mult  = float(_sv.get("supervisor_atr_sl_mult",  risk_cfg.atr_sl_mult))
                        risk_cfg.atr_tp_mult  = float(_sv.get("supervisor_atr_tp_mult",  risk_cfg.atr_tp_mult))
                        if _sv.get("supervisor_fast"):
                            bot_cfg.fast_period = int(_sv.get("supervisor_fast", bot_cfg.fast_period))
                            bot_cfg.slow_period = int(_sv.get("supervisor_slow", bot_cfg.slow_period))
                        log.debug(
                            f"Regime={_sv['supervisor_regime']} | "
                            f"Strategie={_sv.get('supervisor_strategy_name','?')} "
                            f"f={bot_cfg.fast_period}/s={bot_cfg.slow_period} | "
                            f"RSI<{risk_cfg.rsi_buy_max} RSI>{risk_cfg.rsi_sell_min} | "
                            f"SL×{risk_cfg.atr_sl_mult} TP×{risk_cfg.atr_tp_mult}"
                        )
                    except (ValueError, TypeError) as e:
                        log.warning(f"Supervisor-State ungültig: {e}")

                # 3) Signal berechnen (inkl. RSI-Filter)
                signal, last_price, rsi_val = get_signal(
                    candles,
                    bot_cfg.fast_period,
                    bot_cfg.slow_period,
                    risk_cfg.rsi_period,
                    risk_cfg.rsi_buy_max,
                    risk_cfg.rsi_sell_min,
                )
                rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else "–"

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

                # 4) SL/TP prüfen (höchste Priorität)
                open_trades = db.get_open_trades(bot_cfg.symbol)
                triggered   = sl_tp.check(last_price, open_trades)
                for hit in triggered:
                    trade  = hit["trade"]
                    reason = hit["reason"]
                    executor.sell(
                        amount=risk.calc_sell_amount(balance),
                        last_price=last_price,
                        trade_client_id=trade["client_id"],
                        reason=reason,
                    )
                if triggered:
                    balance = feed.fetch_balance()

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
                else:
                    # 6) Order ausführen
                    if signal == "BUY":
                        executor.buy(risk.calc_buy_amount(balance, last_price, exchange), last_price, candles)
                    elif signal == "SELL":
                        executor.sell(risk.calc_sell_amount(balance), last_price)

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
                db.set_state("status",         "running")

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
