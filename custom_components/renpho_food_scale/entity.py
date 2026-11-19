"""Base entity for the Renpho food scale."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER, MODEL
from .scale import ScaleConnection


class ScaleEntity(Entity):
    """Pushes state whenever the connection manager reports a change."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _needs_connection = True  # unavailable while the scale is off / not connected

    def __init__(self, scale: ScaleConnection, key: str) -> None:
        self.scale = scale
        self._attr_translation_key = key
        self._attr_unique_id = f"{scale.address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, scale.address)},
            identifiers={(DOMAIN, scale.address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=scale.name,
        )

    @property
    def available(self) -> bool:
        return self.scale.connected or not self._needs_connection

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.scale.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
