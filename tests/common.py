"""Общий харнесс интеграционных тестов робота (аналог AiBot/tests/test_util.h).

Suite: отправка команд по протоколу, ожидание ответов с таймаутом и подсчёт
PASS/FAIL в стиле прошивки. Константы (формат аудиофрейма, таймауты) берутся
из единого конфига AiServer/config/settings.json.
"""
import asyncio
import sys
import time
from pathlib import Path


def ts() -> str:
    """Текущее время с миллисекундами (HH:MM:SS.mmm) для логов сервера."""
    t = time.time()
    base = int(t)
    return (f"{time.strftime('%H:%M:%S', time.localtime(base))}."
            f"{int((t - base) * 1000):03d}")

import websockets

# Позволяет импортировать пакет src (config) из каталога tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config as app_config  # noqa: E402

# Тесты используют TEST_CONFIG (основной конфиг + test_settings.json).
CONF = app_config.TEST_CONFIG

AUDIO_TYPE = CONF["audio"]["frame_type"]
AUDIO_CODEC_PCM = CONF["audio"]["codec_pcm"]
SAMPLE_RATE = CONF["audio"]["sample_rate"]

DEFAULT_TIMEOUT = CONF["tests"]["ack_timeout_s"]
MOVE_TIMEOUT = CONF["tests"]["move_timeout_s"]
AUDIO_TIMEOUT = CONF["tests"]["audio_timeout_s"]
RECORD_SECONDS = CONF["tests"]["record_seconds"]
AUDIO_CHUNK_SECONDS = CONF["tests"]["audio_chunk_seconds"]
RECORD_DIR = CONF["tests"]["record_dir"]


class Suite:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.passed = 0
        self.failed = 0

    def _log_hb(self, msg) -> None:
        """Heartbeat робота ("HB" каждые 15 с): только логируем и пропускаем —
        соединение НЕ обрывается при отсутствии HB."""
        if isinstance(msg, str) and msg.strip().upper() == "HB":
            print(f"[{ts()}] [hb] от робота")

    def report(self, ok: bool, name: str, extra: str = "") -> None:
        tag = "PASS" if ok else "FAIL"
        line = f"[TEST] {tag}: {name}"
        if extra:
            line += f" - {extra}"
        print(line)
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    async def send(self, cmd: str) -> bool:
        """Отправляет команду. False — соединение с роботом закрыто."""
        try:
            await self.ws.send(cmd)
            return True
        except websockets.exceptions.ConnectionClosed:
            return False

    async def expect(self, prefix: str | None = None,
                     timeout: float = DEFAULT_TIMEOUT, binary: bool = False):
        """Ждёт сообщение с префиксом prefix (или бинарное при binary=True).

        Пропускает нерелевантные сообщения (служебный текст, чужие фреймы).
        Возвращает сообщение либо None по таймауту.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            except websockets.exceptions.ConnectionClosed:
                return None
            if binary:
                if isinstance(msg, bytes):
                    return msg
                continue
            if isinstance(msg, str):
                self._log_hb(msg)
                if prefix is not None and msg.startswith(prefix):
                    return msg
                continue
            # бинарное сообщение во время ожидания текста — пропускаем

    async def recv_msg(self, timeout: float = DEFAULT_TIMEOUT):
        """Возвращает очередное сообщение без фильтрации.

        Формат: (is_binary, msg), где msg — bytes или str.
        (None, None) — по таймауту или при закрытии соединения.
        """
        try:
            msg = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        except (asyncio.TimeoutError,
                websockets.exceptions.ConnectionClosed):
            return None, None
        if isinstance(msg, bytes):
            return True, msg
        self._log_hb(msg)
        return False, msg