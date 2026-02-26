# Guía de uso — gestiona_trades

Sistema de trading automático de posiciones SHORT en Binance Futures.
Lee señales generadas por `selecciona_pares.py`, abre posiciones y las gestiona
con TP/SL hasta su cierre.

---

## Índice

1. [Requisitos](#1-requisitos)
2. [Instalación](#2-instalación)
3. [Configuración](#3-configuración)
4. [Estructura de ficheros](#4-estructura-de-ficheros)
5. [Flujo completo del sistema](#5-flujo-completo-del-sistema)
6. [Ciclo de vida de un trade](#6-ciclo-de-vida-de-un-trade)
7. [Órdenes de entrada BBO](#7-órdenes-de-entrada-bbo)
8. [Dashboard web](#8-dashboard-web)
9. [API REST](#9-api-rest)
10. [Base de datos SQLite](#10-base-de-datos-sqlite)
11. [Logs](#11-logs)
12. [Arranque y parada](#12-arranque-y-parada)
13. [Casos de uso habituales](#13-casos-de-uso-habituales)
14. [Errores comunes](#14-errores-comunes)

---

## 1. Requisitos

- **Python 3.11+**
- Cuenta Binance Futures con API habilitada (permisos: Futures trading)
- `selecciona_pares.py` corriendo y generando `fut_pares_short.csv`

### Coordinación con selecciona_pares.py

Los dos scripts se comunican a través del CSV `fut_pares_short.csv`.
Cada uno tiene su propio fichero de configuración con el path de ese CSV:

| Script | Config | Campo |
|--------|--------|-------|
| `selecciona_pares.py` | `selecciona_pares.json` | `output_file` |
| `gestiona_trades.py`  | `config.yaml`           | `signals.file_path` |

**Ambos valores deben apuntar al mismo fichero.** Si se lanza cada script desde
un directorio distinto, usar rutas absolutas en los dos sitios.

```
selecciona_pares.json          config.yaml
──────────────────────         ──────────────────────
"output_file":                 signals:
  "/datos/fut_pares_short.csv"   file_path: "/datos/fut_pares_short.csv"
        │                                   ▲
        └───── mismo fichero ───────────────┘
```

`selecciona_pares.py` se configura copiando la plantilla:
```bash
cd trade_futuros/
cp selecciona_pares.json.example selecciona_pares.json
# editar output_file, datos_dir, filtros, etc.
```

Parámetros principales de `selecciona_pares.json`:

| Campo | Default | Descripción |
|-------|---------|-------------|
| `output_file` | `"fut_pares_short.csv"` | Ruta donde escribe el CSV de señales |
| `datos_dir` | `"datos"` | Directorio donde guarda las velas descargadas por par |
| `interval_min` | `10` | Cada cuántos minutos se ejecuta un ciclo (múltiplo de 5) |
| `lookback_candles` | `12` | Velas usadas para calcular el momentum (12 × 5 min = 1 h) |
| `mom_min` | `3.0` | Momentum mínimo (%) para incluir un par |
| `vol_min` | `4.0` | Vol ratio mínimo para incluir un par |
| `tr_min` | `5.0` | Trades ratio mínimo para incluir un par |
| `quintil` | `[1,2,3]` | Quintiles de marketcap permitidos (1=small, 5=large) |

### Compatibilidad de sistema operativo

| Sistema | Soporte | Notas |
|---------|---------|-------|
| **Linux** | ✅ Completo | Incluye despliegue con systemd |
| **Windows 10/11** | ✅ Completo | El script configura automáticamente el event loop de asyncio |
| macOS | ✅ Completo | Sin configuración especial |

> El script detecta automáticamente si está en Windows y aplica
> `WindowsSelectorEventLoopPolicy` para compatibilidad con `aiohttp`.
> No se necesita ninguna configuración manual.

---

## 2. Instalación

**Linux / macOS:**
```bash
cd trade_futuros/gestiona_trades/

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
cd trade_futuros\gestiona_trades

python -m venv .venv

# Solo la primera vez: habilitar ejecución de scripts locales
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

**Windows (CMD):**
```cmd
cd trade_futuros\gestiona_trades

python -m venv .venv
.venv\Scripts\activate.bat

pip install -r requirements.txt
```

> **El venv es permanente.** Una vez creado e instaladas las dependencias,
> **no vuelvas a ejecutar `python -m venv .venv`** — eso lo recrea desde cero
> borrando todos los paquetes. La próxima vez que abras una terminal solo
> necesitas activarlo:
>
> ```powershell
> # Windows — solo esto cada vez que abras una terminal nueva
> .venv\Scripts\Activate.ps1
> python gestiona_trades.py
> ```
> ```bash
> # Linux / macOS
> source .venv/bin/activate
> python gestiona_trades.py
> ```
>
> **Nota Dropbox:** si el proyecto está en Dropbox, excluye la carpeta `.venv`
> de la sincronización (son miles de ficheros pequeños). En la app de Dropbox:
> Preferencias → Sincronización → Excluir carpetas → selecciona `.venv`.

Dependencias instaladas:

| Paquete | Uso |
|---------|-----|
| `aiohttp>=3.9` | HTTP async (cliente REST + servidor dashboard) |
| `aiosqlite>=0.19` | SQLite async (persistencia de trades) |
| `PyYAML>=6.0` | Lectura de config.yaml |
| `websockets>=12` | WebSocket Binance User Data Stream |

---

## 3. Configuración

Copiar la plantilla y editarla:

```bash
cp config.yaml.example config.yaml
nano config.yaml
```

### Secciones principales

#### `binance` — credenciales y entorno

```yaml
binance:
  api_key:    "TU_API_KEY"
  api_secret: "TU_API_SECRET"
  base_url:   "https://fapi.binance.com"        # producción
  # base_url: "https://testnet.binancefuture.com"  # testnet
```

> El WebSocket se deriva automáticamente de `base_url`:
> - `fapi.binance.com` → `wss://fstream.binance.com`
> - testnet → `wss://stream.binancefuture.com`

#### `strategy` — parámetros de trading

```yaml
strategy:
  mode:                "short"   # solo "short" por ahora
  capital_per_trade:   10        # USDT a invertir por trade
  max_open_trades:     10        # trades abiertos simultáneos (global)
  max_trades_per_pair: 1         # trades abiertos en el mismo par
  leverage:            1         # apalancamiento (1 = sin apalancamiento)
  tp_pct:              15        # take profit en %
  sl_pct:              60        # stop loss en %
  trigger_offset_pct:  10        # trigger N% antes del precio objetivo
  timeout_hours:       24        # cerrar trade si supera N horas abierto
  top_n:               1         # procesar solo los N primeros ranks
```

**Valores válidos para `mode`:**

| Valor | Descripción |
|-------|-------------|
| `"short"` | Opera posiciones cortas (ventas en descubierto). Único modo disponible actualmente. |

**Cálculo de precios para SHORT** (con `entry_price = 100`):

| Parámetro | Fórmula | Ejemplo (tp=15%, sl=60%) | Mecanismo |
|-----------|---------|--------------------------|-----------|
| `tp_trigger` | `entry × (1 - tp/100)` | `85.00` | Algo TAKE_PROFIT — BBO al mejor ask cuando mark ≤ stopPrice |
| `sl_trigger` | `entry × (1 + sl/100)` | `160.00` | Algo STOP_MARKET — ejecuta MARKET cuando mark ≥ stopPrice |

Ambas órdenes se colocan en Binance vía `/fapi/v1/algoOrder` y **sobreviven al reinicio del proceso**.

> `trigger_offset_pct` está en la configuración por compatibilidad histórica
> pero **no se aplica** en la implementación actual.

#### `strategy` — filtros de señal

```yaml
strategy:
  min_momentum_pct:   0          # descartar si mom_1h_pct < este valor
  min_vol_ratio:      0          # descartar si vol_ratio < este valor (0=desactivado)
  min_trades_ratio:   0          # descartar si trades_ratio < este valor
  allowed_quintiles:  [1,2,3,4,5] # quintiles permitidos ([] = todos)
```

**Valores válidos para `allowed_quintiles`:** lista de enteros en el rango `[1, 5]`.
Lista vacía `[]` equivale a permitir todos.

| Quintil | Significado |
|---------|-------------|
| `1` | Capitalización más pequeña (small cap) |
| `2` | Segunda banda de capitalización |
| `3` | Capitalización media |
| `4` | Cuarta banda de capitalización |
| `5` | Capitalización más grande (large cap) |

#### `signals` — fuente de señales

```yaml
signals:
  file_path:              "fut_pares_short.csv"  # ruta al CSV de selecciona_pares
  poll_interval_seconds:  15                     # frecuencia de chequeo
  max_signal_age_minutes: 10                     # señales más viejas → ignorar
```

> **Cómo funciona `file_path`:**
> La ruta es **relativa al directorio de trabajo** desde donde ejecutas
> `python gestiona_trades.py` (no relativa a `config.yaml`).
>
> | Situación | Valor recomendado |
> |-----------|-------------------|
> | Ambos scripts en el mismo directorio, lanzados desde allí | `"fut_pares_short.csv"` |
> | `selecciona_pares.py` escribe en otro directorio | Ruta absoluta: `"/datos/fut_pares_short.csv"` |
> | Windows, distintas carpetas | `"C:\\ruta\\a\\fut_pares_short.csv"` |
>
> Si el fichero no existe al arrancar, el sistema espera en silencio hasta que aparezca.
> El log muestra la ruta resuelta al iniciar:
> `[INFO] SignalWatcher iniciado → /ruta/completa/fut_pares_short.csv`

#### `entry` — apertura de posición

```yaml
entry:
  order_type:             "BBO"   # "BBO" (recomendado) | "LIMIT_GTX"
  chase_interval_seconds: 2       # recolocar limit cada N segundos si no hay fill
  chase_timeout_seconds:  30      # cancelar si no fill en N segundos
  max_chase_attempts:     3       # intentos antes de descartar la señal
  market_fallback:        false   # usar MARKET si BBO no llena tras todos los intentos
```

**Valores válidos para `order_type`:**

| Valor | Descripción |
|-------|-------------|
| `"BBO"` | Best Bid Offer — Binance calcula el precio en el servidor. Attempt 1 usa `OPPONENT_5`; chase usa `OPPONENT`. **Recomendado.** |
| `"LIMIT_GTX"` | Orden LIMIT post-only (GTX). El cliente calcula y envía el precio. Susceptible al error `-5022` si el mercado se mueve durante el envío. |

El sistema usa **órdenes BBO** (Best Bid Offer) para abrir posición:

- **Attempt 1** → `OPPONENT_5` (Counterparty 5): precio en el 5º mejor bid,
  entrada conservadora que permite capturar un precio algo mejor si el mercado
  tiene profundidad suficiente.
- **Chase (2+)** → `OPPONENT` (Counterparty 1): precio en el mejor bid,
  máxima agresividad para asegurar el fill en el menor tiempo posible.

Si no se llena en `chase_timeout_seconds`, cancela y reintenta hasta
`max_chase_attempts` veces. Con `market_fallback: true`, si ningún intento BBO
llena, se coloca una orden MARKET como último recurso para garantizar la entrada.
Sin fallback, el trade queda `NOT_EXECUTED`.

> Ver sección [Órdenes de entrada BBO](#órdenes-de-entrada-bbo) para más detalle.

#### `exit` — cierre por timeout y stop loss

```yaml
exit:
  timeout_order_type:      "BBO"    # "BBO" (recomendado) | "LIMIT" | "MARKET"
  timeout_chase_seconds:   30       # tiempo máximo para BBO/LIMIT antes de market
  timeout_market_fallback: true     # si no llena → MARKET
  sl_mark_poll_interval:   1.0      # cada cuántos segundos sondear el mark price para SL
```

**Valores válidos para `timeout_order_type`:**

| Valor | Descripción |
|-------|-------------|
| `"BBO"` | Cierre con orden BBO (`priceMatch=OPPONENT` = mejor ask). Binance calcula el precio en el servidor. Siempre maker. Fallback MARKET si `timeout_market_fallback: true`. **Recomendado.** |
| `"LIMIT"` | Cierre con orden LIMIT al mejor ask calculado por el cliente. Fallback MARKET si `timeout_market_fallback: true`. |
| `"MARKET"` | Cierre directo con orden de mercado. Garantiza fill inmediato; ignora `timeout_chase_seconds` y `timeout_market_fallback`. |

`timeout_market_fallback: true` activa el cierre MARKET automático si la orden
BBO o LIMIT no se llena en `timeout_chase_seconds`. Solo aplica cuando
`timeout_order_type` es `"BBO"` o `"LIMIT"`.

#### Stop Loss y Take Profit — órdenes Algo nativas en Binance

Tanto el TP como el SL se colocan en Binance vía `/fapi/v1/algoOrder`,
lo que garantiza que **sobreviven a un reinicio del proceso**.

```
tp_trigger = entry_price × (1 - tp_pct/100)   → Algo TAKE_PROFIT BBO GTC
sl_trigger = entry_price × (1 + sl_pct/100)   → Algo STOP_MARKET MARK_PRICE
```

- **TP (TAKE_PROFIT Algo)**: cuando el mark price baja al `stopPrice`, Binance
  ejecuta al mejor ask (`priceMatch=OPPONENT`). Siempre maker.
- **SL (STOP_MARKET Algo)**: cuando el mark price sube al `stopPrice`, Binance
  ejecuta a MARKET. Garantiza el cierre aunque el proceso esté caído.

Esta arquitectura usa `/fapi/v1/algoOrder` para evitar el error `-4120` que
Binance devuelve para órdenes condicionales en cuentas migradas al servicio Algo.

> `trigger_offset_pct` y `sl_mark_poll_interval` están en la configuración
> por compatibilidad histórica pero **no se aplican** en la implementación actual.

#### `dashboard` / `logging` / `database`

```yaml
dashboard:
  enabled: true
  host:    "0.0.0.0"   # escuchar en todas las interfaces
  port:    8080

logging:
  level:         "DEBUG"                   # fichero (todo)
  console_level: "INFO"                    # consola (solo INFO+)
  file:          "logs/gestiona_trades.log"
  max_bytes:     10485760                  # 10 MB por fichero
  backup_count:  5                         # hasta 5 ficheros rotados

database:
  path: "data/trades.db"
```

**Valores válidos para `logging.level` y `logging.console_level`:**

| Valor | Detalle registrado |
|-------|--------------------|
| `"DEBUG"` | Todo: requests HTTP, precios, decisiones internas. Recomendado para `level` (fichero). |
| `"INFO"` | Eventos clave: señales, aperturas, cierres, errores. Recomendado para `console_level`. |
| `"WARNING"` | Solo advertencias y errores. |
| `"ERROR"` | Solo errores recuperables y críticos. |
| `"CRITICAL"` | Solo fallos fatales. |

---

## 4. Estructura de ficheros

```
gestiona_trades/
├── gestiona_trades.py       ← punto de entrada
├── config.yaml              ← configuración activa (NO subir a git)
├── config.yaml.example      ← plantilla
├── requirements.txt
│
├── src/
│   ├── models.py            ← dataclasses Signal, Trade, Event; enums
│   ├── config.py            ← carga y validación de config.yaml
│   ├── logger.py            ← logging rotativo
│   ├── state.py             ← persistencia SQLite async
│   ├── order_manager.py     ← Binance Futures REST API
│   ├── ws_manager.py        ← WebSocket User Data Stream
│   ├── signal_watcher.py    ← vigilancia de fut_pares_short.csv
│   ├── trade_engine.py      ← máquina de estados del trade
│   └── dashboard.py         ← servidor web aiohttp
│
├── static/
│   └── dashboard.html       ← SPA de monitorización
│
├── logs/
│   └── gestiona_trades.log  ← creado automáticamente
│
└── data/
    └── trades.db            ← creado automáticamente
```

Los directorios `logs/` y `data/` se crean automáticamente al arrancar.

---

## 5. Flujo completo del sistema

```
selecciona_pares.py
      │
      │  escribe fut_pares_short.csv  (leido="no")
      ▼
 SignalWatcher  ─── cada 15s chequea mtime del CSV
      │
      │  señal válida (leido=no, edad<10min, pasa filtros)
      │  marca leido="si" en CSV (escritura atómica)
      ▼
 TradeEngine.on_signal()
      │
      │  verifica max_open_trades y max_trades_per_pair
      │  crea Trade(status=SIGNAL_RECEIVED) → guarda en DB
      │
      │  [task async: _open_trade]
      │  ┌── configura leverage+margin para el par
      │  ├── obtiene best_bid (referencia para calcular cantidad)
      │  ├── coloca BBO SELL (attempt 1: OPPONENT_5 / chase: OPPONENT)
      │  └── espera fill via WebSocket (chase_timeout segundos)
      │       │
      │       │  ORDER_TRADE_UPDATE FILLED → on_entry_fill()
      │       ▼
      │  Trade(status=OPEN, entry_price=X, entry_quantity=Y)
      │  coloca Algo TAKE_PROFIT BBO (TP) + Algo STOP_MARKET (SL) en Binance
      │
      │  [loop timeout: cada 60s revisa todos los trades abiertos]
      │
      ├── TP llena → detiene monitor SL → PnL > 0 → Trade(status=CLOSED, exit=tp)
      ├── SL (mark≥trigger) → BUY MARKET → cancela TP → CLOSED (exit=sl)
      └── timeout → cancela TP + detiene monitor SL → BBO/LIMIT/MARKET → CLOSED(timeout)
```

---

## 6. Ciclo de vida de un trade

```
SIGNAL_RECEIVED
     │
     ▼
  OPENING  ←── chase loop BBO (max_chase_attempts)
     │              │
     │         no fill ──→ [market_fallback=true] MARKET order ──→ (entry fill)
     │                              │
     │                         no fill (10s) → NOT_EXECUTED
     │
     ▼ (entry fill)
   OPEN
     │
     ├──── TP fill ──→ CLOSING ──→ CLOSED (exit_type=tp)
     ├──── SL fill ──→ CLOSING ──→ CLOSED (exit_type=sl)
     └──── timeout ──→ CLOSING ──→ CLOSED (exit_type=timeout)

     (en cualquier estado con error grave → ERROR)
```

### Campos del trade en la DB

| Campo | Descripción |
|-------|-------------|
| `trade_id` | UUID único |
| `pair` | Ej: `BTCUSDT` |
| `signal_ts` | Timestamp de la señal original |
| `status` | Estado actual (ver diagrama) |
| `entry_price` | Precio de apertura (fill) |
| `entry_quantity` | Cantidad en unidades del activo |
| `tp_price` / `sl_price` | Precio límite TP/SL |
| `tp_trigger_price` / `sl_trigger_price` | Precio de activación de la orden |
| `exit_price` | Precio de cierre (fill) |
| `exit_type` | `tp` / `sl` / `timeout` / `manual` |
| `pnl_usdt` | PnL neto (descontando fees) |
| `pnl_pct` | PnL en % sobre capital invertido |
| `fees_usdt` | Comisiones estimadas (0.04% × 2) |

---

## 7. Órdenes de entrada BBO

### ¿Qué es una orden BBO?

**Best Bid Offer (BBO)** es un tipo de orden LIMIT de Binance Futures en la que
**no se especifica precio**. En su lugar, se indica un modo (`priceMatch`) y
Binance calcula el precio en el servidor en el momento exacto de procesar la
orden, tomando como referencia el estado del order book en ese instante.

Documentación oficial Binance:
[Understanding and Using BBO Orders on Binance Futures](https://www.binance.com/en/support/faq/understanding-and-using-bbo-orders-on-binance-futures-7f93c89ef09042678cfa73e8a28612e8)

### Opciones de priceMatch

Para una orden **SELL** (short entry):

| `priceMatch` | Nombre | Precio resultante |
|---|---|---|
| `OPPONENT` | Counterparty 1 | Mejor bid (el más alto disponible) |
| `OPPONENT_5` | Counterparty 5 | 5º mejor bid |
| `QUEUE` | Queue 1 | Mejor ask (lado vendedor) |
| `QUEUE_5` | Queue 5 | 5º mejor ask |

> Para un **SELL**, el lado "counterparty" es el bid (compradores).
> El mejor bid es el precio más alto que alguien está dispuesto a pagar,
> por lo que vender a ese precio es lo más favorable para el vendedor.

### Estrategia de chase en este sistema

```
Attempt 1  →  priceMatch = OPPONENT_5  (Counterparty 5)
Chase 2+   →  priceMatch = OPPONENT    (Counterparty 1)
```

**¿Por qué esta progresión?**

- **Attempt 1 — OPPONENT_5**: el mercado acaba de generar la señal. Se intenta
  entrar a un precio ligeramente más conservador (5º mejor bid), que en un
  libro con profundidad normal es solo 1-2 ticks inferior al mejor bid. Si el
  mercado tiene liquidez, la orden se llena igualmente y capturamos un precio
  algo mejor para el short.

- **Chase (2+) — OPPONENT**: si el primer intento no se llenó en
  `chase_timeout_seconds`, el mercado se ha movido. Se cambia a `OPPONENT`
  (mejor bid) para maximizar la probabilidad de fill en los intentos restantes.

### Ventajas sobre LIMIT GTX (post-only con precio manual)

| | GTX con precio manual | BBO |
|---|---|---|
| Precio calculado | En el cliente, antes de enviar | En el servidor, al procesar |
| Riesgo de –5022 | Sí (precio desactualizado si el mercado se mueve) | No (precio siempre coherente con el book) |
| Latencia de red | Puede desfasar el precio | Irrelevante |
| Garantía maker | Solo si el precio no cruza el spread | Siempre maker |

### Parámetros de la API

**Apertura (SELL — entrada short):**
```
POST /fapi/v1/order
  symbol       = BTCUSDT
  side         = SELL
  positionSide = BOTH
  type         = LIMIT
  timeInForce  = GTC
  priceMatch   = OPPONENT_5   ← attempt 1; o OPPONENT en el chase
  quantity     = 0.003
  # sin campo "price"
```

**Cierre por timeout (BUY — salida short):**
```
POST /fapi/v1/order
  symbol       = BTCUSDT
  side         = BUY
  positionSide = BOTH
  type         = LIMIT
  timeInForce  = GTC
  priceMatch   = OPPONENT     ← para BUY, OPPONENT = mejor ask
  quantity     = 0.003
  reduceOnly   = true
  # sin campo "price"
```

> Para un **BUY**, el lado "counterparty" es el ask (vendedores).
> `OPPONENT` en una orden BUY = el precio más bajo que alguien está dispuesto
> a vender, que es exactamente el precio de cierre óptimo.

> `BBO_GTC` (Good Till Cancel) mantiene la orden activa hasta que se llene o
> se cancele explícitamente. El sistema la cancela tras `chase_timeout_seconds`
> (entrada) o `timeout_chase_seconds` (cierre) si no hay fill.

### Cálculo de cantidad

Aunque la orden BBO no lleva precio, la cantidad en unidades del activo sí
debe calcularse antes de enviarla. El sistema usa `get_best_bid()` como precio
de referencia para `calc_quantity()`:

```
capital_per_trade (USDT)
qty = ─────────────────────────
       best_bid × leverage
```

Esta cantidad se redondea al `stepSize` del par según los filtros de
`/fapi/v1/exchangeInfo`. El precio de referencia no se envía a Binance; sirve
únicamente para dimensionar la posición.

---

## 8. Dashboard web

Acceder en `http://localhost:8080` (o la IP del servidor).

### Secciones

**Chips de estado** (fila superior):
- Balance USDT disponible
- Trades abiertos / máximo
- PnL del día y PnL total
- Trades cerrados hoy
- Win rate %
- Estado WebSocket Binance

**Tabla de trades abiertos**: par, estado, precio entrada, TP/SL, tiempo transcurrido.

**Tabla de trades cerrados** (últimos 200): par, PnL, tipo de salida
(✅ TP / ❌ SL / ⏰ timeout / 🔧 manual).

**Log de eventos en vivo**: stream de eventos con color por tipo,
auto-scroll al último. Tipos:

| Color | Eventos |
|-------|---------|
| Verde | `entry_fill`, `tp_fill`, `startup` |
| Rojo | `sl_fill`, `error`, `ws_disconnect` |
| Amarillo | `timeout`, `cancel` |
| Azul | `signal`, `entry_sent`, `tp_placed`, `sl_placed` |
| Gris | `ws_connect`, `shutdown` |

El dashboard se actualiza en tiempo real vía WebSocket. Si la conexión
se pierde, hace polling REST cada 30 segundos como fallback.

---

## 9. API REST

Base URL: `http://localhost:8080`

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/api/status` | Estado general: balance, PnL, open count, uptime |
| GET | `/api/trades` | Últimos 200 trades (activos + cerrados) |
| GET | `/api/trades/{id}` | Detalle de un trade + sus eventos |
| GET | `/api/events?limit=N` | Últimos N eventos (default 100) |
| GET | `/api/config` | Configuración activa (sin API keys) |
| POST | `/api/trades/{id}/close` | Cierre manual de emergencia |

Ejemplo:
```bash
# Estado general
curl http://localhost:8080/api/status | python -m json.tool

# Todos los trades
curl http://localhost:8080/api/trades | python -m json.tool

# Detalle de un trade específico
curl http://localhost:8080/api/trades/a1b2c3d4-... | python -m json.tool

# Últimos 20 eventos
curl "http://localhost:8080/api/events?limit=20" | python -m json.tool
```

---

## 10. Base de datos SQLite

Ubicación: `data/trades.db` (configurable).

```bash
# Ver trades abiertos
sqlite3 data/trades.db "SELECT trade_id, pair, status, entry_price, pnl_usdt
                         FROM trades WHERE status NOT IN ('closed','not_executed')
                         ORDER BY created_at DESC;"

# PnL total
sqlite3 data/trades.db "SELECT SUM(pnl_usdt), COUNT(*) FROM trades WHERE status='closed';"

# Win rate
sqlite3 data/trades.db "SELECT
  SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
  COUNT(*) AS total,
  ROUND(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS win_pct
  FROM trades WHERE status='closed';"

# Últimos 10 eventos
sqlite3 data/trades.db "SELECT timestamp, event_type, details
                         FROM events ORDER BY event_id DESC LIMIT 10;"
```

Tablas:
- **`trades`**: un registro por trade con todo su ciclo de vida.
- **`events`**: log de auditoría cronológico con `trade_id` nullable (eventos
  globales como STARTUP/SHUTDOWN no tienen trade_id).

---

## 11. Logs

Fichero: `logs/gestiona_trades.log` (rotativo, máx. 10 MB × 5 ficheros).

Formato:
```
2024-01-15 10:23:45 [INFO ] [trade_engine] Trade a1b2c3d4 SIGNAL_RECEIVED BTCUSDT
2024-01-15 10:23:46 [INFO ] [order_manager] BTCUSDT SELL SHORT qty=0.003 price=42150.00
2024-01-15 10:23:47 [INFO ] [trade_engine] Trade a1b2c3d4 OPEN entry=42150.00 qty=0.003
2024-01-15 10:23:47 [INFO ] [trade_engine] Trade a1b2c3d4 TP placed @ 35827.50 trigger=39445.75
2024-01-15 10:23:47 [INFO ] [trade_engine] Trade a1b2c3d4 SL placed @ 67440.00 trigger=60696.00
```

Seguir el log en tiempo real:
```bash
tail -f logs/gestiona_trades.log
tail -f logs/gestiona_trades.log | grep -E "(OPEN|CLOSED|ERROR|tp_fill|sl_fill)"
```

---

## 12. Arranque y parada

### Arrancar

```bash
# Con config por defecto (config.yaml en el mismo directorio)
python gestiona_trades.py

# Con config en otra ruta
python gestiona_trades.py --config /ruta/a/mi_config.yaml
python gestiona_trades.py -c /ruta/a/mi_config.yaml
```

### Verificar que funciona

```bash
# Los primeros mensajes deben ser:
# [INFO] gestiona_trades arrancando — modo=short
# [INFO] DB inicializada: data/trades.db
# [INFO] Balance USDT disponible: 1234.56
# [INFO] WSManager: listenKey obtenido
# [INFO] WSManager conectado a wss://fstream.binance.com/ws/...
# [INFO] Dashboard en http://0.0.0.0:8080
# [INFO] Sistema listo. Esperando señales...
```

### Parar (shutdown graceful)

**Linux / macOS:**
```bash
# Ctrl+C en la terminal, o enviar señal:
kill -SIGTERM <pid>
```

**Windows:**
```
# Solo Ctrl+C en la terminal (SIGTERM no existe en Windows)
# El sistema captura Ctrl+C y realiza el apagado limpio igualmente
```

El sistema completa el ciclo en curso, cierra conexiones y apaga limpiamente.
**Los trades abiertos en Binance con sus TP/SL siguen vigentes** — el sistema
no cierra posiciones al apagarse.

### Ejecutar como servicio en Linux (systemd)

```ini
# /etc/systemd/system/gestiona_trades.service
[Unit]
Description=gestiona_trades — Binance Futures trading bot
After=network.target

[Service]
Type=simple
User=tu_usuario
WorkingDirectory=/ruta/a/gestiona_trades
ExecStart=/ruta/a/gestiona_trades/.venv/bin/python gestiona_trades.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable gestiona_trades
sudo systemctl start gestiona_trades
sudo systemctl status gestiona_trades
journalctl -u gestiona_trades -f
```

### Ejecutar como servicio en Windows (Task Scheduler)

Opción 1 — **Programador de tareas** (sin software adicional):

1. Abrir "Programador de tareas" → "Crear tarea básica"
2. Desencadenador: "Al iniciar el sistema"
3. Acción: Iniciar programa
   - Programa: `C:\ruta\a\gestiona_trades\.venv\Scripts\python.exe`
   - Argumentos: `gestiona_trades.py`
   - Iniciar en: `C:\ruta\a\gestiona_trades`
4. Marcar "Ejecutar tanto si el usuario inició sesión como si no"

Opción 2 — **NSSM** (Non-Sucking Service Manager, recomendado):

```powershell
# Instalar NSSM (https://nssm.cc)
# Luego, desde PowerShell como administrador:
nssm install gestiona_trades "C:\ruta\a\gestiona_trades\.venv\Scripts\python.exe"
nssm set gestiona_trades AppParameters gestiona_trades.py
nssm set gestiona_trades AppDirectory "C:\ruta\a\gestiona_trades"
nssm set gestiona_trades AppStdout "C:\ruta\a\gestiona_trades\logs\service.log"
nssm set gestiona_trades AppStderr "C:\ruta\a\gestiona_trades\logs\service.log"
nssm start gestiona_trades
```

---

## 13. Casos de uso habituales

### Iniciar solo en testnet (sin dinero real)

```yaml
binance:
  base_url: "https://testnet.binancefuture.com"
  api_key:  "TU_TESTNET_KEY"
  api_secret: "TU_TESTNET_SECRET"

strategy:
  capital_per_trade: 10
  max_open_trades:   3
  leverage:          1
```

### Ser más selectivo con las señales

```yaml
strategy:
  top_n:              1          # solo el rank 1
  min_momentum_pct:   5          # solo pares con caída >5% en 1h
  min_vol_ratio:      2.0        # volumen al menos 2x la media
  allowed_quintiles:  [1, 2]     # solo los dos quintiles más bajistas
  max_trades_per_pair: 1         # un solo trade por par a la vez
```

### Gestión de riesgo conservadora

```yaml
strategy:
  capital_per_trade:  20         # 20 USDT por trade
  max_open_trades:    5          # máximo 5 simultáneos = 100 USDT en riesgo
  tp_pct:             10         # TP ajustado
  sl_pct:             5          # SL ajustado (stop ajustado)
  trigger_offset_pct: 5
  timeout_hours:      12
```

### Ver solo trades con PnL positivo en los últimos 7 días

```bash
sqlite3 data/trades.db "
  SELECT pair, exit_type, pnl_usdt, pnl_pct, exit_fill_ts
  FROM trades
  WHERE status='closed'
    AND pnl_usdt > 0
    AND exit_fill_ts >= datetime('now', '-7 days')
  ORDER BY pnl_usdt DESC;"
```

---

## 14. Errores comunes

### `ModuleNotFoundError: No module named 'yaml'` (u otro módulo)
El venv fue recreado con `python -m venv .venv` sin reinstalar las dependencias,
o bien se está ejecutando el script fuera del venv activo.

```powershell
# Verificar que el venv está activo (debe aparecer (.venv) en el prompt)
.venv\Scripts\Activate.ps1

# Reinstalar dependencias
pip install -r requirements.txt
```

**Nunca volver a ejecutar `python -m venv .venv`** a menos que quieras empezar
de cero — borra todos los paquetes instalados.

### `Config no encontrada: config.yaml`
No existe `config.yaml`. Ejecutar:
```bash
cp config.yaml.example config.yaml
# editar con las API keys
```

### `config.yaml falta: binance.api_key`
El campo está vacío o tiene el valor placeholder. Rellenar con la key real.

### Error al verificar credenciales Binance (arranque)
- Verificar que la API key tiene permisos de **Futures**.
- Verificar que la IP está en la whitelist de la API key.
- Si es testnet, asegurarse de usar keys de testnet con `base_url` de testnet.

### `BinanceError -4046: No need to change margin type`
Normal. Significa que el par ya estaba en modo ISOLATED. El sistema lo ignora.

### `BinanceError -4120` al colocar TP/SL
Ciertas cuentas Binance están migradas al servicio Algo, donde los tipos
condicionales (`STOP_MARKET`, `TAKE_PROFIT_MARKET`, `STOP`, `TAKE_PROFIT`) dan
error `-4120` en `/fapi/v1/order`.

El sistema ya incluye una implementación compatible con estas cuentas:

- **TP**: `BUY LIMIT GTC reduceOnly` — orden pasiva en el libro al precio
  objetivo. Nunca lanza `-4120` porque no es un tipo condicional.
- **SL**: Monitor externo de mark price + `BUY MARKET` al dispararse — no
  coloca ninguna orden condicional en Binance.

Si aparece `-4120` en los logs, verificar que no se está usando una versión
antigua del código que intentaba colocar órdenes `STOP_MARKET` o
`TAKE_PROFIT_MARKET`. La versión actual no debería ver este error.

### Orden de entrada rechazada (error BBO inesperado)
Con el modo BBO el error `-5022` (post-only rechazada) no puede ocurrir porque
Binance calcula el precio en el servidor garantizando que la orden es siempre
maker. Si aparece un `BinanceError` durante la apertura, verificar:
- Que el par tiene liquidez suficiente (`priceMatch=OPPONENT_5` requiere que
  existan al menos 5 niveles de bid en el book).
- Que el `quantity` resultante supera el `minNotional` del par.

### La curva de equity del dashboard no muestra datos
El `GET /api/trades` devuelve datos pero el dashboard necesita trades con
`status=closed` y `exit_fill_ts` válido. Esperar a que se cierre el primer trade.

### SignalWatcher no detecta señales nuevas
- Verificar que `signals.file_path` apunta al CSV correcto.
- Verificar que las filas tienen `leido=no`.
- Verificar que `fecha_hora` del CSV está en formato `YYYY/MM/DD HH:MM:SS`
  y que la señal no tiene más de `max_signal_age_minutes` de antigüedad.

### El sistema arranca pero no abre trades
Posibles causas (ver log):
1. `max_open_trades` ya alcanzado
2. `max_trades_per_pair` ya alcanzado para ese par
3. Señal descartada por filtros (`min_momentum_pct`, `allowed_quintiles`, etc.)
4. Chase loop agotado sin fill (`NOT_EXECUTED`)
5. Balance insuficiente en Binance para `capital_per_trade`

---

### Errores específicos de Windows

#### `NotImplementedError` al arrancar (versiones antiguas)
Si aparece este error relacionado con señales del sistema operativo, asegúrate
de usar Python 3.11+ y la versión más reciente del script (ya incluye el
fallback automático para Windows).

#### `aiohttp` falla con `RuntimeError: Event loop is closed` o errores de conector
Síntoma: el sistema arranca pero las llamadas HTTP a Binance fallan.
Causa: Python en Windows usa `ProactorEventLoop` por defecto, incompatible con `aiohttp`.
Solución: el script ya aplica `WindowsSelectorEventLoopPolicy` automáticamente.
Si el error persiste, verificar que se está ejecutando con Python 3.11+ y no
con una versión antigua donde este fix no estaba presente.

#### `tail -f logs/...` no funciona en Windows
El comando `tail` no existe en Windows. Alternativas:
```powershell
# PowerShell — equivalente a tail -f
Get-Content logs\gestiona_trades.log -Wait -Tail 50

# O instalar Git for Windows y usar su bash
tail -f logs/gestiona_trades.log
```

#### `sqlite3` no disponible en la línea de comandos de Windows
Para ejecutar consultas SQLite en Windows, instalar el cliente oficial:
- Descargar desde https://www.sqlite.org/download.html → `sqlite-tools-win64`
- O usar cualquier GUI como DB Browser for SQLite (https://sqlitebrowser.org)
