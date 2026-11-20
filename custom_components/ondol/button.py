from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OndolConfigEntry
from .entity import OndolEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OndolConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([ClearReturn(entry.runtime_data)])


class ClearReturn(OndolEntity, ButtonEntity):
    """Forget the planned return: rooms stay at 외출 while the house is away."""

    _attr_icon = "mdi:home-remove"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "clear_return")

    async def async_press(self) -> None:
        await self.coordinator.async_set_return(None, "지우기 버튼")
