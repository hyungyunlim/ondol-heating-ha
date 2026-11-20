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
    away = plan(ROOM, PARAMS, now("12:00", 25.0, away=True), Memory(), TOL)
    assert (away.reason, away.preset, away.setpoint) == ("away", "away", 20.0)
    assert away.next_text == "귀가하면 편안 25.0°C"  # nothing happens while away: what coming home brings
    assert plan(ROOM, PARAMS, now("23:00", 25.0, away=True), Memory(), TOL).next_text == "귀가하면 수면 26.0°C"

    # someone warming the room on the way home: their setting outranks away until the next block
    mem = Memory(hold=Hold(26.5, at("22:30"), since=at("21:40")))
    held = plan(ROOM, PARAMS, now("21:45", 24.0, away=True), mem, TOL)
    assert (held.reason, held.setpoint, held.next_text) == ("hold", 26.5, "22:30 수동 유지 끝")
    assert plan(ROOM, PARAMS, now("22:31", 25.0, away=True), mem, TOL).reason == "away"  # then away again

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


def test_no_preheat_is_announced_from_aired_air():
    """2026-11-02: 안방 aired 10:03-10:17 and its Nest fell 25.7 → 23.5 °C; the card then said a preheat
    for 23:00 starts at 10:47, in an empty room that needed none before the airing."""
    anbang = Room(presets={"comfort": 23.0, "sleep": 25.5, "eco": 22.0, "away": 21.0}, entries=parse("매일 07:00 편안, 23:00 수면"))
    params = Params(dead_time=1.487, rise=0.1762, loss=0.0025, coast=0.1065)  # learned that morning
    t = datetime(2026, 11, 2, 10, 18, 8, tzinfo=KST)
    unaware = plan(anbang, params, Now(at=t, temp=23.5, outdoor=11.4), Memory(), TOL)
    assert unaware.next_text == "10:47 예열 시작 → 23:00 수면 25.5°C"
    for airing in ({"airing_since": t - timedelta(minutes=15)}, {"aired_recently": True}):
        p = plan(anbang, params, Now(at=t, temp=23.5, outdoor=11.4, **airing), Memory(), TOL)
        assert p.next_text == "23:00 수면 25.5°C"  # as plan() won't start one now either


AGIBANG = Room(
    presets={"comfort": 25.0, "sleep": 26.0, "eco": 22.0, "away": 21.0},
    entries=parse("월-금 08:30 편안, 17:00 편안\n토,일 09:00 편안\n매일 22:30 수면"),
    follows_daycare=True,
)
NOV = Params(dead_time=1.36, rise=0.28, loss=0.0058, coast=0.16, coast_time=0.86)  # 아기방, mid-November


def nov(hm: str, day: int, **kwargs) -> Now:
    hour, minute = map(int, hm.split(":"))
    return Now(at=datetime(2026, 11, day, hour, minute, tzinfo=KST), outdoor=13.0, **kwargs)


def test_an_early_stop_is_judged_on_a_finer_sensors_fall():
    """2026-11-17 02:53: 아기방's Nest still said 26.3 from 00:07 while its Awair had fallen 0.5; the
    stop latched on 26.3 and was called off at 04:19, cooling faster than predicted."""
    assert plan(AGIBANG, NOV, nov("02:53", 17, temp=26.3), Memory(), 0.3).reason == "early_stop"
    fresh = plan(AGIBANG, NOV, nov("02:53", 17, temp=26.3, fresh=25.8), Memory(), 0.3)
    assert (fresh.reason, fresh.setpoint) == ("schedule", 26.0)
    # a rise the Nest hasn't reported isn't taken: it could only stop early sooner
    assert plan(AGIBANG, NOV, nov("02:53", 17, temp=25.8, fresh=26.3), Memory(), 0.3).reason == "schedule"


def test_no_early_stop_during_or_right_after_a_heat_pulse():
    """2026-11-13 03:49: 아기방 latched an hour after a 32-min pulse, at its peak; the room then fell
    0.3 °C/h for two hours and the stop was called off at 05:38."""
    pulse = (datetime(2026, 11, 13, 2, 9, tzinfo=KST), datetime(2026, 11, 13, 2, 41, tzinfo=KST))
    assert plan(AGIBANG, NOV, nov("03:50", 13, temp=26.2), Memory(), 0.3).reason == "early_stop"  # not knowing
    assert plan(AGIBANG, NOV, nov("03:50", 13, temp=26.2, run=pulse), Memory(), 0.3).reason == "schedule"
    running = (datetime(2026, 11, 13, 3, 40, tzinfo=KST), None)
    assert plan(AGIBANG, NOV, nov("03:50", 13, temp=26.2, heating=True, run=running), Memory(), 0.3).reason == "schedule"
    # settled, an hour past the coast's peak
    assert plan(AGIBANG, NOV, nov("04:45", 13, temp=26.2, run=pulse), Memory(), 0.3).reason == "early_stop"
    # a long run has charged the slab: cutting it short is what the early stop is for (안방 at 01:00)
    long_run = (datetime(2026, 11, 13, 2, 0, tzinfo=KST), None)
    assert plan(AGIBANG, NOV, nov("03:50", 13, temp=26.0, heating=True, run=long_run), Memory(), 0.3).reason == "early_stop"


def test_an_early_stop_isnt_called_off_in_its_last_dead_time():
    """2026-11-11 08:18: 아기방 called its stop off 12 minutes before 08:30 and heated, for nothing."""
    mem = Memory()
    assert plan(AGIBANG, NOV, nov("04:50", 11, temp=26.1), mem, 0.3).reason == "early_stop"
    late = plan(AGIBANG, NOV, nov("08:18", 11, temp=25.6), mem, 0.3)  # under 26 - 0.3, but heat lands after 08:30
    assert (late.reason, late.setpoint) == ("early_stop", 25.0)
    earlier = Memory()
    plan(AGIBANG, NOV, nov("04:50", 11, temp=26.1), earlier, 0.3)
    assert plan(AGIBANG, NOV, nov("06:00", 11, temp=25.6), earlier, 0.3).reason == "schedule"  # still in time


def test_a_planned_return_preheats_while_away():
    """Back at 23:00 after a trip: the room counts it as a block with its own preset then (sleep 26 from
    22:30), however far ahead, and starts with its usual lead so it is at 26 - tolerance by 23:00."""
    back = at("23:00")
    cold = Params(dead_time=1.5, rise=0.2, loss=0.004)
    away = dict(outdoor=8.0, away=True, returning=back)
    lead = cold.lead(23.0, 25.5, 8.0)
    assert 15 < lead < 17  # below 10 °C outside: the cold-weather lead
    start = back - timedelta(hours=lead)
    waiting = plan(ROOM, cold, Now(at=start - timedelta(minutes=10), temp=23.0, **away), Memory(), TOL)
    assert (waiting.reason, waiting.target) == ("away", 20.0)
    assert waiting.next_text == f"{start:%H:%M} 예열 시작 → 23:00 수면 26.0°C"
    assert waiting.return_start == start
    going = plan(ROOM, cold, Now(at=start + timedelta(minutes=10), temp=23.0, **away), Memory(), TOL)
    assert (going.reason, going.target, going.text) == ("preheat", 26.0, f"예열: 23:00 수면 26.0°C, {lead:.1f}시간 가동")
    # a room at 21 °C in a frost needs more than a day by the model: it starts 24 h ahead, not sooner
    frost = Now(at=back - timedelta(hours=23), temp=21.0, outdoor=0.0, away=True, returning=back)
    assert plan(ROOM, cold, frost, Memory(), TOL).reason == "preheat"
    early = Now(at=back - timedelta(hours=25), temp=21.0, outdoor=0.0, away=True, returning=back)
    assert plan(ROOM, cold, early, Memory(), TOL).reason == "away"


def test_a_planned_return_only_counts_while_away():
    back = at("23:00")
    home = plan(ROOM, PARAMS, Now(at=at("12:00"), temp=25.9, outdoor=8.0, returning=back), Memory(), TOL)
    assert (home.reason, home.return_start) == ("schedule", None)
    # late, still away: keep the room ready (the coordinator gives up three hours on)
    late = plan(ROOM, PARAMS, Now(at=at("23:40"), temp=26.0, outdoor=8.0, away=True, returning=back), Memory(), TOL)
    assert (late.reason, late.target, late.text) == ("preheat", 26.0, "예열: 23:00 수면 26.0°C")
    # days off, the start isn't within a day: the next action says what coming home now brings
    far = plan(ROOM, PARAMS, Now(at=at("12:00"), temp=24.0, outdoor=8.0, away=True, returning=back + timedelta(days=3)), Memory(), TOL)
    assert far.reason == "away" and far.next_text == "귀가하면 편안 25.0°C"
