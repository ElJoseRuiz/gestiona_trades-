"""
models.py — Dataclasses y enums del dominio.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────

class TradeStatus(str, Enum):
    SIGNAL_RECEIVED = "signal_received"
    OPENING         = "opening"
    NOT_EXECUTED    = "not_executed"
    OPEN            = "open"
    CLOSING         = "closing"
    CLOSED          = "closed"
    ERROR           = "error"


class ExitType(str, Enum):
    TP      = "tp"
    SL      = "sl"
    TIMEOUT = "timeout"
    MANUAL  = "manual"


class EventType(str, Enum):
    SIGNAL        = "signal"
    ENTRY_SENT    = "entry_sent"
    ENTRY_FILL    = "entry_fill"
    TP_PLACED     = "tp_placed"
    SL_PLACED     = "sl_placed"
    TP_FILL       = "tp_fill"
    SL_FILL       = "sl_fill"
    SL_TRIGGERED  = "sl_triggered"   # mark price cruzó sl_trigger → inicia chase
    TIMEOUT       = "timeout"
    CANCEL        = "cancel"
    ERROR         = "error"
    WS_CONNECT    = "ws_connect"
    WS_DISCONNECT = "ws_disconnect"
    STARTUP       = "startup"
    SHUTDOWN      = "shutdown"


# ──────────────────────────────────────────────────────────────────────────────
# Signal (leída del CSV de selecciona_pares)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    fecha_hora:   str
    pair:         str
    top:          int
    close:        float
    mom_1h_pct:   float
    mom_pct:      float
    vol_ratio:    float
    trades_ratio: float
    quintil:      int
    signal_dt:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────────────────────────────────────
# Trade (ciclo de vida completo)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    trade_id:          str            = field(default_factory=lambda: str(uuid.uuid4()))
    pair:              str            = ""
    signal_ts:         str            = ""
    signal_data:       dict           = field(default_factory=dict)

    # entry
    entry_order_id:    Optional[int]  = None
    entry_price:       Optional[float] = None
    entry_quantity:    Optional[float] = None
    entry_fill_ts:     Optional[str]  = None

    # TP/SL algo orders
    tp_order_id:       Optional[str]  = None
    sl_order_id:       Optional[str]  = None
    tp_price:          Optional[float] = None
    sl_price:          Optional[float] = None
    tp_trigger_price:  Optional[float] = None
    sl_trigger_price:  Optional[float] = None

    # exit
    exit_price:        Optional[float] = None
    exit_fill_ts:      Optional[str]  = None
    exit_type:         Optional[str]  = None
    pnl_usdt:          Optional[float] = None
    pnl_pct:           Optional[float] = None
    fees_usdt:         Optional[float] = None

    status:            TradeStatus    = TradeStatus.SIGNAL_RECEIVED
    error_message:     Optional[str]  = None
    created_at:        str            = field(default_factory=_now_iso)
    updated_at:        str            = field(default_factory=_now_iso)
    timeout_triggered: bool           = False
    reconciled:        bool           = False

    def touch(self):
        self.updated_at = _now_iso()


# ──────────────────────────────────────────────────────────────────────────────
# Event (log de auditoría)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Event:
    event_id:   Optional[int] = None
    trade_id:   Optional[str] = None
    event_type: str           = ""
    details:    dict          = field(default_factory=dict)
    timestamp:  str           = field(default_factory=_now_iso)
