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
import asyncio
import logging
import time

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


# Сколько ждать последний неполный PCM-чанк после RECORD:stop.
TAIL_WAIT_SECONDS = 2.5


async def _feed_tail(sm: SessionStateMachine, ws: WebSocket) -> None:
    """Дочитывает хвостовые PCM-чанки после текстовой команды RECORD:stop.

    Робот по завершении сегмента (тишина/таймаут) сначала шлёт RECORD:stop,
    а сразу за ним — последний неполный чанк (до 2 с аудио). Порядок TCP
    гарантирован, но финализация (Yandex STT) занимает секунды, поэтому чанк
    нужно забрать здесь, иначе конец фразы потеряется.
    """
    deadline = time.monotonic() + TAIL_WAIT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            return
        if msg["type"] == "websocket.disconnect":
            return
        data = msg.get("bytes")
        if data is not None and len(data) >= 2 \
                and data[0] == AUDIO_TYPE and data[1] == AUDIO_CODEC_PCM:
            await sm.on_audio(data[2:])
            # Чанк получен — даём ещё короткое окно на доп. пакеты.
            deadline = min(deadline, time.monotonic() + 0.6)


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
                if text.strip().upper() == "RECORD:STOP":
                    # Забрать хвостовой чанк до финализации (VAD-режим).
                    await _feed_tail(sm, ws)
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