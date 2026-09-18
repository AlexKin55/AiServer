"""State machine сессии с роботом AIBot (только VAD-режим).

Робот — инициатор записи: слушает микрофон в состоянии ready и по резкому
росту шума (голос) сам шлёт RECORD:start, передаёт PCM-чанки (16 кГц/моно,
int16 LE), а по тишине (~0.9 с) или таймауту сегмента (5 с) шлёт RECORD:stop.

Состояния и переходы:
  DISCONNECTED -(on_connected)-> IDLE
  IDLE         -(RECORD:start от робота)-> RECORDING  (VAD: робот определил речь)
  RECORDING    -(RECORD:stop от робота)-> IDLE  (VAD: тишина/таймаут + финализация)
  любое        -(on_disconnected)-> DISCONNECTED

Финализация (_finalize) — фоновая задача: сохранение .wav, ретрансляция в
Yandex (STT -> GPT -> TTS) и озвучка ответа роботу (PCM codec 1) сразу по
готовности синтеза. Цикл приёма WebSocket при этом не блокируется.
"""
import asyncio
import enum
import logging
import time

from . import config as app_config
from . import emotions as emotions_mod
from . import recorder as recorder_mod
from . import robot as robot_mod

logger = logging.getLogger("uvicorn")

# Формат аудиофреймов протокола (единственный кодек — PCM, codec 1).
AUDIO_TYPE = app_config.CONFIG["audio"]["frame_type"]
AUDIO_CODEC_PCM = app_config.CONFIG["audio"]["codec_pcm"]

# Текстовые команды протокола (робот -> сервер) в VAD-режиме.
CMD_RECORD_START = "RECORD:START"
CMD_RECORD_STOP = "RECORD:STOP"

# Автосмена эмоций при простое: (эмоция, доп. задержка от начала отсчёта, с).
# После последнего диалога: +10 с -> Neutral, +20 с -> Sad, +30 с -> Sleepy.
EMOTION_DECAY_STAGES = (("neutral", 10.0), ("sad", 10.0), ("sleepy", 10.0))


class State(enum.Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    RECORDING = "recording"


class SessionStateMachine:
    def __init__(self, bot: robot_mod.RobotSession,
                 rec: recorder_mod.Recorder) -> None:
        self.bot = bot
        self.rec = rec
        self.state = State.DISCONNECTED
        # Потоковый STT (yandex.StreamingRecognizer) активного сегмента.
        self._stt = None
        # Результат последнего распознавания «аудио + промпт -> текст».
        self.last_answer: dict = {}
        # Фоновая задача финализации сегмента (STT -> GPT -> TTS -> озвучка).
        self._finalize_task: asyncio.Task | None = None
        # Невостребованные ACK:MOVE:* от робота (движения выполняются
        # асинхронно в motionTask прошивки; ACK приходит по завершении).
        self._move_ack_pending: list[str] = []
        # Автосмена эмоций при простое (Neutral -> Sad -> Sleepy после
        # последнего диалога); touch() вызывается из on_text/_finalize.
        self.emotions = emotions_mod.EmotionController(
            lambda name: self.bot.send_text(f"EMOTION:{name}"))
        # Автосмена эмоций при простое: отсчёт от последней активности
        # (диалога); событие сбрасывает/перезапускает отсчёт.
        self._emotion_reset = asyncio.Event()
        self._emotion_loop_task: asyncio.Task | None = None
        self._emotion_since: float = 0.0

    # ------------------------------------------------------------------
    # События от робота.
    # ------------------------------------------------------------------
    async def on_connected(self) -> None:
        logger.info("Робот подключён: %s", self.bot.peer)
        self.state = State.IDLE
        self.emotions.start()

    async def on_disconnected(self) -> None:
        logger.info("Робот отключён")
        self.state = State.DISCONNECTED
        # Прерываем незавершённую финализацию: робота больше нет, озвучку
        # слать некому (поток STT в любом случае завершится как daemon).
        if self._finalize_task is not None and not self._finalize_task.done():
            self._finalize_task.cancel()
        self._finalize_task = None
        self.rec.chunks.clear()
        self._stt = None  # daemon-поток стрима завершится сам

    async def on_text(self, text: str) -> None:
        logger.info("Робот -> %s", text)
        cmd = text.strip().upper()
        if cmd == CMD_RECORD_START:
            # VAD-режим: робот сам определил речь (резкий рост шума)
            # и начал слать PCM-чанки.
            if self.state is not State.RECORDING:
                self.rec.begin()
                self.state = State.RECORDING
                logger.info("VAD: робот начал запись (RECORD:start)")
                # Потоковое распознавание: PCM-чанки уходят в Yandex сразу,
                # partial-текст приходит по мере речи (не ждём конца файла).
                try:
                    from . import yandex as yandex_mod  # noqa: PLC0415
                    stt = yandex_mod.StreamingRecognizer()
                    stt.start()
                    self._stt = stt
                except Exception as exc:  # noqa: BLE001
                    logger.warning("STT-стрим не запущен: %s", exc)
                    self._stt = None
            else:
                logger.warning("RECORD:start повторно — игнорирую")
            return
        if cmd == CMD_RECORD_STOP:
            # VAD-режим: тишина (~0.9 с) или таймаут сегмента (5 с) —
            # досылается последний чанк, финализируем запись.
            if self.state is State.RECORDING:
                logger.info("VAD: робот закончил запись (RECORD:stop)")
                # Диалог завершён (речь кончилась) — отсчёт автосмены эмоций
                # стартует от конца записи, а не от её начала: при долгой
                # финализации (STT+GPT+TTS) decay не успевает отправить
                # EMOTION:sad раньше/во время озвучки ответа.
                self.emotions.touch()
                # Не блокируем цикл приёма сообщений: STT -> GPT -> TTS идёт
                # в фоновой задаче, озвучка уходит роботу сразу по готовности
                # синтеза (робот всё время слушает сокет). Снимок сегмента
                # делаем синхронно здесь — recorder/_stt переиспользуются
                # следующим сегментом, который может начаться, пока мы ждём.
                self.state = State.IDLE
                stt = self._stt
                self._stt = None
                pcm = self.rec.raw_pcm
                # Метка сегмента — чтобы отличать параллельные финализации
                # в логе (робот может начать новый сегмент, пока идёт старая).
                seg_tag = time.strftime('%H:%M:%S')
                self._finalize_task = asyncio.create_task(
                    self._finalize(pcm, stt, tag=seg_tag))
            else:
                logger.info("RECORD:stop вне сегмента — игнорирую")
            return
        if cmd == "HB":
            # Heartbeat робота (каждые 15 с): держит NAT/Wi-Fi живым, чтобы
            # канал не умирал от простоя. Только логируем — соединение НЕ
            # обрываем, если HB перестал приходить.
            logger.info("HB от робота")
            return
        # Подтверждения движений: ACK:MOVE:<ось>[:<градусы>] присылает
        # motionTask прошивки только ПОСЛЕ фактического завершения поворота —
        # на них может ждать танцевальный паттерн (см. wait_move_ack).
        if cmd.startswith("ACK:MOVE"):
            self._move_ack_pending.append(text.strip())
            return
        # Прочий служебный текст — просто логируем.

    async def wait_move_ack(self, axis: str, deg: int,
                            timeout: float = 8.0) -> bool:
        """Ждёт ACK:MOVE от робота — сигнал фактического завершения движения.

        Движения выполняются асинхронно (motionTask прошивки), и ACK:MOVE:<ось>
        [: <градусы>] приходит только после окончания поворота (waitMotion).
        Невостребованные ACK не теряются: хранятся в _move_ack_pending до
        совпадения или таймаута. Возвращает True, если движение подтверждено.
        """
        want = f"ack:move:{axis}" + ("" if axis == "center" else f":{deg}")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for i, ack in enumerate(self._move_ack_pending):
                if ack.strip().lower() == want:
                    del self._move_ack_pending[i]
                    return True
            await asyncio.sleep(min(0.02, remaining))

    async def on_audio(self, data: bytes) -> None:
        if self.state is State.RECORDING:
            self.rec.feed_chunk(data)
            if self._stt is not None:
                try:
                    self._stt.feed(data)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("STT feed error: %s", exc)

    # ------------------------------------------------------------------
    # Финализация VAD-сегмента.
    # ------------------------------------------------------------------
    async def _finalize(self, pcm: bytes, stt,
                        tag: str = "-") -> tuple[bool, dict]:
        """Фоновая финализация VAD-сегмента; не блокирует приём WebSocket.

        pcm/stt — синхронный снимок сегмента из on_text (RECORD:stop):
        робот может начать новый сегмент, пока идут STT/GPT/TTS, recorder
        и _stt переиспользуются. STT -> GPT -> TTS — асинхронные шаги;
        PCM от TTS отправляется роботу сразу, как только синтез готов.
        tag — метка сегмента для читаемого лога параллельных задач.
        """
        n = len(pcm)
        if n == 0:
            return True, {"status": "no audio", "seconds": 0.0}
        rate = app_config.CONFIG["audio"]["sample_rate"]
        wav_path = self.rec.save(pcm)
        info = {
            "status": "saved",
            "file": wav_path,
            "bytes": n,
            "seconds": round(n / (2 * rate), 2),
        }
        # Распознавание шло потоково (по мере поступления чанков): закрываем
        # стрим и забираем текст; fallback — распознать накопленный PCM.
        try:
            from . import yandex as yandex_mod  # noqa: PLC0415
            if yandex_mod.credentials_ok():
                t_stt = time.monotonic()
                text = ""
                stt_src = "-"
                if stt is not None:
                    stt_src = "stream"
                    text = await asyncio.to_thread(stt.finish)
                if not text and pcm:
                    stt_src = "file"
                    text = await asyncio.to_thread(
                        yandex_mod.recognize_pcm, pcm)
                logger.info("[%s] STT: текст=%r (источник=%s, %.1fs, %d B PCM)",
                            tag, text, stt_src, time.monotonic() - t_stt, n)
                info["recognized"] = text
                answer = ""
                if text:
                    t_gpt = time.monotonic()
                    logger.info("[%s] GPT: отправлено %d символов -> YandexGPT",
                                tag, len(text))
                    answer = await asyncio.to_thread(
                        yandex_mod.ask_gpt, text,
                        system_prompt=yandex_mod.default_system_prompt())
                    logger.info("[%s] GPT: ответ=%r (%.1fs, %d символов)",
                                tag, answer,
                                time.monotonic() - t_gpt, len(answer))
                # Хвост «\n\nEmotion: Happy» — команда роботу, а не речь:
                # вырезаем из текста для TTS. Обычные эмоции уходят командой
                # EMOTION:<name>, а «Dancing» запускает танцевальный паттерн
                # (команды EMOTION:dancing в протоколе робота нет).
                answer, emotion = yandex_mod.split_emotion(answer)
                if emotion == "dancing":
                    logger.info("[%s] EMOTION: dancing -> танец (фон)", tag)
                    # Танец крутится в фоне, чтобы не задерживать озвучку;
                    # каждый следующий шаг стартует после ACK:MOVE от робота
                    # (движение реально завершилось), а не по фиксированной
                    # паузе — ритм танца = скорость сервоприводов.
                    async def _dance() -> None:
                        await self.bot.dancing(
                            wait_ack=self.wait_move_ack,
                            ack_timeout=8.0)
                    asyncio.create_task(_dance())
                elif emotion:
                    logger.info("[%s] EMOTION: %s -> робот", tag, emotion)
                    ok_emo = await self.bot.send_text(f"EMOTION:{emotion}")
                    logger.info("[%s] EMOTION: отправка -> %s", tag,
                                "ok" if ok_emo else "НЕТ СОЕДИНЕНИЯ")
                info["answer"] = answer
                info["emotion"] = emotion
                self.last_answer = {"recognized": text, "answer": answer}
                logger.info("[%s] Распознано: %s", tag, text)
                logger.info("[%s] Ответ GPT: %s", tag, answer)

                # Синтез ответа (TTS) и отправка роботу для озвучки:
                # PCM от TTS уходит напрямую (codec 1) сразу по готовности,
                # робот воспроизводит его без декодирования.
                if answer:
                    try:
                        t_tts = time.monotonic()
                        logger.info("[%s] TTS: синтез %d символов -> SpeechKit",
                                    tag, len(answer))
                        pcm_out = await asyncio.to_thread(
                            yandex_mod.synthesize, answer)
                        logger.info("[%s] TTS: готово %d B PCM (%.1fs)",
                                    tag, len(pcm_out),
                                    time.monotonic() - t_tts)
                        if pcm_out:
                            # Ответ TTS готов — на время озвучки переключаем
                            # робота на эмоцию «doubt» (задумался), чтобы лицо
                            # отражало процесс говорения ответа.
                            ok_doubt = await self.bot.send_text("EMOTION:doubt")
                            logger.info("[%s] TTS: готово -> EMOTION:doubt "
                                        "(%s)", tag,
                                        "ok" if ok_doubt else "НЕТ СОЕДИНЕНИЯ")
                            # Сохраняем озвучку в WAV для проверки
                            # (records/out_robot_*.wav), затем шлём роботу.
                            try:
                                out_wav = self.rec.save(pcm_out, "out_robot")
                                info["speech_file"] = out_wav
                                logger.info("[%s] SEND: озвучка сохранена: %s",
                                            tag, out_wav)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("[%s] SEND: не удалось "
                                               "сохранить озвучку: %s",
                                               tag, exc)
                            sent = await self._send_pcm_chunks(
                                pcm_out, tag, rate)
                            info["spoken"] = bool(sent)
                            info["speech_bytes"] = len(pcm_out)
                            logger.info(
                                "[%s] SEND: итого %d B PCM (%.2f s) -> %s",
                                tag, len(pcm_out),
                                len(pcm_out) / (2 * rate),
                                "ok" if sent else "НЕТ СОЕДИНЕНИЯ")
                        else:
                            logger.warning("TTS вернул пустое аудио")
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("TTS/send error: %s", exc)
                        info["speech_error"] = str(exc)
            else:
                logger.info("Yandex STT пропущен: не заданы креды")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Yandex STT/GPT error: %s", exc)
            info["recognize_error"] = str(exc)
        # Диалог завершён — стартует отсчёт автосмены эмоций (Neutral ->
        # Sad -> Sleepy через каждые 10 с, если не было новых диалогов).
        self.emotions.touch()
        return True, info

    async def _send_pcm_chunks(self, pcm: bytes, tag: str, rate: int) -> bool:
        """Отправляет PCM роботу фреймами заданной длины.

        Длина чанка — config audio.play_chunk_seconds (по умолчанию 0.05 с):
        WS-клиент робота не принимает фреймы >~8 КБ. Скорость доставки —
        config audio.play_speed (1.0 = реальное время): пауза между чанками
        равна длительности чанка / play_speed. Ускорять без нужды не стоит:
        буфер воспроизведения робота накопит хвост, и микрофон (VAD) не
        вернётся в работу, пока хвост не отыграет.
        """
        try:
            play_secs = float(
                app_config.CONFIG["audio"]["play_chunk_seconds"])
        except Exception:  # noqa: BLE001
            play_secs = 0.05
        if play_secs <= 0 or play_secs > 10:
            play_secs = 0.05
        try:
            play_speed = float(app_config.CONFIG["audio"]["play_speed"])
        except Exception:  # noqa: BLE001
            play_speed = 1.0
        if play_speed <= 0 or play_speed > 10:
            play_speed = 1.0
        chunk_size = round(rate * play_secs) * 2  # N с PCM (int16 LE, моно)
        chunk_dur = chunk_size / (2 * rate)  # длительность чанка, с
        n_chunks = (len(pcm) + chunk_size - 1) // chunk_size
        sent_any = False
        logger.info("[%s] PLAY: начало озвучки (%d B, %d чанков по %.1f с, "
                    "speed x%.2f)", tag, len(pcm), n_chunks, chunk_dur,
                    play_speed)
        for idx in range(n_chunks):
            part = pcm[idx * chunk_size:(idx + 1) * chunk_size]
            ok = await self.bot.send_audio_frame(
                AUDIO_TYPE, AUDIO_CODEC_PCM, part)
            sent_any = sent_any or ok
            logger.info(
                "[%s] PLAY: чанк %d/%d (%d B, %.2f s) -> %s",
                tag, idx + 1, n_chunks, len(part),
                len(part) / (2 * rate),
                "ok" if ok else "НЕТ СОЕДИНЕНИЯ")
            if not ok:
                logger.warning("[%s] PLAY: обрыв на чанке %d/%d",
                               tag, idx + 1, n_chunks)
                break
            # Пауза = длительность чанка / скорость доставки.
            await asyncio.sleep(chunk_dur / play_speed)
        if sent_any:
            # Маркер конца озвучки: пустой аудиофрейм [тип][кодек][0 байт].
            # Прошивка робота возвращает микрофон (VAD) при приходе нового
            # чанка — пустой чанк даёт ей триггер «поток закончился».
            eof_ok = await self.bot.send_audio_frame(
                AUDIO_TYPE, AUDIO_CODEC_PCM, b"")
            logger.info("[%s] PLAY: маркер конца потока (0 B) -> %s", tag,
                        "ok" if eof_ok else "НЕТ СОЕДИНЕНИЯ")
        logger.info("[%s] PLAY: озвучка завершена "
                    "(отправлено %d из %d чанков)",
                    tag, n_chunks if sent_any else 0, n_chunks)
        return sent_any