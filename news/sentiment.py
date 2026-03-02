"""
Sentiment-Analyse für News-Texte.
Kombiniert VADER (optimal für kurze Social-Media/News-Texte) mit TextBlob.
Score-Bereich: -1.0 (sehr bearish) bis +1.0 (sehr bullish).
"""
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Krypto-spezifisches VADER-Lexikon
# VADER wurde auf allgemeines Englisch trainiert und kennt Krypto-Jargon nicht.
# Scores: -4.0 (sehr negativ) bis +4.0 (sehr positiv), Compound bleibt in -1..+1
# ---------------------------------------------------------------------------
_CRYPTO_LEXICON: dict[str, float] = {
    # Stark bullish
    "bullish":       2.5, "mooning":      2.5, "moon":         2.0,
    "breakout":      2.2, "rally":        2.0, "rallying":     2.0,
    "soar":          2.2, "soaring":      2.2, "surge":        2.2,
    "surging":       2.2, "skyrocket":    2.5, "skyrocketing": 2.5,
    "hodl":          1.2, "accumulate":   1.5, "accumulating": 1.5,
    "adoption":      1.8, "approved":     2.0, "approval":     1.8,
    "upgrade":       1.5, "partnership":  1.2, "bullrun":      2.8,
    "institutional": 1.5, "mainstream":   1.3, "breakout":     2.2,
    "outperform":    1.8, "outperforming":1.8, "pumping":      1.5,
    "ath":           2.0, "all-time-high":2.0, "new high":     2.0,
    # Stark bearish
    "bearish":      -2.5, "rekt":        -2.8, "dump":        -1.8,
    "dumping":      -2.0, "dumped":      -2.0, "crash":       -2.5,
    "crashing":     -2.5, "plunge":      -2.2, "plunging":    -2.2,
    "collapse":     -2.5, "collapsing":  -2.5, "meltdown":    -2.8,
    "rugpull":      -3.5, "rug pull":    -3.5, "rug":         -2.5,
    "scam":         -2.8, "fraud":       -2.8, "fraudulent":  -2.8,
    "hack":         -2.5, "hacked":      -2.8, "stolen":      -2.5,
    "exploit":      -2.8, "exploited":   -2.8, "vulnerability":-1.8,
    "ban":          -2.0, "banned":      -2.2, "crackdown":   -2.0,
    "seized":       -2.2, "arrest":      -2.2, "arrested":    -2.2,
    "sanctions":    -2.0, "fud":         -1.5, "panic":       -2.0,
    "liquidated":   -2.2, "liquidation": -2.0, "insolvent":   -3.0,
    "bankruptcy":   -3.0, "bankrupt":    -3.0, "delisted":    -2.5,
    "delistment":   -2.5, "rejected":    -2.2, "rejection":   -2.2,
    "shutdown":     -2.0, "shutting down":-2.0,"halted":      -1.8,
    "contagion":    -2.5, "selloff":     -2.0, "sell-off":    -2.0,
    # Kontext-abhängig aber oft falsch klassifiziert
    "correction":   -1.0, "dip":         -0.8, "volatile":    -0.5,
    "volatility":   -0.5, "resistance":  -0.3, "support":      0.3,
}

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
        _vader_analyzer.lexicon.update(_CRYPTO_LEXICON)
        logger.debug("VADER: Krypto-Lexikon geladen (%d Einträge)", len(_CRYPTO_LEXICON))
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
