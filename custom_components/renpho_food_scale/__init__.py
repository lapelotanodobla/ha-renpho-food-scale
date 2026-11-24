"""Renpho food scale (ES-SNG01) over Bluetooth."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the explicit administrator-only capture action."""
    async def diagnose(call: ServiceCall) -> ServiceResponse:
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError("Select a loaded Renpho food scale")
        rows = await entry.runtime_data.diagnose_proxies(call.data.get("sources"))
        return {"sources": rows, "error": None if rows else "not_seen_on_esphome"}

    async_register_admin_service(
        hass, DOMAIN, "diagnose_proxies", diagnose,
        schema=vol.Schema({
            vol.Required("config_entry_id"): str,
            vol.Optional("sources"): vol.All([vol.All(str, vol.Length(min=1))], vol.Length(min=1, max=2)),
        }),
        supports_response=SupportsResponse.ONLY,
    )
    return True


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
