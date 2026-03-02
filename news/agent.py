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
from news.fetcher import (CryptoPanicFetcher, RSSFetcher, GoogleNewsFetcher,
                           TwitterFetcher, FearGreedFetcher, CoinGeckoTrendingFetcher,
                           NewsItem)
from news import sentiment as sent

logger = logging.getLogger(__name__)

# Quellen-Gewichtung für Konsens-Score.
# Höher = verlässlicheres Sentiment-Signal.
# coingecko=0: Trending-Daten sind kein Sentiment → komplett raus aus Alerts.
SOURCE_WEIGHTS: dict[str, float] = {
    "fear_greed":  2.5,   # Echter Marktdaten-Index
    "cryptopanic": 2.0,   # Kuratierte Krypto-News
    "rss":         1.0,   # Etablierte Medien (CoinTelegraph, Decrypt, …)
    "google":      0.7,   # Breite Abdeckung, weniger Krypto-spezifisch
    "twitter":     0.6,   # Social-Media-Rauschen
    "coingecko":   0.0,   # Popularität ≠ Sentiment → nicht in Alerts
}


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

CREATE TABLE IF NOT EXISTS news_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_news_fetched_at
    ON news_events(fetched_at);
CREATE INDEX IF NOT EXISTS idx_sentiment_symbol_ts
    ON sentiment_scores(symbol, timestamp);
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
        # Letzter Alert-Zeitstempel pro Coin (in-memory, resets bei Neustart)
        self._last_alert: dict[str, datetime] = {}
        # Gespeichertes Alert-Interval aus DB laden (falls via Telegram geändert)
        row = self.conn.execute(
            "SELECT value FROM news_settings WHERE key='alert_cooldown_minutes'"
        ).fetchone()
        if row:
            self.cfg.alert_cooldown_minutes = int(row["value"])
            logger.info("Alert-Cooldown aus DB geladen: %d min", self.cfg.alert_cooldown_minutes)

    def _build_fetchers(self):
        return [
            CryptoPanicFetcher(
                api_key=self.cfg.cryptopanic_api_key,
                max_items=self.cfg.max_articles_per_run,
            ),
            RSSFetcher(
                feed_urls=self.cfg.rss_feeds,
                max_items=self.cfg.max_articles_per_run,
                fetch_full_body=self.cfg.fetch_full_body,
            ),
            GoogleNewsFetcher(
                queries=self.cfg.google_news_queries,
                max_items=20,
            ),
            TwitterFetcher(
                bearer_token=self.cfg.twitter_bearer_token,
                max_results=10,
            ),
            FearGreedFetcher(),
            CoinGeckoTrendingFetcher(),
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
        # Sammelt Alert-Kandidaten pro Coin → am Ende als Konsens-Alert senden
        pending_alerts: dict[str, list] = {}

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

            # Fear & Greed: direkter Score-Override (VADER interpretiert den Titel falsch)
            if item.source == "fear_greed":
                import re as _re
                m = _re.search(r"Index: (\d+)", item.title)
                if m:
                    raw_val = int(m.group(1))
                    score   = round((raw_val - 50) / 50, 3)
                    label   = sent.score_to_label(score, threshold=0.2)
                    result  = {"score": score, "label": label, "vader": score, "textblob": score}

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

            # 8. Alert-Kandidat sammeln wenn Score über Schwelle
            # coingecko = Trending-Daten, kein Sentiment → kein Alert
            if abs(score) >= self.cfg.sentiment_threshold and item.source != "coingecko":
                event_id = self.conn.execute(
                    "SELECT id FROM news_events WHERE url_hash = ?", (item.url_hash,)
                ).fetchone()["id"]

                # Primär-Coin für Gruppierung (erster Treffer oder "MARKET")
                primary = coins[0] if coins else "MARKET"
                pending_alerts.setdefault(primary, []).append({
                    "item": item, "score": score, "label": label,
                    "coins": coins, "event_id": event_id,
                })

        # 9. Gesammelte Alerts als Konsens pro Coin senden
        alerts_sent += self._flush_alerts(pending_alerts, dry_run)

        logger.info(
            "Fetch-Cycle abgeschlossen | gefetcht=%d | qualität=%d | alt=%d | url-dup=%d | "
            "titel-dup=%d | irrelevant=%d | alerts=%d",
            len(all_items), skipped_quality, skipped_old, skipped_url,
            skipped_title, skipped_irrelevant, alerts_sent,
        )
        return alerts_sent

    # ------------------------------------------------------------------
    # Alert-Aggregation
    # ------------------------------------------------------------------

    def _flush_alerts(self, pending: dict, dry_run: bool) -> int:
        """
        Sendet für jeden Coin einen einzigen Konsens-Alert.
        Einzelartikel → send_alert() (bisheriges Format).
        Mehrere Artikel → send_aggregated_alert() mit Konsens-Score.
        Cooldown: kein zweiter Alert für denselben Coin innerhalb N Minuten.
        """
        if not pending:
            return 0

        alerts_sent = 0
        now = datetime.now(timezone.utc)
        cooldown = timedelta(minutes=self.cfg.alert_cooldown_minutes)

        for coin, articles in pending.items():
            # Cooldown-Check
            last = self._last_alert.get(coin)
            if last and (now - last) < cooldown:
                remaining = int((cooldown - (now - last)).total_seconds() / 60)
                logger.info(
                    "Alert-Cooldown %s: nächster Alert frühestens in %d min", coin, remaining
                )
                continue

            if dry_run:
                for a in articles:
                    logger.info(
                        "[DRY-RUN] Alert [%s] %.3f | %s | %s",
                        a["label"].upper(), a["score"], coin, a["item"].title[:60]
                    )
                self._last_alert[coin] = now
                alerts_sent += 1
                continue

            if not self.telegram:
                continue

            try:
                if len(articles) == 1:
                    a = articles[0]
                    self.telegram.send_alert(a["item"], a["score"], a["label"], a["coins"], a["event_id"])
                    self.conn.execute("UPDATE news_events SET alerted=1 WHERE id=?", (a["event_id"],))
                else:
                    # Gewichteter Konsens: verlässlichere Quellen haben mehr Einfluss
                    weighted_sum = sum(
                        a["score"] * SOURCE_WEIGHTS.get(a["item"].source, 1.0)
                        for a in articles
                    )
                    total_weight = sum(
                        SOURCE_WEIGHTS.get(a["item"].source, 1.0)
                        for a in articles
                    )
                    consensus = weighted_sum / total_weight if total_weight > 0 else 0.0
                    consensus_label = sent.score_to_label(consensus)
                    self.telegram.send_aggregated_alert(coin, articles, consensus, consensus_label)
                    for a in articles:
                        self.conn.execute("UPDATE news_events SET alerted=1 WHERE id=?", (a["event_id"],))

                self.conn.commit()
                self._last_alert[coin] = now
                alerts_sent += 1
            except Exception as e:
                logger.error("Telegram Alert fehlgeschlagen (%s): %s", coin, e)

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
