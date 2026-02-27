"""
News-Agent Orchestrator.
Loop: fetch → dedupe → keyword-match → sentiment → DB → telegram alert.
"""
import hashlib
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from news.config import NewsAgentConfig
from news.fetcher import CryptoPanicFetcher, RSSFetcher, GoogleNewsFetcher, TwitterFetcher, NewsItem
from news import sentiment as sent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datenbank-Setup
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash        TEXT    NOT NULL UNIQUE,
    url             TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    body            TEXT    DEFAULT '',
    source          TEXT    NOT NULL,
    published_at    TEXT    NOT NULL,
    coins           TEXT    DEFAULT '',   -- JSON-Array als String
    sentiment_score REAL    DEFAULT 0.0,
    sentiment_label TEXT    DEFAULT 'neutral',
    vader_score     REAL    DEFAULT 0.0,
    textblob_score  REAL    DEFAULT 0.0,
    fetched_at      TEXT    NOT NULL,
    alerted         INTEGER DEFAULT 0    -- 0=nein, 1=ja
);

CREATE TABLE IF NOT EXISTS sentiment_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    score           REAL    NOT NULL,
    source          TEXT    NOT NULL,
    headline_count  INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alert_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    news_event_id   INTEGER NOT NULL,
    telegram_msg_id INTEGER,
    action          TEXT    DEFAULT 'sent',   -- 'sent' | 'dismissed' | 'stopped_bot'
    acted_at        TEXT    NOT NULL
);
"""


def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Keyword-Matching
# ---------------------------------------------------------------------------

def _match_coins(item: NewsItem, cfg: NewsAgentConfig) -> list[str]:
    """
    Ordnet einem NewsItem Coins zu basierend auf coin_keywords-Mapping.
    Wenn die CryptoPanic-API bereits Coins liefert, wird das übernommen.
    """
    if item.coins:
        # Nur Coins behalten die wir auch handeln
        known = set(cfg.coin_keywords.keys())
        return [c for c in item.coins if c in known]

    text = (item.title + " " + item.body).lower()
    matched = []
    for symbol, keywords in cfg.coin_keywords.items():
        if any(kw in text for kw in keywords):
            matched.append(symbol)
    return matched


def _is_relevant(item: NewsItem, cfg: NewsAgentConfig) -> bool:
    """
    Prüft ob ein Artikel relevant ist:
    - Enthält einen der watch_persons / watch_keywords
    - Oder wurde einem der gehandelten Coins zugeordnet
    """
    text = (item.title + " " + item.body).lower()
    for kw in cfg.watch_persons + cfg.watch_keywords:
        if kw.lower() in text:
            return True
    if item.coins:
        known = set(cfg.coin_keywords.keys())
        if any(c in known for c in item.coins):
            return True
    return False


# ---------------------------------------------------------------------------
# Deduplizierung & Altersfilter
# ---------------------------------------------------------------------------

def _already_seen(conn: sqlite3.Connection, url_hash: str, dedupe_hours: int) -> bool:
    """URL-basierte Dedup: gleiche URL innerhalb von dedupe_hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=dedupe_hours)
    row = conn.execute(
        "SELECT id FROM news_events WHERE url_hash = ? AND fetched_at > ?",
        (url_hash, cutoff.isoformat()),
    ).fetchone()
    return row is not None


def _is_too_old(item, max_age_hours: int) -> bool:
    """Filtert Artikel deren published_at älter als max_age_hours ist."""
    try:
        age = datetime.now(timezone.utc) - item.published_at
        return age.total_seconds() > max_age_hours * 3600
    except Exception:
        return False  # Im Zweifel durchlassen


_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "will", "would", "could", "should", "may", "might",
    "it", "its", "this", "that", "as", "s", "not", "no", "new", "says", "say",
}


def _title_words(title: str) -> set[str]:
    """Normalisiert einen Titel zu einer Menge signifikanter Wörter."""
    import re
    words = re.sub(r"[^\w\s]", "", title.lower()).split()
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _title_too_similar(
    conn: sqlite3.Connection,
    title: str,
    hours: int,
    threshold: float,
) -> bool:
    """
    Semantische Dedup: gleiche Story von verschiedenen Outlets.
    Jaccard-Ähnlichkeit der Titelwörter ≥ threshold → Duplikat.
    Vergleicht gegen alle Einträge der letzten `hours` Stunden.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = conn.execute(
        "SELECT title FROM news_events WHERE fetched_at > ?",
        (cutoff.isoformat(),),
    ).fetchall()

    new_words = _title_words(title)
    if not new_words:
        return False

    for (existing_title,) in rows:
        ex_words = _title_words(existing_title)
        if not ex_words:
            continue
        union = new_words | ex_words
        if not union:
            continue
        similarity = len(new_words & ex_words) / len(union)
        if similarity >= threshold:
            return True
    return False


# ---------------------------------------------------------------------------
# Haupt-Agent-Klasse
# ---------------------------------------------------------------------------

class NewsAgent:
    def __init__(self, cfg: NewsAgentConfig, telegram_bot=None):
        self.cfg = cfg
        self.telegram = telegram_bot  # Kann None sein (--dry-run)
        self.conn = _open_db(cfg.db_path)

        self._fetchers = self._build_fetchers()

    def _build_fetchers(self):
        return [
            CryptoPanicFetcher(
                api_key=self.cfg.cryptopanic_api_key,
                max_items=self.cfg.max_articles_per_run,
            ),
            RSSFetcher(
                feed_urls=self.cfg.rss_feeds,
                max_items=self.cfg.max_articles_per_run,
            ),
            GoogleNewsFetcher(
                queries=self.cfg.google_news_queries,
                max_items=20,
            ),
            TwitterFetcher(
                bearer_token=self.cfg.twitter_bearer_token,
                max_results=10,
            ),
        ]

    # ------------------------------------------------------------------
    # Einzel-Lauf
    # ------------------------------------------------------------------

    def run_once(self, dry_run: bool = False) -> int:
        """
        Führt einen kompletten Fetch-Cycle durch.
        Gibt die Anzahl der ausgelösten Alerts zurück.
        """
        logger.info("News-Agent: starte Fetch-Cycle")

        # 1. Parallel fetchen
        all_items: list[NewsItem] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(f.fetch): f.__class__.__name__ for f in self._fetchers}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    items = future.result()
                    all_items.extend(items)
                except Exception as e:
                    logger.error("%s: unerwarteter Fehler: %s", name, e)

        logger.info("Gesamt gefetcht: %d Artikel", len(all_items))

        alerts_sent = 0

        skipped_old = skipped_url = skipped_title = skipped_quality = skipped_irrelevant = 0

        for item in all_items:
            # 2. Qualitätsfilter: Titel zu kurz (Reddit-Posts, Platzhalter etc.)
            if len(item.title.split()) < self.cfg.min_title_words:
                skipped_quality += 1
                continue

            # 3. Altersfilter
            if _is_too_old(item, self.cfg.max_age_hours):
                skipped_old += 1
                continue

            # 3. URL-Deduplizierung
            if _already_seen(self.conn, item.url_hash, self.cfg.dedupe_hours):
                skipped_url += 1
                continue

            # 4. Semantische Titel-Deduplizierung (gleiche Story, andere Quelle)
            if _title_too_similar(
                self.conn, item.title,
                self.cfg.title_dedupe_hours,
                self.cfg.title_dedupe_threshold,
            ):
                skipped_title += 1
                logger.debug("Titel-Duplikat übersprungen: %s", item.title[:60])
                continue

            # 5. Relevanz-Filter + Coin-Matching
            coins = _match_coins(item, self.cfg)
            item.coins = coins
            if not _is_relevant(item, self.cfg):
                skipped_irrelevant += 1
                continue

            # 6. Sentiment-Score berechnen
            result = sent.combined_score(item.text)
            score = result["score"]
            label = result["label"]

            logger.debug(
                "[%s] %s | score=%.3f | coins=%s",
                label.upper(), item.title[:60], score, coins or "allgemein"
            )

            # 7. In DB speichern
            import json
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                self.conn.execute(
                    """INSERT OR IGNORE INTO news_events
                       (url_hash, url, title, body, source, published_at,
                        coins, sentiment_score, sentiment_label,
                        vader_score, textblob_score, fetched_at, alerted)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (
                        item.url_hash, item.url, item.title, item.body,
                        item.source, item.published_at.isoformat(),
                        json.dumps(coins), score, label,
                        result["vader"], result["textblob"], now_iso,
                    ),
                )
                self.conn.commit()
            except sqlite3.IntegrityError:
                continue  # Race-Condition: bereits eingetragen

            # Sentiment-Score pro Symbol in sentiment_scores speichern
            for symbol in (coins if coins else ["MARKET"]):
                self.conn.execute(
                    """INSERT INTO sentiment_scores (symbol, timestamp, score, source, headline_count)
                       VALUES (?,?,?,?,1)""",
                    (symbol, now_iso, score, item.source),
                )
            self.conn.commit()

            # 8. Alert auslösen wenn Score über Schwelle
            if abs(score) >= self.cfg.sentiment_threshold:
                event_id = self.conn.execute(
                    "SELECT id FROM news_events WHERE url_hash = ?", (item.url_hash,)
                ).fetchone()["id"]

                if dry_run:
                    logger.info(
                        "[DRY-RUN] Alert: [%s] %.3f | %s | %s",
                        label.upper(), score, coins or "allgemein", item.title[:80]
                    )
                    alerts_sent += 1
                elif self.telegram:
                    try:
                        self.telegram.send_alert(item, score, label, coins, event_id)
                        self.conn.execute(
                            "UPDATE news_events SET alerted=1 WHERE id=?", (event_id,)
                        )
                        self.conn.commit()
                        alerts_sent += 1
                    except Exception as e:
                        logger.error("Telegram Alert fehlgeschlagen: %s", e)

        logger.info(
            "Fetch-Cycle abgeschlossen | gefetcht=%d | qualität=%d | alt=%d | url-dup=%d | "
            "titel-dup=%d | irrelevant=%d | alerts=%d",
            len(all_items), skipped_quality, skipped_old, skipped_url,
            skipped_title, skipped_irrelevant, alerts_sent,
        )
        return alerts_sent

    # ------------------------------------------------------------------
    # Dauerhafter Loop
    # ------------------------------------------------------------------

    def run_loop(self, dry_run: bool = False):
        """Läuft endlos mit poll_interval_minutes Pause zwischen den Cycles."""
        interval = self.cfg.poll_interval_minutes * 60
        logger.info(
            "News-Agent gestartet. Interval: %d Minuten. dry_run=%s",
            self.cfg.poll_interval_minutes, dry_run
        )
        while True:
            try:
                self.run_once(dry_run=dry_run)
            except Exception as e:
                logger.error("Unerwarteter Fehler im Fetch-Cycle: %s", e, exc_info=True)
            logger.info("Nächster Cycle in %d Minuten...", self.cfg.poll_interval_minutes)
            time.sleep(interval)

    def close(self):
        if self.conn:
            self.conn.close()
