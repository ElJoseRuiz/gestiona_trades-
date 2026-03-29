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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

# ── Añadir el directorio del paquete al path ──────────────────────────────────
import os
sys.path.insert(0, os.path.dirname(__file__))

import yaml

from src.config        import Config
from src.dashboard     import DashboardServer
from src.logger        import get_logger, setup_logging
from src.models        import Event, EventType, Trade, TradeStatus
from src.notifier      import NOTIFIER_VERSION, Notifier
from src.order_manager import BinanceError, OrderManager
from src.paper_trade_engine import PAPER_TRADE_ENGINE_VERSION, PaperTradeEngine
from src.signal_watcher import SignalWatcher
from src.state         import StateDB
from src.trade_engine  import TradeEngine
from src.ws_manager    import WSManager

APP_VERSION = "0.22"

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
        self._paper_finish_in_progress = False
        self._notifier = Notifier(cfg)
        self._notify_task: asyncio.Task | None = None
        self._ws_disconnected_since: datetime | None = None
        self._ws_disconnect_alert_sent = False
        self._orphans_alert_active = False
        self._last_orphan_alert_at: datetime | None = None
        self._last_summary_at: datetime | None = None

    # ──────────────────────────────────────────────────────────────────
    # Arranque
    # ──────────────────────────────────────────────────────────────────

    async def run(self):
        log.info("=" * 60)
        log.info(f"gestiona_trades v{APP_VERSION} arrancando - modo={self._cfg.mode}")
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
                finish_paper_trading = self._finish_paper_trading,
            )
            if self._cfg.dashboard_enabled:
                await self._dash.start()

            await self._notifier.start()

            # 10. Evento STARTUP
            await self._on_event(Event(
                event_type = EventType.STARTUP.value,
                details    = {
                    "version":          APP_VERSION,
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

            await self._notify_startup()
            if self._notifier.enabled:
                self._notify_task = asyncio.create_task(self._notification_loop())

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
        if self._paper_finish_in_progress:
            log.info(f"Senal ignorada para {signal.pair}: fin_paper_trading en curso")
            return

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

        if event.event_type == EventType.ERROR.value:
            await self._notify_error_event(event)

    async def _safe_notify(self, title: str, lines: list[str] | None = None) -> None:
        try:
            await self._notifier.send(title, lines)
        except Exception as exc:
            log.warning(f"No se pudo completar la notificacion: {exc}")

    async def _build_status_lines(self, status: dict) -> list[str]:
        balance = status.get("balance_usdt")
        balance_text = "n/d" if balance is None else f"{float(balance):.2f} USDT"
        unrealized_real, unrealized_paper = await self._get_control_mision_unrealized_pnl()
        return [
            f"instancia: {self._cfg.instance_name}",
            f"modo: {self._cfg.mode} | paper={self._cfg.paper_trading} | solo_cerrando_real={self._cfg.real_trading_solo_cerrando}",
            (
                "reales abiertos: "
                f"{status.get('open_trades_real', 0)} | paper abiertos: {status.get('open_trades_paper', 0)}"
            ),
            f"Unrealized PnL Real / Paper: {unrealized_real:.4f} / {unrealized_paper:.4f} USDT",
            (
                "PnL real hoy/total: "
                f"{status.get('pnl_today_usdt', 0.0):.4f} / {status.get('pnl_total_usdt', 0.0):.4f} USDT"
            ),
            (
                "PnL paper hoy/total: "
                f"{status.get('paper_pnl_today_usdt', 0.0):.4f} / {status.get('paper_pnl_total_usdt', 0.0):.4f} USDT"
            ),
            f"huérfanas Binance: {status.get('orphan_count', 0)}",
            f"WS Binance: {'OK' if status.get('ws_binance_connected') else 'DOWN'}",
            f"balance USDT: {balance_text}",
        ]

    async def _get_control_mision_unrealized_pnl(self) -> tuple[float, float]:
        """
        Replica la base de calculo de control_mision:
        - toma trades activos reales y paper
        - usa mark prices del endpoint premiumIndex
        - asume SHORT, igual que la operativa actual del bot
        """
        if not self._db or not self._om:
            return 0.0, 0.0

        try:
            real_trades, paper_trades = await asyncio.gather(
                self._db.load_active_trades(),
                self._db.load_active_paper_trades(),
            )
        except Exception as exc:
            log.warning(f"No se pudieron cargar trades activos para unrealized PnL: {exc}")
            return 0.0, 0.0

        try:
            items = await self._om._get("/fapi/v1/premiumIndex")
        except Exception as exc:
            log.warning(f"No se pudieron cargar mark prices para unrealized PnL: {exc}")
            return 0.0, 0.0

        mark_prices: dict[str, float] = {}
        if isinstance(items, list):
            for item in items:
                symbol = str(item.get("symbol") or "").strip()
                if not symbol:
                    continue
                try:
                    mark_prices[symbol] = float(item.get("markPrice", 0) or 0)
                except (TypeError, ValueError):
                    continue

        def _sum_unrealized(trades: list[Trade]) -> float:
            total = 0.0
            for trade in trades:
                if not trade.pair or trade.entry_price is None or trade.entry_quantity is None:
                    continue
                mark_price = mark_prices.get(trade.pair)
                if mark_price is None:
                    continue
                entry_price = float(trade.entry_price or 0)
                quantity = float(trade.entry_quantity or 0)
                if entry_price <= 0 or quantity <= 0:
                    continue
                total += (entry_price - mark_price) * quantity
            return round(total, 4)

        return _sum_unrealized(real_trades), _sum_unrealized(paper_trades)

    async def _notify_startup(self) -> None:
        if not (self._notifier.enabled and self._cfg.notify_startup):
            return
        status = await self._engine_status()
        lines = [
            f"versiones: app={APP_VERSION} | notifier={NOTIFIER_VERSION}",
            f"instancia configurada: {self._cfg.instance_name}",
        ]
        lines.extend(await self._build_status_lines(status))
        if self._cfg.startup_warnings:
            lines.append(f"warnings de arranque: {len(self._cfg.startup_warnings)}")
        await self._safe_notify("gestiona_trades arrancado", lines)
        self._last_summary_at = datetime.now(timezone.utc)

    async def _notify_error_event(self, event: Event) -> None:
        if not (self._notifier.enabled and self._cfg.notify_errors):
            return
        details = event.details or {}
        lines = []
        if event.trade_id:
            lines.append(f"trade_id: {event.trade_id}")
        if details:
            for key in ("pair", "symbol", "reason", "message", "error", "status"):
                value = details.get(key)
                if value not in (None, ""):
                    lines.append(f"{key}: {value}")
            if not lines:
                lines.append(f"details: {details}")
        await self._safe_notify("ERROR en gestiona_trades", lines)

    async def _notification_loop(self) -> None:
        interval = self._cfg.notify_health_check_interval_seconds
        while not self._stop_event.is_set():
            try:
                await self._run_notification_checks()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(f"Error en el bucle de notificaciones: {exc}")
            await asyncio.sleep(interval)

    async def _run_notification_checks(self) -> None:
        if not self._notifier.enabled:
            return

        status = await self._engine_status()
        now = datetime.now(timezone.utc)

        ws_connected = bool(status.get("ws_binance_connected"))
        if ws_connected:
            if self._ws_disconnect_alert_sent and self._cfg.notify_ws_disconnect:
                await self._safe_notify(
                    "WS Binance recuperado",
                    await self._build_status_lines(status),
                )
            self._ws_disconnected_since = None
            self._ws_disconnect_alert_sent = False
        else:
            if self._ws_disconnected_since is None:
                self._ws_disconnected_since = now
            grace = timedelta(minutes=self._cfg.notify_ws_disconnect_grace_minutes)
            if (
                self._cfg.notify_ws_disconnect
                and not self._ws_disconnect_alert_sent
                and now - self._ws_disconnected_since >= grace
            ):
                elapsed_minutes = int((now - self._ws_disconnected_since).total_seconds() // 60)
                lines = [f"tiempo desconectado: {elapsed_minutes} min"]
                lines.extend(await self._build_status_lines(status))
                await self._safe_notify("WS Binance desconectado", lines)
                self._ws_disconnect_alert_sent = True

        orphan_count = int(status.get("orphan_count", 0) or 0)
        if orphan_count <= 0:
            if self._orphans_alert_active and self._cfg.notify_orphans:
                await self._safe_notify("Huérfanas Binance resueltas", await self._build_status_lines(status))
            self._orphans_alert_active = False
            self._last_orphan_alert_at = None
        else:
            repeat_delta = timedelta(minutes=self._cfg.notify_orphan_repeat_minutes)
            should_repeat = (
                self._last_orphan_alert_at is None
                or now - self._last_orphan_alert_at >= repeat_delta
            )
            if self._cfg.notify_orphans and (not self._orphans_alert_active or should_repeat):
                lines = [f"posiciones huérfanas detectadas: {orphan_count}"]
                orphan_positions = status.get("orphan_positions") or []
                for orphan in orphan_positions[:5]:
                    pair = orphan.get("pair", "desconocido")
                    qty = orphan.get("position_amt", 0)
                    lines.append(f"{pair}: qty={qty}")
                lines.extend(await self._build_status_lines(status))
                await self._safe_notify("Huérfanas Binance detectadas", lines)
                self._orphans_alert_active = True
                self._last_orphan_alert_at = now

        summary_minutes = self._cfg.notify_summary_interval_minutes
        if self._cfg.notify_summary and summary_minutes > 0:
            summary_delta = timedelta(minutes=summary_minutes)
            if self._last_summary_at is None or now - self._last_summary_at >= summary_delta:
                await self._safe_notify("Resumen gestiona_trades", await self._build_status_lines(status))
                self._last_summary_at = now

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

        metrics = await self._db.get_dashboard_summary(paper=False) or {}
        paper_metrics = await self._db.get_dashboard_summary(paper=True) or {}
        orphan_positions = await self._get_orphan_positions()
        balance_usdt = None

        if self._om:
            try:
                balance_usdt = await self._om.get_balance()
            except Exception as e:
                log.warning(f"No se pudo obtener balance para dashboard: {e}")

        real_open = int(metrics.get("open_total", 0) or 0)
        real_open_today = int(metrics.get("open_today", 0) or 0)
        real_entries_total = int(metrics.get("entries_total", 0) or 0)
        real_entries_today = int(metrics.get("entries_today", 0) or 0)
        total_closed = int(metrics.get("closed_total", 0) or 0)
        wins = int(metrics.get("wins_total", 0) or 0)
        wins_today = int(metrics.get("wins_today", 0) or 0)
        closed_today_non_manual = int(metrics.get("closed_today_non_manual", 0) or 0)
        wins_today_non_manual = int(metrics.get("wins_today_non_manual", 0) or 0)
        pnl_today = float(metrics.get("pnl_today", 0.0) or 0.0)
        pnl_total = float(metrics.get("pnl_total", 0.0) or 0.0)
        closed_today = int(metrics.get("closed_today", 0) or 0)
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0
        win_rate_today = (
            wins_today_non_manual / closed_today_non_manual * 100
        ) if closed_today_non_manual > 0 else None
        paper_open = int(paper_metrics.get("open_total", 0) or 0)
        paper_open_today = int(paper_metrics.get("open_today", 0) or 0)
        paper_entries_total = int(paper_metrics.get("entries_total", 0) or 0)
        paper_entries_today = int(paper_metrics.get("entries_today", 0) or 0)
        paper_total_closed = int(paper_metrics.get("closed_total", 0) or 0)
        paper_wins = int(paper_metrics.get("wins_total", 0) or 0)
        paper_wins_today = int(paper_metrics.get("wins_today", 0) or 0)
        paper_closed_today_non_manual = int(paper_metrics.get("closed_today_non_manual", 0) or 0)
        paper_wins_today_non_manual = int(paper_metrics.get("wins_today_non_manual", 0) or 0)
        paper_win_rate = (paper_wins / paper_total_closed * 100) if paper_total_closed > 0 else 0
        paper_win_rate_today = (
            paper_wins_today_non_manual / paper_closed_today_non_manual * 100
        ) if paper_closed_today_non_manual > 0 else None
        return {
            "open_trades":      real_open + paper_open,
            "open_trades_real": real_open,
            "open_trades_today_real": real_open_today,
            "opened_trades_total_real": real_entries_total,
            "opened_trades_today_real": real_entries_today,
            "open_trades_paper": paper_open,
            "open_trades_today_paper": paper_open_today,
            "opened_trades_total_paper": paper_entries_total,
            "opened_trades_today_paper": paper_entries_today,
            "max_open_trades":  self._cfg.max_open_trades,
            "mode":             self._cfg.mode,
            "pnl_today_usdt":   round(pnl_today, 4),
            "pnl_total_usdt":   round(pnl_total, 4),
            "trades_today":     closed_today,
            "trades_total":     total_closed,
            "win_rate_today_pct": round(win_rate_today, 1) if win_rate_today is not None else None,
            "win_rate_pct":     round(win_rate, 1),
            "paper_pnl_today_usdt": round(float(paper_metrics.get("pnl_today", 0.0) or 0.0), 4),
            "paper_pnl_total_usdt": round(float(paper_metrics.get("pnl_total", 0.0) or 0.0), 4),
            "paper_trades_today": int(paper_metrics.get("closed_today", 0) or 0),
            "paper_trades_total": paper_total_closed,
            "paper_win_rate_today_pct": round(paper_win_rate_today, 1) if paper_win_rate_today is not None else None,
            "paper_win_rate_pct": round(paper_win_rate, 1),
            "balance_usdt":     round(balance_usdt, 4) if balance_usdt is not None else None,
            "orphan_positions": orphan_positions,
            "orphan_count":     len(orphan_positions),
            "paper_trading":    self._cfg.paper_trading,
            "real_solo_cerrando_trades": self._cfg.real_trading_solo_cerrando,
            "ws_connected":     self._ws_mgr._connected if self._ws_mgr else False,
            "ws_binance_connected": self._ws_mgr._connected if self._ws_mgr else False,
        }

    async def _finish_paper_trading(self) -> dict:
        if self._paper_finish_in_progress:
            raise RuntimeError("El proceso de fin_paper_trading ya esta en curso.")
        if not self._paper_engine:
            raise RuntimeError("PaperTradeEngine no esta disponible.")

        self._paper_finish_in_progress = True
        try:
            finish_result = await self._paper_engine.finish_paper_trading()
            report = await self._build_paper_trading_finish_report(finish_result)
            report_path = self._write_paper_trading_finish_report(report)
            asyncio.create_task(self._request_stop_after_paper_finish())
            return {
                "status": "ok",
                "report_path": str(report_path),
                "closed_open_trades": finish_result.get("closed_open_trades", 0),
                "cancelled_pending_trades": finish_result.get("cancelled_pending_trades", 0),
                "finished_at": finish_result.get("requested_at"),
            }
        except Exception:
            self._paper_finish_in_progress = False
            raise

    async def _request_stop_after_paper_finish(self):
        await asyncio.sleep(0.5)
        self.request_stop()

    async def _build_paper_trading_finish_report(self, finish_result: dict) -> dict:
        session_started_at = finish_result.get("session_started_at")
        finished_at = finish_result.get("requested_at")
        session_start_dt = self._parse_iso_dt(session_started_at) or datetime.now(timezone.utc)
        session_end_dt = self._parse_iso_dt(finished_at) or datetime.now(timezone.utc)

        public_cfg = self._cfg.public_dict()
        all_history = await self._db.get_all_history_trades()
        paper_session_trades = [
            trade for trade in all_history
            if getattr(trade, "source", "real") == "paper"
            and self._trade_belongs_to_session(trade, session_start_dt)
        ]
        closed_trades = [t for t in paper_session_trades if t.status == TradeStatus.CLOSED]
        not_executed_trades = [t for t in paper_session_trades if t.status == TradeStatus.NOT_EXECUTED]
        error_trades = [t for t in paper_session_trades if t.status == TradeStatus.ERROR]

        pnl_pct_values = [float(t.pnl_pct) for t in closed_trades if t.pnl_pct is not None]
        pnl_usdt_values = [float(t.pnl_usdt) for t in closed_trades if t.pnl_usdt is not None]
        wins = sum(1 for value in pnl_usdt_values if value > 0)
        losses = sum(1 for value in pnl_usdt_values if value < 0)
        flats = sum(1 for value in pnl_usdt_values if value == 0)
        total_closed = len(closed_trades)

        exit_type_stats: dict[str, dict] = {}
        pair_stats: dict[str, dict] = {}
        for trade in closed_trades:
            exit_key = trade.exit_type or "unknown"
            exit_bucket = exit_type_stats.setdefault(exit_key, {
                "count": 0,
                "wins": 0,
                "pnl_total_usdt": 0.0,
                "avg_pnl_pct": [],
            })
            exit_bucket["count"] += 1
            pnl_usdt = float(trade.pnl_usdt or 0.0)
            pnl_pct = float(trade.pnl_pct or 0.0)
            exit_bucket["pnl_total_usdt"] += pnl_usdt
            exit_bucket["avg_pnl_pct"].append(pnl_pct)
            if pnl_usdt > 0:
                exit_bucket["wins"] += 1

            pair_bucket = pair_stats.setdefault(trade.pair, {
                "count": 0,
                "pnl_total_usdt": 0.0,
                "avg_pnl_pct": [],
            })
            pair_bucket["count"] += 1
            pair_bucket["pnl_total_usdt"] += pnl_usdt
            pair_bucket["avg_pnl_pct"].append(pnl_pct)

        exit_type_stats = {
            key: {
                "count": value["count"],
                "win_rate_pct": round(value["wins"] * 100.0 / value["count"], 2) if value["count"] else 0.0,
                "pnl_total_usdt": round(value["pnl_total_usdt"], 4),
                "avg_pnl_pct": round(sum(value["avg_pnl_pct"]) / len(value["avg_pnl_pct"]), 4) if value["avg_pnl_pct"] else 0.0,
            }
            for key, value in sorted(exit_type_stats.items(), key=lambda item: item[1]["count"], reverse=True)
        }

        pair_rows = [
            {
                "pair": pair,
                "count": value["count"],
                "pnl_total_usdt": round(value["pnl_total_usdt"], 4),
                "avg_pnl_pct": round(sum(value["avg_pnl_pct"]) / len(value["avg_pnl_pct"]), 4) if value["avg_pnl_pct"] else 0.0,
            }
            for pair, value in pair_stats.items()
        ]
        pair_rows.sort(key=lambda item: item["pnl_total_usdt"])

        duration_hours = (session_end_dt - session_start_dt).total_seconds() / 3600.0
        return {
            "paper_trading_summary_version": 1,
            "inicio_utc": session_started_at,
            "fin_utc": finished_at,
            "duracion_horas": round(duration_hours, 4),
            "motivo_fin": "fin_paper_trading_desde_dashboard",
            "versiones": {
                "gestiona_trades": APP_VERSION,
                "paper_trade_engine": PAPER_TRADE_ENGINE_VERSION,
            },
            "parametros_efectivos": {
                "strategy": public_cfg.get("strategy", {}),
                "effective_filters_entrada": public_cfg.get("effective_filters_entrada", {}),
                "filtros_gestiona_trades_meta": public_cfg.get("filtros_gestiona_trades_meta", {}),
            },
            "resultado_fin_paper": finish_result,
            "estadisticas": {
                "trades_sesion_total": len(paper_session_trades),
                "trades_cerrados": total_closed,
                "trades_no_ejecutados": len(not_executed_trades),
                "trades_error": len(error_trades),
                "wins": wins,
                "losses": losses,
                "flat": flats,
                "win_rate_pct": round(wins * 100.0 / total_closed, 2) if total_closed else 0.0,
                "pnl_total_usdt": round(sum(pnl_usdt_values), 4),
                "pnl_medio_usdt": round(sum(pnl_usdt_values) / total_closed, 4) if total_closed else 0.0,
                "pnl_medio_pct": round(sum(pnl_pct_values) / total_closed, 4) if total_closed else 0.0,
                "pnl_mediana_pct": round(median(pnl_pct_values), 4) if pnl_pct_values else 0.0,
                "mejor_trade_usdt": round(max(pnl_usdt_values), 4) if pnl_usdt_values else 0.0,
                "peor_trade_usdt": round(min(pnl_usdt_values), 4) if pnl_usdt_values else 0.0,
                "mejor_trade_pct": round(max(pnl_pct_values), 4) if pnl_pct_values else 0.0,
                "peor_trade_pct": round(min(pnl_pct_values), 4) if pnl_pct_values else 0.0,
                "por_tipo_salida": exit_type_stats,
                "top_5_pares_peores": pair_rows[:5],
                "top_5_pares_mejores": list(reversed(pair_rows[-5:])),
            },
        }

    def _write_paper_trading_finish_report(self, report: dict) -> Path:
        end_dt = self._parse_iso_dt(report.get("fin_utc")) or datetime.now(timezone.utc)
        stamp = end_dt.strftime("%Y%m%d-%H%M%S")
        logs_dir = Path("logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        output_path = logs_dir / f"paper_trading-{stamp}-final.yaml"
        output_path.write_text(
            yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        log.info(f"Resumen fin_paper_trading guardado en {output_path}")
        return output_path

    @staticmethod
    def _parse_iso_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _trade_belongs_to_session(self, trade: Trade, session_start_dt: datetime) -> bool:
        for value in (
            trade.created_at,
            trade.updated_at,
            trade.signal_ts,
            trade.entry_fill_ts,
            trade.exit_fill_ts,
        ):
            dt = self._parse_iso_dt(value)
            if dt and dt >= session_start_dt:
                return True
        return False

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

        if self._notifier.enabled and self._cfg.notify_shutdown:
            try:
                status = await self._engine_status()
                await self._safe_notify("gestiona_trades detenido", await self._build_status_lines(status))
            except Exception as exc:
                log.warning(f"No se pudo enviar notificacion de apagado: {exc}")

        if self._notify_task:
            self._notify_task.cancel()
            try:
                await self._notify_task
            except asyncio.CancelledError:
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

        await self._notifier.stop()

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
        try:
            await app._safe_notify(
                "Error fatal en gestiona_trades",
                [
                    f"instancia: {cfg.instance_name}",
                    f"error: {e}",
                ],
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    # aiohttp requiere SelectorEventLoop en Windows (el ProactorEventLoop
    # por defecto en Python 3.8+ en Windows no es compatible con aiohttp)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_main())
