"""Интеграция с Yandex Cloud: распознавание речи и генерация ответа.

- recognize_pcm(): SpeechKit STT v3 RecognizeStreaming (gRPC, официальный
  пример): PCM (16 кГц/моно, int16 LE) отправляется как raw LINEAR16_PCM
  чанками AudioChunk; текст собирается из событий final/final_refinement.
- ask_gpt(): YandexGPT v3 (REST foundationModels/v1/completion).
- synthesize(): SpeechKit TTS v3 (REST tts/v3/utteranceSynthesis) — возвращает
  PCM 16 кГц/моно, который робот получает напрямую (codec 1).

Креды (Api-Key и folderId) берутся из переменных окружения
YANDEX_API_KEY / YANDEX_FOLDER_ID, либо из конфига config/settings.json
(секция "yandex", поддержка ${VAR}). Зависимости импортируются лениво.

Аудиоформат робота/сервера — сырой PCM (16 кГц/моно/16 бит, int16 LE) без
какой-либо компрессии: робот пишет микрофон и шлёт PCM-чанки
[тип][кодек=1][pcm], сервер озвучивает ответ тем же PCM.

Пример (см. также tests/audio.py):
    pcm = open("rec.pcm", "rb").read()        # int16 LE, 16 кГц, моно
    text, answer = ask_with_pcm(pcm)
"""
import array
import base64
import logging
import os
import re
import socket
import struct
import time

logger = logging.getLogger("uvicorn")

# Частота PCM аудио робота и сервера (STT/TTS), Гц.
STT_SAMPLE_RATE_HZ = 16000

# В этой сети IPv6-адреса Yandex Cloud не отвечают, а Python/requests/grpc
# берут ПЕРВЫЙ адрес из getaddrinfo (это IPv6) и висят до таймаута
# (в логах — GPT по 120+ секунд без ответа). Предпочитаем IPv4 для хостов
# Yandex Cloud: соединение устанавливается сразу.
_orig_getaddrinfo = socket.getaddrinfo
if socket.has_ipv6:
    def _getaddrinfo_ipv4_first(host: str, *args, **kwargs):
        result = _orig_getaddrinfo(host, *args, **kwargs)
        if str(host).endswith("api.cloud.yandex.net"):
            return sorted(result, key=lambda item: item[0] != socket.AF_INET)
        return result
    socket.getaddrinfo = _getaddrinfo_ipv4_first


def get_credentials() -> tuple[str, str]:
    """Возвращает (api_key, folder_id) из env или конфига."""
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if key and folder:
        return key, folder
    try:
        from . import config as app_config
        y = app_config.CONFIG.get("yandex", {})
        key = key or str(y.get("api_key", "")).strip()
        folder = folder or str(y.get("folder_id", "")).strip()
    except Exception:  # noqa: BLE001
        pass
    return key, folder


def credentials_ok() -> bool:
    key, folder = get_credentials()
    return bool(key and folder)


def recognize_pcm(pcm: bytes, language_code: str = "ru-RU") -> str:
    """Распознаёт PCM (int16 LE, моно, 16 кГц) через SpeechKit STT v3.

    Используется тестом testYandexApi/yandex_test.py и боевым сервером
    (ask_with_pcm): PCM уходит в RecognizeStreaming как raw LINEAR16_PCM.
    """
    return _stream_recognize(pcm, language_code=language_code)


def _stream_recognize(pcm: bytes, language_code: str = "ru-RU",
                      sample_rate_hz: int = STT_SAMPLE_RATE_HZ,
                      poll_timeout_s: float = 180.0) -> str:
    """Распознаёт PCM через SpeechKit STT v3 RecognizeStreaming (gRPC).

    Схема из официального примера Yandex: первый StreamingRequest несёт
    session_options с raw_audio=LINEAR16_PCM (частота — как у аудио),
    затем поток StreamingRequest(chunk=AudioChunk(data=...)) чанками
    ~4000 байт. Результат собирается из событий oneof Event:
      - final: alternatives[].text
      - final_refinement.normalized_text.alternatives[].text
    Возвращает склеенный текст (или "" при ошибке/пусто).
    """
    if not pcm:
        return ""
    import grpc
    from yandex.cloud.ai.stt.v3 import stt_pb2
    from yandex.cloud.ai.stt.v3 import stt_service_pb2_grpc

    key, folder = get_credentials()
    if not key or not folder:
        raise RuntimeError("Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID")

    chunk_size = 4000

    def gen():
        yield stt_pb2.StreamingRequest(
            session_options=stt_pb2.StreamingOptions(
                recognition_model=stt_pb2.RecognitionModelOptions(
                    audio_format=stt_pb2.AudioFormatOptions(
                        raw_audio=stt_pb2.RawAudio(
                            audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
                            sample_rate_hertz=sample_rate_hz,
                            audio_channel_count=1,
                        ),
                    ),
                    text_normalization=stt_pb2.TextNormalizationOptions(
                        text_normalization=(
                            stt_pb2.TextNormalizationOptions
                            .TEXT_NORMALIZATION_ENABLED
                        ),
                        profanity_filter=False,
                        literature_text=False,
                    ),
                    language_restriction=stt_pb2.LanguageRestrictionOptions(
                        restriction_type=(
                            stt_pb2.LanguageRestrictionOptions.WHITELIST
                        ),
                        language_code=[language_code],
                    ),
                    audio_processing_type=(
                        stt_pb2.RecognitionModelOptions.REAL_TIME
                    ),
                ),
            ),
        )
        for i in range(0, len(pcm), chunk_size):
            yield stt_pb2.StreamingRequest(
                chunk=stt_pb2.AudioChunk(data=pcm[i:i + chunk_size]),
            )

    channel = grpc.secure_channel(
        "stt.api.cloud.yandex.net:443", grpc.ssl_channel_credentials())
    metadata = (
        ("authorization", f"Api-Key {key}"),
        ("x-folder-id", folder),
    )
    try:
        stub = stt_service_pb2_grpc.RecognizerStub(channel)
        texts: list[str] = []
        last_partial = ""
        for resp in stub.RecognizeStreaming(gen(), metadata=metadata,
                                            timeout=poll_timeout_s):
            event_type = resp.WhichOneof("Event")
            if event_type == "status_code":
                logger.info("STT v3 status_code=%s: %s",
                            resp.status_code.code_type,
                            resp.status_code.message or "")
                continue
            if event_type == "partial":
                # Промежуточный результат: держим последний как fallback —
                # если сервер не пришлёт final (фраза обрезана VAD на конце).
                if (resp.partial.alternatives
                        and resp.partial.alternatives[0].text):
                    last_partial = resp.partial.alternatives[0].text
                continue
            if event_type == "final":
                alts = resp.final.alternatives
            elif event_type == "final_refinement":
                alts = resp.final_refinement.normalized_text.alternatives
            else:
                continue
            if alts and alts[0].text:
                texts.append(alts[0].text)
        result = " ".join(texts).strip()
        if not result and last_partial:
            logger.info("STT v3: final отсутствует, беру последний partial: %r",
                        last_partial)
            return last_partial
        return result
    except grpc.RpcError as exc:
        logger.error("Yandex STT v3 streaming error: %s: %s",
                     exc.code(), exc.details())
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("Yandex STT v3 streaming error: %s", exc)
        return ""
    finally:
        channel.close()


class StreamingRecognizer:
    """Потоковое распознавание SpeechKit STT v3 — по мере поступления PCM.

    Открывает RecognizeStreaming при start(), кормит AudioChunk через feed()
    и возвращает собранный текст при finish(). partial-события логируются,
    итоговый текст складывается из final/final_refinement. Работает в
    отдельном потоке, чтобы не блокировать asyncio-цикл сервера.
    """

    def __init__(self, language_code: str = "ru-RU",
                 sample_rate_hz: int = STT_SAMPLE_RATE_HZ) -> None:
        import queue
        import threading
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._texts: list[str] = []
        # Последний partial-текст: fallback, если final так и не придёт.
        self._last_partial = ""
        self._err: Exception | None = None
        self._done = threading.Event()
        self._started = False
        self._language = language_code
        self._rate = sample_rate_hz

    def start(self) -> None:
        """Запускает поток распознавания (gRPC-стрим на Yandex)."""
        if self._started:
            return
        self._started = True
        import threading
        threading.Thread(target=self._run, name="stt-stream",
                         daemon=True).start()

    def feed(self, pcm: bytes) -> None:
        """Отправляет очередной PCM-чанк в поток (int16 LE, моно).

        Неблокирующая вставка: при переполнении очереди выбрасываем самые
        старые данные (свежий конец фразы важнее начала), чтобы не замораживать
        asyncio-цикл сервера.
        """
        if self._started and not self._done.is_set() and pcm:
            while self._q.qsize() >= self._q.maxsize:
                self._q.get_nowait()
            self._q.put_nowait(bytes(pcm))

    def finish(self, timeout_s: float = 120.0) -> str:
        """Закрывает поток и возвращает собранный текст распознавания.

        Приоритет: события final/final_refinement. Если сервер их не прислал
        (фраза обрезана VAD на стыке сегмента), возвращаем последний partial.
        """
        self._q.put(None)  # sentinel: конец аудио
        if not self._done.wait(timeout_s):
            logger.warning("STT-стрим не завершился за %ss", timeout_s)
        if self._err is not None:
            logger.error("Yandex STT streaming error: %s", self._err)
        result = " ".join(t for t in self._texts if t).strip()
        if not result and self._last_partial:
            logger.info("STT: final отсутствует, беру последний partial: %r",
                        self._last_partial)
            return self._last_partial
        return result

    def _gen(self):
        from yandex.cloud.ai.stt.v3 import stt_pb2

        yield stt_pb2.StreamingRequest(
            session_options=stt_pb2.StreamingOptions(
                recognition_model=stt_pb2.RecognitionModelOptions(
                    audio_format=stt_pb2.AudioFormatOptions(
                        raw_audio=stt_pb2.RawAudio(
                            audio_encoding=stt_pb2.RawAudio.LINEAR16_PCM,
                            sample_rate_hertz=self._rate,
                            audio_channel_count=1,
                        ),
                    ),
                    text_normalization=stt_pb2.TextNormalizationOptions(
                        text_normalization=(
                            stt_pb2.TextNormalizationOptions
                            .TEXT_NORMALIZATION_ENABLED
                        ),
                        profanity_filter=False,
                        literature_text=False,
                    ),
                    language_restriction=stt_pb2.LanguageRestrictionOptions(
                        restriction_type=(
                            stt_pb2.LanguageRestrictionOptions.WHITELIST
                        ),
                        language_code=[self._language],
                    ),
                    audio_processing_type=(
                        stt_pb2.RecognitionModelOptions.REAL_TIME
                    ),
                ),
            ),
        )
        while True:
            item = self._q.get()
            if item is None:  # конец аудио — закрываем стрим
                return
            # Yandex принимает небольшие AudioChunk (в примере — 4000 Б);
            # крупные PCM-чанки робота (до ~2 с) режем перед отправкой.
            for i in range(0, len(item), 4000):
                yield stt_pb2.StreamingRequest(
                    chunk=stt_pb2.AudioChunk(data=item[i:i + 4000]))

    def _run(self) -> None:
        import grpc
        from yandex.cloud.ai.stt.v3 import stt_service_pb2_grpc

        key, folder = get_credentials()
        if not key or not folder:
            self._err = RuntimeError(
                "Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID")
            self._done.set()
            return
        channel = grpc.secure_channel(
            "stt.api.cloud.yandex.net:443", grpc.ssl_channel_credentials())
        try:
            stub = stt_service_pb2_grpc.RecognizerStub(channel)
            metadata = (
                ("authorization", f"Api-Key {key}"),
                ("x-folder-id", folder),
            )
            for resp in stub.RecognizeStreaming(self._gen(),
                                                metadata=metadata):
                ev = resp.WhichOneof("Event")
                if ev == "status_code":
                    logger.info("STT v3 status_code=%s: %s",
                                resp.status_code.code_type,
                                resp.status_code.message or "")
                    continue
                if ev == "partial":
                    if (resp.partial.alternatives
                            and resp.partial.alternatives[0].text):
                        self._last_partial = resp.partial.alternatives[0].text
                        logger.info("STT partial: %s", self._last_partial)
                    continue
                if ev == "final":
                    alts = resp.final.alternatives
                elif ev == "final_refinement":
                    alts = resp.final_refinement.normalized_text.alternatives
                else:
                    continue
                if alts and alts[0].text:
                    self._texts.append(alts[0].text)
        except grpc.RpcError as exc:
            self._err = exc
        except Exception as exc:  # noqa: BLE001
            self._err = exc
        finally:
            channel.close()
            self._done.set()


def default_system_prompt() -> str:
    """Системный промпт по умолчанию (из конфига yandex.system_prompt)."""
    try:
        from . import config as app_config
        return str(app_config.CONFIG.get("yandex", {}).get("system_prompt", ""))
    except Exception:  # noqa: BLE001
        return ""


def ask_gpt(user_text: str, system_prompt: str = "",
            temperature: float = 0.5, max_tokens: int = 1000) -> str:
    """Отправляет текст в YandexGPT v3 и возвращает ответ (или "" при ошибке).

    Модель и таймаут — из конфига (yandex.gpt_model, yandex.gpt_timeout_s),
    по умолчанию yandexgpt/latest и 90 c. Каждый этап логируется с таймстемпом
    и длительностью, чтобы было видно, где именно виснет запрос.
    """
    import requests

    key, folder = get_credentials()
    if not key or not folder:
        raise RuntimeError("Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID")

    model = "yandexgpt/latest"
    timeout_s = 90.0
    try:
        from . import config as app_config
        y = app_config.CONFIG.get("yandex", {})
        model = str(y.get("gpt_model", model))
        timeout_s = float(y.get("gpt_timeout_s", timeout_s))
    except Exception:  # noqa: BLE001
        pass

    messages = []
    if system_prompt:
        messages.append({"role": "system", "text": system_prompt})
    messages.append({"role": "user", "text": user_text})

    payload = {
        "modelUri": f"gpt://{folder}/{model}",
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": str(max_tokens),
        },
        "messages": messages,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {key}",
    }
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    t0 = time.monotonic()
    logger.info("GPT: POST %s model=%s text=%d символов (timeout=%ss)",
                url, model, len(user_text), timeout_s)
    try:
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=(15, timeout_s))
        dt = time.monotonic() - t0
        logger.info("GPT: HTTP %d за %.1fs", resp.status_code, dt)
        resp.raise_for_status()
        data = resp.json()
        alternatives = data.get("result", {}).get("alternatives", [])
        if alternatives:
            text = alternatives[0]["message"]["text"]
            logger.info("YandexGPT: %.1fs, %d chars", time.monotonic() - t0,
                        len(text))
            return text
        logger.warning("YandexGPT: %.1fs, no alternatives (ответ: %s)",
                       time.monotonic() - t0, str(data)[:300])
    except Exception as exc:  # noqa: BLE001
        body = ""
        status = ""
        r = getattr(exc, "response", None)
        if r is not None:
            status = r.status_code
            body = (r.text or "")[:300]
        logger.exception("YandexGPT error after %.1fs (HTTP=%s): %s%s%s",
                         time.monotonic() - t0, status, exc,
                         "; тело: " if body else "", body)
    return ""


def _tts_settings() -> dict:
    """Параметры TTS из конфига (voice/speed/role)."""
    try:
        from . import config as app_config
        y = app_config.CONFIG.get("yandex", {})
        return {
            "voice": str(y.get("tts_voice", "zahar")),
            "speed": float(y.get("tts_speed", 1.00)),
            "role": str(y.get("tts_role", "friendly")),
        }
    except Exception:  # noqa: BLE001
        return {"voice": "zahar", "speed": 1.00, "role": "friendly"}


def _extract_pcm_wav(raw: bytes) -> tuple[bytes, int, int]:
    """Извлекает PCM из WAV-контейнера(ов), возвращает (pcm, rate, channels).

    SpeechKit TTS v3 отдаёт audioChunk как WAV (RIFF/WAVE) с собственным
    заголовком, где указаны настоящая частота и число каналов; один ответ
    может содержать несколько WAV-секций подряд. Если буфер не RIFF —
    считаем его сырым PCM (16 кГц/моно).
    """
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw, 16000, 1
    out = bytearray()
    rate = 0
    channels = 0
    pos = 0
    while pos + 12 <= len(raw):
        if raw[pos:pos + 4] != b"RIFF" or raw[pos + 8:pos + 12] != b"WAVE":
            break
        riff_size = struct.unpack_from("<I", raw, pos + 4)[0]
        end = min(pos + 8 + riff_size, len(raw))
        off = pos + 12
        got_data = False
        while off + 8 <= end:
            cid = raw[off:off + 4]
            size = struct.unpack_from("<I", raw, off + 4)[0]
            body = raw[off + 8:off + 8 + size]
            if cid == b"fmt " and len(body) >= 16:
                _, channels, rate, _, _, _ = struct.unpack_from(
                    "<HHIIHH", body, 0)
            elif cid == b"data":
                out += body
                got_data = True
            off += 8 + size + (size & 1)
        if not got_data:
            break
        pos = end
    return bytes(out), rate or 16000, channels or 1


def _resample_pcm(pcm: bytes, src_rate: int, dst_rate: int,
                  channels: int = 1) -> bytes:
    """Пересэмплирует int16 PCM в моно (линейная интерполяция).

    При стерео сначала сводит каналы в моно; возвращает dst_rate PCM.
    """
    samples = array.array("h")
    samples.frombytes(pcm)
    if channels > 1:
        samples = array.array("h",
                              (samples[i] for i in range(0, len(samples),
                                                         channels)))
    if src_rate == dst_rate:
        return samples.tobytes()
    ratio = src_rate / dst_rate
    n_out = round(len(samples) * dst_rate / src_rate)
    out = array.array("h")
    pos = 0.0
    for _ in range(n_out):
        idx = int(pos)
        frac = pos - idx
        a = samples[idx] if idx < len(samples) else samples[-1]
        b = samples[idx + 1] if idx + 1 < len(samples) else a
        out.append(int(a * (1.0 - frac) + b * frac))
        pos += ratio
    return out.tobytes()


def clean_for_speech(text: str) -> str:
    """Готовит текст для синтеза речи: убирает вероятные триггеры 400.

    YandexGPT отвечает markdown'ом (списки '*', жирный '**', ссылки
    [x](url), код '`', заголовки '#', цитаты '>'), а SpeechKit TTS v3 может
    отклонять такой текст (400 Bad Request) или читать разметку буквально.
    Дополнительно вычищаются SSML-подобные '<...>' (v3 парсит угловые
    скобки как разметку), эмодзи и управляющие символы, голые URL;
    текст обрезается до безопасной длины. Параметры голоса задаются
    hints, а не текстом.
    """
    # markdown-ссылки [text](url) -> text, затем голые URL
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    # разметка ** / __ / * / _ / ` (звёздочки и подчёркивания речи не нужны)
    text = re.sub(r"[*_`]", "", text)
    # заголовки "# ", "## " и цитаты "> " в начале строк
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # маркеры списков "- " / "+ " в начале строки
    text = re.sub(r"^\s*[-+]\s+", "", text, flags=re.MULTILINE)
    # SSML-подобные теги <...> и оставшиеся угловые скобки
    text = re.sub(r"<[^>]*>", "", text)
    text = text.replace("<", " ").replace(">", " ")
    # эмодзи и пиктограммы (включая вариационные селекторы)
    text = re.sub(
        r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
        r"\uFE0F\u200D\u2190-\u21FF]",
        "", text)
    # управляющие символы (кроме \n и \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # лишние переводы строк и пробелы
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = text.strip()
    # защита от чрезмерной длины (SpeechKit v3: до ~5000 символов)
    if len(text) > 5000:
        text = text[:5000].rstrip()
    return text


def _tts_post(url: str, headers: dict, payload: dict) -> bytes:
    """Один запрос SpeechKit TTS v3 utteranceSynthesis -> PCM (16 кГц/моно).

    Ответ — NDJSON-поток, каждый чанк содержит base64-WAV; из тела извлекается
    настоящий PCM и при необходимости пересэмплируется в 16 кГц/моно.
    При ошибке HTTP бросает requests.HTTPError (тело — в exc.response.text).
    """
    import json

    import requests

    resp = requests.post(url, json=payload, headers=headers,
                         stream=True, timeout=120)
    resp.raise_for_status()
    raw = bytearray()
    for line in resp.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        b64 = chunk.get("result", {}).get("audioChunk", {}).get("data")
        if b64:
            raw += base64.b64decode(b64)
    pcm, rate, channels = _extract_pcm_wav(bytes(raw))
    if rate != STT_SAMPLE_RATE_HZ or channels != 1:
        pcm = _resample_pcm(pcm, rate, STT_SAMPLE_RATE_HZ, channels)
    return pcm


def _split_half(text: str) -> tuple[str, str]:
    """Делит текст на две непустые части по границе ближе к середине.

    Ищет знак препинания (предложение/полуфраза) рядом с серединой; если его
    нет — режет по пробелу, иначе пополам (безопасно для рекурсии).
    """
    mid = len(text) // 2
    cut = mid
    bounds = []
    for sep in ".!?;…:,—":
        i = text.rfind(sep, 0, mid)
        if i != -1:
            bounds.append(i + 1)
        i = text.find(sep, mid)
        if i != -1:
            bounds.append(i + 1)
    if bounds:
        cut = min(bounds, key=lambda i: abs(i - mid))
    else:
        space = text.rfind(" ", 0, mid)
        if space > 0:
            cut = space + 1
    left, right = text[:cut].strip(), text[cut:].strip()
    if not left or not right:
        cut = max(mid, 1)
        left, right = text[:cut].strip(), text[cut:].strip()
    return left, right


def _synthesize_part(text: str, url: str, headers: dict,
                     voice: str, speed: float, role: str,
                     sample_rate_hz: int, depth: int = 0) -> bytes:
    """Синтезирует часть текста; при 400 'Too long text' делит пополам.

    Лимит текста на один запрос у SpeechKit TTS v3 может быть много меньше
    официальных 5000 символов (на некоторых тарифах — ошибка 'Too long text'
    уже на ~260 символах), поэтому при таком 400 текст рекурсивно делится
    пополам и каждая половина синтезируется отдельно, PCM склеивается.
    """
    from requests import HTTPError

    payload = {
        "text": text,
        "outputAudioSpec": {
            "rawData": {
                "audioEncoding": "LINEAR16_PCM",
                "sampleRateHertz": sample_rate_hz,
            }
        },
        "hints": [
            {"voice": voice},
            {"speed": speed},
            {"role": role},
        ],
    }
    try:
        t0 = time.monotonic()
        pcm = _tts_post(url, headers, payload)
        logger.info("TTS part(%d): %.1fs, %d B pcm", depth,
                    time.monotonic() - t0, len(pcm))
        return pcm
    except HTTPError as exc:
        body = ""
        try:
            body = getattr(exc.response, "text", "") or ""  # noqa: BLE001
        except Exception:  # noqa: BLE001
            pass
        if "too long" not in body.lower():
            raise
        if len(text) <= 8:
            # Даже минимальный кусок отклонён — это уже не длина.
            logger.exception("Yandex TTS: короткий текст отклонён: %r "
                             "(body: %s)", text, body[:400])
            raise
        left, right = _split_half(text)
        logger.warning("Yandex TTS: 'Too long text' (%d симв.) — делю: "
                       "%d + %d", len(text), len(left), len(right))
        out = _synthesize_part(left, url, headers, voice, speed, role,
                               sample_rate_hz, depth + 1)
        out += _synthesize_part(right, url, headers, voice, speed, role,
                                sample_rate_hz, depth + 1)
        return out


def synthesize(text: str, sample_rate_hz: int = 16000,
               voice: str | None = None,
               speed: float | None = None,
               role: str | None = None) -> bytes:
    """Синтез речи в PCM (16 кГц/моно/16 бит) через SpeechKit TTS v3.

    URL: https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis
    Текст очищается от markdown/спецсимволов (clean_for_speech); при 400
    'Too long text' делится пополам и синтезируется частями (см.
    _synthesize_part). Возвращает байты PCM (или b"" при ошибке/пустом тексте).
    """
    if not text or not text.strip():
        return b""
    text = clean_for_speech(text)
    if not text:
        return b""
    key, folder = get_credentials()
    if not key or not folder:
        raise RuntimeError("Не заданы YANDEX_API_KEY / YANDEX_FOLDER_ID")

    url = "https://tts.api.cloud.yandex.net:443/tts/v3/utteranceSynthesis"
    headers = {
        "Authorization": f"Api-Key {key}",
        "x-folder-id": folder,
        "Content-Type": "application/json",
    }
    s = _tts_settings()
    speed_eff = float(speed if speed else s["speed"])
    voice_eff = voice if voice else s["voice"]
    role_eff = role if role else s["role"]
    logger.info("TTS: voice=%s speed=%.2f role=%s (%d символов)",
                voice_eff, speed_eff, role_eff, len(text))

    t0 = time.monotonic()
    try:
        pcm = _synthesize_part(text, url, headers, voice_eff, speed_eff,
                               role_eff, sample_rate_hz)
    except Exception as exc:  # noqa: BLE001
        body = ""
        try:
            body = getattr(getattr(exc, "response", None), "text",
                           "")[:800]  # noqa: BLE001
        except Exception:  # noqa: BLE001
            pass
        logger.exception("Yandex TTS error after %.1fs: %s%s%s",
                         time.monotonic() - t0, exc,
                         "\nbody: " if body else "", body)
        return b""
    logger.info("Yandex TTS: %.1fs, всего %d B pcm (16k mono)",
                time.monotonic() - t0, len(pcm))
    return pcm


def ask_with_pcm(pcm: bytes, system_prompt: str | None = None,
                 temperature: float = 0.5, max_tokens: int = 1000
                 ) -> tuple[str, str]:
    """Единый шаг: аудио (PCM робота) + промпт -> текстовый ответ.

    pcm — сырой PCM (16 кГц/моно/16 бит, int16 LE) от микрофона робота.

    1. Распознаёт речь (SpeechKit STT v3 RecognizeStreaming, raw LINEAR16_PCM);
    2. Отправляет распознанный текст в YandexGPT с системным промптом.

    Возвращает (recognized, answer). Если промпт не задан — берётся из
    конфига yandex.system_prompt; answer может быть "" при ошибке/пусто.
    """
    if not pcm:
        return "", ""
    text = recognize_pcm(pcm)
    if not text:
        return text, ""
    if system_prompt is None:
        system_prompt = default_system_prompt()
    answer = ask_gpt(text, system_prompt=system_prompt,
                     temperature=temperature, max_tokens=max_tokens)
    return text, answer