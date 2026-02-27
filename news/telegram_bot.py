"""
Telegram Bot für News-Alerts mit Inline-Buttons und vollständiger Bot-Steuerung.

Sicherheit: Alle eingehenden Nachrichten/Callbacks werden gegen TELEGRAM_CHAT_ID geprüft.
Fremde User erhalten keine Antwort.

Befehle:
  /start          – Begrüßung + Befehlsübersicht
  /status         – Status aller Bots (Regime, Signal, P&L)
  /bots           – Alias für /status
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
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
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
        self._app.add_handler(CommandHandler("start_bot", self._cmd_start_bot))
        self._app.add_handler(CommandHandler("stop_bot",  self._cmd_stop_bot))
        self._app.add_handler(CommandHandler("stop_all",  self._cmd_stop_all))
        self._app.add_handler(CommandHandler("buy",       self._cmd_buy))
        self._app.add_handler(CommandHandler("sell",      self._cmd_sell))
        self._app.add_handler(CommandHandler("set_sl",    self._cmd_set_sl))
        self._app.add_handler(CommandHandler("set_tp",    self._cmd_set_tp))
        self._app.add_handler(CommandHandler("params",    self._cmd_params))

        # Inline-Button-Callbacks
        self._app.add_handler(CallbackQueryHandler(self._callback_handler))

        logger.info("Telegram-Bot initialisiert (nur Chat-ID %s autorisiert)", self.cfg.telegram_chat_id)

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
        msg = (
            f"▶ Bot *{_esc(symbol)}* gestartet\\.\n_5m \\| Fast 9 \\| Slow 21 \\| SL 3% \\| TP 6%_"
            if result["ok"] else f"❌ Fehler: `{_esc(result['error'])}`"
        )
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
            "/status \\- Status aller Bots\n"
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

            lines = ["📊 *Bot\\-Status:*\n"]
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
                sim_str = (f" \\| Sim: {_esc(sim_pnl)}%" if sim_pnl not in ("–", "") else "")

                lines.append(
                    f"{icon} *{_esc(bot.get('symbol', '?'))}*\n"
                    f"  Status: {_esc(status)} \\| {sig_ico} {_esc(signal)}\n"
                    f"  Regime: {_esc(regime)} \\| Strategie: {_esc(strat)} \\({fast}/{slow}\\){sim_str}\n"
                )

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler beim Abrufen: {_esc(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)

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
            await update.message.reply_text(
                f"✅ *{_esc(symbol)}* gestartet\\.\n_5m \\| Fast 9 \\| Slow 21 \\| SL 3% \\| TP 6%_",
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
    try:
        resp = requests.post(
            f"{base_url}/api/bot/start",
            json={"symbol": symbol, "timeframe": "5m", "fast": 9, "slow": 21,
                  "sl": 0.03, "tp": 0.06, "safety_buffer": 0.10},
            timeout=10,
        )
        resp.raise_for_status()
        return {"ok": True, "symbol": symbol}
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
