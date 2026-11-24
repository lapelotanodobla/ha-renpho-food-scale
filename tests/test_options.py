"""Exercise the per-entry options form without a running Home Assistant server."""
import asyncio
import importlib
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import Mock

from test_proxy_diagnosis import paths, radio  # noqa: F401


def test_pick_a_registered_proxy_while_scale_sleeps_and_restore_automatic(radio, paths, monkeypatch):
    _, add, _ = paths
    scanner = add("A")
    passive = add("B", supported=False)

    def module(name, **attrs):
        mod = ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    class Flow:
        def __init_subclass__(cls, domain=None):
            pass

    class Options:
        def async_show_form(self, **kwargs):
            return kwargs
        def async_create_entry(self, **kwargs):
            return kwargs

    # CI intentionally installs only pytest. This schema double records the choices.
    module("voluptuous", Schema=lambda fields: fields, Required=lambda key, default=None: key, In=lambda opts: opts)
    m, bt, _ = radio
    module("homeassistant.components.bluetooth", BluetoothServiceInfoBleak=object, async_discovered_service_info=Mock())
    module("homeassistant.config_entries", ConfigFlow=Flow, ConfigFlowResult=dict, OptionsFlowWithReload=Options)
    module("homeassistant.const", CONF_ADDRESS="address")
    bt.async_current_scanners.return_value = [scanner, passive]
    flow_module = importlib.import_module(m.__package__ + ".config_flow")
    entry = SimpleNamespace(options={"preferred_source": "offline"})
    flow = flow_module.RenphoFoodScaleConfigFlow.async_get_options_flow(entry)
    flow.config_entry, flow.hass = entry, object()
    try:
        form = asyncio.run(flow.async_step_init())
        choices = form["data_schema"]["preferred_source"]
        assert set(choices) == {"automatic", "A", "offline"}
        selected = asyncio.run(flow.async_step_init({"preferred_source": "A"}))
        assert selected["data"] == {"preferred_source": "A"}
        reset = asyncio.run(flow.async_step_init({"preferred_source": "automatic"}))
        assert reset["data"] == {}
    finally:
        sys.modules.pop(flow_module.__name__, None)
