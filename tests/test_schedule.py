from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.ondol.schedule import ScheduleError, block_at, parse, upcoming

KST = ZoneInfo("Asia/Seoul")
TEXT = "월-금 06:30 편안, 09:30 절약, 17:00 편안, 22:30 수면\n토,일 08:00 편안, 22:30 수면"


def at(day: int, hm: str) -> datetime:
    """``day`` days after Monday 2026-10-26."""
    hour, minute = map(int, hm.split(":"))
    return datetime(2026, 10, 26, tzinfo=KST) + timedelta(days=day, hours=hour, minutes=minute)


def test_blocks_follow_the_week():
    entries = parse(TEXT)
    assert block_at(entries, at(0, "07:00")).preset == "comfort"
    assert block_at(entries, at(0, "12:00")).preset == "eco"
    assert block_at(entries, at(0, "23:00")).preset == "sleep"
    saturday = block_at(entries, at(5, "07:00"))  # still Friday night
    assert (saturday.preset, saturday.start) == ("sleep", at(4, "22:30"))
    monday = block_at(entries, at(0, "05:00"))  # wraps back to last Sunday night
    assert monday.start == datetime(2026, 10, 25, 22, 30, tzinfo=KST)


def test_upcoming_crosses_days_and_weeks():
    blocks = upcoming(parse(TEXT), at(6, "21:00"), 12)  # Sunday evening
    assert [(b.start, b.preset) for b in blocks] == [
        (at(6, "22:30"), "sleep"),
        (datetime(2026, 11, 2, 6, 30, tzinfo=KST), "comfort"),
    ]


def test_names_in_either_language():
    assert parse("daily 07:00 comfort, 23:00 sleep") == parse("매일 07:00 편안, 23:00 수면")
    assert parse("mon-sun 07:00 comfort") == parse("매일 07:00 편안")
    assert {minute // 1440 for minute, _ in parse("금-월 08:00 편안")} == {4, 5, 6, 0}


@pytest.mark.parametrize(
    "text", ["", "# only a comment", "월-금 7시 편안", "월-금 25:00 편안", "월-금 07:00 따뜻", "휴일 07:00 편안", "월-금"]
)
def test_bad_schedules(text):
    with pytest.raises(ScheduleError):
        parse(text)
