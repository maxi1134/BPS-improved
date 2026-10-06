"""Room-stability metrics over recorder history (the panel's Health page).

The same numbers tools/flap_kpi.py prints from the command line, computed
here from Home Assistant's recorder so the panel can show them without a
token or a shell. The metric functions are pure and duplicated from the
tool on purpose: tools/ runs outside Home Assistant and must not import
the integration, and the integration must not import tools/.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime
from itertools import combinations

from shapely.geometry import Polygon

SUFFIXES = ("_sextant_room", "_sextant_floor")
# Older recorder history and saved baselines use the names before the
# BPS -> Sextant rename and before zones became rooms (3.8.0); one sensor
# under any of these names is the same sensor.
LEGACY_SUFFIXES = {"_bps_zone": "_sextant_room", "_sextant_zone": "_sextant_room", "_bps_floor": "_sextant_floor"}


def canonical(entity_id):
    """The current name of a zone/room/floor sensor whatever it was called when recorded."""
    for old, new in LEGACY_SUFFIXES.items():
        if entity_id.endswith(old):
            return entity_id[: -len(old)] + new
    return entity_id


def canonical_group(name):
    """Summary group name ("sextant_zone" -> "sextant_room")."""
    return canonical("_" + name).lstrip("_")
DEAD_STATES = {"unknown", "unavailable", "", None}

# Two rooms on one floor are neighbours when their outlines come within this
# many metres: touching rooms, and rooms either side of a hallway or a
# doorway strip too narrow to register as a room of its own. A move between
# rooms that are further apart than that, on the same floor, is a "far move".
# Walked, a far move takes long enough that the thing should have been seen
# somewhere between; a far move that was not is a position that jumped.
NEIGHBOUR_REACH_M = 2.5


def room_neighbours(layout, reach_m=NEIGHBOUR_REACH_M):
    """Which rooms count as neighbours, from the layout's room outlines.

    Returns ``{"floor": {room_name: floor_name}, "pairs": {frozenset(a, b)}}``
    for the rooms drawn on each floor. Room names are what the room sensors
    report as their state, so the result keys straight into recorder history.
    """
    floors, polys = {}, {}
    for floor in (layout or {}).get("floor") or []:
        scale = floor.get("scale")
        if not isinstance(scale, (int, float)) or scale <= 0:
            continue
        for zone in floor.get("zones") or []:
            pts = [(p["x"] / scale, p["y"] / scale) for p in zone.get("cords") or [] if "x" in p and "y" in p]
            name = zone.get("entity_id")
            if len(pts) < 3 or not name:
                continue
            poly = Polygon(pts).buffer(0)
            if poly.is_empty:
                continue
            polys[name] = poly
            floors[name] = floor.get("name")
    pairs = {
        frozenset((a, b))
        for a, b in combinations(polys, 2)
        if floors[a] == floors[b] and polys[a].distance(polys[b]) <= reach_m
    }
    return {"floor": floors, "pairs": pairs}


def _parse_ts(value):
    """ISO-8601 (HA's format, with offset) or an aware datetime -> aware datetime."""
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def compute_metrics(rows, window_hours=None, neighbours=None):
    """Stability metrics for one entity's history.

    ``rows`` is a chronological list of ``{"state", "last_changed"}`` (the
    first row being the state at the window start). Consecutive duplicate
    states collapse first, so a re-report of the same value is not a change.

    With ``neighbours`` (from ``room_neighbours``) the far moves are counted
    too: changes between two rooms on the same floor that are not
    neighbours. Without it, or for a sensor whose states are not rooms, the
    far-move fields are None.
    """
    seq = []
    for row in rows:
        state = row.get("state")
        ts = _parse_ts(row.get("last_changed") or row.get("last_updated"))
        if seq and seq[-1][1] == state:
            continue
        seq.append((ts, state))

    if window_hours:
        hours = float(window_hours)
    elif len(seq) >= 2:
        hours = max((seq[-1][0] - seq[0][0]).total_seconds() / 3600.0, 1e-9)
    else:
        hours = 0.0

    # A pass through unavailable or unknown is the sensor going away and coming
    # back - a restart, a reload - and says nothing about where the thing is.
    # Those rows go before anything is counted: Kitchen, unavailable, Kitchen is
    # no change at all, and Kitchen, unavailable, Office is the one change it
    # always was. Counted as they were, one restart added two changes to every
    # sensor, and a day with three restarts read as a fleet-wide flap.
    dead = sum(1 for _ts, s in seq if s in DEAD_STATES)
    live = []
    for ts, s in seq:
        if s in DEAD_STATES or (live and live[-1][1] == s):
            continue
        live.append((ts, s))
    seq = live
    changes = max(len(seq) - 1, 0)

    flips = sum(
        1
        for i in range(len(seq) - 2)
        if seq[i][1] == seq[i + 2][1] and seq[i][1] != seq[i + 1][1]
    )
    dwells = [(seq[i + 1][0] - seq[i][0]).total_seconds() for i in range(len(seq) - 1)]
    short = sum(1 for d in dwells if d < 60.0)

    transitions = Counter((seq[i][1], seq[i + 1][1]) for i in range(len(seq) - 1))
    pairs = Counter()
    for (a, b), n in transitions.items():
        pairs[tuple(sorted((a, b)))] += n

    far = None
    if neighbours:
        floor_of, near = neighbours.get("floor") or {}, neighbours.get("pairs") or set()
        placed = [(a, b) for (a, b), n in transitions.items() for _ in range(n) if a in floor_of and b in floor_of]
        if placed:
            far = sum(1 for a, b in placed if floor_of[a] == floor_of[b] and frozenset((a, b)) not in near)

    return {
        "changes": changes,
        "hours": round(hours, 3),
        "changes_per_hour": round(changes / hours, 2) if hours else None,
        "flips": flips,
        "flip_ratio": round(flips / changes, 3) if changes else None,
        "median_dwell_s": round(statistics.median(dwells), 1) if dwells else None,
        "short_dwell_ratio": round(short / len(dwells), 3) if dwells else None,
        "dead": dead,
        "states": len({s for _ts, s in seq if s not in DEAD_STATES}),
        "top_pairs": [{"pair": list(pair), "count": n} for pair, n in pairs.most_common(3)],
        "far_moves": far,
        "far_moves_per_day": round(far * 24.0 / hours, 2) if far is not None and hours else None,
        "far_move_ratio": round(far / changes, 3) if far is not None and changes else None,
    }


def summarise(per_entity):
    """Fleet-level roll-up: totals over every entity of one kind."""
    out = {}
    for suffix in SUFFIXES:
        members = {canonical(k): v for k, v in per_entity.items() if canonical(k).endswith(suffix)}
        if not members:
            continue
        changes = sum(m["changes"] for m in members.values())
        flips = sum(m["flips"] for m in members.values())
        hours = sum(m["hours"] for m in members.values())
        dwells = [m["median_dwell_s"] for m in members.values() if m["median_dwell_s"] is not None]
        far_known = [m for m in members.values() if m.get("far_moves") is not None]
        far = sum(m["far_moves"] for m in far_known)
        far_hours = sum(m["hours"] for m in far_known)
        out[suffix.lstrip("_")] = {
            "entities": len(members),
            "changes": changes,
            "changes_per_thing_hour": round(changes / hours, 2) if hours else None,
            "flip_ratio": round(flips / changes, 3) if changes else None,
            "median_of_median_dwell_s": round(statistics.median(dwells), 1) if dwells else None,
            "far_moves": far if far_known else None,
            "far_moves_per_thing_day": round(far * 24.0 / far_hours, 2) if far_known and far_hours else None,
        }
    return out


def rows_from_recorder(states):
    """Recorder rows (State objects, or minimal-response dicts) -> metric rows."""
    rows = []
    for item in states or []:
        if isinstance(item, dict):
            state = item.get("state")
            ts = item.get("last_changed") or item.get("last_updated")
        else:
            state = getattr(item, "state", None)
            ts = getattr(item, "last_changed", None) or getattr(item, "last_updated", None)
        if ts is None:
            continue
        rows.append({"state": state, "last_changed": ts})
    return rows


DELTA_METRICS = ("changes_per_hour", "flip_ratio", "median_dwell_s", "short_dwell_ratio", "dead", "far_moves_per_day")
SUMMARY_DELTA_METRICS = ("changes_per_thing_hour", "flip_ratio", "median_of_median_dwell_s", "far_moves_per_thing_day")


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def deltas(current, baseline):
    """current minus baseline, per entity and per summary group.

    Both are ``{"entities": {eid: metrics}, "summary": {group: metrics}}`` as
    ``compute_metrics``/``summarise`` produce them. Only entities and metrics
    present on both sides with numeric values appear; a thing added since
    the baseline simply has no delta row.
    """
    out = {"entities": {}, "summary": {}}
    cur_e = {canonical(k): v for k, v in ((current or {}).get("entities") or {}).items()}
    base_e = {canonical(k): v for k, v in ((baseline or {}).get("entities") or {}).items()}
    for eid, metrics in cur_e.items():
        base = base_e.get(eid)
        if not isinstance(base, dict) or not isinstance(metrics, dict):
            continue
        row = {}
        for key in DELTA_METRICS:
            a, b = _num(metrics.get(key)), _num(base.get(key))
            if a is not None and b is not None:
                row[key] = round(a - b, 4)
        if row:
            out["entities"][eid] = row
    cur_s = {canonical_group(k): v for k, v in ((current or {}).get("summary") or {}).items()}
    base_s = {canonical_group(k): v for k, v in ((baseline or {}).get("summary") or {}).items()}
    for group, metrics in cur_s.items():
        base = base_s.get(group)
        if not isinstance(base, dict) or not isinstance(metrics, dict):
            continue
        row = {}
        for key in SUMMARY_DELTA_METRICS:
            a, b = _num(metrics.get(key)), _num(base.get(key))
            if a is not None and b is not None:
                row[key] = round(a - b, 4)
        if row:
            out["summary"][group] = row
    return out
