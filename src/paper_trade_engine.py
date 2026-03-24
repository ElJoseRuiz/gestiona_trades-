"""
paper_trade_engine.py - Motor de paper trading sin ejecucion real en Binance.

- Las entradas se abren al precio de mercado del momento.
- Los cierres por TP/SL se evalúan con el cierre de la última vela cerrada de 5m.
- Los cierres por hold usan el precio de mercado del momento.
- Toda la persistencia va a la tabla paper_trades.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, Optional

from .config import Config
from .logger import get_logger
from .models import Event, EventType, ExitType, Signal, Trade, TradeStatus
from .order_manager import OrderManager
from .state import StateDB

log = get_logger("paper_trade_engine")

OnEventCallback = Callable[[Event], Awaitable[None]]
PAPER_TRADE_ENGINE_VERSION = "0.19"
_PAPER_KLINE_INTERVAL_S = 300
_PAPER_KLINE_GRACE_S = 10
_KLINE_WARNING_INTERVAL_S = 300.0


class PaperTradeEngine:
    def __init__(self,
                 cfg: Config,
                 order_mgr: OrderManager,
                 db: StateDB,
                 on_event: OnEventCallback):
        self._cfg = cfg
        self._order_mgr = order_mgr
        self._db = db
        self._on_event = on_event
        self._trades: Dict[str, Trade] = {}
        self._timeout_task: Optional[asyncio.Task] = None
        self._candle_task: Optional[asyncio.Task] = None
        self._open_tasks: set[asyncio.Task] = set()
        self._ignore_cycles: Dict[str, dict] = {}
        self._last_pair_candle_close_ms: Dict[str, int] = {}
        self._pair_kline_failures: Dict[str, dict] = {}
        self._session_started_at: Optional[str] = None

    @property
    def session_started_at(self) -> Optional[str]:
        return self._session_started_at

    @property
    def open_count(self) -> int:
        return sum(
            1 for t in self._trades.values()
            if t.status in (TradeStatus.OPEN, TradeStatus.OPENING, TradeStatus.SIGNAL_RECEIVED)
        )

    def open_count_pair(self, pair: str) -> int:
        return sum(
            1 for t in self._trades.values()
            if t.pair == pair and t.status in (
                TradeStatus.OPEN, TradeStatus.OPENING, TradeStatus.SIGNAL_RECEIVED
            )
        )

    def get_active_trades(self) -> list[Trade]:
        return [
            t for t in self._trades.values()
            if t.status not in (TradeStatus.CLOSED, TradeStatus.NOT_EXECUTED, TradeStatus.ERROR)
        ]

    async def restore(self, trades: list[Trade]):
        self._trades = {}
        for trade in trades:
            trade.source = "paper"
            self._ensure_trade_levels(trade)
            self._trades[trade.trade_id] = trade
        if trades:
            log.info(f"Restaurados {len(trades)} trades paper activos desde la DB")

    async def start(self):
        if self._session_started_at is None:
            self._session_started_at = datetime.now(timezone.utc).isoformat()
        self._timeout_task = asyncio.create_task(self._timeout_loop(), name="paper_timeout_checker")
        self._candle_task = asyncio.create_task(self._candle_loop(), name="paper_candle_checker")
        log.info(f"PaperTradeEngine v{PAPER_TRADE_ENGINE_VERSION} iniciado")

    async def stop(self):
        for task in (self._timeout_task, self._candle_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info(f"PaperTradeEngine detenido. Trades abiertos: {self.open_count}")

    async def close_all_for_session_end(self):
        for trade in list(self.get_active_trades()):
            await self._close_trade_now(
                trade,
                exit_type=ExitType.MANUAL.value,
                reason="paper_trading_off_startup",
            )

    async def finish_paper_trading(self) -> dict:
        await self._stop_background_tasks_for_finish()

        try:
            if self._session_started_at is None:
                self._session_started_at = datetime.now(timezone.utc).isoformat()

            requested_at = datetime.now(timezone.utc).isoformat()
            active_trades = list(self.get_active_trades())
            tradable_trades = [
                t for t in active_trades
                if t.entry_price is not None and t.entry_quantity is not None
            ]
            pending_trades = [t for t in active_trades if t not in tradable_trades]

            pair_prices: Dict[str, dict] = {}
            for pair in sorted({t.pair for t in tradable_trades}):
                kline = await self._order_mgr.get_last_closed_kline(pair, interval="5m")
                close_time_ms = int(kline["close_time"])
                pair_prices[pair] = {
                    "exit_price": float(kline["close"]),
                    "candle_close_utc": datetime.fromtimestamp(
                        close_time_ms / 1000.0,
                        tz=timezone.utc,
                    ).isoformat(),
                }

            cancelled_pending = 0
            for trade in pending_trades:
                await self._cancel_pending_trade_for_finish(trade, requested_at)
                cancelled_pending += 1

            closed_open_trades = 0
            for trade in tradable_trades:
                pair_snapshot = pair_prices[trade.pair]
                await self._close_trade_at_price(
                    trade,
                    exit_price=pair_snapshot["exit_price"],
                    exit_ts=requested_at,
                    exit_type=ExitType.MANUAL.value,
                    reason="fin_paper_trading",
                    extra_details={
                        "price_source": "last_closed_kline_5m",
                        "candle_close_utc": pair_snapshot["candle_close_utc"],
                    },
                )
                closed_open_trades += 1

            log.info(
                f"Fin paper trading completado: {closed_open_trades} trades cerrados "
                f"y {cancelled_pending} pendientes cancelados."
            )
            return {
                "session_started_at": self._session_started_at,
                "requested_at": requested_at,
                "active_trades_before_finish": len(active_trades),
                "closed_open_trades": closed_open_trades,
                "cancelled_pending_trades": cancelled_pending,
                "pair_prices": pair_prices,
            }
        except Exception:
            await self._restart_background_tasks_after_finish_error()
            raise

    async def on_signal(self, sig: Signal):
        if self._cfg.signal_filter_overlap and self.open_count_pair(sig.pair) > 0:
            log.info(
                f"Señal PAPER {sig.pair} descartada: filtro_overlap activo "
                "(hay operativa paper viva en el par)"
            )
            return

        ignore_reason = self._register_ignore_cycle(sig)
        if ignore_reason:
            log.info(f"Señal PAPER {sig.pair} descartada: {ignore_reason}")
            return

        if self._cfg.quarantine_hours > 0:
            last_closed = await self._db.get_last_paper_closed_time(sig.pair)
            if last_closed:
                now_utc = datetime.now(timezone.utc)
                if last_closed.tzinfo is None:
                    last_closed = last_closed.replace(tzinfo=timezone.utc)
                hours_since = (now_utc - last_closed).total_seconds() / 3600.0
                if hours_since < self._cfg.quarantine_hours:
                    log.info(
                        f"Señal PAPER {sig.pair} descartada: CUARENTENA "
                        f"(último cierre paper hace {hours_since:.2f}h, requiere {self._cfg.quarantine_hours}h)"
                    )
                    return

        trade = Trade(
            pair=sig.pair,
            signal_ts=sig.fecha_hora,
            signal_data=_signal_to_dict(sig),
            source="paper",
        )
        self._trades[trade.trade_id] = trade
        await self._db.save_paper_trade(trade)
        await self._emit(EventType.SIGNAL, trade.trade_id, {
            "pair": sig.pair,
            "top": sig.top,
            "rank": sig.rank,
            "mom_1h_pct": sig.mom_1h_pct,
            "mom_pct": sig.mom_pct,
            "bp": sig.bp,
            "close": sig.close,
            "paper": True,
        })
        log.info(f"Trade {trade.trade_id[:8]} SIGNAL_RECEIVED PAPER {sig.pair}")

        task = asyncio.create_task(
            self._open_trade(trade, sig),
            name=f"paper_open_{trade.trade_id[:8]}",
        )
        self._open_tasks.add(task)
        task.add_done_callback(self._open_tasks.discard)

    async def _open_trade(self, trade: Trade, sig: Signal):
        trade.status = TradeStatus.OPENING
        trade.touch()
        await self._db.save_paper_trade(trade)

        try:
            entry_price = await self._order_mgr.get_mark_price(sig.pair)
            qty = await self._order_mgr.calc_quantity(sig.pair, entry_price)

            trade.entry_price = entry_price
            trade.entry_quantity = qty
            trade.entry_fill_ts = datetime.now(timezone.utc).isoformat()
            trade.status = TradeStatus.OPEN
            self._ensure_trade_levels(trade)
            trade.touch()
            await self._db.save_paper_trade(trade)

            await self._emit(EventType.ENTRY_FILL, trade.trade_id, {
                "price": entry_price,
                "qty": qty,
                "paper": True,
            })
            await self._emit(EventType.TP_PLACED, trade.trade_id, {
                "stopPrice": trade.tp_trigger_price,
                "paper": True,
            })
            await self._emit(EventType.SL_PLACED, trade.trade_id, {
                "stopPrice": trade.sl_trigger_price,
                "paper": True,
            })
            self._reset_ignore_cycle(trade.pair)
            log.info(
                f"Trade {trade.trade_id[:8]} OPEN PAPER {trade.pair} "
                f"@ {entry_price} qty={qty}"
            )
        except Exception as e:
            log.error(f"Error abriendo trade paper {trade.trade_id[:8]} {sig.pair}: {e}", exc_info=True)
            trade.status = TradeStatus.ERROR
            trade.error_message = f"Paper open error: {e}"
            trade.touch()
            await self._db.save_paper_trade(trade)
            await self._emit(EventType.ERROR, trade.trade_id, {"msg": f"Paper open error: {e}", "paper": True})

    def _ensure_trade_levels(self, trade: Trade):
        if not trade.entry_price:
            return
        if trade.tp_trigger_price is None:
            trade.tp_trigger_price = trade.entry_price * (1 - self._cfg.tp_pct / 100.0)
        if trade.tp_price is None:
            trade.tp_price = trade.tp_trigger_price
        if trade.sl_trigger_price is None:
            trade.sl_trigger_price = trade.entry_price * (1 + self._cfg.sl_pct / 100.0)
        if trade.sl_price is None:
            trade.sl_price = trade.sl_trigger_price

    def _register_ignore_cycle(self, sig: Signal) -> Optional[str]:
        ignore_n = self._cfg.signal_ignore_n
        if ignore_n <= 0:
            return None

        if self.open_count_pair(sig.pair) > 0:
            return None

        state = self._ignore_cycles.get(sig.pair)
        signal_dt = sig.signal_dt or datetime.now(timezone.utc)
        ignore_h = self._cfg.signal_ignore_h

        if state and ignore_h > 0:
            window_started_at = state.get("window_started_at")
            if isinstance(window_started_at, datetime):
                elapsed_h = (signal_dt - window_started_at).total_seconds() / 3600.0
                if elapsed_h >= ignore_h:
                    self._ignore_cycles.pop(sig.pair, None)
                    state = None

        if state is None:
            state = {"ignored_count": 0, "window_started_at": signal_dt}

        if state["ignored_count"] < ignore_n:
            state["ignored_count"] += 1
            self._ignore_cycles[sig.pair] = state
            return (
                f"ignore_n activo ({state['ignored_count']}/{ignore_n})"
                + (f" en ventana de {ignore_h}h" if ignore_h > 0 else "")
            )

        self._ignore_cycles[sig.pair] = state
        return None

    def _reset_ignore_cycle(self, pair: str):
        if pair in self._ignore_cycles:
            self._ignore_cycles.pop(pair, None)
            log.debug(f"Ciclo ignore_n PAPER reseteado para {pair}")

    async def _timeout_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self._check_timeouts()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Error en paper timeout_loop: {e}", exc_info=True)

    async def _check_timeouts(self):
        now = datetime.now(timezone.utc)
        timeout_d = timedelta(hours=self._cfg.timeout_hours)
        for trade in list(self._trades.values()):
            if trade.status != TradeStatus.OPEN or not trade.entry_fill_ts:
                continue
            try:
                fill_dt = datetime.fromisoformat(trade.entry_fill_ts)
            except ValueError:
                continue
            if fill_dt.tzinfo is None:
                fill_dt = fill_dt.replace(tzinfo=timezone.utc)
            if now - fill_dt < timeout_d:
                continue

            await self._close_trade_now(
                trade,
                exit_type=ExitType.TIMEOUT.value,
                reason="hold",
                event_details={
                    "open_since": trade.entry_fill_ts,
                    "hours": (now - fill_dt).total_seconds() / 3600.0,
                },
            )

    async def _candle_loop(self):
        while True:
            try:
                await self._check_paper_tp_sl()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Error en paper candle_loop: {e}", exc_info=True)
            await asyncio.sleep(self._seconds_until_next_paper_candle_check())

    def _seconds_until_next_paper_candle_check(self) -> float:
        now_ts = time.time()
        next_close_ts = (int(now_ts / _PAPER_KLINE_INTERVAL_S) + 1) * _PAPER_KLINE_INTERVAL_S
        target_ts = next_close_ts + _PAPER_KLINE_GRACE_S
        return max(1.0, target_ts - now_ts)

    async def _check_paper_tp_sl(self):
        open_pairs = sorted({t.pair for t in self._trades.values() if t.status == TradeStatus.OPEN})
        for pair in open_pairs:
            try:
                kline = await self._order_mgr.get_last_closed_kline(pair, interval="5m")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_kline_fetch_failure(pair, e)
                continue
            self._log_kline_fetch_recovered(pair)
            close_time = int(kline["close_time"])
            if self._last_pair_candle_close_ms.get(pair) == close_time:
                continue
            self._last_pair_candle_close_ms[pair] = close_time
            price_ctx = self._resolve_paper_tp_sl_price_context(kline)
            exit_ts = datetime.now(timezone.utc).isoformat()

            pair_trades = [
                t for t in self._trades.values()
                if t.pair == pair and t.status == TradeStatus.OPEN
            ]
            trigger_trade, trigger_type = self._select_pair_trigger_trade(
                pair_trades,
                tp_check_price=price_ctx["tp_check_price"],
                sl_check_price=price_ctx["sl_check_price"],
            )
            if trigger_trade and trigger_type:
                exit_price = (
                    price_ctx["tp_exit_price"]
                    if trigger_type == ExitType.TP.value
                    else price_ctx["sl_exit_price"]
                )
                await self._close_trade_by_signal(
                    trigger_trade,
                    trigger_type,
                    exit_price,
                    exit_ts,
                    price_ctx["price_source"],
                )

    def _select_pair_trigger_trade(self,
                                   pair_trades: list[Trade],
                                   tp_check_price: float,
                                   sl_check_price: float) -> tuple[Optional[Trade], Optional[str]]:
        eligible_trades = [
            t for t in pair_trades
            if self._trades.get(t.trade_id) is t and t.status == TradeStatus.OPEN
        ]
        if not eligible_trades:
            return None, None

        sl_candidates = [
            t for t in eligible_trades
            if t.sl_trigger_price is not None and sl_check_price >= t.sl_trigger_price
        ]
        if sl_candidates:
            # En SHORT, el SL que se toca primero es el de trigger mas bajo.
            return min(
                sl_candidates,
                key=lambda t: (
                    float(t.sl_trigger_price or 0.0),
                    t.entry_fill_ts or t.created_at,
                ),
            ), ExitType.SL.value

        tp_candidates = [
            t for t in eligible_trades
            if t.tp_trigger_price is not None and tp_check_price <= t.tp_trigger_price
        ]
        if tp_candidates:
            # En SHORT, el TP que se toca primero es el de trigger mas alto.
            return min(
                tp_candidates,
                key=lambda t: (
                    -float(t.tp_trigger_price or 0.0),
                    t.entry_fill_ts or t.created_at,
                ),
            ), ExitType.TP.value

        return None, None

    def _resolve_paper_tp_sl_price_context(self, kline: dict) -> dict[str, float | str]:
        close_price = float(kline["close"])
        if self._cfg.paper_tp_sl_price_mode == "high_low_5m":
            return {
                "tp_check_price": float(kline["low"]),
                "sl_check_price": float(kline["high"]),
                "tp_exit_price": float(kline["low"]),
                "sl_exit_price": float(kline["high"]),
                "price_source": "kline_5m_high_low",
            }

        return {
            "tp_check_price": close_price,
            "sl_check_price": close_price,
            "tp_exit_price": close_price,
            "sl_exit_price": close_price,
            "price_source": "kline_5m_close",
        }

    def _log_kline_fetch_failure(self, pair: str, error: Exception):
        now = time.monotonic()
        state = self._pair_kline_failures.setdefault(
            pair,
            {"count": 0, "last_log_at": 0.0},
        )
        state["count"] += 1
        if now - state["last_log_at"] < _KLINE_WARNING_INTERVAL_S:
            return

        state["last_log_at"] = now
        log.warning(
            f"No se pudo revisar TP/SL PAPER para {pair} en vela 5m: {error} "
            f"(fallos consecutivos: {state['count']})"
        )

    def _log_kline_fetch_recovered(self, pair: str):
        state = self._pair_kline_failures.pop(pair, None)
        if not state or state.get("count", 0) <= 0:
            return

        log.info(
            f"Recuperada la revision TP/SL PAPER de {pair} tras "
            f"{state['count']} fallo(s) consecutivo(s)"
        )

    async def _close_trade_by_signal(self,
                                     trade: Trade,
                                     exit_type: str,
                                     exit_price: float,
                                     exit_ts: str,
                                     price_source: str):
        trade.status = TradeStatus.CLOSING
        trade.exit_price = exit_price
        trade.exit_fill_ts = exit_ts
        trade.exit_type = exit_type
        trade.touch()
        await self._db.save_paper_trade(trade)

        event_type = EventType.TP_FILL if exit_type == ExitType.TP.value else EventType.SL_FILL
        await self._emit(event_type, trade.trade_id, {
            "price": exit_price,
            "paper": True,
            "candle_5m": True,
            "price_source": price_source,
        })
        await self._close_trade(trade)

        if exit_type == ExitType.TP.value and self._cfg.tp_posicion:
            await self._close_sibling_trades(
                trade.pair,
                "TP",
                trade.trade_id,
                exit_price,
                exit_ts,
                price_source,
            )
        if exit_type == ExitType.SL.value and self._cfg.sl_posicion:
            await self._close_sibling_trades(
                trade.pair,
                "SL",
                trade.trade_id,
                exit_price,
                exit_ts,
                price_source,
            )

    async def _close_sibling_trades(self,
                                    pair: str,
                                    trigger_type: str,
                                    exclude_trade_id: str,
                                    exit_price: float,
                                    exit_ts: str,
                                    price_source: str):
        siblings = [
            t for t in list(self._trades.values())
            if t.pair == pair and t.trade_id != exclude_trade_id and t.status == TradeStatus.OPEN
        ]
        if not siblings:
            return

        if trigger_type == "TP":
            min_tp_pct = self._cfg.min_tp_posicion_pct
            if min_tp_pct > 0:
                total_cost = sum((t.entry_price or 0.0) * (t.entry_quantity or 0.0) for t in siblings)
                total_value = sum(exit_price * (t.entry_quantity or 0.0) for t in siblings)
                if total_cost > 0:
                    combined_pnl_pct = ((total_cost - total_value) / total_cost) * 100.0
                    if combined_pnl_pct < min_tp_pct:
                        log.info(
                            f"Cascada TP PAPER abortada para {pair}: PnL combinado hermanos "
                            f"({combined_pnl_pct:.2f}%) < Min_TP_posicion ({min_tp_pct:.2f}%)"
                        )
                        return

        log.info(
            f"Cierre en cascada PAPER ({trigger_type}_posicion=True) "
            f"para {len(siblings)} trades de {pair}"
        )

        for trade in siblings:
            trade.status = TradeStatus.CLOSING
            trade.exit_price = exit_price
            trade.exit_fill_ts = exit_ts
            trade.exit_type = f"{trigger_type}_CASCADE_PAPER_5M"
            trade.touch()
            await self._db.save_paper_trade(trade)
            await self._emit(
                EventType.TP_FILL if trigger_type == "TP" else EventType.SL_FILL,
                trade.trade_id,
                {
                    "price": exit_price,
                    "paper": True,
                    "cascade": True,
                    "candle_5m": True,
                    "price_source": price_source,
                },
            )
            await self._close_trade(trade)

    async def _close_trade_now(self,
                               trade: Trade,
                               exit_type: str,
                               reason: str,
                               event_details: Optional[dict] = None):
        if self._trades.get(trade.trade_id) is not trade:
            return
        exit_price = trade.entry_price or 0.0
        try:
            exit_price = await self._order_mgr.get_mark_price(trade.pair)
        except Exception as e:
            log.warning(
                f"Trade {trade.trade_id[:8]} no pudo obtener mark price "
                f"para cierre paper inmediato ({reason}): {e}"
            )

        trade.status = TradeStatus.CLOSING
        trade.exit_price = exit_price
        trade.exit_fill_ts = datetime.now(timezone.utc).isoformat()
        trade.exit_type = exit_type
        trade.touch()
        await self._db.save_paper_trade(trade)

        details = dict(event_details or {})
        details.setdefault("reason", reason)

        if exit_type == ExitType.TIMEOUT.value:
            await self._emit(EventType.TIMEOUT, trade.trade_id, details)
        else:
            await self._emit(EventType.CANCEL, trade.trade_id, details)
        await self._close_trade(trade)

    async def _close_trade_at_price(self,
                                    trade: Trade,
                                    exit_price: float,
                                    exit_ts: str,
                                    exit_type: str,
                                    reason: str,
                                    extra_details: Optional[dict] = None):
        if self._trades.get(trade.trade_id) is not trade:
            return

        trade.status = TradeStatus.CLOSING
        trade.exit_price = exit_price
        trade.exit_fill_ts = exit_ts
        trade.exit_type = exit_type
        trade.touch()
        await self._db.save_paper_trade(trade)

        details = dict(extra_details or {})
        details.setdefault("reason", reason)
        details.setdefault("paper", True)
        await self._emit(EventType.CANCEL, trade.trade_id, details)
        await self._close_trade(trade)

    async def _cancel_pending_trade_for_finish(self, trade: Trade, exit_ts: str):
        if self._trades.get(trade.trade_id) is not trade:
            return

        trade.status = TradeStatus.NOT_EXECUTED
        trade.error_message = "Paper trading finalizado antes del fill de entrada"
        trade.exit_fill_ts = exit_ts
        trade.exit_type = ExitType.MANUAL.value
        trade.touch()
        await self._db.save_paper_trade(trade)
        self._trades.pop(trade.trade_id, None)
        await self._emit(
            EventType.CANCEL,
            trade.trade_id,
            {
                "reason": "fin_paper_trading_pending",
                "paper": True,
            },
        )

    async def _stop_background_tasks_for_finish(self):
        for attr in ("_timeout_task", "_candle_task"):
            task = getattr(self, attr, None)
            if not task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            setattr(self, attr, None)

    async def _restart_background_tasks_after_finish_error(self):
        if self._timeout_task is None:
            self._timeout_task = asyncio.create_task(self._timeout_loop(), name="paper_timeout_checker")
        if self._candle_task is None:
            self._candle_task = asyncio.create_task(self._candle_loop(), name="paper_candle_checker")

    async def _close_trade(self, trade: Trade):
        if trade.entry_price and trade.exit_price and trade.entry_quantity:
            pnl_pct = ((trade.entry_price - trade.exit_price) / trade.entry_price * 100)
            pnl_usdt = (trade.entry_price - trade.exit_price) * trade.entry_quantity
            trade.pnl_pct = round(pnl_pct, 4)
            trade.pnl_usdt = round(pnl_usdt, 4)
            trade.fees_usdt = 0.0

        trade.status = TradeStatus.CLOSED
        trade.touch()
        await self._db.save_paper_trade(trade)
        self._trades.pop(trade.trade_id, None)

        pnl_u = trade.pnl_usdt or 0.0
        pnl_p = trade.pnl_pct or 0.0
        sign = "+" if pnl_u >= 0 else ""
        log.info(
            f"Trade {trade.trade_id[:8]} CLOSED PAPER [{trade.exit_type}] "
            f"{trade.pair} PnL={sign}{pnl_u:.4f} USDT "
            f"({sign}{pnl_p:.2f}%)"
        )

    async def _emit(self, etype: EventType, trade_id: Optional[str], details: dict):
        payload = dict(details)
        payload.setdefault("paper", True)
        ev = Event(trade_id=trade_id, event_type=etype.value, details=payload)
        try:
            await self._on_event(ev)
        except Exception as e:
            log.debug(f"Error emitiendo evento paper {etype}: {e}")


def _signal_to_dict(sig: Signal) -> dict:
    return {
        "fecha_hora": sig.fecha_hora,
        "pair": sig.pair,
        "top": sig.top,
        "rank": sig.rank,
        "close": sig.close,
        "mom_1h_pct": sig.mom_1h_pct,
        "mom_pct": sig.mom_pct,
        "vol_ratio": sig.vol_ratio,
        "trades_ratio": sig.trades_ratio,
        "quintil": sig.quintil,
        "bp": sig.bp,
        "categoria": sig.categoria,
    }
