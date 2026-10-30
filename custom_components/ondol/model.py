"""What an ondol slab does to a room, learned from the thermostat's own history.

A room with the valve open first keeps drifting for ``dead_time`` hours (the slab warms up),
then rises at ``rise_at(outdoor) - loss * (room - outdoor)`` °C/h. With the valve closed it
drifts towards the outdoor temperature at ``loss`` (1/h). After a long run closes, the slab
still lifts the room by ``coast`` °C, peaking ``coast_time`` hours later.

District heating raises its supply temperature as it gets colder outside, so the gross rise is
fitted as a line over the outdoor temperature (``rise`` at 10 °C plus ``rise_slope`` per °C).
Calibration runs daily: ``extract`` turns a history window into per-run samples, which are kept
for weeks, and ``fit`` weighs recent samples more so the model follows the season. The heat loss
comes from nights when there are any: early stops run at night, and a still night shows the
room's own loss, where a daytime stretch carries sun and people.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
import math

# (time, valve open, room °C or None) per recorder row, oldest first
Sample = tuple[datetime, bool, float | None]

BOUNDS = {
    "dead_time": (0.5, 3.0),
    "rise": (0.05, 0.6),
    "rise_slope": (-0.02, 0.0),  # colder outside -> hotter supply water -> faster rise
    "loss": (0.0, 0.02),
    "coast": (0.0, 1.0),
    "coast_time": (0.25, 3.0),
}
PRIOR_WEIGHT = 3.0  # a fresh estimate needs a few runs before it outweighs the defaults
# measured 2026-10: a heating room rose 0.13 °C/h net at ~17 °C above outdoor; the gross rise prior
# follows whatever loss is learned, so the net stays put when the loss changes
PRIOR_NET_RISE, PRIOR_GAP = 0.13, 17.0
HALF_LIFE_DAYS = 30.0
KEEP_DAYS = 90
REFERENCE_OUTDOOR = 10.0
NIGHT_FROM, NIGHT_UNTIL = 22, 7  # local hours: a quiet stretch centred here is a night

# airing: a window open, or the air falling faster than any slab lets it
AIRING_MAX = timedelta(hours=4)  # a window open longer than this is forgotten or stuck, not airing
AIRING_SETTLE = timedelta(hours=1)  # the air takes about this long to settle back afterwards
DROP_WINDOW = timedelta(minutes=15)
DROP_TRIGGER = 0.4  # °C within DROP_WINDOW: a night cools ~0.03
CO2_TRIGGER = 0.25  # share of CO₂ gone within DROP_WINDOW, next to a fine sensor's drop
CO2_ONLY_TRIGGER = 0.4  # on its own: a door opened at night dips ~25 % and comes back, airing ~65 %


@dataclass(frozen=True)
class Params:
    # defaults measured 2026-10 (outdoor 9-18 °C): 1.5-2 h dead time, +0.12-0.16 °C/h at
    # ~17 °C indoor-outdoor difference, +0.2-0.4 °C coast over 30-90 min, 0 to -0.07 °C/h idle
    dead_time: float = 1.75
    rise: float = 0.2
    rise_slope: float = 0.0
    loss: float = 0.004
    coast: float = 0.3
    coast_time: float = 1.0
    runs: int = 0  # heating runs behind the fit

    @classmethod
    def from_dict(cls, data: dict | None) -> Params:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def rise_at(self, outdoor: float) -> float:
        low, high = BOUNDS["rise"]
        return min(max(self.rise + self.rise_slope * (outdoor - REFERENCE_OUTDOOR), low), high)

    def net_rise(self, temp: float, outdoor: float) -> float:
        return self.rise_at(outdoor) - self.loss * (temp - outdoor)

    def idle(self, temp: float, outdoor: float, hours: float) -> float:
        """Room temperature after ``hours`` with the valve closed."""
        return outdoor + (temp - outdoor) * math.exp(-self.loss * max(hours, 0.0))

    def lead(self, temp: float, goal: float, outdoor: float) -> float:
        """Hours from opening the valve now until the room reaches ``goal`` (inf if it can't)."""
        start = self.idle(temp, outdoor, self.dead_time)
        if goal <= start:
            return self.dead_time
        rise = self.rise_at(outdoor)
        if self.loss <= 1e-6:
            return self.dead_time + (goal - start) / rise
        ceiling = outdoor + rise / self.loss  # where heat in = heat out
        if goal >= ceiling - 0.05:
            return math.inf
        return self.dead_time + math.log((ceiling - start) / (ceiling - goal)) / self.loss

    def hold(self, temp: float, floor: float, outdoor: float) -> float:
        """Hours the room stays at or above ``floor`` with the valve closed."""
        if temp < floor:
            return 0.0
        if self.loss <= 1e-6 or floor <= outdoor:
            return math.inf
        return math.log((temp - outdoor) / (floor - outdoor)) / self.loss


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600


def _value_at(series: Sequence[tuple[datetime, float]], when: datetime, default: float) -> float:
    value = default
    for t, v in series:
        if t > when:
            break
        value = v
    return value


def _mean_over(series: Sequence[tuple[datetime, float]], start: datetime, end: datetime, default: float) -> float:
    inside = [v for t, v in series if start <= t <= end]
    return sum(inside) / len(inside) if inside else _value_at(series, start, default)


def _near_boundary(temp: float) -> bool:
    return abs((temp * 2) % 1 - 0.5) <= 0.2 + 1e-9  # within 0.1 °C of an x.25 / x.75 display flip


def _level_at(
    temps: Sequence[tuple[datetime, float]], when: datetime, loss: float, outdoor: float, snap: bool
) -> float:
    """Best guess of the room at ``when`` from the last report: still drifting if it was falling."""
    past = [(t, temp) for t, temp in temps if t <= when]
    if not past:
        return temps[0][1]
    t, temp = past[-1]
    level = _snapped(temp, snap)
    if len(past) >= 2 and past[-2][1] > temp:  # crossed down, so it kept falling (within the band)
        return level - min(loss * (level - outdoor) * _hours(when - t), 0.45)
    return level


def _snapped(temp: float, snap: bool) -> float:
    """A Nest reports just past the display flip it crossed (25.3 for 25.25 going up)."""
    return math.floor(temp * 2) / 2 + 0.25 if snap and _near_boundary(temp) else temp


def _slope(series: Sequence[tuple[datetime, float]], start: datetime, end: datetime) -> float | None:
    """Least-squares °C/h of a frequently reporting sensor between ``start`` and ``end``."""
    points = [(_hours(t - start), v) for t, v in series if start <= t <= end]
    if len(points) < 6 or points[-1][0] - points[0][0] < 1.5:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / spread if spread else None


def _fall(series: Sequence[tuple[datetime, float]], times: list[datetime], when: datetime, share: bool) -> float:
    """How far a series has fallen from its highest point in the DROP_WINDOW before ``when``."""
    i, j = bisect_left(times, when - DROP_WINDOW), bisect_left(times, when, hi=len(times))
    window = [v for t, v in series[i : j + 1] if t <= when]
    if not window:
        return 0.0
    top, now = max(window), window[-1]
    return (top - now) / top if share and top else top - now


def fast_drops(
    fine: Sequence[tuple[datetime, float]] | None, co2: Sequence[tuple[datetime, float]] | None
) -> list[tuple[datetime, datetime]]:
    """Stretches where a room's air fell like an opened window: the fine sensor down DROP_TRIGGER °C
    and CO₂ down CO2_TRIGGER (CO2_ONLY_TRIGGER without a fine sensor) within 15 minutes. One lasts until
    nothing new has fallen for 10 minutes, at most an hour (a window sensor covers longer airings)."""
    if not fine and not co2:
        return []
    fine, co2 = fine or [], co2 or []
    fine_times, co2_times = [t for t, _ in fine], [t for t, _ in co2]
    found: list[tuple[datetime, datetime]] = []
    start = end = None
    lows: tuple[float, float] = (math.inf, math.inf)
    for t, _ in fine or co2:
        temp, air = _value_at(fine, t, math.inf), _value_at(co2, t, math.inf)
        if start is None:
            if all(
                fall >= limit
                for series, times, share, limit in (
                    (fine, fine_times, False, DROP_TRIGGER),
                    (co2, co2_times, True, CO2_TRIGGER if fine else CO2_ONLY_TRIGGER),
                )
                if series
                for fall in (_fall(series, times, t, share),)
            ):
                start, end, lows = t - DROP_WINDOW, t, (temp, air)
            continue
        if temp < lows[0] - 0.04 or air < lows[1] * 0.98:  # still finding new lows
            end, lows = t, (min(temp, lows[0]), min(air, lows[1]))
        if t - end > timedelta(minutes=10) or t - start > timedelta(hours=1):
            found.append((start, end))
            start = None
    if start is not None:
        found.append((start, end))
    return found


def window_airings(
    changes: Sequence[tuple[datetime, bool]], end_of_data: datetime
) -> list[tuple[datetime, datetime]]:
    """Open stretches of one window from (time, open) changes; one open longer than AIRING_MAX is
    left out (forgotten or a stuck sensor)."""
    found, opened = [], None
    for t, is_open in [*changes, (end_of_data, False)]:
        if is_open and opened is None:
            opened = t
        elif not is_open and opened is not None:
            if t - opened <= AIRING_MAX:
                found.append((opened, t))
            opened = None
    return found


def extract(
    samples: Sequence[Sample],
    outdoor: Sequence[tuple[datetime, float]],
    params: Params,
    fine: Sequence[tuple[datetime, float]] | None = None,
    aired: Sequence[tuple[datetime, datetime]] = (),
) -> list[dict]:
    """One sample per finished heating run in a history window, keyed by its start time.

    Nest only reports a new temperature when its 0.5 °C display changes, so every rate here is
    measured between such reports and only over long stretches.
    """
    temps: list[tuple[datetime, float]] = []
    for t, _, temp in samples:
        if temp is not None and (not temps or temps[-1][1] != temp):
            temps.append((t, temp))
    if not temps:
        return []
    # Nest-style reports cluster at the display flips; plain sensors spread over every 0.1 °C
    snap = sum(_near_boundary(temp) for _, temp in temps) >= 0.75 * len(temps)

    runs: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    unknown_start = samples[0][1]  # already heating when the window opened
    for t, heating, _ in samples:
        if heating and start is None and not unknown_start:
            start = t
        elif not heating:
            if start is not None:
                runs.append((start, t))
            start, unknown_start = None, False
    end_of_data = samples[-1][0]

    def clear(start: datetime, end: datetime) -> bool:
        """No airing, nor the hour the air takes to settle after one, between start and end."""
        return not any(a < end and start < b + AIRING_SETTLE for a, b in aired)

    found = []
    for i, (on, off) in enumerate(runs):
        hours = _hours(off - on)
        rested = _hours(on - (runs[i - 1][1] if i else samples[0][0])) >= 3
        quiet_after = (runs[i + 1][0] if i + 1 < len(runs) else end_of_data) - off
        temp_on = _value_at(temps, on, temps[0][1])
        out = _mean_over(outdoor, on, off, REFERENCE_OUTDOOR)
        sample: dict = {"t": on.astimezone(UTC).isoformat(timespec="minutes"), "outdoor": round(out, 1)}

        # rise: from the first new high after the dead time to the last one before the valve closed
        highs: list[tuple[datetime, float]] = []
        for t, temp in temps:
            if on + timedelta(hours=params.dead_time) <= t <= off and (not highs or temp > highs[-1][1]):
                highs.append((t, temp))
        net = params.net_rise(temp_on, out)
        if (
            len(highs) >= 2
            and highs[-1][1] - highs[0][1] >= 0.3
            and _hours(highs[-1][0] - highs[0][0]) >= 1
            and clear(on, off)
        ):
            net = (highs[-1][1] - highs[0][1]) / _hours(highs[-1][0] - highs[0][0])
            mid = (highs[-1][1] + highs[0][1]) / 2
            # the observed net rise and where it was measured; fit() adds the loss it learns
            sample["slope"], sample["gap"] = round(net, 4), round(mid - out, 1)

        # dead time: a long run from rest (short ones are the Nest topping up a warm slab). A Nest
        # flipping between 25.2 and 25.3 sits at 25.25; it has risen once it reports the next flip.
        if rested and hours >= 2:
            step = 0.5 if snap else 0.2
            soon = next((temp for t, temp in temps if on <= t <= on + timedelta(minutes=30)), None)
            start = _snapped(soon, snap) if soon is not None else _level_at(temps, on, params.loss, out, snap)
            rising = next(
                ((t, temp) for t, temp in temps if on < t <= off and _snapped(temp, snap) >= start + step - 0.01),
                None,
            )
            if rising is not None and clear(on, rising[0]):
                before = [(t, temp) for t, temp in temps if on < t < rising[0]]
                lows = [(t, temp) for t, temp in before if _snapped(temp, snap) < start - 0.01]
                if lows:
                    # dipped below a flip and came back up through it: it fell at the idle drift and
                    # rose at the net rate, so it turned that far between those two reports
                    low = min(temp for _, temp in lows)
                    last_low = [t for t, temp in lows if temp == low][-1]
                    after_low = next(t for t, _ in temps if t > last_low)
                    drift = max(params.loss * (low - out), 0.0)
                    dead = _hours(last_low + (after_low - last_low) * (net / max(drift + net, 0.05)) - on)
                else:  # never seen lower: solve drift-then-rise, but not before it was last seen at the start
                    drift = params.loss * (start - out)
                    span = _hours(rising[0] - on)
                    dead = (start + net * span - _snapped(rising[1], snap)) / max(drift + net, 0.05)
                    still = [t for t, temp in before if _snapped(temp, snap) <= start + 0.01]
                    dead = min(max(dead, _hours(still[-1] - on) if still else 0.0), span)
                if dead >= 0.3:  # faster than any slab: people warmed the room (runs start at bedtime)
                    sample["dead_time"] = round(dead, 3)

        # coast: a long run followed by at least 3 quiet hours
        if hours >= 1.5 and _hours(quiet_after) >= 3 and clear(off, off + timedelta(hours=3)):
            temp_off = _value_at(temps, off, temp_on)
            after = [(t, temp) for t, temp in temps if off < t <= off + timedelta(hours=3)]
            peak = max(after, key=lambda tv: tv[1], default=(off, temp_off))
            sample["coast"] = round(max(0.0, peak[1] - temp_off), 2)
            if peak[1] > temp_off:
                sample["coast_time"] = round(_hours(peak[0] - off), 3)

        # loss: the quiet stretch once the coast has passed, until the valve opens again. A night cut
        # short by the morning block barely moves a Nest's 0.5 °C display, so a finer sensor's slope
        # is used when there is one (its offset doesn't matter for a slope).
        a, b = off + timedelta(hours=max(1.0, params.coast_time + 0.5)), off + quiet_after
        if _hours(b - a) >= 2 and clear(a, b):
            slope = _slope(fine, a, b) if fine else None
            drift = [(t, temp) for t, temp in temps if a <= t <= b]
            if slope is None and len(drift) >= 2 and _hours(drift[-1][0] - drift[0][0]) >= 2:
                slope = (drift[-1][1] - drift[0][1]) / _hours(drift[-1][0] - drift[0][0])
            gap = _value_at(temps, a, temp_on) - _mean_over(outdoor, a, b, REFERENCE_OUTDOOR)
            if slope is not None and gap > 3:
                middle = a + (b - a) / 2
                sample["loss"] = round(max(0.0, -slope / gap), 5)
                sample["night"] = middle.hour >= NIGHT_FROM or middle.hour < NIGHT_UNTIL
        found.append(sample)
    return found


def merge(kept: Iterable[dict], new: Iterable[dict], now: datetime) -> list[dict]:
    """Add a window's samples to the kept ones (a re-read run replaces its older sample)."""
    by_start = {s["t"]: s for s in kept}
    by_start.update({s["t"]: s for s in new})
    oldest = now - timedelta(days=KEEP_DAYS)
    return sorted(
        (s for s in by_start.values() if datetime.fromisoformat(s["t"]) >= oldest),
        key=lambda s: datetime.fromisoformat(s["t"]),
    )


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs = sorted(pairs)
    half, total = sum(w for _, w in pairs) / 2, 0.0
    for value, weight in pairs:
        total += weight
        if total >= half:
            return value
    return pairs[-1][0]


def fit(kept: Sequence[dict], now: datetime) -> Params:
    """Weigh the kept samples (recent ones more) into parameters, shrunk towards the defaults."""

    def weight(sample: dict) -> float:
        age = _hours(now - datetime.fromisoformat(sample["t"])) / 24
        return 0.5 ** (max(age, 0.0) / HALF_LIFE_DAYS)

    values = Params().as_dict()
    nights = [(s["loss"], weight(s)) for s in kept if s.get("loss") is not None and s.get("night")]
    if nights:  # a measured night is the room's own loss: take it as it is, recent nights first
        low, high = BOUNDS["loss"]
        values["loss"] = min(max(_weighted_median(nights), low), high)
    for name in ("dead_time", "loss", "coast", "coast_time"):
        pairs = [(s[name], weight(s)) for s in kept if s.get(name) is not None]
        if not pairs or (name == "loss" and nights):
            continue
        total = sum(w for _, w in pairs)
        if name == "coast":  # mostly 0 or one 0.5 °C display step, so average instead
            estimate = sum(v * w for v, w in pairs) / total
        else:
            estimate = _weighted_median(pairs)
        low, high = BOUNDS[name]
        estimate = min(max(estimate, low), high)
        values[name] = estimate + PRIOR_WEIGHT / (total + PRIOR_WEIGHT) * (values[name] - estimate)

    loss = values["loss"]
    rises = [
        (s["outdoor"], s["slope"] + loss * s["gap"] if "slope" in s else s["rise"], weight(s))
        for s in kept
        if s.get("slope") is not None or s.get("rise") is not None  # "rise": stored before 0.1.7
    ]
    values["rise"] = PRIOR_NET_RISE + loss * PRIOR_GAP
    if rises:
        total = sum(w for *_, w in rises)
        mean_out = sum(o * w for o, _, w in rises) / total
        mean_rise = sum(r * w for _, r, w in rises) / total
        spread = sum(w * (o - mean_out) ** 2 for o, _, w in rises)
        slope = 0.0
        # ponytail: a straight line over outdoor temperature; bin it if winter shows a knee
        if len(rises) >= 4 and spread / total >= 9:  # outdoor sd of 3 °C or more
            slope = sum(w * (o - mean_out) * (r - mean_rise) for o, r, w in rises) / spread
            low, high = BOUNDS["rise_slope"]
            slope = min(max(slope, low), high) * total / (total + PRIOR_WEIGHT)
        # level at the reference outdoor temperature: a median, so one run warmed by people at
        # bedtime doesn't drag it
        rise = _weighted_median([(r - slope * (o - REFERENCE_OUTDOOR), w) for o, r, w in rises])
        low, high = BOUNDS["rise"]
        rise = min(max(rise, low), high)
        values["rise"] = rise + PRIOR_WEIGHT / (total + PRIOR_WEIGHT) * (values["rise"] - rise)
        values["rise_slope"] = slope

    values = {k: round(v, 4) if isinstance(v, float) else v for k, v in values.items()}
    return Params(**{**values, "runs": len(kept)})
