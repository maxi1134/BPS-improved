"""Placement advice: which rooms the proxies serve worst, and where a proxy would help.

Pure geometry over the layout plus the self-test result; nothing here reads
Home Assistant. For every room on every floor it combines two views:

* measured: the leave-one-out self-test error of the proxies placed in the
  room (median and worst), which is how well the proxies there are placed
  and heard;
* geometric: for a grid of points inside the room, how far the third-nearest
  proxy on the floor is (a fix needs three) and whether the three nearest
  all sit on one side (a fix between proxies is well conditioned, a fix off
  to one side of them is not).

From those it names an issue per room - no proxy, a weak proxy, poor
coverage, one-sided geometry, or fine - and, for coverage and geometry,
picks wall spots (an outlet or a switch is on a wall) one at a time, each
the spot that best covers what is still uncovered, until the room is
covered or MAX_ADD spots are used.
"""

import math

import numpy as np
from shapely.geometry import Point, Polygon

GRID_M = 0.5            # interior sample spacing
WALL_STEP_M = 0.25      # candidate spacing along the walls
WALL_INSET_M = 0.12     # a wall-mounted proxy sits just inside the wall
TARGET_3RD_M = 5.0      # the third-nearest proxy should be within this of any point
ONE_SIDED_DEG = 300.0   # the three nearest all within a 60-degree cone: one-sided (tuned against rooms that measure well)
COVERED_FRAC = 0.85     # a room is covered when this share of its points pass both tests
MAX_ADD = 2
WEAK_M = 3.0            # a proxy this far off, and twice the room's median, is a weak proxy
GOOD_MEDIAN_M = 2.0     # a room measuring this well gets no coverage advice


def _ring(zone):
    return [(float(c["x"]), float(c["y"])) for c in zone.get("cords", []) if c.get("x") is not None and c.get("y") is not None]


def _largest(geom):
    """The biggest polygon part: repairing a self-crossing room, or insetting
    one with a narrow neck, yields a MultiPolygon, which has no exterior."""
    if geom.geom_type == "MultiPolygon":
        return max(geom.geoms, key=lambda g: g.area)
    return geom


def _rooms_of(floor):
    rooms = []
    for zone in floor.get("zones") or []:
        if zone.get("no_go") or len(_ring(zone)) < 3:
            continue
        poly = _largest(Polygon(_ring(zone)).buffer(0))
        if not poly.is_empty and poly.area > 0:
            rooms.append((str(zone.get("entity_id") or zone.get("zone_id")), poly))
    return rooms


def _grid(poly, scale):
    step = GRID_M * scale
    inner = poly.buffer(-0.2 * scale)
    if inner.is_empty:
        inner = poly
    minx, miny, maxx, maxy = poly.bounds
    pts = []
    y = miny + step / 2
    while y < maxy:
        x = minx + step / 2
        while x < maxx:
            if inner.contains(Point(x, y)):
                pts.append((x, y))
            x += step
        y += step
    if not pts:
        c = poly.centroid
        pts = [(c.x, c.y)]
    return np.array(pts, dtype=float)


def _coverage(grid, proxies, scale):
    """Per grid point: third-nearest distance (m) and the largest angular gap (deg) of the three nearest."""
    if len(proxies) < 3:
        return np.full(len(grid), np.inf), np.full(len(grid), 360.0)
    px = np.array(proxies, dtype=float)
    d = np.hypot(grid[:, None, 0] - px[None, :, 0], grid[:, None, 1] - px[None, :, 1]) / scale
    order = np.argsort(d, axis=1)[:, :3]
    d3 = np.take_along_axis(d, order[:, 2:3], axis=1)[:, 0]
    near = px[order]  # (points, 3, 2)
    ang = np.sort(np.degrees(np.arctan2(near[:, :, 1] - grid[:, None, 1], near[:, :, 0] - grid[:, None, 0])), axis=1)
    gaps = np.diff(np.concatenate([ang, ang[:, :1] + 360.0], axis=1), axis=1).max(axis=1)
    return d3, gaps


def _covered_share(grid, proxies, scale):
    d3, gaps = _coverage(grid, proxies, scale)
    ok = (d3 <= TARGET_3RD_M) & (gaps <= ONE_SIDED_DEG)
    return float(ok.mean()), d3, gaps


def _wall_candidates(poly, scale):
    ring = poly.exterior
    inner = _largest(poly.buffer(-WALL_INSET_M * scale))
    out = []
    n = max(4, int(ring.length / (WALL_STEP_M * scale)))
    for i in range(n):
        p = ring.interpolate(i / n, normalized=True)
        if inner.is_empty:
            out.append((p.x, p.y))
            continue
        q = inner.exterior.interpolate(inner.exterior.project(p))
        out.append((q.x, q.y))
    return out


def _suggest(poly, grid, proxies, scale):
    """Greedy wall spots until the room is covered, at most MAX_ADD."""
    have = list(proxies)
    spots = []
    share, _d3, _gaps = _covered_share(grid, have, scale)
    candidates = _wall_candidates(poly, scale)
    while share < COVERED_FRAC and len(spots) < MAX_ADD and candidates:
        best = None
        for c in candidates:
            s, d3, gaps = _covered_share(grid, have + [c], scale)
            # Tie-break on how far the worst point still is from its third proxy.
            key = (s, -float(np.clip(d3, 0, 50).max()))
            if best is None or key > best[0]:
                best = (key, c)
        (s, _), c = best
        if s <= share + 1e-9:
            break  # no wall spot helps any more
        spots.append({"x": round(c[0], 1), "y": round(c[1], 1)})
        have.append(c)
        share = s
    return spots, share


def _stats(errors):
    if not errors:
        return None, None
    arr = np.array(errors, dtype=float)
    return round(float(np.median(arr)), 3), round(float(arr.max()), 3)


def advise(layout, selftest, unplaced=None):
    """Advice rows, worst first, plus the unplaced proxies worth placing.

    ``layout`` is the stored layout, ``selftest`` a run_selftest result (its
    rows carry entity, floor, room, error_m), ``unplaced`` a list of
    {slug, name} for proxies that are heard but placed on no floor.
    """
    solved = [r for r in selftest.get("receivers", []) if isinstance(r.get("error_m"), (int, float))]
    rows = []
    for floor in layout.get("floor", []):
        scale = floor.get("scale")
        if not scale:
            continue
        name = floor.get("name")
        rooms = _rooms_of(floor)
        placed = [(float(r["cords"]["x"]), float(r["cords"]["y"]), str(r.get("entity_id")))
                  for r in floor.get("receivers", []) if isinstance(r.get("cords"), dict)]
        positions = [(x, y) for x, y, _s in placed]
        for room, poly in rooms:
            inside = [s for x, y, s in placed if poly.covers(Point(x, y))]
            errs = {r["entity"]: r["error_m"] for r in solved if r.get("floor") == name and r.get("room") == room}
            median_m, max_m = _stats(list(errs.values()))
            worst = max(errs, key=errs.get) if errs else None
            grid = _grid(poly, scale)
            share, d3, gaps = _covered_share(grid, positions, scale)
            row = {
                "floor": name, "room": room, "area_m2": round(poly.area / scale ** 2, 1),
                "proxies": len(inside), "solved": len(errs), "median_m": median_m, "max_m": max_m,
                "worst": worst, "covered": round(share, 2),
                "third_proxy_m": round(float(np.clip(d3, 0, 99).max()), 1) if len(d3) else None,
                "one_sided": round(float((gaps > ONE_SIDED_DEG).mean()), 2) if len(gaps) else None,
                "add": 0, "spots": [], "issue": "ok", "note": "",
            }
            # Weak proxies: far off, and far off compared with the rest of the
            # room without them (a plain median hides a bad pair among three).
            weak = []
            for slug, e in sorted(errs.items(), key=lambda kv: -kv[1]):
                rest = [v for k, v in errs.items() if k != slug]
                if rest and e >= WEAK_M and e >= 2 * float(np.median(rest)):
                    weak.append(slug)
            poor = share < COVERED_FRAC
            one_sided = row["one_sided"] or 0
            if not inside:
                row["issue"] = "no proxy"
                if poor:
                    row["spots"], _s = _suggest(poly, grid, positions, scale)
                    if not row["spots"]:
                        row["spots"] = [{"x": round(poly.centroid.x, 1), "y": round(poly.centroid.y, 1)}]
                    row["add"] = max(1, len(row["spots"]))
                    row["note"] = "no proxy in this room and the neighbours' do not reach all of it"
                else:
                    row["note"] = "no proxy in this room, but the neighbours' cover it; one here only buys spot-level accuracy"
            elif weak:
                row["issue"] = "weak proxy"
                row["weak"] = weak
                rest = [v for k, v in errs.items() if k not in weak]
                named = ", ".join(f"{w} ({errs[w]:.1f} m)" for w in weak)
                row["note"] = (f"{named} land far from where they are placed, the rest of the room {float(np.median(rest)):.1f} m: "
                               "check they are where the plan shows, and move them off metal and out of cupboards")
                if poor:
                    row["spots"], _s = _suggest(poly, grid, positions, scale)
                    row["add"] = len(row["spots"])
            elif (median_m is None or median_m > GOOD_MEDIAN_M) and poor:
                row["spots"], _s = _suggest(poly, grid, positions, scale)
                row["add"] = len(row["spots"])
                row["issue"] = "one-sided" if one_sided > 0.5 else "coverage"
                row["note"] = (f"the three nearest proxies sit to one side for {one_sided:.0%} of the room"
                               if row["issue"] == "one-sided"
                               else f"the third-nearest proxy is up to {row['third_proxy_m']} m away")
            elif median_m is not None and median_m > GOOD_MEDIAN_M:
                row["issue"] = "noisy"
                row["note"] = (f"covered, yet all {len(errs)} proxies here place about {median_m:.1f} m off: check this floor's scale and "
                               "that each proxy is where the plan shows, and set their mount heights")
                if len(inside) <= 3:
                    # One more opinion helps a room whose few proxies disagree: the wall spot farthest from them.
                    here = [(x, y) for x, y, s in placed if s in inside]
                    cands = _wall_candidates(poly, scale)
                    if cands and here:
                        far = max(cands, key=lambda c: min(math.hypot(c[0] - x, c[1] - y) for x, y in here))
                        row["spots"] = [{"x": round(far[0], 1), "y": round(far[1], 1)}]
                        row["add"] = 1
                        row["note"] += "; a fourth proxy on the far wall gives the room another opinion"
            rows.append(row)
    rank = {"no proxy": 0, "weak proxy": 1, "one-sided": 2, "coverage": 2, "noisy": 3, "ok": 4}
    rows.sort(key=lambda r: (rank[r["issue"]], -(r["median_m"] or 0), r["floor"], r["room"]))
    # Proxies heard but placed nowhere: hand each the next open spot, worst room first.
    unplaced = [dict(u) for u in (unplaced or [])]
    open_spots = [(r["floor"], r["room"], s) for r in rows for s in r["spots"]]
    for u, (fl, room, spot) in zip(unplaced, open_spots):
        u["suggest"] = {"floor": fl, "room": room, "x": spot["x"], "y": spot["y"]}
    return {"rooms": rows, "unplaced": unplaced,
            "summary": {"rooms": len(rows), "to_add": sum(r["add"] for r in rows),
                        "issues": {k: sum(1 for r in rows if r["issue"] == k) for k in rank if any(r["issue"] == k for r in rows)}}}
