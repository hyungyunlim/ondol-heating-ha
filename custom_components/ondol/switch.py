from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OndolConfigEntry
from .entity import OndolEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OndolConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ControlSwitch(entry.runtime_data)])


class ControlSwitch(OndolEntity, SwitchEntity):
    """Off: advisory, only logs what it would do. On: sends setpoints to the Nests."""

    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "control")

    @property
    def is_on(self) -> bool:
        return self.coordinator.control

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control(False)
