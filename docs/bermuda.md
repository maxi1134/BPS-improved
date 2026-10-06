[Sextant](../README.md) › Bermuda

# Bermuda

![The Bermuda page: Bermuda's global options, Find My accessories and the Tiles card](../img/screenshots/sextant-bermuda.png)

Bermuda's global options (reference power, attenuation, max area radius,
max velocity, not-home timeout, update interval, smoothing samples, scanner
entities, Tile identity probes) edited in place; Bermuda reloads to apply.
**Add accessories…** walks through exporting a Find My accessory's keys and
pasting them. The Tiles card shows each configured Tile (click its name to rename it),
the address it is bound to, its rotation history, and an **Adopt** picker
for the moment Bermuda loses one; see
[Tiles](bermuda.md#tiles-find-my-and-apple-devices).

## Tiles, Find My and Apple devices

**Tiles** rotate their Bluetooth address, and rotate again right after any
connection, so an address never names one for long. The Bermuda fork
tracks a Tile as a metadevice (`tile_<id>`) and follows rotations by
matching the per-proxy RSSI pattern of the old and new address, which
needs at least two proxies hearing both. The pattern is remembered, so a
Tile is found again after a restart. When one is lost anyway (marked *not
heard*, typically a Tile sitting among other Tiles), the Bermuda page's
**Adopt** picker lists the live Tile addresses with the room of the
loudest proxy; pick the one where the Tile is. *Tile identity probes*
reads each Tile's ID over Bluetooth, but on the Private ID Tiles seen so
far that ID rotates with the address, so the switch stays off unless your
Tiles keep a stable ID. Ringing a Tile needs the Tile account's
authentication and is not possible from here. Name a Tile from the
Things page ("Kitchen keys") so the map does not show an address.

**Find My accessories** (AirTags, licensed tags and AirPods - not the Siri
Remote, which despite tvOS's "find my remote" is not a Find My accessory and
needs a tag stuck to it) rotate on a key schedule. Export the accessory's pairing keys
(`master_key`, `skn`, `sks`, `paired_at`) and paste them in the walkthrough
on the Bermuda page; Bermuda then derives the addresses the tag can be using
and tracks it like an IRK device. Getting those keys is the awkward part and
depends on your macOS version: on 14 and earlier `tools/findmy_export.py` in
the Bermuda fork reads them off the Mac; on 15 and later they come from
iCloud; and when the iCloud sign-in will not complete, a throwaway macOS 15
virtual machine does it. [The walkthrough](https://github.com/davidcoulson/bermuda/blob/fork-testing/docs/findmy.md)
has all three. The keys live in Bermuda's config entry; treat backups and
diagnostics accordingly, and delete the exported files once they are pasted.

The list shows each accessory's state. *Seen as* primary or secondary, with
how long ago, is a tag being tracked. *Looking for it* means Bermuda knows
roughly where the tag's key schedule is but has not heard it yet: a tag that
lives at home with you rotates slowly and runs months behind its pairing
date, so Bermuda searches up from Apple's last sighting a month of schedule
per rebuild, and the first find can take a few. *Not heard yet* means there
is no sighting to start from. AirPods near their owner send a different
advert from the Find My one; an ESPHome proxy that blocks Apple's
manufacturer ID needs `allow_findmy` from the filtering proxy component's
v1.5.1 to pass it ([Hardware](hardware.md#what-every-proxy-runs)).

**Apple phones and watches** come in through Home Assistant's Private BLE
Device integration (their IRK) and appear in Bermuda automatically. The
heard list labels other Apple adverts by type so you can tell AirPods from
an iPhone from a Find My tag before tracking anything.
