"""A floor's election bias as a field over its plan, rather than one number.

A floor's scalar ``bias`` says "things are usually on this floor". That is one
prior for the whole plan, and a house is not uniform: over a slab the floor
below is attenuated and the election has an easy time; beside a void - a
catwalk over a foyer, a gallery over a great room - both floors hear the thing
line-of-sight, fit it about equally well, and the election has nothing to
separate them with. The places where the scalar is wrong are *places*, so the
correction has to be able to vary by place.

The field is a coarse grid of multipliers laid over the floor's plan, sampled
at that floor's OWN candidate fix. That last part is what makes it workable:
floors live in separate pixel spaces and nothing registers one against
another, but a field never needs to cross floors. "If the Second Floor's solve
lands on the catwalk, believe it a bit more" and "if it lands out in the void,
where there is no floor to stand on, believe it a lot less" are both
statements about one floor's fix on that floor's plan.

It MULTIPLIES the scalar bias; it does not replace it. A flat field is all
ones and changes nothing, which is how it is meant to be introduced: lay the
flat field down, confirm nothing moved, then shape it from evidence. The scalar
stays the global knob and the field is local relief on top of it.

Stored on the floor as::

    "bias_field": {"cell_m": 1.0, "cell_px": 102.0, "x0": 120.0, "y0": 80.0,
                   "values": [[1.0, 1.0, ...], ...]}

``values[row][col]``; rows run down the plan (y), columns across (x). ``x0`` /
``y0`` are the pixel position of the grid's top-left corner. ``cell_m`` is the
cell edge asked for, in metres, so a grid means the same thing on a plan drawn
at any resolution; ``cell_px`` is what that came to on the day it was laid,
and is what the grid is actually drawn with. The distinction matters the
moment a floor's scale is corrected: the plan's pixels do not move, the rooms
drawn on it do not move, and a cell painted under the catwalk must stay under
the catwalk. A grid re-derived from the new scale would slide every painted
cell across the plan by the size of the correction.

Pure Python on purpose: the grid is tens of cells a side and is touched four
cells at a time, so there is nothing for numpy to speed up, and the module can
be tested - and used by offline tooling - without Home Assistant.
"""

from __future__ import annotations

import math

FIELD_KEY = "bias_field"

# Same ceiling the scalar bias allows. The floor is not zero: a zero would
# make a place unelectable outright, which no amount of evidence could
# overturn, and a prior should never be able to do that.
FIELD_MIN = 0.1
FIELD_MAX = 10.0

CELL_MIN_M = 0.25
CELL_MAX_M = 10.0
# A 250 x 250 grid is already a 250 m house at 1 m cells. The cap exists so a
# hand-edited layout cannot make every election walk a million-entry list.
MAX_CELLS_PER_SIDE = 250


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _cell_value(value) -> float:
    """One cell's multiplier; anything unusable reads as neutral."""
    number = _number(value)
    if number is None or number <= 0:
        return 1.0
    return min(FIELD_MAX, max(FIELD_MIN, number))


def parse(floor) -> dict | None:
    """The floor's field as ``{cell_px, x0, y0, rows, cols, values}``, or None.

    None for "no field", and equally for a field that cannot be used - a
    missing scale, a ragged grid, a nonsense cell size. A broken field must
    behave exactly like an absent one: the election falls back to the scalar
    bias rather than guessing at what a malformed grid meant.
    """
    if not isinstance(floor, dict):
        return None
    raw = floor.get(FIELD_KEY)
    if not isinstance(raw, dict):
        return None
    scale = _number(floor.get("scale"))
    cell_m = _number(raw.get("cell_m"))
    x0, y0 = _number(raw.get("x0")), _number(raw.get("y0"))
    values = raw.get("values")
    if scale is None or scale <= 0 or cell_m is None or x0 is None or y0 is None:
        return None
    if not CELL_MIN_M <= cell_m <= CELL_MAX_M:
        return None
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_CELLS_PER_SIDE:
        return None
    cell_px = _number(raw.get("cell_px"))
    if cell_px is None or cell_px <= 0:
        cell_px = cell_m * scale   # a field laid before cell_px was recorded
    cols = len(values[0]) if isinstance(values[0], list) else 0
    if not 1 <= cols <= MAX_CELLS_PER_SIDE:
        return None
    if any(not isinstance(row, list) or len(row) != cols for row in values):
        return None
    return {
        "cell_px": cell_px, "x0": x0, "y0": y0,
        "rows": len(values), "cols": cols, "values": values,
    }


def sample(floor, point) -> float:
    """The field's multiplier at ``point`` (this floor's pixels). 1.0 if none.

    Bilinear between cell centres, so the bias a fix receives is continuous in
    where the fix lands. That matters more than it looks: a candidate fix
    jitters by a good fraction of a metre from cycle to cycle, and a field
    with hard cell edges would turn that jitter into a score that flickers
    between two values. Beyond the outermost cell centres the edge value
    holds - a fix just off the grid is treated like the nearest place on it.
    """
    field = parse(floor)
    if field is None or point is None:
        return 1.0
    try:
        px, py = _number(point[0]), _number(point[1])
    except (TypeError, IndexError, KeyError):
        return 1.0
    if px is None or py is None:
        return 1.0
    cell, values = field["cell_px"], field["values"]
    # Continuous cell-centre coordinates: 0.0 is the centre of cell 0.
    gx = min(max((px - field["x0"]) / cell - 0.5, 0.0), field["cols"] - 1.0)
    gy = min(max((py - field["y0"]) / cell - 0.5, 0.0), field["rows"] - 1.0)
    c0, r0 = int(gx), int(gy)
    c1, r1 = min(c0 + 1, field["cols"] - 1), min(r0 + 1, field["rows"] - 1)
    fx, fy = gx - c0, gy - r0
    top = _cell_value(values[r0][c0]) * (1 - fx) + _cell_value(values[r0][c1]) * fx
    bottom = _cell_value(values[r1][c0]) * (1 - fx) + _cell_value(values[r1][c1]) * fx
    return top * (1 - fy) + bottom * fy


def flat(bounds_px, scale, cell_m=1.0, value=1.0) -> dict:
    """A uniform field covering ``bounds_px`` = (minx, miny, maxx, maxy).

    At the default value of 1 this is the no-op field: every sample is exactly
    1.0, so the election's arithmetic is unchanged to the last bit.
    """
    scale, cell_m = _number(scale), _number(cell_m)
    if scale is None or scale <= 0:
        raise ValueError("the floor has no scale; set it on the Edit page first")
    if cell_m is None or not CELL_MIN_M <= cell_m <= CELL_MAX_M:
        raise ValueError(f"cell_m must be between {CELL_MIN_M} and {CELL_MAX_M} m")
    minx, miny, maxx, maxy = (float(v) for v in bounds_px)
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)) or maxx <= minx or maxy <= miny:
        raise ValueError("the floor has no extent; place proxies or draw rooms first")
    cell_px = cell_m * scale
    cols = max(1, math.ceil((maxx - minx) / cell_px))
    rows = max(1, math.ceil((maxy - miny) / cell_px))
    if cols > MAX_CELLS_PER_SIDE or rows > MAX_CELLS_PER_SIDE:
        raise ValueError(
            f"a {cell_m} m grid over this floor is {cols} x {rows} cells; "
            f"use a larger cell_m (limit {MAX_CELLS_PER_SIDE} a side)"
        )
    fill = _cell_value(value)
    return {
        "cell_m": cell_m, "cell_px": round(cell_px, 4), "x0": round(minx, 3), "y0": round(miny, 3),
        "values": [[fill] * cols for _ in range(rows)],
    }


def _inside(px, py, polygon) -> bool:
    """Even-odd ray cast. ``polygon`` is a list of (x, y)."""
    hit = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def paint(floor, polygon, value, mode="set") -> int:
    """Write ``value`` into every cell whose centre lies inside ``polygon``.

    ``mode`` is "set" or "multiply" (relative to what the cell already holds,
    so two overlapping strokes compound instead of the later one winning).
    Edits the floor's field in place and returns how many cells changed hands.
    Zero is a real answer, not an error: a polygon smaller than one cell can
    fall between centres, and the caller should say so rather than report a
    stroke that did nothing as a success.
    """
    field = parse(floor)
    if field is None:
        raise ValueError("the floor has no usable bias field; lay a flat one first")
    number = _number(value)
    if number is None or number <= 0:
        raise ValueError("value must be a positive number")
    if mode not in ("set", "multiply"):
        raise ValueError('mode must be "set" or "multiply"')
    points = [(float(p[0]), float(p[1])) for p in polygon]
    if len(points) < 3:
        raise ValueError("a polygon needs at least three points")
    cell, values = field["cell_px"], field["values"]
    touched = 0
    for r in range(field["rows"]):
        cy = field["y0"] + (r + 0.5) * cell
        for c in range(field["cols"]):
            if _inside(field["x0"] + (c + 0.5) * cell, cy, points):
                base = _cell_value(values[r][c]) if mode == "multiply" else 1.0
                values[r][c] = round(min(FIELD_MAX, max(FIELD_MIN, base * number)), 4)
                touched += 1
    return touched


def describe(floor) -> dict | None:
    """Shape and spread of the floor's field, for logs and service responses."""
    field = parse(floor)
    if field is None:
        return None
    cells = [_cell_value(v) for row in field["values"] for v in row]
    return {
        "rows": field["rows"], "cols": field["cols"],
        "cell_m": floor[FIELD_KEY]["cell_m"],
        "min": min(cells), "max": max(cells),
        "flat": min(cells) == max(cells),
        "shaped_cells": sum(1 for v in cells if v != 1.0),
    }
