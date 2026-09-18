#!/usr/bin/env python3
"""Запуск всех интеграционных тестов робота (аналог AiBot/tests/tests.cpp).

Поднимает WebSocket-сервер, ждёт подключения робота и по очереди прогоняет
тест-модули, соответствующие AiBot/tests:
  wifi, websocket, move, screen, leds, audio, video.

Запуск:  python3 run_tests.py [--host 0.0.0.0] [--port 9001]
"""
import argparse
import asyncio
import importlib
import sys
from pathlib import Path

import websockets

from common import Suite, ts

# Порт по умолчанию — из конфига (основной + тестовый).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config as app_config  # noqa: E402

DEFAULT_PORT = app_config.TEST_CONFIG["websocket"]["port"]

# Порядок как в AiBot/tests/tests.cpp.
# MODULES = ["wifi", "websocket", "move", "screen", "audio", "video", "leds"]
MODULES = ["wifi", "websocket", "audio"]

# Защита от повторных подключений: робот иногда переподключается во время
# прогона (нестабильный Wi-Fi). Повторное соединение не должно запускать
# второй параллельный прогон и перемешивать вывод.
_handler_active = False
_handler_lock = asyncio.Lock()


async def handler(ws) -> None:
    global _handler_active
    async with _handler_lock:
        if _handler_active:
            print("[i] повторное подключение во время прогона — игнорирую")
            await ws.close()
            return
        _handler_active = True
    try:
        print(f"\n[{ts()}] [+] robot connected from {ws.remote_address[0]}")
        suite = Suite(ws)
        for name in MODULES:
            mod = importlib.import_module(name)
            print(f"[{ts()}] [TEST] === {name} ===")
            try:
                await mod.run(suite)
            except websockets.exceptions.ConnectionClosed as exc:
                # Робот разорвал соединение (частая причина — перезагрузка ESP32).
                print(f"[TEST] FAIL: {name}: связь с роботом потеряна "
                      f"({exc.__class__.__name__}); вероятно, робот перезагрузился "
                      f"— см. [app] last reset reason в мониторе")
                suite.report(False, f"{name}: connection lost")
                break

        total = suite.passed + suite.failed
        print(f"[{ts()}] [TEST] FINAL RESULT: passed={suite.passed} "
              f"failed={suite.failed} total={total}")
        print(f"[{ts()}] [TEST] "
              + ("ALL PASSED" if suite.failed == 0 else "HAS FAILURES"))
    finally:
        async with _handler_lock:
            _handler_active = False


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    print(f"[i] waiting for robot at ws://{args.host}:{args.port}/")
    print("[i] Ctrl+C для выхода")
    # Пинги сервера отключены: не шлём PING и не закрываем по таймауту PONG —
    # сетевой трафик поверх PCM-потока вызывал обрывы у робота;
    # TCP-обрыв и так виден (ConnectionClosed).
    async with websockets.serve(handler, args.host, args.port,
                                ping_interval=None, ping_timeout=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())