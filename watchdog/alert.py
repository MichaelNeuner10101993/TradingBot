"""systemd OnFailure-Hook: Bot-Crash-Telegram-Alert.

Aufruf: alert.py crash <unit-instance>
Beispiel: alert.py crash HYPE_EUR  (von tradingbot-alert@HYPE_EUR.service)

Liest letzte Zeilen aus journalctl -u tradingbot@<unit>.service und schickt
eine Telegram-Nachricht. Idempotent gegen doppelte Trigger via stamp-File.
"""
import os
import sys
import subprocess
import time
import logging
from pathlib import Path

sys.path.insert(0, "/root/bot/watchdog")
from notify import telegram_send

log = logging.getLogger("watchdog.alert")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

STAMP_DIR = Path("/run/tradingbot-watchdog")
DEDUP_WINDOW_SEC = 60


def journal_tail(unit: str, lines: int = 20) -> str:
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            timeout=10,
        ).decode(errors="replace")
        return out.strip()
    except Exception as e:
        return f"(journalctl fehlgeschlagen: {e})"


def systemctl_show(unit: str, prop: str) -> str:
    try:
        out = subprocess.check_output(
            ["systemctl", "show", unit, "--property", prop, "--value"],
            timeout=5,
        ).decode().strip()
        return out
    except Exception:
        return ""


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "crash":
        print("usage: alert.py crash <unit-instance>", file=sys.stderr)
        return 2

    instance = sys.argv[2]  # e.g. HYPE_EUR
    unit = f"tradingbot@{instance}.service"

    STAMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = STAMP_DIR / f"alert_{instance}.stamp"
    now = time.time()
    if stamp.exists() and (now - stamp.stat().st_mtime) < DEDUP_WINDOW_SEC:
        log.info(f"Dedupe: alert für {instance} vor {now - stamp.stat().st_mtime:.0f}s schon raus")
        return 0
    stamp.touch()

    state = systemctl_show(unit, "ActiveState") or "?"
    sub = systemctl_show(unit, "SubState") or "?"
    result = systemctl_show(unit, "Result") or "?"
    exit_code = systemctl_show(unit, "ExecMainStatus") or "?"
    n_restarts = systemctl_show(unit, "NRestarts") or "0"

    tail = journal_tail(unit, 15)
    # Letzte ERROR/FATAL-Zeile suchen
    err_line = ""
    for line in reversed(tail.splitlines()):
        if any(kw in line for kw in ("FATAL", "ERROR", "Traceback", "Exception")):
            err_line = line[:300]
            break

    sym = instance.replace("_", "/")
    text = (
        f"🚨 <b>Bot-Crash: {sym}</b>\n"
        f"Unit: <code>{unit}</code>\n"
        f"State: <b>{state}/{sub}</b>  Result: {result}\n"
        f"Exit: {exit_code}  Restarts: {n_restarts}\n"
    )
    if err_line:
        text += f"\nLetzter Fehler:\n<pre>{err_line}</pre>\n"
    text += f"\nWatchdog: trade_guard pingt in &lt;5min wenn Trade offen."

    ok = telegram_send(text)
    log.info(f"Alert für {instance}: telegram={'ok' if ok else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
