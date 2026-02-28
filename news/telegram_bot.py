"""
Telegram Bot für News-Alerts mit Inline-Buttons und vollständiger Bot-Steuerung.

Sicherheit: Alle eingehenden Nachrichten/Callbacks werden gegen TELEGRAM_CHAT_ID geprüft.
Fremde User erhalten keine Antwort.

Befehle:
  /start          – Begrüßung + Befehlsübersicht
  /status         – Status aller Bots (Regime, Signal, P&L, Balance)
  /bots           – Alias für /status
  /portfolio      – Offene Positionen aller Bots (Entry, Preis, P&L, SL/TP)
  /start_bot XXX  – Bot für Symbol starten (z.B. /start_bot BTC/EUR)
  /stop_bot XXX   – Bot für Symbol stoppen
  /stop_all       – Alle laufenden Bots stoppen
  /help           – Befehlsübersicht

Architektur:
- send_alert / send_test_message: asyncio.run() mit eigenem Bot-Kontext (fire-and-forget)
- run_polling(): läuft in Daemon-Thread, empfängt Inline-Button-Callbacks + Commands
"""
import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta

import requests
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

from news.config import NewsAgentConfig
from news.fetcher import NewsItem

logger = logging.getLogger(__name__)

LABEL_EMOJI = {
    "bearish": "🔴",
    "bullish": "🟢",
    "neutral": "⚪",
}


class TelegramNewsBot:
    def __init__(self, cfg: NewsAgentConfig, db_conn: sqlite3.Connection):
        self.cfg = cfg
        self.db = db_conn
        self._app: Application | None = None

    # ------------------------------------------------------------------
    # Auth-Guard – nur eigene Chat-ID darf den Bot bedienen
    # ------------------------------------------------------------------

    def _is_authorized(self, update: Update) -> bool:
        """Gibt True zurück wenn der Absender die konfigurierte Chat-ID ist."""
        if not self.cfg.telegram_chat_id:
            return False
        chat_id = str(
            update.effective_chat.id if update.effective_chat else
            update.effective_user.id if update.effective_user else ""
        )
        return chat_id == str(self.cfg.telegram_chat_id)

    async def _unauthorized(self, update: Update):
        """Stille Ablehnung – kein Log, kein Reply (verhindert Bot-Enumeration)."""
        logger.warning(
            "Unautorisierter Zugriff von chat_id=%s user=%s",
            update.effective_chat.id if update.effective_chat else "?",
            update.effective_user.username if update.effective_user else "?",
        )
        # Kein Reply – gibt fremden Usern keinen Hinweis dass der Bot existiert

    # ------------------------------------------------------------------
    # Initialisierung
    # ------------------------------------------------------------------

    def start(self):
        """Baut die Application und registriert Handler. Kein Loop-Start hier."""
        if not self.cfg.telegram_bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN nicht gesetzt – Telegram-Bot deaktiviert")
            return
        if not self.cfg.telegram_chat_id:
            logger.warning("TELEGRAM_CHAT_ID nicht gesetzt – Telegram-Bot deaktiviert")
            return

        self._app = Application.builder().token(self.cfg.telegram_bot_token).build()

        # Commands
        self._app.add_handler(CommandHandler("start",     self._cmd_start))
        self._app.add_handler(CommandHandler("help",      self._cmd_help))
        self._app.add_handler(CommandHandler("status",    self._cmd_status))
        self._app.add_handler(CommandHandler("bots",      self._cmd_status))
        self._app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))
        self._app.add_handler(CommandHandler("start_bot", self._cmd_start_bot))
        self._app.add_handler(CommandHandler("stop_bot",  self._cmd_stop_bot))
        self._app.add_handler(CommandHandler("stop_all",  self._cmd_stop_all))
        self._app.add_handler(CommandHandler("buy",       self._cmd_buy))
        self._app.add_handler(CommandHandler("sell",      self._cmd_sell))
        self._app.add_handler(CommandHandler("set_sl",    self._cmd_set_sl))
        self._app.add_handler(CommandHandler("set_tp",    self._cmd_set_tp))
        self._app.add_handler(CommandHandler("params",    self._cmd_params))
        self._app.add_handler(CommandHandler("holdings",    self._cmd_holdings))
        self._app.add_handler(CommandHandler("rendite",     self._cmd_rendite))
        self._app.add_handler(CommandHandler("supervisor",  self._cmd_supervisor))
        self._app.add_handler(CommandHandler("sentiment",   self._cmd_sentiment))
        self._app.add_handler(CommandHandler("news",       self._cmd_news))

        # Inline-Button-Callbacks
        self._app.add_handler(CallbackQueryHandler(self._callback_handler))

        # Freitext-Handler (nach allen Command-Handlern registrieren)
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._cmd_natural_language))

        # Commands bei Telegram registrieren (erscheinen als Vorschläge wenn man / tippt)
        self._register_commands()

        logger.info("Telegram-Bot initialisiert (nur Chat-ID %s autorisiert)", self.cfg.telegram_chat_id)

    def _register_commands(self):
        """Registriert Befehle bei Telegram (sichtbar als Vorschläge beim Tippen von /)."""
        commands = [
            ("start",     "Begrüßung"),
            ("status",    "Status aller Bots inkl. Balance"),
            ("portfolio", "Offene Positionen + P&L"),
            ("params",    "Parameter eines Bots (z.B. /params BTC/EUR)"),
            ("start_bot", "Bot starten (z.B. /start_bot BTC/EUR)"),
            ("stop_bot",  "Bot stoppen (z.B. /stop_bot BTC/EUR)"),
            ("stop_all",  "Alle Bots sofort stoppen"),
            ("buy",       "Force-Kauf (z.B. /buy BTC/EUR)"),
            ("sell",      "Force-Verkauf (z.B. /sell BTC/EUR)"),
            ("set_sl",    "Stop-Loss setzen (z.B. /set_sl BTC/EUR 2.0)"),
            ("set_tp",    "Take-Profit setzen (z.B. /set_tp BTC/EUR 4.0)"),
            ("holdings",  "Alle gehaltenen Coins auf Kraken"),
            ("rendite",     "Detaillierte Rentabilität aller Bots"),
            ("supervisor",  "Supervisor-Erfahrung abrufen (z.B. /supervisor BTC/EUR)"),
            ("sentiment",   "Sentiment-Trend der letzten 24h (z.B. /sentiment BTC/EUR)"),
            ("news",        "Letzte News (z.B. /news BTC/EUR)"),
            ("help",        "Alle Befehle anzeigen"),
        ]
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/setMyCommands",
                json={"commands": [{"command": c, "description": d} for c, d in commands]},
                timeout=10,
            )
            if resp.json().get("ok"):
                logger.info("Telegram-Commands registriert (%d Befehle)", len(commands))
            else:
                logger.warning("setMyCommands fehlgeschlagen: %s", resp.text)
        except Exception as e:
            logger.warning("Fehler beim Registrieren der Commands: %s", e)

    # ------------------------------------------------------------------
    # Alert senden (fire-and-forget via asyncio.run)
    # ------------------------------------------------------------------

    def send_alert(
        self,
        item: NewsItem,
        score: float,
        label: str,
        coins: list[str],
        event_id: int,
    ):
        """Sendet einen Sentiment-Alert. Blockiert bis Nachricht bestätigt."""
        if not self.cfg.telegram_bot_token:
            return
        asyncio.run(self._send_alert_async(item, score, label, coins, event_id))

    async def _send_alert_async(
        self,
        item: NewsItem,
        score: float,
        label: str,
        coins: list[str],
        event_id: int,
    ):
        emoji = LABEL_EMOJI.get(label, "⚪")
        coins_str = ", ".join(coins) if coins else "Allgemeiner Markt"
        score_str = f"{score:+.2f}"

        age = datetime.now(timezone.utc) - item.published_at
        if age < timedelta(minutes=60):
            age_str = f"vor {int(age.total_seconds() / 60)} Minuten"
        elif age < timedelta(hours=24):
            age_str = f"vor {int(age.total_seconds() / 3600)} Stunden"
        else:
            age_str = item.published_at.strftime("%d.%m.%Y")

        if label == "bearish":
            recommendation = "⚠️ Empfehlung: Bot\\(s\\) pausieren bis Lage klarer"
        elif label == "bullish":
            recommendation = "💡 Empfehlung: Starkes positives Signal – Bots laufen lassen"
        else:
            recommendation = "ℹ️ Neutrales Signal"

        text = (
            f"{emoji} *\\[{label.upper()}\\]* {_esc(coins_str)} — Score: `{score_str}`\n"
            f"Quelle: {_esc(item.source)} \\| {_esc(age_str)}\n\n"
            f"_{_esc(item.title[:200])}_\n\n"
            f"{recommendation}\n"
            f"{'─' * 20}"
        )

        # Inline-Buttons – abhängig von laufenden Bots und Signal-Richtung
        running = _get_running_symbols(self.cfg.web_api_base)
        buttons = []

        if label == "bearish":
            stoppable = [s for s in coins if s in running] if coins else []
            if stoppable:
                for symbol in stoppable[:3]:
                    buttons.append(InlineKeyboardButton(
                        f"🛑 {symbol} stoppen",
                        callback_data=json.dumps({"action": "stop_bot", "symbol": symbol, "event_id": event_id}),
                    ))
            elif not coins and running:
                buttons.append(InlineKeyboardButton(
                    "🛑 Alle Bots stoppen",
                    callback_data=json.dumps({"action": "stop_all", "event_id": event_id}),
                ))

        elif label == "bullish":
            startable = [s for s in coins if s not in running] if coins else []
            for symbol in startable[:3]:
                buttons.append(InlineKeyboardButton(
                    f"▶ {symbol} starten",
                    callback_data=json.dumps({"action": "start_bot", "symbol": symbol, "event_id": event_id}),
                ))

        buttons.append(InlineKeyboardButton(
            "✅ Ignorieren",
            callback_data=json.dumps({"action": "dismiss", "event_id": event_id}),
        ))

        async with Bot(self.cfg.telegram_bot_token) as bot:
            msg = await bot.send_message(
                chat_id=self.cfg.telegram_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([buttons]),
                disable_web_page_preview=True,
            )

        self.db.execute(
            "INSERT INTO alert_history (news_event_id, telegram_msg_id, action, acted_at) VALUES (?,?,?,?)",
            (event_id, msg.message_id, "sent", datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        logger.info("Telegram-Alert gesendet (msg_id=%d, event_id=%d)", msg.message_id, event_id)

    # ------------------------------------------------------------------
    # Callback-Handler (Inline-Buttons)
    # ------------------------------------------------------------------

    async def _callback_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_authorized(update):
            await self._unauthorized(update)
            return

        try:
            data = json.loads(query.data)
        except (json.JSONDecodeError, TypeError):
            await query.edit_message_text("❌ Ungültige Callback-Daten")
            return

        action   = data.get("action")
        event_id = data.get("event_id")

        if action == "dismiss":
            await self._handle_dismiss(query, event_id)
        elif action == "stop_bot":
            await self._handle_stop_bot(query, data.get("symbol", ""), event_id)
        elif action == "stop_all":
            await self._handle_stop_all(query, event_id)
        elif action == "start_bot":
            await self._handle_start_bot(query, data.get("symbol", ""), event_id)
        else:
            await query.edit_message_text("❓ Unbekannte Aktion")

    async def _handle_dismiss(self, query, event_id: int):
        self.db.execute(
            "INSERT INTO alert_history (news_event_id, action, acted_at) VALUES (?,?,?)",
            (event_id, "dismissed", datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Alert ignoriert\\.", parse_mode=ParseMode.MARKDOWN_V2)

    async def _handle_stop_bot(self, query, symbol: str, event_id: int):
        result = _call_stop_api(self.cfg.web_api_base, symbol)
        msg = (f"🛑 Bot *{_esc(symbol)}* gestoppt\\." if result["ok"]
               else f"❌ Fehler: `{_esc(result['error'])}`")
        self.db.execute(
            "INSERT INTO alert_history (news_event_id, action, acted_at) VALUES (?,?,?)",
            (event_id, f"stopped_bot:{symbol}", datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

    async def _handle_start_bot(self, query, symbol: str, event_id: int):
        result = _call_start_api(self.cfg.web_api_base, symbol)
        if result["ok"]:
            p    = result.get("params", {})
            sl_s = f"{p.get('sl', 0.03)*100:.1f}%"
            tp_s = f"{p.get('tp', 0.06)*100:.1f}%"
            msg  = (
                f"▶ Bot *{_esc(symbol)}* gestartet\\.\n"
                f"_{_esc(p.get('timeframe', '5m'))} \\| Fast {p.get('fast', 9)} \\| "
                f"Slow {p.get('slow', 21)} \\| SL {_esc(sl_s)} \\| TP {_esc(tp_s)}_"
            )
        else:
            msg = f"❌ Fehler: `{_esc(result['error'])}`"
        self.db.execute(
            "INSERT INTO alert_history (news_event_id, action, acted_at) VALUES (?,?,?)",
            (event_id, f"started_bot:{symbol}", datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

    async def _handle_stop_all(self, query, event_id: int):
        results = _call_stop_all_api(self.cfg.web_api_base)
        lines = ["🛑 *Alle Bots stoppen:*"]
        for r in results:
            sym = _esc(r["symbol"])
            lines.append(f"  ✅ {sym} gestoppt" if r["ok"] else f"  ❌ {sym} Fehler")
        if not results:
            lines.append("  ℹ️ Keine laufenden Bots")
        self.db.execute(
            "INSERT INTO alert_history (news_event_id, action, acted_at) VALUES (?,?,?)",
            (event_id, "stopped_all", datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        await update.message.reply_text(
            "🤖 *TradingBot News\\-Agent*\n\n"
            "Ich benachrichtige dich bei wichtigen Krypto\\-News und lasse dich "
            "deine Bots direkt aus Telegram heraus steuern\\.\n\n"
            "/help – alle Befehle anzeigen",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        await update.message.reply_text(
            "📋 *Befehle:*\n\n"
            "📊 *Übersicht*\n"
            "/status \\- Status aller Bots \\(inkl\\. Balance\\)\n"
            "/portfolio \\- Offene Positionen \\+ P&L\n"
            "/rendite \\- Detaillierte Rentabilität \\(Win\\-Rate, P&L, Trades\\)\n"
            "/holdings \\- Alle gehaltenen Coins auf Kraken\n"
            "/supervisor \\- Supervisor\\-Erfahrung \\(Regime\\-Verlauf, Strategien\\)\n"
            "/sentiment \\- Sentiment\\-Trend der letzten 24h \\(z\\.B\\. /sentiment BTC/EUR\\)\n"
            "/news BTC/EUR \\- Letzte relevante News für einen Coin\n"
            "/params BTC/EUR \\- Parameter anzeigen\n\n"
            "▶ *Bot\\-Verwaltung*\n"
            "/start\\_bot BTC/EUR \\- Bot starten\n"
            "/stop\\_bot BTC/EUR \\- Bot stoppen\n"
            "/stop\\_all \\- Alle Bots sofort stoppen\n\n"
            "⚡ *Manueller Handel*\n"
            "/buy BTC/EUR \\- Kauf erzwingen \\(nächster Loop\\)\n"
            "/sell BTC/EUR \\- Verkauf erzwingen\n\n"
            "🎯 *SL/TP anpassen*\n"
            "/set\\_sl BTC/EUR 2\\.0 \\- SL auf 2% unter Entry\n"
            "/set\\_tp BTC/EUR 4\\.0 \\- TP auf 4% über Entry",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        try:
            resp = requests.get(f"{self.cfg.web_api_base}/api/bots", timeout=5)
            bots = resp.json()
            if not bots:
                await update.message.reply_text("ℹ️ Keine Bots konfiguriert\\.", parse_mode=ParseMode.MARKDOWN_V2)
                return

            # Echte Kraken-Balance holen (alle Coins, nicht nur bot-verwaltete)
            try:
                bal_resp  = requests.get(f"{self.cfg.web_api_base}/api/balance", timeout=15)
                bal_data  = bal_resp.json()
                fiat_eur  = float(bal_data.get("eur_free", 0))
                coin_total = float(bal_data.get("coins_total_eur", 0))
                total_eur  = float(bal_data.get("total_eur", 0))
            except Exception:
                # Fallback: aus Bot-DBs aggregieren
                seen_quotes: dict = {}
                coin_total = 0.0
                for b in sorted(bots, key=lambda x: x.get("last_update", ""), reverse=True):
                    if b.get("error"):
                        continue
                    q    = b.get("quote", "EUR")
                    rate = float(b.get("rate_to_eur", 1.0))
                    if q not in seen_quotes:
                        seen_quotes[q] = float(b.get("balance_quote_float", 0)) * rate
                    coin_total += float(b.get("coin_value_eur", 0))
                fiat_eur  = sum(seen_quotes.values())
                total_eur = fiat_eur + coin_total

            lines = [
                "📊 *Bot\\-Status:*\n",
                f"💰 Frei: `{fiat_eur:.2f} EUR` \\| Coins: `{coin_total:.2f} EUR` \\| Gesamt: `{total_eur:.2f} EUR`\n",
            ]
            for bot in bots:
                status  = bot.get("status", "unknown")
                signal  = bot.get("signal", "–")
                regime  = bot.get("regime", "–")
                sim_pnl = bot.get("sim_pnl", "–")
                strat   = bot.get("strategy_name", "–")
                fast    = bot.get("fast_period", "?")
                slow    = bot.get("slow_period", "?")

                icon    = "🟢" if status == "running" else "🔴"
                sig_ico = "📈" if signal == "BUY" else ("📉" if signal == "SELL" else "➡️")
                sim_str = (f" \\| Sim: {_esc(str(sim_pnl))}%" if sim_pnl not in ("–", "", None) else "")

                lines.append(
                    f"{icon} *{_esc(bot.get('symbol', '?'))}*\n"
                    f"  Status: {_esc(status)} \\| {sig_ico} {_esc(signal)}\n"
                    f"  Regime: {_esc(regime)} \\| Strategie: {_esc(strat)} \\({fast}/{slow}\\){sim_str}\n"
                )

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler beim Abrufen: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Zeigt offene Positionen aller Bots mit Entry, aktuellem Preis, P&L und SL/TP."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        try:
            resp = requests.get(f"{self.cfg.web_api_base}/api/bots", timeout=5)
            bots = resp.json()

            lines    = ["💼 *Offene Positionen:*\n"]
            total_pnl = 0.0
            no_pos   = []
            has_any  = False

            for bot in bots:
                symbol     = bot.get("symbol", "?")
                trades     = bot.get("open_trades", [])
                last_price = float(bot.get("last_price", 0) or 0)
                base       = bot.get("base", "")

                if not trades:
                    no_pos.append(symbol)
                    continue

                has_any = True
                for t in trades:
                    entry   = float(t.get("entry_price") or 0)
                    sl      = float(t.get("sl_price") or 0)
                    tp      = float(t.get("tp_price") or 0)
                    amount  = float(t.get("amount") or 0)
                    pnl_pct = float(t.get("pnl_pct") or 0)
                    dist_sl = t.get("dist_to_sl_pct")
                    dist_tp = t.get("dist_to_tp_pct")

                    pnl_eur = (last_price - entry) * amount if (entry and amount and last_price) else 0.0
                    total_pnl += pnl_eur

                    pnl_sign = "+" if pnl_eur >= 0 else ""
                    pct_sign = "+" if pnl_pct >= 0 else ""
                    sl_dist  = f" \\(Abst: {_esc(f'{dist_sl:.1f}')}%\\)" if dist_sl is not None else ""
                    tp_dist  = f" \\(Abst: {_esc(f'{dist_tp:.1f}')}%\\)" if dist_tp is not None else ""

                    lines.append(
                        f"📈 *{_esc(symbol)}* – {_esc(f'{amount:.6g}')} {_esc(base)}\n"
                        f"  Entry: `{_fmt_p(entry)}` \\| Jetzt: `{_fmt_p(last_price)}`\n"
                        f"  P&L: `{pnl_sign}{pnl_eur:.2f} EUR` \\(`{pct_sign}{pnl_pct:.2f}%`\\)\n"
                        f"  SL: `{_fmt_p(sl)}`{sl_dist} \\| TP: `{_fmt_p(tp)}`{tp_dist}\n"
                    )

            if not has_any:
                lines.append("ℹ️ Keine offenen Positionen\\.")

            if no_pos:
                lines.append(f"\n_Keine Position: {_esc(', '.join(no_pos))}_")

            pnl_sign = "+" if total_pnl >= 0 else ""
            lines.append(f"\n💰 *Gesamt P&L:* `{pnl_sign}{total_pnl:.2f} EUR`")

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            logger.exception("_cmd_portfolio Fehler")
            await update.message.reply_text(f"❌ Fehler: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_rendite(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Zeigt detaillierte Rentabilitäts-Auswertung aller Bots (P&L, Win-Rate, Trades)."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        try:
            resp = requests.get(f"{self.cfg.web_api_base}/api/bots", timeout=5)
            bots = resp.json()

            lines = ["📊 <b>Rentabilität – Detailauswertung</b>\n"]

            for bot in bots:
                symbol      = bot.get("symbol", "?")
                status      = bot.get("status", "?")
                regime      = bot.get("regime") or "–"
                strategy    = bot.get("strategy_name") or "Standard"
                fast        = bot.get("fast_period", "9")
                slow        = bot.get("slow_period", "21")
                last_price  = float(bot.get("last_price") or 0)
                open_trades = bot.get("open_trades", [])
                closed      = bot.get("closed_trades", [])
                sim_pnl     = bot.get("sim_pnl") or "–"

                icon = "🟢" if status == "running" else "🔴"
                lines.append(f"{icon} <b>{symbol}</b>  <i>{regime} | {strategy} {fast}/{slow}</i>")

                # Offene Position
                if open_trades:
                    t        = open_trades[0]
                    entry    = float(t.get("entry_price") or 0)
                    amount   = float(t.get("amount") or 0)
                    pnl_pct  = float(t.get("pnl_pct") or 0)
                    pnl_eur  = (last_price - entry) * amount if (entry and amount and last_price) else 0.0
                    dist_sl  = t.get("dist_to_sl_pct")
                    dist_tp  = t.get("dist_to_tp_pct")
                    p_sign   = "+" if pnl_eur >= 0 else ""
                    pct_sign = "+" if pnl_pct >= 0 else ""
                    pyr      = int(t.get("pyramid_count") or 0)
                    pyr_str  = f" 🔺×{pyr}" if pyr else ""
                    sl_str   = f" ({dist_sl:.1f}% Abst)" if dist_sl is not None else ""
                    tp_str   = f" ({dist_tp:.1f}% Abst)" if dist_tp is not None else ""
                    lines.append(
                        f"  📌 Position{pyr_str}: {amount:.6g} @ {_fmt_p(entry)}\n"
                        f"  Jetzt: {_fmt_p(last_price)} | P&L: <b>{p_sign}{pnl_eur:.2f} EUR</b> ({pct_sign}{pnl_pct:.2f}%)\n"
                        f"  SL: {_fmt_p(float(t.get('sl_price') or 0))}{sl_str} | TP: {_fmt_p(float(t.get('tp_price') or 0))}{tp_str}"
                    )
                else:
                    lines.append("  ⏸ Keine offene Position")

                # Geschlossene Trades auswerten
                valid = [t for t in closed if t.get("pnl_pct") is not None]
                if valid:
                    wins     = [t for t in valid if (t.get("pnl_pct") or 0) > 0]
                    total_eur = sum(
                        ((t.get("tp_price") if t["status"] == "tp_hit" else t.get("sl_price")) or t["entry_price"])
                        * float(t.get("amount") or 0)
                        - float(t.get("entry_price") or 0) * float(t.get("amount") or 0)
                        for t in valid
                    )
                    win_rate = len(wins) / len(valid) * 100
                    sign     = "+" if total_eur >= 0 else ""
                    best_t   = max(valid, key=lambda x: x.get("pnl_pct") or 0)
                    worst_t  = min(valid, key=lambda x: x.get("pnl_pct") or 0)
                    lines.append(
                        f"  📈 {len(valid)} Trades | Win-Rate: {win_rate:.0f}% | "
                        f"Gesamt: <b>{sign}{total_eur:.2f} EUR</b>\n"
                        f"  Bester: {best_t.get('pnl_pct', 0):+.2f}% ({best_t.get('status','?')}) | "
                        f"Schlechtester: {worst_t.get('pnl_pct', 0):+.2f}%"
                    )
                else:
                    lines.append("  📈 Noch keine abgeschlossenen Trades")

                if sim_pnl and sim_pnl != "–":
                    lines.append(f"  🔬 Sim-P&L (Backtest): {sim_pnl}%")

                lines.append("")  # Leerzeile zwischen Bots

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            logger.exception("_cmd_rendite Fehler")
            await update.message.reply_text(f"❌ Fehler: {e}")

    async def _cmd_supervisor(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Zeigt die gesammelte Erfahrung des Supervisors (Regime-Historie, Strategie-Verlauf)."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return

        import os
        import glob as _glob

        db_dir = os.path.dirname(os.path.abspath(self.cfg.db_path))
        symbol_arg = " ".join(context.args).upper().strip() if context.args else None
        # Symbol normalisieren (BTC → BTC/EUR)
        if symbol_arg and "/" not in symbol_arg:
            symbol_arg = symbol_arg + "/EUR"

        try:
            if symbol_arg:
                # Detailansicht eines einzelnen Bots
                safe = symbol_arg.replace("/", "_")
                db_path = os.path.join(db_dir, f"{safe}.db")
                if not os.path.exists(db_path):
                    await update.message.reply_text(
                        f"❌ Keine DB für <b>{symbol_arg}</b> gefunden.", parse_mode="HTML"
                    )
                    return

                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM supervisor_log ORDER BY id DESC LIMIT 20"
                ).fetchall()
                conn.close()

                if not rows:
                    await update.message.reply_text(
                        f"ℹ️ Noch keine Supervisor-Einträge für <b>{symbol_arg}</b>.\n"
                        f"Der Supervisor speichert erst nach dem nächsten Durchlauf.",
                        parse_mode="HTML",
                    )
                    return

                # Statistiken berechnen
                regime_counts: dict = {}
                strat_counts:  dict = {}
                cross_events:  list = []
                pnl_vals:      list = []

                for r in rows:
                    regime_counts[r["regime"]] = regime_counts.get(r["regime"], 0) + 1
                    strat_counts[r["strategy_name"]] = strat_counts.get(r["strategy_name"], 0) + 1
                    if r["source"] and r["source"].startswith("cross:"):
                        cross_events.append(r)
                    if r["sim_pnl"] is not None:
                        pnl_vals.append(r["sim_pnl"])

                total = len(rows)
                regime_str = "  ".join(
                    f"{regime}: {cnt}/{total}" for regime, cnt in sorted(
                        regime_counts.items(), key=lambda x: -x[1]
                    )
                )
                top_strats = sorted(strat_counts.items(), key=lambda x: -x[1])[:3]
                strat_str  = ", ".join(f"{n} ({c}×)" for n, c in top_strats)
                avg_pnl    = sum(pnl_vals) / len(pnl_vals) if pnl_vals else 0.0

                lines = [
                    f"🧠 <b>Supervisor-Erfahrung: {symbol_arg}</b>\n",
                    f"📋 Letzte {total} Einträge (max. 20)\n",
                    f"📊 Regime-Verteilung: {regime_str}",
                    f"🎯 Top-Strategien: {strat_str}",
                    f"📈 Ø Sim-P&amp;L: {avg_pnl:+.2f}%",
                ]

                if cross_events:
                    lines.append(f"\n🔗 Cross-Bot-Übernahmen: {len(cross_events)}×")
                    for ce in cross_events[:3]:
                        ts = ce["timestamp"][:16].replace("T", " ") if ce["timestamp"] else "?"
                        lines.append(
                            f"  [{ts}] {ce['strategy_name']} ({ce['sim_pnl']:+.2f}%)"
                        )

                lines.append("\n<b>Verlauf (neueste zuerst):</b>")
                for r in rows[:10]:
                    ts = r["timestamp"][:16].replace("T", " ") if r["timestamp"] else "?"
                    src = f" ← {r['source']}" if r["source"] and r["source"] != "own" else ""
                    adx_str = f" ADX={r['adx']:.0f}" if r["adx"] and r["adx"] >= 0 else ""
                    lines.append(
                        f"  {ts} | {r['regime']}{adx_str} | "
                        f"{r['strategy_name']} {r['fast']}/{r['slow']} | "
                        f"{r['sim_pnl']:+.2f}%{src}"
                    )

                await update.message.reply_text("\n".join(lines), parse_mode="HTML")

            else:
                # Übersicht: letzter Eintrag pro Bot
                db_paths = sorted(_glob.glob(os.path.join(db_dir, "*.db")))
                db_paths = [p for p in db_paths if os.path.basename(p) not in ("candles.db", "news.db")]

                if not db_paths:
                    await update.message.reply_text("ℹ️ Keine Bot-DBs gefunden.", parse_mode="HTML")
                    return

                lines = ["🧠 <b>Supervisor-Übersicht</b>\n"]
                for db_path in db_paths:
                    try:
                        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                        conn.row_factory = sqlite3.Row
                        row = conn.execute(
                            "SELECT * FROM supervisor_log ORDER BY id DESC LIMIT 1"
                        ).fetchone()
                        sym_row = conn.execute(
                            "SELECT value FROM bot_state WHERE key='symbol'"
                        ).fetchone()
                        count = conn.execute("SELECT COUNT(*) FROM supervisor_log").fetchone()[0]
                        conn.close()

                        sym = sym_row[0] if sym_row else os.path.basename(db_path).replace(".db", "").replace("_", "/", 1)
                        if row:
                            ts  = row["timestamp"][:16].replace("T", " ") if row["timestamp"] else "?"
                            src = f" ← {row['source']}" if row["source"] and row["source"] != "own" else ""
                            lines.append(
                                f"🔹 <b>{sym}</b> ({count} Einträge)\n"
                                f"   Letztes Regime: {row['regime']} | {row['strategy_name']} {row['fast']}/{row['slow']}\n"
                                f"   Sim-P&amp;L: {row['sim_pnl']:+.2f}% | {ts}{src}\n"
                            )
                        else:
                            lines.append(f"🔹 <b>{sym}</b> – noch keine Supervisor-Daten\n")
                    except Exception:
                        continue

                lines.append("💡 <i>/supervisor BTC/EUR – Detailansicht mit Verlauf</i>")
                await update.message.reply_text("\n".join(lines), parse_mode="HTML")

        except Exception as e:
            logger.exception("_cmd_supervisor Fehler")
            await update.message.reply_text(f"❌ Fehler: {e}", parse_mode="HTML")

    async def _cmd_sentiment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Zeigt den Sentiment-Trend der letzten 24h, optional gefiltert nach Symbol."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return

        import re as _re
        from datetime import timedelta

        symbol_arg = " ".join(context.args).upper().strip() if context.args else None
        if symbol_arg and "/" not in symbol_arg:
            symbol_arg = symbol_arg + "/EUR"

        now   = datetime.now(timezone.utc)
        ago24 = (now - timedelta(hours=24)).isoformat()
        ago4  = (now - timedelta(hours=4)).isoformat()

        try:
            # Alle Symbole oder gefiltert
            if symbol_arg:
                rows = self.db.execute(
                    "SELECT symbol, AVG(score) as avg_score, COUNT(*) as cnt, MAX(timestamp) as last_ts "
                    "FROM sentiment_scores WHERE timestamp >= ? AND symbol = ? "
                    "GROUP BY symbol ORDER BY symbol",
                    (ago24, symbol_arg),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT symbol, AVG(score) as avg_score, COUNT(*) as cnt, MAX(timestamp) as last_ts "
                    "FROM sentiment_scores WHERE timestamp >= ? "
                    "GROUP BY symbol ORDER BY symbol",
                    (ago24,),
                ).fetchall()

            if not rows:
                msg = (
                    f"ℹ️ Keine Sentiment\\-Daten für *{_esc(symbol_arg)}* in den letzten 24h\\."
                    if symbol_arg else
                    "ℹ️ Noch keine Sentiment\\-Daten in den letzten 24h\\."
                )
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                return

            lines = ["📊 *Sentiment\\-Trend \\(letzte 24h\\)*\n"]

            for row in rows:
                symbol    = row["symbol"]
                avg_score = row["avg_score"]
                cnt       = row["cnt"]
                label     = sent.score_to_label(avg_score, threshold=0.2)
                emoji     = LABEL_EMOJI.get(label, "⚪")

                # Trend-Pfeil: letzte 4h vs. 4-24h ago
                recent = self.db.execute(
                    "SELECT AVG(score) FROM sentiment_scores WHERE symbol=? AND timestamp >= ?",
                    (symbol, ago4),
                ).fetchone()[0]
                older = self.db.execute(
                    "SELECT AVG(score) FROM sentiment_scores WHERE symbol=? AND timestamp >= ? AND timestamp < ?",
                    (symbol, ago24, ago4),
                ).fetchone()[0]

                if recent is not None and older is not None:
                    diff = recent - older
                    trend = "↑" if diff > 0.05 else ("↓" if diff < -0.05 else "→")
                elif recent is not None:
                    trend = "→"
                else:
                    trend = "–"

                lines.append(
                    f"{emoji} *{_esc(symbol)}* {trend} `{avg_score:+.2f}`  "
                    f"\\({cnt} Artikel\\)  _{_esc(label)}_"
                )

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

        except Exception as e:
            logger.exception("_cmd_sentiment Fehler")
            await update.message.reply_text(
                f"❌ Fehler: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2
            )

    async def _cmd_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Zeigt letzte News aus news.db, optional nach Symbol gefiltert."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return

        symbol_arg = " ".join(context.args).upper().strip() if context.args else None
        if symbol_arg and "/" not in symbol_arg:
            symbol_arg = symbol_arg + "/EUR"

        now   = datetime.now(timezone.utc)
        ago48 = (now - timedelta(hours=48)).isoformat()

        try:
            if symbol_arg:
                rows = self.db.execute(
                    "SELECT title, sentiment_score, sentiment_label, source, published_at "
                    "FROM news_events "
                    "WHERE fetched_at >= ? AND coins LIKE ? "
                    "ORDER BY fetched_at DESC LIMIT 5",
                    (ago48, f'%"{symbol_arg}"%'),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT title, sentiment_score, sentiment_label, source, published_at "
                    "FROM news_events "
                    "WHERE fetched_at >= ? "
                    "ORDER BY ABS(sentiment_score) DESC, fetched_at DESC LIMIT 10",
                    (ago48,),
                ).fetchall()

            if not rows:
                msg = (
                    f"ℹ️ Keine News für *{_esc(symbol_arg)}* in den letzten 48h\\."
                    if symbol_arg else
                    "ℹ️ Keine News in den letzten 48h\\."
                )
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
                return

            header = (
                f"📰 *News – {_esc(symbol_arg)} \\(letzte 48h\\)*\n"
                if symbol_arg else
                "📰 *Stärkste News \\(letzte 48h, alle Coins\\)*\n"
            )
            lines = [header]

            for row in rows:
                title  = row[0]
                score  = row[1]
                label  = row[2]
                source = row[3]
                pub_at = row[4]

                emoji     = LABEL_EMOJI.get(label or "neutral", "⚪")
                score_str = f"{score:+.2f}" if score is not None else "n/a"

                try:
                    pub_dt  = datetime.fromisoformat(pub_at.replace("Z", "+00:00"))
                    age     = now - pub_dt
                    if age < timedelta(hours=1):
                        age_str = f"vor {int(age.total_seconds()/60)}min"
                    elif age < timedelta(hours=24):
                        age_str = f"vor {int(age.total_seconds()/3600)}h"
                    else:
                        age_str = f"vor {age.days}d"
                except Exception:
                    age_str = pub_at[:10] if pub_at else "?"

                lines.append(
                    f"{emoji} `{score_str}` · _{_esc(title[:120])}_\n"
                    f"   {_esc(source or '?')} · {_esc(age_str)}\n"
                )

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

        except Exception as e:
            logger.exception("_cmd_news Fehler")
            await update.message.reply_text(f"❌ Fehler: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_holdings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Zeigt alle aktuell auf Kraken gehaltenen Coins mit Preis und EUR-Wert."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        try:
            resp = requests.get(f"{self.cfg.web_api_base}/api/balance", timeout=15)
            data = resp.json()
            if "error" in data:
                await update.message.reply_text(f"❌ Fehler: `{_esc(data['error'])}`", parse_mode=ParseMode.MARKDOWN_V2)
                return

            eur_free   = float(data.get("eur_free", 0))
            coins      = data.get("coins", {})
            coin_total = float(data.get("coins_total_eur", 0))
            total      = float(data.get("total_eur", 0))

            lines = ["💼 *Holdings \\(Kraken\\):*\n"]
            lines.append(f"💶 EUR:  `{eur_free:.2f} EUR`\n")

            # Coins nach EUR-Wert sortiert
            for coin, d in sorted(coins.items(), key=lambda x: -x[1]["value_eur"]):
                amount    = d["amount"]
                price     = d["price"]
                value_eur = d["value_eur"]
                if value_eur > 0 or amount > 0:
                    price_str = _fmt_p(price) if price else "kein Markt"
                    lines.append(
                        f"🔸 *{_esc(coin)}:* `{amount:.6g}` × `{_esc(price_str)}` \\= `{value_eur:.2f} EUR`"
                    )

            lines.append(f"\n📊 Coins gesamt: `{coin_total:.2f} EUR`")
            lines.append(f"💰 *Gesamt: `{total:.2f} EUR`*")

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_natural_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Versteht Freitext wie 'status', 'stoppe BTC', 'kauf ETH', 'holdings' etc."""
        if not self._is_authorized(update):
            return
        import re
        text = (update.message.text or "").strip().lower()

        # status / bots
        if re.search(r'\bstatus\b|\bbots?\b', text):
            await self._cmd_status(update, context)
            return

        # portfolio / positionen
        if re.search(r'\bportfolio\b|\bpositionen?\b|\bposition\b', text):
            await self._cmd_portfolio(update, context)
            return

        # rendite / rentabilität / performance
        if re.search(r'\brendite\b|\brentabilit\b|\bperformance\b|\bstats?\b|\bauswertung\b', text):
            await self._cmd_rendite(update, context)
            return

        # supervisor / erfahrung / lernverlauf
        m = re.search(r'\bsupervisor\b|\berfahrung\b|\blernverlauf\b|\bregime.verlauf\b', text)
        if m:
            # Optionales Symbol extrahieren
            _coins = "|".join(k.split("/")[0].lower() for k in self.cfg.coin_keywords.keys())
            sym_m  = re.search(rf'({_coins})(?:[/\s]eur)?', text)
            if sym_m:
                context.args = [_normalize_symbol(sym_m.group(1))]
            else:
                context.args = []
            await self._cmd_supervisor(update, context)
            return

        # sentiment / stimmung / marktlage
        m = re.search(r'\b(sentiment|stimmung|marktlage)\b\s*([A-Za-z]{2,6})?', text)
        if m:
            context.args = [_normalize_symbol(m.group(2))] if m.group(2) else []
            return await self._cmd_sentiment(update, context)

        # news [symbol]
        m = re.search(r'\bnews\b\s*([A-Za-z]{2,6})?', text)
        if m:
            context.args = [_normalize_symbol(m.group(1))] if m.group(1) else []
            return await self._cmd_news(update, context)

        # holdings / bestände / was halte ich
        if re.search(r'\bholdings?\b|\bbestände?\b|\bbestand\b|\bwas halt', text):
            await self._cmd_holdings(update, context)
            return

        # hilfe
        if re.search(r'\bhilfe\b|\bhelp\b|\bbefehle?\b', text):
            await self._cmd_help(update, context)
            return

        # stop all
        if re.search(r'\bstop(?:pe)?\s+all(?:e)?\b|\balle\s+stopp?en\b|\bnotfall\b', text):
            await self._cmd_stop_all(update, context)
            return

        # stop <symbol>
        m = re.search(r'\bstop(?:pe)?\s+([\w/]+)', text)
        if m:
            context.args = [_normalize_symbol(m.group(1))]
            await self._cmd_stop_bot(update, context)
            return

        # start <symbol>
        m = re.search(r'\bstart(?:e)?\s+([\w/]+)', text)
        if m:
            context.args = [_normalize_symbol(m.group(1))]
            await self._cmd_start_bot(update, context)
            return

        # kauf / buy <symbol>
        m = re.search(r'\b(?:kauf(?:e)?|buy)\s+([\w/]+)', text)
        if m:
            context.args = [_normalize_symbol(m.group(1))]
            await self._cmd_buy(update, context)
            return

        # verkauf / sell <symbol>
        m = re.search(r'\b(?:verk(?:auf(?:e)?)?|sell)\s+([\w/]+)', text)
        if m:
            context.args = [_normalize_symbol(m.group(1))]
            await self._cmd_sell(update, context)
            return

        # params / parameter <symbol>
        m = re.search(r'\bparams?\s+([\w/]+)|\bparameter\s+([\w/]+)', text)
        if m:
            context.args = [_normalize_symbol(m.group(1) or m.group(2))]
            await self._cmd_params(update, context)
            return

        # sl setzen: "sl btc 2" oder "stop loss btc 2.5"
        m = re.search(r'\b(?:sl|stop\s*loss)\s+([\w/]+)\s+([\d.]+)', text)
        if m:
            context.args = [_normalize_symbol(m.group(1)), m.group(2)]
            await self._cmd_set_sl(update, context)
            return

        # tp setzen: "tp btc 4" oder "take profit btc 4.0"
        m = re.search(r'\b(?:tp|take\s*profit)\s+([\w/]+)\s+([\d.]+)', text)
        if m:
            context.args = [_normalize_symbol(m.group(1)), m.group(2)]
            await self._cmd_set_tp(update, context)
            return

        await update.message.reply_text(
            "❓ Nicht verstanden\\.\n\n"
            "Beispiele: _status_ · _holdings_ · _portfolio_ · "
            "_stoppe BTC_ · _starte ETH_ · _kauf BTC_ · _sl BTC 2_ · _hilfe_",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    async def _cmd_start_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        if not context.args:
            await update.message.reply_text(
                "Verwendung: `/start_bot BTC/EUR`", parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        symbol = " ".join(context.args).upper().strip()
        await update.message.reply_text(f"▶ Starte {_esc(symbol)}…", parse_mode=ParseMode.MARKDOWN_V2)
        result = _call_start_api(self.cfg.web_api_base, symbol)
        if result["ok"]:
            p    = result.get("params", {})
            sl_s = f"{p.get('sl', 0.03)*100:.1f}%"
            tp_s = f"{p.get('tp', 0.06)*100:.1f}%"
            await update.message.reply_text(
                f"✅ *{_esc(symbol)}* gestartet\\.\n"
                f"_{_esc(p.get('timeframe', '5m'))} \\| Fast {p.get('fast', 9)} \\| "
                f"Slow {p.get('slow', 21)} \\| SL {_esc(sl_s)} \\| TP {_esc(tp_s)}_",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                f"❌ Fehler beim Starten von *{_esc(symbol)}*:\n`{_esc(result['error'])}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    async def _cmd_stop_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        if not context.args:
            await update.message.reply_text(
                "Verwendung: `/stop_bot BTC/EUR`", parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        symbol = " ".join(context.args).upper().strip()
        result = _call_stop_api(self.cfg.web_api_base, symbol)
        if result["ok"]:
            await update.message.reply_text(
                f"🛑 *{_esc(symbol)}* gestoppt\\.", parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await update.message.reply_text(
                f"❌ Fehler beim Stoppen von *{_esc(symbol)}*:\n`{_esc(result['error'])}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    async def _cmd_stop_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        await update.message.reply_text("🛑 Stoppe alle laufenden Bots…", parse_mode=ParseMode.MARKDOWN_V2)
        results = _call_stop_all_api(self.cfg.web_api_base)
        if not results:
            await update.message.reply_text("ℹ️ Keine laufenden Bots gefunden\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        lines = ["🛑 *Ergebnis:*\n"]
        for r in results:
            sym = _esc(r["symbol"])
            lines.append(f"  ✅ {sym} gestoppt" if r["ok"] else f"  ❌ {sym}: {_esc(r.get('error',''))}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    async def _cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Setzt ein Force-BUY-Signal für einen Bot."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        if not context.args:
            await update.message.reply_text(
                "Verwendung: `/buy BTC/EUR`\n_Führt beim nächsten Bot\\-Loop einen Kauf aus\\._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        symbol = " ".join(context.args).upper().strip()
        result = _call_force_signal(self.cfg.web_api_base, symbol, "BUY")
        if result["ok"]:
            await update.message.reply_text(
                f"📈 *Force\\-BUY* für *{_esc(symbol)}* gesetzt\\.\n"
                f"_Wird beim nächsten Loop \\(\\~60s\\) ausgeführt\\._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                f"❌ Fehler: `{_esc(result['error'])}`", parse_mode=ParseMode.MARKDOWN_V2
            )

    async def _cmd_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Setzt ein Force-SELL-Signal für einen Bot."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        if not context.args:
            await update.message.reply_text(
                "Verwendung: `/sell BTC/EUR`\n_Schließt die offene Position beim nächsten Loop\\._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        symbol = " ".join(context.args).upper().strip()
        result = _call_force_signal(self.cfg.web_api_base, symbol, "SELL")
        if result["ok"]:
            await update.message.reply_text(
                f"📉 *Force\\-SELL* für *{_esc(symbol)}* gesetzt\\.\n"
                f"_Wird beim nächsten Loop \\(\\~60s\\) ausgeführt\\._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                f"❌ Fehler: `{_esc(result['error'])}`", parse_mode=ParseMode.MARKDOWN_V2
            )

    async def _cmd_set_sl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Setzt Stop-Loss in % vom Entry-Preis. Beispiel: /set_sl BTC/EUR 2.0"""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "Verwendung: `/set_sl BTC/EUR 2\\.0`\n_Setzt SL auf 2% unter Entry\\-Preis\\._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        symbol = context.args[0].upper()
        try:
            pct = float(context.args[1])
            if pct <= 0 or pct >= 50:
                raise ValueError("Wert muss zwischen 0 und 50 liegen")
        except ValueError as e:
            await update.message.reply_text(f"❌ Ungültiger Wert: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
            return
        result = _call_set_sltp_pct(self.cfg.web_api_base, symbol, sl_pct=pct)
        if result["ok"]:
            await update.message.reply_text(
                f"✅ *{_esc(symbol)}* SL auf `{pct}%` unter Entry gesetzt\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                f"❌ Fehler: `{_esc(result['error'])}`", parse_mode=ParseMode.MARKDOWN_V2
            )

    async def _cmd_set_tp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Setzt Take-Profit in % vom Entry-Preis. Beispiel: /set_tp BTC/EUR 4.0"""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "Verwendung: `/set_tp BTC/EUR 4\\.0`\n_Setzt TP auf 4% über Entry\\-Preis\\._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        symbol = context.args[0].upper()
        try:
            pct = float(context.args[1])
            if pct <= 0 or pct >= 100:
                raise ValueError("Wert muss zwischen 0 und 100 liegen")
        except ValueError as e:
            await update.message.reply_text(f"❌ Ungültiger Wert: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)
            return
        result = _call_set_sltp_pct(self.cfg.web_api_base, symbol, tp_pct=pct)
        if result["ok"]:
            await update.message.reply_text(
                f"✅ *{_esc(symbol)}* TP auf `{pct}%` über Entry gesetzt\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await update.message.reply_text(
                f"❌ Fehler: `{_esc(result['error'])}`", parse_mode=ParseMode.MARKDOWN_V2
            )

    async def _cmd_params(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Zeigt aktuelle Parameter eines Bots."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return
        if not context.args:
            await update.message.reply_text(
                "Verwendung: `/params BTC/EUR`", parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        symbol = " ".join(context.args).upper().strip()
        try:
            resp = requests.get(f"{self.cfg.web_api_base}/api/bots", timeout=5)
            bots = {b["symbol"]: b for b in resp.json()}
            if symbol not in bots:
                await update.message.reply_text(
                    f"❌ Bot *{_esc(symbol)}* nicht gefunden\\.", parse_mode=ParseMode.MARKDOWN_V2
                )
                return
            b   = bots[symbol]
            st  = b.get("state", {})
            lines = [
                f"⚙️ *Parameter: {_esc(symbol)}*\n",
                f"SMA:     Fast `{st.get('fast_period','?')}` / Slow `{st.get('slow_period','?')}`",
                f"RSI:     buy \\< `{st.get('supervisor_rsi_buy_max', st.get('rsi_buy_max','?'))}` "
                f"/ sell \\> `{st.get('supervisor_rsi_sell_min', st.get('rsi_sell_min','?'))}`",
                f"ATR SL:  `×{st.get('supervisor_atr_sl_mult','?')}`  "
                f"ATR TP: `×{st.get('supervisor_atr_tp_mult','?')}`",
                f"Regime:  `{b.get('regime','–')}`  ADX: `{st.get('supervisor_adx','–')}`",
                f"Strategie: `{b.get('strategy_name','–')}` \\(Sim: `{b.get('sim_pnl','–')}%`\\)",
                f"Fallback SL: `{float(st.get('sl_pct',0.03))*100:.1f}%` "
                f"/ TP: `{float(st.get('tp_pct',0.06))*100:.1f}%`",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            await update.message.reply_text(
                f"❌ Fehler: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2
            )

    # ------------------------------------------------------------------
    # Test-Nachricht
    # ------------------------------------------------------------------

    def send_test_message(self):
        """Sendet eine Test-Nachricht um die Konfiguration zu verifizieren."""
        if not self.cfg.telegram_bot_token or not self.cfg.telegram_chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt")

        async def _send():
            async with Bot(self.cfg.telegram_bot_token) as bot:
                await bot.send_message(
                    chat_id=self.cfg.telegram_chat_id,
                    text=(
                        "🤖 *TradingBot News\\-Agent – Test*\n\n"
                        "✅ Verbindung erfolgreich\\!\n"
                        "Nur du \\(Chat\\-ID: `" + str(self.cfg.telegram_chat_id) + "`\\) "
                        "kannst diesen Bot steuern\\.\n\n"
                        "/help – Befehle anzeigen"
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )

        asyncio.run(_send())
        logger.info("Test-Nachricht erfolgreich gesendet")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run_polling(self):
        """Startet Polling in eigenem asyncio-Loop (blockierend, in Daemon-Thread aufrufen)."""
        if not self._app:
            return
        asyncio.run(self._app.run_polling(stop_signals=None))

    def stop(self):
        pass  # run_polling() endet wenn der Thread beendet wird (daemon=True)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escaped Sonderzeichen für Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text


def _get_running_symbols(base_url: str) -> set[str]:
    try:
        resp = requests.get(f"{base_url}/api/bots", timeout=5)
        return {b["symbol"] for b in resp.json() if b.get("status") == "running"}
    except Exception:
        return set()


def _call_start_api(base_url: str, symbol: str) -> dict:
    sl, tp, fast, slow, timeframe = 0.03, 0.06, 9, 21, "5m"
    try:
        bots = {b["symbol"]: b for b in requests.get(f"{base_url}/api/bots", timeout=5).json()}
        if symbol in bots:
            st        = bots[symbol].get("state", {})
            sl        = float(st.get("sl_pct",       sl))
            tp        = float(st.get("tp_pct",       tp))
            fast      = int(float(st.get("fast_period", fast)))
            slow      = int(float(st.get("slow_period", slow)))
            timeframe = st.get("timeframe",           timeframe)
    except Exception:
        pass  # Fallback auf Defaults

    try:
        resp = requests.post(
            f"{base_url}/api/bot/start",
            json={"symbol": symbol, "timeframe": timeframe, "fast": fast, "slow": slow,
                  "sl": sl, "tp": tp, "safety_buffer": 0.10},
            timeout=10,
        )
        resp.raise_for_status()
        return {"ok": True, "symbol": symbol,
                "params": {"sl": sl, "tp": tp, "fast": fast, "slow": slow, "timeframe": timeframe}}
    except Exception as e:
        return {"ok": False, "symbol": symbol, "error": str(e)}


def _call_stop_api(base_url: str, symbol: str) -> dict:
    try:
        resp = requests.post(f"{base_url}/api/bot/stop", json={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        return {"ok": True, "symbol": symbol}
    except Exception as e:
        return {"ok": False, "symbol": symbol, "error": str(e)}


def _call_stop_all_api(base_url: str) -> list[dict]:
    results = []
    try:
        resp = requests.get(f"{base_url}/api/bots", timeout=5)
        for bot in resp.json():
            if bot.get("status") == "running":
                results.append(_call_stop_api(base_url, bot.get("symbol", "")))
    except Exception as e:
        logger.error("Fehler beim Holen der Bot-Liste: %s", e)
    return results


def _call_force_signal(base_url: str, symbol: str, signal: str) -> dict:
    """Setzt ein Force-Signal (BUY/SELL) via Web-API."""
    try:
        resp = requests.post(
            f"{base_url}/api/bot/force_signal",
            json={"symbol": symbol, "signal": signal},
            timeout=10,
        )
        resp.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fmt_p(price: float) -> str:
    """Dynamische Dezimalstellen je nach Preishöhe (für Telegram-Ausgabe)."""
    if price == 0:
        return "0.00"
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.6f}"
    return f"{price:.8f}"


def _normalize_symbol(s: str) -> str:
    """btc → BTC/EUR, btc/eur → BTC/EUR"""
    s = s.upper()
    if "/" not in s:
        s = f"{s}/EUR"
    return s


def _call_set_sltp_pct(
    base_url: str,
    symbol: str,
    sl_pct: float | None = None,
    tp_pct: float | None = None,
) -> dict:
    """Setzt SL/TP als Prozentsatz vom Entry-Preis."""
    try:
        payload: dict = {"symbol": symbol}
        if sl_pct is not None:
            payload["sl_pct"] = sl_pct
        if tp_pct is not None:
            payload["tp_pct"] = tp_pct
        resp = requests.post(
            f"{base_url}/api/bot/set_sltp_pct",
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return {"ok": True}
    except requests.HTTPError as e:
        try:
            return {"ok": False, "error": e.response.json().get("error", str(e))}
        except Exception:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
