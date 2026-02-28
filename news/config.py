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
    # Altersfilter: Artikel älter als N Stunden werden ignoriert
    max_age_hours: int = 48
    # Semantische Dedup: gleiche Story von mehreren Outlets → nur eine Meldung
    title_dedupe_hours: int = 4          # Vergleichsfenster
    title_dedupe_threshold: float = 0.50 # Jaccard-Ähnlichkeit ab der = Duplikat
    # Qualitätsfilter: Artikel mit zu kurzem Titel (Reddit-Posts, Placeholders etc.)
    min_title_words: int = 5             # Mindestanzahl Wörter im Titel
    # Volltext-Crawling via trafilatura (opt-in, langsamer)
    fetch_full_body: bool = False
    # Alert-Aggregation: mehrere Artikel pro Coin werden zu einem Konsens-Alert zusammengefasst
    alert_cooldown_minutes: int = 60   # kein zweiter Alert für denselben Coin innerhalb von N Minuten

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
        # Etablierte Medien
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        # Weitere kostenlose Quellen
        "https://bitcoinmagazine.com/.rss/excerpt/",
        "https://cryptoslate.com/feed/",
        "https://blockworks.co/feed/",
        "https://www.newsbtc.com/feed/",
        "https://cryptonews.com/news/feed/",
        "https://ambcrypto.com/feed/",
        "https://beincrypto.com/feed/",
        "https://cryptobriefing.com/feed/",
        "https://coingape.com/feed/",
        "https://theblock.co/rss.xml",
        # Reddit – Community-Sentiment (tägliche Top-Posts)
        "https://www.reddit.com/r/CryptoCurrency/top.rss?t=day",
        "https://www.reddit.com/r/Bitcoin/top.rss?t=day",
        "https://www.reddit.com/r/ethereum/top.rss?t=day",
        "https://www.reddit.com/r/XRP/top.rss?t=day",
        "https://www.reddit.com/r/cardano/top.rss?t=day",
    ])

    # Google News RSS – Suchbegriffe
    google_news_queries: list = field(default_factory=lambda: [
        "bitcoin",
        "crypto regulation",
        "cryptocurrency hack",
        "ethereum",
        "trump crypto",
        "XRP ripple SEC",
        "crypto ETF approval",
        "DeFi exploit",
        "bitcoin whale",
        "ADA cardano",
        "SNX synthetix",
        "PEPE memecoin",
        "crypto market crash",
        "stablecoin regulation",
    ])
