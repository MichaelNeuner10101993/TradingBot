"""
Risk & Position Sizing.
Berechnet Ordergrößen dynamisch basierend auf:
  - Aktuelle Gesamtbalance
  - Anzahl wirklich laufender Bot-Instanzen mit gleicher Quote-Currency (Pool)
  - Sicherheitspuffer (safety_buffer_pct)

Kapital-Pools: EUR-Bots und USDT-Bots konkurrieren NICHT um dasselbe Kapital.
_count_active_bots() zählt nur tatsächlich laufende Prozesse:
  1. PID-Dateien in run/*.pid (web-API-gestartete Bots)
  2. Aktive systemd tradingbot@*.service Units
  Grid-Bots (grid_*.pid) werden nicht gezählt (eigenes Budget via --amount).
"""
import logging
import os
import subprocess
from glob import glob
from bot.config import RiskConfig

log = logging.getLogger("tradingbot.risk")


def _quote_from_name(name: str) -> str:
    """BTC_EUR → EUR | BTC_USDT → USDT"""
    parts = name.split("_")
    return parts[-1].upper() if len(parts) >= 2 else "EUR"


def _pid_is_alive(pid_file: str) -> bool:
    """Prüft ob PID-Datei existiert und der Prozess noch läuft."""
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _count_active_bots(db_dir: str, quote_currency: str = "") -> int:
    """
    Zählt tatsächlich laufende Trend-Bot-Instanzen im gleichen Quote-Currency-Pool.

    Quellen (vereinigt, keine Doppelzählung):
      1. run/*.pid  — web-API-gestartete Bots mit lebendem Prozess
      2. systemd tradingbot@*.service — systemd-verwaltete Bots (active)

    Grid-Bots (grid_*) werden ausgeschlossen — die verwalten ihr Budget selbst.
    """
    run_dir = os.path.join(os.path.dirname(db_dir), "run")
    running: set[str] = set()

    # 1. PID-Dateien
    for pid_file in glob(os.path.join(run_dir, "*.pid")):
        name = os.path.basename(pid_file)[:-4]          # z.B. BTC_EUR
        if name.startswith("grid_"):
            continue
        if _pid_is_alive(pid_file):
            running.add(name)

    # 2. Systemd-Units
    try:
        out = subprocess.check_output(
            ["systemctl", "list-units", "tradingbot@*.service",
             "--state=active", "--no-legend", "--plain"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        for line in out.splitlines():
            # "tradingbot@BTC_EUR.service  loaded active running ..."
            if "tradingbot@" not in line:
                continue
            name = line.split("tradingbot@")[1].split(".service")[0]
            running.add(name)
    except Exception:
        pass

    # Nach Quote-Currency filtern
    if quote_currency:
        q = quote_currency.upper()
        running = {n for n in running if _quote_from_name(n) == q}

    count = max(len(running), 1)
    log.debug(f"Laufende Bots im {quote_currency or 'gesamt'}-Pool: {count} {sorted(running)}")
    return count


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def check_guardrails(self, open_orders: list, balance: dict) -> tuple[bool, str]:
        if len(open_orders) >= self.cfg.max_open_orders:
            return False, f"Zu viele offene Orders ({len(open_orders)}/{self.cfg.max_open_orders})"

        quote_free     = balance.get("quote", 0.0)
        quote_currency = balance.get("quote_currency", "")
        num_bots       = _count_active_bots(self.cfg.db_dir, quote_currency)
        usable         = quote_free * (1 - self.cfg.safety_buffer_pct)
        per_bot        = usable / num_bots
        trade_quote    = per_bot * self.cfg.quote_risk_fraction

        if trade_quote < self.cfg.min_order_quote:
            return False, (
                f"Trade-Größe zu klein: {trade_quote:.2f} {quote_currency} "
                f"(Balance={quote_free:.2f}, Pool={num_bots} {quote_currency}-Bots, "
                f"Puffer={self.cfg.safety_buffer_pct*100:.0f}%)"
            )

        return True, "ok"

    def calc_buy_amount(self, balance: dict, last_price: float, exchange) -> float:
        quote_free     = balance.get("quote", 0.0)
        quote_currency = balance.get("quote_currency", "")
        num_bots       = _count_active_bots(self.cfg.db_dir, quote_currency)

        usable      = quote_free * (1 - self.cfg.safety_buffer_pct)
        per_bot     = usable / num_bots
        trade_quote = per_bot * self.cfg.quote_risk_fraction

        log.info(
            f"Sizing: Balance={quote_free:.2f} {quote_currency} | "
            f"Puffer={self.cfg.safety_buffer_pct*100:.0f}% | "
            f"Pool={num_bots} {quote_currency}-Bots | "
            f"pro Bot={per_bot:.2f} | Trade={trade_quote:.2f} {quote_currency}"
        )
        return trade_quote / last_price

    def calc_sell_amount(self, balance: dict) -> float:
        return balance.get("base", 0.0)
