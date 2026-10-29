from unittest.mock import patch

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ondol.const import DOMAIN

HUB = {"outdoor_entity": "sensor.outdoor", "tolerance": 0.5, "won_per_hour": 0}
ROOM = {
    "climate_entity": "climate.agibang",
    "comfort": 25.0,
    "sleep": 26.0,
    "eco": 22.0,
    "away": 20.0,
    "follows_daycare": True,
    "schedule": "월-금 07:00 편안, 22:30 수면\n토,일 08:00 편안, 22:30 수면",
}


async def test_hub_flow_is_single_instance(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.ondol.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], HUB)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (result["title"], result["options"]) == ("온돌 난방", HUB)

    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert (again["type"], again["reason"]) == (FlowResultType.ABORT, "already_configured")


async def test_options_flow(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, options=HUB, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**HUB, "presence_entity": "binary_sensor.nobody_home_certain", "won_per_hour": 850}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["won_per_hour"] == 850
    assert entry.options["presence_entity"] == "binary_sensor.nobody_home_certain"


async def test_room_flow_validates_and_names_the_room(hass: HomeAssistant):
    hass.states.async_set("climate.agibang", "heat", {"friendly_name": "아기방"})
    entry = MockConfigEntry(domain=DOMAIN, options=HUB, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init((entry.entry_id, "room"), context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ROOM, "schedule": "월-금 7시 편안"}
    )
    assert result["errors"] == {"schedule": "invalid_schedule"}
    assert "HH:MM" in result["description_placeholders"]["error"]

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], ROOM)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(entry.subentries.values()))
    assert (subentry.title, subentry.unique_id, subentry.data["name"]) == ("아기방", "climate.agibang", "아기방")

    again = await hass.config_entries.subentries.async_init((entry.entry_id, "room"), context={"source": SOURCE_USER})
    again = await hass.config_entries.subentries.async_configure(again["flow_id"], ROOM)
    assert again["errors"] == {"climate_entity": "already_configured"}


async def test_room_reconfigure(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        options=HUB,
        unique_id=DOMAIN,
        subentries_data=[
            {"data": {**ROOM, "name": "아기방"}, "subentry_type": "room", "title": "아기방", "unique_id": "climate.agibang"}
        ],
    )
    entry.add_to_hass(hass)
    subentry_id = next(iter(entry.subentries))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "room"), context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**ROOM, "name": "아이방", "sleep": 25.5}
    )
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    subentry = entry.subentries[subentry_id]
    assert (subentry.title, subentry.data["sleep"]) == ("아이방", 25.5)
