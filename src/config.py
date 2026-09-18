"""Загрузка настроек AiServer из config/*.json.

Основной конфиг:      config/settings.json      (переменная AISERVER_CONFIG)
Тестовый конфиг:      config/test_settings.json (переменная AISERVER_TEST_CONFIG)

CONFIG — основной конфиг (использует сервер).
TEST_CONFIG — тестовый конфиг, наслоенный поверх основного (используют тесты).

Отсутствующие ключи дополняются значениями по умолчанию. В любом строковом
значении JSON поддерживается подстановка переменных окружения вида ${VAR}
(например, "${YANDEX_API_KEY}"); если переменная не задана — подставляется
пустая строка.
"""
import json
import os
import re
from pathlib import Path
from typing import Any, Dict

# Дефолты основного конфига (без тестовых параметров — они только в
# test_settings.json / TEST_CONFIG).
_DEFAULTS: Dict[str, Any] = {
    "wifi": {"ssid": "", "pass": ""},
    "websocket": {"host": "0.0.0.0", "port": 9001, "path": "/"},
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
        "bits_per_sample": 16,
        "frame_type": 1,
        "codec_pcm": 1,
    },
    "recording": {"record_dir": "records"},
    "yandex": {
        "_comment": "Креды Yandex Cloud (API-ключ + folder_id).",
        "api_key": "",
        "folder_id": "",
        "system_prompt": "Ты умный ИИ-ассистент. Пользователь задал тебе вопрос голосом (распознанный текст ниже). Ответь на вопрос кратко, понятно и по-русски.",
        "tts_voice": "alena",
        "tts_speed": 1.05,
        "tts_role": "good",
    },
}

# Дефолты тестовых параметров (наслаиваются только в TEST_CONFIG).
_TEST_DEFAULTS: Dict[str, Any] = {
    "tests": {
        "ack_timeout_s": 5.0,
        "audio_timeout_s": 10.0,
        "move_timeout_s": 8.0,
        "record_seconds": 5.0,
        "audio_chunk_seconds": 2.0,
        "record_dir": "test_records",
        "yandex_api_file": "",
    },
}


def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _default_path(name: str) -> str:
    return str(_config_dir() / name)


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


# Подстановка ${VAR} во всех строковых значениях JSON.
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Рекурсивно заменяет ${VAR} в строках на значения из окружения."""
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            return os.environ.get(match.group(1), "")
        return _ENV_RE.sub(repl, value)
    return value


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return _expand_env(json.load(f))
    except FileNotFoundError:
        return {}


def load() -> Dict[str, Any]:
    """Основной конфиг (config/settings.json)."""
    path = os.environ.get("AISERVER_CONFIG", _default_path("settings.json"))
    return _merge(_DEFAULTS, _read(path))


def load_with_test() -> Dict[str, Any]:
    """Основной конфиг + тестовые параметры (config/test_settings.json)."""
    cfg = load()
    path = os.environ.get("AISERVER_TEST_CONFIG",
                          _default_path("test_settings.json"))
    test_data = _merge(_TEST_DEFAULTS, _read(path))
    return _merge(cfg, test_data)


CONFIG: Dict[str, Any] = load()
TEST_CONFIG: Dict[str, Any] = load_with_test()