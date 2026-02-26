"""
logger.py — Setup de logging con niveles separados para fichero y consola.
Cada módulo usa logging.getLogger("gestiona_trades.<módulo>").
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .config import Config


def setup_logging(cfg: Config) -> None:
    """Configura el logger raíz. Llamar una sola vez al arrancar."""
    log_path = Path(cfg.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("gestiona_trades")
    root.setLevel(logging.DEBUG)          # captura todo; los handlers filtran
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-8s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Fichero: nivel configurado (default DEBUG) ──────────────────────
    fh = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=cfg.log_max_bytes,
        backupCount=cfg.log_backup_count,
        encoding="utf-8",
    )
    fh.setLevel(getattr(logging, cfg.log_level.upper(), logging.DEBUG))
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # ── Consola: nivel configurado (default INFO) ────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, cfg.log_console_level.upper(), logging.INFO))
    ch.setFormatter(fmt)
    root.addHandler(ch)


def get_logger(name: str) -> logging.Logger:
    """
    Devuelve logger hijo bajo 'gestiona_trades'.
    Uso: log = get_logger("trade_engine")
    """
    return logging.getLogger(f"gestiona_trades.{name}")
