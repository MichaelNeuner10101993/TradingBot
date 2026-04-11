"""
Telegram-Benachrichtigungen für den Trend-Scanner.
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
    _send(
        f"🚀 <b>Scanner: {symbol} gestartet</b>{dry_tag}\n"
        f"Score: <b>{score}</b> | Regime: <b>{regime}</b>\n"
        f"ADX={adx:.1f} | RSI={rsi_str}\n"
        f"Grund: {reason}\n"
        f"Bots aktiv: {active_bots + 1}/{max_bots} | Balance: {balance_eur:.2f}€"
    )


def send_scanner_stopped(symbol: str, reason: str, regime: str,
                          consecutive_sl: int, dry_run: bool = False):
    """Meldung wenn Scanner einen Bot stoppt."""
    dry_tag = " <i>[DRY-RUN]</i>" if dry_run else ""
    _send(
        f"⛔ <b>Scanner: {symbol} gestoppt</b>{dry_tag}\n"
        f"Grund: {reason}\n"
        f"Regime: {regime} | SL-Serie: {consecutive_sl}×"
    )
