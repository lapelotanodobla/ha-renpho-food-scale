"""Connection manager: keeps a GATT connection to the scale whenever it is awake."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .protocol import (
    BATTERY_UUID,
    NOTIFY_UUID,
    WRITE_UUID,
    Cmd,
    ScaleEvent,
    ScaleState,
    parse,
    set_unit_command,
    tare_command,
)
from .proxy_client import ProxyClient

_LOGGER = logging.getLogger(__name__)


class ScaleConnection:
    """Connects when the scale advertises (it only does so while switched on).

    Readings live in ``state``; listeners are told when anything changes and
    event listeners get the ScaleEvents decoded from each frame.
    """

    RETRY_INITIAL = 2.0
    RETRY_MAX = 60.0
    CONNECT_TIMEOUT = 30.0
    IO_TIMEOUT = 8.0
    PREFERRED_RETRY = 60.0
    SESSION_POLL = 15.0

    def __init__(self, hass: HomeAssistant, address: str, name: str, preferred_source: str = "") -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.preferred_source = preferred_source
        self._retry_source_at: dict[str, float] = {}
        self.state = ScaleState()
        self.battery: int | None = None
        self.connected = False
        self.hold = True  # False releases the scale for the phone / Renpho app
        self._client: BleakClientWithServiceCache | ProxyClient | None = None
        self._closing_client: BleakClientWithServiceCache | ProxyClient | None = None
        self._worker: asyncio.Task | None = None
        self._advertised = asyncio.Event()
        self._control_lock = asyncio.Lock()
        self._listeners: list[CALLBACK_TYPE] = []
        self._event_listeners: list[Callable[[ScaleEvent], None]] = []
        self._unsub_adv: CALLBACK_TYPE | None = None

    # ---- listeners -------------------------------------------------------

    @callback
    def add_listener(self, cb: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def add_event_listener(self, cb: Callable[[ScaleEvent], None]) -> CALLBACK_TYPE:
        self._event_listeners.append(cb)
        return lambda: self._event_listeners.remove(cb)

    @callback
    def _changed(self) -> None:
        for cb in list(self._listeners):
            cb()

    # ---- lifecycle -------------------------------------------------------

    @callback
    def start(self) -> None:
        self._unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        if self.hold:
            self._start_worker()

    async def stop(self) -> None:
        async with self._control_lock:
            if self._unsub_adv is not None:
                self._unsub_adv()
                self._unsub_adv = None
            await self._stop_worker()

    @callback
    def _on_advertisement(
        self, _info: bluetooth.BluetoothServiceInfoBleak, _change: bluetooth.BluetoothChange
    ) -> None:
        self._advertised.set()

    def _start_worker(self) -> None:
        if self._unsub_adv is not None and self._worker is None:
            self._worker = self.hass.async_create_background_task(
                self._run(), f"renpho_food_scale connect {self.address}"
            )

    async def _stop_worker(self) -> None:
        task = self._worker
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self._worker = None
        if not await self._close_pending():
            raise HomeAssistantError("Could not release the scale's Bluetooth connection. Try again.")

    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    def _proxy_path(self, *, preferred: bool):
        if not self.preferred_source:
            return None
        from .proxy_diagnosis import paths_for, source_order

        now = self.hass.loop.time()
        eligible = (
            path for source, path in paths_for(self.hass, self.address).items()
            if (source == self.preferred_source) == preferred
            and self._retry_source_at.get(source, 0) <= now
            and path.scanner.connector.can_connect()
        )
        return min(eligible, key=source_order, default=None)

    async def _run(self) -> None:
        delay = self.RETRY_INITIAL
        while True:
            self._advertised.clear()
            try:
                if await self._close_pending():
                    path = self._proxy_path(preferred=True) or self._proxy_path(preferred=False)
                    device = path.ble_device if path else self._ble_device()
                    if device is not None and await self._session(device, path):
                        delay = self.RETRY_INITIAL
            except Exception as err:
                _LOGGER.debug("%s: connection attempt ended: %s", self.address, err)
            # HA can coalesce identical adverts after wake. A timer retries even
            # when the next advert never reaches our callback. A fresh advert
            # shortens the backoff; the minimum delay also bounds radio churn.
            await asyncio.sleep(self.RETRY_INITIAL)
            if delay > self.RETRY_INITIAL:
                try:
                    async with asyncio.timeout(delay - self.RETRY_INITIAL):
                        await self._advertised.wait()
                except TimeoutError:
                    pass
            delay = min(delay * 2, self.RETRY_MAX)

    async def _session(self, device: BLEDevice, path=None) -> bool:
        ended = asyncio.Event()
        client = None
        received = {Cmd.LIVE: 0, Cmd.STABLE: 0}
        rejected = 0

        def mark_lost(dropped):
            if dropped is client and not dropped.is_connected:
                ended.set()

        def on_disconnect(dropped):
            # Each callback closes only its own session, even if queued by a
            # proxy that has already released the old client.
            self.hass.loop.call_soon_threadsafe(mark_lost, dropped)

        def on_notify(_char, data):
            nonlocal rejected
            if self._client is not client or ended.is_set():
                return
            frame = parse(bytes(data))
            if frame is None:
                rejected += 1
                if rejected == 1:
                    _LOGGER.debug("Invalid scale notification (%d bytes)", len(data))
                return
            if frame.cmd in received:
                if len(frame.payload) == 4:
                    received[frame.cmd] += 1
                else:
                    rejected += 1
                    if rejected == 1:
                        _LOGGER.debug("Unexpected weight payload length %d", len(frame.payload))
            events = self.state.feed(frame)
            self._changed()
            for event in events:
                for listener in list(self._event_listeners):
                    listener(event)
            if frame.cmd == Cmd.POWER_OFF:
                ended.set()

        def on_battery(_char, data):
            if self._client is client and not ended.is_set() and data:
                self.battery = data[0]
                self._changed()

        try:
            async with asyncio.timeout(self.CONNECT_TIMEOUT):
                if path is None:
                    client = await establish_connection(
                        BleakClientWithServiceCache, device, self.name,
                        disconnected_callback=on_disconnect,
                        ble_device_callback=self._ble_device, max_attempts=3,
                    )
                else:
                    client = ProxyClient(path, on_disconnect, self.CONNECT_TIMEOUT)
                    await client.connect()
                if ended.is_set() or not client.is_connected:
                    return False
                # Some adapters deliver the first weight before start_notify returns.
                self._client = client
                await client.start_notify(NOTIFY_UUID, on_notify)
            if ended.is_set() or not client.is_connected:
                return False
            self.connected = True
            self._changed()
            _LOGGER.debug("%s: connected; weight notifications subscribed", self.address)
            try:
                async with asyncio.timeout(self.IO_TIMEOUT):
                    battery = await client.read_gatt_char(BATTERY_UUID)
                    on_battery(None, battery)
                    if not ended.is_set():
                        await client.start_notify(BATTERY_UUID, on_battery)
            except Exception as err:
                _LOGGER.debug("%s: battery unavailable: %s", self.address, err)
            subscribed_at = self.hass.loop.time()
            while client.is_connected and not ended.is_set():
                try:
                    async with asyncio.timeout(self.SESSION_POLL):
                        await ended.wait()
                except TimeoutError:
                    # Allow a full silent window before switching. A route that
                    # delivered a weight keeps the session until disconnection.
                    if (
                        self.preferred_source
                        and not any(received.values())
                        and self.hass.loop.time() - subscribed_at >= self.PREFERRED_RETRY
                    ):
                        on_preferred = path is not None and path.scanner.source == self.preferred_source
                        if self._proxy_path(preferred=not on_preferred) is not None:
                            break
            return True
        finally:
            if path is not None and not any(received.values()) and not asyncio.current_task().cancelling():
                # Try another observed source on the next attempt. When none is
                # available, HA's normal routing still covers other adapters.
                self._retry_source_at[path.scanner.source] = self.hass.loop.time() + self.PREFERRED_RETRY
            ended.set()
            if client is not None:
                self._client = None
                self.connected = False
                self.state.disconnected()
                self._changed()
                if client.is_connected:
                    # A release during an already-running power-off cleanup must
                    # still return the physical connection slot to the proxy.
                    await self._release_client(client)
                _LOGGER.debug(
                    "%s: session ended: live=%d stable=%d rejected=%d",
                    self.address, received[Cmd.LIVE], received[Cmd.STABLE], rejected,
                )

    async def _close_pending(self) -> bool:
        client = self._closing_client
        if client is None:
            return True
        for _ in range(2):
            if not client.is_connected:
                break
            try:
                async with asyncio.timeout(self.IO_TIMEOUT):
                    await client.disconnect()
            except Exception as err:
                _LOGGER.debug("%s: disconnect error: %s", self.address, err)
        if client.is_connected:
            return False
        self._closing_client = None
        return True

    # ---- commands --------------------------------------------------------

    async def _write(self, frame: bytes) -> None:
        if self._client is None or not self.connected:
            raise HomeAssistantError("The scale is not connected (switch it on)")
        await self._client.write_gatt_char(WRITE_UUID, frame, response=True)

    async def tare(self) -> None:
        await self._write(tare_command())

    async def set_unit(self, unit: str) -> None:
        await self._write(set_unit_command(unit))

    async def diagnose_proxies(self, sources: list[str] | None = None) -> list[dict]:
        """Temporarily lend only this scale's link to a source-specific diagnostic."""
        from . import proxy_diagnosis as probe

        if self._control_lock.locked():
            raise HomeAssistantError("Another scale operation is in progress")
        async with self._control_lock:
            if self._unsub_adv is None:
                raise HomeAssistantError("The scale is not loaded")
            was_held = self.hold
            try:
                await self._stop_worker()
                if was_held:
                    # An exclusive device advertises again only after release.
                    await asyncio.sleep(probe.ADVERTISE_SECONDS)
                paths = probe.paths_for(self.hass, self.address)
                requested = list(dict.fromkeys(sources)) if sources else sorted(
                    paths, key=lambda key: probe.source_order(paths[key])
                )[:2]
                results = []
                for source in requested[:2]:
                    row = probe.result_for(source)
                    results.append(row)
                    paths = probe.paths_for(self.hass, self.address)
                    if probe.has_scale_link(paths, self.address):
                        row["error"] = "scale_slot_still_held"
                        continue
                    path = paths.get(source)
                    if path is None:
                        row["error"] = "not_seen_on_esphome"
                        continue
                    await probe.capture(path, row, self._release_client)
                    if self._closing_client is not None:
                        row["error"] = "release_failed"
                        results.extend(
                            dict(probe.result_for(later), error="previous_release_failed")
                            for later in requested[len(results):2]
                        )
                        break
                    if len(results) < len(requested):
                        await asyncio.sleep(probe.ADVERTISE_SECONDS)
                return results
            finally:
                # Hold remains the user's choice throughout. Unload and changes to
                # the switch share this lock, so a later explicit choice still wins.
                if was_held:
                    self._start_worker()

    async def _release_client(self, client) -> None:
        self._closing_client = client
        cleanup = asyncio.create_task(self._close_pending())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def set_hold(self, hold: bool) -> None:
        async with self._control_lock:
            self.hold = hold
            if hold:
                self._start_worker()
            else:
                await self._stop_worker()
            self._changed()
