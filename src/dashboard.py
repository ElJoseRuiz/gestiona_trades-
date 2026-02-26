"""
dashboard.py — Servidor aiohttp embebido para monitorización en tiempo real.

Endpoints REST:
  GET  /api/status          → estado general
  GET  /api/trades          → trades activos + últimos N cerrados
  GET  /api/trades/{id}     → detalle + eventos de un trade
  GET  /api/events?limit=N  → últimos N eventos
  GET  /api/config          → config activa (sin API keys)
  POST /api/trades/{id}/close → cierre manual de emergencia

WebSocket:
  ws://host:port/ws  → push de eventos en tiempo real

Estático:
  GET / → static/dashboard.html
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Set

from aiohttp import web

from .config import Config
from .logger import get_logger
from .models import Event, Trade, TradeStatus
from .state import StateDB

log = get_logger("dashboard")

_STATIC_DIR = Path(__file__).parent.parent / "static"


def _trade_to_dict(t: Trade) -> dict:
    return {
        "trade_id":         t.trade_id,
        "pair":             t.pair,
        "signal_ts":        t.signal_ts,
        "status":           t.status.value,
        "entry_price":      t.entry_price,
        "entry_quantity":   t.entry_quantity,
        "entry_fill_ts":    t.entry_fill_ts,
        "tp_price":         t.tp_price,
        "tp_trigger_price": t.tp_trigger_price,
        "sl_price":         t.sl_price,
        "sl_trigger_price": t.sl_trigger_price,
        "exit_price":       t.exit_price,
        "exit_fill_ts":     t.exit_fill_ts,
        "exit_type":        t.exit_type,
        "pnl_usdt":         t.pnl_usdt,
        "pnl_pct":          t.pnl_pct,
        "fees_usdt":        t.fees_usdt,
        "error_message":    t.error_message,
        "created_at":       t.created_at,
        "updated_at":       t.updated_at,
        "signal_data":      t.signal_data,
    }


def _event_to_dict(ev: Event) -> dict:
    return {
        "event_id":   ev.event_id,
        "trade_id":   ev.trade_id,
        "event_type": ev.event_type,
        "details":    ev.details,
        "timestamp":  ev.timestamp,
    }


class DashboardServer:
    def __init__(self,
                 cfg:         Config,
                 db:          StateDB,
                 get_engine_status: Callable[[], dict]):
        self._cfg    = cfg
        self._db     = db
        self._get_engine_status = get_engine_status
        self._ws_clients: Set[web.WebSocketResponse] = set()
        self._app    = web.Application()
        self._runner = None
        self._start_time = datetime.now(timezone.utc).isoformat()

        self._setup_routes()

    def _setup_routes(self):
        self._app.router.add_get("/",              self._handle_index)
        self._app.router.add_get("/api/status",    self._handle_status)
        self._app.router.add_get("/api/trades",    self._handle_trades)
        self._app.router.add_get("/api/trades/{id}", self._handle_trade_detail)
        self._app.router.add_get("/api/events",    self._handle_events)
        self._app.router.add_get("/api/config",    self._handle_config)
        self._app.router.add_post("/api/trades/{id}/close", self._handle_close)
        self._app.router.add_get("/ws",            self._handle_ws)

    # ──────────────────────────────────────────────────────────────────
    # Ciclo de vida
    # ──────────────────────────────────────────────────────────────────

    async def start(self):
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner,
                           self._cfg.dashboard_host,
                           self._cfg.dashboard_port)
        await site.start()
        log.info(
            f"Dashboard en http://{self._cfg.dashboard_host}:"
            f"{self._cfg.dashboard_port}"
        )

    async def stop(self):
        for ws in list(self._ws_clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()
        log.info("Dashboard detenido")

    # ──────────────────────────────────────────────────────────────────
    # Push de eventos a clientes WS
    # ──────────────────────────────────────────────────────────────────

    async def broadcast_event(self, ev: Event):
        if not self._ws_clients:
            return
        msg = json.dumps({"type": "event", "data": _event_to_dict(ev)})
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    async def broadcast_trade_update(self, trade: Trade):
        if not self._ws_clients:
            return
        msg = json.dumps({"type": "trade_update", "data": _trade_to_dict(trade)})
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ──────────────────────────────────────────────────────────────────
    # Handlers
    # ──────────────────────────────────────────────────────────────────

    async def _handle_index(self, request: web.Request) -> web.Response:
        html_path = _STATIC_DIR / "control_mision.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Dashboard no encontrado", status=404)

    async def _handle_status(self, request: web.Request) -> web.Response:
        status = self._get_engine_status()
        status["uptime_start"] = self._start_time
        status["now"]          = datetime.now(timezone.utc).isoformat()
        return web.json_response(status)

    async def _handle_trades(self, request: web.Request) -> web.Response:
        try:
            all_trades = await self._db.load_all_trades(limit=200)
            return web.json_response([_trade_to_dict(t) for t in all_trades])
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_trade_detail(self, request: web.Request) -> web.Response:
        trade_id = request.match_info["id"]
        trade    = await self._db.get_trade(trade_id)
        if not trade:
            return web.json_response({"error": "Trade no encontrado"}, status=404)
        events = await self._db.get_trade_events(trade_id)
        return web.json_response({
            "trade":  _trade_to_dict(trade),
            "events": [_event_to_dict(e) for e in events],
        })

    async def _handle_events(self, request: web.Request) -> web.Response:
        limit  = int(request.rel_url.query.get("limit", 100))
        events = await self._db.get_last_events(limit)
        return web.json_response([_event_to_dict(e) for e in events])

    async def _handle_config(self, request: web.Request) -> web.Response:
        return web.json_response(self._cfg.public_dict())

    async def _handle_close(self, request: web.Request) -> web.Response:
        # Cierre manual de emergencia — implementación básica
        trade_id = request.match_info["id"]
        log.warning(f"Solicitud de cierre manual: trade_id={trade_id}")
        return web.json_response({
            "status": "accepted",
            "trade_id": trade_id,
            "note": "Cierre manual registrado — implementar en trade_engine",
        })

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        log.debug(f"WS cliente conectado ({len(self._ws_clients)} total)")

        try:
            # Enviar estado inicial
            status = self._get_engine_status()
            await ws.send_str(json.dumps({"type": "status", "data": status}))

            # Últimos eventos
            events = await self._db.get_last_events(50)
            await ws.send_str(json.dumps({
                "type": "history",
                "data": [_event_to_dict(e) for e in events],
            }))

            async for msg in ws:
                pass   # no se espera input del browser
        except Exception as e:
            log.debug(f"WS cliente desconectado: {e}")
        finally:
            self._ws_clients.discard(ws)
        return ws
