"""Durable state in SQLite (WAL, synchronous=FULL).

v3 rewrote one JSON file on every change without fsync; a power loss could leave a
renamed-but-empty file, and concurrent readers (stats tool) raced the writer. SQLite
gives atomic multi-row transactions, crash durability and safe concurrent reads.

Orders are recorded as an *intent* row BEFORE any network call (write-ahead), keyed by
a client_order_id that is unique per attempt. After a crash, every intent is resolvable
at the broker by that id.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from .broker import Order
from .indicators import ET
from .strategy import PositionView

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,              -- opening | open | closing | closed | void | quarantined
    qty INTEGER NOT NULL DEFAULT 0,    -- shares believed held (entry fills - sell fills)
    entry_qty INTEGER NOT NULL DEFAULT 0,
    planned_qty INTEGER NOT NULL DEFAULT 0,
    entry_price REAL,
    entry_time TEXT,
    hwm_from TEXT,
    atr_d REAL NOT NULL,
    initial_stop REAL,
    stop REAL,
    target REAL,
    hwm REAL,
    be_locked INTEGER NOT NULL DEFAULT 0,
    next_earnings TEXT,
    pending_exit TEXT,                 -- exit reason awaiting execution
    exit_reason TEXT,
    exit_price REAL,
    realized_pnl REAL,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    quarantine_reason TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS positions_status ON positions(status);
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    purpose TEXT NOT NULL,             -- entry | protect | exit
    broker_id TEXT UNIQUE,
    parent_broker_id TEXT,
    side TEXT NOT NULL,
    state TEXT NOT NULL,               -- intent | unknown | absent | rejected | <broker status>
    active INTEGER NOT NULL DEFAULT 1, -- 1 = current order for its purpose
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    snapshot TEXT
);
CREATE INDEX IF NOT EXISTS orders_position ON orders(position_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_et() -> pd.Timestamp:
    return pd.Timestamp.now(tz=ET)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    return str(value)


def order_to_json(o: Order) -> str:
    return json.dumps(asdict(o), default=_json_default)


def order_from_json(text: str) -> Order:
    d = json.loads(text)
    d["legs"] = tuple(order_from_json(json.dumps(leg)) for leg in d.get("legs") or ())
    d["filled_at"] = pd.Timestamp(d["filled_at"]).to_pydatetime() if d.get("filled_at") else None
    return Order(**d)


@dataclass
class Position:
    id: str
    symbol: str
    status: str
    atr_d: float
    created_at: str
    qty: int = 0
    entry_qty: int = 0
    planned_qty: int = 0
    entry_price: float | None = None
    entry_time: str | None = None
    hwm_from: str | None = None
    initial_stop: float | None = None
    stop: float | None = None
    target: float | None = None
    hwm: float | None = None
    be_locked: bool = False
    next_earnings: str | None = None
    pending_exit: str | None = None
    exit_reason: str | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None
    closed_at: str | None = None
    quarantine_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def view(self) -> PositionView:
        assert self.entry_price is not None and self.entry_time and self.hwm_from
        assert self.initial_stop is not None and self.stop is not None
        return PositionView(
            entry_price=self.entry_price, entry_time=pd.Timestamp(self.entry_time).tz_convert(ET),
            hwm_from=pd.Timestamp(self.hwm_from).tz_convert(ET), atr_d=self.atr_d,
            initial_stop=self.initial_stop, stop=self.stop, hwm=self.hwm or self.entry_price,
            be_locked=self.be_locked, target=self.target,
            next_earnings=date.fromisoformat(self.next_earnings) if self.next_earnings else None)


@dataclass
class OrderRow:
    client_order_id: str
    position_id: str
    symbol: str
    purpose: str
    side: str
    state: str
    active: bool
    created_at: str
    updated_at: str
    broker_id: str | None = None
    parent_broker_id: str | None = None
    snapshot: Order | None = None


_POSITION_FIELDS = [f.name for f in fields(Position)]


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._depth = 0

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant transaction: only the outermost block commits."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._db
            except BaseException:
                self._depth -= 1
                if outer:
                    self._db.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._db.execute("COMMIT")

    # ------------------------------------------------------------------ events / kv
    def event(self, kind: str, symbol: str | None = None, **payload: Any) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO events(ts, kind, symbol, payload) VALUES (?,?,?,?)",
                       (now_et().isoformat(), kind, symbol, json.dumps(payload, default=_json_default)))

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, default=_json_default)))

    # ------------------------------------------------------------------ positions
    def new_position(self, symbol: str, atr_d: float, planned_qty: int, meta: dict[str, Any]) -> Position:
        pos = Position(id=uuid.uuid4().hex, symbol=symbol, status="opening", atr_d=atr_d,
                       created_at=now_et().isoformat(), planned_qty=planned_qty, meta=meta)
        self.save_position(pos)
        return pos

    def save_position(self, pos: Position) -> None:
        values = [getattr(pos, name) for name in _POSITION_FIELDS]
        values[_POSITION_FIELDS.index("meta")] = json.dumps(pos.meta, default=_json_default)
        values[_POSITION_FIELDS.index("be_locked")] = int(pos.be_locked)
        placeholders = ",".join("?" for _ in _POSITION_FIELDS)
        updates = ",".join(f"{n}=excluded.{n}" for n in _POSITION_FIELDS if n != "id")
        with self.tx() as db:
            db.execute(f"INSERT INTO positions({','.join(_POSITION_FIELDS)}) VALUES ({placeholders}) "
                       f"ON CONFLICT(id) DO UPDATE SET {updates}", values)

    @staticmethod
    def _position(row: sqlite3.Row) -> Position:
        d = {k: row[k] for k in _POSITION_FIELDS}
        d["meta"] = json.loads(d["meta"] or "{}")
        d["be_locked"] = bool(d["be_locked"])
        return Position(**d)

    def position(self, position_id: str) -> Position:
        with self._lock:
            row = self._db.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            raise KeyError(position_id)
        return self._position(row)

    def positions(self, *statuses: str) -> list[Position]:
        with self._lock:
            if statuses:
                q = f"SELECT * FROM positions WHERE status IN ({','.join('?' for _ in statuses)}) ORDER BY created_at"
                rows = self._db.execute(q, statuses).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM positions ORDER BY created_at").fetchall()
        return [self._position(r) for r in rows]

    def active_positions(self) -> list[Position]:
        return self.positions("opening", "open", "closing", "quarantined")

    def closed_trades(self) -> list[Position]:
        return self.positions("closed")

    # ------------------------------------------------------------------ orders
    def order_intent(self, client_order_id: str, position_id: str, symbol: str, purpose: str, side: str) -> None:
        ts = now_et().isoformat()
        with self.tx() as db:
            db.execute("INSERT INTO orders(client_order_id, position_id, symbol, purpose, side, state, active, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (client_order_id, position_id, symbol, purpose, side, "intent", 1, ts, ts))

    def record_order(self, order: Order, position_id: str, purpose: str, parent_broker_id: str | None = None,
                     active: bool | None = None) -> None:
        """Upsert a broker snapshot (and, recursively, its legs)."""
        ts = now_et().isoformat()
        with self.tx() as db:
            row = db.execute("SELECT client_order_id FROM orders WHERE broker_id=? OR client_order_id=?",
                             (order.id, order.client_order_id)).fetchone()
            if row is None:
                db.execute("INSERT INTO orders(client_order_id, position_id, symbol, purpose, broker_id, "
                           "parent_broker_id, side, state, active, created_at, updated_at, snapshot) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (order.client_order_id, position_id, order.symbol, purpose, order.id, parent_broker_id,
                            order.side, order.status, 1 if active is None else int(active), ts, ts,
                            order_to_json(order)))
            else:
                sets = "broker_id=?, state=?, updated_at=?, snapshot=?"
                args: list[Any] = [order.id, order.status, ts, order_to_json(order)]
                if active is not None:
                    sets += ", active=?"
                    args.append(int(active))
                db.execute(f"UPDATE orders SET {sets} WHERE client_order_id=?", args + [row["client_order_id"]])
            for leg in order.legs:
                self.record_order(leg, position_id, purpose, parent_broker_id=order.id, active=active)

    def set_order_state(self, client_order_id: str, state: str, active: bool | None = None) -> None:
        with self.tx() as db:
            if active is None:
                db.execute("UPDATE orders SET state=?, updated_at=? WHERE client_order_id=?",
                           (state, now_et().isoformat(), client_order_id))
            else:
                db.execute("UPDATE orders SET state=?, active=?, updated_at=? WHERE client_order_id=?",
                           (state, int(active), now_et().isoformat(), client_order_id))

    def deactivate_orders(self, position_id: str, purpose: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE orders SET active=0 WHERE position_id=? AND purpose=?", (position_id, purpose))

    @staticmethod
    def _order_row(row: sqlite3.Row) -> OrderRow:
        return OrderRow(client_order_id=row["client_order_id"], position_id=row["position_id"],
                        symbol=row["symbol"], purpose=row["purpose"], side=row["side"], state=row["state"],
                        active=bool(row["active"]), created_at=row["created_at"], updated_at=row["updated_at"],
                        broker_id=row["broker_id"], parent_broker_id=row["parent_broker_id"],
                        snapshot=order_from_json(row["snapshot"]) if row["snapshot"] else None)

    def orders(self, position_id: str, purpose: str | None = None, active_only: bool = False) -> list[OrderRow]:
        q, args = "SELECT * FROM orders WHERE position_id=?", [position_id]
        if purpose:
            q += " AND purpose=?"
            args.append(purpose)
        if active_only:
            q += " AND active=1"
        with self._lock:
            rows = self._db.execute(q + " ORDER BY created_at, rowid", args).fetchall()
        return [self._order_row(r) for r in rows]

    def next_seq(self, position_id: str) -> int:
        with self.tx():
            key = f"seq:{position_id}"
            value = int(self.get(key, 0)) + 1
            self.put(key, value)
            return value
