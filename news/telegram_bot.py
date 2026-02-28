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

# ---------------------------------------------------------------------------
# Parameter-Erklärungen (einfache Sprache, für /erklaerung)
# ---------------------------------------------------------------------------
PARAM_HELP: dict[str, tuple[str, str]] = {
    # key: (Titel, HTML-Erklärungstext)
    "timeframe": (
        "⏱ Timeframe (Kerzen-Größe)",
        "Die Kerzen-Größe bestimmt, <b>wie oft der Bot eine Entscheidung trifft</b>.\n\n"
        "• <b>1m / 3m:</b> Sehr schnell – viele Trades, viele Fehlsignale. Nur für erfahrene Nutzer.\n"
        "• <b>5m (Standard):</b> Gute Balance. Der Bot schaut alle 5 Minuten auf den Markt.\n"
        "• <b>15m / 1h:</b> Ruhiger – weniger Trades, aber verlässlichere Signale.\n\n"
        "💡 <i>Faustregel: Je volatiler der Coin, desto größer der Timeframe.</i>",
    ),
    "sma": (
        "📈 Fast MA / Slow MA (SMA-Crossover)",
        "Das ist das <b>Herzstück der Strategie</b>.\n\n"
        "Stell dir zwei Linien vor, die dem Kurs folgen:\n"
        "• <b>Fast MA (Standard: 9):</b> Kurze Linie – reagiert schnell auf Änderungen.\n"
        "• <b>Slow MA (Standard: 21):</b> Lange Linie – träge und stabil.\n\n"
        "Kreuzt die schnelle Linie die langsame <b>von unten nach oben</b> → 🟢 <b>Kaufen</b>\n"
        "Kreuzt sie sie <b>von oben nach unten</b> → 🔴 <b>Verkaufen</b>\n\n"
        "• Fast klein (5–7): Reagiert früh – macht aber mehr Fehler.\n"
        "• Fast groß (12–15): Weniger Fehler – steigt aber später ein.\n\n"
        "💡 <i>Fast sollte ¼ bis ½ des Slow-Werts sein (z.B. 9/21 oder 5/15).</i>",
    ),
    "sl": (
        "🛑 Stop-Loss %",
        "Die <b>Verlustbegrenzung</b>. Der Bot verkauft automatisch, wenn der Kurs zu weit fällt.\n\n"
        "<b>Beispiel mit 3% Stop-Loss:</b>\n"
        "• Kauf bei 100 €\n"
        "• Kurs fällt auf 97 € (–3%) → Bot verkauft sofort\n"
        "• Verlust begrenzt auf max. 3%\n\n"
        "• <b>1–2%:</b> Sehr eng – wird bei normaler Volatilität oft ausgelöst (nervös).\n"
        "• <b>3% (Standard):</b> Guter Mittelwert für BTC/ETH.\n"
        "• <b>5–8%:</b> Gibt mehr Spielraum, verliert aber mehr wenn er auslöst.\n\n"
        "💡 <i>Dieser Wert ist nur der Fallback. Normalerweise berechnet der Bot den SL\n"
        "automatisch aus der aktuellen Volatilität (ATR).</i>",
    ),
    "tp": (
        "🎯 Take-Profit %",
        "Das <b>Gewinnziel</b>. Der Bot verkauft automatisch, wenn der Kurs weit genug gestiegen ist.\n\n"
        "<b>Beispiel mit 6% Take-Profit:</b>\n"
        "• Kauf bei 100 €\n"
        "• Kurs steigt auf 106 € (+6%) → Bot verkauft sofort\n"
        "• 6% Gewinn gesichert ✅\n\n"
        "• <b>3–4%:</b> Nimmt Gewinne schnell mit – gut in ruhigen Märkten.\n"
        "• <b>6% (Standard):</b> Ausgewogener Mittelwert.\n"
        "• <b>10–15%:</b> Wartet auf große Bewegungen – gut in starken Trends.\n\n"
        "💡 <i>TP sollte mindestens 2× so groß sein wie der SL\n"
        "(Chance:Risiko ≥ 2:1 → langfristig profitabel auch mit 40% Trefferquote).</i>",
    ),
    "trailing": (
        "🎢 Trailing Stop-Loss",
        "Der SL <b>folgt dem steigenden Kurs automatisch nach oben</b> – aber nie nach unten.\n"
        "So werden wachsende Gewinne abgesichert.\n\n"
        "<b>Beispiel mit 2% Trailing:</b>\n"
        "• Kauf bei 100 €, SL startet bei 98 €\n"
        "• Kurs steigt auf 110 € → SL wandert auf 107,80 €\n"
        "• Kurs steigt auf 120 € → SL wandert auf 117,60 €\n"
        "• Kurs fällt auf 117,60 € → Bot verkauft → Gewinn gesichert ✅\n\n"
        "• <b>1%:</b> Eng – sichert früh ab, wird aber bei kleinen Rücksetzern ausgelöst.\n"
        "• <b>2% (Standard):</b> Gute Balance für BTC/ETH.\n"
        "• <b>3–5%:</b> Gibt dem Kurs mehr Luft für Schwankungen.\n\n"
        "💡 <i>Gut kombinierbar mit Breakeven:\n"
        "Erst kein Verlust sichern (Breakeven), dann Gewinne schützen (Trailing).</i>",
    ),
    "breakeven": (
        "🏁 Breakeven Stop-Loss",
        "Sobald ein Trade einen kleinen Gewinn erreicht, <b>springt der SL auf den Kaufpreis</b>.\n"
        "Danach ist ein Verlust <b>unmöglich</b> – im schlimmsten Fall ±0.\n\n"
        "<b>Beispiel mit 1% Breakeven:</b>\n"
        "• Kauf bei 100 €\n"
        "• Kurs steigt auf 101 € (+1%) → SL springt auf 100 €\n"
        "• Kurs fällt danach auf 100 € → Verkauf bei ±0 € (kein Verlust!) ✅\n\n"
        "• <b>0.5%:</b> Sehr früh – fast sofort abgesichert, viele Trades enden bei ±0.\n"
        "• <b>1% (Standard):</b> Sinnvoller Kompromiss.\n"
        "• <b>2–3%:</b> Mehr Spielraum vor der Absicherung.\n\n"
        "💡 <i>Perfekt kombiniert mit Trailing-SL:\n"
        "Breakeven = kein Verlust möglich, Trailing = Gewinn sichern.</i>",
    ),
    "partial": (
        "✂️ Partial Take-Profit",
        "Beim Zielkurs <b>nur einen Teil der Position verkaufen</b>. Der Rest läuft weiter.\n\n"
        "<b>Beispiel: 1 BTC, Kauf 90.000 €, TP 93.000 €, 50% Partial:</b>\n"
        "• Kurs erreicht 93.000 € → 0,5 BTC verkauft → 1.500 € gesichert ✅\n"
        "• Rest 0,5 BTC läuft weiter: SL = 90.000 € (kein Verlust), TP = 96.000 €\n"
        "• Kurs erreicht 96.000 € → Rest verkauft → weitere 1.500 € ✅\n\n"
        "• <b>25–33%:</b> Kleiner Teilverkauf – Großteil läuft weiter. Mehr Potential.\n"
        "• <b>50% (Standard):</b> Ausgewogen – Hälfte sichern, Hälfte läuft weiter.\n"
        "• <b>75%:</b> Konservativ – Großteil sofort gesichert.\n\n"
        "💡 <i>Der Remainder-Trade läuft nur wenn der Restbetrag über 15 € liegt.</i>",
    ),
    "htf": (
        "🔭 HTF-Filter (Higher Timeframe)",
        "Der Bot kauft nur, wenn auch der <b>übergeordnete Zeitrahmen aufwärts zeigt</b>.\n"
        "Verhindert Käufe gegen den großen Trend.\n\n"
        "<b>Beispiel (Bot auf 5m, HTF = 1h):</b>\n"
        "• 5m-Chart zeigt: 🟢 Kaufsignal\n"
        "• 1h-Chart zeigt: 📉 Abwärtstrend\n"
        "→ Bot kauft NICHT – der große Trend ist gegen uns.\n\n"
        "• <b>15m:</b> Leichte Filterung – für 1m/3m-Bots.\n"
        "• <b>1h (empfohlen für 5m):</b> Filtert Käufe gegen den Stunden-Trend.\n"
        "• <b>4h:</b> Starke Filterung – sehr wenige, aber zuverlässige Signale.\n"
        "• <b>1d:</b> Kauft nur im täglichen Aufwärtstrend (Swing-Trading).\n\n"
        "💡 <i>Aktivieren wenn du viele Verluste in Korrekturen hast.\n"
        "Weniger Trades, aber höhere Trefferquote.</i>",
    ),
    "volumen": (
        "📊 Volumen-Filter",
        "Kauft nur, wenn <b>wirklich etwas los ist</b> am Markt.\n\n"
        "Ein Kaufsignal bei sehr geringem Volumen ist oft ein Fehlalarm.\n\n"
        "<b>Beispiel mit Faktor 1.2:</b>\n"
        "• Durchschnittliches Volumen (letzte 20 Kerzen): 1.000.000 €\n"
        "• Aktuelles Volumen: 1.200.000 € (+20%) ✅ → Kauf erlaubt\n"
        "• Aktuelles Volumen: 800.000 € ❌ → Kein Kauf\n\n"
        "• <b>1.0:</b> Jedes Volumen über Durchschnitt reicht.\n"
        "• <b>1.2 (Standard):</b> 20% über Durchschnitt nötig.\n"
        "• <b>1.5–2.0:</b> Nur bei deutlichem Volumen-Anstieg.",
    ),
    "cooldown": (
        "⏳ SL-Cooldown",
        "<b>Wartepause nach einem Verlust.</b>\n\n"
        "Wenn der Bot ausgestoppt wird, wartet er N Kerzen bevor er wieder kauft.\n"
        "Verhindert, dass er sofort wieder in einen fallenden Markt springt.\n\n"
        "<b>Beispiel: 3 Candles bei 5m-Timeframe:</b>\n"
        "• SL ausgelöst um 12:00 Uhr\n"
        "• Nächster möglicher Kauf: 12:15 Uhr\n\n"
        "• <b>0:</b> Kein Cooldown – sofortiger Wiederkauf möglich.\n"
        "• <b>3 (Standard):</b> 15 Minuten Pause bei 5m-Timeframe.\n"
        "• <b>5–10:</b> 25–50 Minuten – konservativer Ansatz.",
    ),
    "buffer": (
        "🛡 Safety Buffer",
        "Ein <b>Kapitalanteil der niemals investiert wird</b>.\n\n"
        "<b>Beispiel: 10% Buffer, 1.000 € Guthaben:</b>\n"
        "• 100 € werden nie angefasst (Reserve)\n"
        "• Von den verbleibenden 900 € wird der Handelsbetrag berechnet\n\n"
        "• <b>5%:</b> Aggressiv – fast alles wird eingesetzt.\n"
        "• <b>10% (Standard):</b> Sinnvoller Puffer.\n"
        "• <b>20–30%:</b> Konservativ – mehr Reserve für schlechte Zeiten.\n\n"
        "💡 <i>Wichtig wenn mehrere Bots gleichzeitig laufen – damit sie sich nicht\n"
        "gegenseitig das Kapital wegkaufen.</i>",
    ),
    "rsi": (
        "📉 RSI-Filter",
        "Verhindert Käufe, wenn der Markt <b>schon zu weit gestiegen ist</b>.\n\n"
        "RSI = Wert von 0–100.\n"
        "• Über 70: überkauft (zu heiß)\n"
        "• Unter 30: überverkauft (zu billig)\n\n"
        "Der RSI-Filter blockiert:\n"
        "• <b>Käufe</b> wenn RSI > rsi_buy_max (Standard: 65)\n"
        "• <b>Verkäufe</b> wenn RSI < rsi_sell_min (Standard: 35)\n\n"
        "💡 <i>Du kannst den RSI nicht direkt setzen – der Supervisor\n"
        "passt ihn automatisch ans Marktregime an\n"
        "(TREND: breiter, SIDEWAYS: enger, VOLATILE: sehr eng).</i>",
    ),
    "atr": (
        "📐 ATR – Automatisches SL/TP",
        "Der Bot passt SL und TP automatisch an die <b>aktuelle Volatilität</b> an.\n\n"
        "ATR = Wie weit sich der Kurs im Durchschnitt pro Kerze bewegt.\n\n"
        "<b>Beispiel: BTC, ATR = 500 €:</b>\n"
        "• SL-Multiplikator 1.5 → SL = 1,5 × 500 = 750 € unter Kaufpreis\n"
        "• TP-Multiplikator 2.5 → TP = 2,5 × 500 = 1.250 € über Kaufpreis\n\n"
        "Ist gerade viel los (hohe ATR) → weiter SL/TP.\n"
        "Ruhiger Markt (niedrige ATR) → enger SL/TP.\n\n"
        "💡 <i>Die Multiplikatoren werden vom Supervisor automatisch angepasst:\n"
        "TREND → 1.5/2.5 | SIDEWAYS → 1.2/1.8 | VOLATILE → 2.0/3.5</i>",
    ),
}


def _parse_duration(s: str) -> int | None:
    """
    Parst eine Zeitangabe → Minuten.
    Unterstützt: '30', '30min', '30m', '2h', '2std', '1.5h', '90minuten'
    Gibt None zurück bei ungültigem Format.
    """
    import re as _re
    m = _re.match(r'^([\d.]+)\s*(h|std(?:unden?)?|min(?:uten?)?|m)?$', s.strip().lower())
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "min").lower()
    if unit.startswith("h") or unit.startswith("st"):
        return int(val * 60)
    return int(val)


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
        self._app.add_handler(CommandHandler("set_alert_interval", self._cmd_set_alert_interval))
        self._app.add_handler(CommandHandler("erklaerung",         self._cmd_erklaerung))

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
            ("news",               "Letzte News (z.B. /news BTC/EUR)"),
            ("set_alert_interval", "Alert-Interval setzen (z.B. /set_alert_interval 2h)"),
            ("erklaerung",         "Parameter erklärt (z.B. /erklaerung trailing)"),
            ("help",               "Alle Befehle anzeigen"),
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

    def send_aggregated_alert(
        self,
        coin: str,
        articles: list[dict],
        consensus_score: float,
        label: str,
    ):
        """Sendet einen Konsens-Alert für mehrere Artikel desselben Coins."""
        if not self.cfg.telegram_bot_token:
            return
        asyncio.run(self._send_aggregated_async(coin, articles, consensus_score, label))

    async def _send_aggregated_async(
        self,
        coin: str,
        articles: list[dict],
        consensus_score: float,
        label: str,
    ):
        emoji = LABEL_EMOJI.get(label, "⚪")
        score_str = f"{consensus_score:+.2f}"
        n = len(articles)

        bullish  = [a for a in articles if a["score"] > 0]
        bearish  = [a for a in articles if a["score"] < 0]
        neutral  = [a for a in articles if a["score"] == 0]

        lines = [
            f"{emoji} *\\[{label.upper()}\\]* {_esc(coin)} — Konsens: `{score_str}` \\({n} Artikel\\)\n",
        ]

        if bullish:
            lines.append("📈 *Bullish:*")
            for a in sorted(bullish, key=lambda x: -x["score"])[:3]:
                lines.append(f"  `{a['score']:+.2f}` · _{_esc(a['item'].title[:120])}_")
        if bearish:
            lines.append("📉 *Bearish:*")
            for a in sorted(bearish, key=lambda x: x["score"])[:3]:
                lines.append(f"  `{a['score']:+.2f}` · _{_esc(a['item'].title[:120])}_")
        if neutral:
            lines.append("⚪ *Neutral:*")
            for a in neutral[:2]:
                lines.append(f"  `{a['score']:+.2f}` · _{_esc(a['item'].title[:120])}_")

        lines.append("")
        if label == "bearish":
            lines.append("⚠️ Empfehlung: Bot\\(s\\) pausieren bis Lage klarer")
        elif label == "bullish":
            lines.append("💡 Empfehlung: Starkes positives Signal – Bots laufen lassen")
        else:
            lines.append("⚖️ Empfehlung: Widersprüchliche Signale – Abwarten")
        lines.append(f"{'─' * 20}")

        text = "\n".join(lines)

        running = _get_running_symbols(self.cfg.web_api_base)
        buttons = []
        event_ids = [a["event_id"] for a in articles]

        if label == "bearish":
            stoppable = [coin] if coin in running else []
            if stoppable:
                buttons.append(InlineKeyboardButton(
                    f"🛑 {coin} stoppen",
                    callback_data=json.dumps({"action": "stop_bot", "symbol": coin, "event_id": event_ids[0]}),
                ))
        elif label == "bullish" and coin not in running:
            buttons.append(InlineKeyboardButton(
                f"▶ {coin} starten",
                callback_data=json.dumps({"action": "start_bot", "symbol": coin, "event_id": event_ids[0]}),
            ))
        buttons.append(InlineKeyboardButton(
            "✅ Ignorieren",
            callback_data=json.dumps({"action": "dismiss", "event_id": event_ids[0]}),
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
            (event_ids[0], msg.message_id, "sent", datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        logger.info(
            "Aggregierter Alert gesendet: %s | %d Artikel | Konsens %.2f (msg_id=%d)",
            coin, n, consensus_score, msg.message_id,
        )

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
            "/set\\_tp BTC/EUR 4\\.0 \\- TP auf 4% über Entry\n\n"
            "📖 *Hilfe*\n"
            "/erklaerung \\- Parameter einfach erklärt \\(z\\.B\\. /erklaerung trailing\\)\n"
            "/erklaerung sl \\| tp \\| sma \\| trailing \\| breakeven \\| partial \\| htf \\| volumen \\| cooldown \\| buffer \\| rsi \\| atr",
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

        # erklaerung / was ist / wie funktioniert
        m = re.search(
            r'\b(?:erkl[äa]r(?:ung)?e?|was ist|wie funktioniert)\s+([\w-]+)', text
        )
        if m:
            context.args = [m.group(1)]
            await self._cmd_erklaerung(update, context)
            return
        if re.search(r'\berkl[äa]r(?:ung)?\b', text):
            context.args = []
            await self._cmd_erklaerung(update, context)
            return

        # alert interval: "alert interval 30", "cooldown 2h", "benachrichtigung 45min"
        m = re.search(
            r'\b(?:alert[\s\-]?(?:interval|cooldown)?|cooldown|benachrichtigungs?[\s\-]?intervall?)\s+([\d.]+(?:h|std|min|m)?)\b',
            text,
        )
        if m:
            context.args = [m.group(1)]
            await self._cmd_set_alert_interval(update, context)
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
                "Verwendung: `/start_bot BTC/EUR \\[sl\\=2 tp\\=4 trailing breakeven htf\\=1h\\]`\n"
                "_Optionale Parameter: `sl=2`, `tp=4`, `trailing`, `trailing=2`, `breakeven`, "
                "`breakeven=1`, `partial`, `partial=60`, `htf=1h`, `volume`, `cooldown=5`_",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        symbol    = _normalize_symbol(context.args[0])
        overrides = _parse_bot_overrides(context.args[1:]) if len(context.args) > 1 else {}
        await update.message.reply_text(f"▶ Starte {_esc(symbol)}…", parse_mode=ParseMode.MARKDOWN_V2)
        result = _call_start_api(self.cfg.web_api_base, symbol, overrides)
        if result["ok"]:
            p    = result.get("params", {})
            sl_s = f"{p.get('sl', 0.03)*100:.1f}%"
            tp_s = f"{p.get('tp', 0.06)*100:.1f}%"
            tf   = p.get("timeframe", "5m")
            # Feature-Zusammenfassung
            feats = []
            if p.get("trailing_sl"):     feats.append(f"Trailing {p.get('trailing_sl_pct', 0.02)*100:.1f}%")
            if p.get("breakeven"):       feats.append(f"Breakeven {p.get('breakeven_pct', 0.01)*100:.1f}%")
            if p.get("partial_tp"):      feats.append(f"Partial\\-TP {p.get('partial_tp_fraction', 0.5)*100:.0f}%")
            if p.get("htf_timeframe"):   feats.append(f"HTF {p.get('htf_timeframe')}")
            if p.get("volume_filter"):   feats.append(f"Vol ×{p.get('volume_factor', 1.2):.1f}")
            cd = p.get("sl_cooldown", 3)
            if cd != 3:                  feats.append(f"Cooldown {cd}c")
            msg = (
                f"✅ *{_esc(symbol)}* gestartet\\.\n"
                f"_{_esc(tf)} \\| Fast {p.get('fast', 9)} \\| Slow {p.get('slow', 21)} "
                f"\\| SL {_esc(sl_s)} \\| TP {_esc(tp_s)}_"
            )
            if feats:
                msg += f"\n_Features: {_esc(' · '.join(feats))}_"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
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
    # Parameter-Erklärungen
    # ------------------------------------------------------------------

    async def _cmd_erklaerung(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Erklärt einen Parameter in einfacher Sprache."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return

        # Alias-Map: was der User tippt → PARAM_HELP-Key
        ALIASES = {
            "trailing": "trailing", "trail": "trailing",
            "breakeven": "breakeven", "be": "breakeven",
            "partial": "partial", "partiell": "partial", "teiltp": "partial",
            "htf": "htf", "timefilter": "htf", "trendfilter": "htf",
            "volumen": "volumen", "volume": "volumen", "vol": "volumen",
            "cooldown": "cooldown", "pause": "cooldown",
            "sl": "sl", "stoploss": "sl", "stop": "sl",
            "tp": "tp", "takeprofit": "tp",
            "sma": "sma", "ma": "sma", "fast": "sma", "slow": "sma", "crossover": "sma",
            "buffer": "buffer", "sicherheit": "buffer", "reserve": "buffer",
            "rsi": "rsi",
            "atr": "atr", "volatilität": "atr", "volatility": "atr",
            "timeframe": "timeframe", "tf": "timeframe", "kerzen": "timeframe",
        }

        if not context.args:
            # Übersicht aller verfügbaren Erklärungen
            lines = ["<b>📖 Parameter-Erklärungen</b>\n"]
            lines.append("Schreibe <code>/erklaerung &lt;parameter&gt;</code> für Details:\n")
            for key, (titel, _) in PARAM_HELP.items():
                lines.append(f"• <code>/erklaerung {key}</code> – {titel}")
            lines.append('\n<i>Freetext: "erklaere trailing" oder "was ist breakeven"</i>')
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        raw = context.args[0].lower().replace("-", "").replace("_", "").replace(" ", "")
        key = ALIASES.get(raw)

        if not key:
            # Fuzzy: Teilstring-Suche
            for alias, k in ALIASES.items():
                if raw in alias or alias in raw:
                    key = k
                    break

        if not key:
            known = ", ".join(f"<code>{k}</code>" for k in PARAM_HELP)
            await update.message.reply_text(
                f"❓ Unbekannter Parameter: <code>{raw}</code>\n\nVerfügbar: {known}",
                parse_mode=ParseMode.HTML,
            )
            return

        titel, text = PARAM_HELP[key]
        await update.message.reply_text(
            f"<b>{titel}</b>\n\n{text}",
            parse_mode=ParseMode.HTML,
        )

    # ------------------------------------------------------------------
    # Alert-Interval konfigurieren
    # ------------------------------------------------------------------

    async def _cmd_set_alert_interval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Setzt das Mindest-Intervall zwischen zwei Alerts desselben Coins."""
        if not self._is_authorized(update):
            await self._unauthorized(update)
            return

        current = self.cfg.alert_cooldown_minutes
        if current >= 60 and current % 60 == 0:
            current_str = f"{current // 60}h"
        elif current >= 60:
            current_str = f"{current // 60}h {current % 60}min"
        else:
            current_str = f"{current}min"

        if not context.args:
            await update.message.reply_text(
                f"⏱ *Alert\\-Interval* \\(aktuell: `{_esc(current_str)}`\\)\n\n"
                f"Verwendung: `/set_alert_interval <Wert>`\n"
                f"Beispiele: `30` `45min` `1h` `2h` `90min`\n"
                f"Minimum: 5 min \\| Maximum: 24h",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        minutes = _parse_duration(context.args[0])
        if minutes is None or not (5 <= minutes <= 1440):
            await update.message.reply_text(
                "❌ Ungültiger Wert\\. Beispiele: `30` `45min` `1h` `2h`\n"
                "Erlaubt: 5 min bis 24h \\(1440 min\\)",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        self.cfg.alert_cooldown_minutes = minutes
        self.db.execute(
            "INSERT OR REPLACE INTO news_settings (key, value) VALUES ('alert_cooldown_minutes', ?)",
            (str(minutes),),
        )
        self.db.commit()

        if minutes >= 60 and minutes % 60 == 0:
            display = f"{minutes // 60}h"
        elif minutes >= 60:
            display = f"{minutes // 60}h {minutes % 60}min"
        else:
            display = f"{minutes}min"

        await update.message.reply_text(
            f"✅ Alert\\-Interval gesetzt: `{_esc(display)}`\n"
            f"Kein zweiter Alert für denselben Coin innerhalb von `{_esc(display)}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
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


def _parse_bot_overrides(args: list[str]) -> dict:
    """
    Parst Inline-Argumente für /start_bot nach dem Symbol.
    Beispiele: trailing  trailing=2  breakeven=1  htf=1h  sl=2  partial=60  volume  cooldown=5
    """
    overrides: dict = {}
    for arg in args:
        arg = arg.lower().strip()
        key, val = (arg.split("=", 1) + [None])[:2]
        try:
            if key in ("sl", "stop-loss", "stop_loss"):
                if val: overrides["sl"] = float(val.strip("%")) / 100
            elif key in ("tp", "take-profit", "take_profit"):
                if val: overrides["tp"] = float(val.strip("%")) / 100
            elif key in ("fast", "f"):
                if val: overrides["fast"] = int(val)
            elif key in ("slow", "s"):
                if val: overrides["slow"] = int(val)
            elif key in ("tf", "timeframe"):
                if val: overrides["timeframe"] = val
            elif key in ("trailing", "trail"):
                overrides["trailing_sl"] = True
                if val: overrides["trailing_sl_pct"] = float(val.strip("%")) / 100
            elif key in ("breakeven", "be"):
                overrides["breakeven"] = True
                if val: overrides["breakeven_pct"] = float(val.strip("%")) / 100
            elif key in ("partial", "partial-tp"):
                overrides["partial_tp"] = True
                if val: overrides["partial_tp_fraction"] = float(val.strip("%")) / 100
            elif key in ("htf", "htf-timeframe"):
                if val: overrides["htf_timeframe"] = val
            elif key in ("volume", "vol"):
                overrides["volume_filter"] = True
                if val: overrides["volume_factor"] = float(val)
            elif key == "cooldown":
                if val: overrides["sl_cooldown"] = int(val)
            elif key in ("notrailing", "no-trailing"):
                overrides["trailing_sl"] = False
            elif key in ("nobreakeven", "no-breakeven"):
                overrides["breakeven"] = False
            elif key in ("nopartial", "no-partial"):
                overrides["partial_tp"] = False
            elif key in ("novol", "no-volume"):
                overrides["volume_filter"] = False
            elif key == "nohtf":
                overrides["htf_timeframe"] = ""
        except (ValueError, TypeError):
            pass
    return overrides


def _call_start_api(base_url: str, symbol: str, overrides: dict | None = None) -> dict:
    params = {
        "sl": 0.03, "tp": 0.06, "fast": 9, "slow": 21, "timeframe": "5m",
        "safety_buffer": 0.10,
        "trailing_sl": False, "trailing_sl_pct": 0.02, "sl_cooldown": 3,
        "volume_filter": False, "volume_factor": 1.2,
        "breakeven": False, "breakeven_pct": 0.01,
        "partial_tp": False, "partial_tp_fraction": 0.5,
        "htf_timeframe": "", "htf_fast": 9, "htf_slow": 21,
    }
    try:
        bots = {b["symbol"]: b for b in requests.get(f"{base_url}/api/bots", timeout=5).json()}
        if symbol in bots:
            st = bots[symbol].get("state", {})
            params["sl"]               = float(st.get("sl_pct",               params["sl"]))
            params["tp"]               = float(st.get("tp_pct",               params["tp"]))
            params["fast"]             = int(float(st.get("fast_period",       params["fast"])))
            params["slow"]             = int(float(st.get("slow_period",       params["slow"])))
            params["timeframe"]        = st.get("timeframe",                   params["timeframe"])
            params["trailing_sl"]      = st.get("use_trailing_sl", "False").lower() == "true"
            params["trailing_sl_pct"]  = float(st.get("trailing_sl_pct",      params["trailing_sl_pct"]))
            params["sl_cooldown"]      = int(float(st.get("sl_cooldown_candles", params["sl_cooldown"])))
            params["volume_filter"]    = st.get("volume_filter", "False").lower() == "true"
            params["volume_factor"]    = float(st.get("volume_factor",         params["volume_factor"]))
            params["breakeven"]        = st.get("breakeven_enabled", "False").lower() == "true"
            params["breakeven_pct"]    = float(st.get("breakeven_trigger_pct", params["breakeven_pct"]))
            params["partial_tp"]       = st.get("partial_tp_enabled", "False").lower() == "true"
            params["partial_tp_fraction"] = float(st.get("partial_tp_fraction", params["partial_tp_fraction"]))
            params["htf_timeframe"]    = st.get("htf_timeframe",               params["htf_timeframe"])
            params["htf_fast"]         = int(float(st.get("htf_fast",          params["htf_fast"])))
            params["htf_slow"]         = int(float(st.get("htf_slow",          params["htf_slow"])))
    except Exception:
        pass  # Fallback auf Defaults

    if overrides:
        params.update(overrides)

    try:
        resp = requests.post(f"{base_url}/api/bot/start", json=params, timeout=10)
        resp.raise_for_status()
        return {"ok": True, "symbol": symbol, "params": params}
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
