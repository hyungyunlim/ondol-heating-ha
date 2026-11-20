from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OndolConfigEntry
from .entity import OndolEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OndolConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([PlannedReturn(entry.runtime_data)])


class PlannedReturn(OndolEntity, DateTimeEntity):
    """When the family is due home from a trip. While away every room preheats to be at its schedule's
    preset by then; it is cleared once home, three hours after it while still away, or by its button."""

    _attr_icon = "mdi:home-clock"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "return")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.return_at

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Each room's preheat start for it (known while away), and the first of them."""
        starts = {
            room.name: room.plan.return_start
            for room in self.coordinator.rooms.values()
            if room.plan and room.plan.return_start
        }
        first = min(starts.values(), default=None)
        return {
            "preheat_starts": {name: dt_util.as_local(start).isoformat() for name, start in starts.items()},
            "first_preheat": dt_util.as_local(first).isoformat() if first else None,
        }

    async def async_set_value(self, value: datetime) -> None:
        await self.coordinator.async_set_return(value)
