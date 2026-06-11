#!/usr/bin/env python3
"""quest_tts_daemon — главный демон озвучки квестов WoW.

Цикл работы:
  1. Найти процесс WoW (psutil по имени WoW.exe).
  2. Каждые QUEST_TTS_SCAN_INTERVAL секунд читать /proc/<pid>/mem,
     искать в heap-регионах маркер §QS§ ... §/QS§.
  3. Извлечённый текст сравнивается с предыдущим; если отличается —
     отправляется в TTS-движок (piper или espeak).
  4. Очередь TTS имеет размер 1, новая фраза вытесняет старую
     (игрок переключил квест → слышит актуальный текст).

Запуск:
  python3 -m daemon.quest_tts_daemon           # рабочий режим
  python3 -m daemon.quest_tts_daemon --test "Привет"
  python3 -m daemon.quest_tts_daemon --check   # проверка piper
  python3 -m daemon.quest_tts_daemon --list    # каталог голосов
  python3 -m daemon.quest_tts_daemon --status  # только статус без TTS
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from typing import Optional

from . import config, tts_engine, wow_memory


def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # piper-ttts шумит на INFO — уменьшим ему болтливость
    logging.getLogger("piper").setLevel(logging.WARNING)
    logging.getLogger("onnxruntime").setLevel(logging.WARNING)


def _check_piper_voice() -> bool:
    """Проверить, что piper-модель валидна."""
    path = config.PIPER_VOICE
    log = logging.getLogger("quest_tts.daemon")

    if not os.path.isfile(path):
        log.error("piper voice не найдена: %s", path)
        log.error("Скачайте модель, например:")
        log.error("  huggingface-cli download rhasspy/piper-voices "
                  "--include 'ru/ru_RU/irina/*' --local-dir models/")
        return False

    # Конфиг-файл должен лежать рядом: model.onnx + model.onnx.json
    cfg_path = path + ".json"
    if not os.path.isfile(cfg_path):
        log.error("Не найден .onnx.json конфиг рядом с %s", path)
        return False

    log.info("Piper voice OK: %s", path)
    return True


def _cmd_test(text: str, backend: str) -> int:
    """Однократно прогнать фразу через TTS (для отладки)."""
    log = logging.getLogger("quest_tts.daemon")
    if not _check_piper_voice():
        return 2
    log.info("Тестовая фраза: %r", text)
    engine = tts_engine.TTSEngine(backend=backend)
    engine.speak(text)
    # ждём, пока воркер обработает
    time.sleep(2.0)
    engine.stop()
    return 0


def _cmd_check(backend: str) -> int:
    log = logging.getLogger("quest_tts.daemon")
    log.info("=== quest_tts_daemon — проверка окружения ===")

    issues = 0

    # Python-зависимости
    try:
        import psutil  # noqa: F401
        log.info("psutil: OK")
    except ImportError:
        log.error("psutil: ОТСУТСТВУЕТ. pip install psutil")
        issues += 1

    if backend in ("piper", "auto"):
        try:
            import piper  # noqa: F401
            log.info("piper-tts: OK")
        except ImportError:
            log.warning("piper-tts: ОТСУТСТВУЕТ — будет использован espeak")
            if backend == "piper":
                issues += 1
        if not _check_piper_voice():
            issues += 1

    if backend in ("espeak", "auto"):
        import shutil
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            log.info("espeak: OK")
        else:
            log.warning("espeak: ОТСУТСТВУЕТ — sudo apt install espeak-ng")

    # Плеер
    log.info("player: %s", tts_engine._detect_player() or "НЕ НАЙДЕН")

    # WoW-процесс
    pid = wow_memory.find_wow_pid()
    if pid:
        log.info("WoW PID: %s", pid)
    else:
        log.warning("WoW процесс не найден — демон будет ждать его запуска")

    log.info("=== ИТОГ: %d проблем ===", issues)
    return 1 if issues else 0


def _cmd_list(backend: str) -> int:
    log = logging.getLogger("quest_tts.daemon")
    log.info("=== Доступные голоса piper (локально) ===")
    models_dir = config.PROJECT_ROOT / "models"
    if not models_dir.is_dir():
        log.info("(каталог models/ пуст — установите piper-модель)")
        return 0
    for f in sorted(models_dir.glob("*.onnx")):
        cfg = f.with_suffix(".onnx.json")
        marker = "OK" if cfg.is_file() else "НЕТ КОНФИГА"
        log.info("  %s — %s", f, marker)

    log.info("=== Скачать русскую модель ===")
    log.info("huggingface-cli download rhasspy/piper-voices \\")
    log.info("    --include 'ru/ru_RU/irina/medium/*' \\")
    log.info("    --local-dir models/")
    return 0


def _cmd_watch(backend: str) -> int:
    """Главный цикл демона."""
    log = logging.getLogger("quest_tts.daemon")
    log.info("=== quest_tts_daemon стартует ===")
    log.info("Интервал сканирования: %.2fs", config.SCAN_INTERVAL)
    log.info("piper voice: %s", config.PIPER_VOICE)

    # Проверка piper-модели заранее, чтобы быстро упасть с понятной ошибкой
    if backend == "piper" and not _check_piper_voice():
        return 2

    engine = tts_engine.TTSEngine(backend=backend)

    last_text: Optional[str] = None
    last_speak_time: float = 0.0
    wow_pid: Optional[int] = None

    stop = False

    def _on_signal(signum, _frame):
        nonlocal stop
        log.info("Получен сигнал %s — завершаюсь", signum)
        stop = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log.info("Готов к работе. Запустите WoW, откройте квест — текст "
             "будет озвучен. Ctrl+C для остановки.")

    while not stop:
        # 1) Проверяем, что WoW всё ещё жив
        if wow_pid is None or not _pid_alive(wow_pid):
            wow_pid = wow_memory.find_wow_pid()
            if wow_pid is None:
                log.info("WoW не найден, жду 5 секунд…")
                if _sleep_or_stop(5.0, lambda: stop):
                    break
                continue
            log.info("WoW найден, PID=%s", wow_pid)

        # 2) Читаем буфер
        try:
            text = wow_memory.read_quest_buffer(wow_pid)
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            # Процесс умер
            log.warning("WoW (PID %s) завершился, ищу заново", wow_pid)
            wow_pid = None
            continue
        except Exception as e:
            log.exception("Ошибка чтения памяти: %s", e)
            if _sleep_or_stop(2.0, lambda: stop):
                break
            continue

        # 3) Обрабатываем результат
        now = time.time()
        if text and (text != last_text or
                     (now - last_speak_time) > config.DEDUP_WINDOW):
            log.info("→ %s", text[:200].replace("\n", " "))
            engine.speak(text)
            last_text = text
            last_speak_time = now
        elif not text and last_text:
            # WoW ушёл с квеста, сбрасываем last_text, чтобы при
            # возврате к тому же квесту заново озвучить
            last_text = None

        if _sleep_or_stop(config.SCAN_INTERVAL, lambda: stop):
            break

    engine.stop()
    log.info("quest_tts_daemon остановлен")
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _sleep_or_stop(seconds: float, stop_predicate) -> bool:
    """Сон с возможностью прерывания по stop_predicate()."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if stop_predicate():
            return True
        time.sleep(0.1)
    return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="quest_tts_daemon",
        description="Озвучка квестов WoW через TTS (piper/espeak).",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="подробный лог",
    )
    parser.add_argument(
        "--backend", choices=["auto", "piper", "espeak"], default="auto",
        help="TTS-бэкенд (по умолчанию auto → piper с fallback на espeak)",
    )

    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("watch", help="рабочий режим (по умолчанию)")
    sub.add_parser("check", help="проверить окружение")

    p_list = sub.add_parser("list", help="показать установленные голоса")

    p_test = sub.add_parser("test", help="однократно прогнать фразу")
    p_test.add_argument("text", nargs="?", default="Привет, мир! "
                       "Это тестовая фраза для проверки озвучки.")

    sub.add_parser("status", help="краткий статус (как check)")

    args = parser.parse_args(argv)
    _setup_logging(args.debug)

    cmd = args.cmd or "watch"

    if cmd == "watch":
        return _cmd_watch(args.backend)
    if cmd == "check":
        return _cmd_check(args.backend)
    if cmd == "list":
        return _cmd_list(args.backend)
    if cmd == "test":
        return _cmd_test(args.text, args.backend)
    if cmd == "status":
        return _cmd_check(args.backend)
    return 1


if __name__ == "__main__":
    sys.exit(main())
