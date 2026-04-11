"""
Telegram-Benachrichtigungen fuer den Trend-Scanner.
Eigene Datei um notify.py (Binary-Corruption) zu umgehen.
"""
import asyncio
import logging
import os

log = logging.getLogger("scanner")


def _send(text: str) -> bool:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        from telegram import Bot
        async def _do():
            async with Bot(token) as bot:
                await asyncio.wait_for(
                    bot.send_message(chat_id, text, parse_mode="HTML"),
                    timeout=5.0,
                )
        asyncio.run(_do())
        return True
    except Exception as e:
        log.warning(f"Telegram-Notify fehlgeschlagen: {e}")
        return False


def send_scanner_started(symbol: str, score: int, regime: str, adx: float,
                          rsi, reason: str, active_bots: int, max_bots: int,
                          balance_eur: float, dry_run: bool = False):
    """Meldung wenn Scanner einen neuen Bot startet."""
    dry_tag = " <i>[DRY-RUN]</i>" if dry_run else ""
    rsi_str = f"{rsi:.1f}" if rsi is not None else "–"
    nl = chr(10)
    msg = (
        f"🚀 <b>Scanner: {symbol} gestartet</b>{dry_tag}" + nl
        + f"Score: <b>{score}</b> | Regime: <b>{regime}</b>" + nl
        + f"ADX={adx:.1f} | RSI={rsi_str}" + nl
        + f"Grund: {reason}" + nl
        + f"Bots aktiv: {active_bots + 1}/{max_bots} | Balance: {balance_eur:.2f}€"
    )
    _send(msg)


def send_scanner_stopped(symbol: str, reason: str, regime: str,
                          consecutive_sl: int, dry_run: bool = False):
    """Meldung wenn Scanner einen Bot stoppt."""
    dry_tag = " <i>[DRY-RUN]</i>" if dry_run else ""
    nl = chr(10)
    msg = (
        f"⛔ <b>Scanner: {symbol} gestoppt</b>{dry_tag}" + nl
        + f"Grund: {reason}" + nl
        + f"Regime: {regime} | SL-Serie: {consecutive_sl}×"
    )
    _send(msg)


def send_daily_summary(
    balance_eur: float,
    pnl_24h_eur: float,
    active_bots: list,
    top_candidates: list,
) -> None:
    """Taegliche Zusammenfassung um 08:00 UTC."""
    nl        = chr(10)
    pnl_sign  = "+" if pnl_24h_eur >= 0 else ""
    bots_lines = [
        f"  • {b['symbol']} ({b.get('regime', '?')})" for b in active_bots
    ]
    bots_str  = nl.join(bots_lines) or "  – keine aktiven Bots –"
    top_lines = [
        f"  {i+1}. {c['symbol']} | Score={c['score']} | {c['regime']}"
        for i, c in enumerate(top_candidates[:3])
    ]
    top_str   = nl.join(top_lines) or "  – keine Kandidaten –"
    pnl_str   = f"{pnl_sign}{pnl_24h_eur:.2f}€"
    msg = (
        f"📊 <b>Tägliche Scanner-Zusammenfassung</b>" + nl
        + f"Balance: <b>{balance_eur:.2f}€</b> | P&amp;L 24h: <b>{pnl_str}</b>" + nl
        + nl + f"<b>Aktive Bots ({len(active_bots)}):</b>" + nl + bots_str + nl
        + nl + f"<b>Top-3 Kandidaten:</b>" + nl + top_str
    )
    _send(msg)
