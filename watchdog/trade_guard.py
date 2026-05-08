"""Open-Trade-Watchdog mit auto-restart.

Läuft alle 5 min (systemd-Timer). Scannt alle /root/bot/db/*_EUR.db nach
Trades mit Status 'open' oder 'tp_partial_closed'. Wenn ein offener Trade
existiert UND der zugehörige tradingbot@<sym>.service nicht active ist:

  1. Telegram-Alert
  2. Auto-Restart via `systemctl start tradingbot@<sym>.service`
  3. Verify nach 90s — wenn immer noch nicht active → eskalieren

Idempotenz: stamp-File pro Symbol verhindert Spam (max 1 Alert/Stunde).
"""
import glob
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/root/bot/watchdog")
from notify import telegram_send

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("watchdog.trade_guard")

DB_DIR = "/root/bot/db"
STAMP_DIR = Path("/var/lib/tradingbot-watchdog")
ALERT_COOLDOWN_SEC = 3600  # 1h zwischen Alerts pro Symbol
OPEN_STATUSES = ("open", "tp_partial_closed")


def is_unit_active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def find_open_trades() -> list[dict]:
    """Returns [{symbol, db, trade_id, status, opened_at, amount, entry_price}, ...]"""
    found = []
    for db in sorted(glob.glob(os.path.join(DB_DIR, "*_EUR.db"))):
        sym_safe = os.path.basename(db).replace(".db", "")
        if sym_safe.startswith("grid_") or sym_safe == "_EUR":
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            placeholders = ",".join(["?"] * len(OPEN_STATUSES))
            rows = c.execute(
                f"SELECT id, symbol, amount, entry_price, sl_price, tp_price, status, opened_at "
                f"FROM trades WHERE status IN ({placeholders})",
                OPEN_STATUSES,
            ).fetchall()
            c.close()
            for r in rows:
                found.append({
                    "sym_safe": sym_safe,
                    "db": db,
                    "id": r[0], "symbol": r[1], "amount": r[2], "entry_price": r[3],
                    "sl_price": r[4], "tp_price": r[5], "status": r[6], "opened_at": r[7],
                })
        except Exception as e:
            log.warning(f"DB-Scan {sym_safe} fehlgeschlagen: {e}")
    return found


def cooldown_passed(stamp: Path) -> bool:
    if not stamp.exists():
        return True
    return (time.time() - stamp.stat().st_mtime) > ALERT_COOLDOWN_SEC


def restart_bot(sym_safe: str) -> tuple[bool, str]:
    unit = f"tradingbot@{sym_safe}.service"
    try:
        r = subprocess.run(
            ["systemctl", "start", unit],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return False, f"start rc={r.returncode} stderr={r.stderr[:200]}"
        time.sleep(90)  # Bot hat startup-delay 60s + boot-time
        return is_unit_active(unit), "started"
    except Exception as e:
        return False, str(e)


def main() -> int:
    STAMP_DIR.mkdir(parents=True, exist_ok=True)

    open_trades = find_open_trades()
    if not open_trades:
        log.info("Keine offenen Trades — alles gut.")
        return 0

    issues = []
    for t in open_trades:
        unit = f"tradingbot@{t['sym_safe']}.service"
        active = is_unit_active(unit)
        log.info(f"{t['symbol']} status={t['status']} unit_active={active} opened={t['opened_at']}")
        if not active:
            issues.append(t)

    if not issues:
        log.info(f"{len(open_trades)} offene Trade(s), alle Bots laufen.")
        return 0

    log.warning(f"{len(issues)} verwaiste Trade(s) gefunden!")
    for t in issues:
        stamp = STAMP_DIR / f"orphan_{t['sym_safe']}.stamp"
        send_alert = cooldown_passed(stamp)

        ok, info = restart_bot(t["sym_safe"])
        log.info(f"restart {t['sym_safe']}: ok={ok} info={info}")

        if send_alert:
            stamp.touch()
            status_emoji = "✅" if ok else "❌"
            text = (
                f"⚠️ <b>Verwaister Trade entdeckt: {t['symbol']}</b>\n"
                f"Bot war gestoppt, aber Position offen seit {t['opened_at']}.\n"
                f"Trade-ID {t['id']}: {t['amount']:.6g} @ {t['entry_price']} EUR  "
                f"SL={t['sl_price']} TP={t['tp_price']}\n"
                f"Auto-Restart: {status_emoji} {info}\n"
            )
            if not ok:
                text += f"\n🚨 <b>RESTART FEHLGESCHLAGEN — manueller Eingriff nötig!</b>"
            telegram_send(text)

    return 0 if all(issues) else 1


if __name__ == "__main__":
    sys.exit(main())
