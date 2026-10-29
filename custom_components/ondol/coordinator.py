"""Runs the rooms: watches the Nests and inputs, plans every few minutes, and (with control on)
sends Nest setpoints within the Nest rate limits. With control off it only logs what it would do.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_ECO,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE_DATA,
    ATTR_TEMPERATURE,
    EVENT_CALL_SERVICE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import model, planner, schedule
from .const import (
    CONF_CLIMATE,
    CONF_DAYCARE,
    CONF_FOLLOWS_DAYCARE,
    CONF_OCCUPANCY,
    CONF_NAME,
    CONF_OUTDOOR,
    CONF_PRESENCE,
    CONF_SCHEDULE,
    CONF_TOLERANCE,
    CONF_WON_PER_HOUR,
    DEFAULT_TOLERANCE,
    DOMAIN,
    EVENT_ONDOL,
    PRESETS,
    SUBENTRY_ROOM,
)

LOGGER = logging.getLogger(__package__)

# Nest allows 5 commands/min and 100/hour per device, shared with every other app; stay far below
PER_MINUTE = 2
PER_HOUR = 12
CONFIRM_WITHIN = timedelta(minutes=10)  # the Nest reports a new setpoint through Pub/Sub
MAX_FAILURES = 3
IN_USE_AFTER = timedelta(minutes=10)  # someone walking past an occupancy sensor isn't using the room
HISTORY_DAYS = 7
OFF = (None, STATE_UNAVAILABLE, STATE_UNKNOWN)


@dataclass
class RoomState:
    subentry_id: str
    name: str
    nest: str
    room: planner.Room
    params: model.Params = field(default_factory=model.Params)
    samples: list[dict] = field(default_factory=list)
    memory: planner.Memory = field(default_factory=planner.Memory)
    plan: planner.Plan | None = None
    status: str = "starting"  # ok | advisory | hold | paused | unavailable | error
    entity_id: str | None = None  # our climate entity, for logbook entries
    last_command: float | None = None  # last setpoint the Nest confirmed for us
    pending: tuple[float, datetime] | None = None
    sent: deque[datetime] = field(default_factory=deque)
    failures: int = 0
    retry_at: datetime | None = None
    heating_since: datetime | None = None
    today: list = field(default_factory=lambda: ["", 0.0])  # [date, hours]
    month: list = field(default_factory=lambda: ["", 0.0])  # [YYYY-MM, hours]
    checked: bool = False  # compared the Nest with last_command after a restart
    last_call: tuple[datetime, str] | None = None  # someone else's climate call on this Nest: (when, origin)
    occupancy: str | None = None  # binary sensor, on = someone in the room
    last_used: datetime | None = None  # last time someone had been in the room for IN_USE_AFTER
    logged: tuple | None = None


class OndolCoordinator(DataUpdateCoordinator[None]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, LOGGER, config_entry=entry, name=DOMAIN, update_interval=timedelta(minutes=5))
        options = entry.options
        self.presence: str | None = options.get(CONF_PRESENCE)
        self.daycare: str | None = options.get(CONF_DAYCARE)
        self.outdoor_entity: str = options[CONF_OUTDOOR]
        self.tolerance: float = options.get(CONF_TOLERANCE, DEFAULT_TOLERANCE)
        self.won_per_hour: float = options.get(CONF_WON_PER_HOUR) or 0.0
        self.control = False
        self.rooms: dict[str, RoomState] = {}
        self._outdoor = model.REFERENCE_OUTDOOR
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._context = Context()  # marks our own service calls in the logbook

    # ---- lifecycle -------------------------------------------------------------------------

    async def _async_setup(self) -> None:
        stored = await self._store.async_load() or {}
        self.control = stored.get("control", False)
        saved = stored.get("rooms", {})
        for subentry in self.config_entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_ROOM:
                continue
            data = subentry.data
            room = RoomState(
                subentry_id=subentry.subentry_id,
                name=data.get(CONF_NAME) or subentry.title,
                nest=data[CONF_CLIMATE],
                room=planner.Room(
                    presets={preset: float(data[preset]) for preset in PRESETS},
                    entries=schedule.parse(data[CONF_SCHEDULE]),
                    follows_daycare=data.get(CONF_FOLLOWS_DAYCARE, False),
                ),
                occupancy=data.get(CONF_OCCUPANCY),
            )
            self._restore(room, saved.get(subentry.subentry_id, {}))
            # known from the last run, so logbook lines from the first refresh land on the entity too
            room.entity_id = er.async_get(self.hass).async_get_entity_id(
                CLIMATE_DOMAIN, DOMAIN, f"{subentry.subentry_id}_room"
            )
            self.rooms[subentry.subentry_id] = room

    async def async_start(self) -> None:
        entry = self.config_entry
        by_nest = {room.nest: room for room in self.rooms.values()}
        entry.async_on_unload(
            async_track_state_change_event(self.hass, list(by_nest), partial(self._nest_changed, by_nest))
        )
        entry.async_on_unload(
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, partial(self._service_called, by_nest), _climate_call)
        )
        inputs = [e for e in (self.presence, self.daycare, self.outdoor_entity) if e]
        inputs += [room.occupancy for room in self.rooms.values() if room.occupancy]
        entry.async_on_unload(async_track_state_change_event(self.hass, inputs, self._input_changed))
        entry.async_on_unload(async_track_time_change(self.hass, self._calibrate_daily, hour=4, minute=30, second=0))
        entry.async_create_background_task(self.hass, self.async_calibrate(), f"{DOMAIN}_calibrate")

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self._store.async_save(self._snapshot())

    # ---- persistence -----------------------------------------------------------------------

    def _restore(self, room: RoomState, saved: dict) -> None:
        room.params = model.Params.from_dict(saved.get("params"))
        room.samples = saved.get("samples", [])
        room.last_command = saved.get("last_command")
        room.today = saved.get("today", room.today)
        room.month = saved.get("month", room.month)
        used = saved.get("last_used")  # unknown until someone has used the room: plain schedule
        room.last_used = datetime.fromisoformat(used) if used else None
        if hold := saved.get("hold"):
            room.memory.hold = planner.Hold(hold["target"], datetime.fromisoformat(hold["until"]), hold.get("preset"))
        # in-flight decisions, so a restart mid-preheat or mid-night keeps them
        mem = saved.get("memory") or {}
        room.memory.preheat_for, room.memory.early_for = _when(mem.get("preheat_for")), _when(mem.get("early_for"))
        room.memory.phase = mem.get("phase", "normal")
        room.memory.phase_target, room.memory.phase_since = mem.get("phase_target"), _when(mem.get("phase_since"))

    def _snapshot(self) -> dict[str, Any]:
        now = dt_util.now()
        rooms = {}
        for sid, room in self.rooms.items():
            self._settle(room, now)
            hold, mem = room.memory.hold, room.memory
            rooms[sid] = {
                "params": room.params.as_dict(),
                "samples": room.samples,
                "last_command": room.last_command,
                "today": room.today,
                "month": room.month,
                "last_used": room.last_used.isoformat() if room.last_used else None,
                "hold": {"target": hold.target, "until": hold.until.isoformat(), "preset": hold.preset} if hold else None,
                "memory": {
                    "preheat_for": _iso(mem.preheat_for),
                    "early_for": _iso(mem.early_for),
                    "phase": mem.phase,
                    "phase_target": mem.phase_target,
                    "phase_since": _iso(mem.phase_since),
                },
            }
        return {"control": self.control, "rooms": rooms}

    def _save(self) -> None:
        self._store.async_delay_save(self._snapshot, 30)

    # ---- inputs ------------------------------------------------------------------------------

    def _is_on(self, entity_id: str | None) -> bool:
        """Unavailable reads as off: not certainly away, not at daycare."""
        state = self.hass.states.get(entity_id) if entity_id else None
        return state is not None and state.state == STATE_ON

    def outdoor(self) -> float:
        state = self.hass.states.get(self.outdoor_entity)
        try:
            self._outdoor = float(state.state)  # type: ignore[union-attr]
        except (AttributeError, TypeError, ValueError):
            pass  # keep the last good reading
        return self._outdoor

    def _used_ago(self, room: RoomState, now: datetime) -> float | None:
        """Hours since the room was last in use (0 while it is), None without an occupancy sensor."""
        if not room.occupancy:
            return None
        state = self.hass.states.get(room.occupancy)
        if state is not None and state.state == STATE_ON and now - state.last_changed >= IN_USE_AFTER:
            room.last_used = now
        return max((now - room.last_used).total_seconds(), 0) / 3600 if room.last_used else None

    @callback
    def _input_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        for room in self.rooms.values():  # a long enough stay ended: in use until it ended
            if (
                event.data["entity_id"] == room.occupancy
                and old is not None
                and old.state == STATE_ON
                and new is not None
                and new.last_changed - old.last_changed >= IN_USE_AFTER
            ):
                room.last_used = new.last_changed
        if (
            event.data["entity_id"] == self.daycare
            and new is not None
            and new.state == STATE_ON
            and (old is None or old.state != STATE_ON)
        ):
            for room in self.rooms.values():  # the child left: a manual setting for them is over
                if room.room.follows_daycare:
                    room.memory.hold = None
        self.config_entry.async_create_task(self.hass, self.async_request_refresh())

    @callback
    def _service_called(self, by_nest: dict[str, RoomState], event: Event) -> None:
        """Remember who last called a climate service on a Nest; the Nest's own state update
        arrives later through Pub/Sub without that context."""
        if event.context.id == self._context.id:
            return
        origin = "automation" if event.context.parent_id and not event.context.user_id else "user"
        targets = (event.data.get(ATTR_SERVICE_DATA) or {}).get(ATTR_ENTITY_ID) or []
        for entity_id in [targets] if isinstance(targets, str) else targets:
            if room := by_nest.get(entity_id):
                room.last_call = (dt_util.now(), origin)

    @callback
    def _nest_changed(self, by_nest: dict[str, RoomState], event: Event[EventStateChangedData]) -> None:
        room = by_nest[event.data["entity_id"]]
        old, new = event.data["old_state"], event.data["new_state"]
        now = dt_util.now()
        self._track_heating(room, new, now)
        if old is None or new is None or old.state in OFF or new.state != HVACMode.HEAT:
            self.config_entry.async_create_task(self.hass, self.async_request_refresh())
            return
        before, after = old.attributes.get(ATTR_TEMPERATURE), new.attributes.get(ATTR_TEMPERATURE)
        if after is not None and before != after:
            if room.pending and abs(after - room.pending[0]) < 0.01:
                room.pending, room.failures, room.retry_at = None, 0, None
                room.last_command = after
            elif self._by_automation(room, now) and not self.control:
                # the interim YAML automations still run during the advisory trial: plan as if retired
                self._log(room, ("automation", after), f"자동화가 Nest를 {after:.1f}°C로 바꿈 — 제안에는 반영 안 함")
            else:
                self._manual(room, float(after), now, "자동화 변경" if self._by_automation(room, now) else "수동 변경")
            self._save()
        self.config_entry.async_create_task(self.hass, self.async_request_refresh())

    def _manual(self, room: RoomState, target: float, now: datetime, why: str) -> None:
        """A person set the Nest: keep their value until the next schedule block."""
        until = self.next_block_start(room, now)
        room.memory.hold = planner.Hold(target, until)
        room.memory.preheat_for = room.memory.early_for = None
        room.pending = None
        self._log(room, ("manual", target, until), f"{why}: Nest {target:.1f}°C — {until:%H:%M}까지 유지")

    @staticmethod
    def _by_automation(room: RoomState, now: datetime) -> bool:
        call = room.last_call
        return call is not None and call[1] == "automation" and now - call[0] < CONFIRM_WITHIN

    def next_block_start(self, room: RoomState, now: datetime) -> datetime:
        ahead = schedule.upcoming(room.room.entries, now, 24 * 7)
        return ahead[0].start if ahead else now + timedelta(days=1)

    # ---- heating hours -------------------------------------------------------------------------

    def _track_heating(self, room: RoomState, state: State | None, now: datetime) -> None:
        heating = state is not None and state.attributes.get(ATTR_HVAC_ACTION) == HVACAction.HEATING
        self._settle(room, now)
        if heating and room.heating_since is None:
            room.heating_since = now
        elif not heating:
            room.heating_since = None

    def _settle(self, room: RoomState, now: datetime) -> None:
        """Move heating time up to now into today's and this month's totals, split at midnight."""
        start = room.heating_since
        midnight = dt_util.start_of_local_day(now)
        if start is not None and start < midnight:
            self._add(room, start, midnight)
            start = midnight
        day, month = now.date().isoformat(), now.strftime("%Y-%m")
        if room.today[0] != day:
            room.today = [day, 0.0]
        if room.month[0] != month:
            room.month = [month, 0.0]
        if start is not None:
            self._add(room, start, now)
            room.heating_since = now

    @staticmethod
    def _add(room: RoomState, start: datetime, end: datetime) -> None:
        hours = max((end - start).total_seconds(), 0) / 3600
        local = dt_util.as_local(start)
        if room.today[0] == local.date().isoformat():
            room.today[1] += hours
        if room.month[0] == local.strftime("%Y-%m"):
            room.month[1] += hours

    def wants_heat(self, room: RoomState) -> bool | None:
        """Whether the plan's setpoint is above the room, i.e. the Nest would heat for it."""
        nest = self.hass.states.get(room.nest)
        temp = nest.attributes.get(ATTR_CURRENT_TEMPERATURE) if nest is not None else None
        if room.plan is None or not _number(temp):
            return None
        return float(temp) < room.plan.setpoint

    def hours_today(self, room: RoomState) -> float:
        self._settle(room, dt_util.now())
        return room.today[1]

    def cost(self, period: str) -> float | None:
        if not self.won_per_hour:
            return None
        now = dt_util.now()
        total = 0.0
        for room in self.rooms.values():
            self._settle(room, now)
            total += room.today[1] if period == "today" else room.month[1]
        return round(total * self.won_per_hour)

    # ---- calibration -----------------------------------------------------------------------------

    @callback
    def _calibrate_daily(self, _now: datetime) -> None:
        self.config_entry.async_create_background_task(self.hass, self.async_calibrate(), f"{DOMAIN}_calibrate")

    async def async_calibrate(self) -> None:
        """Re-learn every room from the last week of recorder history."""
        now = dt_util.now()
        start = now - timedelta(days=HISTORY_DAYS)
        try:
            states = await get_instance(self.hass).async_add_executor_job(
                partial(
                    history.get_significant_states,
                    self.hass,
                    start,
                    now,
                    [room.nest for room in self.rooms.values()] + [self.outdoor_entity],
                    significant_changes_only=False,
                )
            )
        except Exception as err:
            LOGGER.warning("Could not read history for calibration: %s", err)
            return
        outdoor = [(s.last_updated, float(s.state)) for s in states.get(self.outdoor_entity, []) if _number(s.state)]
        for room in self.rooms.values():
            rows = [
                (
                    s.last_updated,
                    s.attributes.get(ATTR_HVAC_ACTION) == HVACAction.HEATING,
                    s.attributes.get(ATTR_CURRENT_TEMPERATURE),
                )
                for s in states.get(room.nest, [])
                if s.state not in OFF
            ]
            if not rows:
                continue
            found = model.extract(rows, outdoor, room.params)
            room.samples = model.merge(room.samples, found, now)
            room.params = model.fit(room.samples, now)
            LOGGER.info("Calibrated %s from %d runs: %s", room.name, len(found), room.params)
        self._save()
        self.async_update_listeners()

    # ---- planning and commands ------------------------------------------------------------------

    async def _async_update_data(self) -> None:
        now = dt_util.now()
        for room in self.rooms.values():
            try:
                await self._async_run(room, now)
            except Exception:
                LOGGER.exception("Planning %s failed", room.name)
                room.status = "error"
        self._save()

    async def _async_run(self, room: RoomState, now: datetime) -> None:
        nest = self.hass.states.get(room.nest)
        available = nest is not None and nest.state not in OFF
        temp = nest.attributes.get(ATTR_CURRENT_TEMPERATURE) if available else None  # type: ignore[union-attr]
        target = nest.attributes.get(ATTR_TEMPERATURE) if available else None  # type: ignore[union-attr]
        if available and not room.checked:
            room.checked = True
            self._track_heating(room, nest, now)
            if room.last_command is not None and target is not None and abs(target - room.last_command) >= 0.01:
                self._manual(room, float(target), now, "재시작 중 수동 변경")

        room.plan = planner.plan(
            room.room,
            room.params,
            planner.Now(
                at=now,
                temp=float(temp) if _number(temp) else None,
                outdoor=self.outdoor(),
                heating=available and nest.attributes.get(ATTR_HVAC_ACTION) == HVACAction.HEATING,  # type: ignore[union-attr]
                away=self._is_on(self.presence),
                daycare=self._is_on(self.daycare),
                used_ago=self._used_ago(room, now),
            ),
            room.memory,
            self.tolerance,
        )
        plan = room.plan
        if not available:
            room.status = "unavailable"
            return
        if nest.state != HVACMode.HEAT or nest.attributes.get(ATTR_PRESET_MODE) == PRESET_ECO:  # type: ignore[union-attr]
            room.status = "paused"  # a person turned it off or to eco; leave it alone
            return
        if plan.reason == "hold":
            room.status = "hold"
            return
        if target is not None and abs(target - plan.setpoint) < 0.01:
            room.status, room.logged = "ok", None
            return
        if room.failures >= MAX_FAILURES and room.retry_at and now < room.retry_at:
            room.status = "error"
            return
        if not self.control:
            room.status = "advisory"
            self._log(
                room,
                ("advise", plan.setpoint, plan.reason),
                f"제안: Nest {_fmt(target)} → {plan.setpoint:.1f}°C — {plan.text}",
            )
            return
        await self._async_send(room, plan.setpoint, now, plan.text)

    async def _async_send(self, room: RoomState, setpoint: float, now: datetime, why: str) -> bool:
        if room.pending:
            if now - room.pending[1] < CONFIRM_WITHIN:
                return False  # still waiting for the Nest to report the last one
            room.pending = None
            self._failed(room, now, "Nest가 설정을 반영하지 않음")
        if room.retry_at and now < room.retry_at:
            return False
        while room.sent and now - room.sent[0] >= timedelta(hours=1):
            room.sent.popleft()
        if len(room.sent) >= PER_HOUR or sum(now - t < timedelta(minutes=1) for t in room.sent) >= PER_MINUTE:
            LOGGER.debug("%s: holding a command back for the Nest rate limit", room.name)
            return False
        room.sent.append(now)
        room.pending = (setpoint, now)
        try:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                {ATTR_ENTITY_ID: room.nest, ATTR_TEMPERATURE: setpoint},
                blocking=True,
                context=self._context,
            )
        except HomeAssistantError as err:
            room.pending = None
            self._failed(room, now, str(err))
            return False
        room.status = "ok"
        self._log(room, ("sent", setpoint, why), f"Nest {setpoint:.1f}°C — {why}")
        return True

    def _failed(self, room: RoomState, now: datetime, why: str) -> None:
        room.failures += 1
        delay = min(timedelta(minutes=5) * 2 ** (room.failures - 1), timedelta(hours=1))
        room.retry_at = now + delay
        if room.failures >= MAX_FAILURES:
            room.status = "error"
        LOGGER.warning("%s: Nest command failed (%d): %s", room.name, room.failures, why)
        self._log(
            room,
            ("failed", room.failures),
            f"Nest 명령 실패 {room.failures}회 ({why}) — {int(delay.total_seconds() // 60)}분 뒤 다시",
        )

    # ---- entity actions ------------------------------------------------------------------------

    async def async_set_control(self, on: bool) -> None:
        self.control = on
        for room in self.rooms.values():
            room.logged = None
        self._save()
        await self.async_refresh()

    async def async_user_target(self, subentry_id: str, temperature: float | None = None, preset: str | None = None) -> None:
        """A person picked a temperature or preset on our climate entity: send it now, hold it."""
        room = self.rooms[subentry_id]
        now = dt_util.now()
        target = room.room.presets[preset] if preset else float(temperature)  # type: ignore[arg-type]
        room.memory.hold = planner.Hold(target, self.next_block_start(room, now), preset)
        room.memory.preheat_for = room.memory.early_for = None
        room.pending = None
        sent = await self._async_send(room, planner.setpoint_for(target), now, "직접 설정")
        if not sent:
            raise HomeAssistantError(f"{room.name}: the Nest did not take the command (rate limit or error)")
        self._save()
        await self.async_refresh()

    def _log(self, room: RoomState, key: tuple, message: str) -> None:
        """One logbook line per decision, not one per evaluation."""
        if room.logged == key:
            return
        room.logged = key
        LOGGER.info("%s: %s", room.name, message)
        self.hass.bus.async_fire(
            EVENT_ONDOL,
            {"name": room.name, "message": message, "entity_id": room.entity_id},
            context=self._context,
        )


@callback
def _climate_call(data: dict[str, Any]) -> bool:
    return data.get(ATTR_DOMAIN) == CLIMATE_DOMAIN


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _fmt(value: Any) -> str:
    return f"{float(value):.1f}°C" if _number(value) else "?"
