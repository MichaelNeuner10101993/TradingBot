"""
News-Fetcher für verschiedene Quellen.
Alle Fetcher liefern eine einheitliche Liste von NewsItem-Objekten.
"""
import logging
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import requests
import feedparser

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # Sekunden


@dataclass
class NewsItem:
    url: str
    title: str
    body: str           # Leer wenn nur Titel verfügbar
    source: str         # "cryptopanic" | "rss" | "google" | "twitter"
    published_at: datetime
    coins: list = field(default_factory=list)   # ["BTC/EUR", ...] – leer = allgemeiner Markt

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:16]

    @property
    def text(self) -> str:
        """Titel + Body zusammen für Sentiment-Analyse."""
        return f"{self.title}. {self.body}".strip(". ")


# ---------------------------------------------------------------------------
# CryptoPanic
# ---------------------------------------------------------------------------

class CryptoPanicFetcher:
    """
    Offizielle CryptoPanic API (kostenloser API-Key nötig).
    Doku: https://cryptopanic.com/developers/api/
    """
    BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self, api_key: str, max_items: int = 50):
        self.api_key = api_key
        self.max_items = max_items

    def fetch(self) -> list[NewsItem]:
        if not self.api_key:
            logger.debug("CryptoPanic: kein API-Key konfiguriert, übersprungen")
            return []

        items = []
        try:
            resp = requests.get(
                self.BASE_URL,
                params={
                    "auth_token": self.api_key,
                    "kind": "news",
                    "public": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            for post in data.get("results", [])[: self.max_items]:
                published_at = _parse_dt(post.get("published_at", ""))
                coins = [
                    c["code"].upper() + "/EUR"
                    for c in post.get("currencies", [])
                    if c.get("code")
                ]
                items.append(NewsItem(
                    url=post.get("url", ""),
                    title=post.get("title", ""),
                    body="",
                    source="cryptopanic",
                    published_at=published_at,
                    coins=coins,
                ))
        except Exception as e:
            logger.warning("CryptoPanic fetch fehlgeschlagen: %s", e)

        logger.info("CryptoPanic: %d Artikel geholt", len(items))
        return items


# ---------------------------------------------------------------------------
# RSS-Fetcher (CoinTelegraph, Decrypt, CoinDesk, ...)
# ---------------------------------------------------------------------------

class RSSFetcher:
    """Liest beliebige RSS/Atom-Feeds mit feedparser."""

    def __init__(self, feed_urls: list[str], max_items: int = 50):
        self.feed_urls = feed_urls
        self.max_items = max_items

    def fetch(self) -> list[NewsItem]:
        items = []
        for url in self.feed_urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[: self.max_items]:
                    published_at = _parse_feedparser_dt(entry)
                    link = entry.get("link", "")
                    title = entry.get("title", "")
                    summary = entry.get("summary", "") or entry.get("description", "")
                    # HTML-Tags grob entfernen
                    body = _strip_html(summary)[:500]

                    if not link or not title:
                        continue

                    items.append(NewsItem(
                        url=link,
                        title=title,
                        body=body,
                        source="rss",
                        published_at=published_at,
                        coins=[],
                    ))
            except Exception as e:
                logger.warning("RSS fetch fehlgeschlagen (%s): %s", url, e)

        logger.info("RSS: %d Artikel geholt", len(items))
        return items


# ---------------------------------------------------------------------------
# Google News RSS (keine API nötig)
# ---------------------------------------------------------------------------

class GoogleNewsFetcher:
    """
    Nutzt den öffentlichen Google News RSS-Feed.
    Kein API-Key nötig, aber Google kann den Zugang einschränken.
    """
    BASE_URL = "https://news.google.com/rss/search"

    def __init__(self, queries: list[str], max_items: int = 20, lang: str = "en"):
        self.queries = queries
        self.max_items = max_items
        self.lang = lang

    def fetch(self) -> list[NewsItem]:
        items = []
        for query in self.queries:
            try:
                url = f"{self.BASE_URL}?q={quote_plus(query)}&hl={self.lang}&gl=US&ceid=US:en"
                feed = feedparser.parse(url)
                for entry in feed.entries[: self.max_items]:
                    published_at = _parse_feedparser_dt(entry)
                    link = entry.get("link", "")
                    title = entry.get("title", "")
                    if not link or not title:
                        continue
                    items.append(NewsItem(
                        url=link,
                        title=title,
                        body="",
                        source="google",
                        published_at=published_at,
                        coins=[],
                    ))
            except Exception as e:
                logger.warning("Google News fetch fehlgeschlagen (query=%s): %s", query, e)

        logger.info("Google News: %d Artikel geholt", len(items))
        return items


# ---------------------------------------------------------------------------
# Twitter/X (optional – nur wenn Bearer Token gesetzt)
# ---------------------------------------------------------------------------

class TwitterFetcher:
    """
    Twitter/X Basic API v2 – Recent Search.
    Nur aktiv wenn TWITTER_BEARER_TOKEN in .env gesetzt.
    Basic-Tier: ~$100/Monat.
    """
    SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"
    QUERIES = [
        "bitcoin OR btc lang:en -is:retweet",
        "crypto hack OR stolen OR exploit lang:en -is:retweet",
        "trump crypto OR musk crypto lang:en -is:retweet",
    ]

    def __init__(self, bearer_token: str, max_results: int = 10):
        self.bearer_token = bearer_token
        self.max_results = min(max_results, 10)  # Free Tier: max 10 pro Request

    def fetch(self) -> list[NewsItem]:
        if not self.bearer_token:
            logger.debug("Twitter: kein Bearer Token konfiguriert, übersprungen")
            return []

        items = []
        headers = {"Authorization": f"Bearer {self.bearer_token}"}

        for query in self.QUERIES:
            try:
                resp = requests.get(
                    self.SEARCH_URL,
                    headers=headers,
                    params={
                        "query": query,
                        "max_results": self.max_results,
                        "tweet.fields": "created_at,author_id",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()

                for tweet in data.get("data", []):
                    tweet_id = tweet.get("id", "")
                    text = tweet.get("text", "")
                    created_at = _parse_dt(tweet.get("created_at", ""))
                    url = f"https://twitter.com/i/web/status/{tweet_id}"
                    items.append(NewsItem(
                        url=url,
                        title=text[:200],
                        body="",
                        source="twitter",
                        published_at=created_at,
                        coins=[],
                    ))
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    logger.warning("Twitter API: 403 Forbidden – Bearer Token ohne ausreichende Rechte")
                else:
                    logger.warning("Twitter fetch fehlgeschlagen (query=%s): %s", query, e)
            except Exception as e:
                logger.warning("Twitter fetch fehlgeschlagen (query=%s): %s", query, e)

        logger.info("Twitter: %d Tweets geholt", len(items))
        return items


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _parse_dt(s: str) -> datetime:
    """Parst ISO-8601 Strings zu timezone-aware datetime (UTC)."""
    if not s:
        return datetime.now(timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _parse_feedparser_dt(entry) -> datetime:
    """Extrahiert published_at aus einem feedparser-Entry."""
    import time as time_mod
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime.fromtimestamp(time_mod.mktime(t), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _strip_html(text: str) -> str:
    """Entfernt HTML-Tags aus einem String (einfache Variante ohne lxml)."""
    import re
    return re.sub(r"<[^>]+>", " ", text).strip()
