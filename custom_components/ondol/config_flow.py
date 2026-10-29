from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from . import schedule
from .const import (
    CONF_CLIMATE,
    CONF_DAYCARE,
    CONF_FOLLOWS_DAYCARE,
    CONF_NAME,
    CONF_OCCUPANCY,
    CONF_OUTDOOR,
    CONF_PRESENCE,
    CONF_SCHEDULE,
    CONF_TOLERANCE,
    CONF_WON_PER_HOUR,
    DEFAULT_PRESETS,
    DEFAULT_SCHEDULE,
    DEFAULT_TOLERANCE,
    DOMAIN,
    MAX_SETPOINT,
    MIN_SETPOINT,
    PRESETS,
    SUBENTRY_ROOM,
)

HUB_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PRESENCE): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional(CONF_DAYCARE): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Required(CONF_OUTDOOR): EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature")),
        vol.Required(CONF_TOLERANCE): NumberSelector(
            NumberSelectorConfig(min=0.2, max=1.5, step=0.1, unit_of_measurement="°C", mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_WON_PER_HOUR): NumberSelector(
            NumberSelectorConfig(min=0, max=100000, step=1, unit_of_measurement="₩/h", mode=NumberSelectorMode.BOX)
        ),
    }
)
HUB_DEFAULTS = {CONF_TOLERANCE: DEFAULT_TOLERANCE, CONF_WON_PER_HOUR: 0}

_TEMPERATURE = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SETPOINT, max=MAX_SETPOINT, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.BOX
    )
)
ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIMATE): EntitySelector(EntitySelectorConfig(domain="climate")),
        vol.Optional(CONF_NAME): TextSelector(),
        **{vol.Required(preset): _TEMPERATURE for preset in PRESETS},
        vol.Required(CONF_FOLLOWS_DAYCARE): BooleanSelector(),
        vol.Optional(CONF_OCCUPANCY): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Required(CONF_SCHEDULE): TextSelector(TextSelectorConfig(multiline=True)),
    }
)
ROOM_DEFAULTS = {**DEFAULT_PRESETS, CONF_FOLLOWS_DAYCARE: False, CONF_SCHEDULE: DEFAULT_SCHEDULE}


class OndolConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OndolOptionsFlow:
        return OndolOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ROOM: RoomFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="온돌 난방", data={}, options=user_input)
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(HUB_SCHEMA, HUB_DEFAULTS)
        )


class OndolOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(HUB_SCHEMA, {**HUB_DEFAULTS, **self.config_entry.options}),
        )


class RoomFlow(ConfigSubentryFlow):
    """One room: its thermostat, preset temperatures and weekly schedule."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_room("user", user_input, ROOM_DEFAULTS)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._async_room("reconfigure", user_input, dict(self._get_reconfigure_subentry().data))

    async def _async_room(
        self, step_id: str, user_input: dict[str, Any] | None, suggested: dict[str, Any]
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            climate = user_input[CONF_CLIMATE]
            others = [
                s
                for s in self._get_entry().subentries.values()
                if step_id == "user" or s.subentry_id != self._get_reconfigure_subentry().subentry_id
            ]
            try:
                schedule.parse(user_input[CONF_SCHEDULE])
            except schedule.ScheduleError as err:
                errors[CONF_SCHEDULE] = "invalid_schedule"
                placeholders["error"] = str(err)
            if any(s.unique_id == climate for s in others):
                errors[CONF_CLIMATE] = "already_configured"
            if not errors:
                state = self.hass.states.get(climate)
                name = (user_input.get(CONF_NAME) or (state.name if state else climate)).strip()
                data = {**user_input, CONF_NAME: name}
                if step_id == "user":
                    return self.async_create_entry(title=name, data=data, unique_id=climate)
                return self.async_update_and_abort(
                    self._get_entry(), self._get_reconfigure_subentry(), title=name, data=data, unique_id=climate
                )
            suggested = user_input
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(ROOM_SCHEMA, suggested),
            errors=errors,
            description_placeholders=placeholders,
        )
