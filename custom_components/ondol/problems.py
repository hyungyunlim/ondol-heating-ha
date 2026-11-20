"""Problems worth telling someone about: a room heating without warming, a thermostat offline or
refusing commands. One is shown at a time: offline, then failing, then no_heat.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import math

GAP = timedelta(minutes=10)  # idling this briefly doesn't end a heating run
BLIND = timedelta(minutes=30)  # not watched this long (Home Assistant down): the run is unknown
OFFLINE_AFTER = timedelta(minutes=30)
JUDGE_AFTER = 1.5  # hours past the dead time before a call is judged
FELL = 0.2  # °C a finer sensor has fallen below the call's start (it reads in 0.1 °C steps)
STEP_FELL = 0.45  # a Nest reads in 0.5 °C steps: a whole step lower
WARMED = 0.3  # °C above the run's lowest reading: it heats again


@dataclass
class Run:
    """The thermostat calling for heat, idle gaps up to GAP included."""

    start: datetime
    temp: float  # the Nest's reading at the start
    low: float  # its lowest since
    seen: datetime
    idle: datetime | None = None  # idling since
    fine: float | None = None  # a finer sensor at the start, where there is one
    fine_low: float | None = None

    def as_dict(self) -> dict:
        return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict | None) -> Run | None:
        if not data:
            return None
        times = ("start", "seen", "idle")
        return cls(**{k: datetime.fromisoformat(v) if k in times and v else v for k, v in data.items()})


@dataclass(frozen=True)
class Problem:
    kind: str  # offline | failing | no_heat
    since: datetime  # when it began: went offline, first failed command, start of the run
    reason: str  # one plain sentence for whoever gets the notification


def follow(run: Run | None, heating: bool, temp: float | None, now: datetime, fine: float | None = None) -> Run | None:
    """Carry a heating run on to ``now``."""
    if run is not None and (now - run.seen > BLIND or (run.idle is not None and now - run.idle > GAP)):
        run = None
    if run is None:
        return Run(now, temp, temp, now, fine=fine, fine_low=fine) if heating and temp is not None else None
    run.seen = now
    run.idle = None if heating else run.idle or now
    if temp is not None:
        run.low = min(run.low, temp)
    if fine is not None and run.fine_low is not None:
        run.fine_low = min(run.fine_low, fine)
    return run


def _level(temp: float) -> float:
    """A Nest reading taken back onto the display flip it reports just past: 25.7 and 25.8 flicker
    around 25.75, so both are 25.75."""
    return math.floor(temp * 2) / 2 + 0.25 if abs((temp * 2) % 1 - 0.5) <= 0.2 + 1e-9 else temp


def no_heat(run: Run, temp: float, fine: float | None, dead_time: float, now: datetime) -> str | None:
    """Why the room counts as heating without warming, or None: after the dead time and JUDGE_AFTER
    more of one call it is lower than when the call began, by a finer sensor where there is one, else
    by a whole Nest display step. No model in it, so a cold night that slows heating can't hide it."""
    hours = (now - run.start).total_seconds() / 3600
    if hours < dead_time + JUDGE_AFTER:
        return None
    if run.fine is not None and fine is not None:
        fell = run.fine - fine
        if fell < FELL:
            return None
    else:
        fell = _level(run.temp) - _level(temp)
        if fell < STEP_FELL:
            return None
    h, m = divmod(round(hours * 60), 60)
    took = f"{h}시간 {m}분째" if m else f"{h}시간째"
    return f"{took} 난방 중인데 오히려 {fell:.1f}° 내려갔어요. 구동기나 단지 난방 공급을 확인해 주세요."


def warmed(run: Run, temp: float, fine: float | None) -> bool:
    """The room heats again: WARMED above the run's lowest reading, on either sensor."""
    return temp - run.low >= WARMED or (fine is not None and run.fine_low is not None and fine - run.fine_low >= WARMED)
