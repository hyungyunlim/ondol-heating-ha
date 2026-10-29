from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_ENTITY_ID, LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ONDOL


@callback
def async_describe_events(hass: HomeAssistant, async_describe_event: Callable) -> None:
    @callback
    def describe(event: Event) -> dict[str, str | None]:
        return {
            LOGBOOK_ENTRY_NAME: event.data["name"],
            LOGBOOK_ENTRY_MESSAGE: event.data["message"],
            LOGBOOK_ENTRY_ENTITY_ID: event.data.get("entity_id"),
        }

    async_describe_event(DOMAIN, EVENT_ONDOL, describe)
