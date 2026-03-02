"""
Gemeinsamer Candle-Cache für Supervisor und Optimizer.
Speichert OHLCV-Daten pro Symbol in db/candles.db.
"""
import sqlite3
from pathlib import Path

CANDLES_DB  = "db/candles.db"
MAX_CANDLES = 8640  # 30 Tage × 288 Candles/Tag bei 5min

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol    TEXT    NOT NULL,
    timeframe TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts)
);
"""


def open_db(db_path: str = CANDLES_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_candles(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    candles: list,
) -> int:
    """
    Fügt neue Candles ein (ignoriert Duplikate via INSERT OR IGNORE).
    Candle-Format: [ts_ms, open, high, low, close, volume]
    Prunt den Cache auf MAX_CANDLES pro Symbol+Timeframe.
    Gibt Anzahl tatsächlich eingefügter Candles zurück.
    """
    inserted = 0
    for c in candles:
        cur = conn.execute(
            "INSERT OR IGNORE INTO candles (symbol, timeframe, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, timeframe, int(c[0]), c[1], c[2], c[3], c[4], c[5]),
        )
        inserted += cur.rowcount
    conn.commit()

    # Cache begrenzen: älteste Einträge entfernen
    conn.execute(
        """DELETE FROM candles WHERE symbol=? AND timeframe=? AND ts NOT IN (
               SELECT ts FROM candles WHERE symbol=? AND timeframe=?
               ORDER BY ts DESC LIMIT ?
           )""",
        (symbol, timeframe, symbol, timeframe, MAX_CANDLES),
    )
    conn.commit()
    return inserted


def count_candles(conn: sqlite3.Connection, symbol: str, timeframe: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE symbol=? AND timeframe=?",
        (symbol, timeframe),
    ).fetchone()
    return row[0] if row else 0


def load_candles(
    conn: sqlite3.Connection,
    symbol: str,
    timeframe: str,
    limit: int = 500,
) -> list:
    """
    Lädt die letzten `limit` Candles aus dem Cache.
    Rückgabe im CCXT-Format: [[ts, open, high, low, close, volume], ...]
    älteste Candle zuerst (für Indikator-Berechnung).
    """
    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
        (symbol, timeframe, limit),
    ).fetchall()
    return [list(r) for r in reversed(rows)]
