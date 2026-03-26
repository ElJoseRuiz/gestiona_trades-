"""
state.py - Persistencia SQLite asincrona (aiosqlite).
Tabla trades + tabla paper_trades + tabla events. Recuperacion tras reinicio.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import aiosqlite

from .config import Config
from .logger import get_logger
from .models import Event, Trade, TradeStatus

log = get_logger("state")
STATE_VERSION = "0.16"


_CREATE_TRADE_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {table_name} (
    trade_id            TEXT PRIMARY KEY,
    pair                TEXT NOT NULL,
    signal_ts           TEXT,
    signal_data         TEXT,
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
    updated_at          TEXT NOT NULL,
    timeout_triggered   INTEGER NOT NULL DEFAULT 0,
    reconciled          INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_TRADES = _CREATE_TRADE_TABLE_TEMPLATE.format(table_name="trades")
_CREATE_PAPER_TRADES = _CREATE_TRADE_TABLE_TEMPLATE.format(table_name="paper_trades")

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT,
    event_type  TEXT NOT NULL,
    details     TEXT,
    timestamp   TEXT NOT NULL
)
"""


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
        await self._db.execute(_CREATE_PAPER_TRADES)
        await self._db.execute(_CREATE_EVENTS)
        await self._db.commit()
        log.info(f"DB inicializada: {self._path}")

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def save_trade(self, trade: Trade):
        await self._save_trade_to_table("trades", trade)

    async def save_paper_trade(self, trade: Trade):
        await self._save_trade_to_table("paper_trades", trade)

    async def _save_trade_to_table(self, table: str, trade: Trade):
        sql = f"""
            INSERT OR REPLACE INTO {table} (
                trade_id, pair, status, signal_ts,
                entry_order_id, entry_quantity, entry_price, entry_fill_ts,
                tp_order_id, tp_trigger_price, tp_price,
                sl_order_id, sl_trigger_price, sl_price,
                exit_price, exit_fill_ts, exit_type,
                pnl_pct, pnl_usdt, fees_usdt,
                error_message, signal_data,
                created_at, updated_at, timeout_triggered, reconciled
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?
            )
        """
        await self._db.execute(sql, (
            trade.trade_id,
            trade.pair,
            trade.status.value,
            trade.signal_ts,
            trade.entry_order_id,
            trade.entry_quantity,
            trade.entry_price,
            trade.entry_fill_ts,
            trade.tp_order_id,
            trade.tp_trigger_price,
            trade.tp_price,
            trade.sl_order_id,
            trade.sl_trigger_price,
            trade.sl_price,
            trade.exit_price,
            trade.exit_fill_ts,
            trade.exit_type,
            trade.pnl_pct,
            trade.pnl_usdt,
            trade.fees_usdt,
            trade.error_message,
            json.dumps(trade.signal_data),
            trade.created_at,
            trade.updated_at,
            1 if trade.timeout_triggered else 0,
            1 if getattr(trade, "reconciled", False) else 0,
        ))
        await self._db.commit()

    async def load_active_trades(self) -> List[Trade]:
        return await self._load_active_from_table("trades", source="real")

    async def load_active_paper_trades(self) -> List[Trade]:
        return await self._load_active_from_table("paper_trades", source="paper")

    async def _load_active_from_table(self, table: str, source: str) -> List[Trade]:
        terminal = (
            TradeStatus.CLOSED.value,
            TradeStatus.NOT_EXECUTED.value,
            TradeStatus.ERROR.value,
        )
        placeholders = ",".join("?" * len(terminal))
        async with self._db.execute(
            f"SELECT * FROM {table} WHERE status NOT IN ({placeholders})",
            terminal,
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_trade(r, source=source) for r in rows]

    async def load_recent_closed(self, limit: int = 50) -> List[Trade]:
        async with self._db.execute(
            "SELECT * FROM trades WHERE status IN (?,?,?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (
                TradeStatus.CLOSED.value,
                TradeStatus.NOT_EXECUTED.value,
                TradeStatus.ERROR.value,
                limit,
            ),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_trade(r, source="real") for r in rows]

    async def load_all_trades(self, limit: int = 200) -> List[Trade]:
        active_real = await self._load_active_from_table("trades", source="real")
        active_paper = await self._load_active_from_table("paper_trades", source="paper")
        active_trades = active_real + active_paper
        active_ids = {t.trade_id for t in active_trades}

        recent_real = await self._load_terminal_from_table("trades", limit=limit, source="real")
        recent_paper = await self._load_terminal_from_table("paper_trades", limit=limit, source="paper")
        closed_candidates = [t for t in (recent_real + recent_paper) if t.trade_id not in active_ids]
        closed_candidates.sort(
            key=lambda t: (t.exit_fill_ts or t.updated_at or t.created_at or ""),
            reverse=True,
        )
        selected_closed = closed_candidates[:limit]

        merged = active_trades + selected_closed
        merged.sort(key=lambda t: (t.updated_at or t.created_at or ""), reverse=True)
        return merged

    async def _load_terminal_from_table(self,
                                        table: str,
                                        limit: int,
                                        source: str) -> List[Trade]:
        terminal = (
            TradeStatus.CLOSED.value,
            TradeStatus.NOT_EXECUTED.value,
            TradeStatus.ERROR.value,
        )
        placeholders = ",".join("?" * len(terminal))
        sql = (
            f"SELECT * FROM {table} "
            f"WHERE status IN ({placeholders}) "
            "ORDER BY COALESCE(exit_fill_ts, updated_at, created_at) DESC LIMIT ?"
        )
        async with self._db.execute(sql, (*terminal, limit)) as cur:
            rows = await cur.fetchall()
        return [_row_to_trade(r, source=source) for r in rows]

    async def _load_trades_from_table(self,
                                      table: str,
                                      limit: int,
                                      source: str) -> List[Trade]:
        async with self._db.execute(
            f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_trade(r, source=source) for r in rows]

    async def get_closed_trades(self) -> list[Trade]:
        sql = "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_fill_ts ASC"
        async with self._db.execute(sql) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_trade(r, source="real") for r in rows]

    async def get_all_history_trades(self) -> list[Trade]:
        real_rows = await self._load_history_from_table("trades", source="real")
        paper_rows = await self._load_history_from_table("paper_trades", source="paper")
        merged = real_rows + paper_rows
        merged.sort(key=lambda t: t.created_at or "")
        return merged

    async def _load_history_from_table(self, table: str, source: str) -> list[Trade]:
        sql = f"SELECT * FROM {table} ORDER BY created_at ASC"
        async with self._db.execute(sql) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_trade(r, source=source) for r in rows]

    async def get_last_closed_time(self, pair: str) -> datetime | None:
        return await self._get_last_closed_time_from_table("trades", pair)

    async def get_last_paper_closed_time(self, pair: str) -> datetime | None:
        return await self._get_last_closed_time_from_table("paper_trades", pair)

    async def _get_last_closed_time_from_table(self, table: str, pair: str) -> datetime | None:
        sql = (
            f"SELECT exit_fill_ts FROM {table} "
            "WHERE pair = ? AND status = 'closed' AND exit_fill_ts IS NOT NULL "
            "ORDER BY exit_fill_ts DESC LIMIT 1"
        )
        async with self._db.execute(sql, (pair,)) as cursor:
            row = await cursor.fetchone()

        if row and row[0]:
            try:
                ts_str = row[0].replace("Z", "+00:00")
                return datetime.fromisoformat(ts_str)
            except Exception as e:
                log.error(f"Error parseando fecha de cuarentena para {pair} ({row[0]}): {e}")
        return None

    async def get_trade(self, trade_id: str) -> Optional[Trade]:
        async with self._db.execute(
            "SELECT * FROM trades WHERE trade_id=?",
            (trade_id,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return _row_to_trade(row, source="real")

        async with self._db.execute(
            "SELECT * FROM paper_trades WHERE trade_id=?",
            (trade_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_trade(row, source="paper") if row else None

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
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in reversed(rows)]

    async def get_last_events(self, limit: int = 100) -> List[Event]:
        async with self._db.execute(
            "SELECT * FROM events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in rows]

    async def get_daily_metrics(self) -> dict:
        return await self._get_daily_metrics_from_table("trades")

    async def get_paper_daily_metrics(self) -> dict:
        return await self._get_daily_metrics_from_table("paper_trades")

    async def get_dashboard_summary(self, paper: bool = False) -> dict:
        table = "paper_trades" if paper else "trades"
        return await self._get_dashboard_summary_from_table(table)

    async def sync_paper_closed_trade_fees(self, friction_pct: float) -> int:
        friction_pct = max(0.0, float(friction_pct))
        sql = """
            UPDATE paper_trades
            SET fees_usdt = ROUND((entry_price + exit_price) * entry_quantity * (? / 100.0), 4)
            WHERE status = 'closed'
              AND entry_price IS NOT NULL
              AND exit_price IS NOT NULL
              AND entry_quantity IS NOT NULL
        """
        cursor = await self._db.execute(sql, (friction_pct,))
        await self._db.commit()
        updated = cursor.rowcount if cursor.rowcount != -1 else 0
        await cursor.close()
        log.info(
            f"StateDB v{STATE_VERSION}: resincronizadas {updated} fees de paper_trades "
            f"cerrados con friction_pct={friction_pct:.4f}%"
        )
        return updated

    async def _get_dashboard_summary_from_table(self, table: str) -> dict:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl_expr = "pnl_usdt - COALESCE(fees_usdt, 0)" if table == "paper_trades" else "pnl_usdt"
        active_statuses = (
            TradeStatus.OPEN.value,
            TradeStatus.OPENING.value,
            TradeStatus.SIGNAL_RECEIVED.value,
            TradeStatus.CLOSING.value,
        )
        sql = f"""
            SELECT
                SUM(CASE WHEN status IN (?,?,?,?) THEN 1 ELSE 0 END) AS open_total,
                SUM(CASE WHEN status IN (?,?,?,?)
                          AND COALESCE(entry_fill_ts, created_at, '') LIKE ? THEN 1 ELSE 0 END) AS open_today,
                SUM(CASE WHEN entry_fill_ts IS NOT NULL THEN 1 ELSE 0 END) AS entries_total,
                SUM(CASE WHEN entry_fill_ts LIKE ? THEN 1 ELSE 0 END) AS entries_today,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_total,
                SUM(CASE WHEN status = 'closed' AND pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins_total,
                SUM(CASE WHEN status = 'closed' THEN {pnl_expr} ELSE 0 END) AS pnl_total,
                SUM(CASE WHEN status = 'closed'
                          AND COALESCE(exit_fill_ts, updated_at, '') LIKE ? THEN 1 ELSE 0 END) AS closed_today,
                SUM(CASE WHEN status = 'closed'
                          AND COALESCE(exit_fill_ts, updated_at, '') LIKE ?
                          AND pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins_today,
                SUM(CASE WHEN status = 'closed'
                          AND COALESCE(exit_fill_ts, updated_at, '') LIKE ? THEN {pnl_expr} ELSE 0 END) AS pnl_today,
                SUM(CASE WHEN status = 'closed'
                          AND COALESCE(exit_fill_ts, updated_at, '') LIKE ?
                          AND COALESCE(exit_type, '') <> 'manual' THEN 1 ELSE 0 END) AS closed_today_non_manual,
                SUM(CASE WHEN status = 'closed'
                          AND COALESCE(exit_fill_ts, updated_at, '') LIKE ?
                          AND COALESCE(exit_type, '') <> 'manual'
                          AND pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins_today_non_manual
            FROM {table}
        """
        params = (
            *active_statuses,
            *active_statuses,
            f"{today_str}%",
            f"{today_str}%",
            f"{today_str}%",
            f"{today_str}%",
            f"{today_str}%",
            f"{today_str}%",
            f"{today_str}%",
        )
        async with self._db.execute(sql, params) as cursor:
            row = await cursor.fetchone()

        if not row:
            return {
                "open_total": 0,
                "open_today": 0,
                "entries_total": 0,
                "entries_today": 0,
                "closed_total": 0,
                "wins_total": 0,
                "pnl_total": 0.0,
                "closed_today": 0,
                "wins_today": 0,
                "pnl_today": 0.0,
                "closed_today_non_manual": 0,
                "wins_today_non_manual": 0,
            }

        return {
            "open_total": row[0] or 0,
            "open_today": row[1] or 0,
            "entries_total": row[2] or 0,
            "entries_today": row[3] or 0,
            "closed_total": row[4] or 0,
            "wins_total": row[5] or 0,
            "pnl_total": row[6] or 0.0,
            "closed_today": row[7] or 0,
            "wins_today": row[8] or 0,
            "pnl_today": row[9] or 0.0,
            "closed_today_non_manual": row[10] or 0,
            "wins_today_non_manual": row[11] or 0,
        }

    async def _get_daily_metrics_from_table(self, table: str) -> dict:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl_expr = "pnl_usdt - COALESCE(fees_usdt, 0)" if table == "paper_trades" else "pnl_usdt"
        sql = f"""
            SELECT
                COUNT(*) as total_closed,
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                SUM({pnl_expr}) as pnl_total,
                SUM(CASE WHEN exit_fill_ts LIKE ? THEN {pnl_expr} ELSE 0 END) as pnl_today,
                SUM(CASE WHEN exit_fill_ts LIKE ? THEN 1 ELSE 0 END) as closed_today
            FROM {table}
            WHERE status = 'closed'
        """
        async with self._db.execute(sql, (f"{today_str}%", f"{today_str}%")) as cursor:
            row = await cursor.fetchone()

        if not row:
            return {
                "total_closed": 0,
                "wins": 0,
                "pnl_total": 0.0,
                "pnl_today": 0.0,
                "closed_today": 0,
            }

        return {
            "total_closed": row[0] or 0,
            "wins": row[1] or 0,
            "pnl_total": row[2] or 0.0,
            "pnl_today": row[3] or 0.0,
            "closed_today": row[4] or 0,
        }


def _row_to_trade(row, source: str = "real") -> Trade:
    r = dict(row)
    return Trade(
        trade_id=r["trade_id"],
        pair=r["pair"],
        signal_ts=r["signal_ts"] or "",
        signal_data=json.loads(r["signal_data"] or "{}"),
        entry_order_id=r["entry_order_id"],
        entry_price=r["entry_price"],
        entry_quantity=r["entry_quantity"],
        entry_fill_ts=r["entry_fill_ts"],
        tp_order_id=r["tp_order_id"],
        sl_order_id=r["sl_order_id"],
        tp_price=r["tp_price"],
        sl_price=r["sl_price"],
        tp_trigger_price=r["tp_trigger_price"],
        sl_trigger_price=r["sl_trigger_price"],
        exit_price=r["exit_price"],
        exit_fill_ts=r["exit_fill_ts"],
        exit_type=r["exit_type"],
        pnl_usdt=r["pnl_usdt"],
        pnl_pct=r["pnl_pct"],
        fees_usdt=r["fees_usdt"],
        status=TradeStatus(r["status"]),
        error_message=r["error_message"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        timeout_triggered=bool(r["timeout_triggered"]) if r["timeout_triggered"] is not None else False,
        reconciled=bool(r["reconciled"]) if r["reconciled"] is not None else False,
        source=source,
    )


def _row_to_event(row) -> Event:
    r = dict(row)
    return Event(
        event_id=r["event_id"],
        trade_id=r["trade_id"],
        event_type=r["event_type"],
        details=json.loads(r["details"] or "{}"),
        timestamp=r["timestamp"],
    )
