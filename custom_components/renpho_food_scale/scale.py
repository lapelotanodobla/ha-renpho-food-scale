"""Connection manager: keeps a GATT connection to the scale whenever it is awake."""

from __future__ import annotations

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

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.state = ScaleState()
        self.battery: int | None = None
        self.connected = False
        self.hold = True  # False releases the scale for the phone / Renpho app
        self._client: BleakClientWithServiceCache | None = None
        self._connecting = False
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
        self._schedule_connect()

    async def stop(self) -> None:
        if self._unsub_adv:
            self._unsub_adv()
            self._unsub_adv = None
        await self._disconnect()

    @callback
    def _on_advertisement(
        self, _info: bluetooth.BluetoothServiceInfoBleak, _change: bluetooth.BluetoothChange
    ) -> None:
        self._schedule_connect()

    @callback
    def _schedule_connect(self) -> None:
        # Adverts arrive several times a second: set the flag synchronously so only
        # one connect task runs at a time.
        if self.hold and not self.connected and not self._connecting:
            self._connecting = True
            self.hass.async_create_background_task(
                self._connect(), f"renpho_food_scale connect {self.address}"
            )

    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _connect(self) -> None:
        try:
            await self._try_connect()
        finally:
            self._connecting = False

    async def _try_connect(self) -> None:
        if self.connected or not self.hold:
            return
        device = self._ble_device()
        if device is None:
            return  # not advertising: switched off
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                disconnected_callback=self._on_disconnect,
                ble_device_callback=self._ble_device,
                max_attempts=3,
            )
        except Exception as err:  # noqa: BLE001 - bleak raises many types; retry on next advert
            _LOGGER.debug("%s: connect failed: %s", self.address, err)
            return
        try:
            await client.start_notify(NOTIFY_UUID, self._on_notify)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: subscribe failed: %s", self.address, err)
            await client.disconnect()
            return
        self._client = client
        self.connected = True
        _LOGGER.debug("%s: connected", self.address)
        try:
            self.battery = (await client.read_gatt_char(BATTERY_UUID))[0]
            await client.start_notify(BATTERY_UUID, self._on_battery)
        except Exception as err:  # noqa: BLE001 - battery is optional
            _LOGGER.debug("%s: battery unavailable: %s", self.address, err)
        self._changed()

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: disconnect error: %s", self.address, err)
        self._mark_disconnected()

    def _on_disconnect(self, _client: object) -> None:
        # Called by bleak; hop onto the event loop.
        self.hass.loop.call_soon_threadsafe(self._mark_disconnected)

    @callback
    def _mark_disconnected(self) -> None:
        if not self.connected:
            return
        self.connected = False
        self._client = None
        self.state.disconnected()
        _LOGGER.debug("%s: disconnected", self.address)
        self._changed()

    # ---- data ------------------------------------------------------------

    @callback
    def _on_notify(self, _char: object, data: bytearray) -> None:
        frame = parse(bytes(data))
        if frame is None:
            _LOGGER.debug("%s: ignoring frame %s", self.address, bytes(data).hex(" "))
            return
        events = self.state.feed(frame)
        self._changed()
        for event in events:
            for cb in list(self._event_listeners):
                cb(event)

    @callback
    def _on_battery(self, _char: object, data: bytearray) -> None:
        if data:
            self.battery = data[0]
            self._changed()

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
        self.hold = hold
        if hold:
            self._schedule_connect()
        else:
            await self._disconnect()
        self._changed()
