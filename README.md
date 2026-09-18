# Сервер-клиент для робота AIBot (WebSocket)

Принимает подключение робота по WebSocket и обрабатывает запись с его
микрофона. **Единственный сценарий — VAD**: робот сам слушает микрофон,
по резкому росту шума (голос) шлёт текстовую команду `RECORD:start`,
передаёт **PCM-чанки** (16 кГц/моно, int16 LE), а по тишине (~0.9 с) или
таймауту сегмента (5 с) шлёт `RECORD:stop`. Сервер собирает запись,
сохраняет `.wav`, ретранслирует аудио в Yandex (STT -> GPT -> TTS)
и озвучивает ответ роботу тем же PCM-чанком (codec 1).

## Структура
```
AiServer/
├── config/
│   ├── settings.json       # основной конфиг (ws, audio, recording, yandex)
│   └── test_settings.json  # параметры тестов (таймауты)
├── scripts/
│   ├── run_server.sh       # запуск uvicorn
│   ├── run_tests.sh        # интеграционные тесты
│   └── run_yandex_test.sh  # тест Yandex STT на готовом PCM/WAV-файле
├── src/
│   ├── __init__.py         # пакет src
│   ├── server.py           # точка входа FastAPI (WS + HTTP)
│   ├── robot.py            # WebSocket-сессия робота (отправка команд)
│   ├── recorder.py         # накопление PCM-чанков, сохранение .wav
│   ├── state_machine.py    # state machine: DISCONNECTED / IDLE / RECORDING
│   └── yandex.py           # STT (SpeechKit v3) + GPT + TTS (SpeechKit v3)
├── testYandexApi/          # тест STT без робота (PCM/WAV-файл -> текст)
├── tests/                  # интеграционные тесты через протокол WS
├── requirements.txt
└── README.md
```

State machine сервера:
```
DISCONNECTED -(робот подключился)-> IDLE
IDLE -(RECORD:start от робота)-> RECORDING    (VAD: робот определил речь)
RECORDING -(RECORD:stop от робота)-> IDLE     (VAD: тишина/таймаут + финализация)
любое -(робот отключился)-> DISCONNECTED
```

Финализация (`state_machine._finalize`): сохранение записи в `records/`
(`.wav` — тот же PCM, что уходит в Yandex STT), затем при наличии кредов
Yandex — распознавание -> YandexGPT -> синтез речи (TTS) -> отправка роботу
бинарного фрейма `[тип][кодек=1][pcm int16 LE]` на озвучку.

## Конфигурация

Конфиги в каталоге [`config/`](config):
- [`settings.json`](config/settings.json) — основной (сервер, тесты, скрипты):
  Wi-Fi робота (информационно), параметры WebSocket (`host`/`port`/`path`),
  формат аудиофреймов (codec 1 = PCM, единственный), размер чанка
  (`audio_chunk_seconds`), каталог записей (`recording.record_dir`),
  креды Yandex Cloud (`yandex.api_key`/`yandex.folder_id`/`system_prompt`/
  `tts_voice`/`tts_speed`/`tts_role`);
- [`test_settings.json`](config/test_settings.json) — **только параметры тестов**
  (таймауты ожидания ответов робота, файл для теста STT), наслаивается поверх
  основного.

Использование: сервер — `src/config.py::CONFIG`, тесты — `src/config.py::TEST_CONFIG`
(основной + тестовый). Пути переопределяются переменными окружения
`AISERVER_CONFIG` и `AISERVER_TEST_CONFIG`. Отсутствующие ключи дополняются
значениями по умолчанию из `src/config.py`. В любом строковом значении JSON
поддерживается подстановка переменных окружения вида **`${VAR}`** (например,
`"api_key": "${YANDEX_API_KEY}"`); незаданная переменная даёт пустую строку.

### Yandex Cloud — полный голосовой цикл (как у Алисы)

Сценарий повторяет архитектуру голосовых ассистентов:
`аудио вопроса -> STT (текст) -> YandexGPT (текстовый ответ) -> TTS (PCM) ->
робот озвучивает ответ на динамике`.

Модуль [`src/yandex.py`](src/yandex.py):
- `recognize_pcm(pcm)` — SpeechKit STT v3 `RecognizeStreaming` (gRPC,
  официальный пример): PCM отправляется как raw `LINEAR16_PCM` чанками
  `AudioChunk` (по 4000 байт); текст собирается из событий
  `final`/`final_refinement`;
- `ask_gpt()` — YandexGPT v3 (REST `foundationModels/v1/completion`) -> ответ;
- **`ask_with_pcm(pcm, system_prompt=None)`** — единый шаг «аудио + промпт ->
  текстовый ответ»: распознаёт PCM, отправляет текст в GPT с системным промптом,
  возвращает кортеж `(recognized, answer)`. Промпт по умолчанию — из
  `yandex.system_prompt` конфига.
- `synthesize(text)` — SpeechKit TTS v3 (`utteranceSynthesis`, PCM 16 кГц/моно);
  полученный PCM отправляется роботу **без перекодирования** (codec 1).

Где используется:
- прод-сервер: по `RECORD:stop` вызывает `ask_with_pcm()`, озвучивает `answer`
  роботу **PCM-чанком (codec 1)**, результат кладёт в `sm.last_answer`
  (доступен через `GET /ask`);
- интеграционный тест `tests/audio.py`: имитирует сервер — ждёт `RECORD:start`,
  собирает чанки до `RECORD:stop`, сохраняет `.wav`, вызывает `ask_with_pcm()`
  и отправляет синтезированный ответ роботу PCM-чанком.

Креды задаются переменными окружения **`YANDEX_API_KEY`** (API-ключ
сервисного аккаунта — для STT/GPT/TTS) и **`YANDEX_FOLDER_ID`** (приоритет).
В [`settings.json`](config/settings.json) секция `yandex` ссылается на них через
подстановку `${VAR}` — при загрузке конфига строки вида `${ИМЯ_ПЕРЕМЕННОЙ}`
заменяются значением из окружения, поэтому креды хранятся только в env:

```bash
export YANDEX_API_KEY="..."
export YANDEX_FOLDER_ID="..."
./scripts/run_server.sh
```

Без кредов шаг Yandex пропускается (запись сохраняется, ошибкой не считается).

## Установка
```bash
cd ~/Work/AiServer
python3 -m pip install -r requirements.txt
# Для Yandex STT нужны grpcio и yandexcloud (уже в requirements.txt).
```

## Запуск
```bash
cd ~/Work/AiServer
./scripts/run_server.sh                 # или uvicorn src.server:app --host 0.0.0.0 --port 9001
```
Порт должен совпадать с `WS_PORT` робота (по умолчанию `9001`), адрес — с
`WS_HOST` в `config/config.h` робота. WS-путь сервера — `/`
(совпадает с `WS_PATH` робота по умолчанию).

Каталог записей задаётся переменной окружения `RECORD_DIR` (по умолчанию
`records` рядом со скриптом).

## Использование

Робот должен быть запущен и подключиться к серверу (в логе появится
`Робот подключён: <ip>`, состояние станет `idle`).

Ничего делать не нужно — робот сам слушает микрофон в состоянии `ready`:

```
робот  → RECORD:start          (по резкому росту шума/голосу)
робот  → [бинарные PCM-чанки]  (codec 1, чанки ~1 с)
робот  → RECORD:stop           (тишина ~0.9 с или сегмент 5 с)
сервер → [бинарный PCM-чанк]   (codec 1: STT -> GPT -> TTS, озвучка)
```

В логе робота при этом: `[vad] speech started -> RECORD:start` /
`[vad] speech ended -> RECORD:stop`.

Полезные HTTP-эндпоинты:

```bash
# Последний текстовый ответ YandexGPT на вопрос из последнего VAD-сегмента
curl http://127.0.0.1:9001/ask
# → {"recognized":"...","answer":"..."}

# Состояние
curl http://127.0.0.1:9001/health
# → {"state":"recording","robot_connected":true}
```

## Формат фрейма аудио

**Codec 1 — PCM** (единственный формат, в обе стороны):
```
byte[0]   = 1   (тип: AUDIO)
byte[1]   = 1   (кодек: PCM)
byte[2..] = сырые сэмплы int16 LE (16 кГц, моно)
```
Робот -> сервер — чанками ~1 с (по `MIC_AUDIO_CHUNK_SECONDS`); сервер -> робот
(озвучка ответа) — тот же формат: PCM от TTS без перекодирования, робот
воспроизводит его в фоновой задаче `playbackTask`. Файл `.wav` сохраняется как
этот же PCM с RIFF-заголовком. Компрессии в канале WebSocket нет.

## Heartbeat

Робот в состоянии `READY` шлёт текстовый фрейм `HB` каждые 15 с (текстовый
кадр, не аудио) — чтобы роутер/NAT не сбрасывал простаивающую TCP-сессию за
~2 минуты без трафика (иначе сервер не может достучаться с озвучкой после
долгого цикла STT → GPT → TTS). Сервер **только логирует** `HB` (`[hb] от
робота` в тестах, `HB от робота` в боевом сервере) и **не обрывает**
соединение, если HB перестал приходить: online/offline определяется фактом
соединения.

## Тест Yandex STT без робота

Распознавание готового аудиофайла (`.pcm` — сырой PCM, или `.wav`):

```bash
cd ~/Work/AiServer
export YANDEX_API_KEY="..." YANDEX_FOLDER_ID="..."
./scripts/run_yandex_test.sh                      # файл из конфига
./scripts/run_yandex_test.sh --file path.wav      # файл явно
```

Файл по умолчанию — `tests.yandex_api_file` из `config/test_settings.json`
(сейчас `test_records/in_robot_audio_example.pcm`).

## Интеграционные тесты робота через протокол

Каталог `tests/` повторяет структуру `AiBot/tests`: каждый файл проверяет
аналогичный функционал, но команды отправляются через протокол WebSocket.

| AiBot/tests | AiServer/tests | Что проверяется |
|-------------|----------------|-----------------|
| test_util.h | common.py      | харнесс (Suite: report/send/expect/recv_msg) |
| tests.cpp   | run_tests.py   | раннер: WS-сервер + прогон всех модулей |
| wifi.cpp    | wifi.py        | подключение робота (Wi-Fi неявно) |
| websocket.cpp | websocket.py | PING -> PONG; HB логируется (соединение не обрывается) |
| move.cpp    | move.py        | MOVE:left/right/up/down (±180), center |
| screen.cpp  | screen.py      | EMOTION: все эмоции |
| audio.cpp   | audio.py       | VAD: робот сам шлёт RECORD:start по голосу, сервер собирает PCM-чанки (codec 1) до RECORD:stop, сохраняет .wav, распознавание в Yandex STT -> YandexGPT -> озвучка ответа роботу PCM-чанком (codec 1) |
| video.cpp   | video.py       | SKIP (нет команды CAMERA:* в протоколе) |
| leds.cpp    | leds.py        | LED:<r>,<g>,<b> (цвета + off) |

```bash
cd ~/Work/AiServer
./scripts/run_tests.sh                  # или cd tests && python3 run_tests.py --host 0.0.0.0 --port 9001
```
Робот должен работать на **прошивке приложения** (`src/aibot`, не тестовой!)
и иметь в `config/config.h` `WS_HOST` = адрес этой машины. Вывод —
в стиле прошивки: `[TEST] PASS/FAIL` и `FINAL RESULT`.
