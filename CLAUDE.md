# CLAUDE.md — Guía para Asistentes de IA

Este archivo describe la estructura del código, flujos de trabajo y convenciones del proyecto **gestiona_trades** para que los asistentes de IA puedan colaborar de manera efectiva.

---

## Descripción General del Proyecto

**gestiona_trades** es un bot de trading automático para **Binance Futures (posiciones SHORT)** escrito en Python asíncrono (~3.150 líneas). Lee señales de trading desde un archivo CSV generado por `selecciona_pares.py`, abre órdenes de entrada con lógica BBO (Best Bid Offer), gestiona TP/SL mediante Algo Orders de Binance, y expone un dashboard web en tiempo real.

---

## Estructura del Repositorio

```
gestiona_trades/
├── gestiona_trades.py           # Punto de entrada principal (clase App)
├── requirements.txt             # Dependencias Python
├── config.yaml.example          # Plantilla de configuración (copiar a config.yaml)
├── GUIA.md                      # Documentación completa en español
├── .gitignore
│
├── src/                         # Módulos principales
│   ├── __init__.py
│   ├── models.py                # Dataclasses, Enums (TradeStatus, ExitType, etc.)
│   ├── config.py                # Carga y validación de config.yaml
│   ├── logger.py                # Logger rotativo (archivo + consola)
│   ├── state.py                 # Persistencia SQLite asíncrona (aiosqlite)
│   ├── signal_watcher.py        # Monitor de señales CSV
│   ├── order_manager.py         # Cliente REST Binance Futures
│   ├── ws_manager.py            # WebSocket User Data Stream de Binance
│   ├── trade_engine.py          # Máquina de estados de trades (módulo central)
│   └── dashboard.py             # Servidor web + API REST (aiohttp.web)
│
└── static/
    └── control_mision.html      # Dashboard SPA (HTML/CSS/JS vanilla, tema oscuro)
```

**Directorios generados en tiempo de ejecución (no en git):**
- `logs/` — archivos de log rotativos (10MB × 5 ficheros)
- `data/trades.db` — base de datos SQLite con tablas `trades` y `events`
- `config.yaml` — configuración activa con credenciales (nunca commitear)

---

## Stack Tecnológico

| Componente | Tecnología |
|---|---|
| Lenguaje | Python 3.11+ |
| HTTP cliente/servidor | `aiohttp` ≥3.9.0 |
| Base de datos | `aiosqlite` ≥0.19.0 (SQLite WAL) |
| Configuración | `PyYAML` ≥6.0.1 |
| WebSocket | `websockets` ≥12.0 |
| Frontend | HTML5 + JS ES6+ vanilla (sin frameworks) |
| API externa | Binance Futures REST + WebSocket |

---

## Instalación y Ejecución

```bash
# Crear entorno virtual
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\Activate.ps1    # Windows

# Instalar dependencias
pip install -r requirements.txt

# Copiar y editar la configuración
cp config.yaml.example config.yaml
# Editar config.yaml con API keys y parámetros de estrategia

# Ejecutar
python gestiona_trades.py
python gestiona_trades.py --config /ruta/a/config.yaml
```

---

## Configuración (`config.yaml`)

El archivo `config.yaml` **nunca se debe commitear** (está en `.gitignore`). Usar `config.yaml.example` como plantilla.

### Secciones principales

```yaml
binance:
  api_key: ""         # Clave API con permisos de Futures
  api_secret: ""
  base_url: "https://fapi.binance.com"   # o testnet

strategy:
  mode: "short"             # Solo SHORT implementado actualmente
  capital_per_trade: 10     # USDT por operación
  leverage: 1               # 1–125 (1 = sin apalancamiento)
  max_open_trades: 5
  max_trades_per_pair: 1
  tp_pct: 1.0               # Take profit %
  sl_pct: 2.0               # Stop loss %
  timeout_hours: 24         # Cierre forzado por tiempo
  top_n: 3                  # Procesar sólo las N mejores señales

signals:
  file_path: "fut_pares_short.csv"
  poll_interval_seconds: 60
  max_signal_age_minutes: 5

entry:
  order_type: "BBO"         # BBO (recomendado) o LIMIT_GTX
  chase_interval_seconds: 5
  chase_timeout_seconds: 60
  max_chase_attempts: 10
  market_fallback: true

exit:
  timeout_order_type: "BBO"
  timeout_market_fallback: true

dashboard:
  enabled: true
  host: "0.0.0.0"
  port: 8080

logging:
  level: "DEBUG"            # Nivel para archivo
  console_level: "INFO"     # Nivel para consola

database:
  path: "data/trades.db"
```

---

## Ciclo de Vida de un Trade

```
SIGNAL_RECEIVED → OPENING → OPEN → CLOSING → CLOSED
                                           ↘ ERROR
```

1. **SIGNAL_RECEIVED** — Se lee una señal válida del CSV
2. **OPENING** — Se envía orden de entrada BBO/LIMIT_GTX; se espera fill
3. **OPEN** — Fill recibido; se colocan TP (TAKE_PROFIT BBO) y SL (STOP_MARKET) como Algo Orders
4. **CLOSING** — Se detecta fill de TP, SL o timeout; se cancela la orden contraria
5. **CLOSED** — PnL calculado y registrado en DB

Las Algo Orders de Binance **sobreviven reinicios del proceso**. Al arrancar, `App` reconcilia los trades activos en DB con las posiciones abiertas en Binance.

---

## Base de Datos SQLite

Ubicación: `data/trades.db` (WAL mode activado para lecturas concurrentes)

### Tabla `trades`

| Columna | Tipo | Descripción |
|---|---|---|
| `trade_id` | TEXT PK | UUID del trade |
| `pair` | TEXT | Par (ej. BTCUSDT) |
| `status` | TEXT | Estado actual del trade |
| `entry_price` / `entry_quantity` | REAL | Precio y cantidad de entrada |
| `tp_price` / `sl_price` | REAL | Niveles de TP y SL |
| `exit_price` / `exit_type` | REAL/TEXT | Precio de salida y motivo (tp/sl/timeout) |
| `pnl_usdt` / `pnl_pct` | REAL | P&L en USDT y porcentaje |
| `signal_data` | TEXT | JSON con datos de la señal original |
| `created_at` / `updated_at` | TEXT | Timestamps ISO |

### Tabla `events`

Registro de auditoría inmutable. Tipos de evento:
`signal`, `entry_sent`, `entry_fill`, `tp_placed`, `sl_placed`, `tp_fill`, `sl_fill`, `timeout`, `error`, `ws_connect`, `startup`, `shutdown`

---

## API REST del Dashboard

Base URL: `http://localhost:8080`

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Dashboard web (SPA) |
| GET | `/api/status` | Balance, trades abiertos, PnL, uptime |
| GET | `/api/trades` | Últimos 200 trades |
| GET | `/api/trades/{id}` | Detalle de un trade + eventos |
| GET | `/api/events?limit=N` | Últimos N eventos (default 100) |
| GET | `/api/config` | Configuración activa (sin API keys) |
| POST | `/api/trades/{id}/close` | Cierre manual de emergencia |
| GET | `/ws` | WebSocket para eventos en tiempo real |

---

## Patrones y Convenciones de Código

### Arquitectura

- **Totalmente asíncrono**: todo el I/O usa `async/await` (HTTP, WebSocket, SQLite, archivos)
- **Inyección de dependencias**: los componentes reciben sus dependencias por `__init__`
- **Máquina de estados**: `TradeEngine` gestiona transiciones explícitas con persistencia
- **Orientado a eventos**: todos los cambios de estado se registran como eventos y se emiten al dashboard
- **Sin estado global**: todo se pasa explícitamente entre componentes

### Estilo de código

- **Type hints** en todas las firmas de funciones
- **Dataclasses** para modelos de datos (`src/models.py`)
- **Enums** para estados finitos (`TradeStatus`, `ExitType`, `EventType`)
- **Nombres**: mezcla español/inglés (variables de dominio en español, API en inglés)
- **Logging jerárquico**: `gestiona_trades.nombre_modulo`

### Manejo de errores

- `BinanceError` como excepción personalizada para errores de API
- Reintentos con backoff exponencial para errores 429/500
- `try/except` granular — los errores se loguean sin crashear el bot (excepto en inicialización crítica)
- Los trades en estado `ERROR` se persisten en DB para revisión manual

### Acceso a base de datos

- Siempre usar sentencias preparadas (nunca concatenar SQL)
- `INSERT OR REPLACE` para updates idempotentes
- Objetos complejos serializados como JSON en columnas TEXT

### Patrones asyncio

```python
# Tareas en segundo plano
asyncio.create_task(self._loop())

# Sincronización entre tareas
fill_event = asyncio.Event()
await fill_event.wait()

# Limpieza segura en shutdown
await asyncio.gather(*tasks, return_exceptions=True)
```

---

## Flujo de Inicio de la Aplicación

```python
# gestiona_trades.py — clase App
async def run():
    # 1. Cargar config.yaml y configurar logging
    # 2. Inicializar SQLite (StateDB)
    # 3. Conectar REST Binance, verificar credenciales y balance
    # 4. Crear TradeEngine + WSManager
    # 5. Recuperar trades activos de DB y reconciliar con Binance
    # 6. Configurar leverage/margin para pares activos
    # 7. Iniciar WebSocket User Data Stream
    # 8. Iniciar loop de timeout checker
    # 9. Iniciar signal watcher (CSV poller)
    # 10. Iniciar dashboard web
    # 11. Registrar evento STARTUP
    # 12. Esperar SIGINT/SIGTERM → shutdown graceful
```

---

## Notas Importantes para IA

### Lo que NO hacer

- **Nunca** commitear `config.yaml` (contiene credenciales reales)
- **Nunca** usar variables globales — todos los componentes son stateful y se pasan por referencia
- **No** añadir frameworks web externos (el proyecto usa `aiohttp.web` deliberadamente)
- **No** cambiar a código síncrono — toda la aplicación depende de async/await
- **No** usar `time.sleep()` — siempre `await asyncio.sleep()`

### Áreas de alta sensibilidad

- `src/order_manager.py` — llamadas reales a Binance API con dinero real; extrema precaución
- `src/trade_engine.py` — máquina de estados central; los cambios mal hechos pueden causar trades "zombies" o pérdidas
- `src/state.py` — persistencia; mantener backward-compatible el esquema de DB

### No hay tests automatizados

El proyecto no tiene suite de tests. Para validar cambios:
1. Usar testnet de Binance (`https://testnet.binancefuture.com`) con credenciales de prueba
2. Monitorear `logs/gestiona_trades.log` con `tail -f`
3. Verificar el dashboard en `http://localhost:8080`
4. Consultar `data/trades.db` directamente con sqlite3

### Dependencias externas

- El bot requiere que `fut_pares_short.csv` sea generado por `selecciona_pares.py` (proyecto separado)
- Necesita una cuenta de Binance Futures con fondos en USDT y permisos de trading habilitados

---

## Rama de Trabajo

- **Rama principal**: `master`
- **Ramas de desarrollo**: `claude/*` (creadas automáticamente por asistentes de IA)

Para contribuir, crear una rama desde `master`, desarrollar los cambios y abrir un PR.
