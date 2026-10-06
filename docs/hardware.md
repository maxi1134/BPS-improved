[Sextant](../README.md) › Hardware

# Hardware

Sextant wants many proxies. Three per floor is the floor, not the target,
and the difference between a room fix and a spot fix is usually one more
receiver in the right place. That runs into a practical problem: a purpose-
built BLE proxy is a box with a wall wart, and nobody wants twenty of those.

The way out is that every room already has mains-powered devices screwed into
the walls. This page is about turning those into proxies. None of it is
required to run Sextant, and none of it is Sextant's code - it is what the
test-bed house actually does, written down because the question comes up.

> **Mains voltage, and a soldering iron inside a device that plugs into it.**
> Everything below involves opening something wired to a live circuit and
> replacing a component on its board. Do it with the device unplugged and out
> of the wall, and only if you are comfortable doing it. A mistake here is not
> a bricked ESP.

## The pattern

Cheap smart outlets and switches are mostly a power supply, a relay and a
socketed WiFi module. The module is usually a Tuya part, and it is usually
pin-compatible with an Espressif module of the same footprint. Desolder the
vendor's module, solder on an ESP one, and the rest of the board - relay,
buttons, LEDs - is still there, wired to pins you can discover.

So the work per device family is: identify the module footprint, find which
GPIO reaches which part of the board, and write that pin map down once.
After that every unit of that family flashes the same package.

The house runs 36 outlets, 11 in-wall outlets and a set of light switches
this way, alongside six Shellys that needed no surgery.

## ELEGRP RRN00 outlets

A duplex receptacle with two independently switched halves. Thirty-six of
them, and the bulk of the coverage.

The vendor module comes out and one of three goes in, which is the wrinkle:
the same product shipped with two different footprints over time, and a third
was added to try the ESP32-C6 radio.

| Module | Chip | Named in the config |
|---|---|---|
| ESP8685-WROOM-03 | ESP32-C3 | `module: 3` |
| ESP8685-WROOM-06 | ESP32-C3 | `module: 6` |
| WT0132C6 | ESP32-C6 | `module: c6` |

The -03 and -06 are not pin-compatible with each other, which is the whole
reason the config carries a map per module rather than one set of pins:

| Function | -03 | -06 | C6 |
|---|---|---|---|
| Upper relay | GPIO3 | GPIO10 | GPIO7 |
| Lower relay | GPIO5 | GPIO7 | GPIO4 |
| Upper button | GPIO6 | GPIO6 | GPIO10 |
| Lower button | GPIO1 | GPIO3 | GPIO2 |
| Upper LED | GPIO7 | GPIO1 | GPIO8 |
| Lower LED | GPIO4 | GPIO19 | GPIO6 |
| Status LED | GPIO2 | GPIO2 | GPIO5 |

A device's own YAML is then four lines - its MAC suffix, its area, which
module it has - and everything else comes from a shared package.

## s2224 in-wall outlets

Eleven of these, an outlet with USB charging ports built in. Same idea, two
module options again (an XH-C3F or an ESP8685, both ESP32-C3), and a simpler
board: two relays, one button, one status LED.

| Function | XH-C3F | ESP8685 |
|---|---|---|
| Upper relay | GPIO19 | GPIO3 |
| Lower relay | GPIO18 | GPIO10 |
| Button | GPIO4 | GPIO5 |
| Status LED | GPIO5 | GPIO6 |

## Cloudfree SW1 switches

Light switches, ESP32-C3, status LED on GPIO5 inverted. These ship
ESP-based and unlocked, so there is no module swap - they are here because
they run the same proxy package as everything else.

## Shelly

Six, unmodified hardware running ESPHome rather than the vendor firmware, so
they carry the same BLE proxy configuration as the rest. Worth stating
plainly because "ESPHome and Shelly" reads like two firmwares: in this house
it is one.

## What every proxy runs

The point of the surgery is that the result is uniform. Whatever the casing,
each node includes the same BLE package, which sets:

- **A forked `bluetooth_proxy`** that filters on the device rather than
  forwarding every advertisement it hears, with allowlists that keep iBeacon,
  HomeKit, Find My and Tile traffic flowing. Find My covers AirPods too
  from v1.5.1: near their owner they send the pairing advert (Apple subtype
  `0x07`) rather than the Find My one, from the same rotating address.
- **A scan duty cycle of 120 ms listening in every 320 ms interval**, passive.
  That window is airtime taken from WiFi, and it is the knob to turn down
  first if a node becomes unstable.
- **An iBeacon advertisement**, every 500 to 1000 ms. This is what makes
  [calibration](calibration.md) possible: the proxies range each other, and
  comparing those proxy-to-proxy distances against their placed positions is
  what exposes each receiver's error. A proxy that does not beacon is a
  proxy calibration cannot measure.
- **WiFi/BLE coexistence enabled**, because the radio genuinely has two users
  here. The same house's light firmware turns it off, where it would idle.

## Choosing where they go

Placement beats hardware. The [Advice](advice.md) page ranks rooms by how
badly their proxies serve them and says where one more would help, which is
a better guide than adding another outlet to a room that already has three.
