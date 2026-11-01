from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OndolConfigEntry
from .coordinator import OndolCoordinator
from .entity import RoomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OndolConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    for subentry_id in coordinator.rooms:
        async_add_entities([ProblemSensor(coordinator, subentry_id)], config_subentry_id=subentry_id)


class ProblemSensor(RoomEntity, BinarySensorEntity):
    """On while the room needs a person: heating without warming, or its Nest offline or refusing
    commands. For notifications; ``reason`` is one plain sentence."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: OndolCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "problem")

    @property
    def is_on(self) -> bool:
        return self.room.problem is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        problem = self.room.problem
        return {
            "room": self.room.name,
            "kind": problem.kind if problem else None,
            "reason": problem.reason if problem else None,
            "since": dt_util.as_local(problem.since).isoformat() if problem else None,
        }
