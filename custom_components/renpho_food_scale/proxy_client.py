"""Expose the normal scale operations on one HA-owned ESPHome backend."""

from .protocol import NOTIFY_UUID, SERVICE_UUID


class ProxyClient:
    def __init__(self, path, on_disconnect, timeout):
        self.backend = path.scanner.connector.client(
            path.ble_device, disconnected_callback=lambda: on_disconnect(self),
            timeout=timeout, services=None,
        )

    @property
    def is_connected(self):
        return self.backend.is_connected

    async def connect(self):
        await self.backend.connect(pair=False, dangerous_use_bleak_cache=False)

    async def disconnect(self):
        await self.backend.disconnect()

    def _characteristic(self, uuid):
        char = self.backend.services.get_characteristic(uuid)
        if char is None:
            raise ValueError(f"Scale characteristic {uuid} unavailable")
        if uuid == NOTIFY_UUID and (
            char.service_uuid.lower() != SERVICE_UUID or "notify" not in char.properties
        ):
            raise ValueError("Scale weight notification characteristic unavailable")
        return char

    async def start_notify(self, uuid, callback):
        char = self._characteristic(uuid)
        await self.backend.start_notify(char, lambda data: callback(char, data))

    async def read_gatt_char(self, uuid):
        return await self.backend.read_gatt_char(self._characteristic(uuid))

    async def write_gatt_char(self, uuid, data, *, response):
        await self.backend.write_gatt_char(self._characteristic(uuid), data, response=response)
