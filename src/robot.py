"""WebSocket-сессия робота AIBot: соединение и отправка команд."""
import asyncio
import logging
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket

logger = logging.getLogger("uvicorn")

# Танцевальный паттерн по умолчанию: (ось, градусы, пауза после команды, с).
# Оси и протокол — как в tests/move.py: MOVE:left/right/up/down:<deg> и
# MOVE:center. Пауза должна быть не меньше времени отработки сервопривода,
# иначе движения «смазываются».
DANCE_STEPS = (
    ("left", 60, 0.4),
    ("right", 120, 0.4),
    ("left", 60, 0.4),
    ("center", 0, 0.3),
    ("up", 30, 0.3),
    ("down", 60, 0.3),
    ("up", 30, 0.3),
    ("center", 0, 0.5),
)


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

    async def dancing(
            self,
            steps: tuple[tuple[str, int, float], ...] = DANCE_STEPS,
            wait_ack: Callable[[str, int, float], Awaitable[bool]] | None = None,
            ack_timeout: float = 8.0) -> int:
        """Танцевальный паттерн: последовательно выполняет MOVE-команды.

        steps — кортежи (ось, градусы, пауза_после_команды_в_секундах);
        ось "center" отправляется как MOVE:center (градусы игнорируются).
        Команды уходят строго по очереди; при обрыве соединения цикл
        прерывается.

        wait_ack — опциональная асинхронная функция (ось, градусы, таймаут)
        -> bool, ждущая подтверждения ACK:MOVE от робота (сигнал фактического
        завершения движения, его шлёт motionTask прошивки). Если задана —
        фиксированные паузы из steps игнорируются, следующий шаг стартует
        сразу после завершения предыдущего; при таймауте цикл прерывается.

        Возвращает число успешно отправленных команд (0 — робот не на связи).
        """
        sent = 0
        for axis, deg, delay in steps:
            cmd = "MOVE:center" if axis == "center" \
                else f"MOVE:{axis}:{deg}"
            if not await self.send_text(cmd):
                logger.warning("dancing: обрыв на %s, останавливаюсь", cmd)
                break
            sent += 1
            if wait_ack is not None:
                if not await wait_ack(axis, deg, ack_timeout):
                    logger.warning("dancing: нет ACK для %s за %.1f с",
                                   cmd, ack_timeout)
                    break
            else:
                await asyncio.sleep(max(0.0, delay))
        logger.info("dancing: выполнено %d команд из %d",
                    sent, len(steps))
        return sent