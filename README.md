![Sextant](img/logo.png)

# Sextant

**Indoor positioning for Home Assistant, powered by [Bermuda](https://github.com/agittins/bermuda).**

Bermuda's Bluetooth proxies measure how far each phone, watch, pet tag or
Tile is from each proxy. Sextant turns those distances into a dot on your
floor plan, and from the dot into the four things automations want: which
**floor**, which **room**, which **spot** in the room, and the **nearest
room** when the fix sits between two.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=davidcoulson&repository=sextant&category=Integration)

![The Live page: the floor plan with every tracked thing, the list grouped by person with pets on their own, and the selected dog's card with its room, spot, floor, proxies and timeline](img/screenshots/sextant-live.png)

## TL;DR

- 🐱 **Where is the cat?** Put a tag on her collar and Sextant shows her on
  your floor plan: which floor, which room, even which spot in the room
  (the sofa, the bed, the litter box). Same for the dog, your watch, your
  phone, the kids, the car keys.
- 🏠 **Automations that know who is where.** Lights that follow you from
  room to room, heating that knows the bedroom is empty, a nudge when the
  dog is on the couch again, a TV that pauses when you walk out. Every
  tracked thing gets five sensors: where it is (spot if it is in one,
  room if not), plus floor, room, spot and nearest room. Give things an
  owner and each person gets the same, placed by whichever of their things
  is with them, plus a device tracker.
- 📡 **More than Bluetooth.** An mmWave presence sensor on the plan puts
  the phone in front of it on the exact spot, pins locations by itself and
  counts people who carry nothing. Roborock vacuums are placed from their
  own maps.
- 🔧 **Set up in an afternoon.** Install from HACS, upload a floor plan,
  draw your rooms, drop your Bluetooth proxies where they sit in the house,
  choose what to track. That is all.
- 📍 **Steady, not jumpy.** A cat asleep on the bed stays "on the bed". A
  watch on the boundary between the kitchen and the dining room does not
  flip back and forth every few seconds.
- 🎯 **Teach it.** If something shows in the wrong place, click where it
  really is and Sextant works out which settings fit that thing best.
- 🤖 **Built with AI, tested in a real house.** Sextant was developed with
  Claude as a coding partner, and every change runs in the author's home
  before it ships: more than 60 Bluetooth proxies, 23 tracked phones,
  watches, pets and belongings plus three robot vacuums, 20 rooms on three
  floors. See
  [AI-assisted development](#ai-assisted-development).

## Docs

**New here? Start with [Getting started](docs/getting-started.md)**: from
nothing to a thing on your floor plan and a first automation.

| Install | Configure | Tune | Automate |
|---|---|---|---|
| [Getting started](docs/getting-started.md) · [Installing](docs/install.md) · [What you need](docs/install.md#what-you-need) · [Upgrading from BPS](docs/install.md#upgrading-from-bps-or-bps-improved) | [Edit](docs/edit.md) · [Things](docs/things.md) · [Bermuda](docs/bermuda.md) · [Proxies](docs/proxies.md) | [Live](docs/live.md) · [Calibration](docs/calibration.md) · [Tuning](docs/tuning.md) · [Advice](docs/advice.md) · [How positioning works](docs/positioning.md) | [Recipes](docs/recipes.md) · [Sensors, card, services and API](docs/automation.md) |

Also [Hardware](docs/hardware.md) (turning outlets and switches into
proxies, with the pin maps), [Where Sextant fits](docs/compared.md) (Bermuda, BPS, BPS-improved and
Sextant side by side) and [Data and development](docs/development.md).

## Quick start

1. HACS → Integrations → ⋮ → **Custom repositories**: add
   `davidcoulson/sextant` as an Integration, install **Sextant**, restart.
2. **Settings → Devices & Services → Add Integration → Sextant**. Bermuda
   must already be set up; for everything Sextant can do, run the
   [Bermuda fork](https://github.com/davidcoulson/bermuda).
3. Open **Sextant** in the sidebar. On **Edit**, add a floor from a plan
   image, set its scale, place the proxies, draw the rooms, Save.
4. On **Things**, pick what to track. Positions appear on **Live**
   within a cycle.
5. With more than one floor, give each an elevation and line them up with
   [alignment anchors](docs/edit.md#lining-the-floors-up).

[Getting started](docs/getting-started.md) walks through all of it.

You want three or more proxies per floor, and each ESPHome proxy should
advertise an iBeacon so its siblings can range it. The details, including
the beacon block, are in [What you need](docs/install.md#what-you-need).

## The panel

A native Home Assistant panel at `/sextant`, no iframe and no pasted
token, in Home Assistant's own theme and unit system. Eight pages:

| Page | For |
|---|---|
| [Live](docs/live.md) | every tracked thing on the plan, grouped by person; focus one for its room, spot, floor, proxies, timeline and activity heatmap; location pins; a refresh menu on the countdown |
| [Edit](docs/edit.md) | place proxies and mmWave sensors, draw rooms, spots and no-go areas, set the scale and floor levels, line the floors up with anchors |
| [Things](docs/things.md) | what is tracked and everything Bermuda hears; name, class, colour, photo, height, owner and estimator per thing; people and their GPS fallbacks; robot vacuums |
| [Bermuda](docs/bermuda.md) | Bermuda's global options, Find My accessories, Tiles |
| [Proxies](docs/proxies.md) | proxy health by floor and room, what each proxy hears, the self-test |
| [Calibration](docs/calibration.md) | proxies calibrate each other; apply into Sextant or into Bermuda |
| [Tuning](docs/tuning.md) | the stability KPI, accuracy from location pins, every knob live |
| [Advice](docs/advice.md) | which rooms the proxies serve worst and where one more would help |

## What Sextant adds to Bermuda

- A position on the floor plan: trilateration fused with fingerprints the
  proxies build by hearing each other's beacons.
- Rooms as polygons with spots and no-go areas. Room and spot elections
  with a margin, a dwell and a stationary lock, so sensors do not flap.
- Floor election by competition between floors, scaled by proximity and a
  per-floor bias that can vary across the plan (a bias field), for landings
  and double-height rooms where both floors hear a thing equally.
- Alignment anchors that say how the floors stack, with an elevation per
  floor: shared named points on every plan, fitted into one house frame,
  with misplaced pins found and named and each floor's scale audited.
- Proxy calibration with 3D heights, written into Bermuda if you like.
- Location pins: say where a thing really is and Sextant finds the settings
  that fit it, and reports accuracy in metres.
- People: things given an owner place that person, by whichever of them is
  moving or most recently arrived, with a GPS tracker to fall back on when
  the house has lost them.
- mmWave presence sensors (Everything Presence, Apollo R PRO-1 and other
  LD2450 boards) on the plan: a thing near a target is put on it, a still
  target and a still thing make a pin nobody had to tap, and a target no
  device accounts for counts as an untracked person.
- Roborock vacuums placed from their own maps, lined up with the plan by
  the rooms both maps name.
- Bermuda management from the panel: track, untrack, Find My accessories,
  Tiles followed across address rotation (with the fork).
- Five sensors per thing, a map card, services and a websocket push per
  cycle. Pure numpy, no SciPy.

## AI-assisted development

Sextant was written with [Claude](https://claude.ai) as a pair programmer,
through Claude Code. Claude wrote most of the code, the tests and these
docs. What keeps that honest is the test bed: every change runs in the
author's house before it ships, with more than 60 Bluetooth proxies (bare
ESP boards and Shellys, all running ESPHome), 20 rooms with 13 spots across
three floors, an mmWave sensor, three Roborock vacuums, and a tracked mix of
people, phones, watches, Find My accessories and seven animals on iBeacon
collar tags. The author sets the
direction, watches what the things do on the real floor plan, and
decides what ships. Expect the codebase to read the way an AI writes it:
long comments explaining why, a consistent shape from file to file, and a
test for nearly everything. Bugs are still bugs, whoever typed them;
issues and pull requests are welcome on
[davidcoulson/sextant](https://github.com/davidcoulson/sextant).

## Credits

Sextant began as a fork of [Hogster/BPS](https://github.com/Hogster/BPS)
via [maxi1134/BPS-improved](https://github.com/maxi1134/BPS-improved).
Full credit for the original integration goes to
[@Hogster](https://github.com/Hogster) and [@maxi1134](https://github.com/maxi1134),
to [@agittins](https://github.com/agittins) for
[Bermuda](https://github.com/agittins/bermuda), and to
[Megarushing](https://github.com/Megarushing/bermuda) for the Find My
accessory support merged into the Bermuda fork. MIT licensed, see
[LICENSE](LICENSE).
