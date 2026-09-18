"""Накопление и сохранение аудио от робота (PCM-чанки, кодек 1).

Формат фрейма робота: [0]=тип(1), [1]=кодек(1=PCM), далее сырые сэмплы
int16 LE (16 кГц, моно). Recorder копит тела фреймов и сохраняет запись
в WAV-файл (удобно слушать/проверять) — тот же PCM, что уходит в Yandex STT.
"""
import logging
import os
import time
import wave

logger = logging.getLogger("uvicorn")

SAMPLE_RATE = 16000
DEFAULT_RECORD_DIR = "records"


class Recorder:
    """Буфер записываемых PCM-чанков и сохранение в WAV-файл."""

    def __init__(self, record_dir: str | None = None) -> None:
        # Приоритет: явный параметр > env RECORD_DIR > конфиг > дефолт.
        cfg_dir: str | None = None
        try:
            from . import config as app_config
            cfg_dir = app_config.CONFIG["recording"]["record_dir"]
        except Exception:  # noqa: BLE001
            pass
        self._dir = (record_dir
                     or os.environ.get("RECORD_DIR")
                     or cfg_dir
                     or DEFAULT_RECORD_DIR)
        self.chunks: list[bytes] = []
        self.started_at = 0.0

    def begin(self) -> None:
        """Начинает новую запись (сбрасывает буфер)."""
        self.chunks.clear()
        self.started_at = time.time()

    def feed_chunk(self, payload: bytes) -> None:
        """Добавляет тело фрейма (сырые PCM-байты int16 LE)."""
        if payload:
            self.chunks.append(bytes(payload))

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return time.time() - self.started_at

    @property
    def raw_pcm(self) -> bytes:
        """Склеенные PCM-байты (int16 LE, моно, 16 кГц)."""
        return b"".join(self.chunks)

    def save(self, pcm: bytes | None = None, prefix: str = "robot_mic") -> str:
        """Сохраняет аудио как WAV и возвращает путь к файлу.

        pcm — готовые PCM-байты (снимок сегмента / синтез TTS); если не задан,
        берётся текущий буфер chunks. prefix — имя файла: robot_mic — запись
        робота, out_robot — озвучка, синтезированная сервером (TTS).
        """
        os.makedirs(self._dir, exist_ok=True)
        base = time.strftime('%Y%m%d_%H%M%S')
        raw = pcm if pcm is not None else self.raw_pcm

        wav_path = os.path.join(self._dir, f"{prefix}_{base}.wav")
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(raw)

        logger.info("Сохранена запись: %s (%d байт PCM)",
                    wav_path, len(raw))
        return wav_path