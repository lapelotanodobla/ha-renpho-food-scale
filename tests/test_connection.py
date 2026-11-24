"""Exercise reconnection with a controllable radio, including real ES-SNG01 frames."""
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def radio(monkeypatch):
    # Keep these tests usable in HACS's lightweight protocol-test environment.
    def module(name, **attrs):
        m = ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    package = "_renpho_radio_test"
    module(package, __path__=[str(Path(__file__).resolve().parents[1] / "custom_components/renpho_food_scale")])
    module("bleak.backends.device", BLEDevice=object)
    connect = AsyncMock()
    module("bleak_retry_connector", BleakClientWithServiceCache=object, establish_connection=connect)
    bt = Mock()
    bt.async_ble_device_from_address.return_value = object()
    module("homeassistant.components", bluetooth=bt)
    module("homeassistant.core", CALLBACK_TYPE=object, HomeAssistant=object, callback=lambda f: f)
    module("homeassistant.exceptions", HomeAssistantError=RuntimeError)
    manager = importlib.import_module(package + ".scale")
    yield manager, bt, connect
    sys.modules.pop(package + ".scale", None)
    sys.modules.pop(package + ".protocol", None)


class Client:
    def __init__(self):
        self.is_connected = True
        self.notifications = {}
        self.read_gatt_char = AsyncMock(return_value=b'\x64')
        self.on_subscribe = None

    async def start_notify(self, uuid, callback):
        self.notifications[uuid] = callback
        if self.on_subscribe:
            await self.on_subscribe(uuid, callback)

    async def disconnect(self):
        self.is_connected = False
        self.dropped(self)


async def eventually(check):
    async with asyncio.timeout(1):
        while not check():
            await asyncio.sleep(0.002)


def run(radio, scenario):
    manager, bt, connect = radio

    async def simulation():
        loop = asyncio.get_running_loop()
        clients = []
        def factory(*_args, disconnected_callback, **_kwargs):
            client = Client()
            client.dropped = disconnected_callback
            clients.append(client)
            return client
        connect.side_effect = factory
        hass = SimpleNamespace(loop=loop, async_create_background_task=lambda coro, name: loop.create_task(coro, name=name))
        scale = manager.ScaleConnection(hass, "scale-address", "kitchen")
        # Shorten only real production timers, leaving asyncio scheduling and cancellation intact.
        scale.RETRY_INITIAL = 0.015
        scale.RETRY_MAX = 0.03
        scale.CONNECT_TIMEOUT = 0.07
        try:
            await scenario(scale, clients, factory, manager)
        finally:
            await scale.stop()
    asyncio.run(simulation())


def test_retry_without_changed_advert_and_stale_callbacks(radio):
    async def scenario(scale, clients, factory, m):
        scale.start()
        await eventually(lambda: scale.connected)
        old = clients[0]
        await old.disconnect()
        await eventually(lambda: len(clients) == 2 and scale.connected)
        new = clients[-1]
        # Captured stable frame from the real scale, after reconnect.
        packet = bytes.fromhex("55 aa 10 00 04 00 00 00 44 57")
        new.notifications[m.NOTIFY_UUID](None, packet)
        assert scale.state.live_grams == 68
        old.dropped(old)
        old.notifications[m.NOTIFY_UUID](None, bytes.fromhex("55 aa 14 00 04 00 00 00 62 79"))
        await asyncio.sleep(0)
        assert scale.connected and scale.state.live_grams == 68
    run(radio, scenario)


@pytest.mark.parametrize("action", ["release", "unload"])
def test_release_and_unload_during_subscribe(radio, action):
    async def scenario(scale, clients, factory, m):
        entered = asyncio.Event()
        resume = asyncio.Event()
        async def pause(uuid, _callback):
            if uuid == m.NOTIFY_UUID:
                entered.set()
                await resume.wait()
        def create(*args, **kwargs):
            client = factory(*args, **kwargs)
            client.on_subscribe = pause
            return client
        radio[2].side_effect = create
        scale.start()
        await entered.wait()
        if action == "release":
            await scale.set_hold(False)
        else:
            await scale.stop()
        resume.set()
        await asyncio.sleep(0.03)
        assert not scale.connected and not clients[0].is_connected
        assert len(clients) == 1
    run(radio, scenario)


def test_hung_connect_retries_and_first_frame_during_subscribe_is_kept(radio):
    async def scenario(scale, clients, factory, m):
        calls = 0
        async def establish(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            client = factory(*args, **kwargs)
            async def early(uuid, callback):
                if uuid == m.NOTIFY_UUID:
                    callback(None, bytes.fromhex("55 aa 10 00 04 00 00 00 44 57"))
            client.on_subscribe = early
            return client
        radio[2].side_effect = establish
        scale.start()
        await eventually(lambda: scale.connected)
        assert scale.state.stable_grams == 68
    run(radio, scenario)


def test_poweroff_even_if_radio_never_reports_disconnect(radio):
    async def scenario(scale, clients, factory, m):
        scale.start()
        await eventually(lambda: scale.connected)
        old = clients[0]
        frame = bytes.fromhex("55 aa 1b 00 00 1a")
        assert m.parse(frame) is not None
        events = []
        scale.add_event_listener(lambda event: events.append(event.type))
        old.notifications[m.NOTIFY_UUID](None, frame)
        await eventually(lambda: not old.is_connected)
        await eventually(lambda: len(clients) == 2 and scale.connected)
        assert events == ["power_off"]
    run(radio, scenario)


def test_failed_attempt_callback_cannot_end_a_successful_internal_retry(radio):
    async def scenario(scale, clients, factory, m):
        def establish(*args, **kwargs):
            failed = factory(*args, **kwargs)
            failed.is_connected = False
            succeeded = factory(*args, **kwargs)
            asyncio.get_running_loop().call_soon(failed.dropped, failed)
            return succeeded
        radio[2].side_effect = establish
        scale.start()
        await eventually(lambda: scale.connected)
        await asyncio.sleep(0.04)
        assert len(clients) == 2 and clients[-1].is_connected
    run(radio, scenario)


def test_missing_device_and_subscription_failure_then_manual_release(radio):
    async def scenario(scale, clients, factory, m):
        bt = radio[1]
        bt.async_ble_device_from_address.side_effect = [None, object(), object()]
        def establish(*args, **kwargs):
            client = factory(*args, **kwargs)
            if len(clients) == 1:
                client.on_subscribe = AsyncMock(side_effect=OSError("radio lost"))
            return client
        radio[2].side_effect = establish
        scale.start()
        await eventually(lambda: scale.connected)
        assert not clients[0].is_connected and len(clients) == 2
        await scale.set_hold(False)
        scale._on_advertisement(None, None)
        await asyncio.sleep(0.04)
        assert not scale.connected and len(clients) == 2
    run(radio, scenario)


def test_retry_connector_can_reuse_the_same_client(radio):
    async def scenario(scale, clients, factory, m):
        def establish(*args, **kwargs):
            client = factory(*args, **kwargs)
            # bleak-retry-connector can reuse the object across attempts. The
            # earlier callback reaches HA after the next link has been made.
            asyncio.get_running_loop().call_soon(client.dropped, client)
            return client
        radio[2].side_effect = establish
        scale.start()
        await eventually(lambda: scale.connected)
        await asyncio.sleep(0.04)
        assert len(clients) == 1 and clients[0].is_connected
    run(radio, scenario)


def test_release_retries_a_still_connected_client_and_reports_persistent_failure(radio):
    async def scenario(scale, clients, factory, m):
        scale.start()
        await eventually(lambda: scale.connected)
        client = clients[0]
        disconnect = client.disconnect
        client.disconnect = AsyncMock(side_effect=OSError("proxy busy"))
        with pytest.raises(RuntimeError, match="release"):
            await scale.set_hold(False)
        assert client.is_connected and client.disconnect.await_count >= 2
        client.disconnect = disconnect
        await scale.set_hold(False)
        assert not client.is_connected and len(clients) == 1
    run(radio, scenario)
