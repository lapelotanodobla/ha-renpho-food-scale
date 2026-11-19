"""Whether Home Assistant currently holds the scale's Bluetooth connection."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RenphoFoodScaleConfigEntry
from .entity import ScaleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RenphoFoodScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ConnectedSensor(entry.runtime_data)])


class ConnectedSensor(ScaleEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _needs_connection = False

    def __init__(self, scale) -> None:
        super().__init__(scale, "connected")

    @property
    def is_on(self) -> bool:
        return self.scale.connected
