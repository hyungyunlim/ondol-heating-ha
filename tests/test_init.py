from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from custom_components.ondol import logbook as ondol_logbook
from custom_components.ondol.const import DOMAIN, EVENT_ONDOL
from custom_components.ondol.coordinator import OndolCoordinator

NEST = "climate.agibang"
OPTIONS = {
    "presence_entity": "binary_sensor.away",
    "daycare_entity": "binary_sensor.daycare",
    "outdoor_entity": "sensor.outdoor",
    "tolerance": 0.5,
    "won_per_hour": 1000,
}
ROOM = {
    "climate_entity": NEST,
    "name": "아기방",
    "comfort": 25.0,
    "sleep": 26.0,
    "eco": 22.0,
    "away": 20.0,
    "follows_daycare": True,
    "schedule": "매일 07:00 편안, 22:30 수면",
}


def set_nest(hass: HomeAssistant, temperature=25.0, current=25.0, action="idle", mode="heat", preset="none"):
    hass.states.async_set(
        NEST,
        mode,
        {
            "temperature": temperature,
            "current_temperature": current,
            "current_humidity": 55,
            "hvac_action": action,
            "preset_mode": preset,
        },
    )


@pytest.fixture
async def entry(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Asia/Seoul")
    freezer.move_to("2026-10-26 12:00:00+09:00")  # a Monday
    set_nest(hass)
    hass.states.async_set("sensor.outdoor", "8.0")
    hass.states.async_set("binary_sensor.away", "off")
    hass.states.async_set("binary_sensor.daycare", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="온돌 난방",
        options=OPTIONS,
        unique_id=DOMAIN,
        subentries_data=[{"data": ROOM, "subentry_type": "room", "title": "아기방", "unique_id": NEST}],
    )
    entry.add_to_hass(hass)
    await _setup(hass, entry)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with patch.object(OndolCoordinator, "async_calibrate"):  # no recorder in these tests
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
def calls(hass: HomeAssistant, entry: MockConfigEntry):
    """Nest commands (registered after setup, so it replaces the climate service)."""
    return async_mock_service(hass, "climate", "set_temperature")


def coordinator(entry: MockConfigEntry) -> OndolCoordinator:
    return entry.runtime_data


def entity(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str:
    owner = entry.entry_id if key in ("control", "cost_today", "cost_month") else next(iter(entry.subentries))
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{owner}_{key}")
    assert entity_id
    return entity_id


async def refresh_at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry, when: str) -> None:
    freezer.move_to(when)
    await coordinator(entry).async_refresh()
    await hass.async_block_till_done()


async def control_on(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity(hass, entry, "switch", "control")}, blocking=True
    )
    await hass.async_block_till_done()


async def test_entities(hass: HomeAssistant, entry: MockConfigEntry):
    assert entry.state is ConfigEntryState.LOADED
    climate = hass.states.get(entity(hass, entry, "climate", "room"))
    assert climate.state == "heat"
    assert climate.attributes["current_temperature"] == 25.0
    assert climate.attributes["preset_mode"] == "comfort"
    assert climate.attributes["status"] == "ok"
    assert hass.states.get(entity(hass, entry, "switch", "control")).state == "off"
    assert float(hass.states.get(entity(hass, entry, "sensor", "rise_rate")).state) > 0
    assert "예열 시작" in hass.states.get(entity(hass, entry, "sensor", "next_action")).state
    assert hass.states.get(entity(hass, entry, "sensor", "planned_heating")).state == "0"


async def test_advisory_logs_but_sends_nothing(hass, entry, calls, freezer):
    events = async_capture_events(hass, EVENT_ONDOL)
    await refresh_at(hass, freezer, entry, "2026-10-26 17:00:00+09:00")  # preheat for 22:30 sleep
    assert calls == []
    climate = hass.states.get(entity(hass, entry, "climate", "room"))
    assert (climate.attributes["preset_mode"], climate.attributes["temperature"]) == ("sleep", 26.0)
    assert (climate.attributes["nest_setpoint"], climate.attributes["status"]) == (25.5, "advisory")
    assert len(events) == 1 and "제안" in events[0].data["message"] and "25.5" in events[0].data["message"]
    assert hass.states.get(entity(hass, entry, "sensor", "planned_heating")).state == "100"
    assert climate.attributes["hvac_action"] == "heating"  # the plan's valve, while advising
    assert events[0].data["entity_id"] == climate.entity_id

    await refresh_at(hass, freezer, entry, "2026-10-26 17:05:00+09:00")
    assert len(events) == 1  # one line per decision


async def test_control_sends_and_the_nest_confirms(hass, entry, calls, freezer):
    freezer.move_to("2026-10-26 17:00:00+09:00")
    await control_on(hass, entry)
    assert [c.data for c in calls] == [{"entity_id": NEST, "temperature": 25.5}]
    set_nest(hass, temperature=25.5)
    await hass.async_block_till_done()
    room = next(iter(coordinator(entry).rooms.values()))
    assert (room.last_command, room.pending, room.status) == (25.5, None, "ok")
    set_nest(hass, temperature=25.5, action="heating")
    await hass.async_block_till_done()
    await refresh_at(hass, freezer, entry, "2026-10-26 17:01:00+09:00")
    assert hass.states.get(entity(hass, entry, "climate", "room")).attributes["hvac_action"] == "heating"
    await refresh_at(hass, freezer, entry, "2026-10-26 17:10:00+09:00")
    assert len(calls) == 1


async def test_manual_change_holds_until_the_next_block(hass, entry, calls, freezer):
    freezer.move_to("2026-10-26 17:00:00+09:00")
    await control_on(hass, entry)
    set_nest(hass, temperature=25.5)
    await hass.async_block_till_done()

    events = async_capture_events(hass, EVENT_ONDOL)
    set_nest(hass, temperature=27.0)  # someone at the thermostat
    await hass.async_block_till_done()
    await refresh_at(hass, freezer, entry, "2026-10-26 21:00:00+09:00")
    assert len(calls) == 1
    climate = hass.states.get(entity(hass, entry, "climate", "room"))
    assert (climate.attributes["temperature"], climate.attributes["preset_mode"]) == (27.0, None)
    assert climate.attributes["hold_until"].startswith("2026-10-26T22:30")
    assert any("수동 변경" in e.data["message"] for e in events)

    await refresh_at(hass, freezer, entry, "2026-10-26 22:31:00+09:00")
    assert calls[-1].data["temperature"] == 25.5  # sleep 26 °C, aiming one step low on the way up


async def test_interim_automations_are_ignored_only_while_advising(hass, entry, calls, freezer):
    room = next(iter(coordinator(entry).rooms.values()))

    def automation_sets(temperature: float) -> None:
        hass.bus.async_fire(
            EVENT_CALL_SERVICE,
            {"domain": "climate", "service": "set_temperature", "service_data": {"entity_id": [NEST], "temperature": temperature}},
            context=Context(parent_id="trigger"),
        )
        set_nest(hass, temperature=temperature)

    automation_sets(22.0)
    await hass.async_block_till_done()
    assert room.memory.hold is None  # advisory: plan as if the interim YAML were gone

    await control_on(hass, entry)
    automation_sets(21.0)
    await hass.async_block_till_done()
    assert room.memory.hold is not None and room.memory.hold.target == 21.0  # control: don't fight it


async def test_failures_back_off_then_stop(hass, entry, freezer):
    calls = async_mock_service(hass, "climate", "set_temperature", raise_exception=HomeAssistantError("limit"))
    freezer.move_to("2026-10-26 17:00:00+09:00")
    await control_on(hass, entry)
    assert len(calls) == 1
    await refresh_at(hass, freezer, entry, "2026-10-26 17:01:00+09:00")
    assert len(calls) == 1  # backing off for 5 minutes
    await refresh_at(hass, freezer, entry, "2026-10-26 17:06:00+09:00")
    await refresh_at(hass, freezer, entry, "2026-10-26 17:17:00+09:00")
    assert len(calls) == 3
    climate = hass.states.get(entity(hass, entry, "climate", "room"))
    assert climate.attributes["status"] == "error"
    await refresh_at(hass, freezer, entry, "2026-10-26 17:30:00+09:00")
    assert len(calls) == 3  # 20 minutes until the next try


async def test_unconfirmed_command_counts_as_a_failure(hass, entry, calls, freezer):
    freezer.move_to("2026-10-26 17:00:00+09:00")
    await control_on(hass, entry)
    await refresh_at(hass, freezer, entry, "2026-10-26 17:05:00+09:00")
    assert len(calls) == 1  # waiting for the Nest to report it
    await refresh_at(hass, freezer, entry, "2026-10-26 17:11:00+09:00")
    room = next(iter(coordinator(entry).rooms.values()))
    assert (len(calls), room.failures) == (1, 1)
    await refresh_at(hass, freezer, entry, "2026-10-26 17:17:00+09:00")
    assert len(calls) == 2


async def test_away_and_nest_off(hass, entry, calls, freezer):
    await control_on(hass, entry)
    assert calls == []  # comfort 25 °C is already set
    hass.states.async_set("binary_sensor.away", "on")
    await hass.async_block_till_done()
    await refresh_at(hass, freezer, entry, "2026-10-26 12:01:00+09:00")
    assert calls[-1].data["temperature"] == 20.0

    set_nest(hass, temperature=20.0, mode="off")
    hass.states.async_set("binary_sensor.away", "off")
    await hass.async_block_till_done()
    await refresh_at(hass, freezer, entry, "2026-10-26 12:30:00+09:00")
    assert len(calls) == 1
    assert hass.states.get(entity(hass, entry, "climate", "room")).attributes["status"] == "paused"


async def test_user_settings_on_our_entity_go_straight_to_the_nest(hass, entry, calls):
    sid = next(iter(entry.subentries))
    await coordinator(entry).async_user_target(sid, temperature=24.0)  # even with control off
    assert calls[-1].data == {"entity_id": NEST, "temperature": 24.0}
    await coordinator(entry).async_user_target(sid, preset="eco")
    assert calls[-1].data["temperature"] == 22.0
    climate = hass.states.get(entity(hass, entry, "climate", "room"))
    assert (climate.attributes["preset_mode"], climate.attributes["temperature"]) == ("eco", 22.0)
    assert climate.attributes["hold_until"].startswith("2026-10-26T22:30")


async def test_heating_hours_and_cost_split_at_midnight(hass, entry, freezer):
    freezer.move_to("2026-10-26 23:30:00+09:00")
    set_nest(hass, action="heating")
    await hass.async_block_till_done()
    await refresh_at(hass, freezer, entry, "2026-10-27 00:30:00+09:00")
    assert float(hass.states.get(entity(hass, entry, "sensor", "heating_today")).state) == pytest.approx(0.5)
    assert float(hass.states.get(entity(hass, entry, "sensor", "cost_today")).state) == 500
    assert float(hass.states.get(entity(hass, entry, "sensor", "cost_month")).state) == 1000


async def test_restart_keeps_state_and_spots_offline_changes(hass, entry, calls, freezer):
    freezer.move_to("2026-10-26 17:00:00+09:00")
    await control_on(hass, entry)
    set_nest(hass, temperature=25.5)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)

    set_nest(hass, temperature=27.0)  # changed while we were down
    await _setup(hass, entry)
    assert hass.states.get(entity(hass, entry, "switch", "control")).state == "on"
    climate = hass.states.get(entity(hass, entry, "climate", "room"))
    assert (climate.attributes["temperature"], climate.attributes["status"]) == (27.0, "hold")
    assert len(calls) == 1


async def test_logbook_describes_our_events(hass):
    described = {}
    ondol_logbook.async_describe_events(hass, lambda domain, event, fn: described.setdefault(event, fn))

    class FakeEvent:
        data = {"name": "아기방", "message": "Nest 26.0°C", "entity_id": "climate.agibang_ondol"}

    assert described[EVENT_ONDOL](FakeEvent()) == {
        "name": "아기방",
        "message": "Nest 26.0°C",
        "entity_id": "climate.agibang_ondol",
    }
