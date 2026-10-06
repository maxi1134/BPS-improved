"""What a restart would otherwise throw away.

A cycle carries state that is not in the layout and not in any sensor: each
thing's Kalman filter, its room and spot elections with their smoothed
probabilities and timers, and when it arrived where it is. All of it is
built from the last few minutes of readings, so after a restart every thing
starts cold - rooms have to be re-earned, a spot cannot be held, and a
person follows whichever of their things happens to settle first. The house
did not change while Home Assistant was down; only Sextant's memory of it.

So the state is written to disk and read back if the gap was short. Read
back it is a starting point, never a lock: the elections carry on from
where they were and the next cycle of readings can disagree with them. A
longer gap keeps only when each thing was last heard and where, which is
what the Live page needs to say "away since 5:32 PM" instead of nothing.

Pure: the caller hands in the live dicts and gets plain JSON back.
"""

from __future__ import annotations

import math

# How long a gap still counts as "the house has not moved on": a restart, an
# update, a reboot. Past it the elections are stale and only the last sighting
# is kept.
DEFAULT_MAX_AGE_SECS = 300.0


# "There is no value here", as distinct from a value of None. A state dict is
# full of meaningful Nones - no challenger, not moving since, nothing pending -
# and dropping those keys hands the elections a dict they index by name and
# cannot read: the first st["still_since"] then throws the thing back to a cold
# start, which is the whole thing this module exists to avoid.
_DROP = object()


def _clean(value, depth=0):
    """JSON of a state dict: numbers, strings, bools, None, and lists of them.

    Anything else - a numpy array, an object, a NaN - is dropped rather than
    risk a store that cannot be written or read back.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else _DROP
    if depth >= 4:
        return _DROP
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            cleaned = _clean(v, depth + 1)
            if cleaned is not _DROP:
                out[str(k)] = cleaned
        return out
    if isinstance(value, (list, tuple)):
        return [None if (c := _clean(v, depth + 1)) is _DROP else c for v in value]
    if hasattr(value, "tolist"):          # numpy
        return _clean(value.tolist(), depth + 1)
    return _DROP


def _json(value):
    """``_clean`` for a caller that wants None rather than the sentinel."""
    cleaned = _clean(value)
    return None if cleaned is _DROP else cleaned


def _matrix(value, size):
    """``value`` as a size x size list of lists of finite floats, or None."""
    if not isinstance(value, (list, tuple)) or len(value) != size:
        return None
    out = []
    for row in value:
        if not isinstance(row, (list, tuple)) or len(row) != size:
            return None
        cleaned = [float(v) for v in row if isinstance(v, (int, float)) and math.isfinite(v)]
        if len(cleaned) != size:
            return None
        out.append(cleaned)
    return out


def _vector(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        return None
    out = [float(v) for v in value if isinstance(v, (int, float)) and math.isfinite(v)]
    return out if len(out) == size else None


def snapshot(now, kf=None, zones=None, spots=None, arrivals=None, rows=None, floors=None):
    """The state worth keeping, as JSON.

    ``rows`` are the published positions (ent, zone, sub_zone, floor, updated,
    cords): their last sighting is kept even when the rest is not.
    ``floors`` are the floor elections: {ent: {"name", "since", "probs"}}.
    Without them a restart elects every thing's floor from one cold cycle,
    and a floor that comes out differently discards the room and spot
    elections that were just restored - the floor change is what clears them.
    """
    things = {}

    def slot(entity):
        return things.setdefault(str(entity), {})

    for entity, state in (kf or {}).items():
        x, P = _json(state.get("x")), _json(state.get("P"))
        if _vector(x, 4) is None or _matrix(P, 4) is None:
            continue
        slot(entity)["kf"] = {"x": x, "P": P, "ts": state.get("ts"), "floor": state.get("floor")}
    for entity, state in (zones or {}).items():
        slot(entity)["zone"] = _json(state)
    for entity, state in (spots or {}).items():
        slot(entity)["spot"] = _json(state)
    for entity, state in (arrivals or {}).items():
        slot(entity)["arrived"] = _json(state)
    for entity, state in (floors or {}).items():
        slot(entity)["floor"] = _json(state)
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("ent"):
            continue
        cords = _json(row.get("cords"))
        slot(row["ent"])["last"] = {
            "zone": row.get("zone"), "spot": row.get("sub_zone"), "floor": row.get("floor"),
            "updated": row.get("updated"),
            "cords": cords if _vector(cords, 2) is not None else None,
        }
    return {"saved_at": float(now), "things": things}


def restore(data, now, max_age=DEFAULT_MAX_AGE_SECS):
    """``{"kf", "zone", "spot", "arrivals", "floors", "last", "age"}`` from a snapshot.

    Everything comes back after a short gap; after a long one only ``last``,
    so the Live page can still say where a thing was and when. A snapshot
    from the future (the clock moved) is treated as a long gap.
    """
    out = {"kf": {}, "zone": {}, "spot": {}, "arrivals": {}, "floors": {}, "last": {}, "age": None}
    if not isinstance(data, dict):
        return out
    saved_at = data.get("saved_at")
    things = data.get("things")
    if not isinstance(saved_at, (int, float)) or not math.isfinite(saved_at) or not isinstance(things, dict):
        return out
    age = float(now) - float(saved_at)
    out["age"] = age
    fresh = 0 <= age <= max(0.0, float(max_age))
    for entity, state in things.items():
        if not isinstance(state, dict):
            continue
        last = state.get("last")
        if isinstance(last, dict) and isinstance(last.get("updated"), (int, float)):
            out["last"][entity] = last
        if not fresh:
            continue
        kf = state.get("kf")
        if isinstance(kf, dict):
            x, P = _vector(kf.get("x"), 4), _matrix(kf.get("P"), 4)
            ts, floor = kf.get("ts"), kf.get("floor")
            if x and P and isinstance(ts, (int, float)) and isinstance(floor, str):
                out["kf"][entity] = {"x": x, "P": P, "ts": float(ts), "floor": floor}
        for key, target in (("zone", "zone"), ("spot", "spot"), ("arrived", "arrivals"),
                            ("floor", "floors")):
            value = state.get(key)
            if isinstance(value, dict) and value:
                out[target][entity] = value
    return out
