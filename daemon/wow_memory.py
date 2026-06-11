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
    open_len = len(open_b)
    close_len = len(close_b)

    for region in targets:
        if region.size > 200 * 1024 * 1024:  # >200 МБ — пропускаем
            continue
        try:
            blob = _read_region(pid, region)
        except (OSError, OverflowError) as e:
            if config.DEBUG:
                log.debug("Не прочитан регион %s: %s", region, e)
            continue

        # Собираем все позиции открывающего и закрывающего маркеров.
        # В heap может лежать несколько полных пар (старая dataFrame.text
        # от прошлого квеста + новая), поэтому нужен явный «плотный»
        # отбор: open и close из одной пары, без других open между ними.
        opens: list[int] = []
        closes: list[int] = []
        p = 0
        while True:
            i = blob.find(open_b, p)
            if i < 0:
                break
            opens.append(i)
            p = i + 1
        p = 0
        while True:
            i = blob.find(close_b, p)
            if i < 0:
                break
            closes.append(i)
            p = i + 1
        if not opens or not closes:
            continue

        # Идём от последнего open назад (самая свежая запись —
        # последний аллок). Для каждого open ищем первый close после
        # него; если между ними есть ещё один open — это close от
        # более поздней пары, пропускаем.
        for open_idx in reversed(opens):
            close_after = [c for c in closes if c > open_idx]
            if not close_after:
                break  # нет ни одного close после любого open — мусор
            close_idx = close_after[0]

            if any(o > open_idx and o < close_idx for o in opens):
                # Между этим open и найденным close лежит ещё один open
                # → close принадлежит более поздней паре, и байты между
                # ними — мусор из heap, а не текст. Пропускаем.
                continue

            text_bytes = blob[open_idx + open_len : close_idx]
            if b"\x00" in text_bytes:
                # NUL внутри пары = точно не квестовый текст (Lua-строки
                # не содержат '\0' в нашем коде), пропускаем.
                continue

            try:
                text = text_bytes.decode("utf-8")
            except UnicodeDecodeError:
                # Битый UTF-8 между маркерами = скорее всего мусор.
                continue

            text = text.strip()
            if not text:
                continue

            if config.DEBUG:
                log.debug("Marker hit: len=%d preview=%r",
                          len(text), text[:80])
            return text

    return None
