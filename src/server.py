"""Сервер-клиент для робота AIBot (WebSocket) — точка входа FastAPI.

Робот подключается как WebSocket-клиент к ws://HOST:PORT/
(адрес задаётся в config/config.h робота: WS_HOST/WS_PORT/WS_PATH).
Логика вынесена в модули пакета src/:
  robot.py        — WebSocket-сессия робота (отправка команд);
  recorder.py     — накопление PCM-чанков и сохранение .wav;
  state_machine.py— state machine приёма/отправки команд.

Сценарий — VAD (только): робот сам слушает микрофон в состоянии ready и по
голосу шлёт текстовые команды RECORD:start / RECORD:stop, между ними —
PCM-чанки [тип][кодек=1][pcm int16 LE, 16 кГц/моно]. Сервер собирает запись,
ретранслирует её в Yandex (STT -> GPT -> TTS) и озвучивает ответ роботу
тем же PCM-чанком (codec 1).

HTTP-интерфейс:
  GET  /ask      -> последний текстовый ответ YandexGPT;
  GET  /health   -> состояние (state, robot_connected).

Запуск:  uvicorn server:app --host 0.0.0.0 --port 9001
"""
import logging

from fastapi import FastAPI, HTTPException, WebSocket

from . import config as app_config
from . import recorder as recorder_mod
from . import robot as robot_mod
from .state_machine import SessionStateMachine

logger = logging.getLogger("uvicorn")

# Формат аудиофреймов — из конфига (1 = сырой PCM; канал только PCM).
AUDIO_TYPE = app_config.CONFIG["audio"]["frame_type"]
AUDIO_CODEC_PCM = app_config.CONFIG["audio"]["codec_pcm"]

# Глобальная сессия с роботом.
bot = robot_mod.RobotSession()
rec = recorder_mod.Recorder(record_dir=app_config.CONFIG["recording"]["record_dir"])
sm = SessionStateMachine(bot, rec)

app = FastAPI(title="AIBot WebSocket Client")


@app.websocket("/")
async def ws_endpoint(ws: WebSocket):
    """Принимает соединение робота и раздаёт его события state machine."""
    await ws.accept()
    await bot.attach(ws)
    await sm.on_connected()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                if len(data) >= 2 and data[0] == AUDIO_TYPE \
                        and data[1] == AUDIO_CODEC_PCM:
                    # Робот шлёт PCM-чанки (codec 1): тело фрейма — сырые
                    # сэмплы int16 LE (16 кГц/моно), их накапливает Recorder.
                    await sm.on_audio(data[2:])
                else:
                    logger.info("[binary] %d байт, заголовок=%s",
                                len(data), data[:2].hex())
                continue
            text = msg.get("text")
            if text is not None:
                # Ждать хвостовые чанки не нужно: к моменту RECORD:stop все
                # PCM-чанки речи уже получены (робот шлёт после стопа только
                # остаток тишины), поэтому сразу отдаём команду финализации.
                await sm.on_text(text)
    finally:
        await sm.on_disconnected()
        await bot.detach()


@app.get("/ask")
async def ask():
    """Последний текстовый ответ YandexGPT на вопрос из аудио.

    Заполняется после VAD-сегмента (RECORD:start -> PCM-чанки ->
    RECORD:stop): сервер сам распознаёт аудио и формирует промпт.
    Возвращается готовый текст ответа.
    """
    if not sm.last_answer:
        raise HTTPException(404, "нет результатов распознавания")
    return sm.last_answer


@app.get("/health")
async def health():
    return {"state": sm.state.value, "robot_connected": bot.connected}