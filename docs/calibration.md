[Sextant](../README.md) › Calibration

# Calibration

![The Calibration page: a run's per-proxy factors with a before and after error and a warning not to apply a worse solve](../img/screenshots/sextant-calibration.png)

Proxies calibrate each other: every proxy hears every other proxy's beacon
at a known distance, a run collects those readings for the floor picked
in the header and solves one range correction per proxy. Start a timed run
or leave **Auto
calibration** on: it samples every 30 s into a rolling six-hour window
across every floor and re-solves every 15 minutes. With the Bermuda fork the
samples come straight from its scanner-ranging table, which holds only how
each proxy hears the others; stock Bermuda is asked for a full device dump
each round instead, which is several megabytes on a large install. Before writing anything
it judges each floor's solve with the self-test (how far each proxy lands
from where it is placed) three ways: with no corrections, with the
corrections in place, and with the new ones. It applies the new set only
when that median improves by at least 2 %, takes its own earlier
corrections out again if none beats them, and otherwise leaves the floor
alone; the decision and the three figures show against each floor's
result. A manual solve gets the same verdict, and a solve that would not
help is marked *do not apply* (Apply then asks for a confirmation). **Apply** stores the
factors with the layout, or with `calibration_target: bermuda` writes them
into Bermuda as per-scanner RSSI offsets so Bermuda's own sensors are
corrected too. **Reset** removes them.

### Close range

A correction is learned from proxies hearing each other, metres apart. A
proxy that hears its siblings short gets a stretch above 1, and that is
right across the room but wrong for a thing lying beside it: a watch on a
bedside-table proxy read 1.1 m, the stretch of 1.73 made it 1.8 m, and the
fix was pushed off the table into the room. So a stretch fades out at close
range: none of it below `correction_fade_near_m` (1 m), all of it from
`correction_fade_far_m` (2.5 m), blended in between. Corrections below 1
are always applied in full. On the location pins this halved the error at
that table and moved the others by a few centimetres. Switch it off, or
move its edges, under Calibration on the Tuning page. It acts on factors
stored with the layout; with `calibration_target: bermuda` the correction
lives inside Bermuda's distances and is not faded.

**A proxy can be left out of it.** Turn on *Leave this proxy's correction
alone* in a proxy's panel on the Edit page and calibration skips it: it is
kept out of the fit, and whatever correction you set is never overwritten.
(The other switch there, *Use in positioning*, is the opposite case: the
proxy still calibrates and self-tests, it just no longer steers the fix -
see [Edit](edit.md).)
This is for a radio whose distances are the wrong *shape* rather than the
wrong scale. A wall tablet acting as a proxy read 3.2 m for something 2.1 m
away and 2.5 m for something 8.5 m away - long up close, far too short
across the room - so no single multiplier describes it, and the solve ran
into its ceiling of 5.0 trying. Left in the fit, that ceiling drags every
other proxy's correction with it.

Re-run calibration after changing a floor's scale, whether by hand or from
its [alignment anchors](edit.md#lining-the-floors-up): the corrections were
learned against distances measured at the old scale.

How the solve works, and how a factor maps onto a Bermuda RSSI offset, is in
[How positioning works](positioning.md).
