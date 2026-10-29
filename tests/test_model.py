from datetime import datetime, timedelta, UTC
import json
import math
from pathlib import Path

from custom_components.ondol.model import Params, extract, fit, merge

UTC = UTC
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "nest_room_week.json").read_text())


def test_lead_hold_and_ceiling():
    p = Params(dead_time=1.5, rise=0.2, loss=0.004)
    lead = p.lead(25.0, 26.0, 8.0)  # about 1 °C at a net 0.13 °C/h, after the dead time
    assert 1.5 + 7 < lead < 1.5 + 9
    assert p.lead(25.0, 24.0, 8.0) == 1.5  # warm enough already
    assert math.isinf(p.lead(25.0, 60.0, 8.0))  # above what the slab can hold
    hours = p.hold(26.0, 25.5, 8.0)
    assert abs(p.idle(26.0, 8.0, hours) - 25.5) < 1e-6
    assert p.hold(25.0, 25.5, 8.0) == 0.0


def test_colder_outside_rises_faster():
    p = Params(rise=0.2, rise_slope=-0.005)
    assert p.rise_at(0.0) > p.rise_at(10.0) == 0.2 > p.rise_at(15.0)


def test_learns_plausible_numbers_from_a_real_week():
    climate = [(datetime.fromisoformat(t), heating, temp) for t, heating, temp in FIXTURE["climate"]]
    outdoor = [(datetime.fromisoformat(t), value) for t, value in FIXTURE["outdoor"]]
    samples = extract(climate, outdoor, Params())
    params = fit(samples, climate[-1][0])
    # its longest run went 25.75 -> 26.25 °C in 3.8 h at 8 °C outside
    assert any(abs(s.get("rise", 0) - 0.2) < 0.03 for s in samples)
    assert 0.8 <= params.dead_time <= 1.75
    assert 0.15 <= params.rise <= 0.25
    assert 0.002 <= params.loss <= 0.007
    assert 0.0 <= params.coast <= 0.3


def simulate(dead: float, rise: float, loss: float, outdoor: float, days: int = 10):
    """Daily 18:00-02:00 runs, sampled like a Nest: a report whenever the 0.5 °C display changes."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    temp, slab, shown, reported = 23.0, 0.0, None, None
    rows = []
    for minute in range(days * 1440):
        t = start + timedelta(minutes=minute)
        on = (t.hour >= 18 or t.hour < 2) and minute >= 18 * 60
        since_on = (t.hour - 18) % 24 * 60 + t.minute
        # slab heat reaching the air: nothing for the dead time, then full; after closing it fades
        slab = 1.0 if on and since_on >= dead * 60 else (0.0 if on else slab * math.exp(-1 / 60))
        temp += (rise * slab - loss * (temp - outdoor)) / 60
        display = math.floor(temp * 2 + 0.5) / 2
        if display != shown:
            shown, reported = display, round(temp, 1)
            rows.append((t, on, reported))
        elif rows[-1][1] != on:  # a valve change carries the last reported temperature
            rows.append((t, on, reported))
    return rows, [(start, outdoor)]


def test_recovers_a_simulated_slab():
    rows, outdoor = simulate(dead=1.2, rise=0.3, loss=0.006, outdoor=0.0)
    params = fit(extract(rows, outdoor, Params(loss=0.006)), rows[-1][0])
    assert abs(params.rise - 0.3) < 0.05
    assert 0.003 < params.loss < 0.01
    assert 0.7 < params.dead_time < 1.75
    assert params.coast > 0.1


def test_recent_runs_outweigh_old_ones_and_rise_follows_outdoor():
    now = datetime(2026, 12, 31, tzinfo=UTC)

    def sample(days_ago: float, outdoor: float, rise: float) -> dict:
        return {"t": (now - timedelta(days=days_ago)).isoformat(timespec="minutes"), "outdoor": outdoor, "rise": rise}

    slow = [sample(80 + i, 10.0, 0.1) for i in range(6)] + [sample(i, 10.0, 0.3) for i in range(6)]
    fast = [sample(i, 10.0, 0.1) for i in range(6)] + [sample(80 + i, 10.0, 0.3) for i in range(6)]
    assert fit(slow, now).rise > 0.24 > 0.16 > fit(fast, now).rise

    # one run warmed by people at bedtime doesn't drag the level
    assert fit([sample(1, 10.0, 0.2), sample(2, 10.0, 0.21), sample(3, 10.0, 0.5)], now).rise < 0.21

    spread = [sample(i, 0.0, 0.3) for i in range(5)] + [sample(i, 15.0, 0.2) for i in range(5)]
    params = fit(spread, now)
    assert params.rise_slope < 0
    assert params.rise_at(0.0) > params.rise_at(15.0)


def test_merge_replaces_reread_runs_and_forgets_old_ones():
    now = datetime(2026, 12, 31, tzinfo=UTC)
    kept = [{"t": "2026-09-01T10:00+00:00", "rise": 0.1}, {"t": "2026-12-30T10:00+00:00", "rise": 0.1}]
    merged = merge(kept, [{"t": "2026-12-30T10:00+00:00", "rise": 0.2}], now)
    assert merged == [{"t": "2026-12-30T10:00+00:00", "rise": 0.2}]


def test_a_too_cold_early_stop_night_corrects_the_cooling():
    """Like 2026-10-29→30: the Nest shows one 0.5 °C step all night, a finer sensor shows -0.12 °C/h."""
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    day = datetime(2026, 10, 29, tzinfo=kst)
    at = lambda h, m=0: day + timedelta(hours=h, minutes=m)  # noqa: E731
    rows = [
        (at(20, 0), False, 25.8),
        (at(23, 48), True, 25.8),
        (at(24, 32), False, 25.8),
        (at(25, 52), True, 25.8),
        (at(25, 53), False, 25.8),  # early stop
        (at(28, 44), False, 25.5),
        (at(30, 24), True, 25.5),  # someone raised it by hand
        (at(31, 30), True, 25.8),
    ]
    stop = at(25, 53)
    fine = [(stop + timedelta(minutes=i), round(28.5 - 0.12 * i / 60, 1)) for i in range(0, 271)]
    outdoor = [(at(20), 9.0)]
    found = extract(rows, outdoor, Params(), fine)
    night = [s for s in found if s.get("night")]
    assert len(night) == 1 and abs(night[0]["loss"] - 0.12 / (25.8 - 9.0)) < 0.001

    days = [{"t": (day - timedelta(days=i)).isoformat(timespec="minutes"), "outdoor": 15.0, "loss": 0.002} for i in range(1, 6)]
    learned = fit(days + found, at(31))
    assert 0.0065 < learned.loss < 0.0078  # the night decides, not the five warm days
    assert fit(days, at(31)).loss < 0.004
