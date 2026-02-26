"""
config.py — Carga y validación de config.yaml.
Todo parámetro es accesible vía propiedades tipadas; nada hardcodeado.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import yaml


class Config:
    def __init__(self, path: str = "config.yaml"):
        self._path = Path(path)
        self._d: dict = {}
        self.load()

    # ──────────────────────────────────────────────────────────────────────
    # Carga y validación
    # ──────────────────────────────────────────────────────────────────────

    def load(self):
        if not self._path.exists():
            raise FileNotFoundError(f"Config no encontrada: {self._path}")
        with open(self._path, encoding="utf-8") as f:
            self._d = yaml.safe_load(f) or {}
        self._validate()

    def _validate(self):
        required = [
            ("binance",  "api_key"),
            ("binance",  "api_secret"),
            ("binance",  "base_url"),
            ("strategy", "capital_per_trade"),
            ("strategy", "max_open_trades"),
            ("strategy", "tp_pct"),
            ("strategy", "sl_pct"),
            ("signals",  "file_path"),
            ("database", "path"),
        ]
        for section, key in required:
            if not self._d.get(section, {}).get(key):
                raise ValueError(f"config.yaml falta: {section}.{key}")

    def _get(self, *keys, default=None) -> Any:
        d = self._d
        for k in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(k)
            if d is None:
                return default
        return d

    # ──────────────────────────────────────────────────────────────────────
    # Binance
    # ──────────────────────────────────────────────────────────────────────

    @property
    def api_key(self)    -> str: return self._d["binance"]["api_key"]
    @property
    def api_secret(self) -> str: return self._d["binance"]["api_secret"]
    @property
    def base_url(self)   -> str: return self._d["binance"]["base_url"]

    @property
    def ws_base_url(self) -> str:
        # wss://fstream.binance.com para producción
        if "fapi.binance.com" in self.base_url:
            return "wss://fstream.binance.com"
        return "wss://stream.binancefuture.com"   # testnet

    # ──────────────────────────────────────────────────────────────────────
    # Strategy
    # ──────────────────────────────────────────────────────────────────────

    @property
    def mode(self)                -> str:        return self._get("strategy", "mode",               default="short")
    @property
    def capital_per_trade(self)   -> float:      return float(self._get("strategy", "capital_per_trade", default=10))
    @property
    def max_open_trades(self)     -> int:        return int(self._get("strategy",   "max_open_trades",   default=10))
    @property
    def tp_pct(self)              -> float:      return float(self._get("strategy", "tp_pct",            default=15))
    @property
    def sl_pct(self)              -> float:      return float(self._get("strategy", "sl_pct",            default=60))
    @property
    def trigger_offset_pct(self)  -> float:      return float(self._get("strategy", "trigger_offset_pct",default=10))
    @property
    def timeout_hours(self)       -> float:      return float(self._get("strategy", "timeout_hours",     default=24))
    @property
    def top_n(self)               -> int:        return int(self._get("strategy",   "top_n",             default=1))
    @property
    def leverage(self)            -> int:        return int(self._get("strategy",   "leverage",          default=1))
    @property
    def min_momentum_pct(self)    -> float:      return float(self._get("strategy", "min_momentum_pct",  default=0))
    @property
    def min_vol_ratio(self)       -> float:      return float(self._get("strategy", "min_vol_ratio",     default=0))
    @property
    def min_trades_ratio(self)    -> float:      return float(self._get("strategy", "min_trades_ratio",  default=0))
    @property
    def allowed_quintiles(self)   -> List[int]:  return list(self._get("strategy",  "allowed_quintiles", default=[1,2,3,4,5]))
    @property
    def max_trades_per_pair(self) -> int:        return int(self._get("strategy",   "max_trades_per_pair",default=1))

    # ──────────────────────────────────────────────────────────────────────
    # Signals
    # ──────────────────────────────────────────────────────────────────────

    @property
    def signals_file_path(self)          -> str:   return self._get("signals", "file_path",              default="fut_pares_short.csv")
    @property
    def poll_interval_seconds(self)      -> float: return float(self._get("signals", "poll_interval_seconds",  default=15))
    @property
    def max_signal_age_minutes(self)     -> float: return float(self._get("signals", "max_signal_age_minutes", default=10))

    # ──────────────────────────────────────────────────────────────────────
    # Entry
    # ──────────────────────────────────────────────────────────────────────

    @property
    def entry_order_type(self)         -> str:   return self._get("entry", "order_type",             default="LIMIT_GTX")
    @property
    def chase_interval_seconds(self)   -> float: return float(self._get("entry", "chase_interval_seconds", default=2))
    @property
    def chase_timeout_seconds(self)    -> float: return float(self._get("entry", "chase_timeout_seconds",  default=30))
    @property
    def max_chase_attempts(self)       -> int:   return int(self._get("entry",   "max_chase_attempts",     default=3))
    @property
    def entry_market_fallback(self)    -> bool:  return bool(self._get("entry",  "market_fallback",        default=False))

    # ──────────────────────────────────────────────────────────────────────
    # Exit
    # ──────────────────────────────────────────────────────────────────────

    @property
    def timeout_order_type(self)       -> str:   return self._get("exit", "timeout_order_type",     default="LIMIT")
    @property
    def timeout_chase_seconds(self)    -> float: return float(self._get("exit", "timeout_chase_seconds",  default=30))
    @property
    def timeout_market_fallback(self)  -> bool:  return bool(self._get("exit",  "timeout_market_fallback", default=True))

    # SL Limit-Chase + Market Fallback
    # Cuando mark price cruza sl_trigger, en vez de STOP_MARKET nativo el
    # motor lanza un BUY LIMIT al BBO y lo persigue hasta agotar los intentos
    # o el timeout; después hace fallback a MARKET.
    @property
    def sl_chase_timeout_s(self)    -> float: return float(self._get("exit", "sl_chase_timeout_s",    default=2.0))
    @property
    def sl_chase_max_attempts(self) -> int:   return int(self._get("exit",   "sl_chase_max_attempts", default=3))
    @property
    def sl_price_match(self)        -> str:   return self._get("exit", "sl_price_match",              default="OPPONENT")
    @property
    def sl_mark_poll_interval(self) -> float: return float(self._get("exit", "sl_mark_poll_interval", default=1.0))

    # ──────────────────────────────────────────────────────────────────────
    # Dashboard
    # ──────────────────────────────────────────────────────────────────────

    @property
    def dashboard_enabled(self) -> bool: return bool(self._get("dashboard", "enabled", default=True))
    @property
    def dashboard_host(self)    -> str:  return self._get("dashboard", "host",    default="0.0.0.0")
    @property
    def dashboard_port(self)    -> int:  return int(self._get("dashboard", "port", default=8080))

    # ──────────────────────────────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────────────────────────────

    @property
    def log_level(self)         -> str: return self._get("logging", "level",         default="DEBUG")
    @property
    def log_file(self)          -> str: return self._get("logging", "file",          default="logs/gestiona_trades.log")
    @property
    def log_max_bytes(self)     -> int: return int(self._get("logging", "max_bytes", default=10_485_760))
    @property
    def log_backup_count(self)  -> int: return int(self._get("logging", "backup_count", default=5))
    @property
    def log_console_level(self) -> str: return self._get("logging", "console_level", default="INFO")

    # ──────────────────────────────────────────────────────────────────────
    # Database
    # ──────────────────────────────────────────────────────────────────────

    @property
    def db_path(self) -> str: return self._get("database", "path", default="data/trades.db")

    # ──────────────────────────────────────────────────────────────────────
    # Public config dict (sin keys)
    # ──────────────────────────────────────────────────────────────────────

    def public_dict(self) -> dict:
        """Config sin API keys, para el dashboard."""
        import copy
        d = copy.deepcopy(self._d)
        d.get("binance", {}).pop("api_key",    None)
        d.get("binance", {}).pop("api_secret", None)
        return d
