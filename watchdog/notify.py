"""Standalone Telegram-Notifier für Watchdog-Skripte.

Liest TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID aus /root/bot/.env.
Synchron, kein python-telegram-bot Dep, nur requests.
"""
import os
import sys
import logging

import requests

ENV_FILE = "/root/bot/.env"
log = logging.getLogger("watchdog.notify")


def _load_env() -> dict:
    env = {}
    if not os.path.exists(ENV_FILE):
        return env
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def telegram_send(text: str, parse_mode: str = "HTML", silent: bool = False) -> bool:
    env = _load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("TELEGRAM creds fehlen — Watchdog-Alert übersprungen")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        log.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        log.warning(f"Telegram-Send fehlgeschlagen: {e}")
        return False


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) or "Watchdog-Test"
    ok = telegram_send(msg)
    print("OK" if ok else "FAIL")
    sys.exit(0 if ok else 1)
