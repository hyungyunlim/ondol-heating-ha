"""Problems worth telling someone about: a room heating without warming, a thermostat offline or
refusing commands. One is shown at a time: offline, then failing, then no_heat.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Params

GAP = timedelta(minutes=10)  # idling this briefly doesn't end a heating run
BLIND = timedelta(minutes=30)  # not watched this long (Home Assistant down): the run is unknown
OFFLINE_AFTER = timedelta(minutes=30)
JUDGE_AFTER = 1.5  # hours past the dead time before a run is judged
EXPECTED = 0.4  # °C the model must expect by now; less is a slow room (a cold snap), not a fault
FLAT = 0.15  # °C the room rose at most
WARMED = 0.3  # °C above the run's lowest reading: it heats again


@dataclass
class Run:
    """The thermostat calling for heat, idle gaps up to GAP included."""

    start: datetime
    temp: float  # room at the start
    low: float  # lowest reading since
    seen: datetime
    idle: datetime | None = None  # idling since

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


def follow(run: Run | None, heating: bool, temp: float | None, now: datetime) -> Run | None:
    """Carry a heating run on to ``now``."""
    if run is not None and (now - run.seen > BLIND or (run.idle is not None and now - run.idle > GAP)):
        run = None
    if run is None:
        return Run(now, temp, temp, now) if heating and temp is not None else None
    run.seen = now
    run.idle = None if heating else run.idle or now
    if temp is not None:
        run.low = min(run.low, temp)
    return run


def _display(temp: float) -> float:
    return round(temp * 2) / 2


def no_heat(run: Run, temp: float, params: Params, outdoor: float, now: datetime) -> str | None:
    """Why the room counts as heating without warming, or None.

    A Nest reports the room in its 0.5 °C display steps, so the room has only certainly risen less
    than FLAT once its display is a step below where the run started.
    """
    # ponytail: assumes 0.5 °C display steps; a finer thermostat is flagged later than it could be
    hours = (now - run.start).total_seconds() / 3600
    expected = params.heated(run.temp, outdoor, hours) - run.temp
    if hours < params.dead_time + JUDGE_AFTER or expected < EXPECTED:
        return None
    if _display(temp) - _display(run.temp) + 0.5 >= FLAT:
        return None
    h, m = divmod(round(hours * 60), 60)
    took = f"{h}시간 {m}분째" if m else f"{h}시간째"
    return (
        f"{took} 난방 중인데 온도가 오르지 않아요 ({run.temp:.1f}° → {temp:.1f}°, 예상 +{expected:.1f}°). "
        "구동기나 단지 난방 공급을 확인해 주세요."
    )
