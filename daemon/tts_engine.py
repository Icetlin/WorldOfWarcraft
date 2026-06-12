"""TTS-движок: piper (по умолчанию) с fallback на espeak-ng.

Архитектура:

  ┌──────────┐    push(text)     ┌──────────────┐
  │ демон    │ ────────────────► │  TTS queue   │
  └──────────┘                   │  (1 слот)    │
                                 └──────┬───────┘
                                        │ take
                                        ▼
                                 ┌──────────────┐
                                 │  worker      │ ──► синтез WAV (piper/espeak)
                                 │  (поток)     │ ──► воспроизведение (pw-play/paplay)
                                 └──────────────┘

Если в очереди уже что-то лежит, новый текст его ВЫТЕСНЯЕТ: игрок только
что сменил квест — ему важнее услышать новый, а не договаривать старый.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional

from . import config

log = logging.getLogger("quest_tts.tts")


# ─── Воспроизведение ───────────────────────────────────────────────

def _detect_player() -> Optional[str]:
    """Выбрать программу воспроизведения: pw-play → paplay → aplay."""
    if config.PLAYER != "auto":
        if shutil.which(config.PLAYER):
            return config.PLAYER
        log.warning("Запрошенный плеер %s не найден, fallback на auto",
                    config.PLAYER)

    for cand in ("pw-play", "paplay", "aplay", "ffplay"):
        if shutil.which(cand):
            return cand
    return None


def _play_wav(path: str) -> None:
    """Проиграть WAV-файл. Ждём завершения — иначе следующая фраза
    наложится на текущую.

    Если основной плеер не сработал (типичный случай: pw-play
    установлен, но PipeWire-сессия не запущена → exit 1), пробуем
    следующие по цепочке. paplay живёт в обоих мирах (PulseAudio и
    PipeWire при наличии pipewire-pulse), поэтому почти всегда
    выручает.
    """
    primary = _detect_player()
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
    for fallback in ("paplay", "aplay", "ffplay"):
        if fallback not in candidates and shutil.which(fallback):
            candidates.append(fallback)

    if not candidates:
        log.error("Нет доступного плеера (pw-play/paplay/aplay/ffplay). "
                  "Установите PulseAudio или PipeWire.")
        return

    for player in candidates:
        try:
            if player == "pw-play":
                subprocess.run([player, path], check=True, timeout=60)
            elif player == "paplay":
                subprocess.run([player, path], check=True, timeout=60)
            elif player == "aplay":
                subprocess.run([player, "-q", path], check=True, timeout=60)
            elif player == "ffplay":
                subprocess.run(
                    [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                    check=True, timeout=60,
                )
            return  # success
        except subprocess.TimeoutExpired:
            log.warning("Плеер %s превысил таймаут", player)
            return
        except FileNotFoundError:
            log.warning("Плеер %s исчез из PATH, пробую следующий", player)
            continue
        except subprocess.CalledProcessError as e:
            log.warning("Плеер %s завершился с ошибкой (%s), пробую следующий",
                        player, e)
            continue

    log.error("Ни один из %s не смог проиграть файл", candidates)


# ─── Синтез речи ──────────────────────────────────────────────────

class _PiperBackend:
    """Обёртка piper-tts."""

    def __init__(self, voice_path: str):
        # Импортируем только когда реально используем — чтобы не падать
        # при отсутствии piper-tts на машине, где стоит fallback espeak.
        from piper import PiperVoice  # type: ignore

        if not os.path.isfile(voice_path):
            raise FileNotFoundError(
                f"piper voice not found: {voice_path}\n"
                "Скачайте модель, например:\n"
                "  huggingface-cli download rhasspy/piper-voices "
                "--include 'ru/ru_RU/irina/*' --local-dir models/")

        log.info("Загружаю piper voice: %s", voice_path)
        self._voice = PiperVoice.load(voice_path)
        log.info("Piper voice загружена: %s, sample_rate=%s",
                 voice_path, getattr(self._voice.config, "sample_rate", "?"))

    def synthesize(self, text: str) -> Optional[bytes]:
        """Синтез текста в WAV-байты (Piper возвращает chunks)."""
        from piper import SynthesisConfig  # type: ignore

        cfg = SynthesisConfig(
            # Чуть медленнее дефолта — для русского так разборчивее.
            length_scale=1.05,
            # Шёпот/интонация не нужны для квестов
            noise_scale=0.667,
            noise_w_scale=0.8,
        )

        chunks = []
        try:
            for chunk in self._voice.synthesize(text, cfg):
                # chunk.audio_int16_bytes — int16 PCM
                # chunk.sample_rate — int
                chunks.append(chunk.audio_int16_bytes)
        except Exception as e:
            log.exception("Ошибка синтеза piper: %s", e)
            return None

        if not chunks:
            return None

        # Собираем PCM и оборачиваем в WAV-хедер.
        import wave
        import io

        sample_rate = self._voice.config.sample_rate
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)  # int16
            w.setframerate(sample_rate)
            for c in chunks:
                w.writeframes(c)
        return buf.getvalue()


class _EspeakBackend:
    """Fallback на espeak-ng, если piper недоступен.

    Качество низкое, но работает out-of-the-box: espeak-ng есть в
    репозиториях почти любого дистрибутива.
    """

    def __init__(self):
        if not shutil.which("espeak-ng") and not shutil.which("espeak"):
            raise FileNotFoundError(
                "espeak-ng не найден. Установите: sudo apt install espeak-ng")
        self._bin = "espeak-ng" if shutil.which("espeak-ng") else "espeak"
        log.warning("Использую espeak (fallback). Качество ниже, чем у piper.")

    def synthesize(self, text: str) -> Optional[bytes]:
        # espeak-ng умеет писать WAV в stdout с флагом --stdout
        try:
            r = subprocess.run(
                [self._bin, "-v", "ru", "-s", "160", "-w", "-", text],
                check=True, timeout=30, capture_output=True,
            )
            return r.stdout if r.stdout else None
        except subprocess.CalledProcessError as e:
            log.error("espeak ошибка: %s", e)
            return None
        except subprocess.TimeoutExpired:
            log.error("espeak таймаут")
            return None


class _SileroBackend:
    """Обёртка Silero TTS v4 (через torch.hub).

    Silero — российская модель, обученная ПРЕИМУЩЕСТВЕННО на русском.
    Голоса звучат как живые дикторы, в отличие от piper, который
    режет интонации espeak-фонемизатором. На 5–10-секундных квестовых
    фразах CPU inference занимает ~0.3 сек, GPU не нужен — а VRAM
    остаётся свободной для WoW.
    """

    # Доступные русские голоса silero v4 (model="silero_tts", language="ru").
    # У *v2 голосов sample_rate фиксирован в YAML — см. SPEAKER_SR ниже.
    # Спикеры *_8khz / *_16khz используют СТАРУЮ jit-модель с другим API
    # (hub.load возвращает 5 значений, а не 2), поэтому пока не поддержаны.
    SPEAKERS = ("baya_v2", "irina_v2", "kseniya_v2",
                "natasha_v2", "aidar_v2", "ruslan_v2")
    # sample_rate для каждого голоса (из silero-models/models.yml).
    # Для всех *v2 поддерживается только 8k/16k, 48k даёт pitch-shifted мусор.
    SPEAKER_SR = {
        "baya_v2":    16000,
        "irina_v2":   16000,
        "kseniya_v2": 16000,
        "natasha_v2": 16000,
        "aidar_v2":   16000,
        "ruslan_v2":  16000,
    }
    SAMPLE_WIDTH = 2  # int16

    def __init__(self, speaker: str = "baya_v2"):
        if speaker not in self.SPEAKERS:
            raise ValueError(
                f"Неизвестный голос silero: {speaker!r}. "
                f"Доступные: {', '.join(self.SPEAKERS)}"
            )
        self._speaker = speaker
        # Спикеро-зависимая частота дискретизации (см. SPEAKER_SR).
        # Не путать с v4_ru (там до 48kHz) — у *v2 только 8/16 kHz.
        self._sample_rate = self.SPEAKER_SR[speaker]

        import torch  # noqa: F401 — проверляем, что torch вообще есть
        import torch.hub as hub

        log.info("Загружаю silero v4 (голос: %s)…", speaker)
        # torch.hub сам кеширует модель в ~/.cache/torch/hub/.
        # trust_repo=True — silero-models это официальный репо Snakers,
        # иначе torch.hub будет интерактивно спрашивать подтверждение.
        self._model, _ = hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker=speaker,
            trust_repo=True,
        )
        # CPU специально: для коротких фраз (<10 сек) GPU-overhead
        # не окупается, а VRAM остаётся WoW'у. Если захочется
        # ускорить — поменять на "cuda".
        self._device = "cpu"
        log.info("Silero v4 загружен: speaker=%s, device=%s, sr=%d",
                 speaker, self._device, self._sample_rate)

        # Акцентор: silero v2 ждёт ударения, размеченные '+' перед ударной
        # гласной (Съ+ешьте). Без этого модель выдаёт тишину или кашу.
        # ruaccent — нейросеть-акцентуатор, ~35 мс на фразу, грузим один раз.
        # ВАЖНО: omograph_model_size="turbo" в новых версиях ruaccent ломает
        # инференс (модель ждёт token_type_ids, который не подаётся) →
        # process_all() падает с «Required inputs missing». Поэтому НЕ
        # передаём omograph_model_size — дефолтный режим + словарь работает
        # стабильно и не требует BERT-inputs.
        self._accentor = None
        try:
            from ruaccent import RUAccent
            acc = RUAccent()
            acc.load(use_dictionary=True)
            self._accentor = acc
            log.info("ruaccent загружен (словарь)")
        except Exception as e:
            log.warning("ruaccent недоступен (%s) — silero будет выдавать "
                        "тишину/кашу. Поставьте: pip install ruaccent", e)

    # Silero v2 ругается warning'ом на тексты > 140 символов и реально
    # деградирует: pitch-контур «плывёт», ударения теряются. Поэтому
    # разбиваем длинные квестовые тексты на чанки по предложениям.
    # Для квестов WoW типичная длина 30-80 символов, чанкинг сработает
    # почти всегда «как есть» (1 чанк), а редкие длинные склеит из
    # нескольких коротких с паузой между ними.
    MAX_CHUNK_CHARS = 130

    def _split_into_chunks(self, text: str) -> list[str]:
        """Разбить текст на куски ≤ MAX_CHUNK_CHARS по границам предложений.

        Стратегия:
        1) Сначала пытаемся резать по . ! ? (сохраняя знак препинания).
        2) Если предложение всё равно > MAX_CHUNK_CHARS — режем по
           запятым/точкам с запятой.
        3) Если и так не влезает (нет других знаков) — режем по словам
           в районе границы. Крайний случай — один кусок, как было.
        """
        import re
        # Сначала по предложениям
        sentences = re.split(r'(?<=[.!?…])\s+', text.strip())
        chunks: list[str] = []
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(s) <= self.MAX_CHUNK_CHARS:
                chunks.append(s)
                continue
            # Предложение длинное — режем по ; , :
            parts = re.split(r'(?<=[;:])\s+|(?<=,)\s+', s)
            buf = ""
            for p in parts:
                if buf and len(buf) + 1 + len(p) > self.MAX_CHUNK_CHARS:
                    chunks.append(buf)
                    buf = p
                else:
                    buf = (buf + " " + p).strip() if buf else p
            if buf:
                chunks.append(buf)
        return chunks or [text]

    def synthesize(self, text: str) -> Optional[bytes]:
        # Шаг 1: простановка ударений. Silero v2 ждёт «+» перед ударной
        # гласной (модель называется TTSModelAcc_v2 — Accentor). Без
        # акцентора модель выдаёт тишину. ruaccent стоит ~35 мс на фразу.
        if self._accentor is not None:
            try:
                text = self._accentor.process_all(text)
            except Exception as e:
                log.warning("ruaccent.process_all упал (%s), шлю без '+'", e)

        # Шаг 2: разбить длинный текст на чанки ≤ 130 символов (silero v2
        # деградирует на длинных строках, ругается warning'ом).
        chunks = self._split_into_chunks(text)
        if len(chunks) > 1:
            log.debug("silero: разбил на %d чанк(ов) по %d..%d символов",
                      len(chunks),
                      min(len(c) for c in chunks),
                      max(len(c) for c in chunks))

        # Шаг 3: синтез каждого чанка отдельно, склейка PCM.
        import torch
        all_pcm: list[bytes] = []
        silence_bytes: bytes = b""
        for i, chunk in enumerate(chunks):
            try:
                audios = self._model.apply_tts(
                    texts=chunk,
                    sample_rate=self._sample_rate,
                )
            except Exception as e:
                log.exception("Ошибка синтеза silero на чанке %d/%d: %s",
                              i + 1, len(chunks), e)
                continue

            if not audios:
                continue

            # audio: torch.Tensor(float32) в [-0.1, 0.1] обычно. clamp +
            # масштабирование в int16. Делаем один раз.
            audio_int16 = (audios[0].clamp(-1.0, 1.0) * 32767).to(torch.int16)
            all_pcm.append(audio_int16.numpy().tobytes())

            # Пауза 0.15 сек между чанками, чтобы на стыке не было
            # «проглатывания» последней гласной / склейки в одно слово.
            if i + 1 < len(chunks) and not silence_bytes:
                silence_samples = int(0.15 * self._sample_rate)
                silence_bytes = (b"\x00\x00") * silence_samples

        if not all_pcm:
            return None

        pcm_bytes = b"".join(
            chunk + silence_bytes if i + 1 < len(all_pcm) else chunk
            for i, chunk in enumerate(all_pcm)
        )

        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(self.SAMPLE_WIDTH)
            w.setframerate(self._sample_rate)
            w.writeframes(pcm_bytes)
        return buf.getvalue()


# ─── TTS-движок (главный класс) ──────────────────────────────────

class TTSEngine:
    """Очередь + воркер + бэкенд (silero/piper/espeak)."""

    def __init__(self, backend: str = "auto"):
        self._backend = self._init_backend(backend)
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=1)
        self._worker = threading.Thread(
            target=self._run, name="tts-worker", daemon=True
        )
        self._stop = threading.Event()
        self._worker.start()

    def _init_backend(self, backend: str):
        # auto: silero → piper → espeak
        if backend in ("silero", "auto"):
            try:
                return _SileroBackend(config.SILERO_SPEAKER)
            except (ImportError, ValueError) as e:
                if backend == "silero":
                    raise
                log.warning("silero недоступен (%s), пробую piper", e)
        if backend in ("piper", "auto"):
            try:
                return _PiperBackend(config.PIPER_VOICE)
            except (ImportError, FileNotFoundError) as e:
                if backend == "piper":
                    raise
                log.warning("piper недоступен (%s), пробую espeak", e)
        if backend in ("espeak", "auto"):
            try:
                return _EspeakBackend()
            except FileNotFoundError as e:
                if backend == "espeak":
                    raise
                log.error("espeak тоже недоступен: %s", e)
                raise

    def speak(self, text: str) -> None:
        """Положить текст в очередь. Если там уже что-то есть — заменить."""
        if not text or not text.strip():
            return
        # Очередь с maxsize=1 → старая фраза автоматически вытесняется
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            try:
                self._queue.get_nowait()  # выкидываем старое
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(text)
            except queue.Full:
                pass

    def stop(self) -> None:
        """Остановить воркер (например, при завершении демона)."""
        self._stop.set()
        # кладём пустую строку-маркер остановки
        try:
            self._queue.put_nowait("__STOP__")
        except queue.Full:
            pass

    def _run(self) -> None:
        log.info("TTS-воркер запущен")
        while not self._stop.is_set():
            try:
                text = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if text == "__STOP__":
                break

            t0 = time.time()
            try:
                wav = self._backend.synthesize(text)
            except Exception as e:
                log.exception("Сбой бэкенда: %s", e)
                wav = None

            if wav is None or len(wav) == 0:
                continue

            # Сохраняем во временный файл и проигрываем
            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=True
            ) as f:
                f.write(wav)
                f.flush()
                _play_wav(f.name)

            log.debug("Озвучено за %.2fs: %s",
                      time.time() - t0, text[:80])
        log.info("TTS-воркер остановлен")
