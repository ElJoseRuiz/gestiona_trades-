# AGENTS.md — Guía para Asistentes de IA

Este archivo describe la estructura del código, flujos de trabajo y convenciones del proyecto **gestiona_trades** para que los asistentes de IA puedan colaborar de manera efectiva.

---

## Contexto obligatorio al inicio de cada hilo

Siempre hay que leer al comienzo del hilo este documento externo:

- `C:\Users\portatil\Dropbox\Claude\backtest1\crypto-backtest\INTEGRACION_GESTIONA_TRADES.md`

Objetivo de esta regla:

- alinear desde el primer mensaje el contrato real que exporta hoy `backtest1`
- evitar trabajar con la propuesta antigua basada en `strategy_profile.yaml`
- evitar desajustes con `dashboard_top5.html`, la taxonomia de categorias, la semantica de `ignore_n` y `ignore_h`, `btc_reverso`, `global_tp_win` y la prioridad entre `filtros_gestiona_trades.yaml` y `config.yaml`

Regla de precedencia para temas de integracion:

1. `C:\Users\portatil\Dropbox\Claude\backtest1\crypto-backtest\INTEGRACION_GESTIONA_TRADES.md`
2. Este `AGENTS.md`
3. El resto de documentos generales del repositorio

Si hay discrepancias entre este `AGENTS.md` y el documento externo de integracion, para todo lo relacionado con el contrato dashboard -> YAML -> bot debe prevalecer `INTEGRACION_GESTIONA_TRADES.md`.

## Descripción General del Proyecto

**gestiona_trades** es un bot de trading automático para **Binance Futures (posiciones SHORT)** escrito en Python asíncrono (~3.150 líneas).
Lee señales de trading desde un archivo CSV generado por `selecciona_pares.py`, abre órdenes de entrada con lógica BBO (Best Bid Offer), gestiona TP/SL mediante Algo Orders de Binance, y expone un dashboard web en tiempo real.

---

## Estructura del Repositorio

```
gestiona_trades/
├── gestiona_trades.py           # Punto de entrada principal (clase App)
├── requirements.txt             # Dependencias Python
├── config.example.yaml          # Plantilla de configuración (copiar a config.yaml)
├── GUIA.md                      # Documentación completa en español
├── estrategia/
│   └── filtros_gestiona_trades.yaml   # Perfil exportado por el dashboard de optimizacion
├── .gitignore
│
├── src/                         # Módulos principales
│   ├── __init__.py
│   ├── models.py                # Dataclasses, Enums (TradeStatus, ExitType, etc.)
│   ├── config.py                # Carga y validación de config.yaml y filtros_gestiona_trades.yaml
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

**Fichero funcional adicional del flujo de optimizacion:**
- `estrategia/filtros_gestiona_trades.yaml` — perfil de reglas exportado desde el dashboard de optimizacion; complementa a `config.yaml`

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
cp config.example.yaml config.yaml
# Editar config.yaml con API keys y parámetros de estrategia

# Ejecutar
python gestiona_trades.py
python gestiona_trades.py --config /ruta/a/config.yaml
```

---

## Configuración (`config.yaml`)

El archivo `config.yaml` **nunca se debe commitear** (está en `.gitignore`). Usar `config.example.yaml` como plantilla.

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

### Perfil de estrategia exportado por el dashboard

Ademas de `config.yaml`, el bot debe contemplar un perfil externo exportado desde el dashboard de optimizacion:

- Ruta de referencia actual: `./estrategia/filtros_gestiona_trades.yaml`
- Nombre esperado del fichero exportado: `filtros_gestiona_trades.yaml`
- Origen esperado actual: `dashboard_top5.html`
- Uso: aplicar reglas optimizadas de entrada y gestion del trade sin sustituir la configuracion base del bot

Separacion de responsabilidades:

- `config.yaml`: credenciales, rutas, base de datos, logging y parametros base de infraestructura
- `filtros_gestiona_trades.yaml`: reglas optimizadas de filtrado, gestion del trade, contexto de mercado y overlays del dashboard

Orden de prioridad recomendado:

1. `estrategia/filtros_gestiona_trades.yaml`
2. `config.yaml`
3. defaults actuales del bot

Reglas de compatibilidad:

- Si el fichero de filtros no existe, el bot debe seguir funcionando con `config.yaml`.
- Las claves desconocidas del YAML deben ignorarse de forma tolerante.
- `null` significa "desactivado", "sin limite" o "no aplicar" segun el campo.
- Si `schema_version` es superior a la soportada, loggear warning y seguir en modo tolerante si es posible.

### Estructura esperada de `filtros_gestiona_trades.yaml`

Secciones principales:

- `schema_version`
- `exportado_utc`
- `origen`
- `ejecucion`
- `filtros_entrada`
- `gestion_trade`
- `limites`
- `macro_btc`
- `kill_switch_pf`

Mapeo operativo recomendado:

- `ejecucion.mode` sobrescribe el modo operativo. Si llega `long` y el bot solo soporta `short`, rechazarlo con log explicito.
- `gestion_trade.sl_pct` y `gestion_trade.tp_pct` sobrescriben `strategy.sl_pct` y `strategy.tp_pct`.
- `gestion_trade.max_hold` sobrescribe `strategy.timeout_hours`.
- `limites.max_par` sobrescribe `strategy.max_trades_per_pair`.
- `limites.max_global` sobrescribe `strategy.max_open_trades`.

Campos de `filtros_entrada` a priorizar en primera fase:

- `rank_min`, `rank_max`
- `momentum_min`, `momentum_max`
- `vol_ratio_min`, `vol_ratio_max`
- `trades_ratio_min`, `trades_ratio_max`
- `bp_min`, `bp_max`
- `quintiles`
- `categorias`
- `filtro_excluir_overlap`
- `gestion_trade.sl_pos`, `gestion_trade.tp_pos`
- `gestion_trade.global_tp`, `gestion_trade.global_tp_win`

Campos avanzados que deben parsearse aunque aun no se activen:

- `hora_min`, `hora_max`
- `dias_semana`
- `ignore_n`, `ignore_h`
- `macro_btc.*`
- `kill_switch_pf.*`



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
- **Separacion de responsabilidades**: el dashboard optimiza y exporta reglas; `gestiona_trades.py` valida y ejecuta; el emisor externo solo propone señales

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

### Contrato de señales externas

Para que `gestiona_trades.py` pueda aplicar correctamente el perfil exportado, la señal entrante deberia incluir como minimo:

- `par`
- `rank` o `top` que son equivalentes
- `timestamp` UTC o equivalente (`signal_ts` o `fecha_hora`)
- `momentum` o `mom_pct`
- `vol_ratio`
- `trades_ratio`
- `categoria` o `category` si el filtro de categorias esta activo
- `quintile` o `quintil` si el filtro de quintiles esta activo
- `bp` si el filtro de `bp` esta activo
- `price`

Campos recomendados adicionales:

- `hour_utc`
- `dow_utc`
- `source`

Politica recomendada:

- si falta un campo requerido por un filtro activo, rechazar la señal y loggear el motivo
- normalizar nombres equivalentes (`categoria`/`category`, `quintil`/`quintile`, `momentum`/`mom_pct`, `rank`/`top`) en una capa de adaptacion

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
    # 1. Cargar config.yaml , filtros_gestiona_trades.yaml, y configurar logging
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
- `src/signal_watcher.py` — cualquier ampliacion de filtros debe preservar compatibilidad con señales actuales y registrar claramente los rechazos
- futura capa de perfil (`src/filtros_gestiona_trades.py` o `src/strategy_profile.py`) — debe ser tolerante, auditable y no romper el arranque si el fichero falta

## Convenciones de Codigo

- Idioma de comentarios y mensajes: español.
- Mantener todos los archivos de texto en UTF-8 limpio y sin mojibake; si aparece texto mal codificado, corregirlo antes de dar la tarea por cerrada.
- Todas las copias de seguridad, incluidas las de `AGENTS.md` y cualquier otro backup auxiliar, deben guardarse dentro de `./backups/` y no en la raíz del repositorio.
- Cada programa o interfaz visible sigue su propia versión independiente.
- A partir de esta convención, todos los programas, interfaces y componentes versionados del proyecto pasan a arrancar desde `1.00`, aunque antes llevaran otra serie.
- Desde ese reinicio común en `1.00`, cada cambio posterior debe seguir subiendo en `+0.01`.
- Cada cambio de código en un programa debe incrementar su propia versión en `+0.01`.
- Flujo obligatorio para cambios de código: 1) identificar la versión actual del componente, 2) crear backup de esa versión en `./backups/`, 3) aplicar el cambio, 4) subir la versión del propio componente en `+0.01` antes de cerrar la tarea.
- No dejar cambios de código cerrados con una versión antigua o sin actualizar respecto a la modificación realmente hecha.
- El formato de los backups pasa a ser `nombre.version.tipo` dentro de `./backups/`.
- Antes de modificar `gestiona_trades.py`, crear siempre una copia de seguridad en `./backups/` con formato `gestiona_trades.<version>.py`.
- No hacer cambios en `gestiona_trades.py` sin haber creado esa copia previa en la misma sesion de trabajo.
- Antes de modificar `./static/control_mision.html`, crear siempre una copia de seguridad en `./backups/` con formato `control_mision.<version>.html`.
- No hacer cambios en `./static/control_mision.html` sin haber creado esa copia previa en la misma sesion de trabajo.
- Si se hace backup de documentacion u otros archivos auxiliares, usar el mismo patron: `nombre.<version>.<tipo>`.

### Convenciones especificas para la integracion con el dashboard

- No hacer que el bot lea directamente `dashboard_top5.html`.
- No acoplar el bot a `master_signals.csv` ni a CSVs de analisis como fuente de trading live.
- Mantener repos y responsabilidades separadas: el dashboard optimiza; el bot ejecuta.
- Al implementar esta integracion, preferir un cargador dedicado como `src/filtros_gestiona_trades.py`.
- El cargador debe validar, normalizar y mapear overrides sin romper la compatibilidad actual.
- Implementar primero los overrides simples y filtros de entrada basicos; dejar `macro_btc`, `global_tp` y `kill_switch_pf` para una segunda fase si aun no existen en el bot.
- No inventar logicas opacas para campos avanzados no soportados: parsearlos, documentarlos y dejar log claro si quedan inactivos.
- La semantica de `ignore_n` y `ignore_h` debe seguir la del dashboard:
  - `ignore_n = N` ignora las primeras `N` señales de un par cuando no tiene trade abierto y solo permite abrir desde la `N + 1`.
  - el contador se resetea tras abrir el trade.
  - `ignore_h` define la ventana temporal del contador; si expira, el ciclo vuelve a empezar.
- La semantica de `btc_reverso` solo tiene efecto cuando hay filtro BTC activo; no debe invertirse la direccion sin ese contexto.
- Si se implementa `global_tp_win`, al alcanzar el umbral de `global_tp` deben cerrarse solo los trades en beneficio cuando el flag sea `true`.

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
- **Ramas de desarrollo**: `Codex/*` (creadas automáticamente por asistentes de IA)

Para contribuir, crear una rama desde `master`, desarrollar los cambios y abrir un PR.



