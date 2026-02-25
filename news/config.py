"""
Konfiguration des News-Agenten.
Folgt dem gleichen Muster wie bot/config.py (dataclasses + dotenv).
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class NewsAgentConfig:
    # Polling
    poll_interval_minutes: int = 10
    sentiment_threshold: float = 0.5     # Ab |score| > threshold → Alert
    dedupe_hours: int = 24               # Kein doppelter Alert für gleiche URL
    max_articles_per_run: int = 50

    # Pfade
    db_path: str = "db/news.db"
    log_dir: str = "logs"
    log_level: str = "INFO"

    # Web-API des Trading-Bots (für start/stop)
    web_api_base: str = "http://localhost:5001"

    # Telegram
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "")
    )

    # API-Keys
    cryptopanic_api_key: str = field(
        default_factory=lambda: os.getenv("CRYPTOPANIC_API_KEY", "")
    )
    twitter_bearer_token: str = field(
        default_factory=lambda: os.getenv("TWITTER_BEARER_TOKEN", "")
    )

    # Watchlist – Personen und Begriffe die immer relevant sind
    watch_persons: list = field(default_factory=lambda: [
        "trump", "musk", "elon", "trump jr", "melania",
    ])
    watch_keywords: list = field(default_factory=lambda: [
        "darknet", "hack", "hacked", "stolen", "ban", "banned",
        "sec", "regulation", "arrest", "sanctions", "exploit",
        "rug pull", "scam", "fraud", "seized", "crackdown",
        "etf", "approval", "rejected", "delistment", "delisted",
    ])

    # Coin-Mapping: welche News betrifft welchen Bot
    coin_keywords: dict = field(default_factory=lambda: {
        "BTC/EUR":   ["bitcoin", "btc"],
        "ETH/EUR":   ["ethereum", "eth"],
        "XRP/EUR":   ["ripple", "xrp"],
        "SNX/EUR":   ["synthetix", "snx"],
        "TRUMP/EUR": ["trump", "melania", "trump jr", "maga"],
        "PEPE/EUR":  ["pepe", "meme coin", "memecoin"],
    })

    # RSS-Feed-URLs
    rss_feeds: list = field(default_factory=lambda: [
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ])

    # Google News RSS – Suchbegriffe
    google_news_queries: list = field(default_factory=lambda: [
        "bitcoin",
        "crypto regulation",
        "cryptocurrency hack",
        "ethereum",
        "trump crypto",
    ])
