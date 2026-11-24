"""Config flow: Bluetooth discovery, or pick a nearby scale."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import CONF_PREFERRED_SOURCE, DOMAIN, MODEL
from .proxy_diagnosis import esphome_scanner_type


MANUFACTURER_ID = 0x1A10  # shared with Renpho bathroom scales, which start 00 04
DATA_PREFIX = b"\x00\x0d"


def _is_scale(info: BluetoothServiceInfoBleak) -> bool:
    """By name (active scan) or by manufacturer data (also seen by passive proxies)."""
    data = info.manufacturer_data.get(MANUFACTURER_ID, b"")
    return (info.name or "").startswith(MODEL) or data.startswith(DATA_PREFIX)


class RenphoFoodScaleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return RenphoOptionsFlow()

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._found: dict[str, str] = {}

    def _title(self, info: BluetoothServiceInfoBleak) -> str:
        return f"Renpho food scale {info.address[-5:]}"

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": self._title(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        title = self._title(self._discovery)
        if user_input is not None:
            return self.async_create_entry(
                title=title, data={CONF_ADDRESS: self._discovery.address}
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm", description_placeholders={"name": title}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._found[address], data={CONF_ADDRESS: address}
            )
        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if _is_scale(info) and info.address not in current:
                self._found[info.address] = self._title(info)
        if not self._found:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._found)}),
        )


class RenphoOptionsFlow(OptionsFlowWithReload):
    """Prefer a particular HA-owned ESPHome adapter only for this entry."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        automatic = "automatic"
        if user_input is not None:
            options = dict(self.config_entry.options)
            source = user_input[CONF_PREFERRED_SOURCE]
            if source == automatic:
                options.pop(CONF_PREFERRED_SOURCE, None)
            else:
                options[CONF_PREFERRED_SOURCE] = source
            return self.async_create_entry(title="", data=options)

        current = self.config_entry.options.get(CONF_PREFERRED_SOURCE, automatic)
        choices = {automatic: "Automatic (Home Assistant)"}
        # Scanners are registered even while the scale is asleep. Retain an
        # existing preference if its proxy is temporarily offline.
        for scanner in bluetooth.async_current_scanners(self.hass):
            if isinstance(scanner, esphome_scanner_type()) and scanner.connector is not None:
                choices[scanner.source] = f"{scanner.name} ({scanner.source})"
        choices.setdefault(current, current)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_PREFERRED_SOURCE, default=current): vol.In(choices)
            }),
        )
