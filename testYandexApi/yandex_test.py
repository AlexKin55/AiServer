#!/usr/bin/env python3
"""Тест Yandex API без робота: распознавание готового PCM/WAV-файла,
генерация ответа GPT и синтез речи, с опциональной отправкой озвучки роботу.

Режимы:
  yandex_test.py                  — STT: распознать файл (печать текста);
  yandex_test.py --send           — STT -> GPT -> TTS и отправить озвучку
                                    роботу по WebSocket;
  yandex_test.py --play           — отправить роботу готовый аудиофайл
                                    (без Yandex; проверка динамика).

Имя файла по умолчанию — config/test_settings.json (tests.yandex_api_file,
путь относительно корня AiServer); переопределяется --file. Поддерживаются
.wav (RIFF-заголовок разбирается) и сырой .pcm.

Отправка роботу: утилита поднимает WebSocket-сервер на host/port из
config/settings.json (секция websocket, по умолчанию 0.0.0.0:9001), ждёт
подключения робота и шлёт PCM-фреймы [тип][кодек=1][pcm] чанками
audio.play_chunk_seconds — как боевой сервер: WS-клиент робота не принимает
фреймы >~8 КБ. Завершает пустым фреймом-маркером конца потока.

Запуск:
  python3 testYandexApi/yandex_test.py [--file path/file.wav] [--send] [--play]
"""
import argparse
import asyncio
import logging
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")

from src import config as app_config  # noqa: E402
from src import yandex  # noqa: E402


def read_pcm(path: Path) -> bytes:
    """Читает аудио как сырой PCM (int16 LE): WAV разбирается, иначе — как есть."""
    if path.read_bytes()[:4] == b"RIFF":
        with wave.open(str(path), "rb") as w:
            return w.readframes(w.getnframes())
    return path.read_bytes()


async def _send_to_robot(pcm: bytes, host: str, port: int,
                         timeout_s: float = 60.0) -> None:
    """Поднимает WS-сервер, ждёт подключения робота и шлёт PCM-фреймы.

    Чанкование как в боевом сервере (src/state_machine._send_pcm_chunks):
    куски audio.play_chunk_seconds с паузой play_speed + пустой фрейм-маркер
    конца потока (робот по нему возвращает микрофон/VAD).
    """
    import websockets

    cfg = app_config.CONFIG["audio"]
    rate = int(cfg["sample_rate"])
    frame_type = int(cfg["frame_type"])
    codec_pcm = int(cfg["codec_pcm"])
    play_secs = float(cfg.get("play_chunk_seconds", 0.05))
    play_speed = float(cfg.get("play_speed", 1.0))
    if play_secs <= 0 or play_secs > 10:
        play_secs = 0.05
    if play_speed <= 0 or play_speed > 10:
        play_speed = 1.0
    chunk_size = round(rate * play_secs) * 2  # N с PCM (int16 LE, моно)
    chunk_dur = chunk_size / (2 * rate)

    delivered = asyncio.Event()

    async def handler(ws):
        print(f"[robot] подключился {ws.remote_address[0]}")
        n_chunks = (len(pcm) + chunk_size - 1) // chunk_size
        print(f"[robot] шлю {len(pcm)} B PCM: {n_chunks} чанков по "
              f"{chunk_size} B ({chunk_dur:.2f} s), speed x{play_speed:.2f}")
        for idx in range(n_chunks):
            part = pcm[idx * chunk_size:(idx + 1) * chunk_size]
            await ws.send(bytes((frame_type, codec_pcm)) + part)
            await asyncio.sleep(chunk_dur / play_speed)
        await ws.send(bytes((frame_type, codec_pcm)))  # маркер конца
        print("[robot] озвучка отправлена (чанки + маркер конца)")
        delivered.set()
        await asyncio.sleep(2)  # дать роботу доиграть хвост буфера

    async with websockets.serve(handler, host, port,
                                ping_interval=None, ping_timeout=None):
        print(f"[robot] жду робота на ws://{host}:{port}/ ...")
        try:
            await asyncio.wait_for(delivered.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            print(f"[robot] робот не подключился за {timeout_s:.0f} с")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", default=None,
        help="путь к .wav/.pcm-файлу (по умолчанию tests.yandex_api_file "
             "из конфига)")
    parser.add_argument("--language", default="ru-RU", help="код языка")
    parser.add_argument("--send", action="store_true",
                        help="STT -> GPT -> TTS и отправить озвучку роботу")
    parser.add_argument("--play", action="store_true",
                        help="отправить роботу готовый аудиофайл без Yandex")
    parser.add_argument("--ws-host", default=None,
                        help="адрес WS-сервера (по умолчанию из конфига)")
    parser.add_argument("--ws-port", type=int, default=None,
                        help="порт WS-сервера (по умолчанию из конфига)")
    args = parser.parse_args()

    ws_cfg = app_config.CONFIG["websocket"]
    host = args.ws_host or ws_cfg["host"]
    port = args.ws_port or int(ws_cfg["port"])

    cfg_path = args.file
    if not cfg_path:
        cfg_path = app_config.TEST_CONFIG["tests"].get("yandex_api_file", "")
    if not cfg_path:
        print("Не задан аудиофайл. Укажите tests.yandex_api_file в "
              "config/test_settings.json или передайте --file <путь>")
        return 2
    path = Path(cfg_path)
    if not path.is_absolute():
        path = ROOT / path

    # Режим --play: просто отправить готовый файл роботу (проверка динамика).
    if args.play:
        if not path.is_file():
            print(f"Файл не найден: {path}")
            return 2
        pcm = read_pcm(path)
        print(f"[robot] файл: {path} ({len(pcm)} B PCM)")
        asyncio.run(_send_to_robot(pcm, host, port))
        return 0

    if not path.is_file():
        # Нет готового файла: синтезируем образец через TTS (нужны креды) —
        # так тест всегда можно прогнать, а файл остаётся в WAV для плеера.
        print(f"Файл не найден: {path}")
        print("[yandex] пробую синтезировать тестовый образец через TTS ...")
        pcm = yandex.synthesize("Тест распознавания речи в облаке Яндекса.")
        if not pcm:
            print("Синтез не удался (проверьте креды/сеть). "
                  "Укажите --file с готовым WAV/PCM-файлом.")
            return 2
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        print(f"[yandex] создан образец: {path} ({len(pcm)} байт PCM)")

    if not yandex.credentials_ok():
        print("Креды Yandex не заданы: нужны YANDEX_API_KEY / YANDEX_FOLDER_ID")
        return 2

    pcm = read_pcm(path)
    print(f"[yandex] распознаю файл: {path} ({len(pcm)} байт PCM)")
    print("[yandex] отправка в SpeechKit STT v3 RecognizeStreaming ...")

    t0 = time.monotonic()
    try:
        text = yandex.recognize_pcm(pcm, language_code=args.language)
    except Exception as exc:  # noqa: BLE001
        print(f"[yandex] ОШИБКА: {type(exc).__name__}: {exc}")
        body = getattr(getattr(exc, "response", None), "text", "")
        if body:
            print(f"[yandex] ответ сервера: {body[:1000]}")
        return 1
    print(f"[yandex] STT: {time.monotonic() - t0:.2f}s")
    if not text:
        print("[yandex] текст не распознан (пустой ответ/ошибка — см. лог выше)")
        return 1
    print(f"[yandex] распознанный текст: {text}")

    if not args.send:
        return 0

    t1 = time.monotonic()
    answer = yandex.ask_gpt(text, system_prompt=yandex.default_system_prompt())
    print(f"[yandex] GPT: {time.monotonic() - t1:.2f}s, ответ: {answer}")
    # «\n\nEmotion: Happy» — команда роботу показать эмоцию, не часть речи:
    # вырезаем перед TTS (эмоция печатается для информации).
    answer, emotion = yandex.split_emotion(answer)
    if emotion:
        hint = "запуск танца" if emotion == "dancing" \
            else f"команда EMOTION:{emotion}"
        print(f"[yandex] эмоция: {emotion} ({hint})")
    if not answer:
        print("[yandex] GPT не вернул ответ — озвучка пропущена")
        return 1

    t2 = time.monotonic()
    pcm_out = yandex.synthesize(answer)
    print(f"[yandex] TTS: {time.monotonic() - t2:.2f}s, {len(pcm_out)} B PCM")
    if not pcm_out:
        print("[yandex] синтез вернул пусто — озвучка пропущена")
        return 1

    asyncio.run(_send_to_robot(pcm_out, host, port))
    return 0


if __name__ == "__main__":
    sys.exit(main())