"""Dashboard- und Scanner-Liveness-Check.

Läuft alle 15 min (systemd-Timer). Prüft:

1. Web API antwortet auf /api/bots?active_only=false
2. Stale state: bot.state.status='running' aber process_running=False
   → state-File aufräumen (set status='stopped') + Telegram (1x pro Vorfall)
3. scanner.db scan_history: letzte 2 Einträge mit notes='api_unavailable'
   → Telegram (Scanner-Fehlfunktion)
4. Web/Scanner/Supervisor systemd-Units active

Cooldown via stamp-Files, max 1 Alert/Stunde pro Issue-Typ.
"""
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/root/bot/watchdog")
from notify import telegram_send

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("watchdog.api_health")

API_URL = "http://localhost:5001/api/bots?active_only=false"
SCANNER_DB = "/root/bot/db/scanner.db"
STAMP_DIR = Path("/var/lib/tradingbot-watchdog")
COOLDOWN_SEC = 3600

CORE_UNITS = (
    "tradingbot-web.service",
    "tradingbot-scanner.service",
    "tradingbot-supervisor.service",
)


def cooldown_passed(name: str) -> bool:
    stamp = STAMP_DIR / f"health_{name}.stamp"
    if not stamp.exists():
        return True
    return (time.time() - stamp.stat().st_mtime) > COOLDOWN_SEC


def mark(name: str):
    (STAMP_DIR / f"health_{name}.stamp").touch()


def is_active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def check_core_units() -> list[str]:
    issues = []
    for u in CORE_UNITS:
        if not is_active(u):
            issues.append(u)
    return issues


def fetch_api() -> list | None:
    try:
        with urllib.request.urlopen(API_URL, timeout=8) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning(f"API-Fetch fehlgeschlagen: {e}")
        return None


def find_stale_state(bots: list) -> list[dict]:
    """status='running' aber process_running=False → veraltete state.json"""
    return [b for b in bots if b.get("status") == "running" and not b.get("process_running")]


def scanner_recent_unavailable() -> int:
    """Wieviele der letzten 3 Scans haben notes='api_unavailable'?"""
    try:
        c = sqlite3.connect(f"file:{SCANNER_DB}?mode=ro", uri=True, timeout=5)
        rows = c.execute(
            "SELECT notes FROM scan_history ORDER BY rowid DESC LIMIT 3"
        ).fetchall()
        c.close()
        return sum(1 for (n,) in rows if n == "api_unavailable")
    except Exception as e:
        log.warning(f"scanner.db read failed: {e}")
        return 0


def main() -> int:
    STAMP_DIR.mkdir(parents=True, exist_ok=True)

    # Check 1: Core systemd units
    dead_units = check_core_units()
    if dead_units and cooldown_passed("dead_units"):
        mark("dead_units")
        text = (
            f"🚨 <b>Trading-Infrastruktur down</b>\n"
            f"Inaktive Core-Units:\n"
            + "\n".join(f"• <code>{u}</code>" for u in dead_units)
        )
        telegram_send(text)

    # Check 2: API erreichbar
    bots = fetch_api()
    if bots is None:
        if cooldown_passed("api_unreachable"):
            mark("api_unreachable")
            telegram_send(
                f"🚨 <b>Web-Dashboard API nicht erreichbar</b>\n"
                f"<code>{API_URL}</code> antwortet nicht."
            )
        return 1

    log.info(f"API ok: {len(bots)} Configs, "
             f"{sum(1 for b in bots if b.get('process_running'))} laufen")

    # Check 3: Stale state
    stale = find_stale_state(bots)
    if stale and cooldown_passed("stale_state"):
        mark("stale_state")
        names = ", ".join(b["symbol"] for b in stale)
        text = (
            f"⚠️ <b>Stale state-Files:</b> {len(stale)} Bot(s)\n"
            f"<code>status='running'</code> aber Prozess tot: {names}\n"
            f"<i>(Hinweis: Crash ohne Cleanup. trade_guard hätte schon restart probiert.)</i>"
        )
        telegram_send(text)

    # Check 4: Scanner liefert wiederholt api_unavailable
    n_unavail = scanner_recent_unavailable()
    if n_unavail >= 2 and cooldown_passed("scanner_blocked"):
        mark("scanner_blocked")
        telegram_send(
            f"⚠️ <b>Scanner blockiert</b>\n"
            f"Letzte {n_unavail}/3 Scans mit <code>notes=api_unavailable</code>.\n"
            f"Bug-Reprise des active_only-Issues? Logs prüfen."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
