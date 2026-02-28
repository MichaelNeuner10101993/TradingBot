#!/usr/bin/env python3
"""
news_agent.py – Entry Point des News-Agenten.

Verwendung:
  python news_agent.py                  # Normaler Betrieb (Loop)
  python news_agent.py --dry-run        # Fetch + Log, kein Telegram
  python news_agent.py --test-telegram  # Sendet Test-Nachricht, dann Exit
  python news_agent.py --once           # Einmaliger Cycle, dann Exit
"""
import argparse
import logging
import os
import sqlite3
import sys
import threading

# Arbeitsverzeichnis auf das Projektverzeichnis setzen (für relative Pfade)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from news.config import NewsAgentConfig
from news.agent import NewsAgent, _open_db


def _setup_logging(cfg: NewsAgentConfig):
    os.makedirs(cfg.log_dir, exist_ok=True)
    log_file = os.path.join(cfg.log_dir, "news_agent.log")

    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    # Token-URLs nicht ins Journal loggen
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="TradingBot News-Agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetcht Quellen und loggt, aber sendet keine Telegram-Nachrichten",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Sendet eine Test-Nachricht via Telegram und beendet sich",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Führt einen einzigen Fetch-Cycle durch und beendet sich",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="MINUTEN",
        help="Poll-Interval in Minuten (überschreibt Config-Wert)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        metavar="SCORE",
        help="Sentiment-Schwelle für Alerts (0.0-1.0, überschreibt Config-Wert)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        metavar="PFAD",
        help="Pfad zur news.db (überschreibt Config-Wert)",
    )
    args = parser.parse_args()

    # Konfiguration laden
    cfg = NewsAgentConfig()
    if args.interval:
        cfg.poll_interval_minutes = args.interval
    if args.threshold is not None:
        cfg.sentiment_threshold = args.threshold
    if args.db:
        cfg.db_path = args.db

    # Logging einrichten
    _setup_logging(cfg)
    logger = logging.getLogger("news_agent")

    logger.info("=" * 60)
    logger.info("TradingBot News-Agent startet")
    logger.info("dry_run=%s, once=%s, test_telegram=%s", args.dry_run, args.once, args.test_telegram)
    logger.info("DB: %s | Interval: %dmin | Threshold: %.2f",
                cfg.db_path, cfg.poll_interval_minutes, cfg.sentiment_threshold)
    logger.info("=" * 60)

    # Prüfen ob Konfiguration vollständig
    if not args.dry_run and not args.test_telegram:
        if not cfg.telegram_bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN nicht gesetzt – Alerts werden nicht gesendet")
        if not cfg.telegram_chat_id:
            logger.warning("TELEGRAM_CHAT_ID nicht gesetzt – Alerts werden nicht gesendet")

    # Datenbank öffnen
    os.makedirs(os.path.dirname(cfg.db_path) if os.path.dirname(cfg.db_path) else ".", exist_ok=True)
    db_conn = _open_db(cfg.db_path)

    # Telegram-Bot initialisieren (nur wenn nicht dry-run)
    telegram = None
    telegram_thread = None

    if not args.dry_run and cfg.telegram_bot_token and cfg.telegram_chat_id:
        try:
            from news.telegram_bot import TelegramNewsBot
            telegram = TelegramNewsBot(cfg, db_conn)
            telegram.start()

            if args.test_telegram:
                logger.info("Sende Test-Nachricht...")
                telegram.send_test_message()
                logger.info("Test-Nachricht gesendet. Beende.")
                return

            # Polling in eigenem Thread starten
            telegram_thread = threading.Thread(
                target=telegram.run_polling,
                daemon=True,
                name="telegram-polling",
            )
            telegram_thread.start()

        except ImportError:
            logger.error("python-telegram-bot nicht installiert – bitte `pip install python-telegram-bot`")
            if args.test_telegram:
                sys.exit(1)
    elif args.test_telegram:
        logger.error("--test-telegram benötigt TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID in .env")
        sys.exit(1)

    # Agent starten
    agent = NewsAgent(cfg, telegram_bot=telegram)

    try:
        if args.once:
            count = agent.run_once(dry_run=args.dry_run)
            logger.info("Einmaliger Cycle abgeschlossen. Alerts: %d", count)
        else:
            agent.run_loop(dry_run=args.dry_run)
    except KeyboardInterrupt:
        logger.info("News-Agent durch Benutzer gestoppt (Ctrl+C)")
    finally:
        agent.close()
        if telegram:
            telegram.stop()


if __name__ == "__main__":
    main()
