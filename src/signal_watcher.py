"""
signal_watcher.py - Vigila fut_pares_short.csv y emite senales.

Manejo del CSV:
  - Encoding utf-8-sig (soporta BOM de Windows)
  - CRLF / LF: csv.reader lo maneja automaticamente con newline=""
  - Headers con espacios: strip() en cada nombre de columna
  - Alias tolerantes de columnas para acompanar la integracion con el dashboard

Logica:
  - Procesa solo filas con leido=="no"
  - Senales con antiguedad > max_signal_age_minutes -> marca "timeout"
  - Senales validas -> aplica filtros de la config -> emite al trade engine
  - Marca "si" (procesada) o "timeout" incluso si se descarta por filtros
"""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Tuple

from .config import Config
from .logger import get_logger
from .models import Signal

COMPONENT_VERSION = "0.12"

log = get_logger("signal_watcher")

OnSignalCallback = Callable[[Signal], Awaitable[None]]


class SignalWatcher:
    def __init__(self, cfg: Config, on_signal: OnSignalCallback):
        self._cfg = cfg
        self._on_signal = on_signal
        self._task: Optional[asyncio.Task] = None
        self._last_mtime: float = 0.0

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop(), name="signal_watcher")
        log.info(
            f"SignalWatcher iniciado -> {self._cfg.signals_file_path} "
            f"(poll cada {self._cfg.poll_interval_seconds}s)"
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

        signals, updates = self._read_and_filter(path)

        if updates:
            await asyncio.get_event_loop().run_in_executor(None, _update_csv, path, updates)

        for sig in signals:
            try:
                await self._on_signal(sig)
            except Exception as exc:
                log.error(f"Error procesando senal {sig.pair}: {exc}", exc_info=True)

    def _read_and_filter(self, path: Path) -> Tuple[List[Signal], dict]:
        """
        Devuelve (senales_validas, {(fecha_hora, par, rank): nuevo_leido}).
        """
        now = datetime.now(timezone.utc)
        signals: List[Signal] = []
        updates: dict = {}

        try:
            rows = _read_csv(path)
        except Exception as exc:
            log.error(f"Error leyendo CSV {path}: {exc}")
            return [], {}

        for row in rows:
            leido = _get_row_value(row, "leido", default="").strip().lower()
            if leido != "no":
                continue

            fecha_hora = _get_row_value(row, "fecha_hora", "timestamp", "signal_ts", default="").strip()
            par = _get_row_value(row, "par", "pair", default="").strip()
            rank_raw = _get_row_value(row, "rank", "top", default="").strip()
            key = (fecha_hora, par, rank_raw)

            if not fecha_hora:
                log.warning("Senal descartada: falta timestamp (fecha_hora/timestamp/signal_ts)")
                updates[key] = "si"
                continue
            if not par:
                log.warning("Senal descartada: falta par (par/pair)")
                updates[key] = "si"
                continue
            if not rank_raw:
                log.warning(f"Senal {par} descartada: falta rank/top")
                updates[key] = "si"
                continue

            try:
                sig_dt = datetime.strptime(fecha_hora, "%Y/%m/%d %H:%M:%S")
                sig_dt = sig_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning(f"Timestamp invalido en senal: '{fecha_hora}'")
                updates[key] = "si"
                continue

            age_min = (now - sig_dt).total_seconds() / 60
            if age_min > self._cfg.max_signal_age_minutes:
                log.info(
                    f"Senal expirada ({age_min:.1f}min > {self._cfg.max_signal_age_minutes}min): "
                    f"{par} -> timeout"
                )
                updates[key] = "timeout"
                continue

            try:
                top = int(float(rank_raw))
            except ValueError:
                log.warning(f"Senal {par} descartada: rank/top invalido '{rank_raw}'")
                updates[key] = "si"
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
                updates[key] = "si"
                continue

            reject_reason = self._apply_filters(sig)
            if reject_reason:
                log.info(f"Senal {par} descartada ({reject_reason})")
                updates[key] = "si"
                continue

            log.info(
                f"Senal aceptada: {par} rank={top} cat={sig.categoria or 'N/A'} "
                f"mom={_fmt_pct(sig.mom_pct)} vol={_fmt_metric(sig.vol_ratio)} "
                f"tr={_fmt_metric(sig.trades_ratio)} "
                f"Q{sig.quintil if sig.quintil is not None else 'N/A'} "
                f"bp={_fmt_metric(sig.bp, 4)}"
            )
            signals.append(sig)
            updates[key] = "si"

        return signals, updates

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


def _detect_csv_newline(text: str) -> str:
    """
    Detecta el separador de linea preferido del CSV.
    Si el fichero ya viene corrupto con '\r' sueltos, lo normaliza a CRLF.
    """
    if "\r\n" in text:
        return "\r\n"
    if "\n" in text:
        return "\n"
    if "\r" in text:
        return "\r\n"
    return "\n"


def _update_csv(path: Path, updates: dict) -> None:
    """
    Actualiza la columna 'leido' de las filas indicadas.
    updates = {(fecha_hora, par, rank_str): new_value}
    Escritura atomica via fichero temporal.
    """
    try:
        content = path.read_bytes()
        encoding = "utf-8-sig" if content.startswith(b"\xef\xbb\xbf") else "utf-8"
        text = content.decode(encoding)
        newline_style = _detect_csv_newline(text)
        lines = text.splitlines()

        if not lines:
            return

        non_blank_lines = [line for line in lines if line.strip()]
        if not non_blank_lines:
            return

        header_line = non_blank_lines[0].strip()
        sep = ","
        headers = [h.strip() for h in header_line.split(sep)]
        try:
            leido_idx = headers.index("leido")
        except ValueError:
            log.warning("Columna 'leido' no encontrada en CSV, no se puede actualizar")
            return

        # Reescribimos el fichero sin lineas vacias para impedir la acumulacion
        # de saltos de linea corruptos al marcar la columna "leido".
        new_lines = [header_line + newline_style]
        for line in non_blank_lines[1:]:
            stripped = line.strip()
            parts = stripped.split(sep)

            try:
                key = (
                    parts[headers.index("fecha_hora")].strip() if "fecha_hora" in headers else "",
                    parts[headers.index("par")].strip() if "par" in headers else "",
                    parts[headers.index("rank")].strip() if "rank" in headers else (
                        parts[headers.index("top")].strip() if "top" in headers else ""
                    ),
                )
            except IndexError:
                new_lines.append(stripped + newline_style)
                continue

            if key in updates and leido_idx < len(parts):
                parts[leido_idx] = updates[key]
                new_lines.append(sep.join(parts) + newline_style)
            else:
                new_lines.append(stripped + newline_style)

        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(new_lines), encoding=encoding, newline="")
        tmp.replace(path)
        log.debug(f"CSV actualizado: {len(updates)} filas marcadas")

    except Exception as exc:
        log.error(f"Error actualizando CSV {path}: {exc}", exc_info=True)
