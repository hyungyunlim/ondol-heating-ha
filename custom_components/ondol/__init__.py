"""Ondol: runs radiant floor heating through each room's thermostat, ahead of the slab's lag."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import OndolCoordinator

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH]

type OndolConfigEntry = ConfigEntry[OndolCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: OndolConfigEntry) -> bool:
    coordinator = OndolCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await coordinator.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))  # options or rooms changed
    return True


async def _async_reload(hass: HomeAssistant, entry: OndolConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OndolConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
