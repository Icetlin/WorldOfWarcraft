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
    наложится на текущую."""
    player = _detect_player()
    if not player:
        log.error("Нет доступного плеера (pw-play/paplay/aplay/ffplay). "
                  "Установите PulseAudio или PipeWire.")
        return

    try:
        if player == "pw-play":
            # pw-play принимает WAV напрямую
            subprocess.run([player, path], check=True, timeout=60)
        elif player == "paplay":
            subprocess.run([player, path], check=True, timeout=60)
        elif player == "aplay":
            subprocess.run([player, "-q", path], check=True, timeout=60)
        elif player == "ffplay":
            # ffplay -nodisp -autoexit -loglevel quiet
            subprocess.run(
                [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                check=True, timeout=60,
            )
    except subprocess.TimeoutExpired:
        log.warning("Воспроизведение прервано по таймауту")
    except subprocess.CalledProcessError as e:
        log.error("Плеер %s завершился с ошибкой: %s", player, e)


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


# ─── TTS-движок (главный класс) ──────────────────────────────────

class TTSEngine:
    """Очередь + воркер + бэкенд (piper/espeak)."""

    def __init__(self, backend: str = "auto"):
        self._backend = self._init_backend(backend)
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=1)
        self._worker = threading.Thread(
            target=self._run, name="tts-worker", daemon=True
        )
        self._stop = threading.Event()
        self._worker.start()

    def _init_backend(self, backend: str):
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
