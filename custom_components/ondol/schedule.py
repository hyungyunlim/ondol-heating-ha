"""Weekly schedule text, one line per group of days:

    월-금 06:30 편안, 09:30 절약, 17:00 편안, 22:30 수면
    토,일 08:00 편안, 22:30 수면

A block lasts until the next entry, across days; a day without a line keeps the last block.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from .const import PRESET_LABELS, PRESETS

DAYS = {name: i for i, name in enumerate("월화수목금토일")} | {
    name: i for i, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
}
EVERY_DAY = {"매일", "daily"}
PRESET_NAMES = {label: key for key, label in PRESET_LABELS.items()} | {key: key for key in PRESETS}
ENTRY = re.compile(r"^(\d{1,2}):(\d{2})\s+(\S+)$")
WEEK = 7 * 1440

Entries = tuple[tuple[int, str], ...]  # (minute of the week, preset), sorted


class ScheduleError(ValueError):
    """The schedule text can't be read."""


@dataclass(frozen=True)
class Block:
    start: datetime
    preset: str


def _days(spec: str, line: int) -> list[int]:
    if spec.lower() in EVERY_DAY:
        return list(range(7))
    days: list[int] = []
    for part in spec.lower().split(","):
        first, _, last = part.partition("-")
        if first not in DAYS or (last and last not in DAYS):
            raise ScheduleError(f"line {line}: unknown day '{part}'")
        a, b = DAYS[first], DAYS[last or first]
        days += [(a + i) % 7 for i in range((b - a) % 7 + 1)]
    return days


def parse(text: str) -> Entries:
    entries: dict[int, str] = {}
    for line, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        spec, _, rest = raw.partition(" ")
        days = _days(spec, line)
        for item in rest.split(","):
            match = ENTRY.match(item.strip())
            if not match:
                raise ScheduleError(f"line {line}: expected 'HH:MM preset', got '{item.strip()}'")
            hour, minute, name = int(match[1]), int(match[2]), match[3].lower()
            if hour > 23 or minute > 59:
                raise ScheduleError(f"line {line}: bad time '{item.strip()}'")
            if name not in PRESET_NAMES:
                raise ScheduleError(f"line {line}: unknown preset '{match[3]}'")
            for day in days:
                entries[day * 1440 + hour * 60 + minute] = PRESET_NAMES[name]
    if not entries:
        raise ScheduleError("the schedule is empty")
    return tuple(sorted(entries.items()))


def _week_start(when: datetime) -> datetime:
    return (when - timedelta(days=when.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def block_at(entries: Entries, when: datetime) -> Block:
    minute = when.weekday() * 1440 + when.hour * 60 + when.minute
    week = _week_start(when)
    current = [e for e in entries if e[0] <= minute]
    if current:
        start, preset = current[-1]
        return Block(week + timedelta(minutes=start), preset)
    start, preset = entries[-1]  # still in last week's final block
    return Block(week + timedelta(minutes=start - WEEK), preset)


def upcoming(entries: Entries, when: datetime, hours: float) -> list[Block]:
    """Blocks starting after ``when`` and within ``hours``."""
    week = _week_start(when)
    end = when + timedelta(hours=hours)
    blocks = []
    for weeks in range(int(hours // 168) + 2):
        for minute, preset in entries:
            start = week + timedelta(minutes=minute + weeks * WEEK)
            if when < start <= end:
                blocks.append(Block(start, preset))
    return blocks
