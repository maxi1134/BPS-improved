[Sextant](../README.md) › Live

# Live

The floor plan with every tracked thing drawn as an avatar in its own colour, the
same colour as its row in the list. Click a thing (row or avatar) to
focus it: everything else fades, it grows a halo, the panel switches to its
floor, and the side panel shows its room, spot, floor, the proxy it is
anchored to if any, and every proxy that hears it with the distance. Click
a **proxy** instead and a card says what it is and what it is doing: its
ESPHome release and project version, uptime, how many things it is hearing,
how many adverts it forwarded and ignored (and the IRKs behind that, where
the BLE filter publishes them), its Wi-Fi - with the
band, channel and the 802.11 generation it negotiated, where the proxy
publishes them - its chip and board, and its
MACs - the Bluetooth one Bermuda tracks, the one it talks on (Wi-Fi, or
Ethernet on a wired proxy), and the chip's own where it publishes it. Whatever it publishes is what the card shows; a
**Details** disclosure holds the floor odds, spot shares, confidence,
estimator telemetry, trust and speed. **Edit** (administrators) opens the
thing's dialog on the Things page - name, class, colour, photo, height -
without hunting for it in the list. A **blend slider** from geometric to
fingerprint sets how this thing's position is estimated (the two ends
are drawn on the map when the fingerprint switch is on), and **It's
actually here…** records a [location pin](live.md#location-pins). A thing with no fix shows *seen 40s
ago* rather than a blank. A row of buttons over the map draws or hides the
plan image, the labels, the trails, the spots, the proxies, the solver's
distance circles and the fingerprint fix; each lights up while it is on.
Labels names the rooms and the things (and whichever proxy you point at),
while Proxies draws the proxies themselves, which a busy plan is often
better without. In the corner, a countdown says how many seconds until the
next positioning cycle, and falls back to how long ago the last one was
when a cycle is late. For an admin it is also a menu: pick 5 or 10 seconds
to watch something move (or 30 or 60 to go easy on the box) and Sextant
refreshes that often for the next 15 minutes, then goes back to the
configured interval by itself; the countdown shows "every 5s" meanwhile, and
picking "normal" ends it early. The menu also says how long the last cycle
took. A faster refresh does not make the rooms or floors jumpier: their
smoothing and hold timers run on seconds, not cycles, so three 5 s cycles
move them as far as one 15 s cycle did. A history scrubber under
the map replays where a thing has been over the retention window, with a
room band, playback and a jump-to-time picker.

The map follows the theme: on a dark one the floor plan is inverted, so a
drawing that is black on white becomes white on black, and the labels take
a dark plate. Nothing to set - it reads the page's own background.

**Heard, late, away.** A thing moves through three states, each with its
own timer on the [Tuning](tuning.md) page:

| State | When | On the map | In the list |
| --- | --- | --- | --- |
| Heard | a fix this cycle | drawn plainly | plainly, with how long it has been there |
| Late | nothing heard for `stale_after_secs` (2 min) | a ghost: faint, dashed, *3m ago* under it | an hourglass badge and when it was last heard |
| Away | nothing heard for `away_after_secs` (15 min) | gone after `position_timeout` (5 min) | faded, a ghost badge, *since* when and where it was last seen |

A sighting is remembered across restarts, and a thing that went quiet
before Sextant last started is dated from the position history, so *since*
survives an update rather than resetting to it. That holds however many
restarts it has been quiet through: a thing's last sighting stays in the
saved state until it is heard again.

**The list floats over the map**, and the focused thing's detail opens as a
panel in the opposite corner rather than pushing the list down - the plan
runs the full width underneath both. Close it with the × or by clicking the
row again. On a phone nothing floats: the list and the detail are stacked
blocks under the map, as before.

**Batteries.** A thing can name its own battery sensor (Things → the thing →
*Battery sensor*; the list offers percentage sensors, the thing's own first).
Its level shows in the focused thing's card, and once it drops to 20 % a
battery badge appears in the bottom corner of its disc - amber, then red at
10 %. It sits apart from the status badge in the top corner because both can
be true at once, and usually are: a tag that has gone quiet because its
battery is dying looks exactly like a thing that has left, until the battery
says why.

What you see while a thing is late or away is where it *was*; its room and
spot sensors still say the same, because nothing has contradicted them yet.
Every thing Sextant knows stays in the list, so a phone that left the house
sits in its owner's group instead of vanishing, and a person whose things
are all away fades with them. `stale_after_secs` also decides which
readings the solver will use at all, so raising it makes Sextant patient in
both senses. The dashboard card draws ghosts the same way.

**How long it has been there, and where it has been.** The focused thing's
card says how long it has been where it is - *Meg's Cafe for 1h 12m, since
2:41 pm*, and when that is a spot, how long it has been in the room around
it too. Below is the **timeline**: the last day as a band, one colour per
room (a spot is the darker shade of its room, a stretch nobody heard it is
hatched), then the stays newest first with their times and lengths. A `+`
means the stay began before the start of what history keeps, so it is at
least that long. Stays come from the position history, which records a
point on every room and spot change.

On a phone those buttons collapse behind a single options button so they
never force sideways scrolling, the floor picker and the countdown move to
a bar under the page, and a row of **quick actions** — self-test, and for an administrator, adding a
thing and starting calibration — jumps straight to the right page
without hunting through the tabs. The map takes about half the screen and
can be put away: **Hide map** in the Things heading, which stays put as the
list scrolls, or the button on the map itself. Picking a thing opens it
again, under that thing's details. Editing the floor plan itself is still
a desktop job.

Each row carries the thing's name and floor on the left, with how long it
has been in that room or spot beside the floor (the exact time on hover),
and on the right the room it is in with its Home Assistant area's icon, the
spot underneath.
A small badge on the disc says what is special about it: a location marker
for the thing its owner's location is being read from, an hourglass while
it is late, a ghost once it is away.

## Grouped by person

When things have owners (**Belongs to** on the Things page), each person
gets a heading in the list - their picture, their name, and under it where
their own location sensor puts them - and a click on the heading folds the
group away. One thing is enough for a section; only a pet whose one thing
is its own tag stays a plain row. Those rows - the cats and the dog - come
next under **Pets**, and the rest follow under **Everything else**.

## Quick actions

Selecting a thing opens a row of buttons inside its row in the list:
**It's here** (a location pin, with the floor's spots to zoom to),
**Activity** (where it has spent its time; again to hide it), **History** (scrub it),
and **Edit**, which opens the thing on the Things page.

## Location pins

When a thing sits in the wrong place, select it on the Live page, click
**It's actually here…** and tap the spot on the map where it really is.
On a phone, pinch to zoom, or tap one of the floor's spots listed under
the prompt to fill the screen with just that spot (a bedside table
becomes phone-sized); **Whole floor** zooms back out. The pin goes down
when your finger lifts without moving, so panning or pinching never
places one.
Sextant keeps the solver inputs of the last few minutes for every
thing, so it re-solves those cycles under every blend of geometric fit
and fingerprint match and every reference gain, and shows how far each
lands from your pin and how often it gets the room right. **Apply** on a
row makes those the thing's settings (its blend weight, and a gain
multiplier folded into its learned gain). One pin can overfit, so pin a
thing in two or three rooms.

A pin inside a spot is also evidence of being in that spot: a fix is the
weighted blend of the pins and proxies it matched, and the share of that
blend sitting inside the spot counts the way a proxy on the spot does - but
only for a thing whose own fix is on or beside that spot, since pins are
shared by a class and a cat across the room still matches the couch's.
This is what gets a cat onto a couch - lying on it, the cat's own body
makes the couch's outlets read about twice too far, while the pins still
match.

A pin keeps the readings as Bermuda gave them, before calibration and
per-thing trims, and applies whatever corrections are in force each time
it is used, so re-running calibration or changing the close-range fade
never leaves a pin describing yesterday's corrections. Pins made before
3.17.7 have the correction then in force divided back out, assumed to be
the one in force now: re-pin a spot that matters if calibration has
changed since.

A pin guides the thing that made it and things of the same class - the
cats share one another's pins, a phone's pin helps the other phones -
and nothing else: a pin records how one device looks from one place, and
a watch on a wrist does not look like a phone in a hand
(`fingerprint_marks_scope`: `own`, `class` or `all`).

A selected thing lists its own pins - which pin, its floor, when it was
placed and over how many cycles - each with a bin to forget it.

Pins stay, with their samples, in `.storage/sextant_truth`, and do two
more jobs. The Tuning page's **Accuracy** card re-solves every pin under
the settings in force and reports, per thing, the mean error in metres
and the share of cycles in the right room: the accuracy figure the
stability KPI cannot give. And each pin becomes a fingerprint reference
at the pinned point, in the pinning thing's own scale, so rooms with no
probe nearby get a reference too (`fingerprint_marks` turns that off).

## Activity

With a thing selected, **Activity** colours the plan by how long
it spent in each half-metre square over the last hour, 6 hours, day or
week, blended into a smooth wash: blue for passing through, red for the
longest stay. The note beside
it gives the time on this floor, the longest stay in one square, and the
time on other floors. A reading holds until the next one for at most five
minutes, and a dropout counts for one minute, so a thing that went quiet
does not pile hours onto its last square. History keeps 6 hours by
default; **History kept (hours)** on the Tuning page raises it, up to a
week. Asking for more than is kept says how far back it goes.

