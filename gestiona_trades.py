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
    9. Iniciar TradeEngine (timeout checker y reconcile checker)
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
from src.paper_trade_engine import PaperTradeEngine
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
        self._paper_engine: PaperTradeEngine = None  # type: ignore
        self._watcher: SignalWatcher  = None  # type: ignore
        self._dash:    DashboardServer = None  # type: ignore
        self._stop_event = asyncio.Event()
        self._configured_pairs: set[str] = set()
        self._shutdown_started = False

    # ──────────────────────────────────────────────────────────────────
    # Arranque
    # ──────────────────────────────────────────────────────────────────

    async def run(self):
        log.info("=" * 60)
        if self._cfg.real_trading_solo_cerrando:
            log.warning(
                "Trading real en modo solo_cerrando_trades=ON: no se abriran "
                "nuevos trades reales; solo se monitorizaran y cerraran los ya existentes."
            )
        if self._cfg.paper_trading:
            log.warning(
                "Modo paper_trading=ON: las nuevas señales se ejecutaran en paper "
                "sin enviar ordenes a Binance."
            )
        log.info(f"gestiona_trades arrancando — modo={self._cfg.mode}")
        log.info("=" * 60)
        try:
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
            self._paper_engine = PaperTradeEngine(
                cfg       = self._cfg,
                order_mgr = self._om,
                db        = self._db,
                on_event  = self._on_event,
            )

            # 4. Cargar trades activos y reconciliar (Llama SIEMPRE para limpiar huérfanos en Binance)
            active_trades = await self._db.load_active_trades()
            log.info(f"Cargados {len(active_trades)} trades activos de la DB")
            await self._engine.reconcile(active_trades)

            active_paper_trades = await self._db.load_active_paper_trades()
            if self._cfg.paper_trading:
                log.info(f"Cargados {len(active_paper_trades)} trades paper activos de la DB")
                await self._paper_engine.restore(active_paper_trades)
            elif active_paper_trades:
                log.warning(
                    f"paper_trading=OFF al arrancar: cerrando {len(active_paper_trades)} "
                    "trades paper que seguian abiertos."
                )
                await self._paper_engine.restore(active_paper_trades)
                await self._paper_engine.close_all_for_session_end()

            # 5. Configurar leverage + margin type SOLO para los pares que SIGUEN activos
            current_active = self._engine.get_active_trades()
            if current_active:
                pairs_to_setup = {t.pair for t in current_active}
                self._configured_pairs.update(pairs_to_setup)

                # Ejecución concurrente en batches de 10 para no saturar el rate limit REST
                async def _setup_batch(pairs_batch: list):
                    for p in pairs_batch:
                        await self._setup_pair(p)

                pairs_list = list(pairs_to_setup)
                tasks = [
                    _setup_batch(pairs_list[i:i + 10])
                    for i in range(0, len(pairs_list), 10)
                ]
                await asyncio.gather(*tasks)

            # 6. Iniciar WSManager
            await self._ws_mgr.start()

            # 7. Iniciar TradeEngine (timeout checker y reconciliador)
            await self._engine.start()

            if self._cfg.paper_trading:
                await self._paper_engine.start()

            # 8. Iniciar SignalWatcher solo si se permiten nuevas entradas
            if self._cfg.paper_trading or not self._cfg.real_trading_solo_cerrando:
                self._watcher = SignalWatcher(
                    cfg       = self._cfg,
                    on_signal = self._on_signal,
                )
                await self._watcher.start()
            elif self._cfg.real_trading_solo_cerrando:
                log.info(
                    "SignalWatcher no iniciado porque no se permiten nuevas entradas "
                    "(trading real en solo_cerrando y paper desactivado)."
                )

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
                    "solo_cerrando_trades": self._cfg.real_trading_solo_cerrando,
                    "paper_trading": self._cfg.paper_trading,
                },
            ))

            if self._cfg.paper_trading:
                log.info("Sistema listo. Trading real en solo cerrando y nuevas señales en paper.")
            elif self._cfg.real_trading_solo_cerrando:
                log.info("Sistema listo. Monitorizando cierres de trades reales existentes...")
            else:
                log.info("Sistema listo. Esperando señales...")

            # 11. Bucle principal — esperar señal de parada
            await self._stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            # KeyboardInterrupt  → Windows sin signal handler registrado
            # CancelledError     → asyncio.run() cancela la tarea al recibir SIGINT
            log.info("Interrupción recibida, iniciando apagado...")
        finally:
            await self._shutdown()

    # ──────────────────────────────────────────────────────────────────
    # Configuración de par: leverage + margin type
    # ──────────────────────────────────────────────────────────────────

    async def _setup_pair(self, pair: str):
        """Configura leverage e ISOLATED margin para un par."""
        try:
            await self._om.set_margin_type(pair, "ISOLATED")
        except BinanceError as e:
            # -4046: No need to change margin type (ya es ISOLATED)
            # -4067: Position side cannot be changed if there exists open orders
            if "-4046" in str(e) or "-4067" in str(e):
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
        if self._cfg.paper_trading:
            await self._paper_engine.on_signal(signal)
            return

        if self._cfg.real_trading_solo_cerrando:
            log.info(
                f"Senal ignorada para {signal.pair}: trading real en solo_cerrando_trades=ON"
            )
            return

        # Configurar leverage/margin solo la primera vez que vemos el par (caché en memoria)
        if signal.pair not in self._configured_pairs:
            await self._setup_pair(signal.pair)
            self._configured_pairs.add(signal.pair)
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

    async def _get_orphan_positions(self) -> list[dict]:
        """
        Posiciones reales abiertas en Binance sin trade activo correspondiente.
        Solo se exponen al dashboard para visibilidad; no se inventan trades.
        """
        if not self._engine or not self._om:
            return []

        active_pairs = {
            trade.pair
            for trade in self._engine.get_active_trades()
            if getattr(trade, "pair", None)
        }

        try:
            all_positions = await self._om.get_all_positions()
        except Exception as e:
            log.warning(f"No se pudieron obtener posiciones Binance para dashboard: {e}")
            return []

        detected_at = datetime.now(timezone.utc).isoformat()
        orphan_positions: list[dict] = []

        for position in all_positions:
            pair = position.get("symbol")
            if not pair or pair in active_pairs:
                continue

            position_amt = float(position.get("positionAmt", 0) or 0)
            if position_amt == 0:
                continue

            orphan_positions.append({
                "pair": pair,
                "side": "SHORT" if position_amt < 0 else "LONG",
                "position_amt": position_amt,
                "entry_price": float(position.get("entryPrice", 0) or 0),
                "mark_price": float(position.get("markPrice", 0) or 0),
                "unrealized_pnl": float(position.get("unRealizedProfit", 0) or 0),
                "notional": abs(float(position.get("notional", 0) or 0)),
                "isolated_wallet": float(position.get("isolatedWallet", 0) or 0),
                "detected_at": detected_at,
            })

        return orphan_positions

    async def _engine_status(self) -> dict:
        if not self._engine:
            return {"open_trades": 0, "max_open_trades": self._cfg.max_open_trades}

        metrics = await self._db.get_daily_metrics() or {}
        paper_metrics = await self._db.get_paper_daily_metrics() or {}
        orphan_positions = await self._get_orphan_positions()
        balance_usdt = None

        if self._om:
            try:
                balance_usdt = await self._om.get_balance()
            except Exception as e:
                log.warning(f"No se pudo obtener balance para dashboard: {e}")

        total_closed = int(metrics.get("total_closed", 0) or 0)
        wins = int(metrics.get("wins", 0) or 0)
        pnl_today = float(metrics.get("pnl_today", 0.0) or 0.0)
        pnl_total = float(metrics.get("pnl_total", 0.0) or 0.0)
        closed_today = int(metrics.get("closed_today", 0) or 0)
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0
        paper_total_closed = int(paper_metrics.get("total_closed", 0) or 0)
        paper_wins = int(paper_metrics.get("wins", 0) or 0)
        paper_win_rate = (paper_wins / paper_total_closed * 100) if paper_total_closed > 0 else 0
        paper_open = self._paper_engine.open_count if self._paper_engine else 0
        real_open = self._engine.open_count
        return {
            "open_trades":      real_open + paper_open,
            "open_trades_real": real_open,
            "open_trades_paper": paper_open,
            "max_open_trades":  self._cfg.max_open_trades,
            "mode":             self._cfg.mode,
            "pnl_today_usdt":   round(pnl_today, 4),
            "pnl_total_usdt":   round(pnl_total, 4),
            "trades_today":     closed_today,
            "win_rate_pct":     round(win_rate, 1),
            "paper_pnl_today_usdt": round(float(paper_metrics.get("pnl_today", 0.0) or 0.0), 4),
            "paper_pnl_total_usdt": round(float(paper_metrics.get("pnl_total", 0.0) or 0.0), 4),
            "paper_trades_today": int(paper_metrics.get("closed_today", 0) or 0),
            "paper_win_rate_pct": round(paper_win_rate, 1),
            "balance_usdt":     round(balance_usdt, 4) if balance_usdt is not None else None,
            "orphan_positions": orphan_positions,
            "orphan_count":     len(orphan_positions),
            "paper_trading":    self._cfg.paper_trading,
            "real_solo_cerrando_trades": self._cfg.real_trading_solo_cerrando,
            "ws_connected":     self._ws_mgr._connected if self._ws_mgr else False,
            "ws_binance_connected": self._ws_mgr._connected if self._ws_mgr else False,
        }

    # ──────────────────────────────────────────────────────────────────
    # Parada
    # ──────────────────────────────────────────────────────────────────

    def request_stop(self):
        self._stop_event.set()

    async def _shutdown(self):
        if self._shutdown_started:
            return
        self._shutdown_started = True

        log.info("Iniciando apagado graceful...")

        # Evento SHUTDOWN antes de cerrar la DB
        if self._db:
            ev = Event(
                event_type = EventType.SHUTDOWN.value,
                details    = {
                    "open_trades_real": self._engine.open_count if self._engine else 0,
                    "open_trades_paper": self._paper_engine.open_count if self._paper_engine else 0,
                },
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
            try:
                await asyncio.wait_for(self._engine.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("TradeEngine.stop() excedió 5s de timeout, continuando apagado.")

        if self._paper_engine:
            try:
                await asyncio.wait_for(self._paper_engine.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("PaperTradeEngine.stop() excedió 5s de timeout, continuando apagado.")

        # 4. WSManager
        if self._ws_mgr:
            try:
                await asyncio.wait_for(self._ws_mgr.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("WSManager.stop() excedió 5s de timeout, continuando apagado.")

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
