"""Renpho food scale (ES-SNG01) over Bluetooth."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .scale import ScaleConnection

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type RenphoFoodScaleConfigEntry = ConfigEntry[ScaleConnection]


async def async_setup_entry(hass: HomeAssistant, entry: RenphoFoodScaleConfigEntry) -> bool:
    scale = ScaleConnection(hass, entry.data[CONF_ADDRESS], entry.title)
    entry.runtime_data = scale
    # Platforms first: the switch restores "hold connection" before the first connect.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    scale.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RenphoFoodScaleConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.stop()
    return unloaded
