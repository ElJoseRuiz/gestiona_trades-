"""
gestiona_trades.py — Punto de entrada principal del sistema de trading.

Uso:
    python gestiona_trades.py [--config config.yaml]

Secuencia de arranque:
    1. Cargar config.yaml, inicializar logging
    2. Conectar DB (SQLite)
    3. Conectar a Binance REST, verificar credenciales y balance
    4. Configurar leverage y margin type (isolated) para los pares activos
    5. Cargar trades activos de la DB
    6. Inicializar TradeEngine y WSManager
    7. Reconciliar trades activos con el estado real en Binance
    8. Iniciar WebSocket User Data Stream
    9. Iniciar TradeEngine (timeout checker)
    10. Iniciar SignalWatcher
    11. Iniciar Dashboard
    12. Esperar SIGINT/SIGTERM → graceful shutdown

Graceful shutdown:
    - Detener SignalWatcher (no más señales)
    - Detener Dashboard
    - Detener TradeEngine (cancela timeout checker; los trades OPEN siguen
      vigentes en Binance con sus TP/SL)
    - Detener WSManager
    - Cerrar OrderManager (sesión HTTP)
    - Cerrar DB
    - Log SHUTDOWN
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import datetime, timezone

# ── Añadir el directorio del paquete al path ──────────────────────────────────
import os
sys.path.insert(0, os.path.dirname(__file__))

from src.config        import Config
from src.dashboard     import DashboardServer
from src.logger        import get_logger, setup_logging
from src.models        import Event, EventType
from src.order_manager import BinanceError, OrderManager
from src.signal_watcher import SignalWatcher
from src.state         import StateDB
from src.trade_engine  import TradeEngine
from src.ws_manager    import WSManager

log = get_logger("main")

# ──────────────────────────────────────────────────────────────────────────────
# Aplicación principal
# ──────────────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, cfg: Config):
        self._cfg      = cfg
        self._db:      StateDB        = None  # type: ignore
        self._om:      OrderManager   = None  # type: ignore
        self._ws_mgr:  WSManager      = None  # type: ignore
        self._engine:  TradeEngine    = None  # type: ignore
        self._watcher: SignalWatcher  = None  # type: ignore
        self._dash:    DashboardServer = None  # type: ignore
        self._stop_event = asyncio.Event()

    # ──────────────────────────────────────────────────────────────────
    # Arranque
    # ──────────────────────────────────────────────────────────────────

    async def run(self):
        log.info("=" * 60)
        log.info(f"gestiona_trades arrancando — modo={self._cfg.mode}")
        log.info("=" * 60)

        # 1. Base de datos
        self._db = StateDB(self._cfg)
        await self._db.init()

        # 2. Binance REST — verificar credenciales y balance
        self._om = OrderManager(self._cfg)
        await self._om.init()

        try:
            balance = await self._om.get_balance()
            log.info(f"Balance USDT disponible: {balance:.2f}")
        except BinanceError as e:
            log.error(f"Error verificando credenciales Binance: {e}")
            raise SystemExit(1)

        # 3. Construir TradeEngine y WSManager (antes de reconciliar)
        self._ws_mgr = WSManager(
            cfg           = self._cfg,
            order_mgr     = self._om,
            on_entry_fill = self._on_entry_fill_proxy,
            on_tp_fill    = self._on_tp_fill_proxy,
            on_sl_fill    = self._on_sl_fill_proxy,
        )

        self._engine = TradeEngine(
            cfg       = self._cfg,
            order_mgr = self._om,
            ws_mgr    = self._ws_mgr,
            db        = self._db,
            on_event  = self._on_event,
        )

        # 4. Cargar trades activos y reconciliar
        active_trades = await self._db.load_active_trades()
        if active_trades:
            log.info(f"Cargados {len(active_trades)} trades activos de la DB")
            await self._engine.reconcile(active_trades)
        else:
            log.info("Sin trades activos en DB")

        # 5. Configurar leverage + margin type para los pares con trades abiertos
        if active_trades:
            pairs_seen = set()
            for t in active_trades:
                if t.pair not in pairs_seen:
                    await self._setup_pair(t.pair)
                    pairs_seen.add(t.pair)

        # 6. Iniciar WSManager
        await self._ws_mgr.start()

        # 7. Iniciar TradeEngine (timeout checker)
        await self._engine.start()

        # 8. Iniciar SignalWatcher
        self._watcher = SignalWatcher(
            cfg       = self._cfg,
            on_signal = self._on_signal,
        )
        await self._watcher.start()

        # 9. Dashboard
        self._dash = DashboardServer(
            cfg               = self._cfg,
            db                = self._db,
            get_engine_status = self._engine_status,
        )
        if self._cfg.dashboard_enabled:
            await self._dash.start()

        # 10. Evento STARTUP
        await self._on_event(Event(
            event_type = EventType.STARTUP.value,
            details    = {
                "mode":             self._cfg.mode,
                "max_open_trades":  self._cfg.max_open_trades,
                "capital_per_trade": self._cfg.capital_per_trade,
                "leverage":         self._cfg.leverage,
                "tp_pct":           self._cfg.tp_pct,
                "sl_pct":           self._cfg.sl_pct,
            },
        ))

        log.info("Sistema listo. Esperando señales...")

        # 11. Bucle principal — esperar señal de parada
        try:
            await self._stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            # KeyboardInterrupt  → Windows sin signal handler registrado
            # CancelledError     → asyncio.run() cancela la tarea al recibir SIGINT
            log.info("Interrupción recibida, iniciando apagado...")

        await self._shutdown()

    # ──────────────────────────────────────────────────────────────────
    # Configuración de par: leverage + margin type
    # ──────────────────────────────────────────────────────────────────

    async def _setup_pair(self, pair: str):
        """Configura leverage e ISOLATED margin para un par."""
        try:
            await self._om.set_margin_type(pair, "ISOLATED")
        except BinanceError as e:
            # -4046: "No need to change margin type" → ya está en ISOLATED
            if "-4046" in str(e):
                pass
            else:
                log.warning(f"set_margin_type({pair}): {e}")
        try:
            await self._om.set_leverage(pair, self._cfg.leverage)
            log.info(f"Leverage {self._cfg.leverage}x configurado para {pair}")
        except BinanceError as e:
            log.warning(f"set_leverage({pair}): {e}")

    # ──────────────────────────────────────────────────────────────────
    # Callbacks proxy (conectan WS → TradeEngine)
    # ──────────────────────────────────────────────────────────────────

    async def _on_entry_fill_proxy(self, data: dict):
        await self._engine.on_entry_fill(data)

    async def _on_tp_fill_proxy(self, data: dict):
        await self._engine.on_tp_fill(data)

    async def _on_sl_fill_proxy(self, data: dict):
        await self._engine.on_sl_fill(data)

    # ──────────────────────────────────────────────────────────────────
    # on_signal: configurar par y delegar al engine
    # ──────────────────────────────────────────────────────────────────

    async def _on_signal(self, signal):
        # Configurar leverage/margin si es la primera vez que vemos el par
        await self._setup_pair(signal.pair)
        await self._engine.on_signal(signal)

    # ──────────────────────────────────────────────────────────────────
    # on_event: guardar en DB y broadcast al dashboard
    # ──────────────────────────────────────────────────────────────────

    async def _on_event(self, event: Event):
        try:
            await self._db.save_event(event)
        except Exception as e:
            log.error(f"Error guardando evento en DB: {e}")

        if self._dash:
            try:
                await self._dash.broadcast_event(event)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────
    # engine_status para el dashboard
    # ──────────────────────────────────────────────────────────────────

    def _engine_status(self) -> dict:
        if not self._engine:
            return {"open_trades": 0, "max_open_trades": self._cfg.max_open_trades}
        active = self._engine.get_active_trades()
        closed_today = 0
        pnl_today    = 0.0
        pnl_total    = 0.0
        wins         = 0
        total_closed = 0

        # PnL de trades en memoria (trades cerrados recientes)
        # La DB tiene los históricos completos; aquí solo los activos en RAM
        from src.models import TradeStatus
        for t in active:
            if t.status == TradeStatus.CLOSED:
                if t.pnl_usdt:
                    pnl_total += t.pnl_usdt
                    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if t.exit_fill_ts and t.exit_fill_ts.startswith(today_str):
                        pnl_today  += t.pnl_usdt
                        closed_today += 1
                total_closed += 1
                if t.pnl_usdt and t.pnl_usdt > 0:
                    wins += 1

        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

        return {
            "open_trades":      self._engine.open_count,
            "max_open_trades":  self._cfg.max_open_trades,
            "mode":             self._cfg.mode,
            "pnl_today_usdt":   round(pnl_today,  4),
            "pnl_total_usdt":   round(pnl_total,  4),
            "trades_today":     closed_today,
            "win_rate_pct":     round(win_rate, 1),
            "ws_connected":     self._ws_mgr._connected if self._ws_mgr else False,
        }

    # ──────────────────────────────────────────────────────────────────
    # Parada
    # ──────────────────────────────────────────────────────────────────

    def request_stop(self):
        self._stop_event.set()

    async def _shutdown(self):
        log.info("Iniciando apagado graceful...")

        # Evento SHUTDOWN antes de cerrar la DB
        if self._db:
            ev = Event(
                event_type = EventType.SHUTDOWN.value,
                details    = {"open_trades": self._engine.open_count if self._engine else 0},
            )
            try:
                await self._db.save_event(ev)
            except Exception:
                pass

        # 1. SignalWatcher: no más señales
        if self._watcher:
            await self._watcher.stop()

        # 2. Dashboard
        if self._dash:
            await self._dash.stop()

        # 3. TradeEngine (cancela timeout checker)
        if self._engine:
            await self._engine.stop()

        # 4. WSManager
        if self._ws_mgr:
            await self._ws_mgr.stop()

        # 5. OrderManager (cierra sesión HTTP)
        if self._om:
            await self._om.close()

        # 6. DB
        if self._db:
            await self._db.close()

        log.info("Apagado completo.")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="gestiona_trades — Sistema de trading automático Binance Futures"
    )
    p.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Ruta al archivo de configuración (default: config.yaml)",
    )
    return p.parse_args()


async def _main():
    args = _parse_args()

    # Carga de config y logging (antes del get_logger de módulos)
    cfg = Config(args.config)
    setup_logging(cfg)

    app = App(cfg)

    # Capturar señales del SO para shutdown graceful
    loop = asyncio.get_running_loop()

    def _handle_signal():
        log.info("Señal de parada recibida (SIGINT/SIGTERM)")
        app.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, ValueError):
            # Windows: add_signal_handler no está soportado.
            # - SIGINT: no sobreescribir; se gestiona via KeyboardInterrupt en run().
            # - SIGTERM: no existe en Windows, ignorar.
            try:
                if sig == signal.SIGTERM:
                    signal.signal(sig, lambda s, f: loop.call_soon_threadsafe(_handle_signal))
            except (OSError, ValueError):
                pass

    try:
        await app.run()
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        log.critical(f"Error fatal no controlado: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    # aiohttp requiere SelectorEventLoop en Windows (el ProactorEventLoop
    # por defecto en Python 3.8+ en Windows no es compatible con aiohttp)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_main())
