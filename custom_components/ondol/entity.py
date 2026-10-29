from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OndolCoordinator, RoomState


class OndolEntity(CoordinatorEntity[OndolCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OndolCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)}, name="온돌 난방", manufacturer="Ondol")


class RoomEntity(CoordinatorEntity[OndolCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OndolCoordinator, subentry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.subentry_id = subentry_id
        room = coordinator.rooms[subentry_id]
        self._attr_unique_id = f"{subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=f"{room.name} 온돌",
            manufacturer="Ondol",
            model=room.nest,
        )

    @property
    def room(self) -> RoomState:
        return self.coordinator.rooms[self.subentry_id]
