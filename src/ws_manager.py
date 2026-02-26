"""
ws_manager.py — WebSocket User Data Stream de Binance Futures.

Escucha ORDER_TRADE_UPDATE para detectar:
  - Fill de entry order  → callback on_entry_fill(order_data)
  - Fill de TP order     → callback on_tp_fill(order_data)
  - Fill de SL order     → callback on_sl_fill(order_data)

Mantiene el listenKey activo con PUT cada keep_alive_interval segundos.
Reconexión automática con backoff exponencial.
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from .config import Config
from .logger import get_logger
from .order_manager import OrderManager

log = get_logger("ws_manager")

OnFillCallback = Callable[[dict], Awaitable[None]]


class WSManager:
    def __init__(self,
                 cfg:           Config,
                 order_mgr:     OrderManager,
                 on_entry_fill: OnFillCallback,
                 on_tp_fill:    OnFillCallback,
                 on_sl_fill:    OnFillCallback):
        self._cfg           = cfg
        self._order_mgr     = order_mgr
        self._on_entry_fill = on_entry_fill
        self._on_tp_fill    = on_tp_fill
        self._on_sl_fill    = on_sl_fill

        self._listen_key:    Optional[str]  = None
        self._connected:     bool           = False
        self._entry_orders:  set[int]       = set()   # order_ids de entry
        self._tp_orders:     set[int]       = set()   # order_ids de TP
        self._sl_orders:     set[int]       = set()   # order_ids de SL

        # Events opcionales por orderId: se activan al detectar FILLED.
        # LimitChaseExecutor los usa para despertar antes del timeout REST.
        self._fill_events: dict[int, asyncio.Event] = {}

        # referencia al task para poder cancelarlo
        self._task:          Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None

        self._keep_alive_secs = 60 * 25   # cada 25 min < límite de 60 min

    # ──────────────────────────────────────────────────────────────────
    # Registro de órdenes a vigilar
    # ──────────────────────────────────────────────────────────────────

    def register_entry(self, order_id: int):
        self._entry_orders.add(order_id)
        log.debug(f"WS registrado entry orderId={order_id}")

    def register_tp(self, order_id: int):
        self._tp_orders.add(order_id)
        log.debug(f"WS registrado TP orderId={order_id}")

    def register_sl(self, order_id: int):
        self._sl_orders.add(order_id)
        log.debug(f"WS registrado SL orderId={order_id}")

    def register_fill_event(self, order_id: int, event: asyncio.Event):
        """Registra un asyncio.Event que se activará cuando orderId sea FILLED."""
        self._fill_events[order_id] = event
        log.debug(f"WS fill_event registrado orderId={order_id}")

    def unregister_fill_event(self, order_id: int):
        self._fill_events.pop(order_id, None)

    def unregister(self, order_id: int):
        self._entry_orders.discard(order_id)
        self._tp_orders.discard(order_id)
        self._sl_orders.discard(order_id)
        self._fill_events.pop(order_id, None)

    @property
    def connected(self) -> bool:
        return self._connected

    # ──────────────────────────────────────────────────────────────────
    # Ciclo principal
    # ──────────────────────────────────────────────────────────────────

    async def start(self):
        self._task = asyncio.create_task(self._run_loop(), name="ws_manager")
        log.info("WSManager iniciado")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._listen_key:
            try:
                await self._order_mgr.close_listen_key(self._listen_key)
            except Exception:
                pass
        self._connected = False
        log.info("WSManager detenido")

    async def _run_loop(self):
        backoff = 1.0
        while True:
            try:
                await self._connect()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected = False
                log.warning(f"WS desconectado: {e}. Reconectando en {backoff:.0f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect(self):
        self._listen_key = await self._order_mgr.get_listen_key()

        # Iniciar keep-alive de listenKey
        if self._keepalive_task:
            self._keepalive_task.cancel()
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="ws_keepalive"
        )

        url = f"{self._cfg.ws_base_url}/ws/{self._listen_key}"
        log.info(f"WS conectando: {url[:60]}...")

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._connected = True
            log.info("WS User Data Stream conectado ✓")
            async for raw in ws:
                await self._handle_message(raw)

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(self._keep_alive_secs)
            try:
                await self._order_mgr.keep_alive_listen_key(self._listen_key)
            except Exception as e:
                log.warning(f"Keep-alive listenKey falló: {e}")

    # ──────────────────────────────────────────────────────────────────
    # Procesado de mensajes
    # ──────────────────────────────────────────────────────────────────

    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(f"WS mensaje no JSON: {raw[:200]}")
            return

        event_type = msg.get("e")
        log.debug(f"WS msg: {event_type} → {str(msg)[:300]}")

        if event_type != "ORDER_TRADE_UPDATE":
            return

        order = msg.get("o", {})
        exec_type  = order.get("x")   # TRADE = fill
        order_status = order.get("X") # FILLED, PARTIALLY_FILLED, ...

        if exec_type not in ("TRADE", "FILLED") or order_status != "FILLED":
            return

        order_id = int(order.get("i", 0))
        log.info(
            f"WS FILLED: orderId={order_id} symbol={order.get('s')} "
            f"side={order.get('S')} qty={order.get('q')} price={order.get('ap')}"
        )

        # Activar fill_event si hay uno registrado (Limit-Chase executor).
        # Se hace ANTES del callback para que el executor pueda despertar y
        # retornar; on_sl_fill cerrará el trade de forma independiente.
        ev = self._fill_events.pop(order_id, None)
        if ev is not None:
            ev.set()

        if order_id in self._entry_orders:
            self._entry_orders.discard(order_id)
            await self._on_entry_fill(order)
        elif order_id in self._tp_orders:
            self._tp_orders.discard(order_id)
            await self._on_tp_fill(order)
        elif order_id in self._sl_orders:
            self._sl_orders.discard(order_id)
            await self._on_sl_fill(order)
        else:
            log.debug(f"WS fill de orden no registrada: orderId={order_id}")
