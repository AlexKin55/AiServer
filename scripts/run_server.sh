#!/usr/bin/env bash
# Запуск сервера-клиента робота AIBot (WebSocket).
#
# Адрес/порт можно переопределить переменными HOST/PORT:
#   HOST=127.0.0.1 PORT=9002 ./scripts/run_server.sh
# Интерпретатор — переменной PY (по умолчанию python3).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-9001}"
PY="${PY:-python3}"


# Креды Yandex НЕ хардкодим: они берутся из окружения (YANDEX_API_KEY /
# YANDEX_FOLDER_ID) или секции yandex в config/settings.json.
if [[ -z "${YANDEX_API_KEY:-}" || -z "${YANDEX_FOLDER_ID:-}" ]]; then
    echo "[server] предупреждение: YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы — \
STT/GPT/TTS будут недоступны" >&2
fi

cd "$ROOT_DIR"
# Порт по умолчанию — из config/settings.json.
if [[ -z "${PORT:-}" ]]; then
    PORT="$("$PY" -c 'import os,sys; sys.path.insert(0, os.getcwd()); from src import config; print(config.CONFIG["websocket"]["port"])' 2>/dev/null || echo 9001)"
fi
echo "[server] запуск uvicorn на ws://$HOST:$PORT/ ..."
exec "$PY" -m uvicorn src.server:app --host "$HOST" --port "$PORT" \
    --log-config "$ROOT_DIR/config/logging.json"