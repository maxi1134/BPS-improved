[Sextant](../README.md) › Things

# Things

![The Things page: every tracked thing with its class icon, where it is, its height and its reference trim](../img/screenshots/sextant-things.png)

Bermuda's device management without its options flow. **Add a thing…** asks
what you are adding, because how a thing is followed depends on whether its
Bluetooth address stays put:

| What | What it needs |
|---|---|
| A phone or watch | Its Identity Resolving Key. A phone changes its address every few minutes and only that key follows it. Paste the key here and Sextant fills in Home Assistant's own Private BLE Device form, which will only take a key it can watch resolve an address right now — so keep the phone awake and near a proxy. |
| An AirTag or FindMy tag | Its pairing keys, exported from the Mac it was paired from or from iCloud, depending on the macOS version. Opens the walkthrough on the [Bermuda page](bermuda.md). |
| A Tile | A binding, on the [Bermuda page](bermuda.md), so one Tile is followed across its rotations. |
| Anything else | Nothing. An iBeacon, a fitness band or a tag with a fixed address simply appears below once a couple of proxies hear it. |

The top list is what
is tracked: click the name or the icon to open the thing dialog and set a
display name, a class (person, dog, cat, phone, watch, headphones, keys, tag and more,
each with its icon on the map), pronouns (he, she, they or it: how the panel
refers to it; left unset, a man is he, a woman she, a device it, and a
person, child or pet they), who it belongs to (a Home Assistant person),
a colour used everywhere it is drawn, a
photo framed in a circle that replaces the icon, the height it is carried
at, a reference-power trim, and its own position estimator. **Untrack** removes it from Bermuda and
removes its five Sextant sensors and its device with it; a device untracked
while Home Assistant was down is cleaned up on the next cycle.

Below is everything Bermuda hears but does not track, with the kind of
device (iBeacon, Tile, Apple, IRK, plain address), what it appears to be
(the Bermuda fork names families the Bluetooth SIG's lists cannot — a Govee
sensor, a Samsung SmartTag — and says when that kind rotates its address,
which is why some of them cannot be followed at all), where it is (the room
of the loudest placed proxy) and the signal there in dBm, and for Apple
adverts what they are (AirPods and accessories, an iPhone, Watch or Mac
nearby, a Find My tag). Search by name, address, room, floor, proxy, kind
or maker. Adverts heard only by unplaced proxies are ignored, and the
proxies' own probe beacons are hidden. **Track…** opens the same dialog
first, so you choose the name, class and height before Bermuda is told and
reloads.

## People

Give things an owner - **Belongs to**, a Home Assistant person - and two
things follow. On Live, each owner gets a heading of their own (their
picture, and where they are), which folds their things away with a click. And each owner gets three sensors of their own:
`sensor.<person>_sextant_person_location` (the spot, or the room),
`_sextant_person_room` and `_sextant_person_floor` (and a
`device_tracker.<person>_sextant`), with `via` naming the
thing they came from. Automations can then ask where David is rather than
where his phone is.

Which thing speaks for a person: only things that **give their owner's
location** count - by default a watch, a phone, or a person's or pet's own
tag, not headphones, keys, a bag or other tags, which go along only some of
the time (the thing dialog switches it either way); of those, only ones
heard recently and placed in a room. One moving now, or that arrived where
it is in the last ten minutes, beats one that has sat still longer (the
phone left on the couch is not you), and among those a pet's own tag, a
watch, then a phone. When nothing is on the move, the one that arrived
where it is most recently wins - the phone you carried downstairs, not the
watch on its charger since last night.

A thing can also name an **On-charger sensor** - its battery state from
iCloud3, the companion app, or any `binary_sensor` that is on while it
charges. While that reads Charging, Charged or Full, the thing does not
speak for its owner at all: a watch on its charger is on nobody's wrist.
Unavailable or unknown counts as not charging, so a sensor that has nothing
to say never takes a thing out of the running. The location sensor's
`considered` list marks a thing left out this way with `on_charger`.


## The People card

Below the things, a **People** card lists each owner: where they read as
being and how (by their things, the last place held while unheard, or a
GPS tracker), and their **GPS sources** in order. Add the Companion app,
Life360 or any `device_tracker` with coordinates; the first one that is
neither broken nor stale is used once Sextant has lost the person. The card
also says which sources are being passed over right now, and why. See
[People](automation.md#people) for what the sensors then carry.

## Robot vacuums

Roborock vacuums do not advertise over Bluetooth, but each knows exactly
where it is on its own map. The **Robot vacuums** card at the bottom of the
Things page lines that map up with one of your floors:

1. Pick the floor the vacuum cleans and press **Line up**. Sextant reads
   the robot's map once, pairs the rooms both maps name - by room name, or
   by the Home Assistant area a room is linked to, so the robot's "Jack
   Bedroom" finds the room linked to `jack_bedroom` - and fits the robot's
   map onto the plan: turned, moved and (usually) mirrored, never stretched,
   because the robot's millimetres are real ones. The card shows how well
   the rooms agree (a few tenths of a metre is good) and any room left out
   because the two maps disagree about it; a Foyer that is the front hall on
   one map and the whole open ground floor on the other is typical.
2. On Live, select the robot and use **Mark dock**
   (<ha-icon icon="mdi:home-import-outline"></ha-icon>), then tap where
   its dock is. The robot reports its dock itself, so it need not be on it.
   The dock counts for three rooms, and it is the only way to settle a map
   with just two matching rooms, which cannot tell a mirrored map from a
   turned one.

After that the robot is a thing like any other - `sensor.vacuum_<name>_sextant_room`,
`_floor`, `_spot`, `_location` (with the vacuum's own state in
`vacuum_state`), a trail on Live and a history. While docked it is at its
dock and costs nothing; while out it is asked where it is every
`robot_poll_secs` (30 s). Each ask makes the Roborock integration fetch and
parse the map, which holds Home Assistant's event loop for a few hundred
milliseconds, so raise it if a vacuum's cleaning shows up in the loop's
timings. A read that fails keeps the last position; the card counts reads
that worked and failed since Home Assistant started, which is how to tell
whether the interval is too short.

This needs the `roborock.get_vacuum_map_rooms` action, which Home Assistant's
built-in Roborock integration does not have yet; the
[roborock override](https://github.com/davidcoulson/ha-roborock-override)
2026.9.3.2 or later adds it.
