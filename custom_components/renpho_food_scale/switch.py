"""Keep the Bluetooth connection, or release the scale for the phone / Renpho app."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RenphoFoodScaleConfigEntry
from .entity import ScaleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RenphoFoodScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([KeepConnectedSwitch(entry.runtime_data)])


class KeepConnectedSwitch(ScaleEntity, SwitchEntity, RestoreEntity):
    """Off = Home Assistant lets go of the scale so another device can connect."""

    _attr_entity_category = EntityCategory.CONFIG
    _needs_connection = False

    def __init__(self, scale) -> None:
        super().__init__(scale, "keep_connected")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_OFF:
            await self.scale.set_hold(False)

    @property
    def is_on(self) -> bool:
        return self.scale.hold

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.scale.set_hold(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.scale.set_hold(False)
