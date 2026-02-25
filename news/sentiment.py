"""
Sentiment-Analyse für News-Texte.
Kombiniert VADER (optimal für kurze Social-Media/News-Texte) mit TextBlob.
Score-Bereich: -1.0 (sehr bearish) bis +1.0 (sehr bullish).
"""
import logging

logger = logging.getLogger(__name__)

# Lazy Imports – nur laden wenn wirklich gebraucht (reduziert Startzeit)
_vader_analyzer = None
_textblob_available = False

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader_available = True
except ImportError:
    _vader_available = False
    logger.warning("vaderSentiment nicht installiert – VADER nicht verfügbar")

try:
    from textblob import TextBlob
    _textblob_available = True
except ImportError:
    logger.warning("textblob nicht installiert – TextBlob nicht verfügbar")


def _get_vader():
    global _vader_analyzer
    if _vader_analyzer is None and _vader_available:
        _vader_analyzer = SentimentIntensityAnalyzer()
    return _vader_analyzer


def vader_score(text: str) -> float:
    """
    Berechnet VADER compound score (-1.0 bis +1.0).
    Gibt 0.0 zurück wenn VADER nicht verfügbar.
    """
    analyzer = _get_vader()
    if analyzer is None or not text:
        return 0.0
    scores = analyzer.polarity_scores(text)
    return round(scores["compound"], 4)


def textblob_score(text: str) -> float:
    """
    Berechnet TextBlob polarity (-1.0 bis +1.0).
    Gibt 0.0 zurück wenn TextBlob nicht verfügbar.
    """
    if not _textblob_available or not text:
        return 0.0
    try:
        blob = TextBlob(text)
        return round(float(blob.sentiment.polarity), 4)
    except Exception as e:
        logger.debug("TextBlob Fehler: %s", e)
        return 0.0


def combined_score(text: str) -> dict:
    """
    Berechnet kombinierten Sentiment-Score.

    Gewichtung: VADER 70% + TextBlob 30%
    Wenn nur eine Methode verfügbar: 100% dieser Methode.

    Returns:
        {
            "score": float,        # -1.0 bis +1.0
            "label": str,          # "bearish" | "neutral" | "bullish"
            "vader": float,
            "textblob": float,
        }
    """
    v = vader_score(text)
    t = textblob_score(text)

    if _vader_available and _textblob_available:
        score = v * 0.7 + t * 0.3
    elif _vader_available:
        score = v
    elif _textblob_available:
        score = t
    else:
        score = 0.0

    score = round(score, 4)
    label = score_to_label(score)

    return {
        "score": score,
        "label": label,
        "vader": v,
        "textblob": t,
    }


def score_to_label(score: float, threshold: float = 0.3) -> str:
    """Konvertiert numerischen Score in kategorisches Label."""
    if score < -threshold:
        return "bearish"
    if score > threshold:
        return "bullish"
    return "neutral"
