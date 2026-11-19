"""Scale activity: weighings, tares, unit changes, power off."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RenphoFoodScaleConfigEntry
from .entity import ScaleEntity
from .protocol import ScaleEvent


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RenphoFoodScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ScaleActivityEvent(entry.runtime_data)])


class ScaleActivityEvent(ScaleEntity, EventEntity):
    """weighed {grams} · tare {source: scale|app} · unit_changed {unit} · power_off."""

    _attr_event_types = ["weighed", "tare", "unit_changed", "power_off"]
    _needs_connection = False

    def __init__(self, scale) -> None:
        super().__init__(scale, "activity")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.scale.add_event_listener(self._on_event))

    @callback
    def _handle_update(self) -> None:
        """Readings changing is not an event; only decoded ScaleEvents are."""

    @callback
    def _on_event(self, event: ScaleEvent) -> None:
        self._trigger_event(event.type, event.data)
        self.async_write_ha_state()
