#!/usr/bin/env bash
# Тест Yandex API (testYandexApi/yandex_test.py): распознавание готового
# PCM/WAV-файла (STT v3 RecognizeStreaming), при --send — полный цикл
# STT -> GPT -> TTS с отправкой озвучки роботу, при --play — отправка
# готового аудиофайла роботу (без Yandex).
#
# Файл берётся из config/test_settings.json (tests.yandex_api_file, путь
# относительно корня AiServer) либо передаётся аргументом --file:
#   ./scripts/run_yandex_test.sh                     # файл из конфига
#   ./scripts/run_yandex_test.sh --file path.wav     # файл явно
#   ./scripts/run_yandex_test.sh --send --file x.wav # + озвучка роботу
#   ./scripts/run_yandex_test.sh --play --file x.wav # файл на робота
#
# Креды Yandex должны быть в окружении (или в config/settings.json):
#   export YANDEX_API_KEY="..." YANDEX_FOLDER_ID="..."
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-python3}"

if [[ -z "${YANDEX_API_KEY:-}" || -z "${YANDEX_FOLDER_ID:-}" ]]; then
    echo "[yandex] предупреждение: YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы \
в окружении (можно указать в config/settings.json, секция yandex)" >&2
fi

cd "$ROOT_DIR"
exec "$PY" testYandexApi/yandex_test.py "$@"