"""Display unit (the weight Home Assistant reports is always grams)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
    async_add_entities([DisplayUnitSelect(entry.runtime_data)])


class DisplayUnitSelect(ScaleEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["g", "ml", "oz", "lb_oz", "fl_oz"]  # keys of protocol.UNIT_CODES

    def __init__(self, scale) -> None:
        super().__init__(scale, "display_unit")

    @property
    def current_option(self) -> str | None:
        return self.scale.state.unit

    async def async_select_option(self, option: str) -> None:
        await self.scale.set_unit(option)
