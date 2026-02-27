"""
Supervisor Bot – Marktregime-Erkennung und dynamische Strategie-Anpassung.

Läuft als separater Prozess und analysiert alle 5 Minuten (konfigurierbar)
das Marktregime jedes aktiven Coins via ADX + relative ATR.
Simuliert zusätzlich 6 SMA-Varianten auf gecachten Candles und wählt die
profitabelste aus (Stufe 5: Multi-Strategie-Optimierer).
Die erkannten Parameter werden in die jeweilige Bot-DB geschrieben;
die Bots übernehmen sie beim nächsten Loop-Durchlauf ohne Neustart.

Verwendung:
  python supervisor.py                   # Live, alle 300s
  python supervisor.py --interval 60    # Alle 60s
  python supervisor.py --dry-run        # Nur loggen, nichts schreiben
"""
import argparse
import logging
import os
import sys
import time
from glob import glob

import ccxt

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from bot.config import ExchangeConfig, OpsConfig
from bot.data_feed import build_exchange
from bot.ops import setup_logging
from bot.persistence import StateDB, utcnow
from bot.regime import classify_regime, REGIME_TEMPLATES
from bot import candles_db as cdb
from bot import optimizer


def parse_args():
    p = argparse.ArgumentParser(description="Supervisor Bot – Regime-Erkennung + Strategie-Optimierer")
    p.add_argument("--interval",  type=int,   default=300,
                   help="Sekunden zwischen Durchläufen (default: 300)")
    p.add_argument("--db-dir",    default=os.path.join(PROJECT_ROOT, "db"),
                   help="Verzeichnis mit Bot-DBs")
    p.add_argument("--log-dir",   default=os.path.join(PROJECT_ROOT, "logs", "supervisor"),
                   help="Log-Verzeichnis")
    p.add_argument("--timeframe", default="5m",
                   help="Candle-Timeframe für Regime-Analyse (default: 5m)")
    p.add_argument("--candles",   type=int,   default=100,
                   help="Anzahl Candles für ADX/ATR-Berechnung (default: 100, min: 30)")
    p.add_argument("--dry-run",   action="store_true",
                   help="Nicht in DB schreiben, nur loggen")
    return p.parse_args()


def _analyze(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int) -> tuple | None:
    """
    Fetcht OHLCV und klassifiziert das Regime für ein Symbol.
    Gibt (regime, adx_val, atr_pct, candles) oder None bei Fehler zurück.
    """
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if len(candles) < 30:
            logging.getLogger("supervisor").warning(
                f"{symbol}: Zu wenig Candles ({len(candles)}) – übersprungen"
            )
            return None
        regime, adx_val, atr_pct = classify_regime(candles)
        return regime, adx_val, atr_pct, candles
    except ccxt.DDoSProtection:
        logging.getLogger("supervisor").warning(f"{symbol}: Rate-Limit – übersprungen")
        return None
    except ccxt.BadSymbol:
        logging.getLogger("supervisor").warning(f"{symbol}: Nicht auf Exchange – übersprungen")
        return None
    except Exception as e:
        logging.getLogger("supervisor").error(f"{symbol}: Analysefehler – {e}")
        return None


def _write(
    db_path: str,
    regime: str,
    adx_val: float,
    atr_pct: float,
    best: dict,
    dry_run: bool,
):
    """Schreibt Supervisor-Ergebnisse (Regime + Optimizer) in die Bot-DB."""
    log = logging.getLogger("supervisor")
    tmpl = REGIME_TEMPLATES[regime]

    if dry_run:
        log.info(
            f"[DRY] {os.path.basename(db_path)}: regime={regime} | "
            f"strategie={best['name']} (f={best['fast']}/s={best['slow']}) | "
            f"sim_pnl={best['pnl_pct']:+.2f}% | trades={best['num_trades']}"
        )
        return

    try:
        db = StateDB(db_path)
        db.set_state("supervisor_regime",          regime)
        db.set_state("supervisor_adx",             f"{adx_val:.1f}" if adx_val >= 0 else "–")
        db.set_state("supervisor_atr_pct",         f"{atr_pct:.2f}")
        db.set_state("supervisor_rsi_buy_max",     str(tmpl["rsi_buy_max"]))
        db.set_state("supervisor_rsi_sell_min",    str(tmpl["rsi_sell_min"]))
        db.set_state("supervisor_atr_sl_mult",     str(tmpl["atr_sl_mult"]))
        db.set_state("supervisor_atr_tp_mult",     str(tmpl["atr_tp_mult"]))
        db.set_state("supervisor_strategy_name",   best["name"])
        db.set_state("supervisor_fast",            str(best["fast"]))
        db.set_state("supervisor_slow",            str(best["slow"]))
        db.set_state("supervisor_sim_pnl",         f"{best['pnl_pct']:+.2f}")
        db.set_state("supervisor_sim_trades",      str(best["num_trades"]))
        db.set_state("supervisor_last_update",     utcnow())
        db.close()
    except Exception as e:
        log.error(f"DB-Schreibfehler ({db_path}): {e}")


def run_once(exchange: ccxt.Exchange, db_dir: str, timeframe: str, limit: int, dry_run: bool):
    """Einen Supervisor-Durchlauf über alle Bot-DBs."""
    log = logging.getLogger("supervisor")
    db_paths = sorted(glob(os.path.join(db_dir, "*.db")))

    # candles.db aus der Liste heraushalten
    db_paths = [p for p in db_paths if os.path.basename(p) != "candles.db"]

    if not db_paths:
        log.info("Keine Bot-DBs gefunden.")
        return

    log.info(f"Supervisor-Durchlauf: {len(db_paths)} Bot(s)")

    # Candle-Cache einmal öffnen (geteilt über alle Symbole)
    candles_db_path = os.path.join(db_dir, "candles.db")
    conn_c = cdb.open_db(candles_db_path)

    try:
        for db_path in db_paths:
            # Symbol aus DB lesen
            try:
                db     = StateDB(db_path)
                symbol = db.get_state("symbol")
                prev   = db.get_state("supervisor_regime")
                db.close()
            except Exception as e:
                log.error(f"DB lesen fehlgeschlagen ({db_path}): {e}")
                continue

            if not symbol:
                log.debug(f"{db_path}: kein Symbol in bot_state, übersprungen")
                continue

            result = _analyze(exchange, symbol, timeframe, limit)
            if result is None:
                continue

            regime, adx_val, atr_pct, fresh_candles = result

            if regime != prev:
                log.info(f"REGIME-WECHSEL {symbol}: {prev or '?'} → {regime} "
                         f"(ADX={adx_val:.1f} ATR%={atr_pct:.2f}%)")
            else:
                log.debug(f"{symbol}: {regime} bleibt | ADX={adx_val:.1f} ATR%={atr_pct:.2f}%")

            # Candles cachen (auch im dry-run – reine Datenspeicherung)
            inserted = cdb.upsert_candles(conn_c, symbol, timeframe, fresh_candles)
            log.debug(f"{symbol}: {inserted} neue Candles gecacht")

            # Historische Candles für Optimierung laden (wächst über Zeit auf 500)
            history = cdb.load_candles(conn_c, symbol, timeframe, limit=500)
            log.debug(f"{symbol}: {len(history)} Candles für Optimierung verfügbar")

            # Strategie-Optimierung mit aktuellen Regime-Params
            tmpl = REGIME_TEMPLATES[regime]
            best = optimizer.best_variant(
                history,
                rsi_buy_max=tmpl["rsi_buy_max"],
                rsi_sell_min=tmpl["rsi_sell_min"],
                atr_sl_mult=tmpl["atr_sl_mult"],
                atr_tp_mult=tmpl["atr_tp_mult"],
            )

            _write(db_path, regime, adx_val, atr_pct, best, dry_run)

            time.sleep(1.5)   # Kraken Rate-Limit schonen
    finally:
        conn_c.close()


def main():
    args = parse_args()

    ops_cfg = OpsConfig(log_dir=args.log_dir)
    setup_logging(ops_cfg)
    log = logging.getLogger("supervisor")

    log.info(
        f"Supervisor startet | interval={args.interval}s "
        f"| tf={args.timeframe} | candles={args.candles} | dry_run={args.dry_run}"
    )

    exchange = build_exchange(ExchangeConfig())

    try:
        while True:
            try:
                run_once(exchange, args.db_dir, args.timeframe, args.candles, args.dry_run)
            except ccxt.DDoSProtection as e:
                log.warning(f"Rate-Limit: {e} – 120s warten")
                time.sleep(120)
                continue
            except ccxt.NetworkError as e:
                log.warning(f"Netzwerkfehler: {e} – 30s warten")
                time.sleep(30)
                continue
            except Exception as e:
                log.exception(f"Unerwarteter Fehler: {e}")
                time.sleep(30)
                continue

            log.info(f"Durchlauf abgeschlossen – warte {args.interval}s")
            time.sleep(args.interval)

    except KeyboardInterrupt:
        log.info("Supervisor gestoppt (SIGINT).")


if __name__ == "__main__":
    main()
