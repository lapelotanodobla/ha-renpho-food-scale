"""Renpho ES-SNG01 food scale protocol (pure Python, no Home Assistant imports).

Frames on characteristic 1A12 (notify) and 1A11 (write):
    55 AA <cmd> <len u16 BE> <payload> <sum8 of all previous bytes>

See PROTOCOL.md in the repository for the evidence behind each code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

SERVICE_UUID = "00001a10-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "00001a11-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "00001a12-0000-1000-8000-00805f9b34fb"
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

HEADER = b"\x55\xaa"
FROM_APP = 0xFB  # payload of the first echo after a command written by us
WEIGHED_MIN_CHANGE = 3  # grams; smaller settled changes are drift or an empty scale wobbling


class Cmd(IntEnum):
    """Frame codes sent by the scale. App commands are the same code | 0x80."""

    HELLO = 0x09
    STABLE = 0x10
    UNIT = 0x13
    LIVE = 0x14
    STABLE_FLAG = 0x15
    TARE = 0x17
    FL_OZ_EXTRA = 0x19  # follows a switch to fl'oz, meaning unknown
    POWER_OFF = 0x1B
    INFO = 0x1C


# Display unit codes, in the order the scale cycles: g, lb:oz, fl'oz, oz, ml.
UNITS: dict[int, str] = {0x03: "g", 0x02: "lb_oz", 0x04: "fl_oz", 0x00: "oz", 0x01: "ml"}
UNIT_CODES: dict[str, int] = {name: code for code, name in UNITS.items()}


@dataclass(frozen=True)
class Frame:
    cmd: int
    payload: bytes


def build(cmd: int, payload: bytes) -> bytes:
    body = HEADER + bytes([cmd, len(payload) >> 8, len(payload) & 0xFF]) + payload
    return body + bytes([sum(body) & 0xFF])


def parse(data: bytes) -> Frame | None:
    """Return the frame, or None if the header, length or checksum is wrong."""
    if len(data) < 6 or data[:2] != HEADER:
        return None
    length = int.from_bytes(data[3:5], "big")
    if len(data) != length + 6 or (sum(data[:-1]) & 0xFF) != data[-1]:
        return None
    return Frame(data[2], bytes(data[5:-1]))


def tare_command() -> bytes:
    return build(Cmd.TARE | 0x80, b"\x01")


def set_unit_command(unit: str) -> bytes:
    return build(Cmd.UNIT | 0x80, bytes([UNIT_CODES[unit]]))


@dataclass(frozen=True)
class ScaleEvent:
    """Something worth telling the user about (becomes an HA event)."""

    type: str  # "weighed" | "tare" | "unit_changed" | "power_off"
    data: dict = field(default_factory=dict)


@dataclass
class ScaleState:
    """Current readings, updated frame by frame. feed() returns the events a frame causes."""

    live_grams: int | None = None
    stable_grams: int | None = None
    unit: str | None = None
    _last_weighed: int | None = None
    _tare_from_app: bool = False

    def feed(self, frame: Frame) -> list[ScaleEvent]:
        cmd, p = frame.cmd, frame.payload
        if cmd in (Cmd.LIVE, Cmd.STABLE) and len(p) == 4:
            grams = int.from_bytes(p, "big", signed=True)
            self.live_grams = grams
            if cmd == Cmd.STABLE:
                self.stable_grams = grams
                # The scale repeats a stable reading (twice, then every few seconds) and
                # drifts by a gram or two: only a clear change is a new weighing, and an
                # (almost) empty scale is not one.
                last, self._last_weighed = self._last_weighed, grams
                changed = last is None or abs(grams - last) >= WEIGHED_MIN_CHANGE
                if changed and abs(grams) >= WEIGHED_MIN_CHANGE:
                    return [ScaleEvent("weighed", {"grams": grams})]
            return []
        if cmd == Cmd.TARE and p:
            if p[0] == FROM_APP:
                self._tare_from_app = True
                return []
            source = "app" if self._tare_from_app else "scale"
            self._tare_from_app = False
            return [ScaleEvent("tare", {"source": source})]
        if cmd == Cmd.UNIT and p and p[0] != FROM_APP:
            unit = UNITS.get(p[0])
            if unit and unit != self.unit:
                self.unit = unit
                return [ScaleEvent("unit_changed", {"unit": unit})]
            return []
        if cmd == Cmd.INFO and len(p) >= 2:
            self.unit = UNITS.get(p[1], self.unit)
            return []
        if cmd == Cmd.POWER_OFF:
            return [ScaleEvent("power_off")]
        return []

    def disconnected(self) -> None:
        self.live_grams = None
        self._last_weighed = None
        self._tare_from_app = False
