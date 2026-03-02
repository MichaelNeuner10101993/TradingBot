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
from bot import optimizer, notify
from bot.optimizer import RSI_ATR_COMBOS

FEATURE_COMBOS = [
    {"use_trailing_sl": False, "volume_filter": False},
    {"use_trailing_sl": True,  "volume_filter": False},
    {"use_trailing_sl": False, "volume_filter": True},
    {"use_trailing_sl": True,  "volume_filter": True},
]


def _timeframe_ms(tf: str) -> int:
    _map = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(tf[:-1]) * _map.get(tf[-1].lower(), 60_000)


def _collect_symbols(db_dir: str) -> list[str]:
    """Liest Symbol aus allen Bot-DBs (außer candles.db / news.db)."""
    symbols = []
    for db_path in sorted(glob(os.path.join(db_dir, "*.db"))):
        if os.path.basename(db_path) in ("candles.db", "news.db"):
            continue
        try:
            db  = StateDB(db_path)
            sym = db.get_state("symbol")
            db.close()
            if sym:
                symbols.append(sym)
        except Exception:
            pass
    return symbols


def _backfill_candles(
    exchange: ccxt.Exchange,
    conn_c,
    symbol: str,
    timeframe: str,
    target: int = 2000,
    batch: int = 720,
):
    """Füllt candles.db auf `target` Einträge auf, falls zu wenig vorhanden."""
    current = cdb.count_candles(conn_c, symbol, timeframe)
    if current >= target:
        return
    log = logging.getLogger("supervisor")
    log.info(f"Backfill {symbol}: {current}/{target} Candles – hole historische Daten…")
    row = conn_c.execute(
        "SELECT MIN(ts) FROM candles WHERE symbol=? AND timeframe=?",
        (symbol, timeframe),
    ).fetchone()
    tf_ms = _timeframe_ms(timeframe)
    since = (row[0] - batch * tf_ms) if row and row[0] else None
    fetched = 0
    try:
        for _ in range(5):  # max 5 Batches = 3600 Candles
            if current + fetched >= target:
                break
            batch_c = exchange.fetch_ohlcv(symbol, timeframe, limit=batch, since=since)
            if not batch_c:
                break
            inserted = cdb.upsert_candles(conn_c, symbol, timeframe, batch_c)
            fetched += inserted
            since = batch_c[0][0] - batch * tf_ms
            time.sleep(1.5)
    except Exception as e:
        log.warning(f"Backfill {symbol}: {e}")
    log.info(f"Backfill {symbol}: {fetched} neue Candles geladen (gesamt ca. {current + fetched})")


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
        val_str = f" val={best['val_pnl']:+.2f}%" if best.get("val_pnl") is not None else ""
        log.info(
            f"[DRY] {os.path.basename(db_path)}: regime={regime} | "
            f"strategie={best['name']} (f={best['fast']}/s={best['slow']}) | "
            f"trailing={best.get('use_trailing_sl',False)} vol={best.get('volume_filter',False)} | "
            f"sim_pnl={best['pnl_pct']:+.2f}%{val_str} SQN={best.get('sqn',0):.2f} | "
            f"trades={best['num_trades']}"
        )
        return

    try:
        db = StateDB(db_path)
        db.set_state("supervisor_regime",          regime)
        db.set_state("supervisor_adx",             f"{adx_val:.1f}" if adx_val >= 0 else "–")
        db.set_state("supervisor_atr_pct",         f"{atr_pct:.2f}")
        db.set_state("supervisor_rsi_buy_max",     str(best.get("rsi_buy_max",  tmpl["rsi_buy_max"])))
        db.set_state("supervisor_rsi_sell_min",    str(best.get("rsi_sell_min", tmpl["rsi_sell_min"])))
        db.set_state("supervisor_atr_sl_mult",     str(best.get("atr_sl_mult",  tmpl["atr_sl_mult"])))
        db.set_state("supervisor_atr_tp_mult",     str(best.get("atr_tp_mult",  tmpl["atr_tp_mult"])))
        db.set_state("supervisor_strategy_name",   best["name"])
        db.set_state("supervisor_fast",            str(best["fast"]))
        db.set_state("supervisor_slow",            str(best["slow"]))
        db.set_state("supervisor_sim_pnl",         f"{best['pnl_pct']:+.2f}")
        db.set_state("supervisor_sim_trades",      str(best["num_trades"]))
        db.set_state("supervisor_last_update",     utcnow())
        if best.get("val_pnl") is not None:
            db.set_state("supervisor_val_pnl",     f"{best['val_pnl']:+.2f}")
        db.set_state("supervisor_use_trailing_sl", str(best.get("use_trailing_sl", False)))
        db.set_state("supervisor_volume_filter",   str(best.get("volume_filter", False)))
        db.log_supervisor_cycle(
            regime=regime,
            adx=adx_val,
            atr_pct=atr_pct,
            strategy_name=best["name"],
            fast=best["fast"],
            slow=best["slow"],
            sim_pnl=best["pnl_pct"],
            num_trades=best["num_trades"],
            source="own",
            use_trailing_sl=best.get("use_trailing_sl", False),
            volume_filter=best.get("volume_filter", False),
        )

        # Telegram-Empfehlung wenn Supervisor-Empfehlung ≠ aktuelle Bot-Einstellung
        cur_trailing = db.get_state("use_trailing_sl", "False").lower() == "true"
        cur_vol      = db.get_state("volume_filter",   "False").lower() == "true"
        if best.get("use_trailing_sl", False) != cur_trailing or best.get("volume_filter", False) != cur_vol:
            symbol = db.get_state("symbol", os.path.basename(db_path))
            notify.send_supervisor_recommendation(symbol, best, cur_trailing, cur_vol)

        db.close()
    except Exception as e:
        log.error(f"DB-Schreibfehler ({db_path}): {e}")


def _cross_bot_learning(results: dict, dry_run: bool):
    """
    Cross-Bot Learning: Teilt die beste Strategie eines Coins mit anderen Coins
    im gleichen Regime, wenn die geteilte Strategie auf den Ziel-Candles besser abschneidet.

    results: {symbol -> {"regime", "best", "candles", "db_path"}}
    """
    from collections import defaultdict
    log = logging.getLogger("supervisor")

    by_regime: dict[str, list] = defaultdict(list)
    for symbol, data in results.items():
        by_regime[data["regime"]].append((symbol, data))

    for regime, bots in by_regime.items():
        if len(bots) < 2:
            continue

        # Winner = Coin mit höchstem sim_pnl (nur wenn positiv)
        winner_sym, winner_data = max(bots, key=lambda x: x[1]["best"]["pnl_pct"])
        if winner_data["best"]["pnl_pct"] <= 0:
            log.debug(f"Cross-Bot [{regime}]: kein positiver Winner – übersprungen")
            continue

        tmpl = REGIME_TEMPLATES[regime]

        for sym, data in bots:
            if sym == winner_sym:
                continue

            # Winner-Strategie auf Ziel-Candles testen (mit Winner-Feature-Flags + Winner-RSI/ATR)
            w = winner_data["best"]
            shared = optimizer.simulate(
                data["candles"],
                w["fast"],
                w["slow"],
                rsi_buy_max=w.get("rsi_buy_max",  tmpl["rsi_buy_max"]),
                rsi_sell_min=w.get("rsi_sell_min", tmpl["rsi_sell_min"]),
                atr_sl_mult=w.get("atr_sl_mult",   tmpl["atr_sl_mult"]),
                atr_tp_mult=w.get("atr_tp_mult",   tmpl["atr_tp_mult"]),
                use_trailing_sl=w.get("use_trailing_sl", False),
                volume_filter=w.get("volume_filter", False),
            )
            if shared is None:
                continue

            own_pnl    = data["best"]["pnl_pct"]
            shared_pnl = shared["pnl_pct"]

            if shared_pnl > own_pnl:
                shared_name = f"{winner_data['best']['name']}→{winner_sym.split('/')[0]}"
                log.info(
                    f"CROSS-BOT: {sym} übernimmt '{winner_data['best']['name']}' "
                    f"von {winner_sym} (f={winner_data['best']['fast']}/s={winner_data['best']['slow']}) | "
                    f"P&L: {own_pnl:+.2f}% → {shared_pnl:+.2f}%"
                )
                if not dry_run:
                    try:
                        db = StateDB(data["db_path"])
                        db.set_state("supervisor_fast",            str(winner_data["best"]["fast"]))
                        db.set_state("supervisor_slow",            str(winner_data["best"]["slow"]))
                        db.set_state("supervisor_strategy_name",   shared_name)
                        db.set_state("supervisor_sim_pnl",         f"{shared_pnl:+.2f}")
                        db.set_state("supervisor_sim_trades",      str(shared["num_trades"]))
                        db.set_state("supervisor_use_trailing_sl", str(winner_data["best"].get("use_trailing_sl", False)))
                        db.set_state("supervisor_volume_filter",   str(winner_data["best"].get("volume_filter", False)))
                        db.log_supervisor_cycle(
                            regime=data["regime"],
                            adx=-1,
                            atr_pct=0.0,
                            strategy_name=shared_name,
                            fast=winner_data["best"]["fast"],
                            slow=winner_data["best"]["slow"],
                            sim_pnl=shared_pnl,
                            num_trades=shared["num_trades"],
                            source=f"cross:{winner_sym.split('/')[0]}",
                            use_trailing_sl=winner_data["best"].get("use_trailing_sl", False),
                            volume_filter=winner_data["best"].get("volume_filter", False),
                        )
                        db.close()
                    except Exception as e:
                        log.error(f"Cross-Bot DB-Schreibfehler ({data['db_path']}): {e}")
            else:
                log.debug(
                    f"Cross-Bot: {sym} behält eigene Strategie "
                    f"({own_pnl:+.2f}% ≥ {shared_pnl:+.2f}% von {winner_sym})"
                )


def run_once(exchange: ccxt.Exchange, db_dir: str, timeframe: str, limit: int, dry_run: bool):
    """Einen Supervisor-Durchlauf über alle Bot-DBs."""
    log = logging.getLogger("supervisor")
    db_paths = sorted(glob(os.path.join(db_dir, "*.db")))

    # candles.db + news.db aus der Liste heraushalten
    db_paths = [p for p in db_paths if os.path.basename(p) not in ("candles.db", "news.db")]

    if not db_paths:
        log.info("Keine Bot-DBs gefunden.")
        return

    log.info(f"Supervisor-Durchlauf: {len(db_paths)} Bot(s)")

    # Candle-Cache einmal öffnen (geteilt über alle Symbole)
    candles_db_path = os.path.join(db_dir, "candles.db")
    conn_c = cdb.open_db(candles_db_path)

    # Ergebnisse für Cross-Bot Learning sammeln
    results: dict = {}

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

            # Historische Candles für Optimierung laden (wächst über Zeit auf 2000)
            history = cdb.load_candles(conn_c, symbol, timeframe, limit=2000)
            log.debug(f"{symbol}: {len(history)} Candles für Optimierung verfügbar")

            # Walk-Forward: 80% Training / 20% Validation
            MIN_WF_CANDLES = 200
            if len(history) >= MIN_WF_CANDLES:
                split = int(len(history) * 0.8)
                train, val = history[:split], history[split:]
            else:
                train, val = history, None

            # 72-Varianten-Optimierung (3 RSI/ATR × 6 SMA × 4 Feature-Kombos)
            tmpl = REGIME_TEMPLATES[regime]
            all_candidates = []
            for combo in FEATURE_COMBOS:
                candidate = optimizer.best_variant(
                    train,
                    rsi_atr_variants=RSI_ATR_COMBOS[regime],
                    **combo,
                )
                all_candidates.append(candidate)
            best = max(all_candidates, key=lambda x: (x.get("sqn", 0), x["pnl_pct"]))

            # Walk-Forward Validation
            if val:
                val_r = optimizer.simulate(
                    val, best["fast"], best["slow"],
                    rsi_buy_max=best["rsi_buy_max"], rsi_sell_min=best["rsi_sell_min"],
                    atr_sl_mult=best["atr_sl_mult"],  atr_tp_mult=best["atr_tp_mult"],
                    use_trailing_sl=best.get("use_trailing_sl", False),
                    volume_filter=best.get("volume_filter", False),
                )
                best["val_pnl"] = val_r["pnl_pct"] if val_r else None
                if best["val_pnl"] is not None:
                    log.info(
                        f"Walk-Forward {symbol}: train={best['pnl_pct']:+.2f}% "
                        f"val={best['val_pnl']:+.2f}% SQN={best.get('sqn', 0):.2f}"
                    )
            else:
                best["val_pnl"] = None

            _write(db_path, regime, adx_val, atr_pct, best, dry_run)

            # Ergebnis für Cross-Bot Learning merken
            results[symbol] = {
                "regime":   regime,
                "best":     best,
                "candles":  history,
                "db_path":  db_path,
            }

            time.sleep(1.5)   # Kraken Rate-Limit schonen

        # Cross-Bot Learning: beste Strategie zwischen Coins teilen
        if len(results) >= 2:
            _cross_bot_learning(results, dry_run)

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

    # Einmaliger Backfill beim Start
    candles_db_path = os.path.join(args.db_dir, "candles.db")
    conn_init = cdb.open_db(candles_db_path)
    for sym in _collect_symbols(args.db_dir):
        _backfill_candles(exchange, conn_init, sym, args.timeframe)
    conn_init.close()

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
