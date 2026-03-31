"""
Web-Interface – Multi-Bot Dashboard.
Liest alle DB-Dateien aus db/*.db und aggregiert sie.

Start: python web/app.py
"""
import os
import re
import sys
import shlex
import signal
import subprocess
from glob import glob
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify, request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
from bot.persistence import StateDB

DB_DIR       = os.environ.get("BOT_DB_DIR",      os.path.join(PROJECT_ROOT, "db"))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 5001))

KRAKEN_FEE = 0.0026  # Taker-Fee pro Order (0.26%); Round-Trip = 2 × 0.26% = 0.52%

_markets_cache: dict = {"data": None, "ts": 0.0}
_eur_rate_cache: dict = {"data": {}, "ts": 0.0}  # Quote-Currency → EUR-Kurs
_balance_cache:  dict = {"data": None, "ts": 0.0}  # Voller Kraken-Kontostand


def _eur_rate(quote_currency: str) -> float:
    """
    Gibt den EUR-Wechselkurs für eine Quote-Currency zurück.
    EUR → 1.0  |  USDT/USD → Kraken-Preis (5-Min-Cache).
    Fallback: 1.0 wenn Rate nicht verfügbar.
    """
    import time
    import ccxt as _ccxt
    if quote_currency in ("EUR", ""):
        return 1.0
    now = time.time()
    if now - _eur_rate_cache["ts"] < 300 and quote_currency in _eur_rate_cache["data"]:
        return _eur_rate_cache["data"][quote_currency]
    try:
        ex = _ccxt.kraken({"enableRateLimit": True})
        ticker = ex.fetch_ticker(f"{quote_currency}/EUR")
        rate = float(ticker["last"] or 1.0)
        _eur_rate_cache["data"][quote_currency] = rate
        _eur_rate_cache["ts"] = now
        return rate
    except Exception:
        return _eur_rate_cache["data"].get(quote_currency, 1.0)

def _fetch_kraken_balance() -> dict:
    """
    Holt den echten Kraken-Kontostand inkl. aller Coins (5-Min-Cache).
    Gibt {'eur_free', 'coins', 'coins_total_eur', 'total_eur'} zurück.
    """
    import time
    now = time.time()
    if _balance_cache["data"] is not None and now - _balance_cache["ts"] < 300:
        return _balance_cache["data"]
    try:
        from bot.config import ExchangeConfig
        from bot.data_feed import build_exchange
        ex = build_exchange(ExchangeConfig())
        raw = ex.fetch_balance()

        _SKIP = {"info", "free", "used", "total", "datetime", "timestamp"}
        eur_free = float((raw.get("EUR") or {}).get("free", 0) or 0)

        non_eur: dict[str, float] = {}
        for cur, amounts in raw.items():
            if cur in _SKIP or cur == "EUR" or not isinstance(amounts, dict):
                continue
            amt = float(amounts.get("total", 0) or 0)
            if amt > 0:
                non_eur[cur] = amt

        # Preise pro Coin in EUR holen (einzeln, mit Fehler-Toleranz)
        coin_values: dict = {}
        coins_total = 0.0
        if non_eur:
            for coin, amount in non_eur.items():
                try:
                    ticker = ex.fetch_ticker(f"{coin}/EUR")
                    price  = float(ticker.get("last") or 0)
                except Exception:
                    price = 0.0  # kein EUR-Markt (z.B. REPV1, ETHW) → überspringen
                value = amount * price
                coins_total += value
                coin_values[coin] = {
                    "amount":    round(amount, 8),
                    "price":     price,
                    "value_eur": round(value, 2),
                }

        result = {
            "eur_free":       round(eur_free, 2),
            "coins":          coin_values,
            "coins_total_eur": round(coins_total, 2),
            "total_eur":      round(eur_free + coins_total, 2),
        }
        _balance_cache["data"] = result
        _balance_cache["ts"]   = now
        return result
    except Exception as e:
        return {"error": str(e), "eur_free": 0, "coins": {}, "coins_total_eur": 0, "total_eur": 0}


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
        rate_to_eur         = _eur_rate(quote)                          # 1.0 für EUR
        coin_value_eur      = balance_base_float * last_price * rate_to_eur

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
            "rate_to_eur":   rate_to_eur,
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
            "paused":                   state.get("paused", "false").lower() == "true",
            "sentiment_score":          state.get("current_sentiment_score", ""),
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
            "rate_to_eur": 1.0,
        }


_SKIP_DBS = {"candles.db", "news.db"}

def load_all_bots() -> list[dict]:
    paths = sorted(
        p for p in glob(os.path.join(DB_DIR, "*.db"))
        if os.path.basename(p) not in _SKIP_DBS
    )
    return [_load_bot(p) for p in paths]


@app.route("/")
def index():
    bots = load_all_bots()
    # Cache nutzen wenn vorhanden (kein blockierender API-Call beim Seitenaufbau)
    # JS aktualisiert die Werte via /api/balance im Hintergrund
    bal = _balance_cache["data"]
    if bal and not bal.get("error"):
        fiat_eur   = bal.get("eur_free", 0)
        coin_total = bal.get("coins_total_eur", 0)
    else:
        # Fallback auf Bot-DBs wenn Cache leer (erster Start)
        active = [b for b in bots if not b.get("error")]
        seen: dict[str, float] = {}
        for b in sorted(active, key=lambda b: b.get("last_update", ""), reverse=True):
            q = b.get("quote", "EUR")
            if q not in seen:
                seen[q] = b["balance_quote_float"] * b.get("rate_to_eur", 1.0)
        fiat_eur   = sum(seen.values())
        coin_total = sum(b["coin_value_eur"] for b in active)
    return render_template(
        "index.html",
        bots=bots,
        portfolio_eur=round(fiat_eur, 2),
        portfolio_coins=round(coin_total, 2),
        portfolio_total=round(fiat_eur + coin_total, 2),
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


@app.route("/api/balance")
def api_balance():
    """Echter Kraken-Kontostand: EUR + alle Coins (5-Min-Cache)."""
    data = _fetch_kraken_balance()
    if "error" in data:
        return jsonify(data), 500
    return jsonify(data)


@app.route("/api/holdings")
def api_holdings():
    """Alle gehaltenen Coins mit Menge, Kurs, EUR-Wert und Bot-Status."""
    bal  = _fetch_kraken_balance()
    bots = {b["symbol"]: b for b in load_all_bots()}
    result = []
    for coin, d in bal.get("coins", {}).items():
        sym = f"{coin}/EUR"
        bot = bots.get(sym, {})
        result.append({
            "coin":        coin,
            "symbol":      sym,
            "amount":      d.get("amount", 0),
            "price":       d.get("price", 0),
            "value_eur":   d.get("value_eur", 0),
            "has_bot":     sym in bots,
            "bot_running": bot.get("process_running", False),
        })
    result.sort(key=lambda x: -x["value_eur"])
    return jsonify(result)


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
        if data.get("trailing_sl"):
            cmd += ["--trailing-sl", "--trailing-sl-pct", str(float(data.get("trailing_sl_pct", 0.02)))]
        sl_cd = data.get("sl_cooldown")
        if sl_cd is not None:
            cmd += ["--sl-cooldown", str(int(sl_cd))]
        if data.get("volume_filter"):
            cmd += ["--volume-filter", "--volume-factor", str(float(data.get("volume_factor", 1.2)))]
        if data.get("breakeven"):
            cmd += ["--breakeven", "--breakeven-pct", str(float(data.get("breakeven_pct", 0.01)))]
        if data.get("partial_tp"):
            cmd += ["--partial-tp", "--partial-tp-fraction", str(float(data.get("partial_tp_fraction", 0.5)))]
        htf_tf = data.get("htf_timeframe", "")
        if htf_tf:
            cmd += ["--htf-timeframe", htf_tf,
                    "--htf-fast", str(int(data.get("htf_fast", 9))),
                    "--htf-slow", str(int(data.get("htf_slow", 21)))]

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
        service  = f"tradingbot@{symbol_safe}.service"
        subprocess.run(["sudo", "systemctl", "stop", service],
                       capture_output=True, timeout=10)
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


@app.route("/api/direct_sell", methods=["POST"])
def api_direct_sell():
    """Verkauft sofort direkt über Kraken – ohne laufenden Bot."""
    try:
        from bot.config import ExchangeConfig
        from bot.data_feed import build_exchange
        data   = request.get_json(force=True)
        symbol = data.get("symbol", "").strip().upper()
        if not symbol or "/" not in symbol:
            return jsonify({"ok": False, "error": "Ungültiges Symbol"}), 400

        base = symbol.split("/")[0]
        ex   = build_exchange(ExchangeConfig())
        ex.load_markets()
        bal  = ex.fetch_balance()
        amount = float((bal.get(base) or {}).get("free", 0) or 0)
        if amount <= 0:
            return jsonify({"ok": False, "error": f"Kein {base}-Guthaben vorhanden"}), 400

        try:
            amount_p = float(ex.amount_to_precision(symbol, amount))
        except Exception:
            amount_p = round(amount, 8)
        if amount_p <= 0:
            return jsonify({"ok": False, "error": "Menge zu klein nach Precision-Rounding"}), 400

        order = ex.create_market_sell_order(symbol, amount_p)
        exit_price = order.get("average") or order.get("price") or 0

        # DB-Trade schließen falls vorhanden
        symbol_safe = symbol.replace("/", "_")
        db_path = os.path.join(DB_DIR, f"{symbol_safe}.db")
        if os.path.exists(db_path):
            db = StateDB(db_path)
            for trade in db.get_open_trades(symbol):
                db.close_trade(trade["client_id"], "signal_close")
            db.close()

        return jsonify({"ok": True, "symbol": symbol, "amount": amount_p, "price": exit_price})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/bot/force_signal", methods=["POST"])
def api_force_signal():
    """Setzt ein Force-Signal (BUY/SELL) das der Bot im nächsten Loop einmalig ausführt."""
    try:
        data   = request.get_json(force=True)
        symbol = data.get("symbol", "").strip().upper()
        signal = data.get("signal", "").upper()
        if signal not in ("BUY", "SELL"):
            return jsonify({"ok": False, "error": "Signal muss BUY oder SELL sein"}), 400
        symbol_safe = symbol.replace("/", "_")
        db_path = os.path.join(DB_DIR, f"{symbol_safe}.db")
        if not os.path.exists(db_path):
            return jsonify({"ok": False, "error": f"Keine DB für {symbol} gefunden"}), 404
        db = StateDB(db_path)
        db.set_state("force_signal", signal)
        db.close()
        return jsonify({"ok": True, "symbol": symbol, "signal": signal})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/bot/set_sltp_pct", methods=["POST"])
def api_set_sltp_pct():
    """Setzt SL/TP als Prozentsatz vom Entry-Preis aller offenen Trades eines Bots."""
    try:
        data       = request.get_json(force=True)
        symbol     = data.get("symbol", "").strip().upper()
        sl_pct     = data.get("sl_pct")   # z.B. 2.0 → 2%
        tp_pct     = data.get("tp_pct")   # z.B. 4.0 → 4%
        symbol_safe = symbol.replace("/", "_")
        db_path    = os.path.join(DB_DIR, f"{symbol_safe}.db")
        if not os.path.exists(db_path):
            return jsonify({"ok": False, "error": f"Keine DB für {symbol} gefunden"}), 404

        db     = StateDB(db_path)
        trades = db.get_open_trades(symbol)
        if not trades:
            db.close()
            return jsonify({"ok": False, "error": "Kein offener Trade"}), 404

        updated = 0
        for t in trades:
            entry = t.get("entry_price") or 0
            if not entry:
                continue
            new_sl = float(t.get("sl_price") or 0)
            new_tp = float(t.get("tp_price") or 0)
            if sl_pct is not None:
                new_sl = entry * (1 - float(sl_pct) / 100)
            if tp_pct is not None:
                new_tp = entry * (1 + float(tp_pct) / 100)
            if new_sl <= 0 or new_tp <= 0 or new_sl >= new_tp:
                db.close()
                return jsonify({"ok": False, "error": f"Ungültige SL/TP-Werte (SL={new_sl:.4f} TP={new_tp:.4f})"}), 400
            db.update_trade_sltp(t["client_id"], new_sl, new_tp)
            updated += 1

        db.close()
        return jsonify({"ok": True, "symbol": symbol, "updated_trades": updated})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _update_conf_args(symbol_safe: str, data: dict):
    """Aktualisiert BOT_ARGS in bot.conf.d/<SYMBOL>.conf für Persistenz über Neustarts."""
    conf_path = os.path.join(PROJECT_ROOT, "bot.conf.d", f"{symbol_safe}.conf")
    if not os.path.exists(conf_path):
        return
    try:
        with open(conf_path) as f:
            content = f.read()
        m = re.search(r'^BOT_ARGS=(.*)$', content, re.MULTILINE)
        if not m:
            return
        args_list = shlex.split(m.group(1))
        # data_key → (cli_flag, divisor_to_fraction)  divisor=100 wenn UI %-Wert sendet
        ARG_MAP = {
            "fast_period":          ("--fast",               1),
            "slow_period":          ("--slow",               1),
            "sl_pct":               ("--sl",                 100),
            "tp_pct":               ("--tp",                 100),
            "safety_buffer":        ("--safety-buffer",      100),
            "trailing_sl_pct":      ("--trailing-sl-pct",    100),
            "volume_factor":        ("--volume-factor",         1),
            "breakeven_pct":        ("--breakeven-pct",       100),
            "partial_tp_fraction":  ("--partial-tp-fraction", 100),
        }
        for key, (flag, div) in ARG_MAP.items():
            val = data.get(key)
            if val is None:
                continue
            val_str = str(round(float(val) / div, 6)).rstrip("0").rstrip(".")
            if flag in args_list:
                args_list[args_list.index(flag) + 1] = val_str
            else:
                args_list += [flag, val_str]
        # Boolean-Flags (kein Wert, nur Flag-Präsenz): ein-/ausschalten
        BOOL_FLAGS = {
            "trailing_sl":  "--trailing-sl",
            "volume_filter": "--volume-filter",
            "breakeven_enabled": "--breakeven",
            "partial_tp":   "--partial-tp",
        }
        for key, flag in BOOL_FLAGS.items():
            val = data.get(key)
            if val is None:
                continue
            enabled = str(val).lower() in ("true", "1", "yes")
            if enabled and flag not in args_list:
                args_list.append(flag)
            elif not enabled and flag in args_list:
                args_list.remove(flag)
        new_args = " ".join(args_list)
        new_content = re.sub(r'^BOT_ARGS=.*$', f'BOT_ARGS={new_args}', content, flags=re.MULTILINE)
        with open(conf_path, "w") as f:
            f.write(new_content)
    except Exception as e:
        app.logger.warning(f"Conf-Update fehlgeschlagen für {symbol_safe}: {e}")


@app.route("/api/bot/set_runtime_params", methods=["POST"])
def api_set_runtime_params():
    """Setzt alle Laufzeit-Parameter via pending_* Keys in bot_state.
    Der Bot liest und löscht diese beim nächsten Loop (~60s).
    Aktualisiert auch bot.conf.d/*.conf für Persistenz über Neustarts.
    """
    try:
        data        = request.get_json(force=True)
        symbol      = data.get("symbol", "").strip().upper()
        symbol_safe = symbol.replace("/", "_")
        db_path     = os.path.join(DB_DIR, f"{symbol_safe}.db")
        if not os.path.exists(db_path):
            return jsonify({"ok": False, "error": f"Keine DB für {symbol} gefunden"}), 404
        db = StateDB(db_path)
        written = []

        # Boolean/Float-Keys direkt als pending_ speichern
        for key in ("breakeven_enabled",
                    "trailing_sl",
                    "volume_filter", "volume_factor",
                    "partial_tp",
                    "rsi_buy_max", "rsi_sell_min",
                    "fast_period", "slow_period",
                    "sentiment_buy_enabled", "sentiment_buy_min",
                    "sentiment_sell_enabled", "sentiment_sell_max", "sentiment_sell_mode",
                    "sentiment_stop_enabled", "sentiment_stop_threshold"):
            val = data.get(key)
            if val is not None:
                db.set_state(f"pending_{key}", str(val))
                written.append(key)

        # SL/TP + Trailing/Breakeven + Partial-TP: UI sendet %, Bot erwartet Fraktion (0.03 statt 3)
        for key in ("sl_pct", "tp_pct", "trailing_sl_pct", "breakeven_pct", "partial_tp_fraction"):
            val = data.get(key)
            if val is not None:
                db.set_state(f"pending_{key}", str(float(val) / 100))
                written.append(key)

        # Safety Buffer: UI sendet %, Bot erwartet Fraktion
        if data.get("safety_buffer") is not None:
            db.set_state("pending_safety_buffer", str(float(data["safety_buffer"]) / 100))
            written.append("safety_buffer")

        db.close()
        _update_conf_args(symbol_safe, data)
        return jsonify({"ok": True, "symbol": symbol, "updated": written})
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

        # Stufe 1: systemctl (für systemd-verwaltete Bots)
        service = f"tradingbot@{symbol_safe}.service"
        try:
            r = subprocess.run(
                ["sudo", "systemctl", "stop", service],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                killed = True
            else:
                tried.append(f"systemctl: {r.stderr.strip() or 'kein Service'}")
        except Exception as e:
            tried.append(f"systemctl: {e}")

        # Stufe 2: PID-Datei (für via Web-UI gestartete Bots)
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
                # Stale PID – Datei löschen, weiter zu Stufe 3
                try:
                    os.remove(pid_file)
                except OSError:
                    pass
                tried.append("PID-Datei veraltet")
            except Exception as e:
                tried.append(f"PID-Datei: {e}")

        # Stufe 3: pgrep nach Symbol in Kommandozeile
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

        # Stufe 4: pkill nach Symbol
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


def _find_db(symbol: str) -> str | None:
    """Gibt den DB-Pfad für ein Symbol zurück oder None wenn nicht gefunden."""
    symbol_safe = symbol.replace("/", "_")
    path = os.path.join(DB_DIR, f"{symbol_safe}.db")
    return path if os.path.exists(path) else None


@app.route("/api/bot/pause", methods=["POST"])
def api_bot_pause():
    try:
        data   = request.get_json() or {}
        symbol = (data.get("symbol") or "").upper().replace("-", "/")
        pause  = bool(data.get("pause", True))
        db_path = _find_db(symbol)
        if not db_path:
            return jsonify({"ok": False, "error": f"Keine DB für {symbol}"}), 404
        db = StateDB(db_path)
        db.set_state("paused", "true" if pause else "false")
        db.close()
        return jsonify({"ok": True, "symbol": symbol, "paused": pause})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/peer/strategies")
def api_peer_strategies():
    """
    Gibt optimierte Strategien aller Bots zurück – für Cross-Instance Peer Learning.
    Enthält nur Strategie-Parameter + Scoring (kein Kontostand, keine Orders).
    """
    results = []
    for db_path in sorted(glob(os.path.join(DB_DIR, "*.db"))):
        if os.path.basename(db_path) in ("candles.db", "news.db"):
            continue
        try:
            db    = StateDB(db_path)
            state = db.get_all_state()
            db.close()
            sqn = float(state.get("supervisor_sqn", 0) or 0)
            if sqn <= 0:
                continue  # Noch keine optimierte Strategie vorhanden
            val_raw = state.get("supervisor_val_pnl")
            results.append({
                "symbol":          state.get("symbol", ""),
                "regime":          state.get("supervisor_regime", ""),
                "strategy_name":   state.get("supervisor_strategy_name", ""),
                "fast":            int(state.get("supervisor_fast", 9)),
                "slow":            int(state.get("supervisor_slow", 21)),
                "rsi_buy_max":     float(state.get("supervisor_rsi_buy_max", 65)),
                "rsi_sell_min":    float(state.get("supervisor_rsi_sell_min", 35)),
                "atr_sl_mult":     float(state.get("supervisor_atr_sl_mult", 1.5)),
                "atr_tp_mult":     float(state.get("supervisor_atr_tp_mult", 2.5)),
                "use_trailing_sl": state.get("supervisor_use_trailing_sl", "False") == "True",
                "volume_filter":   state.get("supervisor_volume_filter",   "False") == "True",
                "sqn":             sqn,
                "sim_pnl":         float(state.get("supervisor_sim_pnl", 0) or 0),
                "val_pnl":         float(val_raw) if val_raw else None,
                "num_trades":      int(state.get("supervisor_sim_trades", 0) or 0),
            })
        except Exception:
            continue
    return jsonify(results)


@app.route("/api/check_updates")
def check_updates():
    """Prüft ob auf dem main-Branch neuere Commits vorliegen."""
    try:
        git_safe = ["-c", f"safe.directory={PROJECT_ROOT}"]
        fetch = subprocess.run(
            ["git"] + git_safe + ["fetch", "origin", "main"],
            capture_output=True, timeout=20, cwd=PROJECT_ROOT,
        )
        if fetch.returncode != 0:
            err = fetch.stderr.decode(errors="replace")[:300]
            return jsonify({"ok": False, "error": f"git fetch fehlgeschlagen: {err}"})

        log = subprocess.run(
            ["git"] + git_safe + ["log", "HEAD..origin/main", "--oneline", "--no-decorate"],
            capture_output=True, text=True, timeout=10, cwd=PROJECT_ROOT,
        )
        commits = [c.strip() for c in log.stdout.strip().splitlines() if c.strip()]

        current = subprocess.run(
            ["git"] + git_safe + ["log", "-1", "--format=%h %s", "HEAD"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        ).stdout.strip()

        return jsonify({
            "ok":         True,
            "up_to_date": len(commits) == 0,
            "behind_by":  len(commits),
            "commits":    commits[:15],
            "current":    current,
        })
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git nicht gefunden"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timeout – kein Internetzugang?"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
