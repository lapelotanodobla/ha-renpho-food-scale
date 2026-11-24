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
