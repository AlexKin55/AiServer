#!/usr/bin/env bash
# Запуск интеграционных тестов робота через протокол WebSocket.
#
# Поднимает WS-сервер (tests/run_tests.py), ждёт подключения робота и прогоняет
# тест-модули: wifi, websocket, move, screen, audio, video, leds.
# Адрес/порт — переменными HOST/PORT, интерпретатор — PY.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-9001}"
PY="${PY:-python3}"

# Креды Yandex НЕ хардкодим: они берутся из окружения (YANDEX_API_KEY /
# YANDEX_FOLDER_ID) или секции yandex в config/settings.json.
if [[ -z "${YANDEX_API_KEY:-}" || -z "${YANDEX_FOLDER_ID:-}" ]]; then
    echo "[tests] предупреждение: YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы — \
STT/GPT/TTS будут недоступны" >&2
fi

cd "$ROOT_DIR"
# Порт по умолчанию — из config/settings.json.
if [[ -z "${PORT:-}" ]]; then
    PORT="$("$PY" -c 'import os,sys; sys.path.insert(0, os.getcwd()); from src import config; print(config.CONFIG["websocket"]["port"])' 2>/dev/null || echo 9001)"
fi
cd "$ROOT_DIR/tests"
echo "[tests] ожидаю подключение робота на ws://$HOST:$PORT/ ..."
exec "$PY" run_tests.py --host "$HOST" --port "$PORT"
