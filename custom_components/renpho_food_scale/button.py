"""Tare button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RenphoFoodScaleConfigEntry
from .entity import ScaleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RenphoFoodScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([TareButton(entry.runtime_data)])


class TareButton(ScaleEntity, ButtonEntity):
    def __init__(self, scale) -> None:
        super().__init__(scale, "tare")

    async def async_press(self) -> None:
        await self.scale.tare()
