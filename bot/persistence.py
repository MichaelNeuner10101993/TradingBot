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

        # Migration: pyramid_count zu trades (bestehende DBs)
        try:
            self.conn.execute("ALTER TABLE trades ADD COLUMN pyramid_count INTEGER DEFAULT 0")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass  # Spalte existiert bereits

        # Migration: supervisor_log (bestehende DBs)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS supervisor_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT,
                regime        TEXT,
                adx           REAL,
                atr_pct       REAL,
                strategy_name TEXT,
                fast          INTEGER,
                slow          INTEGER,
                sim_pnl       REAL,
                num_trades    INTEGER,
                source        TEXT     -- 'own' | 'cross:BTC' etc.
            );
        """)

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

    def update_trade_pyramid(
        self,
        client_id: str,
        new_amount: float,
        new_entry: float,
        new_sl: float,
        new_tp: float,
    ):
        """Aktualisiert Trade nach Pyramid-Kauf (Menge, Avg-Entry, SL/TP, Zähler)."""
        self.conn.execute(
            """UPDATE trades
               SET amount = ?, entry_price = ?, sl_price = ?, tp_price = ?,
                   pyramid_count = pyramid_count + 1
               WHERE client_id = ? AND status = 'open'""",
            (new_amount, new_entry, new_sl, new_tp, client_id),
        )
        self.conn.commit()
        log.info(
            f"Pyramid-Trade aktualisiert: {client_id[:12]}… "
            f"amount={new_amount:.6f} entry={new_entry:.4f} SL={new_sl:.4f} TP={new_tp:.4f}"
        )

    def update_trade_sltp(self, client_id: str, sl_price: float, tp_price: float):
        """Aktualisiert SL/TP eines offenen Trades (manuell über Dashboard)."""
        self.conn.execute(
            "UPDATE trades SET sl_price = ?, tp_price = ? WHERE client_id = ? AND status = 'open'",
            (sl_price, tp_price, client_id),
        )
        self.conn.commit()
        log.info(f"SL/TP manuell aktualisiert: client_id={client_id} SL={sl_price} TP={tp_price}")

    # --- Supervisor Log ---

    def log_supervisor_cycle(
        self,
        regime: str,
        adx: float,
        atr_pct: float,
        strategy_name: str,
        fast: int,
        slow: int,
        sim_pnl: float,
        num_trades: int,
        source: str = "own",
    ):
        """Speichert einen Supervisor-Durchlauf in supervisor_log (append-only)."""
        self.conn.execute(
            """INSERT INTO supervisor_log
               (timestamp, regime, adx, atr_pct, strategy_name, fast, slow, sim_pnl, num_trades, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (utcnow(), regime, adx, atr_pct, strategy_name, fast, slow, sim_pnl, num_trades, source),
        )
        self.conn.commit()

    def get_supervisor_log(self, limit: int = 20) -> list:
        """Gibt die letzten `limit` Einträge aus supervisor_log zurück (neueste zuerst)."""
        cur = self.conn.execute(
            "SELECT * FROM supervisor_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

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
