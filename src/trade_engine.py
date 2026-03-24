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
from decimal import Decimal
from typing import Callable, Awaitable, Dict, Optional

from .config import Config
from .logger import get_logger
from .models import Event, EventType, Signal, Trade, TradeStatus, ExitType
from .order_manager import BinanceError, OrderManager
from .state import StateDB
from .ws_manager import WSManager

log = get_logger("trade_engine")

OnEventCallback = Callable[[Event], Awaitable[None]]
TRADE_ENGINE_VERSION = "0.11"


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
        # Mapas rápidos para lookup por order_id y client_id
        self._by_entry:      Dict[int, str]   = {}   # entry_order_id → trade_id
        self._by_client_id:  Dict[str, str]   = {}   # newClientOrderId → trade_id
        self._by_tp:         Dict[int, str]   = {}   # tp_order_id    → trade_id
        self._by_sl:         Dict[int, str]   = {}   # sl_order_id    → trade_id

        self._timeout_task:   Optional[asyncio.Task] = None
        self._reconcile_task: Optional[asyncio.Task] = None
        self._open_tasks:     set = set()   # tareas _open_trade en curso
        self._ignore_cycles:  Dict[str, dict] = {}

    # ──────────────────────────────────────────────────────────────────
    # Arranque / Parada
    # ──────────────────────────────────────────────────────────────────

    async def start(self):
        self._timeout_task = asyncio.create_task(
            self._timeout_loop(), name="timeout_checker"
        )
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="reconcile_checker"
        )
        log.info(f"TradeEngine v{TRADE_ENGINE_VERSION} iniciado")

    async def stop(self):
        if self._timeout_task:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass

        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
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

    @staticmethod
    def _parse_iso_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _get_last_startup_dt(self) -> datetime | None:
        events = await self._db.get_last_events(200)
        for ev in events:
            if ev.event_type != EventType.STARTUP.value:
                continue
            dt = self._parse_iso_dt(ev.timestamp)
            if dt:
                return dt
        return None

    async def _infer_external_close_from_binance(self, trade: Trade) -> Optional[dict]:
        if not trade.pair or not trade.entry_price:
            return None

        if trade.tp_order_id:
            try:
                tp_order = await self._order_mgr.get_order(trade.pair, int(trade.tp_order_id))
                if tp_order.get("status") == "FILLED":
                    exit_price = float(tp_order.get("avgPrice") or tp_order.get("price") or 0.0)
                    update_time_ms = int(tp_order.get("updateTime") or tp_order.get("time") or 0)
                    return {
                        "exit_type": ExitType.TP.value,
                        "exit_price": exit_price,
                        "exit_fill_ts": datetime.fromtimestamp(
                            (update_time_ms or int(datetime.now(timezone.utc).timestamp() * 1000)) / 1000.0,
                            tz=timezone.utc,
                        ).isoformat(),
                        "source": "tp_order_history",
                        "matched_order_id": int(trade.tp_order_id),
                    }
            except BinanceError as e:
                log.debug(
                    f"Reconciliación: TP {trade.tp_order_id} de {trade.trade_id[:8]} "
                    f"no consultable por REST: {e}"
                )
            except Exception as e:
                log.debug(
                    f"Reconciliación: error leyendo TP {trade.tp_order_id} de "
                    f"{trade.trade_id[:8]}: {e}"
                )

        startup_dt = await self._get_last_startup_dt()
        candidate_dt = max(
            dt for dt in (
                startup_dt,
                self._parse_iso_dt(trade.entry_fill_ts),
                self._parse_iso_dt(trade.updated_at),
                self._parse_iso_dt(trade.created_at),
            )
            if dt is not None
        )
        start_dt = candidate_dt - timedelta(minutes=5)

        try:
            raw_trades = await self._order_mgr.get_user_trades(
                trade.pair,
                start_time_ms=int(start_dt.timestamp() * 1000),
                limit=200,
            )
        except Exception as e:
            log.warning(
                f"Reconciliación: no se pudieron leer ejecuciones recientes de Binance "
                f"para {trade.pair}: {e}"
            )
            return None

        close_buckets = self._build_recent_close_buckets(raw_trades)
        if not close_buckets:
            return None

        best = self._pick_recent_close_bucket(trade, close_buckets)
        if not best:
            return None

        return {
            "exit_type": best["exit_type"],
            "exit_price": best["avg_price"],
            "exit_fill_ts": datetime.fromtimestamp(
                best["last_time_ms"] / 1000.0,
                tz=timezone.utc,
            ).isoformat(),
            "source": "user_trades_recent",
            "matched_order_id": best["order_id"],
            "matched_qty": round(best["qty"], 8),
        }

    def _build_recent_close_buckets(self, user_trades: list[dict]) -> list[dict]:
        buckets: dict[int, dict] = {}
        for item in user_trades:
            side = self._extract_user_trade_side(item)
            if side != "BUY":
                continue

            order_id = int(item.get("orderId") or item.get("orderID") or 0)
            price = float(item.get("price") or 0.0)
            qty = float(item.get("qty") or item.get("executedQty") or 0.0)
            time_ms = int(item.get("time") or item.get("T") or 0)
            if order_id <= 0 or price <= 0 or qty <= 0 or time_ms <= 0:
                continue

            bucket = buckets.setdefault(order_id, {
                "order_id": order_id,
                "qty": 0.0,
                "notional": 0.0,
                "first_time_ms": time_ms,
                "last_time_ms": time_ms,
                "fills": 0,
            })
            bucket["qty"] += qty
            bucket["notional"] += price * qty
            bucket["first_time_ms"] = min(bucket["first_time_ms"], time_ms)
            bucket["last_time_ms"] = max(bucket["last_time_ms"], time_ms)
            bucket["fills"] += 1

        result: list[dict] = []
        for bucket in buckets.values():
            qty = bucket["qty"]
            if qty <= 0:
                continue
            bucket["avg_price"] = bucket["notional"] / qty
            result.append(bucket)
        result.sort(key=lambda item: item["last_time_ms"], reverse=True)
        return result

    @staticmethod
    def _extract_user_trade_side(item: dict) -> str:
        side = str(item.get("side") or "").upper()
        if side in {"BUY", "SELL"}:
            return side

        buyer = item.get("buyer")
        if isinstance(buyer, bool):
            return "BUY" if buyer else "SELL"
        if isinstance(buyer, str):
            value = buyer.strip().lower()
            if value in {"true", "1"}:
                return "BUY"
            if value in {"false", "0"}:
                return "SELL"
        return ""

    def _pick_recent_close_bucket(self, trade: Trade, buckets: list[dict]) -> Optional[dict]:
        entry_price = float(trade.entry_price or 0.0)
        entry_qty = float(trade.entry_quantity or 0.0)
        tp_ref = float(trade.tp_price or trade.tp_trigger_price or 0.0)
        sl_ref = float(trade.sl_price or trade.sl_trigger_price or 0.0)
        best: Optional[dict] = None

        for bucket in buckets:
            avg_price = float(bucket["avg_price"])
            qty = float(bucket["qty"])
            qty_diff = (abs(qty - entry_qty) / entry_qty) if entry_qty > 0 else 1.0
            dist_tp = abs(avg_price - tp_ref) / tp_ref if tp_ref > 0 else 999.0
            dist_sl = abs(avg_price - sl_ref) / sl_ref if sl_ref > 0 else 999.0

            exit_type = ExitType.TP.value if dist_tp <= dist_sl else ExitType.SL.value
            close_dist = min(dist_tp, dist_sl)
            score = close_dist + min(qty_diff, 1.0) * 0.35

            if exit_type == ExitType.TP.value and avg_price > entry_price:
                score += 0.5
            if exit_type == ExitType.SL.value and avg_price < entry_price:
                score += 0.5

            candidate = dict(bucket)
            candidate["exit_type"] = exit_type
            candidate["qty_diff"] = qty_diff
            candidate["close_dist"] = close_dist
            candidate["score"] = score

            if best is None or candidate["score"] < best["score"]:
                best = candidate

        if not best:
            return None

        if best["qty_diff"] > 0.40 and best["close_dist"] > 0.03:
            return None

        if best["close_dist"] > 0.12:
            return None

        return best

    async def _apply_reconciled_close(self, trade: Trade, reason_msg: str) -> None:
        resolved = await self._infer_external_close_from_binance(trade)
        if resolved:
            trade.status = TradeStatus.CLOSING
            trade.exit_price = resolved["exit_price"]
            trade.exit_fill_ts = resolved["exit_fill_ts"]
            trade.exit_type = resolved["exit_type"]
            trade.touch()
            await self._db.save_trade(trade)

            event_type = EventType.TP_FILL if trade.exit_type == ExitType.TP.value else EventType.SL_FILL
            await self._emit(event_type, trade.trade_id, {
                "price": trade.exit_price,
                "orderId": resolved.get("matched_order_id"),
                "reconcile": True,
                "source": resolved.get("source"),
            })
            log.info(
                f"Reconciliación: trade {trade.trade_id[:8]} ({trade.pair}) "
                f"cerrado externamente como {trade.exit_type.upper()} "
                f"@ {trade.exit_price}"
            )
            await self._close_trade(trade)
            return

        trade.status = TradeStatus.CLOSED
        trade.exit_type = ExitType.MANUAL.value
        trade.exit_fill_ts = trade.exit_fill_ts or datetime.now(timezone.utc).isoformat()
        trade.touch()
        await self._db.save_trade(trade)
        self._trades.pop(trade.trade_id, None)
        await self._emit(EventType.ERROR, trade.trade_id, {
            "msg": reason_msg
        })

    # ──────────────────────────────────────────────────────────────────
    # Reconciliación
    # ──────────────────────────────────────────────────────────────────

    async def _reconcile_loop(self):
        """Ejecuta una reconciliación periódica cada 10 minutos (600s)."""
        while True:
            await asyncio.sleep(600)

            # Validación: No interrumpir si hay tareas de entrada (chase loop)
            if self._open_tasks:
                continue

            try:
                log.info("Iniciando reconciliación periódica (10 min)...")
                await self.reconcile(self.get_active_trades())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Error en reconcile_loop: {e}", exc_info=True)

    async def reconcile(self, db_trades: list[Trade]):
        """
        Reconciliación general de estado y limpieza de huérfanos.
        """
        log.info(f"Reconciliando {len(db_trades)} trades activos de la DB...")

        # 1. Obtener todas las posiciones Binance de una sola llamada
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

        db_open_pairs:    set[str] = set()
        db_opening_pairs: set[str] = set()

        # 2. Reconciliar estado de los trades activos en memoria
        for t in db_trades:
            self._trades[t.trade_id] = t
            try:
                if t.status == TradeStatus.OPEN:
                    await self._reconcile_open(t, binance_pairs)
                    if t.status == TradeStatus.OPEN:
                        db_open_pairs.add(t.pair)
                elif t.status in (TradeStatus.OPENING, TradeStatus.SIGNAL_RECEIVED):
                    db_opening_pairs.add(t.pair)
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

        for pair in binance_pairs - db_open_pairs:
            log.warning(
                f"Reconciliación: posición abierta en Binance para {pair} "
                "sin trade correspondiente en DB → revisar manualmente"
            )

        # 3. Limpieza global de órdenes huérfanas en Binance
        try:
            all_open = await self._order_mgr.get_all_open_orders()
            all_algo = await self._order_mgr.get_all_open_algo_orders()
            all_orders = all_open + all_algo

            # Un par es válido si tiene posición abierta O si el bot está
            # intentando entrar ahora mismo (chase loop activo)
            valid_pairs = binance_pairs.union(db_opening_pairs)

            cleaned_count = 0
            for o in all_orders:
                sym = o.get("symbol")
                oid = int(o.get("orderId"))

                # Si la orden pertenece a un par sin posición abierta → huérfana
                if sym not in valid_pairs:
                    log.warning(
                        f"Reconciliación: eliminando orden huérfana {oid} "
                        f"en {sym} (posición cero)"
                    )
                    try:
                        await self._order_mgr.cancel_order(sym, oid)
                        cleaned_count += 1
                    except Exception as e:
                        log.error(
                            f"Fallo al cancelar orden huérfana {oid} en {sym}: {e}"
                        )

            if cleaned_count > 0:
                log.info(
                    f"Reconciliación: {cleaned_count} órdenes huérfanas "
                    "purgadas de Binance."
                )
        except Exception as e:
            log.error(
                f"Error durante la purga de órdenes huérfanas: {e}",
                exc_info=True
            )

    async def _reconcile_open(self, t: Trade, binance_pairs: set):
        """Trade OPEN: verifica posición y comprueba/re-coloca TP y SL."""
        if t.pair not in binance_pairs:
            log.warning(
                f"Reconciliación: trade {t.trade_id[:8]} ({t.pair}) "
                "OPEN en DB pero sin posición en Binance "
                "→ Cancelando TP/SL huérfanos y resolviendo el motivo de cierre"
            )

            # --- LIMPIEZA ACTIVA DE ÓRDENES CONDICIONALES ---
            await self._cancel_counterpart(t, "tp")
            await self._cancel_counterpart(t, "sl")
            # ------------------------------------------------
            await self._apply_reconciled_close(
                t,
                "Reconciliación: posición cerrada externamente",
            )
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
            self._reset_ignore_cycle(t.pair)
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
            if not t.exit_type or t.exit_type == ExitType.MANUAL.value or not t.exit_price:
                resolved = await self._infer_external_close_from_binance(t)
                if resolved:
                    t.exit_price = resolved["exit_price"]
                    t.exit_fill_ts = resolved["exit_fill_ts"]
                    t.exit_type = resolved["exit_type"]
                    await self._emit(
                        EventType.TP_FILL if t.exit_type == ExitType.TP.value else EventType.SL_FILL,
                        t.trade_id,
                        {
                            "price": t.exit_price,
                            "orderId": resolved.get("matched_order_id"),
                            "reconcile": True,
                            "source": resolved.get("source"),
                        },
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
        if self._cfg.signal_filter_overlap and self.open_count_pair(sig.pair) > 0:
            log.info(
                f"Señal {sig.pair} descartada: filtro_overlap activo "
                f"(hay operativa viva en el par)"
            )
            return

        ignore_reason = self._register_ignore_cycle(sig)
        if ignore_reason:
            log.info(f"Señal {sig.pair} descartada: {ignore_reason}")
            return

        # --- Control de Cuarentena ---
        if self._cfg.quarantine_hours > 0:
            last_closed = await self._db.get_last_closed_time(sig.pair)
            if last_closed:
                now_utc = datetime.now(timezone.utc)
                if last_closed.tzinfo is None:
                    last_closed = last_closed.replace(tzinfo=timezone.utc)
                hours_since = (now_utc - last_closed).total_seconds() / 3600.0
                if hours_since < self._cfg.quarantine_hours:
                    log.info(
                        f"Señal {sig.pair} descartada: CUARENTENA "
                        f"(último cierre hace {hours_since:.2f}h, requiere {self._cfg.quarantine_hours}h)"
                    )
                    return

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
            "pair": sig.pair,
            "top": sig.top,
            "rank": sig.rank,
            "mom_1h_pct": sig.mom_1h_pct,
            "mom_pct": sig.mom_pct,
            "bp": sig.bp,
            "close": sig.close,
        })
        log.info(f"Trade {trade.trade_id[:8]} SIGNAL_RECEIVED {sig.pair}")

        # Lanzar apertura en background para no bloquear el watcher
        task = asyncio.create_task(
            self._open_trade(trade, sig),
            name=f"open_{trade.trade_id[:8]}"
        )
        self._open_tasks.add(task)
        task.add_done_callback(self._open_tasks.discard)

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
            state = {
                "ignored_count": 0,
                "window_started_at": signal_dt,
            }

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
            log.debug(f"Ciclo ignore_n reseteado para {pair}")

    # ──────────────────────────────────────────────────────────────────
    # Apertura (chase loop)
    # ──────────────────────────────────────────────────────────────────

    async def _open_trade(self, trade: Trade, sig: Signal):
        trade.status = TradeStatus.OPENING
        trade.touch()
        await self._db.save_trade(trade)

        cfg = self._cfg
        _cur_client_oid = None   # rastrea el client_oid activo para limpieza en CancelledError
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

                    # Generar ID de cliente propio para neutralizar la latencia REST
                    client_oid = f"ent_{trade.trade_id[:8]}_{attempt}"
                    _cur_client_oid = client_oid

                    # 1. REGISTRO PREVIO AL ENVÍO DE RED
                    self._by_client_id[client_oid] = trade.trade_id
                    self._ws_mgr.register_entry(client_oid)

                    result   = await self._order_mgr.open_short(
                        sig.pair, qty, price_match=price_match,
                        newClientOrderId=client_oid
                    )
                    order_id = int(result["orderId"])

                    # 2. Respaldo numérico tradicional
                    self._by_entry[order_id] = trade.trade_id
                    self._ws_mgr.register_entry(order_id)

                    # 3. ACTUALIZACIÓN Y PERSISTENCIA (I/O)
                    trade.entry_order_id = order_id
                    trade.entry_quantity = qty
                    trade.touch()
                    await self._db.save_trade(trade)
                    await self._emit(EventType.ENTRY_SENT, trade.trade_id, {
                        "orderId": order_id, "priceMatch": price_match,
                        "qty": qty, "attempt": attempt,
                    })
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
                        if e.code == -2011:
                            recovered = await self._recover_entry_after_unknown_cancel(
                                trade=trade,
                                pair=sig.pair,
                                order_id=order_id,
                                client_oid=client_oid,
                            )
                            if recovered:
                                return
                        # Si ya se llenó en la cancelación, lo captura el WS
                        log.warning(f"Cancel order {order_id}: {e}")
                    self._ws_mgr.unregister(order_id)
                    self._ws_mgr.unregister_client_id(client_oid)
                    self._by_entry.pop(order_id, None)
                    self._by_client_id.pop(client_oid, None)

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
                    ref_price  = await self._order_mgr.get_best_bid(sig.pair)
                    qty        = await self._order_mgr.calc_quantity(sig.pair, ref_price)
                    client_oid = f"mkt_{trade.trade_id[:8]}"
                    _cur_client_oid = client_oid

                    # Registro previo al envío de red
                    self._by_client_id[client_oid] = trade.trade_id
                    self._ws_mgr.register_entry(client_oid)

                    result    = await self._order_mgr.open_short_market(
                        sig.pair, qty, newClientOrderId=client_oid
                    )
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
                    self._ws_mgr.unregister_client_id(client_oid)
                    self._by_entry.pop(order_id, None)
                    self._by_client_id.pop(client_oid, None)
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
                if _cur_client_oid:
                    self._ws_mgr.unregister_client_id(_cur_client_oid)
                    self._by_client_id.pop(_cur_client_oid, None)
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

    async def _recover_entry_after_unknown_cancel(self,
                                                  trade: Trade,
                                                  pair: str,
                                                  order_id: int,
                                                  client_oid: str) -> bool:
        """
        Si cancel_order devuelve -2011, consulta el estado real de la orden
        antes de lanzar un nuevo intento. Esto evita duplicar entradas cuando
        Binance ya ejecutó la orden pero el fill no llegó al bot a tiempo.
        """
        try:
            order = await self._order_mgr.get_order(pair, order_id)
        except Exception as e:
            log.warning(
                f"Trade {trade.trade_id[:8]}: no se pudo recuperar orderId={order_id} "
                f"tras cancel -2011: {e}"
            )
            return False

        status = str(order.get("status") or "").upper()
        executed_qty = float(order.get("executedQty") or order.get("z") or 0)
        avg_price = float(
            order.get("avgPrice") or
            order.get("ap") or
            order.get("price") or
            order.get("L") or 0
        )

        log.warning(
            f"Trade {trade.trade_id[:8]}: cancel -2011 para orderId={order_id}; "
            f"Binance reporta status={status} executedQty={executed_qty}"
        )

        if executed_qty <= 0:
            return False

        trade.entry_quantity = executed_qty
        trade.entry_order_id = order_id
        trade.touch()
        await self._db.save_trade(trade)

        synthetic_fill = {
            "i": order_id,
            "c": client_oid,
            "ap": str(avg_price),
            "L": str(avg_price),
        }
        await self.on_entry_fill(synthetic_fill)
        return True

    # ──────────────────────────────────────────────────────────────────
    # Callbacks del WebSocket
    # ──────────────────────────────────────────────────────────────────

    async def on_entry_fill(self, order_data: dict):
        order_id  = int(order_data.get("i", 0))
        client_id = order_data.get("c", "")

        # Verificar primero por nuestro ID garantizado, luego por numérico
        trade_id = self._by_client_id.pop(client_id, None)
        if not trade_id:
            trade_id = self._by_entry.pop(order_id, None)

        if not trade_id:
            log.warning(
                f"on_entry_fill: trade no encontrado para "
                f"orderId={order_id} clientId={client_id}"
            )
            return

        # Limpiar residuos en ambos diccionarios
        self._by_entry.pop(order_id, None)
        self._by_client_id.pop(client_id, None)

        trade = self._trades.get(trade_id)
        if not trade:
            return

        entry_price = float(order_data.get("ap") or order_data.get("L") or 0)
        fill_ts     = datetime.now(timezone.utc).isoformat()

        trade.entry_price   = entry_price
        trade.entry_fill_ts = fill_ts
        trade.status        = TradeStatus.OPEN
        trade.touch()
        self._reset_ignore_cycle(trade.pair)
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
        except BinanceError as e:
            if e.code in (-2021, -2010):
                # El precio ya cruzó el TP mientras el bot estaba apagado/desincronizado.
                # -2021: precio cruzado en algos; -2010: LIMIT GTX rechazado porque ya cruzó.
                # Como es TP (ganancia), y Binance opera en neto, el mercado ya cerró esta porción de liquidez.
                log.warning(
                    f"Trade {trade.trade_id[:8]} {trade.pair}: TP superado (-2021) "
                    f"durante reconciliación/apagado → Marcando trade como cerrado (TP)."
                )
                trade.status       = TradeStatus.CLOSING
                # Asumimos el precio de trigger como precio de salida estimado para PnL local
                trade.exit_price   = trade.tp_trigger_price if trade.tp_trigger_price else (trade.entry_price * (1 - self._cfg.tp_pct / 100))
                trade.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                trade.exit_type    = ExitType.TP.value

                # Intentamos cerrar la porción correspondiente a mercado por seguridad,
                # aunque si el TP real se ejecutó en Binance, la posición neta ya se redujo.
                try:
                    await self._order_mgr.close_position_market(trade.pair, trade.entry_quantity)
                except Exception as close_err:
                    log.debug(f"Trade {trade.trade_id[:8]} aviso cierre MARKET post-TP: {close_err}")

                await self._cancel_counterpart(trade, "sl")
                await self._close_trade(trade)

                # --- Cierre en cascada (-2021 TP) ---
                if self._cfg.tp_posicion:
                    asyncio.create_task(
                        self._close_sibling_trades(trade.pair, "TP", trade.trade_id),
                        name=f"cascade_tp21_{trade.trade_id[:8]}"
                    )
            else:
                log.error(f"Error colocando TP {trade.pair}: {e}", exc_info=True)
                await self._emit(EventType.ERROR, trade.trade_id, {"msg": f"TP error: {e}"})
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

                    # --- Cierre en cascada (-2021 SL) ---
                    if self._cfg.sl_posicion:
                        asyncio.create_task(
                            self._close_sibling_trades(trade.pair, "SL", trade.trade_id),
                            name=f"cascade_sl21_{trade.trade_id[:8]}"
                        )
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

        # --- Cierre en cascada ---
        if self._cfg.tp_posicion:
            asyncio.create_task(
                self._close_sibling_trades(trade.pair, "TP", trade.trade_id),
                name=f"cascade_tp_{trade.trade_id[:8]}"
            )

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

        # --- Cierre en cascada ---
        if self._cfg.sl_posicion:
            asyncio.create_task(
                self._close_sibling_trades(trade.pair, "SL", trade.trade_id),
                name=f"cascade_sl_{trade.trade_id[:8]}"
            )

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
    # Cierre en cascada posicional
    # ──────────────────────────────────────────────────────────────────

    def _cascade_trade_sigue_activo(self, trade: Trade) -> bool:
        actual = self._trades.get(trade.trade_id)
        return actual is trade and actual.status == TradeStatus.CLOSING

    async def _cerrar_cascade_si_posicion_ya_no_existe(self,
                                                        trade: Trade,
                                                        etapa: str) -> bool:
        """Sincroniza cierres en cascada que llegan tarde respecto a Binance."""
        if not self._cascade_trade_sigue_activo(trade):
            log.info(
                f"Trade {trade.trade_id[:8]} cascada abortada en {etapa}: "
                "el trade ya no sigue activo en CLOSING"
            )
            return True

        try:
            position = await self._order_mgr.get_position(trade.pair)
        except Exception as e:
            log.warning(
                f"Trade {trade.trade_id[:8]} no pudo verificar posición "
                f"real en {etapa}: {e}"
            )
            return False

        if position is not None:
            return False

        log.info(
            f"Trade {trade.trade_id[:8]} cascada detecta posición plana "
            f"en Binance durante {etapa} -> CLOSED"
        )
        if not trade.exit_price:
            trade.exit_price = 0.0
        trade.exit_fill_ts = trade.exit_fill_ts or datetime.now(timezone.utc).isoformat()
        trade.exit_type = trade.exit_type or ExitType.MANUAL.value
        await self._close_trade(trade)
        return True

    async def _resolver_reduce_only_rechazado_en_cascada(self,
                                                          trade: Trade,
                                                          etapa: str) -> bool:
        """Evita marcar ERROR si Binance ya no permite otra orden reduceOnly."""
        if not self._cascade_trade_sigue_activo(trade):
            log.info(
                f"Trade {trade.trade_id[:8]} ignora -2022 en {etapa}: "
                "el trade ya no seguía activo en CLOSING"
            )
            return True

        if await self._cerrar_cascade_si_posicion_ya_no_existe(
            trade, f"{etapa} tras -2022"
        ):
            return True

        try:
            open_orders = await self._order_mgr.get_open_orders(trade.pair)
            hay_reduce_only = any(
                str(o.get("side", "")).upper() == "BUY"
                and str(o.get("reduceOnly", "")).lower() == "true"
                for o in open_orders
            )
        except Exception as e:
            hay_reduce_only = False
            log.warning(
                f"Trade {trade.trade_id[:8]} no pudo inspeccionar órdenes "
                f"abiertas tras -2022 en {etapa}: {e}"
            )

        if hay_reduce_only:
            detalle = "Hay otra orden reduceOnly viva para el par."
        else:
            detalle = "No se detectó otra orden reduceOnly viva."

        log.warning(
            f"Trade {trade.trade_id[:8]} cascada {etapa}: Binance rechazó "
            f"reduceOnly (-2022) pero la posición del par sigue abierta. "
            f"{detalle} Se mantiene en CLOSING para que la reconciliación "
            "resuelva el estado real."
        )
        trade.touch()
        await self._db.save_trade(trade)
        return True

    async def _close_sibling_trades(self, pair: str, trigger_type: str, exclude_trade_id: str):
        """
        Cierra trades hermanos en cascada con un modelo Maker Progresivo.
        Evalúa el PnL combinado antes de disparar la cascada TP para respetar Min_TP_posicion.
        """
        # 1. Identificar candidatos SIN bloquearlos aún
        candidates = [
            t for t in self._trades.values()
            if t.pair == pair and t.status == TradeStatus.OPEN and t.trade_id != exclude_trade_id
        ]

        if not candidates:
            return

        # 2. Validación de rentabilidad combinada (Solo para TP)
        if trigger_type == "TP":
            min_tp_pct = self._cfg.min_tp_posicion_pct
            if min_tp_pct > 0:
                try:
                    bid = await self._order_mgr.get_best_bid(pair)
                    ask = await self._order_mgr.get_best_ask(pair)
                    mid_price = (bid + ask) / 2.0

                    total_cost = sum(t.entry_price * t.entry_quantity for t in candidates)
                    total_value = sum(mid_price * t.entry_quantity for t in candidates)

                    if total_cost > 0:
                        combined_pnl_pct = ((total_cost - total_value) / total_cost) * 100.0
                        if combined_pnl_pct < min_tp_pct:
                            log.info(f"Cascada TP abortada para {pair}: PnL combinado hermanos ({combined_pnl_pct:.2f}%) < Min_TP_posicion ({min_tp_pct:.2f}%)")
                            return
                except Exception as e:
                    log.error(f"Error evaluando PnL combinado para {pair}: {e}")
                    return

        # 3. BLOQUEO SÍNCRONO: Previene carrera de doble ejecución
        siblings = []
        for t in candidates:
            if t.status == TradeStatus.OPEN:
                t.status = TradeStatus.CLOSING
                t.touch()
                siblings.append(t)

        if not siblings:
            return

        log.info(f"Cierre en cascada ({trigger_type}_posicion=True) para {len(siblings)} trades de {pair}")

        for t in siblings:
            await self._db.save_trade(t)

        async def process_one_sibling(t: Trade):
            await self._cancel_counterpart(t, "tp")
            await self._cancel_counterpart(t, "sl")

            try:
                if await self._cerrar_cascade_si_posicion_ya_no_existe(
                    t, "inicio de cascada"
                ):
                    return
                if not self._cascade_trade_sigue_activo(t):
                    log.info(
                        f"Trade {t.trade_id[:8]} cascada abortada: "
                        "el trade ya fue resuelto antes de iniciar"
                    )
                    return

                if trigger_type == "TP":
                    bid = await self._order_mgr.get_best_bid(pair)
                    ask = await self._order_mgr.get_best_ask(pair)
                    mid_price = (bid + ask) / 2.0

                    is_winning = mid_price < t.entry_price

                    if is_winning:
                        info = await self._order_mgr.get_exchange_info(pair)
                        tick_size = info["tick_size"]
                        tick_dec = Decimal(str(tick_size))

                        # --- INTENTO 1: LIMIT (Mid Price) ---
                        mid_price_rounded = float(Decimal(str(mid_price)).quantize(tick_dec))
                        log.info(f"Trade {t.trade_id[:8]} cascada TP (GANANDO). Intento 1 (LIMIT) en {mid_price_rounded}")

                        limit_1 = await self._order_mgr.close_position_limit(t.pair, t.entry_quantity, mid_price_rounded)
                        close_oid_1 = int(limit_1["orderId"])

                        filled_p1 = await self._wait_close_fill(close_oid_1, t.pair, 60.0)

                        if filled_p1:
                            t.exit_price = filled_p1
                            t.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                            t.exit_type = "TP_CASCADE_LIMIT_1"
                            await self._close_trade(t)
                            return

                        # --- Expiración Intento 1 ---
                        if await self._cerrar_cascade_si_posicion_ya_no_existe(
                            t, "tras esperar el intento 1"
                        ):
                            return
                        if not self._cascade_trade_sigue_activo(t):
                            log.info(
                                f"Trade {t.trade_id[:8]} cascada abortada tras "
                                "el intento 1: ya no seguía activo"
                            )
                            return

                        log.info(f"Trade {t.trade_id[:8]} Intento 1 expirado. Evaluando Intento 2.")
                        od_1 = await self._order_mgr.get_order(t.pair, close_oid_1)
                        status_1 = od_1.get("status", "")
                        exec_qty_1 = float(od_1.get("executedQty", 0))
                        avg_p1 = float(od_1.get("avgPrice") or od_1.get("price") or 0)

                        if status_1 in ("NEW", "PARTIALLY_FILLED"):
                            try:
                                await self._order_mgr.cancel_order(t.pair, close_oid_1)
                            except Exception as ce:
                                log.warning(f"Trade {t.trade_id[:8]} ignorando fallo al cancelar L1 (-2011 carrera): {ce}")

                        rem_qty_1 = t.entry_quantity - exec_qty_1

                        # Si se llenó por completo silenciosamente
                        if rem_qty_1 <= 0 or status_1 == "FILLED":
                            t.exit_price = avg_p1
                            t.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                            t.exit_type = "TP_CASCADE_LIMIT_1_LATE"
                            await self._close_trade(t)
                            return

                        if await self._cerrar_cascade_si_posicion_ya_no_existe(
                            t, "antes del intento 2"
                        ):
                            return
                        if not self._cascade_trade_sigue_activo(t):
                            log.info(
                                f"Trade {t.trade_id[:8]} cascada abortada antes "
                                "del intento 2: ya no seguía activo"
                            )
                            return

                        # --- INTENTO 2: LIMIT (Maker Agresivo) ---
                        new_bid = await self._order_mgr.get_best_bid(pair)
                        new_ask = await self._order_mgr.get_best_ask(pair)
                        new_mid = (new_bid + new_ask) / 2.0

                        aggro_maker_price = (new_mid + new_ask) / 2.0
                        aggro_rounded = float(Decimal(str(aggro_maker_price)).quantize(tick_dec))

                        log.info(f"Trade {t.trade_id[:8]} cascada TP. Intento 2 (LIMIT Agresivo) en {aggro_rounded} por qty {rem_qty_1}")

                        limit_2 = await self._order_mgr.close_position_limit(t.pair, rem_qty_1, aggro_rounded)
                        close_oid_2 = int(limit_2["orderId"])

                        filled_p2 = await self._wait_close_fill(close_oid_2, t.pair, 60.0)

                        if filled_p2:
                            if exec_qty_1 > 0:
                                t.exit_price = ((avg_p1 * exec_qty_1) + (filled_p2 * rem_qty_1)) / t.entry_quantity
                                t.exit_type = "TP_CASCADE_MIXED_LIMITS"
                            else:
                                t.exit_price = filled_p2
                                t.exit_type = "TP_CASCADE_LIMIT_2"

                            t.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                            await self._close_trade(t)
                            return

                        # --- Expiración Intento 2 ---
                        if await self._cerrar_cascade_si_posicion_ya_no_existe(
                            t, "tras esperar el intento 2"
                        ):
                            return
                        if not self._cascade_trade_sigue_activo(t):
                            log.info(
                                f"Trade {t.trade_id[:8]} cascada abortada tras "
                                "el intento 2: ya no seguía activo"
                            )
                            return

                        log.info(f"Trade {t.trade_id[:8]} Intento 2 expirado. Purgando remanente a MARKET.")
                        od_2 = await self._order_mgr.get_order(t.pair, close_oid_2)
                        status_2 = od_2.get("status", "")
                        exec_qty_2 = float(od_2.get("executedQty", 0))
                        avg_p2 = float(od_2.get("avgPrice") or od_2.get("price") or 0)

                        if status_2 in ("NEW", "PARTIALLY_FILLED"):
                            try:
                                await self._order_mgr.cancel_order(t.pair, close_oid_2)
                            except Exception as ce:
                                log.warning(f"Trade {t.trade_id[:8]} ignorando fallo al cancelar L2 (-2011 carrera): {ce}")

                        rem_qty_final = rem_qty_1 - exec_qty_2

                        if rem_qty_final > 0:
                            # --- INTENTO 3: MARKET (Liquidación Final) ---
                            mkt_res = await self._order_mgr.close_position_market(t.pair, rem_qty_final)
                            mkt_p = float(mkt_res.get("avgPrice") or mkt_res.get("price") or 0)

                            total_val = (exec_qty_1 * avg_p1) + (exec_qty_2 * avg_p2) + (rem_qty_final * mkt_p)
                            t.exit_price = total_val / t.entry_quantity
                            t.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                            t.exit_type = "TP_CASCADE_MARKET_FALLBACK"
                            await self._close_trade(t)
                            return
                        else:
                            total_val = (exec_qty_1 * avg_p1) + (exec_qty_2 * avg_p2)
                            t.exit_price = total_val / t.entry_quantity
                            t.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                            t.exit_type = "TP_CASCADE_MIXED_LIMITS"
                            await self._close_trade(t)
                            return
                    else:
                        log.info(f"Trade {t.trade_id[:8]} en cascada TP (PERDIENDO). Liquidación agresiva (MARKET)")

                if await self._cerrar_cascade_si_posicion_ya_no_existe(
                    t, "antes del fallback MARKET"
                ):
                    return
                if not self._cascade_trade_sigue_activo(t):
                    log.info(
                        f"Trade {t.trade_id[:8]} cascada abortada antes del "
                        "fallback MARKET: ya no seguía activo"
                    )
                    return

                # Fallback Estructural / Perdedores Directos
                result = await self._order_mgr.close_position_market(t.pair, t.entry_quantity)
                exit_price = float(result.get("avgPrice") or result.get("price") or 0)

                t.exit_price = exit_price
                t.exit_fill_ts = datetime.now(timezone.utc).isoformat()
                t.exit_type = f"{trigger_type}_CASCADE_MKT"
                await self._close_trade(t)

            except BinanceError as e:
                if e.code == -2022:
                    handled = await self._resolver_reduce_only_rechazado_en_cascada(
                        t, "cierre en cascada"
                    )
                    if handled:
                        return
                log.error(f"Error cerrando en cascada trade {t.trade_id[:8]}: {e}", exc_info=True)
                t.status = TradeStatus.ERROR
                t.error_message = f"Cascade close error: {e}"
                t.touch()
                await self._db.save_trade(t)
            except Exception as e:
                log.error(f"Error cerrando en cascada trade {t.trade_id[:8]}: {e}", exc_info=True)
                t.status = TradeStatus.ERROR
                t.error_message = f"Cascade close error: {e}"
                t.touch()
                await self._db.save_trade(t)

        # Desarmado escalonado: lanza tareas paralelas separadas por 5 segundos
        for t in siblings:
            asyncio.create_task(process_one_sibling(t), name=f"cascade_{t.trade_id[:8]}")
            await asyncio.sleep(5.0)

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
        "rank":         sig.rank,
        "close":        sig.close,
        "mom_1h_pct":   sig.mom_1h_pct,
        "mom_pct":      sig.mom_pct,
        "vol_ratio":    sig.vol_ratio,
        "trades_ratio": sig.trades_ratio,
        "quintil":      sig.quintil,
        "bp":           sig.bp,
        "categoria":    sig.categoria,
    }
