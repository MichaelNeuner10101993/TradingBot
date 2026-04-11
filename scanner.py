"""
scanner.py — Autonomer Trend Scanner für die CAT-TRADING Bot-Farm.

Läuft alle SCAN_INTERVAL_SECONDS (default: 30 min). Pro Zyklus:
  1. Alle Kraken EUR-Paare mit ausreichend Volumen ermitteln
  2. 1h-Candles laden, Momentum-Score berechnen
  3. Unterperformende Bots stoppen (consecutive_sl >= threshold + schlechtes Regime)
  4. Top-scorende neue Coins als Bots starten (respektiert Kapital + Max-Bots)
  5. Scan-Bericht in scanner.db + Log schreiben

Verwendung:
  python scanner.py               # Dauerschleife
  python scanner.py --once        # Einzelner Scan-Zyklus, dann Exit
  python scanner.py --dry-run     # Überschreibt SCAN_DRY_RUN aus conf
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sqlite3
import sys
import time
from pathlib import Path

import ccxt
import requests

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from bot.config import ExchangeConfig, OpsConfig
from bot.data_feed import build_exchange
from bot.scanner_notify import send_scanner_started, send_scanner_stopped, send_daily_summary
from bot.persistence import StateDB
from bot.scanner_score import score_pair, is_eligible_to_start, is_candidate_for_stop, PairScore

# ─── Konfiguration laden ──────────────────────────────────────────────────────

CONF_DEFAULTS: dict[str, str] = {
    "SCAN_INTERVAL_SECONDS":       "1800",
    "SCAN_MIN_VOLUME_EUR":         "500000",
    "SCAN_MIN_SCORE":              "4",
    "SCAN_MAX_BOTS":               "10",
    "SCAN_MIN_CAPITAL_PER_BOT":    "20",
    "SCAN_CONSECUTIVE_SL_THRESHOLD": "3",
    "SCAN_CANDLE_TIMEFRAME":       "1h",
    "SCAN_CANDLE_LIMIT":           "250",
    "SCAN_RATE_LIMIT_SLEEP":       "0.5",
    "SCAN_WEB_API_URL":            "http://localhost:5001",
    "SCAN_DRY_RUN":                "true",
    "SCAN_LOG_LEVEL":              "INFO",
    "SCAN_BOT_ARGS":               "--live --sl 0.015 --tp 0.50 --trailing-sl --trailing-sl-pct 0.03 --sma200-filter --slope-filter --breakeven --breakeven-pct 0.008 --partial-tp --partial-tp-fraction 0.5 --htf-timeframe 1h --htf-fast 21 --htf-slow 55 --startup-delay 60 --volume-factor 1.5",
}


def load_scanner_conf(path: str) -> dict[str, str]:
    """Liest scanner.conf im Shell-Variablen-Format (KEY="VALUE")."""
    cfg = dict(CONF_DEFAULTS)
    p = Path(path)
    if not p.exists():
        return cfg
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            cfg[key] = val
    return cfg


def _cfg_bool(cfg: dict, key: str) -> bool:
    return cfg.get(key, "false").lower() in ("true", "1", "yes")


def _cfg_int(cfg: dict, key: str) -> int:
    return int(cfg.get(key, CONF_DEFAULTS.get(key, "0")))


def _cfg_float(cfg: dict, key: str) -> float:
    return float(cfg.get(key, CONF_DEFAULTS.get(key, "0")))


# ─── Scanner-DB ───────────────────────────────────────────────────────────────

def init_scanner_db(db_path: str) -> sqlite3.Connection:
    """Initialisiert die scanner.db und erstellt die Tabelle falls nötig."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              INTEGER NOT NULL,
            pairs_scanned   INTEGER,
            pairs_scored    INTEGER,
            bots_started    TEXT,
            bots_stopped    TEXT,
            top_scores      TEXT,
            balance_eur     REAL,
            active_bots     INTEGER,
            notes           TEXT
        )
    """)
    conn.commit()
    return conn


def write_scan_report(
    conn: sqlite3.Connection,
    ts: int,
    pairs_scanned: int,
    pairs_scored: int,
    bots_started: list[str],
    bots_stopped: list[str],
    top_scores: list[dict],
    balance_eur: float,
    active_bots: int,
    notes: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO scan_history
          (ts, pairs_scanned, pairs_scored, bots_started, bots_stopped,
           top_scores, balance_eur, active_bots, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            pairs_scanned,
            pairs_scored,
            json.dumps(bots_started),
            json.dumps(bots_stopped),
            json.dumps(top_scores, ensure_ascii=False),
            balance_eur,
            active_bots,
            notes,
        ),
    )
    conn.commit()


# ─── Exchange-Operationen ─────────────────────────────────────────────────────

def get_all_kraken_eur_pairs(exchange: ccxt.Exchange, log: logging.Logger) -> list[str]:
    """Gibt alle aktiven EUR-Paare zurück die auf Kraken gehandelt werden können."""
    try:
        markets = exchange.load_markets()
        return [
            sym for sym, m in markets.items()
            if m.get("quote") == "EUR"
            and m.get("active", True)
            and m.get("spot", True)
        ]
    except ccxt.NetworkError as e:
        log.warning(f"Netzwerkfehler beim Laden der Märkte: {e}")
        return []
    except Exception as e:
        log.error(f"Unerwarteter Fehler beim Laden der Märkte: {e}")
        return []


def fetch_volume_filtered_pairs(
    exchange: ccxt.Exchange,
    pairs: list[str],
    min_volume_eur: float,
    log: logging.Logger,
) -> list[str]:
    """
    Filtert Paare nach 24h-Handelsvolumen in EUR.
    Nutzt fetch_tickers() als Batch-Aufruf (1 API-Call für alle).
    """
    if not pairs:
        return []
    try:
        tickers = exchange.fetch_tickers(pairs)
        result = []
        for sym, t in tickers.items():
            vol = t.get("quoteVolume") or 0.0
            if vol >= min_volume_eur:
                result.append(sym)
        log.info(
            f"Volume-Filter: {len(result)}/{len(pairs)} EUR-Paare "
            f"mit ≥{min_volume_eur/1000:.0f}k EUR/24h"
        )
        return result
    except ccxt.NetworkError as e:
        log.warning(f"Netzwerkfehler beim Ticker-Fetch: {e}")
        return []
    except Exception as e:
        log.error(f"Fehler beim Volume-Filter: {e}")
        return []


def fetch_candles_safe(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int,
    sleep_s: float,
) -> list | None:
    """
    Lädt OHLCV-Candles mit Rate-Limit-Pause VOR dem Fetch.
    Gibt None zurück bei Fehler (kein Crash).
    """
    time.sleep(sleep_s)  # Pause VOR dem Fetch (verhindert Rate-Limit)
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        return candles if candles else None
    except ccxt.BadSymbol:
        return None   # Kraken unterstützt diesen Timeframe nicht für dieses Paar
    except ccxt.DDoSProtection:
        time.sleep(10)
        return None
    except ccxt.NetworkError:
        return None
    except Exception:
        return None


def fetch_balance_eur(exchange: ccxt.Exchange, log: logging.Logger) -> float:
    """Gibt freies EUR-Guthaben zurück. 0.0 bei Fehler (konservatives Fallback)."""
    try:
        bal = exchange.fetch_balance()
        return float(bal.get("EUR", {}).get("free", 0.0))
    except Exception as e:
        log.warning(f"Balance-Fetch fehlgeschlagen: {e} → 0€ angenommen")
        return 0.0


# ─── Bot-State-Lesen ──────────────────────────────────────────────────────────

def read_bot_state(symbol: str, db_dir: str) -> dict:
    """
    Liest consecutive_sl und offene Trades aus der Bot-DB.
    Gibt konservative Defaults zurück wenn DB nicht existiert.
    """
    db_path = Path(db_dir) / f"{symbol.replace('/', '_')}.db"
    if not db_path.exists():
        return {"consecutive_sl": 0, "has_open_trade": False, "regime": ""}
    try:
        db = StateDB(str(db_path))
        consecutive_sl = int(db.get_state("consecutive_sl", "0") or "0")
        open_trades    = db.get_open_trades(symbol)
        regime         = db.get_state("supervisor_regime", "")
        db.close()
        return {
            "consecutive_sl": consecutive_sl,
            "has_open_trade": len(open_trades) > 0,
            "regime":         regime,
        }
    except Exception:
        return {"consecutive_sl": 0, "has_open_trade": False, "regime": ""}


# ─── Bot-Management via Web API ───────────────────────────────────────────────

def get_active_bots_safe(
    api_url: str,
    conf_dir: str,
    log: logging.Logger,
) -> list[dict] | None:
    """
    Holt laufende Bots vom Web-Dashboard.

    Gibt None zurück wenn:
    - API nicht erreichbar
    - API gibt 0 Bots zurück aber conf-Dateien existieren (API wahrscheinlich down)

    Im None-Fall: START und STOP komplett überspringen.
    """
    try:
        resp = requests.get(f"{api_url}/api/bots", timeout=8)
        resp.raise_for_status()
        bots = resp.json()
        if not isinstance(bots, list):
            log.warning("Web API: unerwartetes Format bei /api/bots")
            return None
        # Sanity-Check: 0 Bots aber conf-Dateien vorhanden → API wahrscheinlich down
        conf_count = len(list(Path(conf_dir).glob("*.conf")))
        if len(bots) == 0 and conf_count > 0:
            log.warning(
                f"Web API: 0 Bots gemeldet, aber {conf_count} conf-Dateien existieren "
                f"→ API evtl. down, skip Start/Stopp"
            )
            return None
        return bots
    except requests.ConnectionError:
        log.warning(f"Web API nicht erreichbar ({api_url}) → skip Start/Stopp")
        return None
    except Exception as e:
        log.warning(f"Web API Fehler: {e} → skip Start/Stopp")
        return None


def calculate_available_slots(
    active_bots: list[dict],
    max_bots: int,
    balance_eur: float,
    min_capital_per_bot: float,
) -> int:
    """Wie viele neue Bots können noch gestartet werden?"""
    running = sum(1 for b in active_bots if b.get("process_running", False))
    affordable = int(balance_eur / min_capital_per_bot) if min_capital_per_bot > 0 else 0
    hard_cap = min(max_bots, affordable)
    return max(0, hard_cap - running)


def write_bot_conf(symbol: str, bot_args: str, conf_dir: str) -> Path:
    """
    Schreibt eine conf-Datei für einen neuen Coin.
    Überschreibt KEINE existierenden conf-Dateien (respektiert manuell konfigurierte Bots).
    """
    safe = symbol.replace("/", "_")
    conf_path = Path(conf_dir) / f"{safe}.conf"
    if conf_path.exists():
        return conf_path   # nicht überschreiben
    content = f'BOT_SYMBOL="{symbol}"\nBOT_ARGS="{bot_args}"\n'
    conf_path.write_text(content)
    return conf_path


def start_bot_api(symbol: str, api_url: str, log: logging.Logger) -> bool:
    """Startet einen Bot via Web API."""
    try:
        resp = requests.post(
            f"{api_url}/api/bot/start",
            json={"symbol": symbol},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info(f"Bot gestartet: {symbol}")
            return True
        log.warning(f"Bot-Start {symbol}: HTTP {resp.status_code} — {resp.text[:100]}")
        return False
    except Exception as e:
        log.error(f"Bot-Start {symbol} fehlgeschlagen: {e}")
        return False


def stop_bot_api(symbol: str, api_url: str, log: logging.Logger) -> bool:
    """Stoppt einen Bot via Web API."""
    try:
        resp = requests.post(
            f"{api_url}/api/bot/stop",
            json={"symbol": symbol},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info(f"Bot gestoppt: {symbol}")
            return True
        log.warning(f"Bot-Stopp {symbol}: HTTP {resp.status_code} — {resp.text[:100]}")
        return False
    except Exception as e:
        log.error(f"Bot-Stopp {symbol} fehlgeschlagen: {e}")
        return False


# ─── Haupt-Scan-Zyklus ────────────────────────────────────────────────────────

def _get_pnl_24h(db_dir: str) -> float:
    """Summiert realisierten P&L aller Bots aus den letzten 24h."""
    import glob as _glob
    cutoff = int(time.time()) - 86400
    total  = 0.0
    for db_path in _glob.glob(os.path.join(db_dir, "*.db")):
        base = os.path.basename(db_path)
        if base in ("scanner.db", "supervisor.db", "news.db"):
            continue
        try:
            conn = sqlite3.connect(db_path, timeout=3)
            rows = conn.execute(
                "SELECT pnl_eur FROM trades WHERE status='closed' AND exit_time > ?",
                (cutoff,)
            ).fetchall()
            conn.close()
            total += sum(r[0] for r in rows if r[0])
        except Exception:
            pass
    return total


def run_scan_cycle(
    cfg: dict,
    exchange: ccxt.Exchange,
    scanner_db: sqlite3.Connection,
    log: logging.Logger,
    dry_run: bool,
) -> None:
    """Führt einen vollständigen Scan-Zyklus durch."""
    ts_start = int(time.time())
    log.info(f"{'[DRY-RUN] ' if dry_run else ''}=== Scan-Zyklus gestartet ===")

    db_dir   = os.path.join(PROJECT_ROOT, "db")
    conf_dir = os.path.join(PROJECT_ROOT, "bot.conf.d")
    api_url  = cfg["SCAN_WEB_API_URL"]

    # ── 1. Aktive Bots holen (Sanity-Check) ───────────────────────────────────
    active_bots = get_active_bots_safe(api_url, conf_dir, log)
    if active_bots is None:
        log.warning("Aktive Bots nicht ermittelbar → Zyklus ohne Start/Stopp")
        write_scan_report(scanner_db, ts_start, 0, 0, [], [], [], 0.0, 0, "api_unavailable")
        return

    active_symbols = {b["symbol"] for b in active_bots if b.get("process_running", False)}
    log.info(f"Aktuell laufende Bots: {len(active_symbols)} — {sorted(active_symbols)}")

    # ── 2. Balance ─────────────────────────────────────────────────────────────
    balance_eur = fetch_balance_eur(exchange, log)
    log.info(f"Verfügbares EUR-Guthaben: {balance_eur:.2f}€")

    # ── 3. EUR-Paare laden und nach Volumen filtern ────────────────────────────
    all_pairs = get_all_kraken_eur_pairs(exchange, log)
    if not all_pairs:
        log.warning("Keine EUR-Paare geladen (Exchange evtl. down)")
        write_scan_report(scanner_db, ts_start, 0, 0, [], [], [], balance_eur, len(active_bots), "exchange_unavailable")
        return

    min_vol = _cfg_float(cfg, "SCAN_MIN_VOLUME_EUR")
    volume_pairs = fetch_volume_filtered_pairs(exchange, all_pairs, min_vol, log)
    if not volume_pairs:
        log.warning("Keine Paare nach Volume-Filter übrig")
        write_scan_report(scanner_db, ts_start, len(all_pairs), 0, [], [], [], balance_eur, len(active_bots), "no_volume_pairs")
        return

    # ── 4. Candles laden und scoren ───────────────────────────────────────────
    timeframe  = cfg["SCAN_CANDLE_TIMEFRAME"]
    limit      = _cfg_int(cfg, "SCAN_CANDLE_LIMIT")
    sleep_s    = _cfg_float(cfg, "SCAN_RATE_LIMIT_SLEEP")

    scores: list[PairScore] = []
    log.info(f"Scanne {len(volume_pairs)} Paare (Timeframe={timeframe}, Limit={limit}) …")

    for sym in volume_pairs:
        candles = fetch_candles_safe(exchange, sym, timeframe, limit, sleep_s)
        if candles is None or len(candles) < 30:
            continue
        ps = score_pair(sym, candles)
        if not ps.disqualified:
            scores.append(ps)

    scores.sort(key=lambda s: (s.total, s.atr_pct), reverse=True)
    log.info(f"Gescored: {len(scores)} Paare")

    # Scores-Dict für schnellen Lookup
    score_by_symbol: dict[str, PairScore] = {s.symbol: s for s in scores}

    # ── 5. STOPP-PHASE ─────────────────────────────────────────────────────────
    bots_stopped: list[str] = []
    sl_threshold = _cfg_int(cfg, "SCAN_CONSECUTIVE_SL_THRESHOLD")

    for bot in active_bots:
        sym = bot.get("symbol", "")
        if not bot.get("process_running", False):
            continue

        state = read_bot_state(sym, db_dir)
        # Regime aus Score (frischer als DB) oder Fallback auf DB-Wert
        regime = score_by_symbol[sym].regime if sym in score_by_symbol else state["regime"]

        should_stop, stop_reason = is_candidate_for_stop(
            regime,
            state["consecutive_sl"],
            state["has_open_trade"],
            sl_threshold,
        )

        if should_stop:
            log.warning(
                f"STOPP-KANDIDAT: {sym} | Grund: {stop_reason} | "
                f"{'[DRY]' if dry_run else 'stoppe…'}"
            )
            if not dry_run:
                if stop_bot_api(sym, api_url, log):
                    bots_stopped.append(sym)
                    send_scanner_stopped(
                        sym, stop_reason, regime,
                        state["consecutive_sl"], dry_run=False
                    )
            else:
                bots_stopped.append(f"{sym}(dry)")
                send_scanner_stopped(
                    sym, stop_reason, regime,
                    state["consecutive_sl"], dry_run=True
                )

    # ── 6. START-PHASE ─────────────────────────────────────────────────────────
    bots_started: list[str] = []
    min_score  = _cfg_int(cfg, "SCAN_MIN_SCORE")
    bot_args   = cfg["SCAN_BOT_ARGS"]

    # Slots nach Stopp neu berechnen
    active_bots_after_stop = [b for b in active_bots
                               if b.get("process_running") and b.get("symbol") not in bots_stopped]
    slots = calculate_available_slots(
        active_bots_after_stop,
        _cfg_int(cfg, "SCAN_MAX_BOTS"),
        balance_eur,
        _cfg_float(cfg, "SCAN_MIN_CAPITAL_PER_BOT"),
    )
    log.info(f"Verfügbare Slots für neue Bots: {slots}")

    if balance_eur < _cfg_float(cfg, "SCAN_MIN_CAPITAL_PER_BOT"):
        log.warning(
            f"Balance {balance_eur:.2f}€ < Min {_cfg_float(cfg, 'SCAN_MIN_CAPITAL_PER_BOT'):.0f}€ "
            f"→ keine neuen Bots"
        )
        slots = 0

    for ps in scores:
        if slots <= 0:
            break
        if ps.symbol in active_symbols:
            continue   # läuft bereits

        eligible, elig_reason = is_eligible_to_start(ps, min_score)
        if not eligible:
            continue

        log.info(
            f"START-KANDIDAT: {ps.symbol} | Score={ps.total} | Regime={ps.regime} | "
            f"ADX={ps.adx:.1f} | RSI={f'{ps.rsi_val:.1f}' if ps.rsi_val is not None else '–'} | "
            f"{'[DRY]' if dry_run else 'starte…'}"
        )

        if not dry_run:
            write_bot_conf(ps.symbol, bot_args, conf_dir)
            if start_bot_api(ps.symbol, api_url, log):
                bots_started.append(ps.symbol)
                active_symbols.add(ps.symbol)
                slots -= 1
                send_scanner_started(
                    ps.symbol, ps.total, ps.regime, ps.adx, ps.rsi_val,
                    elig_reason,
                    len(active_symbols) - 1,
                    _cfg_int(cfg, "SCAN_MAX_BOTS"),
                    balance_eur, dry_run=False
                )
        else:
            bots_started.append(f"{ps.symbol}(dry)")
            slots -= 1

    # ── 7. Report ─────────────────────────────────────────────────────────────
    top_scores_json = [
        {
            "symbol":  s.symbol,
            "score":   s.total,
            "regime":  s.regime,
            "adx":     round(s.adx, 1),
            "rsi":     round(s.rsi_val, 1) if s.rsi_val else None,
            "running": s.symbol in active_symbols,
        }
        for s in scores[:15]
    ]

    duration = int(time.time()) - ts_start
    log.info(
        f"=== Scan abgeschlossen in {duration}s | "
        f"Gescannt={len(volume_pairs)} | Gescored={len(scores)} | "
        f"Gestartet={len(bots_started)} | Gestoppt={len(bots_stopped)} ==="
    )
    log.info(f"Top-5 Coins: {[f'{s.symbol}({s.total}pt,{s.regime})' for s in scores[:5]]}")

    # ── 8. Tägliche Zusammenfassung (08:00 UTC) ──────────────────────────────
    _now_utc = __import__("datetime").datetime.utcnow()
    _summary_key = _now_utc.strftime("%Y-%m-%d")
    _sent_key_file = os.path.join(PROJECT_ROOT, "db", ".summary_sent")
    _last_key = ""
    try:
        if os.path.exists(_sent_key_file):
            _last_key = open(_sent_key_file).read().strip()
    except Exception:
        pass
    if _now_utc.hour >= 8 and _summary_key != _last_key:
        try:
            _active_list = [
                {"symbol": sym, "regime": score_by_symbol[sym].regime if sym in score_by_symbol else "?"}
                for sym in sorted(active_symbols)
            ]
            _top_list = [
                {"symbol": s.symbol, "score": s.total, "regime": s.regime}
                for s in scores[:3]
            ]
            _pnl_24h = _get_pnl_24h(db_dir)
            _staking_eur = 0.0
            try:
                import requests as _req
                _sr = _req.get("http://localhost:5001/api/staking", timeout=5).json()
                _staking_eur = float(_sr.get("total_eur", 0) or 0)
            except Exception:
                pass
            send_daily_summary(balance_eur, _pnl_24h, _active_list, _top_list, staking_eur=_staking_eur)
            open(_sent_key_file, "w").write(_summary_key)
            log.info(f"Tägliche Zusammenfassung gesendet (P&L 24h: {_pnl_24h:+.2f}€)")
        except Exception as _e:
            log.warning(f"Tägliche Zusammenfassung fehlgeschlagen: {_e}")

    write_scan_report(
        scanner_db,
        ts_start,
        len(volume_pairs),
        len(scores),
        bots_started,
        bots_stopped,
        top_scores_json,
        balance_eur,
        len(active_bots),
    )


# ─── Argument-Parser ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CAT-TRADING Trend Scanner")
    p.add_argument("--once",    action="store_true", help="Nur einen Zyklus ausführen, dann Exit")
    p.add_argument("--dry-run", action="store_true", help="Überschreibt SCAN_DRY_RUN=true")
    p.add_argument("--conf",    default=os.path.join(PROJECT_ROOT, "scanner.conf"),
                   help="Pfad zur scanner.conf")
    return p.parse_args()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    cfg  = load_scanner_conf(args.conf)

    # Log-Setup
    log_level = getattr(logging, cfg["SCAN_LOG_LEVEL"].upper(), logging.INFO)
    log_dir   = os.path.join(PROJECT_ROOT, "logs", "scanner")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] scanner: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(log_dir, "scanner.log")),
        ],
    )
    log = logging.getLogger("scanner")

    # Dry-Run: CLI überschreibt conf
    dry_run = _cfg_bool(cfg, "SCAN_DRY_RUN") or args.dry_run
    if dry_run:
        log.info("DRY-RUN aktiv — keine echten Start/Stopp-Aktionen")

    # Exchange & DB
    exchange   = build_exchange(ExchangeConfig())
    db_path    = os.path.join(PROJECT_ROOT, "db", "scanner.db")
    scanner_db = init_scanner_db(db_path)

    interval = _cfg_int(cfg, "SCAN_INTERVAL_SECONDS")
    log.info(f"Scanner gestartet | Intervall={interval}s | DryRun={dry_run}")

    while True:
        cycle_start = time.monotonic()
        try:
            run_scan_cycle(cfg, exchange, scanner_db, log, dry_run)
        except Exception as e:
            log.error(f"Scan-Zyklus Fehler: {e}", exc_info=True)

        if args.once:
            log.info("--once: Scanner beendet.")
            break

        elapsed     = time.monotonic() - cycle_start
        sleep_secs  = max(0.0, interval - elapsed)
        log.info(f"Nächster Scan in {sleep_secs:.0f}s …")
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
