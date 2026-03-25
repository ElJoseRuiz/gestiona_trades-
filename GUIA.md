# Guía de uso de `gestiona_trades`

`gestiona_trades` es un bot de trading para Binance Futures orientado a operativa `short`.
Lee señales desde un CSV externo, abre y gestiona trades reales o paper, guarda todo en SQLite
y expone un dashboard web para seguimiento en tiempo real.

Esta guía está actualizada al comportamiento real del código actual del repositorio.

---

## 1. Qué hace el sistema

Flujo resumido:

1. Un proceso externo genera señales en `fut_pares_short.csv`.
2. `gestiona_trades.py` vigila ese CSV.
3. Si la señal pasa los filtros, el sistema:
   - abre un trade real en Binance, o
   - abre un trade paper interno si `paper_trading: true`.
4. El trade se gestiona con TP, SL, timeout y cierres manuales.
5. Todo queda persistido en:
   - `data/trades.db`
   - `logs/gestiona_trades.log`
6. El dashboard se sirve en `http://localhost:8080`.

---

## 2. Requisitos

- Python 3.11 o superior
- Cuenta Binance Futures con API habilitada si vas a operar en real
- Un CSV de señales compatible
- `config.yaml` válido

Archivos clave:

- `config.yaml`: credenciales, rutas y parámetros base
- `estrategia/filtros_gestiona_trades.yaml`: perfil exportado por el dashboard de optimización
- `fut_pares_short.csv`: señales entrantes

---

## 3. Instalación en Windows

### 3.1. Crear entorno virtual

Si `python` no está en el `PATH`, usa `py -3` en su lugar.

```powershell
cd C:\ruta\al\repo\gestiona_trades
python -m venv .venv
```

### 3.2. Activar el entorno

Si PowerShell bloquea la activación, habilita scripts locales una vez:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Después:

```powershell
.venv\Scripts\Activate.ps1
```

### 3.3. Instalar dependencias

```powershell
pip install -r requirements.txt
```

### 3.4. Crear la configuración

```powershell
Copy-Item config.yaml.example config.yaml
```

Edita `config.yaml` con:

- `binance.api_key`
- `binance.api_secret`
- `signals.file_path`
- parámetros de estrategia

### 3.5. Arrancar

```powershell
python gestiona_trades.py
```

### 3.6. Ver logs en tiempo real

```powershell
Get-Content logs\gestiona_trades.log -Wait -Tail 50
```

---

## 4. Instalación en Linux

En Debian, Ubuntu y derivadas, si aún no tienes `venv`, instala primero:

```bash
sudo apt update
sudo apt install -y python3 python3-venv
```

### 4.1. Crear entorno virtual

```bash
cd /ruta/al/repo/gestiona_trades
python3 -m venv .venv
```

### 4.2. Activar el entorno

```bash
source .venv/bin/activate
```

### 4.3. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 4.4. Crear la configuración

```bash
cp config.yaml.example config.yaml
```

Edita `config.yaml` y después arranca:

```bash
python gestiona_trades.py
```

### 4.5. Ver logs en tiempo real

```bash
tail -f logs/gestiona_trades.log
```

Si quieres dejar el proceso corriendo aunque cierres la terminal, puedes usar por ejemplo:

```bash
nohup .venv/bin/python gestiona_trades.py > logs/nohup.out 2>&1 &
```

---

## 5. Puesta en marcha rápida

### 5.1. Configuración mínima

```yaml
binance:
  api_key: "TU_API_KEY"
  api_secret: "TU_API_SECRET"
  base_url: "https://fapi.binance.com"

strategy:
  mode: "short"
  capital_per_trade: 10
  max_open_trades: 5
  max_trades_per_pair: 1
  tp_pct: 10
  sl_pct: 30
  timeout_hours: 24
  paper_trading: true

signals:
  file_path: "fut_pares_short.csv"
```

### 5.2. Arranque recomendado

Primera prueba:

1. activa `paper_trading: true`
2. confirma que el CSV de señales existe
3. arranca el bot
4. abre:
   - `http://localhost:8080`
   - `http://localhost:8080/history`

### 5.3. Qué deberías ver al arrancar

En el log:

- base de datos inicializada
- balance consultado
- listen key / WebSocket conectado
- dashboard levantado
- sistema listo

En el dashboard:

- chips de estado
- trades abiertos y cerrados
- eventos en vivo

---

## 6. Precedencia de configuración

Regla vigente:

1. `estrategia/filtros_gestiona_trades.yaml`
2. `config.yaml`
3. defaults internos

Pero esto solo aplica a los parámetros que el perfil exportado define y que el bot soporta.

### 6.1. Qué sigue siendo solo de `config.yaml`

Ejemplos:

- `binance.api_key`
- `binance.api_secret`
- `binance.base_url`
- `dashboard.host`
- `dashboard.port`
- `logging.*`
- `database.path`
- `signals.file_path`
- `paper_tp_sl_price_mode`

### 6.2. Qué puede sobrescribir el YAML exportado

Parámetros ya soportados:

- `ejecucion.mode`
- `gestion_trade.tp_pct`
- `gestion_trade.sl_pct`
- `gestion_trade.tp_pos`
- `gestion_trade.sl_pos`
- `gestion_trade.min_tp_posicion`
- `gestion_trade.max_hold`
- `limites.max_par`
- `limites.max_global`
- filtros de entrada (`rank`, `momentum`, `vol_ratio`, `trades_ratio`, `bp`, `categorias`, `quintiles`, `ignore_n`, `ignore_h`, etc.)

Si un campo existe en el YAML exportado pero viene `null`, el bot cae a `config.yaml`.

---

## 7. Parámetros de estrategia y su significado real

Esta es la parte importante: no solo qué significa cada campo “en teoría”, sino cómo se comporta de verdad hoy en el código.

### 7.1. Núcleo de estrategia

| Parámetro | Dónde se define | Qué hace realmente |
|---|---|---|
| `mode` | `config.yaml` o perfil | Solo `short` está soportado operativamente. |
| `capital_per_trade` | `config.yaml` | Capital nominal por trade para calcular la cantidad. |
| `leverage` | `config.yaml` | Apalancamiento usado al dimensionar la posición real. |
| `max_open_trades` | `config.yaml` o perfil | Límite global de trades reales abiertos. Si ya está alcanzado, se descartan señales reales nuevas. En paper no actúa hoy como tope efectivo. |
| `max_trades_per_pair` | `config.yaml` o perfil | Límite de trades reales simultáneos por par. Si ya está alcanzado, se descartan señales reales nuevas para ese par. En paper no actúa hoy como tope efectivo. |
| `tp_pct` | `config.yaml` o perfil | Distancia porcentual del take profit respecto a la entrada. En `short`, el TP está por debajo del precio de entrada. |
| `sl_pct` | `config.yaml` o perfil | Distancia porcentual del stop loss respecto a la entrada. En `short`, el SL está por encima del precio de entrada. |
| `timeout_hours` | `config.yaml` o perfil (`max_hold`) | Cierra el trade por tiempo si sigue abierto pasado ese límite. |
| `quarantine_hours` | `config.yaml` | Tras un cierre, impide reabrir ese mismo par durante X horas si el motor detecta que el último cierre está dentro de esa cuarentena. Se usa tanto en real como en paper. |

### 7.2. Cascadas por posición

| Parámetro | Dónde se define | Qué hace realmente |
|---|---|---|
| `TP_posicion` / `tp_pos` | `config.yaml` o perfil | Si un trade toca TP, puede cerrar también trades hermanos del mismo par. |
| `SL_posicion` / `sl_pos` | `config.yaml` o perfil | Si un trade toca SL, puede cerrar también trades hermanos del mismo par. |
| `Min_TP_posicion` / `min_tp_posicion` | `config.yaml` o perfil | Antes de disparar la cascada TP, exige un PnL combinado mínimo en los trades hermanos. No bloquea el TP del trade disparador; solo la cascada sobre los demás. Se aplica hoy tanto en real como en paper. |

### 7.3. Modos operativos

| Parámetro | Qué hace realmente |
|---|---|
| `solo_cerrando_trades` | No abre trades reales nuevos. El bot solo reconcilia y cierra los ya existentes. No convierte automáticamente las nuevas señales en paper. |
| `paper_trading` | Las señales nuevas van a paper. Además, el motor real pasa a modo solo-cerrando para no abrir posiciones reales nuevas mientras paper está activo. |
| `paper_tp_sl_price_mode` | Define cómo se evalúan TP/SL en paper con vela de 5m. `close_5m` usa el cierre. `high_low_5m` usa `low` para TP y `high` para SL. |

### 7.4. Filtros de entrada del perfil exportado

Estos campos suelen venir de `estrategia/filtros_gestiona_trades.yaml` y afectan a qué señales se aceptan:

| Parámetro | Qué hace realmente |
|---|---|
| `rank_min`, `rank_max` | Limita el rango de ranking/top admitido. |
| `momentum_min`, `momentum_max` | Filtra por momentum de la señal. |
| `vol_ratio_min`, `vol_ratio_max` | Filtra por ratio de volumen. |
| `trades_ratio_min`, `trades_ratio_max` | Filtra por ratio de número de trades. |
| `bp_min`, `bp_max` | Filtra por el campo `bp` si la señal lo trae. |
| `quintiles` | Solo acepta señales de los quintiles indicados. |
| `categorias` | Solo acepta señales de las categorías indicadas. |
| `filtro_overlap` | Activa el rechazo por overlap cuando el perfil lo exige. |
| `ignore_n` | Ignora las primeras `N` señales de un par sin trade abierto. Solo deja abrir a partir de la `N + 1`. |
| `ignore_h` | Ventana temporal en horas para el contador de `ignore_n`. Si expira, el ciclo se reinicia. |
| `hora_min`, `hora_max` | Restringe la entrada a ciertas horas UTC si vienen informadas. |
| `dias_semana` | Restringe la entrada a ciertos días de la semana UTC si vienen informados. |

### 7.5. Filtros legacy en `strategy`

Estos campos siguen existiendo por compatibilidad, pero si el perfil exportado trae el equivalente, manda el perfil:

| Campo legacy | Equivalente del perfil |
|---|---|
| `min_momentum_pct` | `filtros_entrada.momentum_min` |
| `min_vol_ratio` | `filtros_entrada.vol_ratio_min` |
| `min_trades_ratio` | `filtros_entrada.trades_ratio_min` |
| `allowed_quintiles` | `filtros_entrada.quintiles` |

### 7.6. Campos presentes pero hoy no operativos

| Parámetro | Estado actual |
|---|---|
| `trigger_offset_pct` | Se conserva por compatibilidad histórica, pero hoy no afecta a la lógica real. |
| algunos campos avanzados de `macro_btc` | Se parsean y se auditan, pero no están plenamente operativos en el motor actual. |
| `global_tp`, `global_tp_win`, `kill_switch_pf` | Se parsean, pero requieren soporte adicional para tener efecto completo. |

---

## 8. TP/SL real vs paper

### 8.1. Real

En real:

- la entrada se ejecuta en Binance
- el TP/SL se resuelve según la lógica del motor real y las órdenes/ejecuciones reales
- los cierres se reconcilian desde Binance si hace falta al arrancar

### 8.2. Paper

En paper:

- la entrada se abre al precio de mercado consultado por el bot
- el chequeo de TP/SL se hace con la última vela cerrada de `5m`
- la comprobación se alinea al cierre real de esa vela más un pequeño margen
- el modo exacto depende de `paper_tp_sl_price_mode`

Modos:

| Valor | Comportamiento |
|---|---|
| `close_5m` | dispara y cierra con el `close` de la vela cerrada de 5m |
| `high_low_5m` | TP usa `low`; SL usa `high` de la vela cerrada de 5m |

---

## 9. Señales y filtros

El bot espera un CSV compatible con columnas equivalentes a:

```csv
fecha_hora,par,rank,close,mom_pct,vol_ratio,trades_ratio,quintil,bp,categoria,leido
```

Si un filtro activo necesita un campo y la señal no lo trae, la señal debe rechazarse.

Filtros habituales que hoy sí se usan:

- `rank_min`, `rank_max`
- `momentum_min`, `momentum_max`
- `vol_ratio_min`, `vol_ratio_max`
- `trades_ratio_min`, `trades_ratio_max`
- `bp_min`, `bp_max`
- `categorias`
- `quintiles`
- `hora_min`, `hora_max`
- `dias_semana`
- `filtro_overlap`
- `ignore_n`, `ignore_h`

---

## 10. Dashboard y analítica

### 10.1. Dashboard live

URL:

```text
http://localhost:8080
```

Incluye:

- estado general
- chips de configuración efectiva
- trades abiertos
- últimos trades cerrados
- eventos en vivo

### 10.2. Panel histórico

URL:

```text
http://localhost:8080/history
```

Permite ver:

- KPIs históricos
- curva de rendimiento
- drawdown
- concurrencia
- análisis por quintil, momentum, categoría y activo
- selector por fuente: `Real`, `Paper` o `Ambos`

---

## 11. Base de datos

Ruta por defecto:

```text
data/trades.db
```

Tablas principales:

- `trades`: histórico real
- `paper_trades`: histórico paper
- `events`: eventos y auditoría

Consultas útiles:

```bash
sqlite3 data/trades.db "SELECT status, COUNT(*) FROM trades GROUP BY status;"
sqlite3 data/trades.db "SELECT status, COUNT(*) FROM paper_trades GROUP BY status;"
sqlite3 data/trades.db "SELECT event_type, COUNT(*) FROM events GROUP BY event_type;"
```

---

## 12. Logs

Ruta por defecto:

```text
logs/gestiona_trades.log
```

Windows:

```powershell
Get-Content logs\gestiona_trades.log -Wait -Tail 50
```

Linux:

```bash
tail -f logs/gestiona_trades.log
```

---

## 13. Arranque y parada

### 13.1. Arrancar

Windows:

```powershell
.venv\Scripts\Activate.ps1
python gestiona_trades.py
```

Linux:

```bash
source .venv/bin/activate
python gestiona_trades.py
```

También puedes usar una ruta de configuración explícita:

```bash
python gestiona_trades.py --config /ruta/a/config.yaml
```

### 13.2. Parar

En ambos sistemas, la forma habitual es:

```text
Ctrl + C
```

El bot intenta apagar tareas de forma ordenada. En real, las órdenes y posiciones ya existentes en Binance no desaparecen por parar el proceso.

---

## 14. Errores comunes

### `Config no encontrada: config.yaml`

Crea el fichero:

```bash
cp config.yaml.example config.yaml
```

En Windows:

```powershell
Copy-Item config.yaml.example config.yaml
```

### `ModuleNotFoundError`

Suele indicar:

- entorno virtual no activado
- dependencias no instaladas
- entorno recreado sin reinstalar paquetes

### No llegan señales

Revisa:

- `signals.file_path`
- que el CSV exista
- que la señal no esté demasiado antigua
- que el filtro activo no la descarte

### El dashboard no refleja lo último

Revisa:

- si el backend que corre es el mismo código que acabas de editar
- si el navegador tiene HTML/JS cacheado
- si la API `/api/status`, `/api/trades` o `/api/history_data` devuelve datos actualizados

### Inconsistencias tras tocar la base de datos a mano

Si se borran trades abiertos directamente de SQLite mientras el bot sigue vivo, el proceso puede seguir teniéndolos en memoria. En ese caso, conviene reiniciar `gestiona_trades.py`.

---

## 15. Recomendación de uso

Para validar cambios del bot:

1. usa primero `paper_trading: true`
2. revisa `control_mision`
3. revisa `history`
4. confirma en `logs/gestiona_trades.log`
5. solo después mueve la operativa a real

---

## 16. Resumen operativo

Si solo quieres dejarlo corriendo con el mínimo viable:

1. crea `.venv`
2. instala `requirements.txt`
3. crea `config.yaml`
4. apunta `signals.file_path` al CSV correcto
5. arranca con `paper_trading: true`
6. valida dashboard, logs y DB
7. ajusta estrategia y solo después pasa a real
