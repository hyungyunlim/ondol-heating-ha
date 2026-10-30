from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.ondol.model import Params
from custom_components.ondol.planner import Hold, Memory, Now, Room, plan, setpoint_for
from custom_components.ondol.schedule import parse

KST = ZoneInfo("Asia/Seoul")
ROOM = Room(
    presets={"comfort": 25.0, "sleep": 26.0, "eco": 22.0, "away": 20.0},
    entries=parse("매일 07:00 편안, 22:30 수면"),
    follows_daycare=True,
)
PARAMS = Params(dead_time=1.5, rise=0.2, loss=0.004, coast=0.3, coast_time=1.0)
TOL = 0.5


def at(hm: str, day: int = 0) -> datetime:
    hour, minute = map(int, hm.split(":"))
    return datetime(2026, 10, 26, tzinfo=KST) + timedelta(days=day, hours=hour, minutes=minute)


def now(hm: str, temp: float | None, day: int = 0, **kwargs) -> Now:
    return Now(at=at(hm, day), temp=temp, outdoor=8.0, **kwargs)


def test_follows_the_schedule():
    p = plan(ROOM, PARAMS, now("12:00", 25.2), Memory(), TOL)
    assert (p.reason, p.preset, p.target, p.setpoint) == ("schedule", "comfort", 25.0, 25.0)
    assert p.next_text and "예열 시작" in p.next_text  # the sleep block will need heat later


def test_preheat_starts_lead_hours_ahead_and_latches():
    lead = PARAMS.lead(25.0, 26.0 - TOL, 8.0)
    start = at("22:30") - timedelta(hours=lead)
    mem = Memory()
    before = plan(ROOM, PARAMS, Now(at=start - timedelta(minutes=10), temp=25.0, outdoor=8.0), mem, TOL)
    assert before.reason == "schedule"
    assert abs((before.next_at - start).total_seconds()) < 60

    during = plan(ROOM, PARAMS, Now(at=start + timedelta(minutes=5), temp=25.0, outdoor=8.0), mem, TOL)
    assert (during.reason, during.preset, during.target) == ("preheat", "sleep", 26.0)
    assert during.setpoint == 25.5  # a long heat-up aims one step low and lets the slab coast up
    assert "예열" in during.text

    # a little warmer than the model expected: still preheating, no flapping back to the schedule
    later = plan(ROOM, PARAMS, Now(at=start + timedelta(hours=2), temp=25.3, outdoor=8.0), mem, TOL)
    assert later.reason == "preheat"
    # far warmer, needing a fraction of the hours left: paused and planned again nearer the block
    jumped = plan(ROOM, PARAMS, Now(at=start + timedelta(hours=2), temp=25.6, outdoor=8.0), Memory(preheat_for=at("22:30")), TOL)
    assert jumped.reason == "schedule" and jumped.next_text and "예열 시작" in jumped.next_text
    # the block itself takes over at 22:30
    assert plan(ROOM, PARAMS, now("22:31", 25.8), mem, TOL).reason == "schedule"


def test_no_preheat_when_the_slab_keeps_the_room_warm():
    p = plan(ROOM, PARAMS, now("21:00", 25.9), Memory(), TOL)
    assert p.reason == "schedule" and p.target == 25.0


def test_early_stop_when_the_stored_heat_lasts_until_the_next_block():
    still_long = plan(ROOM, PARAMS, now("23:30", 26.0), Memory(), TOL)  # 7.5 h left
    assert still_long.reason == "schedule" and still_long.setpoint == 26.0
    assert still_long.next_text and "조기 종료" in still_long.next_text

    mem = Memory()
    p = plan(ROOM, PARAMS, now("01:00", 26.0, day=1), mem, TOL)  # 6 h left
    assert (p.reason, p.target, p.setpoint) == ("early_stop", 25.0, 25.0)
    assert mem.early_predicted == 25.57  # idle(26.0 °C, 8 °C outside, 6 h)
    # latched while the room cools as predicted
    assert plan(ROOM, PARAMS, now("04:00", 25.8, day=1), mem, TOL).reason == "early_stop"
    # cooling faster than predicted: heat again now, the slab answers late, and no second stop
    assert plan(ROOM, PARAMS, now("05:00", 25.55, day=1), mem, TOL).reason == "schedule"
    assert plan(ROOM, PARAMS, now("05:30", 25.9, day=1, heating=True), mem, TOL).reason == "schedule"
    # a room that never got warm enough is not stopped early
    assert plan(ROOM, PARAMS, now("01:00", 25.2, day=1), Memory(), TOL).reason == "schedule"


def test_early_stop_only_in_the_last_hours_of_a_block():
    office = Room(presets=ROOM.presets, entries=parse("매일 09:00 편안, 18:00 절약"))
    slow = Params(dead_time=1.5, rise=0.2, loss=0.002, coast=0.3)  # would hold all day
    assert plan(office, slow, now("09:00", 25.2), Memory(), TOL).reason == "schedule"
    p = plan(office, slow, now("09:00", 25.2), Memory(), TOL)
    assert p.next_text and p.next_text.startswith("12:00 조기 종료")
    assert plan(office, slow, now("12:00", 25.0), Memory(), TOL).reason == "early_stop"


def test_no_early_stop_when_the_room_needs_no_heat_anyway():
    p = plan(ROOM, PARAMS, now("01:00", 27.5, day=1), Memory(), TOL)  # far above sleep 26 until 07:00
    assert (p.reason, p.target) == ("schedule", 26.0)
    assert "조기 종료" not in (p.next_text or "")


def test_heatup_aims_low_then_holds_the_target():
    mem = Memory()
    params = Params(dead_time=1.5, rise=0.2, loss=0.004, coast=0.3, coast_time=1.0)
    assert plan(ROOM, params, now("07:00", 24.0), mem, TOL).setpoint == 24.5  # heat-up
    assert plan(ROOM, params, now("10:00", 24.5), mem, TOL).setpoint == 24.5  # valve shut, coasting
    assert plan(ROOM, params, now("11:00", 24.8), mem, TOL).setpoint == 24.5
    assert plan(ROOM, params, now("11:31", 24.8), mem, TOL).setpoint == 25.0  # coast over: hold target
    # no coast learned, no offset
    assert plan(ROOM, Params(coast=0.1), now("07:00", 24.0), Memory(), TOL).setpoint == 25.0


def test_away_and_daycare():
    mem = Memory(hold=Hold(27.0, at("22:30")))
    away = plan(ROOM, PARAMS, now("12:00", 25.0, away=True), mem, TOL)
    assert (away.reason, away.preset, away.setpoint) == ("away", "away", 20.0)
    assert mem.hold is None  # leaving ends a manual setting

    daycare = plan(ROOM, PARAMS, now("12:00", 25.0, daycare=True), Memory(), TOL)
    assert (daycare.reason, daycare.preset, daycare.target) == ("daycare", "eco", 22.0)
    other = Room(presets=ROOM.presets, entries=ROOM.entries)  # a room that doesn't follow daycare
    assert plan(other, PARAMS, now("12:00", 25.0, daycare=True), Memory(), TOL).reason == "schedule"


def test_manual_hold_until_the_next_block():
    mem = Memory(hold=Hold(27.0, at("22:30")))
    p = plan(ROOM, PARAMS, now("21:00", 25.0), mem, TOL)
    assert (p.reason, p.preset, p.target, p.setpoint) == ("hold", None, 27.0, 27.0)
    assert plan(ROOM, PARAMS, now("22:30", 26.0), mem, TOL).reason == "schedule"

    # a lower manual setting gives way to the next block's preheat, a higher one doesn't
    low = Memory(hold=Hold(23.0, at("22:30")))
    assert plan(ROOM, PARAMS, now("18:00", 24.0), low, TOL).reason == "preheat"
    assert low.hold is None


def test_setpoints_stay_in_range():
    assert setpoint_for(15.0) == 18.0
    assert setpoint_for(25.24) == 25.0 and setpoint_for(25.25) == 25.5
    assert setpoint_for(35.0) == 30.0
    p = plan(ROOM, PARAMS, now("12:00", None), Memory(), TOL)  # no room temperature: plain schedule
    assert (p.reason, p.setpoint) == ("schedule", 25.0)


PICKUP = Room(
    presets=ROOM.presets,
    entries=parse("매일 07:30 편안, 22:30 수면\n월-금 17:00 편안"),
    follows_daycare=True,
)


def away(start: datetime, minutes: int, daycare: bool) -> Now:
    return Now(at=start + timedelta(minutes=minutes), temp=24.0, outdoor=8.0, away=True, daycare=daycare)


def test_away_with_the_child_at_daycare_still_preheats_for_pickup():
    start = at("17:00") - timedelta(hours=PARAMS.lead(24.0, 25.0 - TOL, 8.0))  # Monday
    mem = Memory()
    before = plan(PICKUP, PARAMS, away(start, -10, daycare=True), mem, TOL)
    assert (before.reason, before.target) == ("away", 20.0)
    assert before.next_text == f"{start:%H:%M} 예열 시작 → 17:00 편안 25.0°C"
    during = plan(PICKUP, PARAMS, away(start, 5, daycare=True), mem, TOL)
    assert (during.reason, during.preset, during.target) == ("preheat", "comfort", 25.0)


def test_away_without_the_child_at_daycare_stays_away():
    start = at("17:00") - timedelta(hours=PARAMS.lead(24.0, 25.0 - TOL, 8.0))
    p = plan(PICKUP, PARAMS, away(start, 5, daycare=False), Memory(), TOL)
    assert (p.reason, p.target) == ("away", 20.0)
    assert "예열" not in (p.next_text or "")
    # a room that doesn't follow daycare stays away even while the child is there
    other = Room(presets=PICKUP.presets, entries=PICKUP.entries)
    assert plan(other, PARAMS, away(start, 5, daycare=True), Memory(), TOL).reason == "away"


OFFICE = Room(presets=ROOM.presets, entries=parse("매일 09:00 편안, 23:00 절약"))


def at_office(hm: str, used_ago: float | None, temp: float = 25.0) -> Now:
    return Now(at=at(hm), temp=temp, outdoor=8.0, used_ago=used_ago)


def test_an_eco_block_in_use_counts_as_comfort_for_an_hour():
    p = plan(OFFICE, PARAMS, at_office("07:30", 0.0), Memory(), TOL)
    assert (p.reason, p.preset, p.target) == ("occupied", "comfort", 25.0)
    p = plan(OFFICE, PARAMS, at_office("07:30", 0.5), Memory(), TOL)  # left at 07:00
    assert p.reason == "occupied" and p.next_text == "08:00 절약 22.0°C"
    assert plan(OFFICE, PARAMS, at_office("07:30", 1.5), Memory(), TOL).reason == "schedule"


def test_a_comfort_block_nobody_uses_drops_to_eco_after_two_hours():
    waiting = plan(OFFICE, PARAMS, at_office("10:00", 12.0), Memory(), TOL)  # nobody since last night
    assert waiting.reason == "schedule" and waiting.next_text == "11:00 비어 있으면 절약 22.0°C"
    empty = plan(OFFICE, PARAMS, at_office("11:00", 13.0), Memory(), TOL)
    assert (empty.reason, empty.preset, empty.target) == ("empty", "eco", 22.0)
    assert plan(OFFICE, PARAMS, at_office("13:00", 0.0), Memory(), TOL).reason == "schedule"  # back
    assert plan(OFFICE, PARAMS, at_office("11:00", None), Memory(), TOL).reason == "schedule"  # no sensor


def test_next_action_announces_only_preheats_that_will_run():
    living = Room(presets={"comfort": 23.0, "sleep": 22.0, "eco": 22.0, "away": 21.0}, entries=parse("매일 06:30 편안, 23:00 수면"))
    warm = plan(living, PARAMS, now("23:05", 25.8), Memory(), TOL)  # far above 편안 23 until 06:30
    assert warm.next_text == "06:30 편안 23.0°C"
    real = plan(ROOM, PARAMS, now("19:00", 25.4), Memory(), TOL)  # short of 수면 26 by 22:30 unless heated
    assert real.next_text and real.next_text.endswith("예열 시작 → 22:30 수면 26.0°C")


def first_early_stop(params: Params, tolerance: float) -> datetime | None:
    """Walk a night in the sleep block (room at 26.0 °C, 9 °C outside) to the first early stop."""
    mem = Memory()
    for minutes in range(0, 7 * 60, 10):
        t = at("00:00", day=1) + timedelta(minutes=minutes)
        if plan(ROOM, params, Now(at=t, temp=26.0, outdoor=9.0), mem, tolerance).reason == "early_stop":
            return t
    return None


def test_a_corrected_cooling_rate_and_a_tighter_tolerance_stop_later():
    old = Params(dead_time=1.5, rise=0.2, loss=0.0035, coast=0.15)
    corrected = Params(dead_time=1.5, rise=0.2, loss=0.0071, coast=0.15)
    before, tighter, after = first_early_stop(old, 0.5), first_early_stop(old, 0.3), first_early_stop(corrected, 0.3)
    assert before == at("01:00", day=1)  # the 6 h cap: last night's 01:52 was this
    assert tighter > before
    assert after is not None and after - before >= timedelta(hours=3)
    assert after <= at("06:00", day=1)


def test_airing_during_an_early_stop_neither_heats_nor_calls_it_off():
    mem = Memory()
    assert plan(ROOM, PARAMS, now("01:00", 26.0, day=1), mem, TOL).reason == "early_stop"
    aired = plan(ROOM, PARAMS, now("03:00", 25.2, day=1, airing_since=at("02:50", day=1)), mem, TOL)
    assert (aired.reason, aired.setpoint, aired.text) == ("airing", 18.0, "환기 중 (02:50부터)")
    assert mem.early_for == at("07:00", day=1) and mem.early_cancelled is None
    settling = plan(ROOM, PARAMS, now("03:30", 25.4, day=1, aired_recently=True), mem, TOL)
    assert settling.reason == "early_stop"  # the cold air is the window's, not the model's error
    assert plan(ROOM, PARAMS, now("04:30", 25.8, day=1), mem, TOL).reason == "early_stop"



def test_a_preheat_latched_on_aired_air_is_dropped():
    """2026-10-30: airing cooled 아기방 to 24.4 °C at 10:50 and latched an 11.7 h preheat for 22:30."""
    params = Params(dead_time=1.5, rise=0.26, loss=0.0071, coast=0.15)
    mem = Memory(preheat_for=at("22:30"))  # as restored from storage after a restart
    aired = plan(ROOM, params, now("11:00", 24.4, airing_since=at("10:38")), mem, TOL)
    assert aired.reason == "airing" and mem.preheat_for is None
    recovered = plan(ROOM, params, now("12:00", 25.4), mem, TOL)  # windows shut, air back from the warm slab
    assert recovered.reason == "schedule" and mem.preheat_for is None
    lead = params.lead(25.4, 26.0 - TOL, 8.0)
    assert 2 < lead < 5  # a few hours from the real room temperature, not 11.7
    assert recovered.next_text == f"{at('22:30') - timedelta(hours=lead):%H:%M} 예열 시작 → 22:30 수면 26.0°C"
