[Sextant](../README.md) › Getting started

# Getting started

From nothing to a thing on your floor plan, and a first automation that
uses it. Budget an afternoon for a house; most of it is drawing rooms. The
reference pages are linked as you go, for when you want the detail.

## Before you start

You need these four. The first two are the ones people skip.

1. **Bluetooth proxies, three or more on every floor** you want positions
   on. ESPHome `bluetooth_proxy` boards or Shelly Gen2+ devices. Three is
   the minimum a position can be solved from, not a target: a proxy every
   three to four metres is what gets you the right room. Smart outlets and
   switches can be turned into proxies; [Hardware](hardware.md) shows how.
2. **An iBeacon on every ESPHome proxy**, so the proxies can range each
   other. Calibration and fingerprinting are built on those proxy-to-proxy
   distances. The block to add is in
   [What you need](install.md#what-you-need). Shellys already advertise.
3. **[Bermuda](https://github.com/agittins/bermuda)**, set up and hearing
   your proxies. For everything Sextant can do, use the
   [Bermuda fork](https://github.com/davidcoulson/bermuda) (`fork-testing`
   releases). Upstream Bermuda gives positions; the fork adds managing
   devices from the panel, Find My accessories, Tiles, calibration written
   back into Bermuda and fingerprinting.
4. **A floor plan image for each floor**: a PNG or JPEG, ideally drawn to
   scale. A builder's plan full of hatching and dimensions works but is
   noisy; `tools/clean_floorplan.py` strips it back to walls
   ([how](edit.md#a-busier-plan-than-the-map-needs)).

Measure one long distance on each floor while you are at it: a wall you
can run a tape along, or an outside dimension from the plans. It sets the
scale, and the scale matters more than anything else you will enter.

## 1. Install

HACS → Integrations → ⋮ → **Custom repositories** → add
`davidcoulson/sextant` as an Integration → install **Sextant** → restart
Home Assistant. Then **Settings → Devices & Services → Add Integration →
Sextant**. **Sextant** appears in the sidebar.

## 2. Draw the first floor

Open **Sextant → Edit**. Start with the floor you spend the most time on.

1. **Add a floor** at the bottom of the side panel: a name and the plan
   image. Give it *Level* 0 if it is the ground floor.
2. **Set the scale.** Pick **Scale** in the toolbar, click the two ends of
   the distance you measured, type it in. Everything Sextant computes is in
   metres through this number, so a scale that is 10 % out makes every
   distance on the floor 10 % out.
3. **Place the proxies.** Pick **Proxy**, choose one from the list (it is
   what Bermuda reports; the room Bermuda thinks it is in is shown beside
   it) and click where it sits. A proxy in an outlet or switch snaps onto
   the wall, on the side you dragged it from, which decides which room it
   counts for. Select a placed proxy to give it a **mount height**: 0.3 m
   for an outlet, 1.2 m for a switch. Heights are worth entering; they
   turn slant distances into distances across the floor.
4. **Draw the rooms.** Pick **Room**, click the corners, close on the
   first corner. Name each one as you want it to appear in automations.
   Open areas that are not a room at all - the void over a double-height
   foyer, a stairwell - are **No-go** areas.
5. **Save.**

Spots (a bed, a sofa, a desk) can wait until things are being tracked;
you will draw better spots once you have watched where things actually sit.
When you do: a spot belongs to the room under its first corner and is
trimmed to that room on Save; measure the furniture and use **Set size**
rather than drawing it by eye; and if a proxy sits on the furniture itself
(a nightstand, a desk), pick it under **Proxies on this spot** - a thing that
proxy hears close by counts as on the spot however wide the position
estimate is ([Edit](edit.md)).

## 3. Track something

Open **Things**. The bottom list is everything Bermuda hears; the top is
what Sextant tracks.

- **An iBeacon or a tag with a fixed address** (pet collars, most fitness
  tags): find it in the lower list and press **Track…**. The dialog asks
  for a name, a class (person, cat, dog, keys…) and the height it is
  carried at - 0.3 m for a cat's collar, 1 m for a phone in a pocket.
- **A phone or a watch** changes its address every few minutes and needs
  its Identity Resolving Key: **Add a thing… → A phone or watch**.
- **AirTags, AirPods and other Find My accessories** need their pairing
  keys exported from Apple, which is the awkward part:
  [the walkthrough](https://github.com/davidcoulson/bermuda/blob/fork-testing/docs/findmy.md).
- **A Tile**: bind it on the [Bermuda page](bermuda.md).

Within a cycle (15 seconds) it appears on **Live**.

## 4. Check it

Click the thing on **Live**. The side panel shows its room, floor and
every proxy that hears it, with the distance. Walk it around. If it lands
in the wrong room:

- **Is it heard by at least three proxies?** The side panel lists them.
  Fewer, and it can only be placed near the loudest one. The
  [Advice](advice.md) page says which rooms are short of proxies and
  where one more would help most.
- **Are the proxies where the plan says they are?** A proxy placed a room
  away pulls everything toward the wrong room.
- **Calibrate.** On **Calibration**, turn **Auto calibration** on and
  leave it for an hour. The proxies range each other at known distances
  and each gets a correction for how loudly it hears.
- **Pin the truth.** Select the thing, **It's actually here…**, click
  where it really is. On a phone, tap the spot's name to zoom to it first,
  or pinch. Sextant re-solves the last few minutes under every setting and
  offers the ones that would have put it there, and the pin becomes a
  reference for placing that thing there again
  ([Location pins](live.md#location-pins)).
- **See where it has been.** **Activity** colours the plan by where
  the selected thing spent the last hours - a quick check that the
  couch it sat on all evening is where the colour is. History keeps 6 hours
  unless **History kept (hours)** on Tuning says otherwise.

## 5. More floors

Add each floor the same way. Then two things matter that did not with one:

- **Level and Elevation.** *Level* orders the floors (-1 basement, 1
  above). *Elevation* is how far that floor's finished floor sits above
  the ground floor's: the ceiling height below it plus about 30 cm of
  floor structure. A basement with 9 ft ceilings is about -3.05 m; a
  floor above an 11 ft ground floor is about +3.66 m.
- **Line the floors up with pins.** Pick four to eight points that run
  straight up through the house and click them on every floor with the
  **Pin** tool, in the same order. Outside corners and stair openings are
  good; interior room corners are not, because interior walls move between
  floors. The **Alignment** card says how well they agree and names any
  pin that is on the wrong corner - delete it rather than nudge it; a pin
  does not have to exist on every floor. If the pins say the floor's scale
  is off, take their word for it (**Use the pins' scale**), then re-run
  calibration on that floor. [More on pins](edit.md#lining-the-floors-up).

## 6. Use it

Every tracked thing gets a set of sensors. The one to read is
`sensor.<thing>_sextant_location`: the spot when the thing is in one, the
room when it is not, with `kind`, `room`, `spot` and `floor` as attributes,
plus `area_id` and `floor_id` when the room is linked to a Home Assistant
area ([Edit](edit.md)).
One entity per thing, whatever resolution you want:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.meg_sextant_location
    to: "Meg's Cafe"
actions:
  - action: light.turn_on
    target: {entity_id: light.catwalk_lamp}
```

```yaml
# Any room, via the attribute: true while the dog is anywhere in the kitchen,
# including on a spot inside it
condition: template
value_template: "{{ state_attr('sensor.primrose_sextant_location', 'room') == 'Kitchen' }}"
```

The other sensors, the map card for dashboards, the services and the API
are in [Sensors, card, services and API](automation.md).

## When something is off

| Symptom | Look at |
|---|---|
| A thing reads the floor above or below | Its floor odds under **Details** on Live. An open landing or double-height room is the usual cause. **Show floor bias** on Edit shows where each floor's prior leans; raising `floor_proximity_weight` (Tuning) trusts the nearest proxies more, and [bias fields](positioning.md) shape it place by place. |
| A thing sits a metre off the table it is on | A proxy near it may carry a calibration stretch; see [close range](calibration.md#close-range). If the proxy is on the furniture, link it to the spot. |
| A thing flips between two rooms | It is on the boundary. The room holds against small challenges by design; if it still flips, see [Tuning](tuning.md). A spot drawn over the boundary (a sofa against a wall) often fixes it. |
| Nothing is placed at all | Fewer than three proxies hear it, or Bermuda is not tracking it. Check the proxy list on Live and the Things page. |
| A proxy shows orange on Edit | Bermuda does not report it any more: renamed, offline, or its address changed. |
| Everything on one floor is a little off | That floor's scale. Pins will tell you if it is. |
