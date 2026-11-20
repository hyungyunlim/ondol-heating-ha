from datetime import datetime, timedelta

from custom_components.ondol.problems import Run, follow, no_heat, warmed

START = datetime.fromisoformat("2026-11-19T00:22:00+09:00")
DEAD = 1.37  # 아기방's dead time then


def first_flag(nest: dict[float, float], fine: dict[float, float] | None = None, hours: float = 8):
    """Heat from START with the Nest reporting ``nest`` and a finer sensor ``fine`` ({hours in: °C});
    (hours, reason) when flagged."""
    run, temp, f = None, nest[0], fine[0] if fine else None
    for i in range(int(hours * 12) + 1):
        h = i / 12
        temp = nest.get(h, temp)
        f = fine.get(h, f) if fine else None
        now = START + timedelta(hours=h)
        run = follow(run, True, temp, now, f)
        if reason := no_heat(run, temp, f, DEAD, now):
            return h, reason
    return None


def falling(start: float, per_hour: float, hours: float = 8) -> dict[float, float]:
    """A finer sensor in 0.1 °C steps, every 5 minutes."""
    return {i / 12: round(start + per_hour * i / 12, 1) for i in range(int(hours * 12) + 1)}


def test_a_room_that_falls_while_heating_is_flagged():
    """2026-11-19: 아기방's Nest called for heat from 00:21 for six hours while its actuator didn't turn;
    the Awair fell 28.6 → 27.5 and no alert came: the model expected only +0.06 °C at 2 °C outside."""
    found = first_flag({0: 25.8, 4.0: 25.2}, falling(28.6, -0.23))
    assert found is not None
    hours, reason = found
    assert DEAD + 1.5 <= hours <= DEAD + 1.5 + 0.1  # 03:14, not 06:37
    assert reason == "2시간 55분째 난방 중인데 오히려 0.7° 내려갔어요. 구동기나 단지 난방 공급을 확인해 주세요."
    assert not any(c in reason for c in "_*[`")  # plain for Telegram's Markdown


def test_without_a_finer_sensor_a_whole_nest_step_counts():
    assert first_flag({0: 25.8, 0.5: 25.7, 1.0: 25.8, 2.0: 25.7}) is None  # flickering around 25.75
    found = first_flag({0: 25.8, 1.0: 25.7, 2.0: 25.3})
    assert found is not None and found[1].startswith("2시간 55분째 난방 중인데 오히려 0.5° 내려갔어요")


def test_heating_rooms_stay_silent():
    assert first_flag({0: 25.3, 2.0: 25.8}, falling(27.6, 0.25)) is None  # an ordinary run
    # 2026-11-19 16:05 at 4 °C: slow, but up (+0.25 by 1.5 h)
    assert first_flag({0: 24.8, 4.5: 25.8}, falling(27.3, 0.1)) is None
    # the Awair wandering by its 0.1 °C step isn't a fall
    assert first_flag({0: 25.8}, {i / 12: 28.6 - 0.1 * (i % 2) for i in range(97)}) is None


def test_a_call_is_judged_only_after_the_dead_time_and_some():
    found = first_flag({0: 25.8}, falling(28.6, -1.0))  # falling fast from the start
    assert found is not None and found[0] == (int((DEAD + 1.5) * 12) + 1) / 12


def test_warming_again_on_either_sensor():
    run = follow(None, True, 25.2, START, 27.5)
    assert not warmed(run, 25.3, 27.7)
    assert warmed(run, 25.3, 27.8)  # the Awair up 0.3 from the low
    assert warmed(run, 25.7, None)  # or the Nest


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
