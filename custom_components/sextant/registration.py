"""How the floors of a house stack: one frame for all of them, from shared pins.

Each floor is drawn on its own plan, at its own resolution, cropped its own
way. Nothing says where the Second Floor's pixel (1232, 1277) is relative to
the Ground Floor's (1606, 682), so nothing that crosses floors can be
computed: two floors' fixes for the same thing cannot be compared, a proxy
on one floor cannot testify about a position on another, and a shape drawn on
one plan cannot be projected onto the next.

A *pin* is a named point on a plan. The same name on two floors says "these
are the same vertical line through the house" - an outside corner, a stair
newel, a chimney breast. From the pins this module fits, per floor, the rigid
motion (a rotation and a shift) that carries that floor's metres into a shared
*house frame*, which is simply the reference floor's own plan in metres.

Rigid, not similarity, on purpose. Every floor already has a scale the user
measured, and the solver works in that scale; if the pins quietly absorbed a
3 % scale error the floors would line up here while every distance on the
floor stayed 3 % wrong. So the scale is held, and the scale the pins WOULD
have chosen is reported beside the fit. With four or more pins that number is
an audit of the measured scale, and usually a better one than the single
tape measurement behind it.

Two shared pins determine a floor. More than two over-determine it, and the
leftover disagreement - reported per pin, in metres - is the quality of the
drawing: a pin that misses by 40 cm while the rest agree to 5 is a pin on the
wrong corner.

The vertical leg is not something pins can give: a floor's ``elevation`` is
metres above the reference floor's finished floor, entered by hand, defaulting
to a storey height per ``level``.

Pure Python, no Home Assistant, no numpy: four to eight points per floor.
"""

from __future__ import annotations

import math

PINS_KEY = "pins"
ELEVATION_KEY = "elevation"
# Finished floor to finished floor. A guess for a house nobody has measured;
# the Edit page asks for the real figure.
DEFAULT_STOREY_M = 3.0
MIN_SHARED_PINS = 2
# Above this the floors are not meaningfully registered and the fit should
# not be used for anything. Generous: a plan traced from a photo is off by
# tens of centimetres and is still far better than no registration at all.
USABLE_RMS_M = 0.75
# A set of pins "agrees" when a free-scale fit leaves them within this. Tighter
# than USABLE_RMS_M because it is judged with the scale free: what is left is
# then pure placement error, and corners clicked on a plan land within ~20 cm.
AGREE_RMS_M = 0.3
# Pins that may be set aside as misplaced, and how many must remain to trust
# the rest. Setting aside more than this is not finding outliers, it is
# choosing the answer.
MAX_SUSPECTS = 3
MIN_AGREEING = 4
# The suspect search tries every subset, which is nothing at eight pins and
# a million fits at two hundred. Past this many pins only single suspects are
# looked for: a floor pinned that densely has redundancy to spare, and the
# search must stay cheap enough to run while a pin is being dragged.
FULL_SEARCH_MAX_PINS = 16


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def elevation(floor) -> float:
    """Metres above the reference floor's finished floor."""
    explicit = _number(floor.get(ELEVATION_KEY)) if isinstance(floor, dict) else None
    if explicit is not None and -100 <= explicit <= 100:
        return explicit
    level = _number(floor.get("level")) if isinstance(floor, dict) else None
    return (level or 0.0) * DEFAULT_STOREY_M


def floor_pins(floor) -> dict[str, tuple[float, float]]:
    """``name -> (x_m, y_m)`` in the floor's own metres. First of a name wins."""
    scale = _number(floor.get("scale")) if isinstance(floor, dict) else None
    if scale is None or scale <= 0:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for pin in floor.get(PINS_KEY) or []:
        if not isinstance(pin, dict):
            continue
        name = pin.get("name")
        cords = pin.get("cords") if isinstance(pin.get("cords"), dict) else {}
        x, y = _number(cords.get("x")), _number(cords.get("y"))
        if not isinstance(name, str) or not name.strip() or x is None or y is None:
            continue
        out.setdefault(name.strip(), (x / scale, y / scale))
    return out


def _fit_rigid(pairs):
    """Least-squares rotation + shift carrying local metres onto house metres.

    ``pairs`` is ``[(name, (lx, ly), (hx, hy)), ...]``. Returns the motion, the
    per-pin miss in metres, and the scale a similarity fit would have picked
    (None when the pins are too close together to say).
    """
    n = len(pairs)
    lcx = sum(p[1][0] for p in pairs) / n
    lcy = sum(p[1][1] for p in pairs) / n
    hcx = sum(p[2][0] for p in pairs) / n
    hcy = sum(p[2][1] for p in pairs) / n
    dot = cross = spread = 0.0
    for _name, (lx, ly), (hx, hy) in pairs:
        ax, ay, bx, by = lx - lcx, ly - lcy, hx - hcx, hy - hcy
        dot += ax * bx + ay * by
        cross += ax * by - ay * bx
        spread += ax * ax + ay * ay
    theta = math.atan2(cross, dot)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    tx = hcx - (cos_t * lcx - sin_t * lcy)
    ty = hcy - (sin_t * lcx + cos_t * lcy)
    # Pins bunched within a metre of each other fix the shift but say nothing
    # trustworthy about rotation or scale; the caller is told via spread.
    implied = (dot * cos_t + cross * sin_t) / spread if spread > 1e-6 else None
    misses = {}
    for name, (lx, ly), (hx, hy) in pairs:
        px = cos_t * lx - sin_t * ly + tx
        py = sin_t * lx + cos_t * ly + ty
        misses[name] = math.hypot(px - hx, py - hy)
    return theta, tx, ty, misses, implied, math.sqrt(spread / n)


def _similarity_rms(pairs):
    """RMS miss (m) with rotation, shift AND scale free; and that scale."""
    n = len(pairs)
    lcx = sum(p[1][0] for p in pairs) / n
    lcy = sum(p[1][1] for p in pairs) / n
    hcx = sum(p[2][0] for p in pairs) / n
    hcy = sum(p[2][1] for p in pairs) / n
    dot = cross = spread = 0.0
    for _name, (lx, ly), (hx, hy) in pairs:
        ax, ay, bx, by = lx - lcx, ly - lcy, hx - hcx, hy - hcy
        dot += ax * bx + ay * by
        cross += ax * by - ay * bx
        spread += ax * ax + ay * ay
    if spread <= 1e-6:
        return float("inf"), None
    theta = math.atan2(cross, dot)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    s = (dot * cos_t + cross * sin_t) / spread
    total = 0.0
    for _name, (lx, ly), (hx, hy) in pairs:
        ax, ay = lx - lcx, ly - lcy
        px, py = s * (cos_t * ax - sin_t * ay) + hcx, s * (sin_t * ax + cos_t * ay) + hcy
        total += (px - hx) ** 2 + (py - hy) ** 2
    return math.sqrt(total / n), s


def _agreeing(pairs):
    """Split pins into the ones that agree with each other and the suspects.

    Least squares has no notion of a wrong pin: two pins on the wrong corner
    drag the whole fit toward themselves, every pin ends up missing by a metre,
    and the one reported "worst" is as likely a good pin as a bad one. So
    before fitting, look for the smallest set of pins whose removal leaves the
    rest in agreement - judged with the scale free, so that a wrong floor
    scale (which is a property of the floor, not of any pin) cannot make good
    pins look bad. Seven pins is a few dozen subsets; nothing here is slow.
    """
    from itertools import combinations  # noqa: PLC0415

    if len(pairs) < MIN_AGREEING + 1 or _similarity_rms(pairs)[0] <= AGREE_RMS_M:
        return pairs, []
    deepest = MAX_SUSPECTS if len(pairs) <= FULL_SEARCH_MAX_PINS else 1
    for k in range(1, min(deepest, len(pairs) - MIN_AGREEING) + 1):
        best = None
        for out in combinations(range(len(pairs)), k):
            keep = [p for i, p in enumerate(pairs) if i not in out]
            rms = _similarity_rms(keep)[0]
            if best is None or rms < best[0]:
                best = (rms, out)
        if best[0] <= AGREE_RMS_M:
            return ([p for i, p in enumerate(pairs) if i not in best[1]],
                    [pairs[i][0] for i in best[1]])
    return pairs, []   # no small set explains it: the floor as a whole disagrees


def solve(layout) -> dict:
    """Register every floor that can be. See ``report`` for the shape.

    The reference is the ``level`` 0 floor if it carries pins, else the floor
    with the most. Floors are then solved outward from it: a floor is solved
    once it shares two pins with floors already solved, so a basement that
    shares pins only with the ground floor, and an attic only with the floor
    below it, still end up in the one frame.
    """
    floors = [f for f in (layout.get("floor") if isinstance(layout, dict) else None) or [] if isinstance(f, dict)]
    pins = {f.get("name"): floor_pins(f) for f in floors}
    pinned = [f for f in floors if pins[f.get("name")]]
    result = {"reference": None, "floors": {}, "pins": {}}
    for f in floors:
        for name in pins[f.get("name")]:
            result["pins"].setdefault(name, []).append(f.get("name"))
    if not pinned:
        return result
    ground = [f for f in pinned if _number(f.get("level")) == 0]
    ref = ground[0] if ground else max(pinned, key=lambda f: len(pins[f.get("name")]))
    ref_name = ref.get("name")
    result["reference"] = ref_name

    frames = {ref_name: {
        "ok": True, "reference": True, "theta": 0.0, "tx": 0.0, "ty": 0.0,
        "scale": float(ref["scale"]), "elevation": elevation(ref),
        "pins": len(pins[ref_name]), "shared": len(pins[ref_name]),
        "rms_m": 0.0, "max_m": 0.0, "worst": None, "misses": {}, "implied_scale": None,
    }}
    # A pin's house position is set ONCE, by the floor nearest the reference
    # that carries it. Averaging it over every floor that has it sounds fairer
    # and is wrong: one badly scaled floor then shifts the target every other
    # floor is fitted to, and the basement's answer changes with the attic's.
    house: dict[str, tuple[float, float]] = dict(pins[ref_name])

    progress = True
    while progress:
        progress = False
        for f in pinned:
            name = f.get("name")
            if name in frames:
                continue
            pairs = [(pin, local, house[pin]) for pin, local in pins[name].items() if pin in house]
            if len(pairs) < MIN_SHARED_PINS:
                continue
            agreeing, suspects = _agreeing(pairs)
            theta, tx, ty, _m, _implied, spread = _fit_rigid(agreeing)
            cos_t, sin_t = math.cos(theta), math.sin(theta)
            # Every pin's miss against the fit the AGREEING pins made, so a
            # misplaced pin shows its real error instead of a share of it.
            misses = {
                pin: math.hypot(cos_t * lx - sin_t * ly + tx - hx, sin_t * lx + cos_t * ly + ty - hy)
                for pin, (lx, ly), (hx, hy) in pairs
            }
            used = {pin for pin, _l, _h in agreeing}
            rms = math.sqrt(sum(m * m for pin, m in misses.items() if pin in used) / len(used))
            worst = max(misses, key=misses.get)
            scale = float(f["scale"])
            free_rms, free_s = _similarity_rms(agreeing)
            implied = free_s if free_rms <= AGREE_RMS_M and len(agreeing) >= MIN_AGREEING else None
            frames[name] = {
                "ok": rms <= USABLE_RMS_M and spread >= 1.0,
                "reference": False, "theta": theta, "tx": tx, "ty": ty,
                "scale": scale, "elevation": elevation(f),
                "pins": len(pins[name]), "shared": len(pairs),
                "rms_m": rms, "max_m": misses[worst], "worst": worst,
                "misses": misses, "spread_m": spread, "suspects": suspects,
                # How well the agreeing pins fit with the scale FREE. When this
                # is small and rms_m is not, nothing is wrong with the pins:
                # the floor's scale is, and saying "check Pin 3" would send
                # the user hunting for a mistake they did not make.
                "agree_rms_m": free_rms if math.isfinite(free_rms) else None,
                # What this floor's px/m would be if the pins, not the tape
                # measure, had set it. Offered only when at least four pins
                # agree with the scale free: a number drawn from pins that do
                # not agree is noise with two decimal places.
                "implied_scale": scale / implied if implied and spread >= 2.0 and implied > 0 else None,
            }
            # Only a usable fit may place pins for the floors after it: a
            # rejected one would hand them its error, and a floor sharing
            # pins with it alone would then fit them "well" in the wrong place.
            if frames[name]["ok"]:
                for pin, (lx, ly) in pins[name].items():
                    house.setdefault(pin, (cos_t * lx - sin_t * ly + tx, sin_t * lx + cos_t * ly + ty))
            progress = True

    for f in floors:
        name = f.get("name")
        if name not in frames:
            shared = sum(1 for pin in pins[name] if pin in house)
            frames[name] = {
                "ok": False, "reference": False, "pins": len(pins[name]), "shared": shared,
                "elevation": elevation(f),
                "why": ("no scale" if not (_number(f.get("scale")) or 0) > 0 else
                        "no pins" if not pins[name] else
                        f"shares {shared} pin{'' if shared == 1 else 's'} with the registered floors; needs {MIN_SHARED_PINS}"),
            }
    result["floors"] = frames
    return result


def to_house(frame, x_px, y_px) -> tuple[float, float] | None:
    """A floor's pixel position in house metres, or None if unregistered."""
    if not frame or not frame.get("ok"):
        return None
    lx, ly = x_px / frame["scale"], y_px / frame["scale"]
    cos_t, sin_t = math.cos(frame["theta"]), math.sin(frame["theta"])
    return cos_t * lx - sin_t * ly + frame["tx"], sin_t * lx + cos_t * ly + frame["ty"]


def from_house(frame, hx, hy) -> tuple[float, float] | None:
    """House metres back onto a floor's plan, in that floor's pixels."""
    if not frame or not frame.get("ok"):
        return None
    dx, dy = hx - frame["tx"], hy - frame["ty"]
    cos_t, sin_t = math.cos(frame["theta"]), math.sin(frame["theta"])
    return (cos_t * dx + sin_t * dy) * frame["scale"], (-sin_t * dx + cos_t * dy) * frame["scale"]


def report(layout) -> dict:
    """``solve`` rounded for people: what the Edit page shows."""
    solved = solve(layout)
    floors = {}
    for name, frame in solved["floors"].items():
        row = {k: frame.get(k) for k in ("ok", "reference", "pins", "shared", "why", "worst", "suspects")}
        row["elevation"] = round(frame.get("elevation", 0.0), 3)
        if "rms_m" in frame:
            row["rms_m"] = round(frame["rms_m"], 3)
            agree = frame.get("agree_rms_m")
            row["agree_rms_m"] = None if agree is None else round(agree, 3)
            row["max_m"] = round(frame["max_m"], 3)
            row["rotation_deg"] = round(math.degrees(frame["theta"]), 2)
            row["misses"] = {pin: round(m, 3) for pin, m in frame["misses"].items()}
            implied = frame.get("implied_scale")
            row["implied_scale"] = None if implied is None else round(implied, 2)
            row["scale"] = round(frame["scale"], 2)
        floors[name] = row
    # Where every OTHER floor says each pin is, drawn onto this floor's plan:
    # the overlay that shows at a glance which pin is on the wrong corner.
    by_name = {f.get("name"): f for f in (layout.get("floor") if isinstance(layout, dict) else None) or [] if isinstance(f, dict)}
    for name, frame in solved["floors"].items():
        if not frame.get("ok"):
            continue
        ghosts = []
        for other, other_frame in solved["floors"].items():
            if other == name or not other_frame.get("ok"):
                continue
            scale = other_frame["scale"]
            for pin, (lx, ly) in floor_pins(by_name[other]).items():
                at = to_house(other_frame, lx * scale, ly * scale)
                here = from_house(frame, *at)
                ghosts.append({"name": pin, "floor": other, "x": round(here[0], 1), "y": round(here[1], 1)})
        floors[name]["ghosts"] = ghosts
    lonely = sorted(pin for pin, on in solved["pins"].items() if len(on) < 2)
    return {"reference": solved["reference"], "floors": floors, "pins": solved["pins"], "unlinked": lonely}
