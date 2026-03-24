"""
filtros_gestiona_trades.py - Carga tolerante del perfil exportado por el dashboard.

Objetivos:
- no romper el arranque si el fichero falta o viene incompleto
- validar de forma basica el contrato conocido
- normalizar tipos para que el resto del bot no dependa del YAML crudo
- ignorar claves futuras desconocidas sin fallar
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_PROFILE_PATH = "estrategia/filtros_gestiona_trades.yaml"
FILTROS_GESTIONA_TRADES_VERSION = "0.12"


@dataclass(slots=True)
class FiltrosGestionaTradesProfile:
    path: Path
    exists: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return self.exists and bool(self.normalized)


def load_filtros_gestiona_trades(path: str | Path = DEFAULT_PROFILE_PATH) -> FiltrosGestionaTradesProfile:
    profile_path = Path(path)
    profile = FiltrosGestionaTradesProfile(path=profile_path)

    if not profile_path.exists():
        return profile

    profile.exists = True

    try:
        with open(profile_path, encoding="utf-8") as fh:
            raw_data = yaml.safe_load(fh) or {}
    except Exception as exc:
        profile.warnings.append(
            f"No se pudo leer el perfil de filtros '{profile_path}': {exc}"
        )
        return profile

    if not isinstance(raw_data, dict):
        profile.warnings.append(
            f"El perfil de filtros '{profile_path}' no contiene un mapa YAML en la raiz."
        )
        return profile

    profile.raw = raw_data
    profile.normalized = _normalize_profile(raw_data, profile.warnings, profile_path)
    return profile


def _normalize_profile(data: dict[str, Any], warnings: list[str], path: Path) -> dict[str, Any]:
    schema_version = _as_int(data.get("schema_version"))
    if schema_version is None:
        warnings.append(
            f"El perfil '{path}' no define schema_version; se aplicara modo tolerante."
        )
    elif schema_version > SUPPORTED_SCHEMA_VERSION:
        warnings.append(
            f"El perfil '{path}' usa schema_version={schema_version}, mayor que la soportada "
            f"({SUPPORTED_SCHEMA_VERSION}); se aplicara modo tolerante."
        )

    origen = _as_dict(data.get("origen"))
    ejecucion = _as_dict(data.get("ejecucion"))
    filtros_entrada = _as_dict(data.get("filtros_entrada"))
    gestion_trade = _as_dict(data.get("gestion_trade"))
    limites = _as_dict(data.get("limites"))
    macro_btc = _as_dict(data.get("macro_btc"))
    kill_switch_pf = _as_dict(data.get("kill_switch_pf"))

    normalized = {
        "schema_version": schema_version,
        "exportado_utc": _as_str(data.get("exportado_utc")),
        "origen": {
            "dashboard": _as_str(origen.get("dashboard")),
            "fichero_senales": _as_str(origen.get("fichero_senales")),
        },
        "ejecucion": {
            "mode": _as_mode(ejecucion.get("mode")),
        },
        "filtros_entrada": {
            "rank_min": _as_int(filtros_entrada.get("rank_min")),
            "rank_max": _as_int(filtros_entrada.get("rank_max")),
            "momentum_min": _as_float(filtros_entrada.get("momentum_min")),
            "momentum_max": _as_float(filtros_entrada.get("momentum_max")),
            "vol_ratio_min": _as_float(filtros_entrada.get("vol_ratio_min")),
            "vol_ratio_max": _as_float(filtros_entrada.get("vol_ratio_max")),
            "trades_ratio_min": _as_float(filtros_entrada.get("trades_ratio_min")),
            "trades_ratio_max": _as_float(filtros_entrada.get("trades_ratio_max")),
            "bp_min": _as_float(filtros_entrada.get("bp_min")),
            "bp_max": _as_float(filtros_entrada.get("bp_max")),
            "hora_min": _as_int(filtros_entrada.get("hora_min")),
            "hora_max": _as_int(filtros_entrada.get("hora_max")),
            "dias_semana": _as_int_list(filtros_entrada.get("dias_semana")),
            "quintiles": _as_quintiles(filtros_entrada.get("quintiles")),
            "categorias": _as_str_list(filtros_entrada.get("categorias")),
            "filtro_overlap": _as_bool(filtros_entrada.get("filtro_overlap"), default=False),
            "ignore_n": _as_int(filtros_entrada.get("ignore_n"), default=0),
            "ignore_h": _as_float(filtros_entrada.get("ignore_h"), default=0.0),
        },
        "gestion_trade": {
            "sl_pct": _as_float(gestion_trade.get("sl_pct")),
            "tp_pct": _as_float(gestion_trade.get("tp_pct")),
            "sl_pos": _as_bool(gestion_trade.get("sl_pos"), default=False),
            "tp_pos": _as_bool(gestion_trade.get("tp_pos"), default=False),
            "min_tp_posicion": _as_float(gestion_trade.get("min_tp_posicion")),
            "global_tp": _as_float(gestion_trade.get("global_tp")),
            "global_tp_win": _as_bool(gestion_trade.get("global_tp_win"), default=False),
            "max_hold": _as_float(gestion_trade.get("max_hold")),
        },
        "limites": {
            "max_par": _as_int(limites.get("max_par")),
            "max_global": _as_int(limites.get("max_global")),
        },
        "macro_btc": {
            "btc_filtro_cross": _as_bool(macro_btc.get("btc_filtro_cross"), default=False),
            "btc_filtro_price": _as_bool(macro_btc.get("btc_filtro_price"), default=False),
            "btc_reverso": _as_bool(macro_btc.get("btc_reverso"), default=False),
            "btc_sma_corta": _as_int(macro_btc.get("btc_sma_corta")),
            "btc_sma_larga": _as_int(macro_btc.get("btc_sma_larga")),
            "btc_sma_precio": _as_str(macro_btc.get("btc_sma_precio")),
        },
        "kill_switch_pf": {
            "enabled": _as_bool(kill_switch_pf.get("enabled"), default=False),
            "window": _as_int(kill_switch_pf.get("window")),
            "review": _as_int(kill_switch_pf.get("review")),
            "pf_off": _as_float(kill_switch_pf.get("pf_off")),
            "pf_on": _as_float(kill_switch_pf.get("pf_on")),
        },
    }
    return normalized


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_mode(value: Any) -> str | None:
    text = _as_str(value)
    return text.lower() if text else None


def _as_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "t", "yes", "y", "si", "on"}:
        return True
    if text in {"false", "0", "f", "no", "n", "off"}:
        return False
    return default


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        text = _as_str(item)
        if text:
            result.append(text)
    return result


def _as_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    result: list[int] = []
    for item in items:
        parsed = _as_int(item)
        if parsed is not None:
            result.append(parsed)
    return result


def _as_quintiles(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        if item is None or item == "":
            continue
        if isinstance(item, (int, float)):
            q_num = int(item)
            if 1 <= q_num <= 5:
                result.append(f"Q{q_num}")
            continue
        text = str(item).strip().upper()
        if not text:
            continue
        if text.isdigit() and 1 <= int(text) <= 5:
            result.append(f"Q{int(text)}")
            continue
        if text.startswith("Q") and text[1:].isdigit() and 1 <= int(text[1:]) <= 5:
            result.append(text)
    return result
