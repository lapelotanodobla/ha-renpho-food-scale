# Renpho Food Scale for Home Assistant

Local Bluetooth integration for the **Renpho ES-SNG01 "Smart Nutrition Scale"** (hardware revision
N3, FCC ID 2A26P-ESSNG01N3). No cloud, no Renpho app. Home Assistant connects to the scale through
your Bluetooth adapter or ESPHome Bluetooth proxies whenever the scale is switched on.

The protocol was reverse-engineered from the scale itself, see [PROTOCOL.md](PROTOCOL.md).

## Entities

| Entity | What it is |
|---|---|
| `sensor.…_weight` | Live weight in grams (up to 4 updates/s). Unavailable while the scale is off. |
| `sensor.…_last_weighing` | Last settled weight in grams. Kept while the scale is off. |
| `event.…_activity` | `weighed` (`grams`), `tare` (`source`: `scale` or `app`), `unit_changed` (`unit`), `power_off` |
| `button.…_tare` | Tare from Home Assistant |
| `select.…_display_unit` | Unit the scale's display shows (g, ml, oz, lb:oz, fl'oz). Weights in HA are always grams. |
| `switch.…_keep_connected` | Turn off to release the scale so a phone or the Renpho app can connect (one connection at a time) |
| `binary_sensor.…_connected`, `sensor.…_battery` | Diagnostics |

`weighed` fires once per new settled weight: the scale repeats settled readings and drifts by a
gram or two, so a weighing needs a change of at least 3 g, and an (almost) empty scale, within
±2 g of zero, is not one. Negative weights after a tare are reported as negative grams.

## Install

HACS → three dots → Custom repositories → `https://github.com/lapelotanodobla/ha-renpho-food-scale`,
category Integration → download → restart Home Assistant. Switch the scale on: it is discovered
automatically (Settings → Devices & services).

Requirements: Home Assistant 2026.1+, and a Bluetooth adapter or an ESPHome Bluetooth proxy with
active connections enabled within range of the scale. The scale takes one connection; it uses one
connection slot on the proxy while switched on.

## Notes

- Only tested with ES-SNG01 revision N3 (made 2026). Older revisions may use a different chip and
  protocol; captures from those are welcome in an issue.
- The live weight sensor is throttled to 4 updates per second. To keep it out of the recorder
  entirely, exclude it in the `recorder:` configuration.
