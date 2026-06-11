"""Конфигурация quest_tts_daemon.

Все параметры можно переопределить через переменные окружения:
    QUEST_TTS_SCAN_INTERVAL  — период опроса памяти в секундах (default 0.4)
    QUEST_TTS_PLAYER         — программа воспроизведения: pw-play, paplay, auto
    QUEST_TTS_PIPER_BIN      — путь к бинарю piper (если не через pip-модуль)
    QUEST_TTS_VOICE          — путь к .onnx + .onnx.json голоса piper
    QUEST_TTS_DEBUG          — 1 для подробного лога
"""

import os
from pathlib import Path

# --- Корень проекта ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Lua-маркеры (UTF-8). Должны совпадать с QuestSpeak.lua ---
MARKER_OPEN_BYTES  = "§QS§".encode("utf-8")
MARKER_CLOSE_BYTES = "§/QS§".encode("utf-8")

# --- Процесс WoW: имена, по которым ищем ---
# Под Proton/Wine это «WoW.exe», под нативным Mac/Win — то же.
WOW_PROCESS_NAMES = (
    "WoW.exe",      # стандартное имя под Wine/Proton
    "Wow.exe",      # иногда так
    "Wow-64.exe",   # macOS
    "World of Warcraft.exe",
)

# --- Чтение памяти ---
# WoW-процесс под Proton занимает 1.5–3 ГБ; lua-строки обычно лежат
# в регионах с пометкой [heap] (это heap аллокатора Wine). Сканируем только их.
# Если не нашли — fallback на все readable-регионы.
SCAN_HEAP_ONLY = True

# Таймаут на чтение одного региона, сек
READ_TIMEOUT = 2.0

# --- Период опроса ---
SCAN_INTERVAL = float(os.environ.get("QUEST_TTS_SCAN_INTERVAL", "0.4"))

# --- TTS (piper) ---
# Голос — путь к .onnx файлу. Скачивается install.sh.
PIPER_VOICE = os.environ.get(
    "QUEST_TTS_VOICE",
    str(PROJECT_ROOT / "models" / "ru_RU-irina-medium.onnx"),
)

# Piper может быть установлен как пакет (тогда используем python API)
# или как бинарь (тогда subprocess). По умолчанию пробуем python.
PIPER_BIN = os.environ.get("QUEST_TTS_PIPER_BIN", "")

# --- Воспроизведение ---
# По умолчанию auto: сначала pw-play (PipeWire), затем paplay (PulseAudio).
PLAYER = os.environ.get("QUEST_TTS_PLAYER", "auto")

# --- Дедупликация: не повторять тот же текст внутри N секунд ---
DEDUP_WINDOW = 5.0

# --- Логирование ---
DEBUG = os.environ.get("QUEST_TTS_DEBUG", "0") == "1"
