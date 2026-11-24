"""Exercise the explicitly invoked diagnostic with HA-owned per-source GATT backends."""

import asyncio
import importlib
import importlib.machinery
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_connection import eventually, radio, run  # noqa: F401


@pytest.fixture
def paths(radio, monkeypatch):
    m, bluetooth, _ = radio
    sys.modules[m.__package__].__spec__ = importlib.machinery.ModuleSpec(
        m.__package__, None, is_package=True
    )
    diag = importlib.import_module(m.__package__ + ".proxy_diagnosis")
    monkeypatch.setattr(diag, "ADVERTISE_SECONDS", 0)
    monkeypatch.setattr(diag, "bluetooth", bluetooth)
    clients = []

    class TestScanner:
        pass

    monkeypatch.setattr(diag, "esphome_scanner_type", lambda: TestScanner)
    monkeypatch.setattr(diag, "LISTEN_SECONDS", 0.01)
    monkeypatch.setattr(diag, "SETUP_SECONDS", 0.1)
    observed = []

    def add(source, *, free=True, supported=True, rssi=-60, seen=1):
        scanner = TestScanner() if supported else SimpleNamespace()
        scanner.source, scanner.name = source, "proxy " + source
        scanner.discovered_device_timestamps = {"scale-address": seen}
        scanner.get_allocations = lambda: SimpleNamespace(allocated=[])
        device = SimpleNamespace(address="scale-address", details={"source": source})

        def connect_backend(given, *, disconnected_callback, timeout, services):
            assert given is device
            char = SimpleNamespace(
                uuid=m.NOTIFY_UUID,
                service_uuid=diag.SERVICE_UUID,
                properties=["notify"],
            )
            client = SimpleNamespace(is_connected=False, mtu_size=64)

            async def connect(*, pair, dangerous_use_bleak_cache):
                assert not pair and not dangerous_use_bleak_cache
                assert not any(c.is_connected for c in clients)
                client.is_connected = True

            async def subscribe(characteristic, cb):
                client.notify = cb
                assert characteristic is char
                cb(bytes.fromhex("55 aa 14 00 04 00 00 00 62 79"))
                cb(bytes.fromhex("55 aa 10 00 04 00 00 00 44 57"))
                cb(b"invalid")

            async def disconnect():
                client.is_connected = False
                disconnected_callback()

            client.connect, client.start_notify, client.disconnect = (
                connect,
                subscribe,
                disconnect,
            )
            client.services = SimpleNamespace(
                get_characteristic=lambda uuid: char if uuid == m.NOTIFY_UUID else None
            )
            clients.append(client)
            return client

        scanner.connector = SimpleNamespace(
            can_connect=lambda: free, client=connect_backend
        )
        observed.append(
            SimpleNamespace(
                scanner=scanner,
                ble_device=device,
                advertisement=SimpleNamespace(rssi=rssi),
            )
        )
        return scanner

    def discovered(hass, address, *, connectable):
        assert address == "scale-address" and connectable
        return list(observed)

    bluetooth.async_scanner_devices_by_address.side_effect = discovered
    return diag, add, clients


@pytest.mark.parametrize("hold", [True, False])
def test_uses_only_named_free_esphome_paths_and_restores_original_hold(
    radio, paths, hold
):
    diag, add, clients = paths
    add("B")
    add("C", free=False)
    add("D", supported=False)
    add("A")

    async def scenario(scale, normal, factory, m):
        if not hold:
            await scale.set_hold(False)
        scale.start()
        if hold:
            await eventually(lambda: scale.connected)
        res = await scale.diagnose_proxies(["B", "A"])
        for row in res:
            assert (
                row["subscribed"]
                and row["live"] == row["stable"] == row["invalid"] == 1
            )
            assert row["frames"] == 3 and row["error"] is None
            assert (
                row["observation"] == "full_window"
                and row["observed_seconds"] > 0
                and row["mtu"] == 64
            )
        assert [x["source"] for x in res] == ["B", "A"]
        assert all(not c.is_connected for c in clients)
        clients[0].notify(b"late old notification")
        assert res[0]["frames"] == 3
        assert scale.hold is hold
        if hold:
            await eventually(lambda: scale.connected)
        else:
            assert scale._worker is None
        res = await scale.diagnose_proxies(["C", "D"])
        assert [x["error"] for x in res] == ["no_free_slot", "not_seen_on_esphome"]
        assert len(clients) == 2

    run(radio, scenario)


def test_cancellation_and_notify_failure_release_the_target_and_restore_hold(
    radio, paths
):
    diag, add, clients = paths
    scanner = add("A")
    original = scanner.connector.client
    entered = asyncio.Event()

    def backend(*args, **kwargs):
        c = original(*args, **kwargs)

        async def notify(char, callback):
            entered.set()
            await asyncio.Event().wait()

        c.start_notify = notify
        return c

    scanner.connector.client = backend

    async def scenario(scale, normal, factory, m):
        scale.start()
        await eventually(lambda: scale.connected)
        task = asyncio.create_task(scale.diagnose_proxies(["A"]))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not clients[0].is_connected and scale.hold
        await eventually(lambda: scale.connected)
        res = await scale.diagnose_proxies(["A"])
        assert res[0]["error"] == "subscribe_timeout" and not clients[-1].is_connected
        await eventually(lambda: scale.connected)

    run(radio, scenario)


def test_default_selection_prefers_free_rssi_then_recency_and_rechecks_scale_slot(
    radio, paths
):
    _, add, clients = paths
    add("C", rssi=-75)
    add("B", rssi=-40, seen=20)
    scanner = add("A", rssi=-40, seen=10)
    add("busy", free=False, rssi=-5)

    async def scenario(scale, normal, factory, m):
        await scale.set_hold(False)
        scale.start()
        result = await scale.diagnose_proxies()
        assert [row["source"] for row in result] == ["B", "A"]
        scanner.get_allocations = lambda: SimpleNamespace(allocated=["scale-address"])
        result = await scale.diagnose_proxies(["B"])
        assert result[0]["error"] == "scale_slot_still_held" and len(clients) == 2
        assert not scale.hold

    run(radio, scenario)


def test_early_disconnect_and_failed_close_do_not_silently_continue(radio, paths):
    _, add, clients = paths
    scanner = add("A")
    add("B")
    original = scanner.connector.client

    def dropped(*args, **kwargs):
        client = original(*args, **kwargs)

        async def subscribe(char, callback):
            client.is_connected = False
            kwargs["disconnected_callback"]()

        client.start_notify = subscribe
        return client

    scanner.connector.client = dropped

    async def scenario(scale, normal, factory, m):
        await scale.set_hold(False)
        scale.start()
        result = await scale.diagnose_proxies(["A"])
        assert result[0]["observation"] == "early_disconnect"

        def stuck(*args, **kwargs):
            client = original(*args, **kwargs)
            client.disconnect = AsyncMock(side_effect=OSError("proxy busy"))
            return client

        scanner.connector.client = stuck
        result = await scale.diagnose_proxies(["A", "B"])
        assert [row["error"] for row in result] == [
            "release_failed",
            "previous_release_failed",
        ]
        assert not scale.hold and len(clients) == 2 and clients[-1].is_connected
        clients[-1].is_connected = False

    run(radio, scenario)


def test_preferred_normal_path_keeps_early_weights_and_commands_and_releases(radio, paths):
    _, add, remote = paths
    add("wrong", rssi=-30)
    scanner = add("chosen", rssi=-90)
    original = scanner.connector.client

    def make(*args, **kwargs):
        c = original(*args, **kwargs)
        lookup = c.services.get_characteristic
        write = SimpleNamespace(uuid=radio[0].WRITE_UUID)
        c.services.get_characteristic = lambda uuid: write if uuid == write.uuid else lookup(uuid)
        c.write_gatt_char = AsyncMock()
        return c

    scanner.connector.client = make

    async def scenario(scale, normal, factory, m):
        scale.preferred_source = "chosen"
        events = []
        scale.add_event_listener(lambda e: events.append(e.type))
        scale.start()
        await eventually(lambda: scale.connected)
        assert not normal and len(remote) == 1
        assert scale.state.stable_grams == 68 and "weighed" in events
        await scale.tare()
        args, kwargs = remote[0].write_gatt_char.await_args
        assert args[0].uuid == m.WRITE_UUID and args[1] == m.tare_command()
        await scale.set_hold(False)
        assert not remote[0].is_connected
        remote[0].notify(bytes.fromhex("55 aa 10 00 04 00 00 00 44 57"))
        assert events.count("weighed") == 1
        await scale.set_hold(True)
        await eventually(lambda: len(remote) == 2 and scale.connected)
    run(radio, scenario)


@pytest.mark.parametrize("fault", ["unseen", "busy", "subscribe_failure"])
def test_fallback_then_uses_recovered_preferred_path(radio, paths, fault):
    _, add, remote = paths
    scanner = None
    if fault != "unseen":
        scanner = add("chosen", free=fault != "busy")
    if fault == "subscribe_failure":
        original = scanner.connector.client

        def broken(*args, **kwargs):
            c = original(*args, **kwargs)
            c.start_notify = AsyncMock(side_effect=OSError("subscription failed"))
            return c

        scanner.connector.client = broken

    async def scenario(scale, normal, factory, m):
        scale.preferred_source = "chosen"
        scale.PREFERRED_RETRY = 0.03
        scale.SESSION_POLL = 0.01
        scale.start()
        await eventually(lambda: scale.connected and normal)
        if fault == "unseen":
            add("chosen")
        elif fault == "busy":
            scanner.connector.can_connect = lambda: True
        else:
            assert remote and not remote[0].is_connected
            scanner.connector.client = original
        await eventually(lambda: scale.state.stable_grams == 68)
        assert not normal[0].is_connected and scale.connected
    run(radio, scenario)


def test_silent_preferred_can_yield_to_a_working_fallback(radio, paths):
    _, add, remote = paths
    preferred = add("chosen")
    add("other")
    original = preferred.connector.client
    def silent(*args, **kwargs):
        c = original(*args, **kwargs)
        c.start_notify = AsyncMock()
        return c
    preferred.connector.client = silent

    async def scenario(scale, normal, factory, m):
        scale.preferred_source = "chosen"
        scale.PREFERRED_RETRY = 0.03
        scale.SESSION_POLL = 0.01
        scale.start()
        await eventually(lambda: scale.connected and remote)
        # The HA default may choose the same strongest silent source again.
        # Explicitly try the available different source before generic routing.
        await eventually(lambda: scale.state.stable_grams == 68)
        await asyncio.sleep(0.08)
        assert scale.connected and len(remote) == 2 and not normal
    run(radio, scenario)


def test_release_during_preferred_setup_keeps_slot_and_next_wake_consistent(radio, paths):
    _, add, remote = paths
    preferred = add("chosen")
    original = preferred.connector.client
    entered = asyncio.Event()

    def slow(*args, **kwargs):
        client = original(*args, **kwargs)
        connect = client.connect
        async def connecting(**opts):
            await connect(**opts)
            entered.set()
            await asyncio.Event().wait()
        client.connect = connecting
        return client

    preferred.connector.client = slow

    async def scenario(scale, normal, factory, m):
        scale.preferred_source = "chosen"
        scale.start()
        await asyncio.wait_for(entered.wait(), 1)
        await scale.set_hold(False)
        assert not remote[0].is_connected and not scale.connected
        preferred.connector.client = original
        scale._on_advertisement(None, None)
        await asyncio.sleep(0.025)
        assert len(remote) == 1 and not normal
        await scale.set_hold(True)
        await eventually(lambda: scale.state.stable_grams == 68)
        assert len(remote) == 2 and not normal
    run(radio, scenario)
