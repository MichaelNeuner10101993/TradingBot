"""
Web-Interface – Multi-Bot Dashboard.
Liest alle DB-Dateien aus db/*.db und aggregiert sie.

Start: python web/app.py
"""
import os
import sys
import signal
import subprocess
from glob import glob
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify, request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
from bot.persistence import StateDB

DB_DIR = os.environ.get("BOT_DB_DIR", os.path.join(PROJECT_ROOT, "db"))

KRAKEN_FEE = 0.0026  # Taker-Fee pro Order (0.26%); Round-Trip = 2 × 0.26% = 0.52%

_markets_cache: dict = {"data": None, "ts": 0.0}

PID_DIR = os.environ.get("BOT_PID_DIR", os.path.join(PROJECT_ROOT, "run"))
LOG_DIR = os.environ.get("BOT_LOG_DIR", os.path.join(PROJECT_ROOT, "logs"))
MAIN_PY = os.path.join(PROJECT_ROOT, "main.py")
PYTHON  = sys.executable

app = Flask(__name__)


def _is_running(pid_file: str) -> bool:
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _price_fmt(price: float) -> str:
    """Dynamische Dezimalstellen je nach Preishöhe."""
    if price == 0:
        return "0.00"
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.6f}"
    return f"{price:.8f}"


def _time_ago(iso: str) -> str:
    if not iso:
        return "–"
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        s  = int((datetime.now(timezone.utc) - dt).total_seconds())
        if s < 60:   return f"{s}s"
        if s < 3600: return f"{s // 60}min"
        return f"{s // 3600}h"
    except Exception:
        return iso


def _load_bot(db_path: str) -> dict:
    """Liest alle relevanten Daten aus einer Bot-DB."""
    try:
        db    = StateDB(db_path)
        state = db.get_all_state()

        symbol     = state.get("symbol", os.path.basename(db_path).replace(".db", "").replace("_", "/"))
        last_price = float(state.get("last_price", 0) or 0)

        open_trades = db.get_open_trades(symbol)
        for t in open_trades:
            entry  = t.get("entry_price") or 0
            amount = t.get("amount") or 0
            tp     = t.get("tp_price") or 0
            sl     = t.get("sl_price") or 0
            cost   = entry * amount  # Kaufwert ohne Gebühren

            if entry and last_price and amount:
                fee = (entry + last_price) * amount * KRAKEN_FEE
                t["pnl_pct"] = ((last_price - entry) * amount - fee) / cost * 100
            else:
                t["pnl_pct"] = None

            if entry and tp and amount:
                fee    = (entry + tp) * amount * KRAKEN_FEE
                profit = (tp - entry) * amount - fee
                t["expect_profit_eur"] = profit
                t["expect_profit_pct"] = profit / cost * 100
            else:
                t["expect_profit_eur"] = None
                t["expect_profit_pct"] = None

            if entry and sl and amount:
                fee  = (entry + sl) * amount * KRAKEN_FEE
                loss = (entry - sl) * amount + fee  # Verlust inkl. Gebühren (positiver Betrag)
                t["expect_loss_eur"] = loss
                t["expect_loss_pct"] = loss / cost * 100
            else:
                t["expect_loss_eur"] = None
                t["expect_loss_pct"] = None

            # Abstand vom aktuellen Preis zu SL/TP
            if last_price and sl:
                t["dist_to_sl_pct"] = (last_price - sl) / last_price * 100
            else:
                t["dist_to_sl_pct"] = None

            if last_price and tp:
                t["dist_to_tp_pct"] = (tp - last_price) / last_price * 100
            else:
                t["dist_to_tp_pct"] = None

        cur = db.conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 10")
        recent_orders = [dict(r) for r in cur.fetchall()]

        cur = db.conn.execute(
            "SELECT * FROM trades WHERE status != 'open' ORDER BY closed_at ASC"
        )
        closed_trades = [dict(r) for r in cur.fetchall()]

        # P&L-Verlauf für Chart berechnen (inkl. Kraken-Gebühren 0.26% × 2)
        pnl_history = []
        cumulative  = 0.0
        for t in closed_trades:
            entry  = t.get("entry_price") or 0
            amount = t.get("amount") or 0
            if not entry or not amount:
                t["pnl_pct"] = None
                continue
            if t["status"] == "tp_hit":
                exit_p = t.get("tp_price") or 0
                fee    = (entry + exit_p) * amount * KRAKEN_FEE
                pnl    = (exit_p - entry) * amount - fee
                t["pnl_pct"] = pnl / (entry * amount) * 100
            elif t["status"] == "sl_hit":
                exit_p = t.get("sl_price") or 0
                fee    = (entry + exit_p) * amount * KRAKEN_FEE
                pnl    = (exit_p - entry) * amount - fee
                t["pnl_pct"] = pnl / (entry * amount) * 100
            elif t["status"] == "signal_close":
                pnl = 0
                t["pnl_pct"] = None
            else:
                t["pnl_pct"] = None
                continue
            cumulative += pnl
            pnl_history.append({
                "date":       (t.get("closed_at") or "")[:16].replace("T", " "),
                "pnl":        round(pnl, 4),
                "cumulative": round(cumulative, 4),
                "status":     t["status"],
            })

        # Für Tabelle: neueste zuerst
        closed_trades = list(reversed(closed_trades))

        cur = db.conn.execute("SELECT * FROM errors ORDER BY occurred_at DESC LIMIT 3")
        recent_errors = [dict(r) for r in cur.fetchall()]
        now_utc = datetime.now(timezone.utc)
        for e in recent_errors:
            try:
                t = datetime.fromisoformat(e["occurred_at"]).replace(tzinfo=timezone.utc)
                e["is_old"] = (now_utc - t).total_seconds() > 86400
            except Exception:
                e["is_old"] = False

        db.close()

        base  = symbol.split("/")[0] if "/" in symbol else "?"
        quote = symbol.split("/")[1] if "/" in symbol else "?"

        balance_quote_float = float(state.get("balance_quote", 0) or 0)
        balance_base_float  = float(state.get("balance_base",  0) or 0)
        coin_value_eur      = balance_base_float * last_price

        # Aggregate P&L für Card-Übersicht
        total_pnl_eur           = 0.0
        total_expect_profit_eur = 0.0
        total_expect_loss_eur   = 0.0
        for t in open_trades:
            entry  = t.get("entry_price") or 0
            amount = t.get("amount") or 0
            if entry and amount and last_price:
                fee = (entry + last_price) * amount * KRAKEN_FEE
                total_pnl_eur += (last_price - entry) * amount - fee
            total_expect_profit_eur += t.get("expect_profit_eur") or 0
            total_expect_loss_eur   += t.get("expect_loss_eur")   or 0

        symbol_safe     = symbol.replace("/", "_")
        pid_file        = os.path.join(PID_DIR, f"{symbol_safe}.pid")
        process_running = _is_running(pid_file)

        # Zusatz: DB-Status "running" nur als running werten wenn last_update < 2 min alt
        if not process_running and state.get("status") == "running":
            try:
                last_up = datetime.fromisoformat(state.get("last_update", "")).replace(tzinfo=timezone.utc)
                age_s   = (datetime.now(timezone.utc) - last_up).total_seconds()
                if age_s < 120:
                    process_running = True
            except Exception:
                pass

        regime        = state.get("supervisor_regime", "–")
        adx_val       = state.get("supervisor_adx", "–")
        atr_pct       = state.get("supervisor_atr_pct", "–")
        supv_update   = state.get("supervisor_last_update", "")
        strategy_name = state.get("strategy_name", "Standard")
        sim_pnl       = state.get("sim_pnl", "–")
        fast_period   = state.get("fast_period", "9")
        slow_period   = state.get("slow_period", "21")

        return {
            "symbol":        symbol,
            "base":          base,
            "quote":         quote,
            "state":         state,
            "last_price":    last_price,
            "last_price_fmt": _price_fmt(last_price),
            "signal":        state.get("last_signal", "–"),
            "status":        state.get("status", "unknown"),
            "regime":        regime,
            "regime_adx":    adx_val,
            "regime_atr_pct": atr_pct,
            "regime_ago":    _time_ago(supv_update),
            "strategy_name": strategy_name,
            "sim_pnl":       sim_pnl,
            "fast_period":   fast_period,
            "slow_period":   slow_period,
            "last_update":   state.get("last_update", ""),
            "ago":           _time_ago(state.get("last_update", "")),
            "dry_run":       state.get("dry_run") == "True",
            "open_trades":   open_trades,
            "recent_orders": recent_orders,
            "closed_trades": closed_trades[:8],
            "pnl_history":   pnl_history,
            "recent_errors": recent_errors,
            "db_path":       db_path,
            "coin_value_eur":           round(coin_value_eur, 2),
            "balance_quote_float":      balance_quote_float,
            "total_pnl_eur":            round(total_pnl_eur, 2),
            "total_expect_profit_eur":  round(total_expect_profit_eur, 2),
            "total_expect_loss_eur":    round(total_expect_loss_eur, 2),
            "process_running":          process_running,
        }
    except Exception as e:
        return {
            "symbol":  os.path.basename(db_path).replace(".db", ""),
            "error":   str(e),
            "status":  "db_error",
            "signal":  "–",
            "ago":     "–",
            "dry_run": False,
            "open_trades": [], "recent_orders": [], "closed_trades": [], "recent_errors": [],
            "pnl_history": [],
            "state": {}, "last_price": 0, "last_price_fmt": "0.00", "base": "?", "quote": "?",
            "coin_value_eur": 0, "balance_quote_float": 0,
            "total_pnl_eur": 0, "total_expect_profit_eur": 0, "total_expect_loss_eur": 0,
            "process_running": False,
            "regime": "–", "regime_adx": "–", "regime_atr_pct": "–", "regime_ago": "–",
            "strategy_name": "Standard", "sim_pnl": "–", "fast_period": "9", "slow_period": "21",
        }


def load_all_bots() -> list[dict]:
    paths = sorted(glob(os.path.join(DB_DIR, "*.db")))
    return [_load_bot(p) for p in paths]


@app.route("/")
def index():
    bots = load_all_bots()
    active = [b for b in bots if not b.get("error")]
    # EUR-Balance: aktuellster Bot (höchstes last_update)
    sorted_active = sorted(active, key=lambda b: b.get("last_update", ""), reverse=True)
    eur_balance = sorted_active[0]["balance_quote_float"] if sorted_active else 0.0
    coin_total  = sum(b["coin_value_eur"] for b in active)
    return render_template(
        "index.html",
        bots=bots,
        portfolio_eur=round(eur_balance, 2),
        portfolio_coins=round(coin_total, 2),
        portfolio_total=round(eur_balance + coin_total, 2),
    )


@app.route("/api/bots")
def api_bots():
    bots = load_all_bots()
    # Nur state-Daten, keine DB-Objekte
    return jsonify([{k: v for k, v in b.items() if k not in ("db_path",)} for b in bots])


@app.route("/api/trade/<path:symbol>/<client_id>/sltp", methods=["POST"])
def update_sltp(symbol: str, client_id: str):
    """Setzt SL/TP eines offenen Trades manuell (aus dem Dashboard)."""
    try:
        data     = request.get_json(force=True)
        sl_price = float(data.get("sl_price", 0))
        tp_price = float(data.get("tp_price", 0))
        if sl_price <= 0 or tp_price <= 0 or sl_price >= tp_price:
            return jsonify({"ok": False, "error": "SL muss > 0 und kleiner als TP sein"}), 400
        symbol_safe = symbol.replace("/", "_")
        db_path     = os.path.join(DB_DIR, f"{symbol_safe}.db")
        db = StateDB(db_path)
        db.update_trade_sltp(client_id, sl_price, tp_price)
        db.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/markets")
def api_markets():
    """Alle aktiven EUR-Spotmärkte auf Kraken mit aktuellem Preis (5-Min-Cache)."""
    import time
    import ccxt
    now = time.time()
    if _markets_cache["data"] is not None and now - _markets_cache["ts"] < 300:
        return jsonify(_markets_cache["data"])
    try:
        ex      = ccxt.kraken({"enableRateLimit": True})
        markets = ex.load_markets()
        eur_syms = sorted([
            s for s, m in markets.items()
            if m.get("quote") == "EUR" and m.get("active") and m.get("spot")
        ])
        tickers = ex.fetch_tickers(eur_syms)
        result  = [
            {
                "symbol": s,
                "base":   markets[s]["base"],
                "last":   tickers.get(s, {}).get("last") or 0,
            }
            for s in eur_syms
        ]
        _markets_cache["data"] = result
        _markets_cache["ts"]   = now
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/bot/start", methods=["POST"])
def api_start_bot():
    try:
        data        = request.get_json(force=True)
        symbol      = data.get("symbol", "").strip().upper()
        if not symbol or "/" not in symbol:
            return jsonify({"ok": False, "error": "Ungültiges Symbol (z.B. BTC/EUR)"}), 400

        symbol_safe = symbol.replace("/", "_")
        pid_file    = os.path.join(PID_DIR, f"{symbol_safe}.pid")
        if _is_running(pid_file):
            return jsonify({"ok": False, "error": f"{symbol} läuft bereits"}), 400

        os.makedirs(PID_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)

        cmd = [
            PYTHON, MAIN_PY,
            "--symbol",        symbol,
            "--timeframe",     data.get("timeframe", "5m"),
            "--fast",          str(int(data.get("fast",  9))),
            "--slow",          str(int(data.get("slow", 21))),
            "--sl",            str(float(data.get("sl",  0.03))),
            "--tp",            str(float(data.get("tp",  0.06))),
            "--safety-buffer", str(float(data.get("safety_buffer", 0.10))),
        ]
        if data.get("dry_run"):
            cmd.append("--dry-run")

        log_path = os.path.join(LOG_DIR, f"{symbol_safe}.log")
        with open(log_path, "a") as logf:
            proc = subprocess.Popen(
                cmd, stdout=logf, stderr=logf,
                start_new_session=True, cwd=PROJECT_ROOT,
            )
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))

        return jsonify({"ok": True, "pid": proc.pid, "symbol": symbol})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/bot/delete", methods=["POST"])
def api_delete_bot():
    """Stoppt den Bot, entfernt conf + PID, archiviert oder löscht die DB."""
    try:
        import shutil as _shutil
        data        = request.get_json(force=True)
        symbol      = data.get("symbol", "").strip().upper()
        keep_db     = data.get("keep_db", True)
        symbol_safe = symbol.replace("/", "_")

        # 1. Prozess stoppen (best-effort, kein Fehler wenn nicht gefunden)
        pid_file = os.path.join(PID_DIR, f"{symbol_safe}.pid")
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    os.kill(int(f.read().strip()), signal.SIGINT)
            except Exception:
                pass
            try:
                os.remove(pid_file)
            except OSError:
                pass
        if _shutil.which("pkill"):
            subprocess.run(["pkill", "-INT", "-f", symbol], capture_output=True)

        # 2. bot.conf.d/<SYMBOL>.conf entfernen → systemd startet Bot nicht mehr
        conf_file = os.path.join(PROJECT_ROOT, "bot.conf.d", f"{symbol_safe}.conf")
        if os.path.exists(conf_file):
            os.remove(conf_file)

        # 3. DB archivieren oder löschen
        db_path = os.path.join(DB_DIR, f"{symbol_safe}.db")
        if os.path.exists(db_path):
            if keep_db:
                archive_dir = os.path.join(DB_DIR, "archive")
                os.makedirs(archive_dir, exist_ok=True)
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                _shutil.move(db_path, os.path.join(archive_dir, f"{symbol_safe}_{ts}.db"))
            else:
                os.remove(db_path)

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/bot/stop", methods=["POST"])
def api_stop_bot():
    try:
        import shutil
        data        = request.get_json(force=True)
        symbol      = data.get("symbol", "").strip().upper()
        symbol_safe = symbol.replace("/", "_")
        pid_file    = os.path.join(PID_DIR, f"{symbol_safe}.pid")
        killed      = False
        tried       = []

        # Stufe 1: PID-Datei (start_bots.sh)
        if os.path.exists(pid_file):
            try:
                with open(pid_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGINT)
                try:
                    os.remove(pid_file)
                except OSError:
                    pass
                killed = True
            except ProcessLookupError:
                # Stale PID – Datei löschen, weiter zu Stufe 2
                try:
                    os.remove(pid_file)
                except OSError:
                    pass
                tried.append("PID-Datei veraltet")
            except Exception as e:
                tried.append(f"PID-Datei: {e}")

        # Stufe 2: pgrep nach Symbol in Kommandozeile
        if not killed and shutil.which("pgrep"):
            try:
                r = subprocess.run(
                    ["pgrep", "-f", symbol],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    for pid_str in r.stdout.strip().splitlines():
                        try:
                            os.kill(int(pid_str), signal.SIGINT)
                        except Exception:
                            pass
                    killed = True
                else:
                    tried.append("pgrep: kein Treffer")
            except Exception as e:
                tried.append(f"pgrep: {e}")

        # Stufe 3: pkill nach Symbol
        if not killed and shutil.which("pkill"):
            try:
                r = subprocess.run(
                    ["pkill", "-INT", "-f", symbol],
                    capture_output=True,
                )
                if r.returncode == 0:
                    killed = True
                else:
                    tried.append("pkill: kein Treffer")
            except Exception as e:
                tried.append(f"pkill: {e}")

        if not killed:
            detail = "; ".join(tried) if tried else "keine Methode verfügbar"
            return jsonify({"ok": False, "error": f"{symbol}: Prozess nicht gefunden ({detail})"}), 404

        return jsonify({"ok": True, "symbol": symbol})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
