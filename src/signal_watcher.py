"""
signal_watcher.py — Vigila fut_pares_short.csv y emite señales.

Manejo del CSV:
  - Encoding utf-8-sig (soporta BOM de Windows)
  - CRLF / LF: csv.reader lo maneja automáticamente con newline=""
  - Headers con espacios: strip() en cada nombre de columna
  - Columna " leido": después de strip() → "leido"

Lógica:
  - Procesa solo filas con leido=="no" y top <= top_n
  - Señales con antigüedad > max_signal_age_minutes → marca "timeout"
  - Señales válidas → aplica filtros de la config → emite al trade engine
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

log = get_logger("signal_watcher")

OnSignalCallback = Callable[[Signal], Awaitable[None]]


class SignalWatcher:
    def __init__(self, cfg: Config, on_signal: OnSignalCallback):
        self._cfg       = cfg
        self._on_signal = on_signal
        self._task:     Optional[asyncio.Task] = None
        self._last_mtime: float = 0.0

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop(), name="signal_watcher")
        log.info(f"SignalWatcher iniciado → {self._cfg.signals_file_path} "
                 f"(poll cada {self._cfg.poll_interval_seconds}s)")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("SignalWatcher detenido")

    # ──────────────────────────────────────────────────────────────────
    # Polling loop
    # ──────────────────────────────────────────────────────────────────

    async def _poll_loop(self):
        while True:
            try:
                await self._check_file()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Error en signal_watcher: {e}", exc_info=True)
            await asyncio.sleep(self._cfg.poll_interval_seconds)

    async def _check_file(self):
        path = Path(self._cfg.signals_file_path)
        if not path.exists():
            return

        # Evitar reprocesar si el fichero no cambió
        mtime = path.stat().st_mtime
        if mtime <= self._last_mtime:
            return
        self._last_mtime = mtime

        signals, updates = self._read_and_filter(path)

        # Actualizar CSV antes de emitir (evita doble proceso si el callback tarda)
        if updates:
            await asyncio.get_event_loop().run_in_executor(
                None, _update_csv, path, updates
            )

        for sig in signals:
            try:
                await self._on_signal(sig)
            except Exception as e:
                log.error(f"Error procesando señal {sig.pair}: {e}", exc_info=True)

    # ──────────────────────────────────────────────────────────────────
    # Lectura y filtrado
    # ──────────────────────────────────────────────────────────────────

    def _read_and_filter(self, path: Path) -> Tuple[List[Signal], dict]:
        """
        Devuelve (señales_válidas, {(fecha_hora,par,top): nuevo_leido}).
        """
        now     = datetime.now(timezone.utc)
        signals: List[Signal] = []
        # updates: (fecha_hora, par, top) → "si" | "timeout"
        updates: dict         = {}

        try:
            rows = _read_csv(path)
        except Exception as e:
            log.error(f"Error leyendo CSV {path}: {e}")
            return [], {}

        for row in rows:
            leido = row.get("leido", "").strip().lower()
            if leido != "no":
                continue

            fecha_hora = row.get("fecha_hora", "").strip()
            par        = row.get("par",        "").strip()
            top_raw    = row.get("top",        "0").strip()
            key        = (fecha_hora, par, top_raw)

            # Parse timestamp señal
            try:
                sig_dt = datetime.strptime(fecha_hora, "%Y/%m/%d %H:%M:%S")
                sig_dt = sig_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning(f"Timestamp inválido en señal: '{fecha_hora}'")
                updates[key] = "si"
                continue

            # Comprobar antigüedad
            age_min = (now - sig_dt).total_seconds() / 60
            if age_min > self._cfg.max_signal_age_minutes:
                log.info(f"Señal expirada ({age_min:.1f}min > "
                         f"{self._cfg.max_signal_age_minutes}min): {par} → timeout")
                updates[key] = "timeout"
                continue

            # Comprobar top_n
            try:
                top = int(top_raw)
            except ValueError:
                updates[key] = "si"
                continue
            if top > self._cfg.top_n:
                updates[key] = "si"
                continue

            # Parsear la señal
            try:
                sig = Signal(
                    fecha_hora   = fecha_hora,
                    pair         = par,
                    top          = top,
                    close        = float(row.get("close",        0) or 0),
                    mom_1h_pct   = float(row.get("mom_1h_pct",   0) or 0),
                    mom_pct      = float(row.get("mom_pct",      0) or 0),
                    vol_ratio    = float(row.get("vol_ratio",    0) or 0),
                    trades_ratio = float(row.get("trades_ratio", 0) or 0),
                    quintil      = int(float(row.get("quintil",  0) or 0)),
                    signal_dt    = sig_dt,
                )
            except (ValueError, TypeError) as e:
                log.warning(f"Error parseando señal {par}: {e}")
                updates[key] = "si"
                continue

            # Aplicar filtros configurados
            reject_reason = self._apply_filters(sig)
            if reject_reason:
                log.info(f"Señal {par} descartada ({reject_reason})")
                updates[key] = "si"
                continue

            log.info(
                f"Señal aceptada: {par} top={top} "
                f"mom_1h={sig.mom_1h_pct:.2f}% mom={sig.mom_pct:.2f}% "
                f"vol={sig.vol_ratio:.1f} tr={sig.trades_ratio:.1f} Q{sig.quintil}"
            )
            signals.append(sig)
            updates[key] = "si"

        return signals, updates

    def _apply_filters(self, sig: Signal) -> Optional[str]:
        """Devuelve el motivo de rechazo, o None si pasa todos los filtros."""
        if sig.mom_1h_pct < self._cfg.min_momentum_pct:
            return f"mom_1h_pct={sig.mom_1h_pct:.2f} < {self._cfg.min_momentum_pct}"
        if self._cfg.min_vol_ratio > 0 and sig.vol_ratio < self._cfg.min_vol_ratio:
            return f"vol_ratio={sig.vol_ratio:.2f} < {self._cfg.min_vol_ratio}"
        if self._cfg.min_trades_ratio > 0 and sig.trades_ratio < self._cfg.min_trades_ratio:
            return f"trades_ratio={sig.trades_ratio:.2f} < {self._cfg.min_trades_ratio}"
        if sig.quintil and sig.quintil not in self._cfg.allowed_quintiles:
            return f"quintil={sig.quintil} no en {self._cfg.allowed_quintiles}"
        return None


# ──────────────────────────────────────────────────────────────────────────────
# CSV I/O (síncrono, ejecutado en executor)
# ──────────────────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> List[dict]:
    """
    Lee el CSV con BOM/CRLF y normaliza los headers (strip).
    Devuelve lista de dicts.
    """
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        # Normalizar headers: strip espacios y BOM residuales
        if reader.fieldnames:
            reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            rows.append({k.strip(): (v.strip() if isinstance(v, str) else v)
                         for k, v in row.items()})
    return rows


def _update_csv(path: Path, updates: dict) -> None:
    """
    Actualiza la columna 'leido' de las filas indicadas.
    updates = {(fecha_hora, par, top_str): new_value}
    Escritura atómica vía fichero temporal.
    """
    try:
        content = path.read_bytes()
        encoding = "utf-8-sig" if content.startswith(b"\xef\xbb\xbf") else "utf-8"
        text     = content.decode(encoding)
        lines    = text.splitlines(keepends=True)

        if not lines:
            return

        # Detectar índice de la columna "leido" en la cabecera
        header_line = lines[0].rstrip("\r\n")
        sep    = ","
        headers = [h.strip() for h in header_line.split(sep)]
        try:
            leido_idx = headers.index("leido")
        except ValueError:
            log.warning("Columna 'leido' no encontrada en CSV, no se puede actualizar")
            return

        new_lines = [lines[0]]
        for line in lines[1:]:
            stripped = line.rstrip("\r\n")
            if not stripped:
                new_lines.append(line)
                continue
            parts = stripped.split(sep)
            ending = "\r\n" if line.endswith("\r\n") else "\n"

            # Clave = (fecha_hora, par, top)
            try:
                key = (
                    parts[headers.index("fecha_hora")].strip() if "fecha_hora" in headers else "",
                    parts[headers.index("par")].strip()        if "par"        in headers else "",
                    parts[headers.index("top")].strip()        if "top"        in headers else "",
                )
            except IndexError:
                new_lines.append(line)
                continue

            if key in updates and leido_idx < len(parts):
                parts[leido_idx] = updates[key]
                new_lines.append(sep.join(parts) + ending)
            else:
                new_lines.append(line)

        # Escritura atómica
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(new_lines), encoding="utf-8")
        tmp.replace(path)
        log.debug(f"CSV actualizado: {len(updates)} filas marcadas")

    except Exception as e:
        log.error(f"Error actualizando CSV {path}: {e}", exc_info=True)
