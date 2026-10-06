"""Point-in-time copies of the layout, so that a bad save can be undone.

The layout is the hand-drawn part of Sextant - rooms traced off a floor plan,
spots sized to real furniture, every proxy placed and its height measured. It
is hours of work and nothing else can regenerate it. It is also written by
several callers (the editor, calibration, the truth-mark applier), any of which
can be wrong: two bedside-table spots once lost their corners and collapsed to
a single point, which draws as nothing at all, and the only copy of the
original was in a Home Assistant backup that turned out to be encrypted.

So every save puts the *outgoing* layout in ``config/sextant_snapshots`` first.
They are small (tens of KB), identical ones are not kept twice, and they are
thinned as they age - everything from the last day, hourly for a week, daily
for a quarter - which lands at a few hundred files and a handful of MB.

This module imports nothing from the rest of the package: ``storage`` owns the
layout and calls in here, and the decisions below stay testable without Home
Assistant or a filesystem.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_DIRNAME = "sextant_snapshots"
KEEP_ALL_HOURS = 24        # every save of the last day
KEEP_HOURLY_DAYS = 7       # then one an hour
KEEP_DAILY_DAYS = 90       # then one a day, and nothing older

# <epoch>-v<layout version>.json — the version is for reading the directory by
# eye, the epoch is what the thinning sorts on.
_NAME = re.compile(r"^(\d+)-v(\d+)\.json$")


def snapshot_name(stamp: float, version: int) -> str:
    return f"{int(stamp)}-v{int(version)}.json"


def parse_name(name: str):
    """``(epoch, version)`` for a snapshot filename, or ``None`` if it is not one."""
    m = _NAME.match(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def keepers(stamps, now: float) -> set:
    """Which snapshot timestamps survive thinning.

    Dense where you are most likely to want them - anything from today is kept
    exactly as saved - and progressively coarser going back, so a year of edits
    costs a few hundred files rather than tens of thousands. The newest is
    always kept, however old it is: a layout nobody has touched in a year still
    deserves one copy.
    """
    stamps = list(stamps)
    if not stamps:
        return set()
    keep, seen = set(), set()
    for s in sorted(stamps, reverse=True):
        age = now - s
        if age < KEEP_ALL_HOURS * 3600:
            keep.add(s)
            continue
        if age < KEEP_HOURLY_DAYS * 86400:
            bucket = ("h", int(s // 3600))
        elif age < KEEP_DAILY_DAYS * 86400:
            bucket = ("d", int(s // 86400))
        else:
            continue
        if bucket not in seen:
            seen.add(bucket)
            keep.add(s)
    keep.add(max(stamps))
    return keep


def _shapes(layout) -> dict:
    """Every drawn polygon in a layout, as ``(floor, kind, name) -> point count``."""
    out = {}
    if not isinstance(layout, dict):
        return out
    for floor in layout.get("floor") or []:
        if not isinstance(floor, dict):
            continue
        name = str(floor.get("name"))
        for kind, key, idkey in (("room", "zones", "zone_id"), ("spot", "subzones", "sub_zone_id")):
            for item in floor.get(key) or []:
                if not isinstance(item, dict):
                    continue
                label = item.get("entity_id") or item.get(idkey)
                out[(name, kind, str(label))] = len(item.get("cords") or [])
    return out


def degraded(before, after) -> list:
    """What a save is about to break, in words; empty when it breaks nothing.

    Only one thing is reported, because only one thing is never intentional: a
    polygon that had three or more corners coming back with fewer. Three is the
    minimum to enclose any area, so one or two corners draw as nothing and the
    shape silently disappears from the plan. Deleting a room or a spot outright
    is a thing people mean to do, so it is not reported here.
    """
    was, now = _shapes(before), _shapes(after)
    lost = []
    for key, points in was.items():
        floor, kind, label = key
        after_points = now.get(key)
        if points >= 3 and after_points is not None and after_points < 3:
            lost.append(
                f"{kind} {label!r} on {floor} drops from {points} corners to "
                f"{after_points} and would no longer be drawn"
            )
    return lost


def snapshot_dir(hass) -> str:
    return hass.config.path(SNAPSHOT_DIRNAME)


def _scan(path: Path) -> list:
    """``(epoch, version, Path)`` for every snapshot in the directory, newest first.

    Snapshots are a handful of small files, so the directory work is a single
    hop to a thread rather than anything cleverer.
    """
    try:
        names = [p.name for p in path.iterdir()]
    except (FileNotFoundError, NotADirectoryError):
        return []
    rows = [(parsed[0], parsed[1], path / name)
            for name in names if (parsed := parse_name(name))]
    rows.sort(reverse=True)
    return rows


async def _listing(path: Path) -> list:
    return await asyncio.to_thread(_scan, path)


async def take(hass, layout, version: int, now: float | None = None) -> str | None:
    """Put ``layout`` in the snapshot directory and thin what is already there.

    Returns the filename written, or ``None`` when there was nothing worth
    writing (no layout yet, or byte-identical to the newest snapshot - saving
    the layout does not always change it, and a shelf of identical copies would
    just push the useful history out of the retention window).

    Never raises: a snapshot is insurance, and insurance that can fail the save
    it is protecting is worse than none.
    """
    try:
        if not isinstance(layout, dict) or not layout.get("floor"):
            return None
        path = Path(snapshot_dir(hass))
        body = json.dumps(layout, separators=(",", ":"), sort_keys=True)
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        rows = await _listing(path)
        if rows and await asyncio.to_thread(rows[0][2].read_text) == body:
            return None
        now = time.time() if now is None else now
        # Two saves inside the same second would collide on the name, so the
        # second one takes the next free second. The stamp is for ordering and
        # thinning, not for timing anything.
        stamp = int(now)
        taken = {r[0] for r in rows}
        while stamp in taken:
            stamp += 1
        name = snapshot_name(stamp, version)
        await asyncio.to_thread((path / name).write_text, body)
        await _prune(path, rows + [(stamp, version, path / name)], now)
        return name
    except Exception as e:  # noqa: BLE001 - see the docstring
        _LOGGER.warning("Could not snapshot the layout: %s", e)
        return None


def _prune_sync(rows, now: float) -> None:
    keep = keepers([r[0] for r in rows], now)
    for stamp, _version, file in rows:
        if stamp in keep:
            continue
        try:
            file.unlink()
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Could not remove old snapshot %s: %s", file.name, e)


async def _prune(path: Path, rows, now: float) -> None:
    await asyncio.to_thread(_prune_sync, rows, now)


def _listing_sync(path: Path) -> list:
    out = []
    for stamp, version, file in _scan(path):
        try:
            size = file.stat().st_size
        except OSError:
            size = None
        out.append({"id": file.name, "at": stamp, "version": version, "bytes": size})
    return out


async def listing(hass) -> list:
    """Every snapshot, newest first, as dicts the panel can show."""
    return await asyncio.to_thread(_listing_sync, Path(snapshot_dir(hass)))


async def read(hass, name: str):
    """One snapshot's layout. Raises ValueError when the name is not one of ours."""
    if not parse_name(name):
        raise ValueError("not a snapshot name")
    # parse_name only admits digits, "-v" and ".json", so the name cannot carry
    # a separator and the join cannot climb out of the directory.
    path = Path(snapshot_dir(hass)) / name
    return json.loads(await asyncio.to_thread(path.read_text))
