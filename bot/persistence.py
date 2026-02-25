"""
Persistence: SQLite-basierte Zustandsspeicherung.
Tabellen: orders, positions, errors
"""
import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("tradingbot.persistence")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateDB:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        log.info(f"Datenbank geöffnet: {db_path}")

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   TEXT UNIQUE,
                exchange_id TEXT,
                symbol      TEXT,
                side        TEXT,
                amount      REAL,
                price       REAL,
                status      TEXT,
                raw         TEXT,
                created_at  TEXT,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT UNIQUE,
                side        TEXT,
                amount      REAL,
                avg_price   REAL,
                updated_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   TEXT UNIQUE,
                symbol      TEXT,
                amount      REAL,
                entry_price REAL,
                sl_price    REAL,
                tp_price    REAL,
                status      TEXT,    -- 'open' | 'sl_hit' | 'tp_hit' | 'signal_close'
                opened_at   TEXT,
                closed_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS errors (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                context     TEXT,
                message     TEXT,
                occurred_at TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.commit()

    # --- Orders ---

    def upsert_order(self, client_id: str, data: dict):
        now = utcnow()
        raw = json.dumps(data)

        # Kraken Market-Orders haben price=None → average oder cost/filled verwenden
        price = (
            data.get("average")
            or data.get("price")
            or (
                data.get("cost") / data.get("filled")
                if data.get("cost") and data.get("filled")
                else None
            )
        )

        self.conn.execute("""
            INSERT INTO orders (client_id, exchange_id, symbol, side, amount, price, status, raw, created_at, updated_at)
            VALUES (:cid, :eid, :sym, :side, :amt, :price, :status, :raw, :now, :now)
            ON CONFLICT(client_id) DO UPDATE SET
                exchange_id = excluded.exchange_id,
                status      = excluded.status,
                raw         = excluded.raw,
                updated_at  = excluded.updated_at
        """, {
            "cid": client_id,
            "eid": data.get("id"),
            "sym": data.get("symbol"),
            "side": data.get("side"),
            "amt": data.get("amount") or data.get("filled"),
            "price": price,
            "status": data.get("status"),
            "raw": raw,
            "now": now,
        })
        self.conn.commit()

    def get_open_orders(self, symbol: str) -> list:
        cur = self.conn.execute(
            "SELECT * FROM orders WHERE symbol = ? AND status NOT IN ('closed','canceled')",
            (symbol,),
        )
        return [dict(r) for r in cur.fetchall()]

    # --- Positions ---

    def update_position(self, symbol: str, side: str, amount: float, avg_price: float):
        self.conn.execute("""
            INSERT INTO positions (symbol, side, amount, avg_price, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side      = excluded.side,
                amount    = excluded.amount,
                avg_price = excluded.avg_price,
                updated_at= excluded.updated_at
        """, (symbol, side, amount, avg_price, utcnow()))
        self.conn.commit()

    def get_position(self, symbol: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,))
        row = cur.fetchone()
        return dict(row) if row else None

    # --- Trades (SL/TP) ---

    def open_trade(
        self,
        client_id: str,
        symbol: str,
        amount: float,
        entry_price: float,
        sl_price: float,
        tp_price: float,
    ):
        self.conn.execute("""
            INSERT INTO trades (client_id, symbol, amount, entry_price, sl_price, tp_price, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
            ON CONFLICT(client_id) DO NOTHING
        """, (client_id, symbol, amount, entry_price, sl_price, tp_price, utcnow()))
        self.conn.commit()
        log.info(f"Trade geöffnet: {symbol} amount={amount} entry={entry_price:.2f} "
                 f"SL={sl_price:.2f} TP={tp_price:.2f}")

    def get_open_trades(self, symbol: str) -> list:
        cur = self.conn.execute(
            "SELECT * FROM trades WHERE symbol = ? AND status = 'open'",
            (symbol,),
        )
        return [dict(r) for r in cur.fetchall()]

    def close_trade(self, client_id: str, reason: str):
        """reason: 'sl_hit' | 'tp_hit' | 'signal_close'"""
        self.conn.execute(
            "UPDATE trades SET status = ?, closed_at = ? WHERE client_id = ?",
            (reason, utcnow(), client_id),
        )
        self.conn.commit()
        log.info(f"Trade geschlossen: client_id={client_id} reason={reason}")

    def update_trade_sltp(self, client_id: str, sl_price: float, tp_price: float):
        """Aktualisiert SL/TP eines offenen Trades (manuell über Dashboard)."""
        self.conn.execute(
            "UPDATE trades SET sl_price = ?, tp_price = ? WHERE client_id = ? AND status = 'open'",
            (sl_price, tp_price, client_id),
        )
        self.conn.commit()
        log.info(f"SL/TP manuell aktualisiert: client_id={client_id} SL={sl_price} TP={tp_price}")

    # --- Bot State ---

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_state(self, key: str, default: str = "") -> str:
        cur = self.conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def get_all_state(self) -> dict:
        cur = self.conn.execute("SELECT key, value FROM bot_state")
        return {row[0]: row[1] for row in cur.fetchall()}

    # --- Errors ---

    def log_error(self, context: str, message: str):
        self.conn.execute(
            "INSERT INTO errors (context, message, occurred_at) VALUES (?, ?, ?)",
            (context, message, utcnow()),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
