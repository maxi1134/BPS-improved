[Sextant](../README.md) › Installing Sextant

# Installing Sextant

## What you need

- **Home Assistant** 2026.x (developed on 2026.9).
- **Bermuda** tracking at least one device. For positioning alone upstream
  Bermuda is enough. For everything Sextant does, install
  [davidcoulson/bermuda](https://github.com/davidcoulson/bermuda) (the
  `fork-testing` releases): it adds the device-management API behind the
  Things and Bermuda pages, proxy-to-proxy ranging for fingerprint fusion
  and calibration, RSSI history for the median estimator, per-scanner RSSI
  offsets so calibration can be written into Bermuda, and Tile tracking
  across address rotations. Sextant detects what the installed Bermuda
  offers and hides what it cannot do.
- **Three or more Bluetooth proxies per floor** you want positions on:
  ESPHome `bluetooth_proxy` boards or Shelly Plus/Gen2+ devices. More is
  better; a proxy every three to four metres gives room-level accuracy.
- For calibration and fingerprint fusion, **each ESPHome proxy should
  advertise an iBeacon** so its siblings can range it (Shelly proxies
  already advertise). One block per proxy, same UUID across the fleet,
  unique `minor`:

  ```yaml
  esp32_ble_beacon:
    type: iBeacon
    uuid: fde3b150-2f64-43ba-aee9-867f75ee4a6f
    major: 1
    minor: ${ static_ip.split('.')[3] | int }
    min_interval: 500ms
    max_interval: 1000ms
  ```

No SciPy: the solver is pure numpy, so 32-bit and constrained installs work.

## Installation

This is the short version. [Getting started](getting-started.md) is the
same thing step by step, with what to check when a thing lands in the
wrong room.

1. HACS → Integrations → ⋮ → **Custom repositories**. Add
   `davidcoulson/sextant` as an Integration, then install **Sextant** and
   restart Home Assistant.
2. **Settings → Devices & Services → Add Integration → Sextant**. Bermuda
   must already be set up.
3. Open **Sextant** in the sidebar. On the **Edit** page add a floor from a
   floor-plan image, set its scale by measuring a known distance, place the
   proxies (picked from the list Bermuda reports), draw the rooms, Save.
4. On **Things**, pick what to track from everything Bermuda hears.
   Positions appear on **Live** within a cycle.

The integration's options (⋮ on its card) hold the sidebar toggle and the
positioning interval (15 s by default, 1 to 300).

### Upgrading from BPS or BPS-improved

Sextant is a separate integration, so this is a re-install rather than an
update:

1. Install Sextant and add the integration as above. On its first start it
   copies the old layout, calibration state, position history and map
   images from where BPS kept them. Nothing is deleted.
2. Remove the old BPS integration entry.
3. Update automations: `sensor.<device>_bps_zone` is now
   `sensor.<device>_sextant_room`, `_bps_floor` is `_sextant_floor`,
   `_bps_nearest_zone` is `_sextant_nearest_room`, and `_bps_sub_zone` is
   `_sextant_spot`. Services are `sextant.*`.
4. Dashboards switch to `custom:sextant-map-card` (resource
   `/sextant/sextant-map-card.js`). The old `custom:bps-map-card` type
   and the `/bps/` path were retired in 3.10.0.
