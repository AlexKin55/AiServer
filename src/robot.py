"""WebSocket-сессия робота AIBot: соединение и отправка команд."""
import logging
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("uvicorn")


class RobotSession:
    """Обёртка над активным WebSocket-соединением с роботом."""

    def __init__(self) -> None:
        self.ws: Optional[WebSocket] = None
        self.peer = ""

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def attach(self, ws: WebSocket) -> None:
        self.ws = ws
        self.peer = ws.client.host if ws.client else "?"

    async def detach(self) -> None:
        self.ws = None
        self.peer = ""

    async def send_text(self, text: str) -> bool:
        """Отправляет текстовую команду роботу. False, если нет соединения."""
        if not self.connected:
            return False
        try:
            await self.ws.send_text(text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отправить %r: %s", text, exc)
            return False

    async def send_audio_frame(self, frame_type: int, codec: int,
                               payload: bytes) -> bool:
        """Отправляет бинарный аудиофрейм [тип][кодек][данные]."""
        if not self.connected:
            return False
        try:
            await self.ws.send_bytes(bytes((frame_type, codec)) + payload)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отправить аудио: %s", exc)
            return False