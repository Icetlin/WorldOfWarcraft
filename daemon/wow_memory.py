"""Чтение Lua-буфера из памяти процесса WoW.

Идея: Lua-аддон QuestSpeak пишет текст в _QuestSpeak_Buffer с маркерами
§QS§ ... §/QS§. Демон ищет этот маркер в памяти процесса WoW через
/proc/<pid>/maps + /proc/<pid>/mem и возвращает извлечённый текст.

Чтобы не сканировать всю память (WoW под Proton занимает 1.5–3 ГБ),
ограничиваемся регионами с пометкой [heap] и wine-аллокатором. Этого
достаточно: lua-строки WoW живут в heap-памяти процесса.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil

from . import config

log = logging.getLogger("quest_tts.memory")


# ─── Поиск PID WoW ─────────────────────────────────────────────────

def find_wow_pid() -> Optional[int]:
    """Найти PID процесса WoW.exe (или совместимое имя)."""
    wanted = {n.lower() for n in config.WOW_PROCESS_NAMES}

    for proc in psutil.process_iter(attrs=["pid", "name", "exe"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name in wanted:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Fallback: иногда имя процесса обрезано. Ищем по exe-пути.
    for proc in psutil.process_iter(attrs=["pid", "exe", "cmdline"]):
        try:
            exe = (proc.info["exe"] or "").lower()
            cmd = " ".join(proc.info["cmdline"] or []).lower()
            if "wow" in exe and "wow.exe" in exe:
                return proc.info["pid"]
            if "wow.exe" in cmd or "world of warcraft" in cmd:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return None


# ─── Карта памяти ──────────────────────────────────────────────────

# Пример строки maps:
# 7f1234000000-7f1234100000 r--p 00000000 08:01 12345   /path/lib.so
# 55a000000000-55a000100000 rw-p 00000000 00:00 0      [heap]
_RE_MAPS_LINE = re.compile(
    r"^([0-9a-f]+)-([0-9a-f]+)\s+([rwxp-]{4})\s+[0-9a-f]+\s+"
    r"[0-9a-f]+:[0-9a-f]+\s+\d+\s*(.*)$"
)


@dataclass(frozen=True)
class MemRegion:
    start: int
    end: int
    perms: str
    name: str

    @property
    def size(self) -> int:
        return self.end - self.start


def parse_maps(pid: int) -> list[MemRegion]:
    """Прочитать /proc/<pid>/maps и вернуть регионы."""
    try:
        with open(f"/proc/{pid}/maps", "r") as f:
            data = f.read()
    except (OSError, PermissionError) as e:
        log.warning("Не удалось прочитать maps для PID %s: %s", pid, e)
        return []

    out: list[MemRegion] = []
    for line in data.splitlines():
        m = _RE_MAPS_LINE.match(line)
        if not m:
            continue
        start = int(m.group(1), 16)
        end = int(m.group(2), 16)
        perms = m.group(3)
        name = (m.group(4) or "").strip()
        if "r" not in perms:
            continue  # нам нужны только readable регионы
        out.append(MemRegion(start=start, end=end, perms=perms, name=name))
    return out


def _is_wine_heap(r: MemRegion) -> bool:
    """Эвристика: регион, в котором может жить Lua-строка WoW.

    Под Proton/Wine lua-строки WoW лежат в heap-памяти (пометка [heap])
    или в больших анонимных rw-областях без пути. Wine иногда мапит
    память с именем «WoW.exe» или пустым путём.
    """
    if r.name == "[heap]":
        return True
    if r.name == "" and "w" in r.perms:
        # анонимный rw-регион, обычно это куча/аллокаторы
        return r.size > 64 * 1024  # игнорируем крошечные регионы
    if r.name and "wow" in r.name.lower():
        return True
    return False


# ─── Чтение байтов региона ────────────────────────────────────────

def _read_region(pid: int, region: MemRegion) -> bytes:
    """Прочитать байты региона через os.pread — это в ~5× быстрее, чем
    обычный seek+read, потому что избегает обёрток Python."""
    fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    try:
        return os.pread(fd, region.size, region.start)
    finally:
        os.close(fd)


# ─── Главная функция ──────────────────────────────────────────────

def read_quest_buffer(pid: int) -> Optional[str]:
    """Найти и вернуть текст из _QuestSpeak_Buffer.

    Возвращает None, если маркер не найден (аддон не загружен, ещё не
    сработал, или текст пуст). При ошибках чтения возвращает None и
    логирует.
    """
    regions = parse_maps(pid)
    if not regions:
        return None

    if config.SCAN_HEAP_ONLY:
        targets = [r for r in regions if _is_wine_heap(r)]
    else:
        targets = regions

    if config.DEBUG:
        log.debug("Найдено регионов: %d (heap/wine: %d)",
                  len(regions), len(targets))

    open_b = config.MARKER_OPEN_BYTES
    close_b = config.MARKER_CLOSE_BYTES

    for region in targets:
        if region.size > 200 * 1024 * 1024:  # >200 МБ — пропускаем
            continue
        try:
            blob = _read_region(pid, region)
        except (OSError, OverflowError) as e:
            if config.DEBUG:
                log.debug("Не прочитан регион %s: %s", region, e)
            continue

        idx = blob.find(open_b)
        if idx < 0:
            continue

        # Нашли открывающий маркер. Ищем закрывающий после него.
        close_idx = blob.find(close_b, idx + len(open_b))
        if close_idx < 0:
            # Маркер оборван — возможно, lua пишет строку прямо сейчас.
            # Пропускаем, в следующем тике прочтём.
            continue

        text_bytes = blob[idx + len(open_b) : close_idx]

        # WoW в Wine — всегда UTF-8.
        try:
            text = text_bytes.decode("utf-8", errors="replace")
        except Exception:
            continue

        text = text.strip()
        if not text:
            continue
        return text

    return None
