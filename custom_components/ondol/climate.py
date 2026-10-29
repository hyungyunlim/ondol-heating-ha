from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OndolConfigEntry
from .const import MAX_SETPOINT, MIN_SETPOINT, PRESETS
from .coordinator import OFF
from .entity import RoomEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: OndolConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    for subentry_id in coordinator.rooms:
        async_add_entities([OndolClimate(coordinator, subentry_id)], config_subentry_id=subentry_id)


class OndolClimate(RoomEntity, ClimateEntity):
    """The room as the ondol controller sees it, wrapping its Nest."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = MIN_SETPOINT
    _attr_max_temp = MAX_SETPOINT
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = list(PRESETS)
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator, subentry_id: str) -> None:
        super().__init__(coordinator, subentry_id, "room")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.room.entity_id = self.entity_id

    def _nest(self, attribute: str) -> Any:
        state = self.hass.states.get(self.room.nest)
        return state.attributes.get(attribute) if state is not None and state.state not in OFF else None

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self.room.nest)
        return super().available and state is not None and state.state not in OFF

    @property
    def current_temperature(self) -> float | None:
        return self._nest(ATTR_CURRENT_TEMPERATURE)

    @property
    def current_humidity(self) -> float | None:
        return self._nest(ATTR_CURRENT_HUMIDITY)

    @property
    def target_temperature(self) -> float | None:
        plan = self.room.plan
        return plan.target if plan else self._nest(ATTR_TEMPERATURE)

    @property
    def hvac_mode(self) -> HVACMode:
        state = self.hass.states.get(self.room.nest)
        return HVACMode.HEAT if state is not None and state.state == HVACMode.HEAT else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        action = self._nest(ATTR_HVAC_ACTION)
        return HVACAction(action) if action in HVACAction._value2member_map_ else None

    @property
    def preset_mode(self) -> str | None:
        plan = self.room.plan
        return plan.preset if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room, plan = self.room, self.room.plan
        return {
            "status": room.status,
            "control": self.coordinator.control,
            "plan": plan.text if plan else None,
            "nest_setpoint": plan.setpoint if plan else None,
            "next_action": plan.next_text if plan else None,
            "hold_until": plan.hold_until.isoformat() if plan and plan.hold_until else None,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_user_target(self.subentry_id, temperature=temperature)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.coordinator.async_user_target(self.subentry_id, preset=preset_mode)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: self.room.nest, "hvac_mode": hvac_mode},
            blocking=True,
        )
