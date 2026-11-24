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

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.state = ScaleState()
        self.battery: int | None = None
        self.connected = False
        self.hold = True  # False releases the scale for the phone / Renpho app
        self._client: BleakClientWithServiceCache | None = None
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

    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _run(self) -> None:
        delay = self.RETRY_INITIAL
        while True:
            self._advertised.clear()
            try:
                if (device := self._ble_device()) is not None and await self._session(device):
                    delay = self.RETRY_INITIAL
            except Exception as err:
                _LOGGER.debug("%s: connection attempt ended: %s", self.address, err)
            # HA can coalesce identical adverts after wake. A timer retries even
            # when the next advert never reaches our callback. A fresh advert
            # shortens the backoff; the minimum delay also bounds radio churn.
            bluetooth.async_rediscover_address(self.hass, self.address)
            await asyncio.sleep(self.RETRY_INITIAL)
            if delay > self.RETRY_INITIAL:
                try:
                    async with asyncio.timeout(delay - self.RETRY_INITIAL):
                        await self._advertised.wait()
                except TimeoutError:
                    pass
            delay = min(delay * 2, self.RETRY_MAX)

    async def _session(self, device: BLEDevice) -> bool:
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
                client = await establish_connection(
                    BleakClientWithServiceCache, device, self.name,
                    disconnected_callback=on_disconnect,
                    ble_device_callback=self._ble_device, max_attempts=3,
                )
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
            while client.is_connected and not ended.is_set():
                try:
                    async with asyncio.timeout(15):
                        await ended.wait()
                except TimeoutError:
                    pass  # A proxy may change is_connected without calling back.
            return True
        finally:
            ended.set()
            if client is not None:
                self._client = None
                self.connected = False
                self.state.disconnected()
                self._changed()
                if client.is_connected:
                    # A release during an already-running power-off cleanup must
                    # still return the physical connection slot to the proxy.
                    cleanup = asyncio.create_task(self._close_client(client))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
                _LOGGER.debug(
                    "%s: session ended: live=%d stable=%d rejected=%d",
                    self.address, received[Cmd.LIVE], received[Cmd.STABLE], rejected,
                )

    async def _close_client(self, client: BleakClientWithServiceCache) -> None:
        try:
            async with asyncio.timeout(self.IO_TIMEOUT):
                await client.disconnect()
        except Exception as err:
            _LOGGER.debug("%s: disconnect error: %s", self.address, err)

    # ---- commands --------------------------------------------------------

    async def _write(self, frame: bytes) -> None:
        if self._client is None or not self.connected:
            raise HomeAssistantError("The scale is not connected (switch it on)")
        await self._client.write_gatt_char(WRITE_UUID, frame, response=True)

    async def tare(self) -> None:
        await self._write(tare_command())

    async def set_unit(self, unit: str) -> None:
        await self._write(set_unit_command(unit))

    async def set_hold(self, hold: bool) -> None:
        async with self._control_lock:
            self.hold = hold
            if hold:
                self._start_worker()
            else:
                await self._stop_worker()
            self._changed()
