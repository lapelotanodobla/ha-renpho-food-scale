"""A bounded, source-specific capture through Home Assistant's existing ESPHome clients."""

from __future__ import annotations

import asyncio
from time import monotonic

from homeassistant.components import bluetooth

from .protocol import Cmd, NOTIFY_UUID, SERVICE_UUID, parse

SETUP_SECONDS = 10.0
LISTEN_SECONDS = 25.0
ADVERTISE_SECONDS = 2.0


def esphome_scanner_type():
    # ESPHome is optional. Normal scale use never imports its backend.
    try:
        from bleak_esphome.backend.scanner import ESPHomeScanner
    except ImportError:
        return ()
    return ESPHomeScanner


def paths_for(hass, address):
    """A path's BLEDevice and bound connector come from the same registered scanner."""
    kind = esphome_scanner_type()
    return {
        path.scanner.source: path
        for path in bluetooth.async_scanner_devices_by_address(
            hass, address, connectable=True
        )
        if isinstance(path.scanner, kind)
        and path.scanner.connector is not None
        and path.ble_device.address == address
    }


def source_order(path):
    # Prefer a usable slot, then the strongest signal. A newer sighting breaks
    # signal ties; source ID only breaks otherwise identical observations.
    seen = path.scanner.discovered_device_timestamps.get(path.ble_device.address, 0)
    return (
        not path.scanner.connector.can_connect(),
        -path.advertisement.rssi,
        -seen,
        path.scanner.source,
    )


def has_scale_link(paths, address):
    return any(
        slots is not None and address in slots.allocated
        for slots in (p.scanner.get_allocations() for p in paths.values())
    )


def result_for(source):
    return {
        "source": source,
        "subscribed": False,
        "frames": 0,
        "live": 0,
        "stable": 0,
        "stable_flags": 0,
        "hello": 0,
        "info": 0,
        "tare": 0,
        "unit": 0,
        "power_off": 0,
        "invalid": 0,
        "error": None,
        "observation": "not_started",
        "observed_seconds": 0.0,
        "mtu": None,
        "rssi": None,
    }


async def capture(path, result, release):
    """Subscribe only to 1A12. The backend handles the normal CCCD subscription."""
    result["rssi"] = path.advertisement.rssi
    connector = path.scanner.connector
    if not connector.can_connect():
        result["error"] = "no_free_slot"
        return
    client = None
    ended = asyncio.Event()
    stage = "connect"
    collecting = False

    def notify(data):
        if not collecting or ended.is_set():
            return
        result["frames"] += 1
        frame = parse(bytes(data))
        if frame is None:
            result["invalid"] += 1
        elif frame.cmd in (Cmd.LIVE, Cmd.STABLE):
            if len(frame.payload) == 4:
                result["live" if frame.cmd == Cmd.LIVE else "stable"] += 1
            else:
                result["invalid"] += 1
        elif (key := CONTROL_FRAMES.get(frame.cmd)) is not None:
            result[key] += 1

    try:
        async with asyncio.timeout(SETUP_SECONDS):
            # The public Bleak client wrapper chooses a source again. This bound factory
            # uses HA's already-subscribed ESPHome API connection for this one path.
            client = connector.client(
                path.ble_device,
                disconnected_callback=ended.set,
                timeout=SETUP_SECONDS,
                services=None,
            )
            await client.connect(pair=False, dangerous_use_bleak_cache=False)
            result["mtu"] = client.mtu_size
            char = client.services.get_characteristic(NOTIFY_UUID)
            if (
                char is None
                or char.service_uuid.lower() != SERVICE_UUID
                or "notify" not in char.properties
            ):
                result["error"] = "weight_notify_unavailable"
                return
            stage = "subscribe"
            collecting = True
            await client.start_notify(char, notify)
            result["subscribed"] = True
        started = monotonic()
        try:
            await asyncio.wait_for(ended.wait(), LISTEN_SECONDS)
            result["observation"] = "early_disconnect"
            result["error"] = "disconnected"
        except TimeoutError:
            result["observation"] = "full_window"
        finally:
            result["observed_seconds"] = round(monotonic() - started, 3)
    except TimeoutError:
        result["error"] = stage + "_timeout"
    except Exception as exc:
        # Backend exception messages can contain addresses and raw packet data.
        result["error"] = stage + "_" + type(exc).__name__
    finally:
        collecting = False
        if client is not None and client.is_connected:
            await release(client)


CONTROL_FRAMES = {
    Cmd.STABLE_FLAG: "stable_flags",
    Cmd.HELLO: "hello",
    Cmd.INFO: "info",
    Cmd.TARE: "tare",
    Cmd.UNIT: "unit",
    Cmd.POWER_OFF: "power_off",
}
