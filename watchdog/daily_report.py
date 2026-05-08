"""Tägliche Trading-Lage 06:00 morgens.

Aggregiert über alle /root/bot/db/*_EUR.db:
- Closed Trades letzte 24h (SL/TP-Verteilung, geschätzter PnL)
- Offene Positionen + Buchwert
- Aktive vs. konfigurierte Bots
- Balance + 24h-Trend (sofern scan_history hergibt)
- Top-3 Coin-Scores des letzten Scans
- Errors letzte 24h

Schickt EINE Telegram-HTML-Nachricht.
"""
import glob
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, "/root/bot/watchdog")
from notify import telegram_send

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("watchdog.daily_report")

DB_DIR = "/root/bot/db"
SCANNER_DB = os.path.join(DB_DIR, "scanner.db")
KRAKEN_FEE = 0.0026

LOOKBACK_H = 24


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_kraken_price(pair_kraken: str) -> float | None:
    url = f"https://api.kraken.com/0/public/Ticker?pair={pair_kraken}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read())
        result = d.get("result", {})
        if not result:
            return None
        first = next(iter(result.values()))
        return float(first["c"][0])
    except Exception as e:
        log.warning(f"Kraken-Ticker {pair_kraken}: {e}")
        return None


def closed_trades_last_24h() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_H)
    out = []
    for db in sorted(glob.glob(os.path.join(DB_DIR, "*_EUR.db"))):
        sym_safe = os.path.basename(db).replace(".db", "")
        if sym_safe.startswith("grid_") or sym_safe == "_EUR":
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            rows = c.execute(
                "SELECT id, symbol, amount, entry_price, sl_price, tp_price, status, "
                "opened_at, closed_at FROM trades "
                "WHERE status IN ('sl_hit','tp_hit','manual_close') AND closed_at IS NOT NULL"
            ).fetchall()
            c.close()
            for r in rows:
                closed_at = parse_iso(r[8])
                if closed_at and closed_at >= cutoff:
                    exit_price = r[4] if r[6] == "sl_hit" else r[5] if r[6] == "tp_hit" else r[3]
                    amount, entry = r[2], r[3]
                    pnl_gross = (exit_price - entry) * amount
                    fees = (entry + exit_price) * amount * KRAKEN_FEE
                    out.append({
                        "sym": sym_safe, "symbol": r[1],
                        "amount": amount, "entry": entry, "exit": exit_price,
                        "status": r[6], "closed_at": closed_at,
                        "pnl_net": pnl_gross - fees,
                    })
        except Exception as e:
            log.warning(f"closed-scan {sym_safe}: {e}")
    return out


def open_positions() -> list[dict]:
    out = []
    for db in sorted(glob.glob(os.path.join(DB_DIR, "*_EUR.db"))):
        sym_safe = os.path.basename(db).replace(".db", "")
        if sym_safe.startswith("grid_") or sym_safe == "_EUR":
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            rows = c.execute(
                "SELECT id, symbol, amount, entry_price, sl_price, tp_price, status, opened_at "
                "FROM trades WHERE status IN ('open','tp_partial_closed')"
            ).fetchall()
            c.close()
            for r in rows:
                out.append({
                    "sym": sym_safe, "symbol": r[1],
                    "id": r[0], "amount": r[2], "entry": r[3],
                    "sl": r[4], "tp": r[5], "status": r[6], "opened_at": r[7],
                })
        except Exception as e:
            log.warning(f"open-scan {sym_safe}: {e}")
    return out


def errors_last_24h() -> list[tuple[str, str, str]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_H)
    out = []
    for db in sorted(glob.glob(os.path.join(DB_DIR, "*_EUR.db"))):
        sym_safe = os.path.basename(db).replace(".db", "")
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            rows = c.execute(
                "SELECT context, message, occurred_at FROM errors ORDER BY id DESC LIMIT 10"
            ).fetchall()
            c.close()
            for r in rows:
                ts = parse_iso(r[2])
                if ts and ts >= cutoff:
                    out.append((sym_safe, r[0], (r[1] or "")[:160]))
        except Exception:
            pass
    return out


def scan_summary() -> dict:
    info = {"balance_now": None, "balance_24h_ago": None, "active_bots": 0,
            "top_scores": [], "last_scan_age_min": None}
    try:
        c = sqlite3.connect(f"file:{SCANNER_DB}?mode=ro", uri=True, timeout=5)
        # latest
        r = c.execute(
            "SELECT ts, balance_eur, active_bots, top_scores FROM scan_history "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if r:
            info["balance_now"] = r[1]
            info["active_bots"] = r[2]
            info["last_scan_age_min"] = (time.time() - r[0]) / 60
            try:
                ts_list = json.loads(r[3] or "[]")
                info["top_scores"] = ts_list[:3]
            except Exception:
                pass
        # ~24h ago
        cutoff_ts = int(time.time() - LOOKBACK_H * 3600)
        r2 = c.execute(
            "SELECT balance_eur FROM scan_history WHERE ts <= ? AND balance_eur > 0 "
            "ORDER BY ts DESC LIMIT 1", (cutoff_ts,)
        ).fetchone()
        if r2:
            info["balance_24h_ago"] = r2[0]
        c.close()
    except Exception as e:
        log.warning(f"scan_history: {e}")
    return info


def list_running_bots() -> list[str]:
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "tradingbot@*", "--state=active",
             "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=10,
        )
        out = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if parts and parts[0].startswith("tradingbot@"):
                out.append(parts[0].replace("tradingbot@", "").replace(".service", ""))
        return out
    except Exception:
        return []


def fmt_eur(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:+.2f}€" if x >= 0 else f"{x:.2f}€"


def main() -> int:
    closed = closed_trades_last_24h()
    opens = open_positions()
    errs = errors_last_24h()
    scan = scan_summary()
    running = list_running_bots()

    # PnL Aggregation
    sl_count = sum(1 for t in closed if t["status"] == "sl_hit")
    tp_count = sum(1 for t in closed if t["status"] == "tp_hit")
    total_pnl = sum(t["pnl_net"] for t in closed)

    # Balance-Delta
    bal_delta = None
    if scan["balance_now"] is not None and scan["balance_24h_ago"] is not None:
        bal_delta = scan["balance_now"] - scan["balance_24h_ago"]

    # Open positions: aktuellen Wert holen
    open_value_eur = 0.0
    open_unrealized = 0.0
    open_lines = []
    for p in opens:
        # HYPE_EUR → HYPEEUR für Kraken-Ticker
        kraken_pair = p["sym"].replace("_", "")
        cur = fetch_kraken_price(kraken_pair)
        if cur is None:
            open_lines.append(
                f"• <b>{p['symbol']}</b>: {p['amount']:.6g} @ {p['entry']:.4f} "
                f"(<i>{p['status']}</i>, kein Live-Preis)"
            )
            continue
        cost = p["amount"] * p["entry"]
        value = p["amount"] * cur
        unrealized_gross = value - cost
        unrealized_net = unrealized_gross - (cost + value) * KRAKEN_FEE
        pct = (cur / p["entry"] - 1) * 100
        open_value_eur += value
        open_unrealized += unrealized_net
        age_d = (datetime.now(timezone.utc) - parse_iso(p["opened_at"])).days if p["opened_at"] else 0
        open_lines.append(
            f"• <b>{p['symbol']}</b>: {p['amount']:.6g} @ {p['entry']:.4f}€ → "
            f"{cur:.4f}€ ({pct:+.2f}%, {fmt_eur(unrealized_net)})  "
            f"<i>{p['status']}, {age_d}d offen</i>"
        )

    # Build message
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sl_quote = (sl_count / max(1, sl_count + tp_count)) * 100 if (sl_count + tp_count) else 0

    text = f"☀️ <b>Trading-Lage {now}</b>\n\n"

    text += f"<b>Bots:</b> {len(running)} aktiv von 26 konfiguriert\n"
    if running:
        text += f"  → {', '.join(sorted(running))}\n"
    text += f"<b>Scanner:</b> letzter Scan vor {scan['last_scan_age_min']:.0f}min, "
    text += f"Balance {scan['balance_now']:.2f}€" if scan['balance_now'] is not None else "Balance ?"
    if bal_delta is not None:
        text += f" ({fmt_eur(bal_delta)} 24h)"
    text += "\n\n"

    text += f"<b>Trades 24h:</b> {len(closed)} closed "
    text += f"(SL: {sl_count}, TP: {tp_count}, Quote {sl_quote:.0f}% SL)\n"
    text += f"  Realisiert: <b>{fmt_eur(total_pnl)}</b>\n\n"

    text += f"<b>Offene Positionen ({len(opens)}):</b>\n"
    if opens:
        text += "\n".join(open_lines) + "\n"
        text += f"  Buchwert: {open_value_eur:.2f}€  Unrealized: <b>{fmt_eur(open_unrealized)}</b>\n"
    else:
        text += "  (keine)\n"
    text += "\n"

    if scan["top_scores"]:
        tops = scan["top_scores"]
        text += "<b>Top-Scores (letzter Scan):</b>\n"
        for s in tops:
            run_marker = " ▶" if s.get("running") else ""
            text += f"  {s['symbol']} {s['score']}pt {s.get('regime','?')}{run_marker}\n"
        text += "\n"

    if errs:
        text += f"<b>Errors 24h:</b> {len(errs)}\n"
        for sym, ctx, msg in errs[:5]:
            text += f"  • <code>{sym}</code> {ctx}: {msg[:80]}\n"
        if len(errs) > 5:
            text += f"  … +{len(errs)-5} weitere\n"
    else:
        text += "<b>Errors 24h:</b> keine ✅\n"

    ok = telegram_send(text)
    log.info(f"daily report sent: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
