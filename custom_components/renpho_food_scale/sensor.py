"""Weight and battery sensors."""

from __future__ import annotations

from time import monotonic

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import RenphoFoodScaleConfigEntry
from .entity import ScaleEntity

LIVE_MIN_INTERVAL = 0.25  # the scale sends ~10 readings/s; 4/s is plenty and spares the recorder


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RenphoFoodScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    scale = entry.runtime_data
    async_add_entities(
        [LiveWeightSensor(scale), StableWeightSensor(scale), BatterySensor(scale)]
    )


class LiveWeightSensor(ScaleEntity, SensorEntity):
    """What the display shows right now, in grams."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_suggested_display_precision = 0

    def __init__(self, scale) -> None:
        super().__init__(scale, "weight")
        self._last_write = 0.0
        self._pending: CALLBACK_TYPE | None = None

    @property
    def native_value(self) -> int | None:
        return self.scale.state.live_grams

    @callback
    def _handle_update(self) -> None:
        if self._pending:
            return  # a write is already scheduled and will pick up the latest value
        wait = self._last_write + LIVE_MIN_INTERVAL - monotonic()
        if wait <= 0 or not self.scale.connected:
            self._write_now()
        else:
            self._pending = async_call_later(self.hass, wait, self._write_later)

    @callback
    def _write_later(self, _now) -> None:
        self._pending = None
        self._write_now()

    @callback
    def _write_now(self) -> None:
        self._last_write = monotonic()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._pending:
            self._pending()
            self._pending = None


class StableWeightSensor(ScaleEntity, RestoreSensor):
    """Last settled weight. Kept while the scale is off."""

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_suggested_display_precision = 0
    _needs_connection = False

    def __init__(self, scale) -> None:
        super().__init__(scale, "stable_weight")
        self._restored: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored = last.native_value

    @property
    def native_value(self) -> int | None:
        stable = self.scale.state.stable_grams
        return stable if stable is not None else self._restored


class BatterySensor(ScaleEntity, RestoreSensor):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _needs_connection = False

    def __init__(self, scale) -> None:
        super().__init__(scale, "battery")
        self._restored: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored = last.native_value

    @property
    def native_value(self) -> int | None:
        return self.scale.battery if self.scale.battery is not None else self._restored
