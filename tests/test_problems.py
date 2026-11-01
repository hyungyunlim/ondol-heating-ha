from datetime import datetime, timedelta

import pytest

from custom_components.ondol.model import Params
from custom_components.ondol.problems import Run, follow, no_heat

START = datetime.fromisoformat("2026-11-01T20:00:00+09:00")
BABY_ROOM = Params(dead_time=1.53, rise=0.2537, loss=0.0071)  # learned 2026-11-01


def first_flag(params: Params, readings: dict[float, float], outdoor: float = 8.0, hours: float = 12):
    """Heat from START with the Nest reporting ``readings`` ({hours in: °C}); (hours, reason) when flagged."""
    run, temp = None, readings[0]
    for i in range(int(hours * 12) + 1):
        h = i / 12
        temp = readings.get(h, temp)
        now = START + timedelta(hours=h)
        run = follow(run, True, temp, now)
        if reason := no_heat(run, temp, params, outdoor, now):
            return h, reason
    return None


def test_heated_is_what_lead_solves():
    params = BABY_ROOM
    for temp, goal in ((25.0, 25.8), (24.0, 26.0), (25.5, 25.4)):
        assert params.heated(temp, 8.0, params.lead(temp, goal, 8.0)) == pytest.approx(max(goal, params.idle(temp, 8.0, params.dead_time)))


def test_a_room_that_stops_warming_is_flagged():
    found = first_flag(BABY_ROOM, {0: 25.3, 1.5: 25.2})  # fell through the 25.25 display flip and stayed
    assert found is not None
    hours, reason = found
    assert 6.0 <= hours <= 6.3  # once the model expects +0.4 °C
    assert reason.startswith("6시간 ") and "온도가 오르지 않아요 (25.3° → 25.2°, 예상 +0.4°)" in reason
    assert reason.endswith("구동기나 단지 난방 공급을 확인해 주세요.")
    assert not any(c in reason for c in "_*[`")  # plain for Telegram's Markdown


def test_normal_and_slow_rooms_stay_silent():
    assert first_flag(BABY_ROOM, {0: 25.3, 3: 25.8}) is None  # a normal run
    # no new report for 12 h: inside one 0.5 °C display step the room may still have risen 0.4
    assert first_flag(BABY_ROOM, {0: 25.3}) is None
    # a cold snap at the slab's limit: the model itself expects the room to fall
    assert first_flag(Params(rise=0.1, loss=0.008), {0: 25.3, 2: 24.7}, outdoor=-10.0) is None


def test_a_run_is_judged_only_after_the_dead_time_and_some():
    quick = Params(dead_time=0.5, rise=0.6, loss=0.0)  # expects +0.4 °C within 1.2 h
    assert first_flag(quick, {0: 25.3, 0.25: 25.2})[0] == pytest.approx(2.0)


def test_short_idles_dont_end_a_run():
    run = follow(None, True, 25.3, START)
    assert follow(run, False, 25.3, START + timedelta(minutes=5)).idle is not None
    run = follow(run, True, 25.3, START + timedelta(minutes=12))  # idled 7 minutes
    assert run.start == START and run.idle is None
    follow(run, False, 25.3, START + timedelta(minutes=20))
    run = follow(run, True, 25.2, START + timedelta(minutes=31))  # idled 11 minutes: a new run
    assert (run.start, run.temp) == (START + timedelta(minutes=31), 25.2)
    run = follow(run, True, 25.2, START + timedelta(hours=2))  # not watched for 89 minutes
    assert run.start == START + timedelta(hours=2)
    assert Run.from_dict(run.as_dict()) == run
