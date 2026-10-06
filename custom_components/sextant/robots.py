"""Robot vacuums on the floor plan: lining a vacuum's own map up with a floor.

A Roborock knows exactly where it is on its own map - millimetres from an
origin near where it first mapped, in a frame of its own choosing - and names
the rooms of that map. Sextant's floor plan is in pixels, drawn off a
drawing. The two share the rooms. So each room both maps name gives one pair
of points (the centre of the room's bounding box on each), the dock the user
marks gives one more, and a rotation, translation and mirror fitted to the
pairs puts the robot on the plan wherever it goes.

The scale is not fitted: the robot's millimetres are real millimetres and the
floor's scale (pixels per metre) is what the user set, so the fit is rigid,
and how well the pairs agree under it (``rms_m``, and the scale they would
imply) says whether the two maps really are the same house.

This module imports nothing from the rest of the package.
"""
import math
import re

# The dock is a point the user placed on the plan and the robot reports to the
# millimetre; a room's box centre is only as good as two maps agree on what
# the room is. So the dock counts for this many rooms.
DOCK_WEIGHT = 3.0
# A matched room whose centre lands this far from its counterpart after the
# fit (and further than OUTLIER_MEDIAN_X times the median) is two maps
# disagreeing about a room - Roborock's Foyer is the hall by the front door,
# Sextant's is the whole open ground floor - and is dropped before refitting.
OUTLIER_M = 1.5
OUTLIER_MEDIAN_X = 2.5
MIN_PAIRS = 2


def norm(name):
    """A room name reduced for matching: "Sun Room", "Sunroom" and "sun_room"
    are one room."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


def plan_rooms(floor):
    """[(name, area_id, (cx, cy))] for a floor's rooms: the centre of each
    allowed room's bounding box, in plan pixels. Bounding boxes, because that
    is what the robot's map gives for its rooms."""
    out = []
    for zone in (floor or {}).get("zones") or []:
        cords = zone.get("cords") or []
        if zone.get("no_go") or len(cords) < 3:
            continue
        xs = [float(c["x"]) for c in cords]
        ys = [float(c["y"]) for c in cords]
        out.append((zone.get("entity_id"), zone.get("area_id"), ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)))
    return out


def match_rooms(robot_rooms, floor):
    """[(robot room name, plan room name, map (x, y), plan (x, y))] for the
    rooms both maps name. A robot room matches a plan room by name, or by the
    Home Assistant area the plan room is linked to ("Jack Bedroom" is the
    room linked to area jack_bedroom). Each plan room is used once."""
    by_key = {}
    for name, area, centre in plan_rooms(floor):
        for key in (norm(name), norm(area)):
            if key and key not in by_key:
                by_key[key] = (name, centre)
    used, out = set(), []
    for room in robot_rooms or []:
        key = norm(room.get("name"))
        hit = by_key.get(key)
        if not key or hit is None or hit[0] in used:
            continue
        try:
            centre = ((float(room["x0"]) + float(room["x1"])) / 2, (float(room["y0"]) + float(room["y1"])) / 2)
        except (KeyError, TypeError, ValueError):
            continue
        used.add(hit[0])
        out.append((room.get("name"), hit[0], centre, hit[1]))
    return out


def _fit(pairs, px_per_mm, reflect):
    """Rigid fit at a fixed scale: (transform, residuals_m). ``pairs`` are
    (map (x, y), plan (x, y), weight)."""
    w = sum(p[2] for p in pairs)
    sign = -1.0 if reflect else 1.0
    a = [(px_per_mm * m[0], sign * px_per_mm * m[1]) for m, _p, _w in pairs]
    ax = sum(p[2] * v[0] for p, v in zip(pairs, a)) / w
    ay = sum(p[2] * v[1] for p, v in zip(pairs, a)) / w
    bx = sum(p[2] * p[1][0] for p in pairs) / w
    by = sum(p[2] * p[1][1] for p in pairs) / w
    s_cross = s_dot = 0.0
    for p, v in zip(pairs, a):
        ux, uy = v[0] - ax, v[1] - ay
        vx, vy = p[1][0] - bx, p[1][1] - by
        s_dot += p[2] * (ux * vx + uy * vy)
        s_cross += p[2] * (ux * vy - uy * vx)
    theta = math.atan2(s_cross, s_dot)
    c, s = math.cos(theta), math.sin(theta)
    tx, ty = bx - (c * ax - s * ay), by - (s * ax + c * ay)
    t = {"scale": px_per_mm, "theta": theta, "reflect": reflect, "tx": tx, "ty": ty}
    px_per_m = px_per_mm * 1000.0
    res = []
    for m, p, _w in pairs:
        q = apply(t, m)
        res.append(math.hypot(q[0] - p[0], q[1] - p[1]) / px_per_m)
    return t, res


def apply(transform, point):
    """A map point (millimetres) on the plan (pixels)."""
    s, c, n = transform["scale"], math.cos(transform["theta"]), math.sin(transform["theta"])
    x = s * float(point[0])
    y = (-1.0 if transform.get("reflect") else 1.0) * s * float(point[1])
    return (c * x - n * y + transform["tx"], n * x + c * y + transform["ty"])


def fit(matches, px_per_m, dock=None):
    """Line a robot's map up with a floor.

    ``matches`` are match_rooms' pairs; ``dock`` is ((map x, y), (plan x, y))
    when the user has marked it. Both mirror senses are tried and the better
    one kept; rooms that disagree by more than OUTLIER_M (and OUTLIER_MEDIAN_X
    times the median) are dropped and the fit repeated, the dock never.
    Returns None with fewer than MIN_PAIRS pairs, else a dict with the
    transform, the rms and each pair's residual in metres, the pairs dropped,
    and the scale the kept pairs would imply over the one used (1.0 is
    agreement; far from it means the floor's scale or the match is wrong).
    """
    px_per_mm = float(px_per_m) / 1000.0
    pairs = [(m, p, 1.0, f"{rn} = {pn}") for rn, pn, m, p in matches]
    if dock is not None:
        pairs.append((tuple(dock[0]), tuple(dock[1]), DOCK_WEIGHT, "dock"))
    if len(pairs) < MIN_PAIRS:
        return None
    dropped = []
    while True:
        core = [(m, p, w) for m, p, w, _l in pairs]
        best = None
        for reflect in (True, False):   # the robot's y points up the plan's points down: try mirrored first
            t, res = _fit(core, px_per_mm, reflect)
            rms = math.sqrt(sum(w * r * r for (_m, _p, w), r in zip(core, res)) / sum(w for _m, _p, w in core))
            if best is None or rms < best[2] - 1e-9:
                best = (t, res, rms)
        t, res, rms = best
        rooms = [(i, r) for i, r in enumerate(res) if pairs[i][3] != "dock"]
        if len(pairs) <= 3 or not rooms:
            break
        median = sorted(r for _i, r in rooms)[len(rooms) // 2]
        worst_i, worst = max(rooms, key=lambda ir: ir[1])
        if worst <= OUTLIER_M or worst <= OUTLIER_MEDIAN_X * median:
            break
        dropped.append({"pair": pairs[worst_i][3], "residual_m": round(worst, 2)})
        pairs.pop(worst_i)
    # The scale the kept pairs would choose for themselves, against the one used.
    sign = -1.0 if t["reflect"] else 1.0
    cx = sum(p[1][0] for p in pairs) / len(pairs); cy = sum(p[1][1] for p in pairs) / len(pairs)
    mx = sum(p[0][0] for p in pairs) / len(pairs); my = sum(sign * p[0][1] for p in pairs) / len(pairs)
    plan_spread = sum(math.hypot(p[1][0] - cx, p[1][1] - cy) for p in pairs)
    map_spread = sum(math.hypot(p[0][0] - mx, sign * p[0][1] - my) for p in pairs) * px_per_mm
    return {
        "transform": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in t.items()},
        "rms_m": round(rms, 3),
        "pairs": [{"pair": p[3], "residual_m": round(r, 2)} for p, r in zip(pairs, res)],
        "dropped": dropped,
        "scale_ratio": round(plan_spread / map_spread, 3) if map_spread > 0 else None,
    }
