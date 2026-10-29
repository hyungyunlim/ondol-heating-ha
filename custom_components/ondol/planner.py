"""Per-room decision: what the room should be at now, and which setpoint the Nest gets for it.

Order of precedence: away > manual hold > daycare > occupancy > schedule. On top of that a warmer block
ahead starts early enough for the slab to get there (preheat), and before a cooler block the
valve closes as soon as the slab's stored heat will carry the room to the end (early stop).
While away nothing preheats, except a room that follows daycare while the child is there: it
still warms up for today's next comfort block, since pickup brings someone home.
While a long heat-up is running the Nest aims 0.5 °C low, so the coast lands on the target.

A room with an occupancy sensor treats an eco block as comfort while it is in use (and for an
hour after), and drops a comfort block to eco once nobody has used it for two hours. The slab
answers hours late either way, so this is for rooms used off schedule, not a quick fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from .const import MAX_SETPOINT, MIN_SETPOINT, PRESET_LABELS
from .model import Params
from .schedule import Block, Entries, block_at, upcoming

HORIZON_HOURS = 18  # a warmer block further ahead than this can't need heat yet
# ponytail: stop at most this long before a block ends (the slab held 3-6 h in Oct 2026); longer
# extrapolations lean too hard on the learned loss. Make it per-room if winter data says so.
EARLY_STOP_HOURS = 6
STEP = 0.5  # Nest setpoints move in 0.5 °C steps
IN_USE_HOURS = 1.0  # a room still counts as in use this long after people leave
EMPTY_HOURS = 2.0  # a comfort block nobody has used for this long drops to eco


@dataclass(frozen=True)
class Room:
    presets: dict[str, float]
    entries: Entries
    follows_daycare: bool = False


@dataclass(frozen=True)
class Now:
    at: datetime  # local time
    temp: float | None
    outdoor: float
    heating: bool = False
    away: bool = False
    daycare: bool = False
    used_ago: float | None = None  # hours since the room was last in use (0 = now); None: no sensor


@dataclass(frozen=True)
class Hold:
    target: float
    until: datetime
    preset: str | None = None  # picked as a preset on our climate entity


@dataclass
class Memory:
    hold: Hold | None = None
    preheat_for: datetime | None = None  # latched so a preheat doesn't flap as the room warms
    early_for: datetime | None = None
    phase: str = "normal"  # normal | heatup | coast
    phase_target: float | None = None
    phase_since: datetime | None = None


@dataclass(frozen=True)
class Plan:
    target: float  # what the room should be at
    setpoint: float  # what the Nest is told
    preset: str | None  # None during a manual hold
    reason: str  # schedule | daycare | occupied | empty | away | hold | preheat | early_stop
    block: Block | None = None  # the block a preheat or early stop is for
    lead: float | None = None  # hours of heating the preheat needs
    hold_until: datetime | None = None
    next_at: datetime | None = None
    next_text: str | None = None

    @property
    def text(self) -> str:
        label = PRESET_LABELS.get(self.preset or "", "")
        if self.reason == "preheat" and self.block:
            lead = f", {self.lead:.1f}시간 가동" if self.lead is not None and math.isfinite(self.lead) else ""
            return f"예열: {self.block.start:%H:%M} {label} {self.target:.1f}°C{lead}"
        if self.reason == "early_stop" and self.block:
            return f"조기 종료: {self.block.start:%H:%M} {label} 전까지 여열로 유지"
        if self.reason == "hold" and self.hold_until:
            return f"수동 {self.target:.1f}°C ({self.hold_until:%H:%M}까지)"
        if self.reason == "away":
            return f"외출 {self.target:.1f}°C"
        if self.reason == "daycare":
            return f"등원 중 {label} {self.target:.1f}°C"
        if self.reason == "occupied":
            return f"재실 중 {label} {self.target:.1f}°C"
        if self.reason == "empty":
            return f"비어 있음: {label} {self.target:.1f}°C"
        return f"일정 {label} {self.target:.1f}°C"


def _hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


def _short_without_heat(params: Params, now: Now, block: Block, goal: float, tolerance: float) -> bool:
    """Whether the room would miss the block's goal if left alone; only then is a preheat due."""
    return params.idle(now.temp, now.outdoor, _hours(now.at, block.start)) < goal - tolerance  # type: ignore[arg-type]


def setpoint_for(target: float) -> float:
    return min(max(math.floor(target / STEP + 0.5) * STEP, MIN_SETPOINT), MAX_SETPOINT)


def plan(room: Room, params: Params, now: Now, mem: Memory, tolerance: float) -> Plan:
    t, temp = now.at, now.temp
    if mem.hold and t >= mem.hold.until:
        mem.hold = None
    if mem.preheat_for and t >= mem.preheat_for:
        mem.preheat_for = None
    if mem.early_for and t >= mem.early_for:
        mem.early_for = None

    ahead = upcoming(room.entries, t, HORIZON_HOURS)
    for_block: Block | None = None
    lead: float | None = None
    found: list[tuple[datetime, str]] = []  # changes coming up, for the next-action text

    if now.away:
        mem.hold = mem.early_for = None
        target, preset, reason = room.presets["away"], "away", "away"
        # pickup is a predictable arrival: a child's room still warms up for today's comfort block
        pickup = next((b for b in ahead if b.preset == "comfort" and b.start.date() == t.date()), None)
        preheatable = [pickup] if pickup and room.follows_daycare and now.daycare else []
    else:
        block = block_at(room.entries, t)
        preset, reason = block.preset, "schedule"
        used = now.used_ago
        if room.follows_daycare and now.daycare and preset == "comfort":
            preset, reason = "eco", "daycare"
        elif used is not None and preset == "eco" and used <= IN_USE_HOURS:
            preset, reason = "comfort", "occupied"
            if used > 0:
                ends = t + timedelta(hours=IN_USE_HOURS - used)
                found.append((ends, f"{ends:%H:%M} 절약 {room.presets['eco']:.1f}°C"))
        elif used is not None and preset == "comfort":
            empty_at = max(t - timedelta(hours=used), block.start) + timedelta(hours=EMPTY_HOURS)
            if empty_at <= t:
                preset, reason = "eco", "empty"
            elif used > 0:
                found.append((empty_at, f"{empty_at:%H:%M} 비어 있으면 절약 {room.presets['eco']:.1f}°C"))
        target = room.presets[preset]
        if mem.hold:
            target, preset, reason = mem.hold.target, mem.hold.preset, "hold"
        preheatable = ahead

    if temp is not None:
        for block in preheatable:
            goal = room.presets[block.preset]
            if goal <= target:
                continue
            need = params.lead(temp, goal - tolerance, now.outdoor)
            if mem.preheat_for != block.start:
                if not _short_without_heat(params, now, block, goal, tolerance) or _hours(t, block.start) > need:
                    continue
                mem.preheat_for = block.start
            mem.hold = None  # the next block's preheat ends a manual hold, but never lowers it
            target, preset, reason, for_block, lead = goal, block.preset, "preheat", block, need

        if reason in ("schedule", "daycare") and ahead and room.presets[ahead[0].preset] < target:
            nxt = ahead[0]
            peak = temp + (params.coast if now.heating else 0.0)
            hours_left = _hours(t, nxt.start)
            at_end = params.idle(peak, now.outdoor, hours_left)
            if mem.early_for == nxt.start and temp < target - tolerance:
                mem.early_for = None  # colder than the model said: heat again
            # only worth it if the Nest would otherwise heat before the block ends
            if mem.early_for == nxt.start or (
                hours_left <= EARLY_STOP_HOURS
                and temp >= target - tolerance
                and target - tolerance <= at_end
                and (now.heating or at_end < target)
            ):
                mem.early_for = nxt.start
                target, preset, reason, for_block = room.presets[nxt.preset], nxt.preset, "early_stop", nxt

    # aim one step low during a long heat-up and its coast, then hold the target itself
    step = STEP if params.coast >= STEP / 2 else 0.0
    if mem.phase_target != target:
        mem.phase, mem.phase_target, mem.phase_since = "normal", target, t
    if temp is None or not step or reason == "hold":
        mem.phase = "normal"
    elif mem.phase == "normal" and temp < target - step - 0.25:
        mem.phase, mem.phase_since = "heatup", t
    elif mem.phase == "heatup" and temp >= target - step:
        mem.phase, mem.phase_since = "coast", t
    elif mem.phase == "coast" and (
        temp >= target or _hours(mem.phase_since or t, t) >= params.coast_time + 0.5
    ):
        mem.phase = "normal"
    setpoint = setpoint_for(target - step if mem.phase in ("heatup", "coast") else target)

    next_at, next_text = _next(room, params, now, mem, tolerance, target, reason, ahead, preheatable, found)
    return Plan(
        target=target,
        setpoint=setpoint,
        preset=preset,
        reason=reason,
        block=for_block,
        lead=lead,
        hold_until=mem.hold.until if mem.hold else None,
        next_at=next_at,
        next_text=next_text,
    )


def _next(
    room: Room,
    params: Params,
    now: Now,
    mem: Memory,
    tolerance: float,
    target: float,
    reason: str,
    ahead: list[Block],
    preheatable: list[Block],
    found: list[tuple[datetime, str]],
) -> tuple[datetime | None, str | None]:
    """When the plan changes next, assuming the room stays where it is."""
    t, temp = now.at, now.temp
    if mem.hold:
        found.append((mem.hold.until, f"{mem.hold.until:%H:%M} 수동 유지 끝"))
    if ahead:
        nxt = ahead[0]
        found.append((nxt.start, f"{nxt.start:%H:%M} {PRESET_LABELS[nxt.preset]} {room.presets[nxt.preset]:.1f}°C"))
    if temp is not None:
        for block in preheatable:
            goal = room.presets[block.preset]
            # announce only the preheats plan() will run: not for a room warm enough anyway
            if goal > target and mem.preheat_for != block.start and _short_without_heat(params, now, block, goal, tolerance):
                start = block.start - timedelta(hours=min(params.lead(temp, goal - tolerance, now.outdoor), 48))
                if t < start < block.start:
                    label = PRESET_LABELS[block.preset]
                    found.append((start, f"{start:%H:%M} 예열 시작 → {block.start:%H:%M} {label} {goal:.1f}°C"))
        if reason in ("schedule", "daycare") and ahead and room.presets[ahead[0].preset] < target:
            nxt = ahead[0]
            peak = temp + (params.coast if now.heating else 0.0)
            stay = params.hold(peak, target - tolerance, now.outdoor)
            needed = params.hold(peak, target, now.outdoor)  # until then the room needs no heat at all
            if math.isfinite(stay) and temp >= target - tolerance and needed < _hours(t, nxt.start):
                stop = nxt.start - timedelta(hours=min(stay, EARLY_STOP_HOURS))
                if t < stop < nxt.start:
                    label = PRESET_LABELS[nxt.preset]
                    found.append((stop, f"{stop:%H:%M} 조기 종료 → {nxt.start:%H:%M} {label}"))
    if not found:
        return None, None
    return min(found, key=lambda item: item[0])
