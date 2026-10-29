from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.climate import ATTR_CURRENT_TEMPERATURE
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OndolConfigEntry
from .coordinator import OndolCoordinator, RoomState
from .entity import OndolEntity, RoomEntity


@dataclass(frozen=True, kw_only=True)
class ModelSensor:
    key: str
    unit: str
    value: Callable[[RoomState, float, float], float]  # (room, room °C, outdoor °C)


# learned parameters, shown at today's conditions where that reads better than the raw value
MODEL_SENSORS = (
    ModelSensor(key="dead_time", unit=UnitOfTime.HOURS, value=lambda r, t, o: r.params.dead_time),
    ModelSensor(key="rise_rate", unit="°C/h", value=lambda r, t, o: r.params.net_rise(t, o)),
    ModelSensor(key="coast", unit=UnitOfTemperature.CELSIUS, value=lambda r, t, o: r.params.coast),
    ModelSensor(key="idle_drift", unit="°C/h", value=lambda r, t, o: -r.params.loss * (t - o)),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: OndolConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([CostSensor(coordinator, "today"), CostSensor(coordinator, "month")])
    for subentry_id in coordinator.rooms:
        async_add_entities(
            [
                NextActionSensor(coordinator, subentry_id),
                HeatingTodaySensor(coordinator, subentry_id),
                PlannedHeatingSensor(coordinator, subentry_id),
                *(RoomModelSensor(coordinator, subentry_id, description) for description in MODEL_SENSORS),
            ],
            config_subentry_id=subentry_id,
        )


class NextActionSensor(RoomEntity, SensorEntity):
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: OndolCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "next_action")

    @property
    def native_value(self) -> str | None:
        plan = self.room.plan
        return plan.next_text if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room, plan = self.room, self.room.plan
        return {
            "at": plan.next_at.isoformat() if plan and plan.next_at else None,
            "now": plan.text if plan else None,
            "nest_setpoint": plan.setpoint if plan else None,
            "status": room.status,
        }


class HeatingTodaySensor(RoomEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: OndolCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "heating_today")

    @property
    def native_value(self) -> float:
        return round(self.coordinator.hours_today(self.room), 3)


class PlannedHeatingSensor(RoomEntity, SensorEntity):
    """100 while the plan's setpoint is above the room, i.e. the Nest would heat. Like the
    sensor.<room>_nanbang duty sensors, its hourly mean is the share of the hour heated, so the
    advisory plan can be charted next to what actually ran (judged on the real room temperature)."""

    _attr_icon = "mdi:radiator"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OndolCoordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "planned_heating")

    @property
    def native_value(self) -> int | None:
        wants = self.coordinator.wants_heat(self.room)
        return None if wants is None else 100 * wants


class RoomModelSensor(RoomEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: OndolCoordinator, subentry_id: str, description: ModelSensor) -> None:
        super().__init__(coordinator, subentry_id, description.key)
        self._description = description
        self._attr_native_unit_of_measurement = description.unit

    @property
    def native_value(self) -> float | None:
        nest = self.hass.states.get(self.room.nest)
        temp = nest.attributes.get(ATTR_CURRENT_TEMPERATURE) if nest else None
        if temp is None:
            return None
        return round(self._description.value(self.room, float(temp), self.coordinator.outdoor()), 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self.room.params.as_dict(), "samples": len(self.room.samples)}


class CostSensor(OndolEntity, SensorEntity):
    """Heating cost so far, from heating hours at a price per room-hour calibrated on a bill."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "KRW"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: OndolCoordinator, period: str) -> None:
        super().__init__(coordinator, f"cost_{period}")
        self._period = period

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.won_per_hour)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.cost(self._period)

    @property
    def last_reset(self) -> datetime:
        start = dt_util.start_of_local_day()
        return start if self._period == "today" else start.replace(day=1)
