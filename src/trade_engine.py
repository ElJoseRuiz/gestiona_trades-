"""
trade_engine.py — Máquina de estados del ciclo de vida de un trade.

Estados:
  SIGNAL_RECEIVED → OPENING → OPEN → CLOSING → CLOSED
                         ↓
                    NOT_EXECUTED

Flujo:
  1. on_signal()     → crea Trade, intenta abrir con chase loop (BBO limit)
  2. on_entry_fill() → coloca TP y SL, Trade → OPEN
  3. on_tp_fill()    → cancela SL, cierra Trade → CLOSED (TP)
  4. on_sl_fill()    → cancela TP, cierra Trade → CLOSED (SL)
  5. timeout check   → cancela TP + SL, cierra con limit/market → CLOSED (timeout)

TP : TAKE_PROFIT CONDITIONAL (algo) con priceMatch (BBO).
     stopPrice = entry * (1 - tp_pct/100). Vive en Binance.

SL : STOP_MARKET CONDITIONAL (algo) con workingType=MARK_PRICE.
     stopPrice = entry * (1 + sl_pct/100). Vive en Binance.

Ambas vía /fapi/v1/algoOrder con algoType="CONDITIONAL".
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Callable, Awaitable, Dict, Optional

from .config import Config
from .logger import get_logger
from .models import Event, EventType, Signal, Trade, TradeStatus, ExitType
from .order_manager import BinanceError, OrderManager
from .state import StateDB
from .ws_manager import WSManager

log = get_logger("trade_engine")

OnEventCallback = Callable[[Event], Awaitable[None]]


class TradeEngine:
    def __init__(self,
                 cfg:        Config,
                 order_mgr:  OrderManager,
                 ws_mgr:     WSManager,
                 db:         StateDB,
                 on_event:   OnEventCallback):
        self._cfg        = cfg
        self._order_mgr  = order_mgr
        self._ws_mgr     = ws_mgr
        self._db         = db
        self._on_event   = on_event

        # Trades activos en memoria {trade_id: Trade}
        self._trades:      Dict[str, Trade] = {}
        # Mapas rápidos para lookup por order_id
        self._by_entry:    Dict[int, str]   = {}   # entry_order_id → trade_id
        self._by_tp:       Dict[int, str]   = {}   # tp_order_id    → trade_id
        self._by_sl:       Dict[int, str]   = {}   # sl_order_id    → trade_id

        self._timeout_task: Optional[asyncio.Task] = None
        self._open_tasks:   set = set()   # tareas _open_trade en curso

    # ──────────────────────────────────────────────────────────────────
    # Arranque / Parada
    # ──────────────────────────────────────────────────────────────────

    async def start(self):
        self._timeout_task = asyncio.create_task(
            self._timeout_loop(), name="timeout_checker"
        )
        log.info("TradeEngine iniciado")

    async def stop(self):
        if self._timeout_task:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass

        # Cancelar apertura de trades en curso y esperar que limpien la DB
        if self._open_tasks:
            for task in list(self._open_tasks):
                task.cancel()
            await asyncio.gather(*list(self._open_tasks), return_exceptions=True)

        log.info(f"TradeEngine detenido. Trades abiertos: {self.open_count}")

    # ──────────────────────────────────────────────────────────────────
    # Propiedades públicas
    # ──────────────────────────────────────────────────────────────────

    @property
    def open_count(self) -> int:
        return sum(1 for t in self._trades.values()
                   if t.status in (TradeStatus.OPEN, TradeStatus.OPENING,
                                   TradeStatus.SIGNAL_RECEIVED))

    def open_count_pair(self, pair: str) -> int:
        return sum(1 for t in self._trades.values()
                   if t.pair == pair and t.status in
                   (TradeStatus.OPEN, TradeStatus.OPENING,
                    TradeStatus.SIGNAL_RECEIVED))

    def get_active_trades(self) -> list:
        return [t for t in self._trades.values()
                if t.status not in (TradeStatus.CLOSED,
                                    TradeStatus.NOT_EXECUTED,
                                    TradeStatus.ERROR)]

    # ──────────────────────────────────────────────────────────────────
    # Reconciliación al arrancar
    # ──────────────────────────────────────────────────────────────────

    async def reconcile(self, db_trades: list[Trade]):
        """
        Reconciliación al arrancar. Para cada trade activo en la DB:

          OPEN    – verifica que la posición exista en Binance; comprueba que
                    las órdenes de TP y SL estén activas y las re-coloca si
                    faltan.
          OPENING – consulta el estado de la orden de entrada en Binance:
                    si se llenó durante el apagado → promueve a OPEN y coloca
                    TP/SL; en otro caso → NOT_EXECUTED.
          CLOSING – si la posición ya desapareció de Binance → CLOSED;
                    si sigue → restaura a OPEN y reconcilia TP/SL.

        Además avisa de posiciones abiertas en Binance sin trade en DB.
        """
        if not db_trades:
            log.info("Reconciliación: sin trades activos en DB")
            return

        log.info(f"Reconciliando {len(db_trades)} trades de la DB...")

        # Obtener todas las posiciones Binance de una sola llamada
        try:
            all_positions = await self._order_mgr.get_all_positions()
            binance_pairs = {p["symbol"] for p in all_positions}
            log.info(
                f"Reconciliación: {len(all_positions)} posición(es) abiertas "
                f"en Binance: {binance_pairs or '(ninguna)'}"
            )
        except Exception as e:
            log.error(f"Reconciliación: no se pudieron obtener posiciones: {e}")
            binance_pairs = set()

        db_open_pairs: set[str] = set()

        for t in db_trades:
            self._trades[t.trade_id] = t   # cargar en memoria siempre
            try:
                if t.status == TradeStatus.OPEN:
                    await self._reconcile_open(t, binance_pairs)
                    if t.status == TradeStatus.OPEN:
                        db_open_pairs.add(t.pair)

                elif t.status in (TradeStatus.OPENING,
                                  TradeStatus.SIGNAL_RECEIVED):
                    await self._reconcile_opening(t, binance_pairs)
                    if t.status == TradeStatus.OPEN:
                        db_open_pairs.add(t.pair)

                elif t.status == TradeStatus.CLOSING:
                    await self._reconcile_closing(t, binance_pairs)
                    if t.status == TradeStatus.OPEN:
                        db_open_pairs.add(t.pair)

                log.info(
                    f"Reconciliación: trade {t.trade_id[:8]} ({t.pair}) "
                    f"→ {t.status.value}"
                )
            except Exception as e:
                log.error(
                    f"Reconciliación: error en {t.trade_id[:8]}: {e}",
                    exc_info=True
                )

        # Posiciones Binance sin trade en DB → aviso
        for pair in binance_pairs - db_open_pairs:
            log.warning(
                f"Reconciliación: posición abierta en Binance para {pair} "
                "sin trade correspondiente en DB → revisar manualmente"
            )

    async def _reconcile_open(self, t: Trade, binance_pairs: set):
        """Trade OPEN: verifica posición y comprueba/re-coloca TP y SL."""
        if t.pair not in binance_pairs:
            log.warning(
                f"Reconciliación: trade {t.trade_id[:8]} ({t.pair}) "
                "OPEN en DB pero sin posición en Binance "
                "→ CLOSED (cerrado externamente)"
            )
            t.status   = TradeStatus.CLOSED
            t.exit_type = ExitType.MANUAL.value
            t.touch()
            await self._db.save_trade(t)
            self._trades.pop(t.trade_id, None)
            await self._emit(EventType.ERROR, t.trade_id, {
                "msg": "Reconciliación: posición cerrada externamente"
            })
            return

        # Obtener órdenes abiertas del par (regulares + algo)
        try:
            open_orders = await self._order_mgr.get_open_orders(t.pair)
            open_oids   = {int(o["orderId"]) for o in open_orders}
        except Exception as e:
            log.error(
                f"Reconciliación: error obteniendo órdenes de {t.pair}: {e}"
            )
            open_oids = set()
        try:
            algo_orders = await self._order_mgr.get_open_algo_orders(t.pair)
            open_oids  |= {int(o["orderId"]) for o in algo_orders}
        except Exception as e:
            log.debug(f"Reconciliación: get_open_algo_orders({t.pair}): {e}")

        # ── TP ──
        if t.tp_order_id and int(t.tp_order_id) in open_oids:
            tp_oid = int(t.tp_order_id)
            self._by_tp[tp_oid] = t.trade_id
            self._ws_mgr.register_tp(tp_oid)
            log.info(
                f"Reconciliación: trade {t.trade_id[:8]} "
                f"TP {tp_oid} re-registrado"
            )
        else:
            if t.tp_order_id:
                log.warning(
                    f"Reconciliación: trade {t.trade_id[:8]} "
                    f"TP {t.tp_order_id} no encontrado → re-colocando"
                )
            else:
                log.warning(
                    f"Reconciliación: trade {t.trade_id[:8]} sin TP → colocando"
                )
            await self._place_one_tp(t)

        # ── SL ──
        if t.sl_order_id and int(t.sl_order_id) in open_oids:
            sl_oid = int(t.sl_order_id)
            self._by_sl[sl_oid] = t.trade_id
            self._ws_mgr.register_sl(sl_oid)
            log.info(
                f"Reconciliación: trade {t.trade_id[:8]} "
                f"SL {sl_oid} re-registrado"
            )
        else:
            if t.sl_order_id:
                log.warning(
                    f"Reconciliación: trade {t.trade_id[:8]} "
                    f"SL {t.sl_order_id} no encontrado → re-colocando"
                )
            else:
                log.warning(
                    f"Reconciliación: trade {t.trade_id[:8]} sin SL → colocando"
                )
            await self._place_one_sl(t)

    async def _reconcile_opening(self, t: Trade, binance_pairs: set):
        """Trade OPENING: consulta si la orden de entrada se llenó mientras
        el sistema estaba parado."""
        if not t.entry_order_id:
            log.warning(
                f"Reconciliación: trade {t.trade_id[:8]} OPENING "
                "sin entry_order_id → NOT_EXECUTED"
            )
            t.status = TradeStatus.NOT_EXECUTED
            t.touch()
            await self._db.save_trade(t)
            self._trades.pop(t.trade_id, None)
            return

        try:
            order  = await self._order_mgr.get_order(t.pair, t.entry_order_id)
            status = order.get("status", "")
        except Exception as e:
            log.error(
                f"Reconciliación: no se pudo obtener orden "
                f"{t.entry_order_id}: {e}"
            )
            t.status = TradeStatus.NOT_EXECUTED
            t.touch()
            await self._db.save_trade(t)
            self._trades.pop(t.trade_id, None)
            return

        if status == "FILLED":
            avg_price = float(order.get("avgPrice") or order.get("price") or 0)
            log.info(
                f"Reconciliación: trade {t.trade_id[:8]} entrada FILLED "
                f"durante apagado @ {avg_price} → promoviendo a OPEN"
            )
            t.entry_price   = avg_price
            t.entry_fill_ts = (t.entry_fill_ts
                               or datetime.now(timezone.utc).isoformat())
            t.status = TradeStatus.OPEN
            t.touch()
            await self._db.save_trade(t)
            await self._emit(EventType.ENTRY_FILL, t.trade_id, {
                "orderId": t.entry_order_id,
                "price": avg_price,
                "qty": t.entry_quantity,
                "reconcile": True,
            })
            await self._place_tp_sl(t)
        else:
            # NEW, PARTIALLY_FILLED, CANCELED, EXPIRED → cancelar y descartar
            if status in ("NEW", "PARTIALLY_FILLED"):
                try:
                    await self._order_mgr.cancel_order(
                        t.pair, t.entry_order_id
                    )
                except Exception:
                    pass
            log.warning(
                f"Reconciliación: trade {t.trade_id[:8]} "
                f"entrada status={status} → NOT_EXECUTED"
            )
            t.status = TradeStatus.NOT_EXECUTED
            t.touch()
            await self._db.save_trade(t)
            self._trades.pop(t.trade_id, None)

    async def _reconcile_closing(self, t: Trade, binance_pairs: set):
        """Trade CLOSING: si la posición ya no existe → CLOSED;
        si sigue abierta → restaura a OPEN y reconcilia TP/SL."""
        if t.pair not in binance_pairs:
            log.info(
                f"Reconciliación: trade {t.trade_id[:8]} CLOSING "
                "→ posición ya cerrada en Binance → CLOSED"
            )
            if not t.exit_price:
                t.exit_price = 0.0
            t.exit_fill_ts = (t.exit_fill_ts
                              or datetime.now(timezone.utc).isoformat())
            t.exit_type = t.exit_type or ExitType.MANUAL.value
            await self._close_trade(t)
        else:
            log.warning(
                f"Reconciliación: trade {t.trade_id[:8]} CLOSING "
                "pero posición sigue en Binance → restaurando a OPEN"
            )
            t.status = TradeStatus.OPEN
            t.touch()
            await self._db.save_trade(t)
            await self._reconcile_open(t, binance_pairs)

    # ──────────────────────────────────────────────────────────────────
    # on_signal: entrada de señal
    # ──────────────────────────────────────────────────────────────────

    async def on_signal(self, sig: Signal):
        # Límites globales
        if self.open_count >= self._cfg.max_open_trades:
            log.info(
                f"Señal {sig.pair} descartada: max_open_trades "
                f"({self._cfg.max_open_trades}) alcanzado"
            )
            return
        if self.open_count_pair(sig.pair) >= self._cfg.max_trades_per_pair:
            log.info(
                f"Señal {sig.pair} descartada: max_trades_per_pair "
                f"({self._cfg.max_trades_per_pair}) alcanzado"
            )
            return

        trade = Trade(pair=sig.pair, signal_ts=sig.fecha_hora,
                      signal_data=_signal_to_dict(sig))
        self._trades[trade.trade_id] = trade

        await self._db.save_trade(trade)
        await self._emit(EventType.SIGNAL, trade.trade_id, {
            "pair": sig.pair, "top": sig.top,
            "mom_1h_pct": sig.mom_1h_pct, "close": sig.close,
        })
        log.info(f"Trade {trade.trade_id[:8]} SIGNAL_RECEIVED {sig.pair}")

        # Lanzar apertura en background para no bloquear el watcher
        task = asyncio.create_task(
            self._open_trade(trade, sig),
            name=f"open_{trade.trade_id[:8]}"
        )
        self._open_tasks.add(task)
        task.add_done_callback(self._open_tasks.discard)

    # ──────────────────────────────────────────────────────────────────
    # Apertura (chase loop)
    # ──────────────────────────────────────────────────────────────────

    async def _open_trade(self, trade: Trade, sig: Signal):
        trade.status = TradeStatus.OPENING
        trade.touch()
        await self._db.save_trade(trade)

        cfg = self._cfg
        try:
            for attempt in range(1, cfg.max_chase_attempts + 1):
                try:
                    # Precio de referencia solo para calc_quantity;
                    # la orden BBO no usa precio explícito.
                    ref_price   = await self._order_mgr.get_best_bid(sig.pair)
                    qty         = await self._order_mgr.calc_quantity(sig.pair, ref_price)
                    # Attempt 1 → BBO Counterparty 5 (5º mejor bid, entrada más conservadora)
                    # Chase     → BBO Counterparty 1 (mejor bid, máxima prioridad de fill)
                    price_match = "OPPONENT_5" if attempt == 1 else "OPPONENT"

                    result   = await self._order_mgr.open_short(
                        sig.pair, qty, price_match=price_match
                    )
                    order_id = int(result["orderId"])

                    trade.entry_order_id = order_id
                    trade.entry_quantity = qty
                    trade.touch()
                    await self._db.save_trade(trade)
                    await self._emit(EventType.ENTRY_SENT, trade.trade_id, {
                        "orderId": order_id, "priceMatch": price_match,
                        "qty": qty, "attempt": attempt,
                    })
                    self._by_entry[order_id] = trade.trade_id
                    self._ws_mgr.register_entry(order_id)

                    log.info(
                        f"Trade {trade.trade_id[:8]} OPENING attempt {attempt}: "
                        f"orderId={order_id} priceMatch={price_match} qty={qty}"
                    )

                    # Esperar fill durante chase_timeout_seconds
                    filled = await self._wait_fill(
                        trade, order_id, cfg.chase_timeout_seconds
                    )
                    if filled:
                        return   # on_entry_fill() lo llevará a OPEN

                    # No fill → cancelar y reintentar
                    log.info(
                        f"Trade {trade.trade_id[:8]}: sin fill en "
                        f"{cfg.chase_timeout_seconds}s (attempt {attempt})"
                    )
                    try:
                        await self._order_mgr.cancel_order(sig.pair, order_id)
                    except BinanceError as e:
                        # Si ya se llenó en la cancelación, lo captura el WS
                        log.warning(f"Cancel order {order_id}: {e}")
                    self._ws_mgr.unregister(order_id)
                    self._by_entry.pop(order_id, None)

                    if attempt < cfg.max_chase_attempts:
                        await asyncio.sleep(cfg.chase_interval_seconds)

                except BinanceError as e:
                    log.error(
                        f"Trade {trade.trade_id[:8]} apertura error attempt {attempt}: {e}",
                        exc_info=True
                    )
                    await self._emit(EventType.ERROR, trade.trade_id, {
                        "attempt": attempt, "error": str(e)
                    })
                    if attempt < cfg.max_chase_attempts:
                        await asyncio.sleep(cfg.chase_interval_seconds)
                except Exception as e:
                    log.error(f"Error inesperado abriendo {sig.pair}: {e}", exc_info=True)
                    break

            # Agotados los intentos BBO/LIMIT → MARKET fallback si configurado
            if self._cfg.entry_market_fallback:
                try:
                    ref_price = await self._order_mgr.get_best_bid(sig.pair)
                    qty       = await self._order_mgr.calc_quantity(sig.pair, ref_price)
                    result    = await self._order_mgr.open_short_market(sig.pair, qty)
                    order_id  = int(result["orderId"])
                    trade.entry_order_id = order_id
                    trade.entry_quantity = qty
                    trade.touch()
                    await self._db.save_trade(trade)
                    self._by_entry[order_id] = trade.trade_id
                    self._ws_mgr.register_entry(order_id)
                    await self._emit(EventType.ENTRY_SENT, trade.trade_id, {
                        "orderId": order_id, "type": "MARKET", "qty": qty,
                    })
                    log.info(
                        f"Trade {trade.trade_id[:8]} OPENING MARKET fallback: "
                        f"orderId={order_id} qty={qty}"
                    )
                    filled = await self._wait_fill(trade, order_id, 10.0)
                    if filled:
                        return  # on_entry_fill() lo llevará a OPEN
                    log.error(
                        f"Trade {trade.trade_id[:8]} MARKET fallback sin fill en 10s"
                    )
                    self._ws_mgr.unregister(order_id)
                    self._by_entry.pop(order_id, None)
                except Exception as e:
                    log.error(
                        f"Trade {trade.trade_id[:8]} MARKET fallback error: {e}",
                        exc_info=True
                    )

            log.warning(
                f"Trade {trade.trade_id[:8]} NOT_EXECUTED: "
                f"no fill tras {attempt} intento(s)"
            )
            trade.status = TradeStatus.NOT_EXECUTED
            trade.touch()
            await self._db.save_trade(trade)
            await self._emit(EventType.ERROR, trade.trade_id, {
                "msg": "NOT_EXECUTED: sin fill tras todos los intentos"
            })
            self._trades.pop(trade.trade_id, None)

        except asyncio.CancelledError:
            # Shutdown mientras la apertura estaba en curso
            log.info(f"Trade {trade.trade_id[:8]} apertura cancelada (shutdown)")
            # Cancelar orden pendiente en Binance si existe
            if trade.entry_order_id and trade.status == TradeStatus.OPENING:
                try:
                    await asyncio.shield(
                        self._order_mgr.cancel_order(sig.pair, trade.entry_order_id)
                    )
                except Exception:
                    pass
                self._ws_mgr.unregister(trade.entry_order_id)
                self._by_entry.pop(trade.entry_order_id, None)
            trade.status = TradeStatus.NOT_EXECUTED
            trade.touch()
            try:
                await asyncio.shield(self._db.save_trade(trade))
            except Exception:
                pass
            self._trades.pop(trade.trade_id, None)
            raise

    async def _wait_fill(self, trade: Trade, order_id: int,
                         timeout: float) -> bool:
        """Espera hasta timeout. Devuelve True si fue fill detectado por WS."""
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < timeout:
            if trade.status == TradeStatus.OPEN:
                return True
            if trade.status == TradeStatus.NOT_EXECUTED:
                return False
            await asyncio.sleep(0.2)
        return False

    # ──────────────────────────────────────────────────────────────────
    # Callbacks del WebSocket
    # ──────────────────────────────────────────────────────────────────

    async def on_entry_fill(self, order_data: dict):
        order_id = int(order_data.get("i", 0))
        trade_id = self._by_entry.pop(order_id, None)
        if not trade_id:
            log.warning(f"on_entry_fill: trade no encontrado para orderId={order_id}")
            return
        trade = self._trades.get(trade_id)
        if not trade:
            return

        entry_price = float(order_data.get("ap") or order_data.get("L") or 0)
        fill_ts     = datetime.now(timezone.utc).isoformat()

        trade.entry_price   = entry_price
        trade.entry_fill_ts = fill_ts
        trade.status        = TradeStatus.OPEN
        trade.touch()
        await self._db.save_trade(trade)
        await self._emit(EventType.ENTRY_FILL, trade_id, {
            "orderId": order_id, "price": entry_price,
            "qty": trade.entry_quantity,
        })
        log.info(
            f"Trade {trade_id[:8]} OPEN: entry filled at {entry_price} "
            f"qty={trade.entry_quantity}"
        )

        # Colocar TP y SL inmediatamente
        await self._place_tp_sl(trade)

    async def _place_tp_sl(self, trade: Trade):
        await self._place_one_tp(trade)
        await self._place_one_sl(trade)

    async def _place_one_tp(self, trade: Trade):
        try:
            tp_result = await self._order_mgr.place_tp(
                trade.pair, trade.entry_quantity, trade.entry_price
            )
            tp_oid                 = int(tp_result["orderId"])
            trade.tp_order_id      = str(tp_oid)
            # Algo TAKE_PROFIT: el precio de ejecución (BBO) no se conoce hasta
            # el fill; guardamos el triggerPrice como referencia del nivel de trigger.
            trade.tp_trigger_price = float(tp_result.get("triggerPrice", 0))
            trade.tp_price         = trade.tp_trigger_price   # aproximación hasta fill
            self._by_tp[tp_oid]    = trade.trade_id
            self._ws_mgr.register_tp(tp_oid)
            trade.touch()
            await self._db.save_trade(trade)
            await self._emit(EventType.TP_PLACED, trade.trade_id, {
                "orderId": tp_oid,
                "stopPrice": trade.tp_trigger_price,
            })
            log.info(
                f"Trade {trade.trade_id[:8]} TP colocado (Algo TAKE_PROFIT): "
                f"algoId={tp_oid} stopPrice={trade.tp_trigger_price}"
            )
        except Exception as e:
            log.error(f"Error colocando TP {trade.pair}: {e}", exc_info=True)
            await self._emit(EventType.ERROR, trade.trade_id, {"msg": f"TP error: {e}"})

    async def _place_one_sl(self, trade: Trade):
        """
        Coloca STOP_MARKET Algo (algoType=CONDITIONAL, workingType=MARK_PRICE)
        en Binance. La orden vive en Binance aunque el proceso se reinicie.
          triggerPrice = entry * (1 + sl_pct/100)

        Si Binance responde -2021 (el precio ya superó el trigger) el trade
        se cierra inmediatamente con una orden MARKET.
        """
        try:
            sl_result = await self._order_mgr.place_sl(
                trade.pair, trade.entry_quantity, trade.entry_price
            )
            sl_oid                 = int(sl_result["orderId"])
            trade.sl_order_id      = str(sl_oid)
            trade.sl_trigger_price = float(sl_result.get("triggerPrice", 0))
            self._by_sl[sl_oid]    = trade.trade_id
            self._ws_mgr.register_sl(sl_oid)
            trade.touch()
            await self._db.save_trade(trade)
            await self._emit(EventType.SL_PLACED, trade.trade_id, {
                "orderId": sl_oid,
                "stopPrice": trade.sl_trigger_price,
            })
            log.info(
                f"Trade {trade.trade_id[:8]} SL colocado (Algo STOP_MARKET): "
                f"algoId={sl_oid} stopPrice={trade.sl_trigger_price}"
            )
        except BinanceError as e:
            if e.code == -2021:
                # El precio ya superó el SL → cerrar posición inmediatamente
                log.warning(
                    f"Trade {trade.trade_id[:8]} {trade.pair}: SL superado "
                    f"(triggerPrice ya cruzado) → cerrando con MARKET"
                )
                try:
                    result = await self._order_mgr.close_position_market(
                        trade.pair, trade.entry_quantity
                    )
                    exit_price = float(result.get("avgPrice") or 0)
                    if not exit_price:
                        exit_price = float(result.get("price") or 0)
                    if not exit_price:
                        log.warning(
                            f"Trade {trade.trade_id[:8]} {trade.pair}: "
                            f"avgPrice=0 en respuesta MARKET, PnL no calculable"
                        )
                    trade.status       = TradeStatus.CLOSING
                    trade.exit_price   = exit_price
                    trade.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                    trade.exit_type    = ExitType.SL.value
                    await self._cancel_counterpart(trade, "tp")
                    await self._close_trade(trade)
                except Exception as close_err:
                    log.error(
                        f"Trade {trade.trade_id[:8]} error cerrando MARKET tras -2021: "
                        f"{close_err}", exc_info=True
                    )
                    await self._emit(EventType.ERROR, trade.trade_id,
                                     {"msg": f"SL -2021 close error: {close_err}"})
            else:
                log.error(f"Error colocando SL {trade.pair}: {e}", exc_info=True)
                await self._emit(EventType.ERROR, trade.trade_id,
                                 {"msg": f"SL error: {e}"})
        except Exception as e:
            log.error(f"Error colocando SL {trade.pair}: {e}", exc_info=True)
            await self._emit(EventType.ERROR, trade.trade_id,
                             {"msg": f"SL error: {e}"})

    async def on_tp_fill(self, order_data: dict):
        order_id = int(order_data.get("i", 0))
        trade_id = self._by_tp.pop(order_id, None)
        if not trade_id:
            return
        trade = self._trades.get(trade_id)
        if not trade or trade.status not in (TradeStatus.OPEN, TradeStatus.CLOSING):
            return

        exit_price = float(order_data.get("ap") or order_data.get("L") or 0)
        trade.status       = TradeStatus.CLOSING
        trade.exit_price   = exit_price
        trade.exit_fill_ts = datetime.now(timezone.utc).isoformat()
        trade.exit_type    = ExitType.TP.value
        trade.touch()
        await self._db.save_trade(trade)
        await self._emit(EventType.TP_FILL, trade_id, {
            "orderId": order_id, "price": exit_price
        })
        log.info(f"Trade {trade_id[:8]} TP ejecutado a {exit_price}")

        # Cancelar SL que quedó pendiente
        await self._cancel_counterpart(trade, "sl")
        await self._close_trade(trade)

    async def on_sl_fill(self, order_data: dict):
        """Callback WS: fill del SL Algo (STOP_MARKET) colocado en Binance."""
        order_id = int(order_data.get("i", 0))
        trade_id = self._by_sl.pop(order_id, None)
        if not trade_id:
            return
        trade = self._trades.get(trade_id)
        if not trade or trade.status not in (TradeStatus.OPEN, TradeStatus.CLOSING):
            return

        exit_price = float(order_data.get("ap") or order_data.get("L") or 0)
        trade.status       = TradeStatus.CLOSING
        trade.exit_price   = exit_price
        trade.exit_fill_ts = datetime.now(timezone.utc).isoformat()
        trade.exit_type    = ExitType.SL.value
        trade.touch()
        await self._db.save_trade(trade)
        await self._emit(EventType.SL_FILL, trade_id, {
            "orderId": order_id, "price": exit_price
        })
        log.warning(f"Trade {trade_id[:8]} SL ejecutado a {exit_price}")

        # Cancelar TP que quedó pendiente
        await self._cancel_counterpart(trade, "tp")
        await self._close_trade(trade)

    async def _cancel_counterpart(self, trade: Trade, side: str):
        """Cancela la orden TP o SL en Binance (regular o algo)."""
        if side == "tp" and trade.tp_order_id:
            oid = int(trade.tp_order_id)
            try:
                await self._order_mgr.cancel_order(trade.pair, oid)
                log.info(f"Trade {trade.trade_id[:8]} TP cancelado (orderId={oid})")
            except BinanceError as e:
                log.warning(f"No se pudo cancelar TP {oid}: {e}")
            self._by_tp.pop(oid, None)
            self._ws_mgr.unregister(oid)
        elif side == "sl" and trade.sl_order_id:
            oid = int(trade.sl_order_id)
            try:
                await self._order_mgr.cancel_order(trade.pair, oid)
                log.info(f"Trade {trade.trade_id[:8]} SL cancelado (algoId={oid})")
            except BinanceError as e:
                log.warning(f"No se pudo cancelar SL {oid}: {e}")
            self._by_sl.pop(oid, None)
            self._ws_mgr.unregister(oid)

    async def _close_trade(self, trade: Trade):
        """Calcula PnL y marca el trade como CLOSED."""
        if trade.entry_price and trade.exit_price and trade.entry_quantity:
            # Para SHORT: PnL = (entry - exit) * qty
            pnl_pct = ((trade.entry_price - trade.exit_price)
                       / trade.entry_price * 100)
            pnl_usdt = (trade.entry_price - trade.exit_price) * trade.entry_quantity
            # Comisión estimada: 0.04% maker (configurable si se desea)
            fees = (trade.entry_price + trade.exit_price) * trade.entry_quantity * 0.0004
            trade.pnl_pct  = round(pnl_pct,  4)
            trade.pnl_usdt = round(pnl_usdt, 4)
            trade.fees_usdt = round(fees,    4)

        trade.status = TradeStatus.CLOSED
        trade.touch()
        await self._db.save_trade(trade)
        self._trades.pop(trade.trade_id, None)

        pnl_u = trade.pnl_usdt or 0.0
        pnl_p = trade.pnl_pct  or 0.0
        sign  = "+" if pnl_u >= 0 else ""
        log.info(
            f"Trade {trade.trade_id[:8]} CLOSED [{trade.exit_type}] "
            f"{trade.pair} PnL={sign}{pnl_u:.4f} USDT "
            f"({sign}{pnl_p:.2f}%)"
        )

    # ──────────────────────────────────────────────────────────────────
    # Timeout checker
    # ──────────────────────────────────────────────────────────────────

    async def _timeout_loop(self):
        while True:
            await asyncio.sleep(60)   # revisar cada minuto
            try:
                await self._check_timeouts()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Error en timeout_loop: {e}", exc_info=True)

    async def _check_timeouts(self):
        now       = datetime.now(timezone.utc)
        timeout_d = timedelta(hours=self._cfg.timeout_hours)

        for trade in list(self._trades.values()):
            if trade.status != TradeStatus.OPEN:
                continue
            if not trade.entry_fill_ts:
                continue
            try:
                fill_dt = datetime.fromisoformat(trade.entry_fill_ts)
            except ValueError:
                continue
            if fill_dt.tzinfo is None:
                fill_dt = fill_dt.replace(tzinfo=timezone.utc)

            if now - fill_dt < timeout_d:
                continue

            log.info(
                f"Trade {trade.trade_id[:8]} TIMEOUT: "
                f"abierto desde {trade.entry_fill_ts}"
            )
            await self._emit(EventType.TIMEOUT, trade.trade_id, {
                "open_since": trade.entry_fill_ts,
                "hours": (now - fill_dt).total_seconds() / 3600,
            })
            asyncio.create_task(
                self._close_by_timeout(trade),
                name=f"timeout_{trade.trade_id[:8]}"
            )

    async def _close_by_timeout(self, trade: Trade):
        trade.status = TradeStatus.CLOSING
        trade.touch()
        await self._db.save_trade(trade)

        # Cancelar TP y SL
        await self._cancel_counterpart(trade, "tp")
        await self._cancel_counterpart(trade, "sl")

        qty = trade.entry_quantity
        if not qty:
            log.error(f"Trade {trade.trade_id[:8]} sin qty para cerrar timeout")
            return

        order_type = self._cfg.timeout_order_type.upper()

        # Intentar con orden no-market primero (BBO o LIMIT)
        if order_type != "MARKET":
            try:
                if order_type == "BBO":
                    order = await self._order_mgr.close_position_bbo(trade.pair, qty)
                    log.info(
                        f"Trade {trade.trade_id[:8]} cierre timeout BBO "
                        f"orderId={order['orderId']}"
                    )
                else:  # LIMIT
                    ask   = await self._order_mgr.get_best_ask(trade.pair)
                    order = await self._order_mgr.close_position_limit(trade.pair, qty, ask)
                    log.info(
                        f"Trade {trade.trade_id[:8]} cierre timeout limit "
                        f"orderId={order['orderId']} price={ask}"
                    )
                close_oid = int(order["orderId"])
                # Esperar fill
                filled_price = await self._wait_close_fill(
                    close_oid, trade.pair,
                    self._cfg.timeout_chase_seconds
                )
                if filled_price:
                    trade.exit_price   = filled_price
                    trade.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                    trade.exit_type    = ExitType.TIMEOUT.value
                    await self._close_trade(trade)
                    return
                # No fill → cancelar
                try:
                    await self._order_mgr.cancel_order(trade.pair, close_oid)
                except BinanceError:
                    pass
            except Exception as e:
                log.error(f"Error cierre timeout {order_type} {trade.pair}: {e}")

        # MARKET: directo (timeout_order_type="MARKET") o fallback
        if order_type == "MARKET" or self._cfg.timeout_market_fallback:
            try:
                result = await self._order_mgr.close_position_market(trade.pair, qty)
                exit_p = float(result.get("avgPrice") or result.get("price") or 0)
                trade.exit_price   = exit_p
                trade.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                trade.exit_type    = ExitType.TIMEOUT.value
                await self._close_trade(trade)
            except Exception as e:
                log.error(f"Error cierre timeout market {trade.pair}: {e}")
                trade.status = TradeStatus.ERROR
                trade.error_message = f"Timeout cierre fallido: {e}"
                trade.touch()
                await self._db.save_trade(trade)

    async def _wait_close_fill(self, order_id: int, symbol: str,
                                timeout: float) -> Optional[float]:
        """Polling REST para verificar fill de la orden de cierre."""
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < timeout:
            await asyncio.sleep(2)
            try:
                od = await self._order_mgr.get_order(symbol, order_id)
                if od.get("status") == "FILLED":
                    return float(od.get("avgPrice") or od.get("price") or 0)
            except Exception as e:
                log.debug(f"Polling fill {order_id}: {e}")
        return None

    # ──────────────────────────────────────────────────────────────────
    # Helper: emitir evento
    # ──────────────────────────────────────────────────────────────────

    async def _emit(self, etype: EventType, trade_id: Optional[str], details: dict):
        ev = Event(trade_id=trade_id, event_type=etype.value, details=details)
        try:
            await self._db.save_event(ev)
            await self._on_event(ev)
        except Exception as e:
            log.debug(f"Error emitiendo evento {etype}: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _signal_to_dict(sig: Signal) -> dict:
    return {
        "fecha_hora":   sig.fecha_hora,
        "pair":         sig.pair,
        "top":          sig.top,
        "close":        sig.close,
        "mom_1h_pct":   sig.mom_1h_pct,
        "mom_pct":      sig.mom_pct,
        "vol_ratio":    sig.vol_ratio,
        "trades_ratio": sig.trades_ratio,
        "quintil":      sig.quintil,
    }
