"""
state.py — Persistencia SQLite asíncrona (aiosqlite).
Tabla trades + tabla events. Recuperación tras reinicio.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import aiosqlite

from .config import Config
from .logger import get_logger
from .models import Event, EventType, Trade, TradeStatus

log = get_logger("state")

# ──────────────────────────────────────────────────────────────────────────────
# Schema SQL
# ──────────────────────────────────────────────────────────────────────────────

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id            TEXT PRIMARY KEY,
    pair                TEXT NOT NULL,
    signal_ts           TEXT,
    signal_data         TEXT,           -- JSON
    entry_order_id      INTEGER,
    entry_price         REAL,
    entry_quantity      REAL,
    entry_fill_ts       TEXT,
    tp_order_id         TEXT,
    sl_order_id         TEXT,
    tp_price            REAL,
    sl_price            REAL,
    tp_trigger_price    REAL,
    sl_trigger_price    REAL,
    exit_price          REAL,
    exit_fill_ts        TEXT,
    exit_type           TEXT,
    pnl_usdt            REAL,
    pnl_pct             REAL,
    fees_usdt           REAL,
    status              TEXT NOT NULL,
    error_message       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
)
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT,
    event_type  TEXT NOT NULL,
    details     TEXT,                   -- JSON
    timestamp   TEXT NOT NULL
)
"""


# ──────────────────────────────────────────────────────────────────────────────
# StateDB
# ──────────────────────────────────────────────────────────────────────────────

class StateDB:
    def __init__(self, cfg: Config):
        self._path = cfg.db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_CREATE_TRADES)
        await self._db.execute(_CREATE_EVENTS)
        await self._db.commit()
        log.info(f"DB inicializada: {self._path}")

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    # ──────────────────────────────────────────────────────────────────
    # Trades
    # ──────────────────────────────────────────────────────────────────

    async def save_trade(self, t: Trade):
        sql = """
        INSERT OR REPLACE INTO trades
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        await self._db.execute(sql, (
            t.trade_id, t.pair, t.signal_ts,
            json.dumps(t.signal_data),
            t.entry_order_id, t.entry_price, t.entry_quantity, t.entry_fill_ts,
            t.tp_order_id, t.sl_order_id,
            t.tp_price, t.sl_price, t.tp_trigger_price, t.sl_trigger_price,
            t.exit_price, t.exit_fill_ts, t.exit_type,
            t.pnl_usdt, t.pnl_pct, t.fees_usdt,
            t.status.value, t.error_message,
            t.created_at, t.updated_at,
        ))
        await self._db.commit()

    async def load_active_trades(self) -> List[Trade]:
        """Recupera trades que no están en estado terminal (para reconciliación)."""
        terminal = (
            TradeStatus.CLOSED.value,
            TradeStatus.NOT_EXECUTED.value,
            TradeStatus.ERROR.value,
        )
        placeholders = ",".join("?" * len(terminal))
        async with self._db.execute(
            f"SELECT * FROM trades WHERE status NOT IN ({placeholders})",
            terminal,
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_trade(r) for r in rows]

    async def load_recent_closed(self, limit: int = 50) -> List[Trade]:
        async with self._db.execute(
            "SELECT * FROM trades WHERE status IN (?,?,?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (TradeStatus.CLOSED.value, TradeStatus.NOT_EXECUTED.value,
             TradeStatus.ERROR.value, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_trade(r) for r in rows]

    async def load_all_trades(self, limit: int = 200) -> List[Trade]:
        async with self._db.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_trade(r) for r in rows]

    async def get_trade(self, trade_id: str) -> Optional[Trade]:
        async with self._db.execute(
            "SELECT * FROM trades WHERE trade_id=?", (trade_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_trade(row) if row else None

    # ──────────────────────────────────────────────────────────────────
    # Events
    # ──────────────────────────────────────────────────────────────────

    async def save_event(self, ev: Event):
        await self._db.execute(
            "INSERT INTO events (trade_id, event_type, details, timestamp) "
            "VALUES (?,?,?,?)",
            (ev.trade_id, ev.event_type, json.dumps(ev.details), ev.timestamp),
        )
        await self._db.commit()

    async def get_trade_events(self, trade_id: str) -> List[Event]:
        async with self._db.execute(
            "SELECT * FROM events WHERE trade_id=? ORDER BY event_id",
            (trade_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in rows]

    async def get_recent_events(self, limit: int = 100) -> List[Event]:
        async with self._db.execute(
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in reversed(await cur.fetchall())]

    async def get_last_events(self, limit: int = 100) -> List[Event]:
        async with self._db.execute(
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de conversión
# ──────────────────────────────────────────────────────────────────────────────

def _row_to_trade(row) -> Trade:
    r = dict(row)
    return Trade(
        trade_id         = r["trade_id"],
        pair             = r["pair"],
        signal_ts        = r["signal_ts"] or "",
        signal_data      = json.loads(r["signal_data"] or "{}"),
        entry_order_id   = r["entry_order_id"],
        entry_price      = r["entry_price"],
        entry_quantity   = r["entry_quantity"],
        entry_fill_ts    = r["entry_fill_ts"],
        tp_order_id      = r["tp_order_id"],
        sl_order_id      = r["sl_order_id"],
        tp_price         = r["tp_price"],
        sl_price         = r["sl_price"],
        tp_trigger_price = r["tp_trigger_price"],
        sl_trigger_price = r["sl_trigger_price"],
        exit_price       = r["exit_price"],
        exit_fill_ts     = r["exit_fill_ts"],
        exit_type        = r["exit_type"],
        pnl_usdt         = r["pnl_usdt"],
        pnl_pct          = r["pnl_pct"],
        fees_usdt        = r["fees_usdt"],
        status           = TradeStatus(r["status"]),
        error_message    = r["error_message"],
        created_at       = r["created_at"],
        updated_at       = r["updated_at"],
    )


def _row_to_event(row) -> Event:
    r = dict(row)
    return Event(
        event_id   = r["event_id"],
        trade_id   = r["trade_id"],
        event_type = r["event_type"],
        details    = json.loads(r["details"] or "{}"),
        timestamp  = r["timestamp"],
    )
