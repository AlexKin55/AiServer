"""Автосмена эмоций робота при простое.

Вынесено в отдельный модуль, чтобы не загромождать state_machine.py.

EmotionController отсчитывает время от последней активности (диалога):
через 10 с после него отправляет EMOTION:neutral, ещё через 10 с —
EMOTION:sad, ещё через 10 с — EMOTION:sleepy. Любая новая активность
(touch()) перезапускает отсчёт с начала.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger("uvicorn")

# Стадии успокоения: (эмоция, доп. задержка от начала отсчёта, с).
EMOTION_DECAY_STAGES = (("neutral", 10.0), ("sad", 10.0), ("sleepy", 10.0))


class EmotionController:
    """Фоновая задача смены эмоций при простое.

    send_emotion — асинхронный колбэк (имя эмоции) -> bool, отправляющий
    команду роботу (например, bot.send_text(f"EMOTION:{name}")).
    """

    def __init__(self, send_emotion: Callable[[str], Awaitable[bool]]) -> None:
        self._send_emotion = send_emotion
        self._reset = asyncio.Event()
        self._since: float = 0.0
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Публичный API.
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Запускает фоновую задачу (идемпотентно)."""
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.create_task(self._run())
            except RuntimeError:
                # Нет запущенного event loop (вызов до старта сервера).
                self._task = None

    def stop(self) -> None:
        """Останавливает фоновую задачу."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def touch(self) -> None:
        """Фиксирует активность (диалог) и перезапускает отсчёт."""
        self._since = time.monotonic()
        self._reset.set()
        self.start()

    # ------------------------------------------------------------------
    # Внутренняя реализация.
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            # Ждём первой активности: не «успокаиваем» робота, пока он
            # вообще не пообщался.
            await self._reset.wait()
            self._reset.clear()
            elapsed = 0.0
            for name, delay in EMOTION_DECAY_STAGES:
                elapsed += delay
                wait = max(0.0, elapsed - (time.monotonic() - self._since))
                try:
                    await asyncio.wait_for(self._reset.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    # Пауза вышла — меняем эмоцию и идём к следующей стадии.
                    try:
                        ok = await self._send_emotion(name)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("emotion decay: ошибка отправки %s: %s",
                                       name, exc)
                        continue
                    logger.info("emotion decay: %s -> %s", name,
                                "ok" if ok else "НЕТ СОЕДИНЕНИЯ")
                    continue
                # Пришла новая активность — перезапускаем отсчёт с начала.
                break