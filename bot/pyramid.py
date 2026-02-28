"""
Pyramid-Modul: Prüft ob Nachkauf-Bedingungen (Pyramiding) erfüllt sind.

Ein Pyramid-Kauf (Nachkauf) wird ausgelöst wenn alle Bedingungen zutreffen:
  1. Offene Position ≥ PYRAMID_PROFIT_MIN_PCT im Plus
  2. Regime in PYRAMID_ALLOWED_REGIMES (TREND oder SIDEWAYS, nicht VOLATILE)
  3. pyramid_count des Trades = 0 (max. 1 Nachkauf pro Trade)
  4. Aktuelle News-Sentiment für das Symbol ≥ PYRAMID_NEWS_THRESHOLD
     und max. PYRAMID_NEWS_MAX_AGE_H Stunden alt

Nachkauf-Größe: PYRAMID_SIZE_FRACTION × normale Positionsgröße (25%)
"""
import sqlite3
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("tradingbot.pyramid")

PYRAMID_PROFIT_MIN_PCT  = 1.5          # Position muss min. 1.5% im Plus sein
PYRAMID_NEWS_THRESHOLD  = 0.4          # Sentiment-Score >= 0.4 (bullish)
PYRAMID_NEWS_MAX_AGE_H  = 4            # News max. 4 Stunden alt
PYRAMID_SIZE_FRACTION   = 0.25         # 25% der normalen Positionsgröße
PYRAMID_MAX_PER_TRADE   = 1            # Max. 1 Nachkauf pro Trade
PYRAMID_ALLOWED_REGIMES = ("TREND", "SIDEWAYS")


def get_recent_sentiment(news_db_path: str, symbol: str, max_age_hours: int = PYRAMID_NEWS_MAX_AGE_H) -> float | None:
    """
    Holt den durchschnittlichen Sentiment-Score für `symbol` aus den letzten
    `max_age_hours` Stunden aus der news.db (sentiment_scores-Tabelle).

    Gibt None zurück wenn keine aktuellen News vorhanden oder DB nicht erreichbar.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{news_db_path}?mode=ro", uri=True)
        cur  = conn.execute(
            "SELECT AVG(score), COUNT(*) FROM sentiment_scores WHERE symbol = ? AND timestamp >= ?",
            (symbol, cutoff),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[1] and row[1] > 0:
            score = float(row[0])
            log.debug(f"Sentiment {symbol}: {score:.3f} ({row[1]} Artikel der letzten {max_age_hours}h)")
            return score
        log.debug(f"Keine aktuellen News für {symbol} (letzten {max_age_hours}h)")
        return None
    except Exception as e:
        log.debug(f"get_recent_sentiment Fehler: {e}")
        return None


def should_pyramid(
    trade: dict,
    last_price: float,
    regime: str,
    news_db_path: str,
    symbol: str,
    min_profit_pct: float = PYRAMID_PROFIT_MIN_PCT,
    news_threshold: float = PYRAMID_NEWS_THRESHOLD,
    max_age_hours:  int   = PYRAMID_NEWS_MAX_AGE_H,
) -> tuple[bool, str]:
    """
    Prüft ob ein Pyramid-Nachkauf ausgelöst werden soll.
    Gibt (True, Begründungstext) oder (False, Ablehnungsgrund) zurück.
    """
    # 1. Regime-Check
    if regime not in PYRAMID_ALLOWED_REGIMES:
        return False, f"Regime '{regime}' nicht erlaubt (nur {PYRAMID_ALLOWED_REGIMES})"

    # 2. Maximale Pyramids pro Trade
    pyramid_count = int(trade.get("pyramid_count") or 0)
    if pyramid_count >= PYRAMID_MAX_PER_TRADE:
        return False, f"Max. Nachkäufe bereits erreicht ({pyramid_count}/{PYRAMID_MAX_PER_TRADE})"

    # 3. Profit-Check: Position muss im Plus liegen
    entry = float(trade.get("entry_price") or 0)
    if not entry:
        return False, "Kein Entry-Preis im Trade"
    profit_pct = (last_price - entry) / entry * 100
    if profit_pct < min_profit_pct:
        return False, f"Profit {profit_pct:.2f}% < {min_profit_pct}% Minimum"

    # 4. News-Sentiment prüfen
    sentiment = get_recent_sentiment(news_db_path, symbol, max_age_hours)
    if sentiment is None:
        return False, f"Keine aktuellen News für {symbol} (letzten {max_age_hours}h)"
    if sentiment < news_threshold:
        return False, f"Sentiment {sentiment:.3f} < {news_threshold} (nicht bullish genug)"

    return True, f"Profit={profit_pct:.2f}% Sentiment={sentiment:.3f} Regime={regime}"
