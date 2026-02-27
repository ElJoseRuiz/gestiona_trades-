"""
order_manager.py — Interfaz con Binance Futures REST API.

Órdenes de cierre para SHORT (TP / SL):

  TP → TAKE_PROFIT CONDITIONAL (algo) con priceMatch (BBO) y workingType=MARK_PRICE.
       Cuando el mark price baja al stopPrice, Binance ejecuta al mejor ask (BBO).
       La orden vive en Binance aunque el proceso se reinicie.
       triggerPrice = entry * (1 - tp_pct/100)

  SL → STOP_MARKET CONDITIONAL (algo) con workingType=MARK_PRICE.
       Cuando el mark price sube al stopPrice, Binance ejecuta MARKET.
       La orden vive en Binance aunque el proceso se reinicie.
       triggerPrice = entry * (1 + sl_pct/100)

Ambas órdenes se colocan vía /fapi/v1/algoOrder con algoType="CONDITIONAL"
(requerido para cuentas migradas al servicio Algo de Binance).
cancel_order() intenta primero /fapi/v1/order y, si recibe -2011 (orden
no encontrada), reintenta con /fapi/v1/algoOrder usando algoId.
"""
from __future__ import annotations

import asyncio
import decimal
import hashlib
import hmac
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import aiohttp

from .config import Config
from .logger import get_logger

log = get_logger("order_manager")

_RETRY_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.5          # segundos


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de redondeo
# ──────────────────────────────────────────────────────────────────────────────

def _round_step(value: float, step: float) -> float:
    """Redondea value HACIA ABAJO al múltiplo más cercano de step."""
    d_step = decimal.Decimal(str(step))
    d_val  = decimal.Decimal(str(value))
    return float((d_val // d_step) * d_step)


def _round_price(value: float, tick: float) -> float:
    """Redondea al tick más cercano."""
    d_tick = decimal.Decimal(str(tick))
    d_val  = decimal.Decimal(str(value))
    return float(round(d_val / d_tick) * d_tick)


# ──────────────────────────────────────────────────────────────────────────────
# OrderManager
# ──────────────────────────────────────────────────────────────────────────────

class OrderManager:
    def __init__(self, cfg: Config):
        self._cfg     = cfg
        self._session: Optional[aiohttp.ClientSession] = None
        self._exinfo_cache: Dict[str, dict] = {}   # symbol → exchange info filters

    async def init(self):
        self._session = aiohttp.ClientSession(
            headers={"X-MBX-APIKEY": self._cfg.api_key},
            connector=aiohttp.TCPConnector(limit=50),
        )
        log.info("OrderManager inicializado")

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    # ──────────────────────────────────────────────────────────────────
    # HTTP helpers
    # ──────────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        qs = urlencode(params)
        sig = hmac.new(
            self._cfg.api_secret.encode("utf-8"),
            qs.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = sig
        return params

    async def _get(self, path: str, params: dict = None, signed: bool = False) -> Any:
        params = params or {}
        if signed:
            params = self._sign(params)
        url = self._cfg.base_url + path
        return await self._request("GET", url, params=params)

    async def _post(self, path: str, params: dict, signed: bool = True) -> Any:
        if signed:
            params = self._sign(params)
        url = self._cfg.base_url + path
        return await self._request("POST", url, data=params)

    async def _delete(self, path: str, params: dict, signed: bool = True) -> Any:
        if signed:
            params = self._sign(params)
        url = self._cfg.base_url + path
        return await self._request("DELETE", url, params=params)

    async def _put(self, path: str, params: dict, signed: bool = True) -> Any:
        if signed:
            params = self._sign(params)
        url = self._cfg.base_url + path
        return await self._request("PUT", url, params=params)

    async def _request(self, method: str, url: str, **kwargs) -> Any:
        t0 = time.time()
        last_exc = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with self._session.request(method, url, **kwargs) as resp:
                    elapsed = round((time.time() - t0) * 1000)
                    body = await resp.json(content_type=None)
                    log.debug(
                        f"[HTTP] {method} {url.split('?')[0]} "
                        f"status={resp.status} {elapsed}ms"
                    )
                    if resp.status in _RETRY_CODES:
                        wait = _BACKOFF_BASE ** attempt
                        log.warning(f"HTTP {resp.status} → reintento {attempt} en {wait:.1f}s")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 400:
                        code = body.get("code", resp.status) if isinstance(body, dict) else resp.status
                        msg  = body.get("msg",  str(body))   if isinstance(body, dict) else str(body)
                        raise BinanceError(code, msg)
                    return body
            except BinanceError:
                raise
            except Exception as e:
                last_exc = e
                wait = _BACKOFF_BASE ** attempt
                log.warning(f"Request error (attempt {attempt}): {e} → retry in {wait:.1f}s")
                await asyncio.sleep(wait)
        raise last_exc or RuntimeError(f"Request falló tras {_MAX_RETRIES} intentos")

    # ──────────────────────────────────────────────────────────────────
    # Account / Info
    # ──────────────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Devuelve el balance disponible en USDT."""
        data = await self._get("/fapi/v2/balance", signed=True)
        for asset in data:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        return 0.0

    async def get_exchange_info(self, symbol: str) -> dict:
        """Retorna tickSize, stepSize, minQty, minNotional para el símbolo."""
        if symbol in self._exinfo_cache:
            return self._exinfo_cache[symbol]
        data = await self._get("/fapi/v1/exchangeInfo")
        for s in data.get("symbols", []):
            if s["symbol"] != symbol:
                continue
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            info = {
                "tick_size":    float(filters.get("PRICE_FILTER", {}).get("tickSize",    "0.0001")),
                "step_size":    float(filters.get("LOT_SIZE",     {}).get("stepSize",    "0.001")),
                "min_qty":      float(filters.get("LOT_SIZE",     {}).get("minQty",      "0.001")),
                "min_notional": float(filters.get("MIN_NOTIONAL", {}).get("notional",    "5")),
            }
            self._exinfo_cache[symbol] = info
            log.debug(f"ExchangeInfo {symbol}: {info}")
            return info
        raise ValueError(f"Símbolo {symbol} no encontrado en exchangeInfo")

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        log.info(f"Configurando leverage {leverage}x para {symbol}")
        return await self._post("/fapi/v1/leverage",
                                {"symbol": symbol, "leverage": leverage})

    async def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> None:
        try:
            await self._post("/fapi/v1/marginType",
                             {"symbol": symbol, "marginType": margin_type})
            log.info(f"Margin type {margin_type} configurado para {symbol}")
        except BinanceError as e:
            if e.code == -4046:   # Already set
                log.debug(f"{symbol} ya tiene margin type {margin_type}")
            else:
                raise

    async def get_best_bid(self, symbol: str) -> float:
        data = await self._get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return float(data["bidPrice"])

    async def get_best_ask(self, symbol: str) -> float:
        data = await self._get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return float(data["askPrice"])

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["markPrice"])

    async def calc_sl_trigger(self, symbol: str, entry_price: float) -> float:
        """Calcula y redondea el stopPrice del SL para un SHORT."""
        info = await self.get_exchange_info(symbol)
        return _round_price(entry_price * (1 + self._cfg.sl_pct / 100),
                            info["tick_size"])

    async def get_position(self, symbol: str) -> Optional[dict]:
        data = await self._get("/fapi/v2/positionRisk",
                               {"symbol": symbol}, signed=True)
        for p in data:
            if p["symbol"] == symbol and float(p["positionAmt"]) != 0:
                return p
        return None

    async def get_all_positions(self) -> list:
        """Devuelve todas las posiciones abiertas (positionAmt != 0)."""
        data = await self._get("/fapi/v2/positionRisk", {}, signed=True)
        return [p for p in data if float(p.get("positionAmt", 0)) != 0]

    async def get_open_orders(self, symbol: str) -> list:
        return await self._get("/fapi/v1/openOrders",
                               {"symbol": symbol}, signed=True)

    async def get_open_algo_orders(self, symbol: str) -> list:
        """
        Devuelve las órdenes algo abiertas para el símbolo
        (TP/SL colocados vía /fapi/v1/algoOrder).
        Normaliza algoId → orderId para uso uniforme en reconciliación.
        """
        try:
            data = await self._get("/fapi/v1/openAlgoOrders",
                                   {"symbol": symbol}, signed=True)
            orders = data if isinstance(data, list) else data.get("orders", [])
            for o in orders:
                if "algoId" in o and "orderId" not in o:
                    o["orderId"] = o["algoId"]
            return orders
        except BinanceError as e:
            log.debug(f"get_open_algo_orders({symbol}): {e}")
            return []

    async def get_all_open_orders(self) -> list:
        """Obtiene TODAS las órdenes regulares abiertas en la cuenta (sin filtro de símbolo)."""
        return await self._get("/fapi/v1/openOrders", signed=True)

    async def get_all_open_algo_orders(self) -> list:
        """Obtiene TODAS las órdenes algo (condicionales) abiertas en la cuenta."""
        try:
            data = await self._get("/fapi/v1/openAlgoOrders", signed=True)
            orders = data if isinstance(data, list) else data.get("orders", [])
            for o in orders:
                if "algoId" in o and "orderId" not in o:
                    o["orderId"] = o["algoId"]
            return orders
        except BinanceError as e:
            log.debug(f"get_all_open_algo_orders: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────
    # Quantity calculation
    # ──────────────────────────────────────────────────────────────────

    async def calc_quantity(self, symbol: str, price: float) -> float:
        """
        Calcula la cantidad a operar:
          qty = capital_per_trade / price, redondeada a stepSize.
          Verifica minQty y minNotional.
        """
        info = await self.get_exchange_info(symbol)
        raw_qty = self._cfg.capital_per_trade / price
        qty     = _round_step(raw_qty, info["step_size"])

        if qty < info["min_qty"]:
            raise ValueError(
                f"{symbol}: qty={qty} < minQty={info['min_qty']}. "
                f"Sube capital_per_trade."
            )
        notional = qty * price
        if notional < info["min_notional"]:
            raise ValueError(
                f"{symbol}: notional={notional:.2f} < minNotional={info['min_notional']}. "
                f"Sube capital_per_trade."
            )
        log.debug(f"qty calc {symbol}: capital={self._cfg.capital_per_trade} "
                  f"price={price} → qty={qty}")
        return qty

    # ──────────────────────────────────────────────────────────────────
    # Entry order (SHORT, LIMIT GTX / post-only)
    # ──────────────────────────────────────────────────────────────────

    async def open_short(self, symbol: str, quantity: float,
                         price: float = None,
                         price_match: str = None,
                         newClientOrderId: str = None) -> dict:
        params = {
            "symbol":       symbol,
            "side":         "SELL",
            "positionSide": "BOTH",
            "type":         "LIMIT",
            "quantity":     str(quantity),
        }
        if newClientOrderId:
            params["newClientOrderId"] = newClientOrderId
        if price_match:
            params["timeInForce"] = "GTC"
            params["priceMatch"]  = price_match
            log.info(f"[ENTRY] open_short {symbol} qty={quantity} priceMatch={price_match}")
        else:
            info    = await self.get_exchange_info(symbol)
            price_r = _round_price(price, info["tick_size"])
            params["timeInForce"] = "GTX"
            params["price"]       = str(price_r)
            log.info(f"[ENTRY] open_short {symbol} qty={quantity} price={price_r}")

        result = await self._post("/fapi/v1/order", params)
        log.info(f"[ENTRY] orderId={result.get('orderId')} status={result.get('status')}")
        return result

    async def open_short_market(self, symbol: str, quantity: float,
                                newClientOrderId: str = None) -> dict:
        """SELL MARKET para abrir short. Fallback cuando BBO no llena."""
        params = {
            "symbol":       symbol,
            "side":         "SELL",
            "positionSide": "BOTH",
            "type":         "MARKET",
            "quantity":     str(quantity),
        }
        if newClientOrderId:
            params["newClientOrderId"] = newClientOrderId
        log.info(f"[ENTRY_MARKET] open_short_market {symbol} qty={quantity}")
        result = await self._post("/fapi/v1/order", params)
        log.info(f"[ENTRY_MARKET] orderId={result.get('orderId')} status={result.get('status')}")
        return result

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """
        Cancela una orden. Si Binance devuelve -2011 (orden no encontrada
        en /fapi/v1/order), reintenta con /fapi/v1/algoOrder usando algoId.
        """
        log.info(f"[CANCEL] {symbol} orderId={order_id}")
        try:
            return await self._delete("/fapi/v1/order",
                                      {"symbol": symbol, "orderId": order_id})
        except BinanceError as e:
            if e.code == -2011:
                log.debug(f"[CANCEL] orderId={order_id} no en /fapi/v1/order, "
                          f"intentando /fapi/v1/algoOrder algoId={order_id}")
                return await self._delete("/fapi/v1/algoOrder",
                                          {"symbol": symbol, "algoId": order_id})
            raise

    async def get_order(self, symbol: str, order_id: int) -> dict:
        return await self._get("/fapi/v1/order",
                               {"symbol": symbol, "orderId": order_id},
                               signed=True)

    # ──────────────────────────────────────────────────────────────────
    # TP / SL vía servicio Algo de Binance (/fapi/v1/algoOrder)
    #
    # NOTA: Esta cuenta está migrada al servicio Algo de Binance.
    # Se usa algoType="CONDITIONAL" para TP y SL nativos que sobreviven
    # a reinicios del proceso.
    #
    # TP → TAKE_PROFIT Algo con BBO (priceMatch=OPPONENT).
    #   triggerPrice = entry * (1 - tp_pct/100).
    #   Cuando mark_price <= stopPrice ejecuta al mejor ask (BBO).
    #
    # SL → STOP_MARKET Algo con workingType=MARK_PRICE.
    #   triggerPrice = entry * (1 + sl_pct/100).
    #   Cuando mark_price >= stopPrice ejecuta MARKET.
    # ──────────────────────────────────────────────────────────────────

    async def place_tp(self, symbol: str, quantity: float,
                       entry_price: float) -> dict:
        """
        TAKE_PROFIT CONDITIONAL (algo) con BBO — TP para SHORT.
        Cuando mark_price <= stopPrice, Binance ejecuta al mejor ask (BBO).
        La orden vive en Binance aunque el proceso se reinicie.
          triggerPrice = entry * (1 - tp_pct/100)
        """
        info         = await self.get_exchange_info(symbol)
        tp_trigger   = entry_price * (1 - self._cfg.tp_pct / 100)
        tp_trigger_r = _round_price(tp_trigger, info["tick_size"])
        price_match  = self._cfg.sl_price_match or "OPPONENT"

        params = {
            "symbol":       symbol,
            "side":         "BUY",
            "positionSide": "BOTH",
            "type":         "TAKE_PROFIT",
            "algoType":     "CONDITIONAL",
            "quantity":     str(quantity),
            "triggerPrice": str(tp_trigger_r),
            "priceMatch":   price_match,
            "timeInForce":  "GTC",
            "workingType":  "MARK_PRICE",
            "reduceOnly":   "true",
            "priceProtect": "true",
        }
        log.info(f"[TP] place_tp {symbol} entry={entry_price} "
                 f"triggerPrice={tp_trigger_r} priceMatch={price_match}")

        result = await self._post("/fapi/v1/algoOrder", params)
        if "algoId" in result and "orderId" not in result:
            result["orderId"] = result["algoId"]

        log.info(f"[TP] algoId={result.get('algoId', result.get('orderId'))} "
                 f"triggerPrice={tp_trigger_r}")
        return result

    async def place_sl(self, symbol: str, quantity: float,
                       entry_price: float) -> dict:
        """
        STOP_MARKET CONDITIONAL (algo) — SL para SHORT.
        Cuando mark_price >= stopPrice, Binance ejecuta BUY MARKET.
        La orden vive en Binance aunque el proceso se reinicie.
          triggerPrice = entry * (1 + sl_pct/100)
        """
        info         = await self.get_exchange_info(symbol)
        sl_trigger   = entry_price * (1 + self._cfg.sl_pct / 100)
        sl_trigger_r = _round_price(sl_trigger, info["tick_size"])

        params = {
            "symbol":       symbol,
            "side":         "BUY",
            "positionSide": "BOTH",
            "type":         "STOP_MARKET",
            "algoType":     "CONDITIONAL",
            "quantity":     str(quantity),
            "triggerPrice": str(sl_trigger_r),
            "workingType":  "MARK_PRICE",
            "reduceOnly":   "true",
            "priceProtect": "true",
        }
        log.info(f"[SL] place_sl {symbol} entry={entry_price} "
                 f"triggerPrice={sl_trigger_r} workingType=MARK_PRICE")

        result = await self._post("/fapi/v1/algoOrder", params)
        if "algoId" in result and "orderId" not in result:
            result["orderId"] = result["algoId"]

        log.info(f"[SL] algoId={result.get('algoId', result.get('orderId'))} "
                 f"triggerPrice={sl_trigger_r}")
        return result

    # ──────────────────────────────────────────────────────────────────
    # Exit orders (timeout / manual)
    # ──────────────────────────────────────────────────────────────────

    async def close_position_limit(self, symbol: str, quantity: float,
                                   price: float) -> dict:
        """BUY LIMIT reduceOnly para cerrar short (timeout / cierre manual)."""
        info    = await self.get_exchange_info(symbol)
        price_r = _round_price(price, info["tick_size"])
        params  = {
            "symbol":       symbol,
            "side":         "BUY",
            "positionSide": "BOTH",
            "type":         "LIMIT",
            "timeInForce":  "GTC",
            "quantity":     str(quantity),
            "price":        str(price_r),
            "reduceOnly":   "true",
        }
        log.info(f"[CLOSE_LIMIT] {symbol} qty={quantity} price={price_r}")
        return await self._post("/fapi/v1/order", params)

    async def close_position_bbo(self, symbol: str, quantity: float) -> dict:
        """BUY BBO reduceOnly para cerrar short (timeout / cierre manual)."""
        params = {
            "symbol":       symbol,
            "side":         "BUY",
            "positionSide": "BOTH",
            "type":         "LIMIT",
            "timeInForce":  "GTC",
            "priceMatch":   "OPPONENT",
            "quantity":     str(quantity),
            "reduceOnly":   "true",
        }
        log.info(f"[CLOSE_BBO] {symbol} qty={quantity} priceMatch=OPPONENT")
        return await self._post("/fapi/v1/order", params)

    async def close_position_market(self, symbol: str, quantity: float) -> dict:
        """BUY MARKET reduceOnly — último recurso si está configurado."""
        params = {
            "symbol":       symbol,
            "side":         "BUY",
            "positionSide": "BOTH",
            "type":         "MARKET",
            "quantity":     str(quantity),
            "reduceOnly":   "true",
        }
        log.warning(f"[CLOSE_MARKET] {symbol} qty={quantity} — market order!")
        return await self._post("/fapi/v1/order", params)

    # ──────────────────────────────────────────────────────────────────
    # User Data Stream (listenKey)
    # ──────────────────────────────────────────────────────────────────

    async def get_listen_key(self) -> str:
        data = await self._post("/fapi/v1/listenKey", {}, signed=False)
        key  = data["listenKey"]
        log.info(f"ListenKey obtenido: {key[:20]}...")
        return key

    async def keep_alive_listen_key(self, listen_key: str) -> None:
        await self._put("/fapi/v1/listenKey",
                        {"listenKey": listen_key}, signed=False)
        log.debug("ListenKey renovado")

    async def close_listen_key(self, listen_key: str) -> None:
        await self._delete("/fapi/v1/listenKey",
                           {"listenKey": listen_key}, signed=False)
        log.info("ListenKey cerrado")


# ──────────────────────────────────────────────────────────────────────────────
# Excepción propia
# ──────────────────────────────────────────────────────────────────────────────

class BinanceError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg  = msg
        super().__init__(f"Binance error {code}: {msg}")
