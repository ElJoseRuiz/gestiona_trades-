"""
config.py — Carga y validación de config.yaml.
Todo parámetro es accesible vía propiedades tipadas; nada hardcodeado.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import yaml

from .filtros_gestiona_trades import (
    DEFAULT_PROFILE_PATH,
    FiltrosGestionaTradesProfile,
    load_filtros_gestiona_trades,
)

CONFIG_VERSION = "0.17"


class Config:
    def __init__(self, path: str = "config.yaml"):
        self._path = Path(path)
        self._d: dict = {}
        self._profile: FiltrosGestionaTradesProfile = FiltrosGestionaTradesProfile(
            path=Path(DEFAULT_PROFILE_PATH)
        )
        self._startup_warnings: List[str] = []
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
        profile_path = self._get("strategy_profile", "path", default=DEFAULT_PROFILE_PATH)
        self._profile = load_filtros_gestiona_trades(profile_path)
        self._startup_warnings = list(self._profile.warnings)
        self._startup_warnings.extend(self._build_profile_feature_warnings())

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

    def _profile_get(self, *keys, default=None) -> Any:
        d: Any = self._profile.normalized
        for k in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(k)
            if d is None:
                return default
        return d

    def _profile_has(self, *keys) -> bool:
        d: Any = self._profile.raw
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return False
            d = d[k]
        return True

    def _effective_value(self,
                         profile_keys: tuple[str, ...],
                         config_keys: tuple[str, ...],
                         default=None) -> Any:
        if self._profile_has(*profile_keys):
            profile_value = self._profile_get(*profile_keys, default=None)
            if profile_value is not None:
                return profile_value
        return self._get(*config_keys, default=default)

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).lower() in ("true", "1", "t", "y", "yes")

    def _effective_strategy_dict(self) -> dict[str, Any]:
        strategy = dict(self._d.get("strategy", {}))
        strategy["mode"] = self.mode
        strategy["max_open_trades"] = self.max_open_trades
        strategy["tp_pct"] = self.tp_pct
        strategy["sl_pct"] = self.sl_pct
        strategy["TP_posicion"] = self.tp_posicion
        strategy["SL_posicion"] = self.sl_posicion
        strategy["Min_TP_posicion"] = self.min_tp_posicion_pct
        strategy["tp_pos"] = self.tp_posicion
        strategy["sl_pos"] = self.sl_posicion
        strategy["timeout_hours"] = self.timeout_hours
        strategy["max_hold"] = self.timeout_hours
        strategy["max_trades_per_pair"] = self.max_trades_per_pair
        strategy["quarantine_hours"] = self.quarantine_hours
        strategy["solo_cerrando_trades"] = self.real_trading_solo_cerrando
        strategy["paper_trading"] = self.paper_trading
        strategy["paper_tp_sl_price_mode"] = self.paper_tp_sl_price_mode
        strategy["friction_pct"] = self.paper_friction_pct
        strategy["paper_friction_pct"] = self.paper_friction_pct
        strategy["solo_cerrando_trades_config"] = self.solo_cerrando_trades
        return strategy

    def _effective_filters_dict(self) -> dict[str, Any]:
        return {
            "rank_min": self.signal_rank_min,
            "rank_max": self.signal_rank_max,
            "momentum_min": self.signal_momentum_min,
            "momentum_max": self.signal_momentum_max,
            "vol_ratio_min": self.signal_vol_ratio_min,
            "vol_ratio_max": self.signal_vol_ratio_max,
            "trades_ratio_min": self.signal_trades_ratio_min,
            "trades_ratio_max": self.signal_trades_ratio_max,
            "bp_min": self.signal_bp_min,
            "bp_max": self.signal_bp_max,
            "hora_min": self.signal_hora_min,
            "hora_max": self.signal_hora_max,
            "dias_semana": self.signal_dias_semana,
            "quintiles": self.signal_allowed_quintile_labels,
            "categorias": self.signal_allowed_categories,
            "filtro_excluir_overlap": self.signal_filter_overlap,
            "filtro_overlap": self.signal_filter_overlap,
            "ignore_n": self.signal_ignore_n,
            "ignore_h": self.signal_ignore_h,
        }

    @staticmethod
    def _normalize_quintile_labels(values: list[Any]) -> list[str]:
        labels: list[str] = []
        for value in values:
            if value is None or value == "":
                continue
            if isinstance(value, (int, float)):
                int_value = int(value)
                if 1 <= int_value <= 5:
                    labels.append(f"Q{int_value}")
                continue
            text = str(value).strip().upper()
            if not text:
                continue
            if text.isdigit() and 1 <= int(text) <= 5:
                labels.append(f"Q{int(text)}")
                continue
            if text.startswith("Q") and text[1:].isdigit() and 1 <= int(text[1:]) <= 5:
                labels.append(text)
        # Mantener orden sin duplicados
        return list(dict.fromkeys(labels))

    def _build_profile_feature_warnings(self) -> List[str]:
        warnings: List[str] = []

        if self.profile_global_tp is not None:
            if self.profile_global_tp_win:
                warnings.append(
                    "gestion_trade.global_tp/global_tp_win cargados, pero todavia no estan soportados; "
                    "se dejan inactivos."
                )
            else:
                warnings.append(
                    "gestion_trade.global_tp cargado, pero todavia no esta soportado; se deja inactivo."
                )
        elif self.profile_global_tp_win:
            warnings.append(
                "gestion_trade.global_tp_win=true sin global_tp; no tendra efecto."
            )

        if self.profile_kill_switch_enabled:
            warnings.append(
                "kill_switch_pf.enabled=true cargado, pero todavia no esta soportado; se deja inactivo."
            )

        if self.profile_btc_filter_active and self.profile_btc_reverso:
            warnings.append(
                "macro_btc activo con btc_reverso=true, pero el bot actual no soporta filtro BTC ni invertir "
                "a long; las senales se rechazaran por seguridad."
            )
        elif self.profile_btc_filter_active:
            warnings.append(
                "macro_btc activo, pero el bot actual no soporta filtro BTC; las senales se rechazaran por seguridad."
            )
        elif self.profile_btc_reverso:
            warnings.append(
                "macro_btc.btc_reverso=true sin filtro BTC activo; no tendra efecto."
            )

        return warnings

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
    def mode(self) -> str:
        return str(self._effective_value(("ejecucion", "mode"), ("strategy", "mode"), default="short"))
    @property
    def capital_per_trade(self)   -> float:      return float(self._get("strategy", "capital_per_trade", default=10))
    @property
    def max_open_trades(self) -> int:
        return int(self._effective_value(("limites", "max_global"), ("strategy", "max_open_trades"), default=10))
    @property
    def tp_pct(self) -> float:
        return float(self._effective_value(("gestion_trade", "tp_pct"), ("strategy", "tp_pct"), default=15))
    @property
    def sl_pct(self) -> float:
        return float(self._effective_value(("gestion_trade", "sl_pct"), ("strategy", "sl_pct"), default=60))
    @property
    def tp_posicion(self) -> bool:
        val = self._effective_value(("gestion_trade", "tp_pos"), ("strategy", "TP_posicion"), default=False)
        return self._as_bool(val, default=False)

    @property
    def sl_posicion(self) -> bool:
        val = self._effective_value(("gestion_trade", "sl_pos"), ("strategy", "SL_posicion"), default=False)
        return self._as_bool(val, default=False)

    @property
    def min_tp_posicion_pct(self) -> float:
        return float(
            self._effective_value(
                ("gestion_trade", "min_tp_posicion"),
                ("strategy", "Min_TP_posicion"),
                default=0.0,
            )
        )

    @property
    def timeout_hours(self) -> float:
        return float(self._effective_value(("gestion_trade", "max_hold"), ("strategy", "timeout_hours"), default=24))
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
    def max_trades_per_pair(self) -> int:
        return int(self._effective_value(("limites", "max_par"), ("strategy", "max_trades_per_pair"), default=1))
    @property
    def quarantine_hours(self) -> float:         return float(self._get("strategy", "quarantine_hours",   default=4.0))
    @property
    def solo_cerrando_trades(self) -> bool:
        return self._as_bool(self._get("strategy", "solo_cerrando_trades", default=False), default=False)
    @property
    def paper_trading(self) -> bool:
        return self._as_bool(self._get("strategy", "paper_trading", default=False), default=False)

    @property
    def paper_tp_sl_price_mode(self) -> str:
        raw = str(self._get("strategy", "paper_tp_sl_price_mode", default="close_5m") or "close_5m")
        mode = raw.strip().lower()
        aliases = {
            "close": "close_5m",
            "close_5m": "close_5m",
            "cierre_5m": "close_5m",
            "high_low": "high_low_5m",
            "high_low_5m": "high_low_5m",
            "hl_5m": "high_low_5m",
            "intravela_5m": "high_low_5m",
        }
        return aliases.get(mode, "close_5m")

    @property
    def paper_friction_pct(self) -> float:
        friction_pct = self._effective_value(
            ("gestion_trade", "friction_pct"),
            ("strategy", "friction_pct"),
            default=0.15,
        )
        return max(0.0, float(friction_pct))

    @property
    def real_trading_solo_cerrando(self) -> bool:
        return self.solo_cerrando_trades or self.paper_trading

    # ──────────────────────────────────────────────────────────────────────
    # Signals
    # ──────────────────────────────────────────────────────────────────────

    @property
    def signals_file_path(self)          -> str:   return self._get("signals", "file_path",              default="fut_pares_short.csv")
    @property
    def poll_interval_seconds(self)      -> float: return float(self._get("signals", "poll_interval_seconds",  default=15))
    @property
    def max_signal_age_minutes(self)     -> float: return float(self._get("signals", "max_signal_age_minutes", default=10))

    @property
    def signal_rank_min(self) -> int | None:
        return self._profile_get("filtros_entrada", "rank_min", default=None)

    @property
    def signal_rank_max(self) -> int | None:
        return self._profile_get("filtros_entrada", "rank_max", default=None)

    @property
    def signal_momentum_min(self) -> float:
        return float(self._effective_value(("filtros_entrada", "momentum_min"), ("strategy", "min_momentum_pct"), default=0))

    @property
    def signal_momentum_max(self) -> float | None:
        return self._profile_get("filtros_entrada", "momentum_max", default=None)

    @property
    def signal_vol_ratio_min(self) -> float:
        return float(self._effective_value(("filtros_entrada", "vol_ratio_min"), ("strategy", "min_vol_ratio"), default=0))

    @property
    def signal_vol_ratio_max(self) -> float | None:
        return self._profile_get("filtros_entrada", "vol_ratio_max", default=None)

    @property
    def signal_trades_ratio_min(self) -> float:
        return float(self._effective_value(("filtros_entrada", "trades_ratio_min"), ("strategy", "min_trades_ratio"), default=0))

    @property
    def signal_trades_ratio_max(self) -> float | None:
        return self._profile_get("filtros_entrada", "trades_ratio_max", default=None)

    @property
    def signal_bp_min(self) -> float | None:
        return self._profile_get("filtros_entrada", "bp_min", default=None)

    @property
    def signal_bp_max(self) -> float | None:
        return self._profile_get("filtros_entrada", "bp_max", default=None)

    @property
    def signal_hora_min(self) -> int | None:
        return self._profile_get("filtros_entrada", "hora_min", default=None)

    @property
    def signal_hora_max(self) -> int | None:
        return self._profile_get("filtros_entrada", "hora_max", default=None)

    @property
    def signal_dias_semana(self) -> List[int]:
        days = self._profile_get("filtros_entrada", "dias_semana", default=[])
        return list(days) if isinstance(days, list) else []

    @property
    def signal_allowed_quintile_labels(self) -> List[str]:
        profile_quintiles = self._profile_get("filtros_entrada", "quintiles", default=[])
        if isinstance(profile_quintiles, list):
            return self._normalize_quintile_labels(profile_quintiles)
        legacy_quintiles = self._get("strategy", "allowed_quintiles", default=[1, 2, 3, 4, 5])
        if isinstance(legacy_quintiles, list):
            return self._normalize_quintile_labels(legacy_quintiles)
        return []

    @property
    def signal_allowed_categories(self) -> List[str]:
        categories = self._profile_get("filtros_entrada", "categorias", default=[])
        if not isinstance(categories, list):
            return []
        return [str(category).strip() for category in categories if str(category).strip()]

    @property
    def signal_filter_overlap(self) -> bool:
        overlap = self._profile_get("filtros_entrada", "filtro_excluir_overlap", default=None)
        if overlap is None:
            overlap = self._profile_get("filtros_entrada", "filtro_overlap", default=False)
        return bool(overlap)

    @property
    def signal_ignore_n(self) -> int:
        return int(self._profile_get("filtros_entrada", "ignore_n", default=0) or 0)

    @property
    def signal_ignore_h(self) -> float:
        return float(self._profile_get("filtros_entrada", "ignore_h", default=0.0) or 0.0)

    @property
    def profile_global_tp(self) -> float | None:
        return self._profile_get("gestion_trade", "global_tp", default=None)

    @property
    def profile_global_tp_win(self) -> bool:
        return bool(self._profile_get("gestion_trade", "global_tp_win", default=False))

    @property
    def profile_btc_filter_active(self) -> bool:
        return bool(
            self._profile_get("macro_btc", "btc_filtro_cross", default=False)
            or self._profile_get("macro_btc", "btc_filtro_price", default=False)
        )

    @property
    def profile_btc_reverso(self) -> bool:
        return bool(self._profile_get("macro_btc", "btc_reverso", default=False))

    @property
    def profile_kill_switch_enabled(self) -> bool:
        return bool(self._profile_get("kill_switch_pf", "enabled", default=False))

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

    # ----------------------------------------------------------------------
    # Notificaciones
    # ----------------------------------------------------------------------

    @property
    def instance_name(self) -> str:
        configured = str(self._get("notifications", "instance_name", default="") or "").strip()
        return configured or self._path.stem

    @property
    def notifications_enabled(self) -> bool:
        return self._as_bool(self._get("notifications", "enabled", default=False), default=False)

    @property
    def telegram_enabled(self) -> bool:
        enabled = self._as_bool(
            self._get("notifications", "telegram", "enabled", default=False),
            default=False,
        )
        return self.notifications_enabled and enabled

    @property
    def telegram_bot_token(self) -> str:
        return str(self._get("notifications", "telegram", "bot_token", default="") or "").strip()

    @property
    def telegram_chat_id(self) -> str:
        return str(self._get("notifications", "telegram", "chat_id", default="") or "").strip()

    @property
    def notify_startup(self) -> bool:
        return self._as_bool(
            self._get("notifications", "rules", "notify_startup", default=True),
            default=True,
        )

    @property
    def notify_shutdown(self) -> bool:
        return self._as_bool(
            self._get("notifications", "rules", "notify_shutdown", default=True),
            default=True,
        )

    @property
    def notify_errors(self) -> bool:
        return self._as_bool(
            self._get("notifications", "rules", "notify_errors", default=True),
            default=True,
        )

    @property
    def notify_orphans(self) -> bool:
        return self._as_bool(
            self._get("notifications", "rules", "notify_orphans", default=True),
            default=True,
        )

    @property
    def notify_ws_disconnect(self) -> bool:
        return self._as_bool(
            self._get("notifications", "rules", "notify_ws_disconnect", default=True),
            default=True,
        )

    @property
    def notify_summary(self) -> bool:
        return self._as_bool(
            self._get("notifications", "rules", "notify_summary", default=True),
            default=True,
        )

    @property
    def notify_health_check_interval_seconds(self) -> int:
        return max(
            30,
            int(
                self._get(
                    "notifications",
                    "rules",
                    "health_check_interval_seconds",
                    default=300,
                )
            ),
        )

    @property
    def notify_summary_interval_minutes(self) -> int:
        return max(
            0,
            int(
                self._get(
                    "notifications",
                    "rules",
                    "summary_interval_minutes",
                    default=240,
                )
            ),
        )

    @property
    def notify_ws_disconnect_grace_minutes(self) -> int:
        return max(
            0,
            int(
                self._get(
                    "notifications",
                    "rules",
                    "ws_disconnect_grace_minutes",
                    default=5,
                )
            ),
        )

    @property
    def notify_orphan_repeat_minutes(self) -> int:
        return max(
            1,
            int(
                self._get(
                    "notifications",
                    "rules",
                    "orphan_repeat_minutes",
                    default=30,
                )
            ),
        )

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
        notifications = d.get("notifications", {})
        if isinstance(notifications, dict):
            telegram = notifications.get("telegram", {})
            if isinstance(telegram, dict):
                telegram["bot_token_configured"] = bool(telegram.get("bot_token"))
                telegram["chat_id_configured"] = bool(telegram.get("chat_id"))
                telegram.pop("bot_token", None)
                telegram.pop("chat_id", None)
        d["strategy"] = self._effective_strategy_dict()
        d["effective_filters_entrada"] = self._effective_filters_dict()
        d["filtros_gestiona_trades"] = self.profile
        d["filtros_gestiona_trades_meta"] = {
            "loaded": self.profile_loaded,
            "exists": self._profile.exists,
            "path": str(self._profile.path),
            "warnings": self.startup_warnings,
            "unsupported_features": {
                "global_tp": self.profile_global_tp is not None,
                "global_tp_win": self.profile_global_tp_win,
                "macro_btc": self.profile_btc_filter_active,
                "btc_reverso": self.profile_btc_reverso,
                "kill_switch_pf": self.profile_kill_switch_enabled,
            },
            "applied_overrides": {
                "mode": self._profile_has("ejecucion", "mode") and self._profile_get("ejecucion", "mode") is not None,
                "tp_pct": self._profile_has("gestion_trade", "tp_pct") and self._profile_get("gestion_trade", "tp_pct") is not None,
                "sl_pct": self._profile_has("gestion_trade", "sl_pct") and self._profile_get("gestion_trade", "sl_pct") is not None,
                "tp_pos": self._profile_has("gestion_trade", "tp_pos") and self._profile_get("gestion_trade", "tp_pos") is not None,
                "sl_pos": self._profile_has("gestion_trade", "sl_pos") and self._profile_get("gestion_trade", "sl_pos") is not None,
                "min_tp_posicion": self._profile_has("gestion_trade", "min_tp_posicion") and self._profile_get("gestion_trade", "min_tp_posicion") is not None,
                "friction_pct": self._profile_has("gestion_trade", "friction_pct") and self._profile_get("gestion_trade", "friction_pct") is not None,
                "max_hold": self._profile_has("gestion_trade", "max_hold") and self._profile_get("gestion_trade", "max_hold") is not None,
                "max_par": self._profile_has("limites", "max_par") and self._profile_get("limites", "max_par") is not None,
                "max_global": self._profile_has("limites", "max_global") and self._profile_get("limites", "max_global") is not None,
            },
        }
        return d

    @property
    def profile(self) -> dict:
        return self._profile.normalized

    @property
    def profile_loaded(self) -> bool:
        return self._profile.loaded

    @property
    def profile_path(self) -> str:
        return str(self._profile.path)

    @property
    def startup_warnings(self) -> List[str]:
        return list(self._startup_warnings)
