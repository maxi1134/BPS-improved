"""mmWave radars on the plan: their targets as points, and which thing is which.

An LD2450-type radar (Everything Presence Pro and Lite, Apollo R PRO-1) tracks
up to three people and reports each as x/y in its own frame: y straight out
from the sensor, x across it. Placed on the plan with the direction it faces,
each target becomes a point on the floor, to a few tens of centimetres - far
better than Bluetooth ranging manages. Sextant uses them three ways:

- a thing whose Bluetooth fix is near a target is placed on the target;
- one target and one thing, both still, is a location pin nobody had to tap;
- a target no thing claims is a person Sextant has no device for.

This module holds the geometry and the pairing; it imports nothing from the
rest of the package, so all of it is testable without Home Assistant.
"""
import math
import re

# The LD2450's field of view is ±60 degrees; its range is set on the device.
FOV_DEG = 120.0
DEFAULT_RANGE_M = 6.0
# A target this close to the sensor on both axes is the radar's "no target".
NO_TARGET_M = 0.05
# What the radar ranges to on a person: the chest, about a metre up standing
# (a little less sitting). A sensor mounted higher measures the slant to it.
TARGET_HEIGHT_M = 1.0

_TO_M = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048, "yd": 0.9144}


def to_metres(value, unit):
    """A reading in metres, or None when it is not a number in a length unit."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    factor = _TO_M.get(str(unit or "mm").strip().lower())
    return None if factor is None else v * factor


_TARGET = re.compile(r"target_?([123])_(x|y|speed|active)$")
_RANGE = re.compile(r"(tracking_detection_range|max_distance|detection_range)$")
_ANGLE = re.compile(r"(installation_angle|tracking_sensor_angle)$")


def device_spec(entity_ids):
    """A radar's entities, from the entity ids of one device.

    Returns {"targets": [{"x", "y", "speed", "active"} per target that has
    both x and y], "range_entity", "angle_entity"}, or None when the device
    reports no target coordinates (a plain presence sensor).
    """
    targets = {}
    range_entity = angle_entity = None
    for eid in sorted(entity_ids):
        obj = eid.split(".", 1)[-1]
        m = _TARGET.search(obj)
        if m:
            domain = eid.split(".", 1)[0]
            if m.group(2) == "active" and domain != "binary_sensor":
                continue
            targets.setdefault(int(m.group(1)), {})[m.group(2)] = eid
        elif eid.startswith("number.") and _RANGE.search(obj):
            range_entity = range_entity or eid
        elif eid.startswith("number.") and _ANGLE.search(obj):
            angle_entity = angle_entity or eid
    usable = [
        {"x": t["x"], "y": t["y"], "speed": t.get("speed"), "active": t.get("active")}
        for _n, t in sorted(targets.items()) if "x" in t and "y" in t
    ]
    if not usable:
        return None
    return {"targets": usable, "range_entity": range_entity, "angle_entity": angle_entity}


def radar_range_m(spec, get_state):
    """The radar's configured range, in metres (the chip's own 6 m when unset)."""
    st = get_state(spec.get("range_entity")) if spec.get("range_entity") else None
    if st is not None:
        r = to_metres(st.state, st.attributes.get("unit_of_measurement"))
        if r and 0.5 <= r <= 20:
            return r
    return DEFAULT_RANGE_M


def read_targets(spec, get_state, range_m=None):
    """The radar's current targets in its own frame: [(index, x_m, y_m, speed_ms)].

    A target counts when both coordinates are numbers, it is not at the
    origin (the radar's "none"), it is within range, and - where the device
    says - it is active.
    """
    rng = range_m if range_m is not None else radar_range_m(spec, get_state)
    out = []
    for i, t in enumerate(spec.get("targets") or [], start=1):
        if t.get("active"):
            act = get_state(t["active"])
            if act is None or act.state != "on":
                continue
        xs, ys = get_state(t["x"]), get_state(t["y"])
        if xs is None or ys is None:
            continue
        x = to_metres(xs.state, xs.attributes.get("unit_of_measurement"))
        y = to_metres(ys.state, ys.attributes.get("unit_of_measurement"))
        if x is None or y is None or (abs(x) < NO_TARGET_M and abs(y) < NO_TARGET_M):
            continue
        if math.hypot(x, y) > rng + 0.5:
            continue
        speed = None
        if t.get("speed"):
            ss = get_state(t["speed"])
            if ss is not None:
                unit = str(ss.attributes.get("unit_of_measurement") or "").lower()
                per = {"mm/s": 0.001, "cm/s": 0.01, "m/s": 1.0, "in/s": 0.0254, "mph": 0.44704, "km/h": 1 / 3.6}.get(unit)
                try:
                    speed = abs(float(ss.state)) * per if per else None
                except (TypeError, ValueError):
                    speed = None
        out.append((i, x, y, speed))
    return out


def floor_factor(x_m, y_m, height_m):
    """What turns a target's reported x/y into distances across the floor.

    The radar ranges in a straight line from where it is mounted to the
    person's chest (TARGET_HEIGHT_M), and keeps the angle; the floor distance
    is the slant range less that height difference, at the same angle. From
    2 m up, someone 3 m out reads 3.16 m (5 % long), someone 1 m out reads
    1.41 m (40 % long). Unset or no higher than a chest: no correction.
    """
    try:
        dh = float(height_m) - TARGET_HEIGHT_M
    except (TypeError, ValueError):
        return 1.0
    r = math.hypot(x_m, y_m)
    if dh <= 0 or r <= 0:
        return 1.0
    return math.sqrt(max(r * r - dh * dh, 0.0)) / r


def floor_range_m(range_m, height_m):
    """How far across the floor a slant range reaches (see floor_factor)."""
    return range_m * floor_factor(0.0, range_m, height_m)


def to_plan(radar, x_m, y_m, px_per_m):
    """A target in the radar's frame, on the plan (pixels).

    ``radar`` is the layout's radar: {"cords": {x, y}, "heading": degrees
    clockwise from straight up the plan, "flip": bool}. The target is y
    metres along the heading and x across it, to the right of the heading
    unless ``flip`` (sensors disagree on which side is positive x). With the
    radar's mounting ``height_m`` the slant it reports becomes floor distance
    (floor_factor).
    """
    k = floor_factor(x_m, y_m, radar.get("height_m"))
    x_m, y_m = x_m * k, y_m * k
    h = math.radians(float(radar.get("heading") or 0.0))
    fwd = (math.sin(h), -math.cos(h))
    right = (math.cos(h), math.sin(h))
    side = -1.0 if radar.get("flip") else 1.0
    cx, cy = float(radar["cords"]["x"]), float(radar["cords"]["y"])
    return (cx + px_per_m * (y_m * fwd[0] + side * x_m * right[0]),
            cy + px_per_m * (y_m * fwd[1] + side * x_m * right[1]))


def pair(targets, things, radius):
    """One-to-one pairs of targets and things, nearest first, within ``radius``.

    ``targets`` is [(key, (x, y))], ``things`` [(key, (x, y))], both in the
    same units as ``radius``. Returns ({target key: thing key}, [unpaired
    target keys]). Greedy on distance, which for a handful of each is the
    same answer the optimal assignment gives in all but contrived layouts.
    """
    cand = sorted(
        (math.hypot(tp[0] - hp[0], tp[1] - hp[1]), tk, hk)
        for tk, tp in targets for hk, hp in things
    )
    by_target, used = {}, set()
    for d, tk, hk in cand:
        if d > radius:
            break
        if tk in by_target or hk in used:
            continue
        by_target[tk] = hk
        used.add(hk)
    return by_target, [tk for tk, _p in targets if tk not in by_target]
