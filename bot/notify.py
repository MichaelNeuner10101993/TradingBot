"""
Leichtgewichtiger Telegram-Notifier für Trade-Events.

Sendet synchron via asyncio.run() – blockiert max. 5s.
Wirft nie Exceptions (kein Trade-Ausfall durch Telegram-Fehler).
Liest Credentials aus Umgebungsvariablen (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
"""
import asyncio
import logging
import os

log = logging.getLogger("tradingbot.notify")

KRAKEN_FEE = 0.0026  # 0.26% pro Order


def _creds() -> tuple[str, str]:
    return os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")


def _fmt(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.6f}"
    return f"{price:.8f}"


def _send_sync(text: str) -> bool:
    """Sendet eine Telegram-Nachricht synchron. Gibt True bei Erfolg zurück."""
    token, chat_id = _creds()
    if not token or not chat_id:
        log.debug("Telegram-Credentials fehlen – Notifier übersprungen")
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


def send_trade_buy(
    symbol: str,
    amount: float,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    dry_run: bool = False,
):
    """Benachrichtigung nach einem Kauf."""
    base = symbol.split("/")[0]
    sl_pct  = (entry_price - sl_price) / entry_price * 100
    tp_pct  = (tp_price - entry_price) / entry_price * 100
    dry_tag = " <i>[DRY]</i>" if dry_run else ""
    text = (
        f"🟢 <b>KAUF {symbol}</b>{dry_tag}\n"
        f"{amount:.6g} {base} @ <b>{_fmt(entry_price)} EUR</b>\n"
        f"SL: {_fmt(sl_price)} <i>(-{sl_pct:.1f}%)</i>  "
        f"TP: {_fmt(tp_price)} <i>(+{tp_pct:.1f}%)</i>"
    )
    _send_sync(text)


def send_trade_sell(
    symbol: str,
    amount: float,
    exit_price: float,
    reason: str,
    pnl_eur: float | None = None,
    dry_run: bool = False,
):
    """Benachrichtigung nach einem Verkauf."""
    base = symbol.split("/")[0]
    dry_tag = " <i>[DRY]</i>" if dry_run else ""

    if reason == "tp_hit":
        emoji, label = "💰", "TAKE-PROFIT"
    elif reason == "sl_hit":
        emoji, label = "🛑", "STOP-LOSS"
    else:
        emoji, label = "📉", "VERKAUF"

    pnl_line = ""
    if pnl_eur is not None:
        fee = exit_price * amount * KRAKEN_FEE * 2
        net = pnl_eur - fee
        sign = "+" if net >= 0 else ""
        pnl_line = f"\nP&L: <b>{sign}{net:.2f} EUR</b>"

    text = (
        f"{emoji} <b>{label} {symbol}</b>{dry_tag}\n"
        f"{amount:.6g} {base} @ <b>{_fmt(exit_price)} EUR</b>"
        f"{pnl_line}"
    )
    _send_sync(text)


def send_pyramid_buy(
    symbol: str,
    amount: float,
    price: float,
    new_entry: float,
    dry_run: bool = False,
):
    """Benachrichtigung nach einem Pyramid-Nachkauf."""
    base = symbol.split("/")[0]
    dry_tag = " <i>[DRY]</i>" if dry_run else ""
    text = (
        f"🔺 <b>NACHKAUF {symbol}</b>{dry_tag}\n"
        f"+{amount:.6g} {base} @ {_fmt(price)} EUR\n"
        f"Avg-Entry: <b>{_fmt(new_entry)} EUR</b>"
    )
    _send_sync(text)
