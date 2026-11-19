# Renpho food scale ES-SNG01 (hardware rev "N3"): BLE protocol

**Confirmed 2026-11-19 on the real scale** (FCC ID 2A26P-ESSNG01N3, made 2026-01 by Shenzhen
Ruiyi, firmware 1.0.0, model string `RW-BLE-1.0`). Captures: `tests/fixtures/`.
This is **Renpho's own "55 AA" protocol** (the 0x1A10 family, like ES-CS20M / R-MSC04 / ES-26BB),
not Qingniu.

## Advertising
- Name `ES-SNG01`, manufacturer id `0x1A10`, data `00 0d 00 2a 1f 07 02 10 01 26 01 01`.
- **The weight is not in the broadcast:** it needs a GATT connection, one client at a time.

## GATT
| service | char | props | use |
|---|---|---|---|
| `1A10` | `1A11` | write (with response) | commands: tare, set unit |
| `1A10` | `1A12` | notify, read | **all scale data** |
| `180F` | `2A19` | notify, read | battery % (0x64 = 100) |
| `180A` | | read | maker RENPHO, model RW-BLE-1.0, serial, fw/hw/sw 1.0.0 |
| `F000FFC0-0451-4000-B000-000000000000` | FFC1/FFC2 | | **TI OAD firmware update: never write here** |

**No handshake:** subscribe to 1A12 and frames start arriving at once.

## Frames: `55 AA cmd len(u16 BE) payload sum8`
sum8 = sum of every byte before it, & 0xFF (all 349 captured frames check out).

| cmd | payload | meaning |
|---|---|---|
| `0x14` | int32 BE, signed | **live weight**, ~10 Hz while it changes (unit TBC, see below) |
| `0x15` | `01` | stable flag: always comes just before the 0x10 |
| `0x10` | int32 BE, signed | **stable weight**, sent twice in a row; this is the one to log |
| `0x17` | `01` | tare button pressed on the scale |
| `0x09` | (none) | sent once on connect |
| `0x1C` | `00 uu 01 00` | info on connect; `uu` = current unit (confirmed: `00` after setting oz) |
| `0x13` | unit code `00`..`04` | **display unit changed**; `03` g, `02` lb:oz, `04` fl'oz, `00` oz, `01` ml (the display cycles g → lb:oz → fl'oz → oz → ml) |
| `0x19` | `01` | always ~1 s after switching to fl'oz (meaning unknown) |
| `0x1B` | `01` | last frame before the scale went quiet: probably power-off |

Negative after tare comes as two's complement (`ff ff ff bb` = -69).

**Weight is always whole grams**, whatever unit is shown (confirmed 2026-11-19: glass+corkscrew =
170 in every unit, `captures/units-1328.log`). Changing the unit only sends `0x13`. 

## Commands (write to 1A11, confirmed 2026-11-19)
Rule: **app command = scale event code | 0x80** (same as the bathroom scales' 0x15 → 0x95 ack).
The scale answers with the event code twice: first payload `fb` (= done, from the app), then the
new value.

| action | frame | scale answers |
|---|---|---|
| **tare** | `55 AA 97 00 01 01 98` | `17 fb`, `17 01`, then stable 0 g |
| **set unit u** | `55 AA 93 00 01 uu cs` (g: `…03 96`, oz: `…00 93`) | `13 fb`, `13 uu` |

Captures: `tests/fixtures/`.
