"""
Leichtgewichtiger Telegram-Notifier fuer Trade-Events.

Sendet synchron via asyncio.run() - blockiert max. 5s.
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


def _send_sync(text: str, reply_markup=None) -> bool:
    """Sendet eine Telegram-Nachricht synchron. Gibt True bei Erfolg zurueck."""
    token, chat_id = _creds()
    if not token or not chat_id:
        log.debug("Telegram-Credentials fehlen - Notifier uebersprungen")
        return False
    try:
        from telegram import Bot

        async def _do():
            async with Bot(token) as bot:
                await asyncio.wait_for(
                    bot.send_message(chat_id, text, parse_mode="HTML",
                                     reply_markup=reply_markup),
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
        pnl_line = f"\nP&amp;L: <b>{sign}{net:.2f} EUR</b>"

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


def send_drawdown_alert(symbol: str, drawdown_pct: float, is_stop: bool):
    """Drawdown-Warnung (>=10%) oder -Stopp (>=15%) per Telegram."""
    if is_stop:
        text = (
            f"🚨 <b>DRAWDOWN-STOPP: {symbol}</b>\n"
            f"Portfolio-Drawdown: <b>{drawdown_pct * 100:.1f}%</b> >= 15%\n"
            f"Handel automatisch pausiert.\n"
            f"Fortsetzen via Dashboard oder /start_bot {symbol.replace('/', '_')}"
        )
    else:
        text = (
            f"⚠️ <b>DRAWDOWN-WARNUNG: {symbol}</b>\n"
            f"Portfolio-Drawdown: <b>{drawdown_pct * 100:.1f}%</b> >= 10%\n"
            f"Position-Sizing auf 50% reduziert bis zur Erholung."
        )
    _send_sync(text)


def send_supervisor_recommendation(
    symbol: str,
    best: dict,
    cur_trailing: bool,
    cur_vol: bool,
):
    """Veraltet - wird nicht mehr genutzt (auto-apply ersetzt manuelles Übernehmen)."""
    pass


def send_supervisor_auto_applied(
    symbol: str,
    best: dict,
    prev_trailing: bool,
    prev_vol: bool,
    new_trailing: bool,
    new_vol: bool,
):
    """Benachrichtigung: Supervisor hat Empfehlung automatisch angewendet + Rükgängig-Button."""
    import json
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def _fmt_flag(val: bool) -> str:
        return "✅ an" if val else "❌ aus"

    val_str  = f"  val={best['val_pnl']:+.2f}%" if best.get("val_pnl") is not None else ""
    changes  = []
    if new_trailing != prev_trailing:
        changes.append(f"Trailing-SL: {_fmt_flag(prev_trailing)} → <b>{_fmt_flag(new_trailing)}</b>")
    if new_vol != prev_vol:
        changes.append(f"Volume-Filter: {_fmt_flag(prev_vol)} → <b>{_fmt_flag(new_vol)}</b>")

    text = (
        f"🤖 <b>Supervisor auto-angewendet: {symbol}</b>\n"
        f"Strategie: <b>{best['name']} {best['fast']}/{best['slow']}</b>\n"
        + "\n".join(changes) + "\n"
        f"Sim-P&amp;L: <b>{best['pnl_pct']:+.2f}%</b>{val_str}  SQN={best.get('sqn', 0):.2f}\n"
        f"<i>Wirkt beim nächsten Loop (~60s)</i>"
    )
    revert_payload = json.dumps({
        "a": "rv",
        "s": symbol,
        "t": int(prev_trailing),
        "v": int(prev_vol),
    }, separators=(",", ":"))
    # Sicherheits-Check: Telegram-Limit = 64 Bytes
    if len(revert_payload.encode()) > 64:
        revert_payload = None
    keyboard = (
        InlineKeyboardMarkup([[InlineKeyboardButton("↩ Rükgängig", callback_data=revert_payload)]])
        if revert_payload else None
    )
    _send_sync(text, reply_markup=keyboard)


def send_peer_strategy(
    symbol: str,
    peer_strat: dict,
    local_pnl: float,
):
    """Benachrichtigung wenn Peer-Learning eine bessere Strategie uebernimmt."""
    val_str = f"  val={peer_strat['val_pnl']:+.2f}%" if peer_strat.get("val_pnl") is not None else ""
    text = (
        f"🔗 <b>Peer-Strategie uebernommen: {symbol}</b>\n"
        f"Strategie: <b>{peer_strat['name']} {peer_strat['fast']}/{peer_strat['slow']}</b>\n"
        f"Peer P&amp;L: {peer_strat['sim_pnl']:+.2f}%{val_str}  SQN={peer_strat['sqn']:.2f}\n"
        f"Lokal getestet: <b>{local_pnl:+.2f}%</b>"
    )
    _send_sync(text)


def send_strategy_learned(
    symbol: str,
    best: dict,
    regime: str,
    prev_regime: str,
    sqn_delta: float,
):
    """Proaktive Nachricht wenn Supervisor eine deutlich bessere Strategie findet."""
    regime_changed = bool(prev_regime) and prev_regime != regime
    icon      = "🆕" if regime_changed else "📈"
    regime_str = f"{prev_regime} → {regime}" if regime_changed else regime
    val_str    = f"  val={best['val_pnl']:+.2f}%" if best.get("val_pnl") is not None else ""
    rsi_str    = f"RSI<={best['rsi_buy_max']:.0f}/{best['rsi_sell_min']:.0f}" if "rsi_buy_max" in best else ""
    text = (
        f"{icon} <b>Gelernte Strategie: {symbol}</b>\n"
        f"Regime: {regime_str}\n"
        f"Strategie: <b>{best['name']} {best['fast']}/{best['slow']}</b>  "
        f"{'↑SL ' if best.get('use_trailing_sl') else ''}{'Vol ' if best.get('volume_filter') else ''}\n"
        f"Sim-P&amp;L: <b>{best['pnl_pct']:+.2f}%</b>{val_str}  SQN={best.get('sqn', 0):.2f}\n"
        f"Delta SQN: {sqn_delta:+.2f}  {rsi_str}"
    )
    _send_sync(text)
