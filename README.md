# Сервер-клиент для робота AIBot (WebSocket)

Принимает подключение робота по WebSocket и управляет записью с его микрофона
командами `AUDIO:start` / `AUDIO:stop` (протокол см. `docs/protocol.md`
проекта AiBot). Принятый PCM (16 кГц, моно, int16) сохраняется в WAV.

## Структура
```
AiServer/
├── src/
│   ├── __init__.py        # пакет src
│   ├── server.py          # точка входа FastAPI (WS + HTTP-эндпоинты)
│   ├── robot.py           # WebSocket-сессия робота (отправка команд)
│   ├── recorder.py        # накопление PCM и сохранение WAV
│   └── state_machine.py   # state machine: DISCONNECTED / IDLE / RECORDING
├── requirements.txt
└── README.md
```

State machine сервера:
```
DISCONNECTED -(робот подключился)-> IDLE
IDLE -(POST /record/start)-> RECORDING     (шлёт роботу AUDIO:start)
RECORDING -(POST /record/stop)-> IDLE      (шлёт AUDIO:stop, сохраняет WAV)
любое -(робот отключился)-> DISCONNECTED
```

## Конфигурация

Конфиги в каталоге [`config/`](config):
- [`settings.json`](config/settings.json) — основной (сервер, тесты, скрипты):
  Wi-Fi робота (информационно), параметры WebSocket (`host`/`port`/`path`),
  формат аудиофреймов, каталог записей;
- [`test_settings.json`](config/test_settings.json) — **только параметры тестов**
  (таймауты ожидания ответов робота), наслаивается поверх основного.

Использование: сервер — `src/config.py::CONFIG`, тесты — `src/config.py::TEST_CONFIG`
(основной + тестовый). Пути переопределяются переменными окружения
`AISERVER_CONFIG` и `AISERVER_TEST_CONFIG`. Отсутствующие ключи дополняются
значениями по умолчанию из `src/config.py`.

## Установка
```bash
cd ~/Work/AiServer
python3 -m pip install -r requirements.txt
```

## Запуск
```bash
cd ~/Work/AiServer
./scripts/run_server.sh                 # или uvicorn src.server:app --host 0.0.0.0 --port 9001
```
Порт должен совпадать с `WS_PORT` робота (по умолчанию `9001`), адрес — с
`WS_HOST` в `include/app_config.h` робота. WS-путь сервера — `/`
(совпадает с `WS_PATH` робота по умолчанию).

Каталог записей задаётся переменной окружения `RECORD_DIR` (по умолчанию
`records` рядом со скриптом).

## Использование

Робот должен быть запущен и подключиться к серверу (в логе появится
`Робот подключён: <ip>`, состояние станет `idle`).

```bash
# Начать запись с микрофона робота
curl -X POST http://127.0.0.1:9001/record/start
# → {"status":"recording"}

# ... робот шлёт PCM-фреймы, они накапливаются ...

# Остановить запись и сохранить WAV
curl -X POST http://127.0.0.1:9001/record/stop
# → {"status":"saved","file":"records/robot_mic_....wav","bytes":N,"samples":M,"seconds":S}

# Состояние
curl http://127.0.0.1:9001/health
# → {"state":"recording","robot_connected":true}
```

## Формат фрейма аудио
```
byte[0]   = 1   (тип: AUDIO)
byte[1]   = 1   (кодек: PCM int16)
byte[2..] = PCM little-endian, int16 моно, 16 кГц
```

## Интеграционные тесты робота через протокол

Каталог `tests/` повторяет структуру `AiBot/tests`: каждый файл проверяет
аналогичный функционал, но команды отправляются через протокол WebSocket.

| AiBot/tests | AiServer/tests | Что проверяется |
|-------------|----------------|-----------------|
| test_util.h | common.py      | харнесс (Suite: report/send/expect) |
| tests.cpp   | run_tests.py   | раннер: WS-сервер + прогон всех модулей |
| wifi.cpp    | wifi.py        | подключение робота (Wi-Fi неявно) |
| websocket.cpp | websocket.py | PING -> PONG, STATUS -> STATUS:online |
| move.cpp    | move.py        | MOVE:left/right/up/down (±180), center |
| screen.cpp  | screen.py      | EMOTION: все эмоции |
| audio.cpp   | audio.py       | AUDIO:start -> захват звука: робот кодирует PCM в Opus (20 мс) и шлёт чанками по audio_chunk_seconds (2 c; редкие всплески Wi-Fi не мешают I2S); сервер сохраняет Opus-пакеты как есть в tests.record_dir (*.opus) -> AUDIO:stop -> досылка последнего чанка -> возврат чанков роботу, он декодирует и проигрывает |
| video.cpp   | video.py       | SKIP (нет команды CAMERA:* в протоколе) |
| leds.cpp    | leds.py        | LED:<r>,<g>,<b> (цвета + off) |

```bash
cd ~/Work/AiServer
./scripts/run_tests.sh                  # или cd tests && python3 run_tests.py --host 0.0.0.0 --port 9001
```
Робот должен работать на **прошивке приложения** (`src/aibot`, не тестовой!)
и иметь в `include/app_config.h` `WS_HOST` = адрес этой машины. Вывод —
в стиле прошивки: `[TEST] PASS/FAIL` и `FINAL RESULT`.# AiServer
