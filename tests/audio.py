"""Тест звука: воспроизведение готового PCM-файла на роботе.

Сценарий — сервер отправляет заранее заготовленный аудиофайл (PCM int16 LE,
16 кГц/моно; .wav или сырой .pcm) роботу на воспроизведение:
  1. читаем файл из конфига tests.playback_file (относительно корня AiServer);
  2. шлём PCM роботу фреймами [тип][кодек=1][pcm] чанками
     audio.play_chunk_seconds с паузой play_speed — как боевой сервер
     (src/state_machine._send_pcm_chunks): WS-клиент робота не принимает
     фреймы >~8 КБ;
  3. завершаем пустым фреймом-маркером конца потока (робот по нему
     возвращает микрофон/VAD).

Yandex API (STT/GPT/TTS) в этом тесте не используется.
"""
import asyncio
import time
import wave
from pathlib import Path

import websockets

from common import (AUDIO_CODEC_PCM, AUDIO_TYPE, CONF, SAMPLE_RATE, ts)

# Задержка после маркера конца потока: дать роботу доиграть хвост буфера.
PLAYBACK_TAIL_DELAY = 2.0


def _playback_path() -> Path | None:
    """Путь к готовому аудиофайлу (tests.playback_file из конфига)."""
    raw = CONF["tests"].get("playback_file", "")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _read_pcm(path: Path) -> bytes:
    """Читает аудио как сырой PCM (int16 LE): WAV разбирается, иначе — как есть."""
    if path.read_bytes()[:4] == b"RIFF":
        with wave.open(str(path), "rb") as w:
            return w.readframes(w.getnframes())
    return path.read_bytes()


async def run(suite) -> None:
    # 1. Читаем заранее заготовленный PCM-файл.
    path = _playback_path()
    if path is None or not path.is_file():
        suite.report(False, "audio playback file",
                     extra=str(path) if path else "tests.playback_file не задан")
        return
    pcm = _read_pcm(path)
    if not pcm:
        suite.report(False, "audio playback file", extra=f"пустой файл: {path}")
        return
    print(f"[{ts()}] [audio] файл: {path} ({len(pcm)} B PCM, "
          f"~{len(pcm) / (SAMPLE_RATE * 2):.1f}s)")

    # 2. Отправляем PCM роботу чанками — как боевой сервер.
    rate = int(SAMPLE_RATE)
    play_secs = float(CONF["audio"].get("play_chunk_seconds", 0.05))
    if play_secs <= 0 or play_secs > 10:
        play_secs = 0.05
    play_speed = float(CONF["audio"].get("play_speed", 1.0))
    if play_speed <= 0 or play_speed > 10:
        play_speed = 1.0
    chunk_size = round(rate * play_secs) * 2  # N с PCM (int16 LE, моно)
    chunk_dur = chunk_size / (2 * rate)
    n_chunks = (len(pcm) + chunk_size - 1) // chunk_size
    print(f"[{ts()}] [audio] воспроизведение: {n_chunks} чанков по "
          f"{chunk_size} B ({chunk_dur:.2f}s), speed x{play_speed:.2f}")

    t_start = time.monotonic()
    sent_any = False
    for idx in range(n_chunks):
        part = pcm[idx * chunk_size:(idx + 1) * chunk_size]
        try:
            await suite.ws.send(bytes((AUDIO_TYPE, AUDIO_CODEC_PCM)) + part)
        except websockets.exceptions.ConnectionClosed:
            suite.report(False, "robot playback (send audio)",
                         extra=f"связь с роботом потеряна на чанке "
                               f"{idx + 1}/{n_chunks}")
            return
        sent_any = True
        await asyncio.sleep(chunk_dur / play_speed)

    # 3. Маркер конца потока: пустой аудиофрейм [тип][кодек][0 байт].
    if sent_any:
        try:
            await suite.ws.send(bytes((AUDIO_TYPE, AUDIO_CODEC_PCM)))
        except websockets.exceptions.ConnectionClosed:
            suite.report(False, "robot playback (end marker)",
                         extra="связь с роботом потеряна")
            return
        await asyncio.sleep(PLAYBACK_TAIL_DELAY)
    print(f"[{ts()}] [audio] озвучка отправлена: {len(pcm)} B PCM "
          f"за {time.monotonic() - t_start:.1f}s")
    suite.report(sent_any, "robot playback (pcm)",
                 extra=f"{len(pcm)} B pcm ({n_chunks} chunk(s)), file: {path}")