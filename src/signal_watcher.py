"""
signal_watcher.py - Vigila fut_pares_short.csv y emite senales.

Manejo del CSV:
  - Encoding utf-8-sig (soporta BOM de Windows)
  - CRLF / LF: csv.reader lo maneja automaticamente con newline=""
  - Headers con espacios: strip() en cada nombre de columna
  - Alias tolerantes de columnas para acompanar la integracion con el dashboard

Logica:
  - No toca el CSV de senales; cada instancia recuerda su propio cursor local
  - Senales con antiguedad > max_signal_age_minutes se descartan
  - Senales validas -> aplica filtros de la config -> emite al trade engine
"""
from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from .config import Config
from .logger import get_logger
from .models import Signal

COMPONENT_VERSION = "0.14"

log = get_logger("signal_watcher")

OnSignalCallback = Callable[[Signal], Awaitable[None]]


class SignalWatcher:
    def __init__(self, cfg: Config, on_signal: OnSignalCallback):
        self._cfg = cfg
        self._on_signal = on_signal
        self._task: Optional[asyncio.Task] = None
        self._last_mtime: float = 0.0
        self._cursor_path = Path(self._cfg.signal_cursor_path)
        self._cursor_state = _empty_cursor_state()
        self._cursor_loaded = False

    async def start(self):
        self._load_cursor_state()
        self._task = asyncio.create_task(self._poll_loop(), name="signal_watcher")
        log.info(
            f"SignalWatcher iniciado -> {self._cfg.signals_file_path} "
            f"(poll cada {self._cfg.poll_interval_seconds}s, modo=cursor_local_compartido)"
        )

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("SignalWatcher detenido")

    async def _poll_loop(self):
        while True:
            try:
                await self._check_file()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"Error en signal_watcher: {exc}", exc_info=True)
            await asyncio.sleep(self._cfg.poll_interval_seconds)

    async def _check_file(self):
        path = Path(self._cfg.signals_file_path)
        if not path.exists():
            return

        mtime = path.stat().st_mtime
        if mtime <= self._last_mtime:
            return
        self._last_mtime = mtime

        try:
            rows = _read_csv(path)
        except Exception as exc:
            log.error(f"Error leyendo CSV {path}: {exc}")
            return

        if self._should_bootstrap_cursor(rows):
            self._bootstrap_cursor(rows)
            self._save_cursor_state()
            return

        signals, cursor_changed = self._read_and_filter(rows)

        if cursor_changed:
            await asyncio.get_event_loop().run_in_executor(None, self._save_cursor_state)

        for sig in signals:
            try:
                await self._on_signal(sig)
            except Exception as exc:
                log.error(f"Error procesando senal {sig.pair}: {exc}", exc_info=True)

    def _read_and_filter(self, rows: List[dict]) -> Tuple[List[Signal], bool]:
        """
        Devuelve (senales_validas, cursor_changed).
        """
        now = datetime.now(timezone.utc)
        signals: List[Signal] = []
        consumed_cursor_rows: List[tuple[datetime, tuple[str, str, str]]] = []

        for row in rows:
            fecha_hora = _get_row_value(row, "fecha_hora", "timestamp", "signal_ts", default="").strip()
            par = _get_row_value(row, "par", "pair", default="").strip()
            rank_raw = _get_row_value(row, "rank", "top", default="").strip()
            key = (fecha_hora, par, rank_raw)

            if not fecha_hora or not par or not rank_raw:
                log.warning(
                    "Senal descartada: falta fecha_hora/par/rank y no se puede "
                    "avanzar el cursor de forma segura"
                )
                continue

            sig_dt = _parse_signal_datetime(fecha_hora)
            if sig_dt is None:
                log.warning(f"Timestamp invalido en senal: '{fecha_hora}'")
                continue

            if not self._is_cursor_candidate(sig_dt, key):
                continue
            consumed_cursor_rows.append((sig_dt, key))

            age_min = (now - sig_dt).total_seconds() / 60
            if age_min > self._cfg.max_signal_age_minutes:
                log.info(
                    f"Senal expirada ({age_min:.1f}min > {self._cfg.max_signal_age_minutes}min): "
                    f"{par} -> timeout"
                )
                continue

            try:
                top = int(float(rank_raw))
            except ValueError:
                log.warning(f"Senal {par} descartada: rank/top invalido '{rank_raw}'")
                continue

            momentum_raw = _get_row_value(row, "momentum", "mom_pct", "mom_1h_pct", default="")

            try:
                sig = Signal(
                    fecha_hora=fecha_hora,
                    pair=par,
                    top=top,
                    close=_parse_opt_float(_get_row_value(row, "close", "price", default="")),
                    mom_1h_pct=_parse_opt_float(momentum_raw),
                    mom_pct=_parse_opt_float(momentum_raw),
                    vol_ratio=_parse_opt_float(_get_row_value(row, "vol_ratio", default="")),
                    trades_ratio=_parse_opt_float(_get_row_value(row, "trades_ratio", default="")),
                    quintil=_parse_opt_int(_get_row_value(row, "quintil", "quintile", default="")),
                    bp=_parse_opt_float(_get_row_value(row, "bp", default="")),
                    categoria=_get_row_value(row, "categoria", "category", default="").strip(),
                    signal_dt=sig_dt,
                )
            except (ValueError, TypeError) as exc:
                log.warning(f"Error parseando senal {par}: {exc}")
                continue

            reject_reason = self._apply_filters(sig)
            if reject_reason:
                log.info(f"Senal {par} descartada ({reject_reason})")
                continue

            log.info(
                f"Senal aceptada: {par} rank={top} cat={sig.categoria or 'N/A'} "
                f"mom={_fmt_pct(sig.mom_pct)} vol={_fmt_metric(sig.vol_ratio)} "
                f"tr={_fmt_metric(sig.trades_ratio)} "
                f"Q{sig.quintil if sig.quintil is not None else 'N/A'} "
                f"bp={_fmt_metric(sig.bp, 4)}"
            )
            signals.append(sig)

        cursor_changed = self._advance_cursor(consumed_cursor_rows)
        return signals, cursor_changed

    def _load_cursor_state(self):
        self._cursor_loaded = True
        if not self._cursor_path.exists():
            self._cursor_state = _empty_cursor_state()
            return

        try:
            payload = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning(f"No se pudo cargar cursor de senales {self._cursor_path}: {exc}")
            self._cursor_state = _empty_cursor_state()
            return

        self._cursor_state = {
            "last_timestamp": payload.get("last_timestamp"),
            "last_keys_at_timestamp": list(payload.get("last_keys_at_timestamp", [])),
        }

    def _save_cursor_state(self):
        self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._cursor_path.with_suffix(f"{self._cursor_path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(self._cursor_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._cursor_path)

    def _should_bootstrap_cursor(self, rows: List[dict]) -> bool:
        if not self._cursor_loaded:
            self._load_cursor_state()
        if not self._cfg.signal_cursor_start_at_end:
            return False
        if self._cursor_state.get("last_timestamp"):
            return False

        latest_dt = _latest_cursor_timestamp(rows)
        if latest_dt is None:
            return False

        age_min = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 60
        return age_min > self._cfg.max_signal_age_minutes

    def _bootstrap_cursor(self, rows: List[dict]):
        latest_dt: Optional[datetime] = None
        latest_keys: set[str] = set()

        for row in rows:
            parsed = _parse_row_cursor_meta(row)
            if parsed is None:
                continue
            sig_dt, key = parsed
            serialized_key = _serialize_signal_key(key)
            if latest_dt is None or sig_dt > latest_dt:
                latest_dt = sig_dt
                latest_keys = {serialized_key}
            elif sig_dt == latest_dt:
                latest_keys.add(serialized_key)

        if latest_dt is None:
            self._cursor_state = _empty_cursor_state()
            log.info(
                "Modo compartido activado sin cursor previo; el CSV no tiene filas validas "
                "para inicializar el cursor."
            )
            return

        self._cursor_state = {
            "last_timestamp": latest_dt.isoformat(),
            "last_keys_at_timestamp": sorted(latest_keys),
        }
        log.info(
            "Modo compartido activado sin cursor previo; se inicializa al final del CSV "
            f"en {latest_dt.isoformat()} para no reprocesar backlog."
        )

    def _is_cursor_candidate(self, sig_dt: datetime, key: tuple[str, str, str]) -> bool:
        last_dt = _cursor_timestamp_to_datetime(self._cursor_state.get("last_timestamp"))
        if last_dt is None:
            return True
        if sig_dt > last_dt:
            return True
        if sig_dt < last_dt:
            return False
        seen_keys = set(self._cursor_state.get("last_keys_at_timestamp", []))
        return _serialize_signal_key(key) not in seen_keys

    def _advance_cursor(self, consumed_rows: List[tuple[datetime, tuple[str, str, str]]]) -> bool:
        if not consumed_rows:
            return False

        last_dt = _cursor_timestamp_to_datetime(self._cursor_state.get("last_timestamp"))
        latest_dt = max(sig_dt for sig_dt, _ in consumed_rows)
        latest_keys = {
            _serialize_signal_key(key)
            for sig_dt, key in consumed_rows
            if sig_dt == latest_dt
        }

        if last_dt is not None and latest_dt == last_dt:
            latest_keys.update(self._cursor_state.get("last_keys_at_timestamp", []))

        new_state = {
            "last_timestamp": latest_dt.isoformat(),
            "last_keys_at_timestamp": sorted(latest_keys),
        }
        if new_state == self._cursor_state:
            return False

        self._cursor_state = new_state
        return True

    def _apply_filters(self, sig: Signal) -> Optional[str]:
        """Devuelve el motivo de rechazo, o None si pasa todos los filtros."""
        mom = sig.mom_pct if sig.mom_pct is not None else sig.mom_1h_pct
        quintile_label = _quintile_label(sig.quintil)

        if self._cfg.profile_btc_filter_active:
            if self._cfg.profile_btc_reverso:
                return "macro_btc activo con btc_reverso=true, pero esta version del bot no soporta inversion long"
            return "macro_btc activo, pero esta version del bot no soporta filtro BTC"

        if self._cfg.signal_rank_min is not None and sig.rank < self._cfg.signal_rank_min:
            return f"rank={sig.rank} < {self._cfg.signal_rank_min}"
        if self._cfg.signal_rank_max is not None and sig.rank > self._cfg.signal_rank_max:
            return f"rank={sig.rank} > {self._cfg.signal_rank_max}"

        if self._cfg.signal_momentum_min > 0 and mom is None:
            return (
                "falta momentum para aplicar el filtro activo "
                f"(momentum_min={self._cfg.signal_momentum_min})"
            )
        if mom is not None and mom < self._cfg.signal_momentum_min:
            return f"momentum={mom:.2f} < {self._cfg.signal_momentum_min}"
        if self._cfg.signal_momentum_max is not None and mom is None:
            return (
                "falta momentum para aplicar el filtro activo "
                f"(momentum_max={self._cfg.signal_momentum_max})"
            )
        if self._cfg.signal_momentum_max is not None and mom is not None and mom > self._cfg.signal_momentum_max:
            return f"momentum={mom:.2f} > {self._cfg.signal_momentum_max}"

        if self._cfg.signal_vol_ratio_min > 0 and sig.vol_ratio is None:
            return f"falta vol_ratio para aplicar el filtro activo ({self._cfg.signal_vol_ratio_min})"
        if self._cfg.signal_vol_ratio_min > 0 and sig.vol_ratio < self._cfg.signal_vol_ratio_min:
            return f"vol_ratio={sig.vol_ratio:.2f} < {self._cfg.signal_vol_ratio_min}"
        if self._cfg.signal_vol_ratio_max is not None and sig.vol_ratio is None:
            return f"falta vol_ratio para aplicar el filtro activo ({self._cfg.signal_vol_ratio_max})"
        if self._cfg.signal_vol_ratio_max is not None and sig.vol_ratio is not None and sig.vol_ratio > self._cfg.signal_vol_ratio_max:
            return f"vol_ratio={sig.vol_ratio:.2f} > {self._cfg.signal_vol_ratio_max}"

        if self._cfg.signal_trades_ratio_min > 0 and sig.trades_ratio is None:
            return (
                "falta trades_ratio para aplicar el filtro activo "
                f"({self._cfg.signal_trades_ratio_min})"
            )
        if self._cfg.signal_trades_ratio_min > 0 and sig.trades_ratio < self._cfg.signal_trades_ratio_min:
            return f"trades_ratio={sig.trades_ratio:.2f} < {self._cfg.signal_trades_ratio_min}"
        if self._cfg.signal_trades_ratio_max is not None and sig.trades_ratio is None:
            return (
                "falta trades_ratio para aplicar el filtro activo "
                f"({self._cfg.signal_trades_ratio_max})"
            )
        if self._cfg.signal_trades_ratio_max is not None and sig.trades_ratio is not None and sig.trades_ratio > self._cfg.signal_trades_ratio_max:
            return f"trades_ratio={sig.trades_ratio:.2f} > {self._cfg.signal_trades_ratio_max}"

        if self._cfg.signal_bp_min is not None and sig.bp is None:
            return f"falta bp para aplicar el filtro activo ({self._cfg.signal_bp_min})"
        if self._cfg.signal_bp_min is not None and sig.bp < self._cfg.signal_bp_min:
            return f"bp={sig.bp:.4f} < {self._cfg.signal_bp_min}"
        if self._cfg.signal_bp_max is not None and sig.bp is None:
            return f"falta bp para aplicar el filtro activo ({self._cfg.signal_bp_max})"
        if self._cfg.signal_bp_max is not None and sig.bp is not None and sig.bp > self._cfg.signal_bp_max:
            return f"bp={sig.bp:.4f} > {self._cfg.signal_bp_max}"

        allowed_categories = self._cfg.signal_allowed_categories
        if allowed_categories and not sig.categoria:
            return "falta categoria para aplicar el filtro activo"
        if allowed_categories and sig.categoria not in allowed_categories:
            return f"categoria={sig.categoria} no en {allowed_categories}"

        allowed_quintiles = self._cfg.signal_allowed_quintile_labels
        if allowed_quintiles and quintile_label is None:
            return "falta quintil para aplicar el filtro activo"
        if allowed_quintiles and quintile_label not in allowed_quintiles:
            return f"quintil={quintile_label or 'N/A'} no en {allowed_quintiles}"

        if not _hour_in_range(sig.signal_dt.hour, self._cfg.signal_hora_min, self._cfg.signal_hora_max):
            return (
                f"hora_utc={sig.signal_dt.hour} fuera de rango "
                f"[{self._cfg.signal_hora_min}, {self._cfg.signal_hora_max}]"
            )

        if not _day_allowed(sig.signal_dt, self._cfg.signal_dias_semana):
            return f"dow_utc={_js_weekday(sig.signal_dt)} no en {self._cfg.signal_dias_semana}"

        return None


def _parse_opt_float(raw: str) -> Optional[float]:
    """Convierte un string a float, devolviendo None si esta vacio."""
    s = raw.strip() if raw else ""
    return float(s) if s else None


def _parse_opt_int(raw: str) -> Optional[int]:
    """Convierte un string a int, devolviendo None si esta vacio."""
    s = raw.strip() if raw else ""
    return int(float(s)) if s else None


def _parse_signal_datetime(raw: str) -> Optional[datetime]:
    try:
        return datetime.strptime(raw, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_row_cursor_meta(row: dict) -> Optional[tuple[datetime, tuple[str, str, str]]]:
    fecha_hora = _get_row_value(row, "fecha_hora", "timestamp", "signal_ts", default="").strip()
    par = _get_row_value(row, "par", "pair", default="").strip()
    rank_raw = _get_row_value(row, "rank", "top", default="").strip()
    if not fecha_hora or not par or not rank_raw:
        return None
    sig_dt = _parse_signal_datetime(fecha_hora)
    if sig_dt is None:
        return None
    return sig_dt, (fecha_hora, par, rank_raw)


def _latest_cursor_timestamp(rows: List[dict]) -> Optional[datetime]:
    latest_dt: Optional[datetime] = None
    for row in rows:
        parsed = _parse_row_cursor_meta(row)
        if parsed is None:
            continue
        sig_dt, _ = parsed
        if latest_dt is None or sig_dt > latest_dt:
            latest_dt = sig_dt
    return latest_dt


def _serialize_signal_key(key: tuple[str, str, str]) -> str:
    return "|".join(key)


def _empty_cursor_state() -> dict[str, Any]:
    return {
        "last_timestamp": None,
        "last_keys_at_timestamp": [],
    }


def _cursor_timestamp_to_datetime(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _get_row_value(row: dict, *names: str, default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text != "":
                return text
        else:
            return str(value)
    return default


def _fmt_metric(value: Optional[float], decimals: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}%"


def _quintile_label(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    if 1 <= value <= 5:
        return f"Q{value}"
    return None


def _hour_in_range(hour: int, min_hour: Optional[int], max_hour: Optional[int]) -> bool:
    if min_hour is None or max_hour is None:
        return True
    if min_hour <= max_hour:
        return min_hour <= hour <= max_hour
    return hour >= min_hour or hour <= max_hour


def _js_weekday(dt: datetime) -> int:
    return (dt.weekday() + 1) % 7


def _day_allowed(dt: datetime, allowed_days: List[int]) -> bool:
    if not allowed_days:
        return True
    js_day = _js_weekday(dt)
    iso_day = dt.isoweekday()
    return js_day in allowed_days or iso_day in allowed_days


def _read_csv(path: Path) -> List[dict]:
    """
    Lee el CSV con BOM/CRLF y normaliza los headers (strip).
    Devuelve lista de dicts.
    """
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames:
            reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            cleaned = {}
            for k, v in row.items():
                if k is None:
                    continue
                cleaned[k.strip()] = v.strip() if isinstance(v, str) else v
            rows.append(cleaned)
    return rows


