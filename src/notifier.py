"""
notifier.py - Notificaciones opcionales para gestiona_trades.

Primera fase orientada a VPS:
- Telegram para alertas inmediatas
- sin dependencias extra fuera de aiohttp
- tolerante a errores de red para no afectar al motor de trading
"""
from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from .config import Config
from .logger import get_logger

NOTIFIER_VERSION = "0.11"

log = get_logger("notifier")


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str

    @property
    def is_ready(self) -> bool:
        return self.enabled and bool(self.bot_token) and bool(self.chat_id)


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig):
        self._cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if not self._cfg.is_ready or self._session:
            return
        timeout = aiohttp.ClientTimeout(total=10)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self) -> None:
        if not self._session:
            return
        await self._session.close()
        self._session = None

    async def send(self, text: str) -> bool:
        if not self._cfg.is_ready:
            return False
        if not self._session:
            await self.start()
        if not self._session:
            return False

        url = f"https://api.telegram.org/bot{self._cfg.bot_token}/sendMessage"
        payload = {
            "chat_id": self._cfg.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:300]
                    log.warning(
                        "Telegram respondio con error %s: %s",
                        resp.status,
                        body,
                    )
                    return False
        except Exception as exc:
            log.warning("No se pudo enviar notificacion Telegram: %s", exc)
            return False
        return True


class Notifier:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._telegram = TelegramNotifier(
            TelegramConfig(
                enabled=cfg.telegram_enabled,
                bot_token=cfg.telegram_bot_token,
                chat_id=cfg.telegram_chat_id,
            )
        )

    @property
    def enabled(self) -> bool:
        return self._telegram._cfg.is_ready

    async def start(self) -> None:
        if not self.enabled:
            if self._cfg.notifications_enabled and self._cfg.telegram_enabled:
                log.warning(
                    "Notificaciones activadas pero Telegram no esta completo; revisa bot_token/chat_id."
                )
            return
        await self._telegram.start()

    async def stop(self) -> None:
        await self._telegram.stop()

    async def send(self, title: str, lines: list[str] | None = None) -> bool:
        if not self.enabled:
            return False
        body = [title.strip()]
        for line in lines or []:
            clean = str(line).strip()
            if clean:
                body.append(clean)
        return await self._telegram.send("\n".join(body))
