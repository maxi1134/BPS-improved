"""Zone geometry cleanup for the BPS floor-plan editor ("Adjust zones").

Given a floor's zones — independent polygons drawn by hand in the 2000px panel
coordinate space, with no shared-edge topology — this proposes a cleaner plan:

  1. squares rooms that are already nearly rectilinear (leaves L-shaped and
     diagonal rooms alone),
  2. snaps near-coincident boundaries so neighbours share edges — both
     corner-to-corner clusters (T1) and a corner meeting the middle of a
     neighbour's wall (T2, by inserting a vertex into that wall),
  3. removes overlaps deterministically,

then re-clamps sub-zones into their (possibly moved) parents. It is PURE
geometry: it returns proposed zones + sub-zones + a per-zone change report and
never persists anything. Tolerances are conservative so genuine gaps (a real
gap between a closet and the next room) are left untouched.

Depends only on shapely + the standard library, so it is unit-testable off HA.
"""

import math

from shapely.geometry import Polygon, MultiPolygon, Point, LineString
from shapely.validation import make_valid
from shapely.ops import unary_union

# All lengths are in the panel's 2000px coordinate space.
DEFAULTS = {
    "tolerance": 22.0,        # max gap/overlap span that counts as "should be shared"
    "square": True,
    "square_angle_deg": 8.0,  # an edge within this of the room axis is treated as axis-aligned
    "aligned_frac_min": 0.85, # square only rooms this rectilinear...
    "rectfill_min": 0.90,     # ...and this close to filling their bounding rectangle
    "min_edge": 3.0,          # drop shorter edges as degenerate
}


# --------------------------------------------------------------------------- #
# Polygon <-> cords helpers
# --------------------------------------------------------------------------- #
def _ring(cords, poly_flag):
    """(x,y) list for a zone's outer ring, normalizing legacy 4-point order.

    Non-finite / unparseable points are dropped so one bad coordinate can't
    crash the whole adjust (the zone just falls back to pass-through if too few
    points survive).
    """
    pts = []
    for p in cords:
        try:
            x, y = float(p["x"]), float(p["y"])
        except (TypeError, ValueError, KeyError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            pts.append((x, y))
    if not poly_flag and len(pts) == 4:
        # Legacy rectangles are stored TL,TR,BL,BR; reorder to a simple ring.
        pts = [pts[0], pts[1], pts[3], pts[2]]
    return pts


def _polygon(pts):
    pts = [(x, y) for (x, y) in pts if math.isfinite(x) and math.isfinite(y)]
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = make_valid(poly)
    return _largest_polygon(poly)


def _largest_polygon(geom):
    """Reduce a (possibly Multi) geometry to its largest simple Polygon."""
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]
    if not polys:
        return None
    return max(polys, key=lambda g: g.area)


def _out_cords(poly):
    """Outer ring as [{x,y}], without the duplicated closing point."""
    ring = list(poly.exterior.coords)[:-1]
    return [{"x": round(x, 3), "y": round(y, 3)} for x, y in ring]


def _dedupe_ring(pts, min_edge):
    """Drop consecutive points closer than min_edge (degenerate edges)."""
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_edge:
            out.append(p)
    if len(out) >= 2 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < min_edge:
        out.pop()
    return out


# --------------------------------------------------------------------------- #
# 1D / point clustering
# --------------------------------------------------------------------------- #
def _cluster_1d(values, tol):
    """Map each value to the mean of its run of within-tol neighbours (sorted)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    group = []
    for k, idx in enumerate(order):
        if group and values[idx] - values[order[k - 1]] > tol:
            mean = sum(values[g] for g in group) / len(group)
            for g in group:
                result[g] = mean
            group = []
        group.append(idx)
    if group:
        mean = sum(values[g] for g in group) / len(group)
        for g in group:
            result[g] = mean
    return result


class _UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# --------------------------------------------------------------------------- #
# Squaring
# --------------------------------------------------------------------------- #
def _dominant_axis(poly):
    mrr = poly.minimum_rotated_rectangle
    rc = list(mrr.exterior.coords)
    return math.atan2(rc[1][1] - rc[0][1], rc[1][0] - rc[0][0])  # radians


def _is_boxy(poly, axis, cfg):
    pts = list(poly.exterior.coords)
    total = aligned = 0.0
    axdeg = math.degrees(axis) % 90
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        length = math.hypot(dx, dy)
        total += length
        a = math.degrees(math.atan2(dy, dx)) % 90
        da = min(abs(a - axdeg), 90 - abs(a - axdeg))
        if da < cfg["square_angle_deg"]:
            aligned += length
    if total == 0:
        return False
    rectfill = poly.area / poly.minimum_rotated_rectangle.area
    return (aligned / total) >= cfg["aligned_frac_min"] and rectfill >= cfg["rectfill_min"]


def _square(poly, cfg):
    """Snap a rectilinear room's edges to its dominant axis. Returns (poly, did)."""
    axis = _dominant_axis(poly)
    if not _is_boxy(poly, axis, cfg):
        return poly, False
    cx, cy = poly.centroid.x, poly.centroid.y
    ext = list(poly.exterior.coords)[:-1]
    # Rotate into the room's own frame, cluster x's and y's (near-equal edges
    # become exactly axis-aligned), rotate back.
    ca, sa = math.cos(-axis), math.sin(-axis)
    rot = [((x - cx) * ca - (y - cy) * sa, (x - cx) * sa + (y - cy) * ca) for x, y in ext]
    xs = _cluster_1d([p[0] for p in rot], cfg["tolerance"])
    ys = _cluster_1d([p[1] for p in rot], cfg["tolerance"])
    cb, sb = math.cos(axis), math.sin(axis)
    out = [(cx + xs[i] * cb - ys[i] * sb, cy + xs[i] * sb + ys[i] * cb) for i in range(len(rot))]
    out = _dedupe_ring(out, cfg["min_edge"])
    sq = _polygon(out)
    if sq is None or sq.area < poly.area * 0.5:
        return poly, False  # squaring collapsed the room — keep the original
    return sq, True


# --------------------------------------------------------------------------- #
# Snapping (T1 corner clusters + T2 t-junctions)
# --------------------------------------------------------------------------- #
def _snap_boundaries(rings, cfg):
    """Weld near-coincident boundaries across zones.

    rings: list of [(x,y),...] outer rings (one per zone). Returns new rings.
    T1: cluster vertices from different zones within tolerance -> shared point.
    T2: a vertex near the interior of another zone's edge -> project onto it and
        insert a matching vertex into that edge so the boundary is shared.
    """
    tol = cfg["tolerance"]
    # Flat index of every vertex: (zone i, position k).
    index = [(i, k) for i, r in enumerate(rings) for k in range(len(r))]
    pos = {(i, k): rings[i][k] for (i, k) in index}

    # --- T1: cluster cross-zone vertices, but never merge two vertices of the
    #     SAME zone into one cluster — that would weld a room's own two corners
    #     to one point and silently drop part of the room. Weld nearest-first.
    uf = _UnionFind(len(index))
    comp_zones = {n: {index[n][0]} for n in range(len(index))}
    pairs = []
    for a in range(len(index)):
        ia, ka = index[a]
        for b in range(a + 1, len(index)):
            ib, kb = index[b]
            if ia == ib:
                continue
            (x1, y1), (x2, y2) = pos[(ia, ka)], pos[(ib, kb)]
            d = math.hypot(x1 - x2, y1 - y2)
            if d <= tol:
                pairs.append((d, a, b))
    for _d, a, b in sorted(pairs, key=lambda p: p[0]):
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb or (comp_zones[ra] & comp_zones[rb]):
            continue  # already together, or merging would duplicate a zone
        uf.union(a, b)
        comp_zones[uf.find(a)] = comp_zones[ra] | comp_zones[rb]
    groups = {}
    for n in range(len(index)):
        groups.setdefault(uf.find(n), []).append(index[n])
    target = dict(pos)
    for members in groups.values():
        if len({ik[0] for ik in members}) >= 2:  # spans >=2 zones -> shared corner
            mx = sum(pos[ik][0] for ik in members) / len(members)
            my = sum(pos[ik][1] for ik in members) / len(members)
            for ik in members:
                target[ik] = (mx, my)

    # --- T2: vertex near the interior of another zone's edge ---------------- #
    inserts = {}  # zone i -> list of (edge_start_k, point) to insert
    for (i, k) in index:
        if target[(i, k)] != pos[(i, k)]:
            continue  # already welded to a corner
        v = Point(pos[(i, k)])
        best = None
        for j, r in enumerate(rings):
            if j == i:
                continue
            for e in range(len(r)):
                a = r[e]
                b = r[(e + 1) % len(r)]
                seg = LineString([a, b])
                d = seg.distance(v)
                if d > tol:
                    continue
                proj = seg.project(v)
                if proj <= tol or proj >= seg.length - tol:
                    continue  # near an endpoint -> that's a T1 case, not T2
                if best is None or d < best[0]:
                    best = (d, j, e, seg.interpolate(proj))
        if best is not None:
            _, j, e, pt = best
            tp = (pt.x, pt.y)
            target[(i, k)] = tp
            inserts.setdefault(j, []).append((e, tp))

    # --- Rebuild rings: apply targets, then splice inserts ------------------ #
    new = [[target[(i, k)] for k in range(len(rings[i]))] for i in range(len(rings))]
    for j, items in inserts.items():
        # Insert each point after its edge-start, deepest offset first so
        # earlier insertions don't shift later indices within the same edge.
        by_edge = {}
        for e, pt in items:
            by_edge.setdefault(e, []).append(pt)
        ring = new[j]
        for e in sorted(by_edge, reverse=True):
            a = ring[e]
            pts = sorted(by_edge[e], key=lambda p: (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
            for pt in reversed(pts):
                ring.insert(e + 1, pt)
    return [_dedupe_ring(r, cfg["min_edge"]) for r in new]


# --------------------------------------------------------------------------- #
# Overlap removal
# --------------------------------------------------------------------------- #
def _remove_overlaps(polys, order):
    """Subtract already-finalized zones from each subsequent zone (stable order),
    so the output has no overlaps. Larger zones keep contested boundaries."""
    finalized = []
    result = [None] * len(polys)
    union = None
    for idx in order:
        p = polys[idx]
        if p is None:
            continue
        if union is not None and p.intersects(union):
            p = _largest_polygon(make_valid(p.difference(union)))
        if p is None or p.is_empty:
            result[idx] = polys[idx]  # never drop a room; fall back to original
            continue
        result[idx] = p
        finalized.append(p)
        union = p if union is None else unary_union([union, p])
    return result


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def adjust_zones(zones, subzones=None, options=None):
    """Return {'zones', 'subzones', 'changes', 'warnings'} for a floor.

    zones/subzones: lists of dicts as stored in bpsdata (entity_id, cords, poly,
    zone_id, ...). Input is not mutated.
    """
    cfg = dict(DEFAULTS)
    if options:
        cfg.update({k: v for k, v in options.items() if v is not None})
    subzones = subzones or []
    warnings = []

    # Load valid polygons; skip (pass through) any zone we cannot parse.
    orig, rings, keep = [], [], []
    for z in zones:
        pts = _ring(z.get("cords", []), z.get("poly"))
        poly = _polygon(pts)
        if poly is None:
            warnings.append(f"Skipped '{z.get('entity_id')}' (invalid polygon).")
            orig.append(None)
            rings.append(None)
            keep.append(z)
            continue
        orig.append(poly)
        rings.append(list(poly.exterior.coords)[:-1])
        keep.append(None)

    idxs = [i for i in range(len(zones)) if orig[i] is not None]

    # 1) Square boxy rooms.
    squared_flags = {i: False for i in idxs}
    if cfg["square"]:
        for i in idxs:
            sq, did = _square(orig[i], cfg)
            squared_flags[i] = did
            rings[i] = list(sq.exterior.coords)[:-1]

    # 2) Snap boundaries (T1 + T2) across the squared rings.
    sub_rings = [rings[i] for i in idxs]
    snapped = _snap_boundaries(sub_rings, cfg)
    for n, i in enumerate(idxs):
        rings[i] = snapped[n]

    polys = [None] * len(zones)
    for i in idxs:
        cand = _polygon(rings[i])
        # Guard against a snap that collapsed/distorted the room (e.g. a pinch
        # that make_valid split into lobes, keeping only the largest): keep the
        # original rather than silently shipping a fragment.
        if cand is not None and cand.area >= orig[i].area * 0.5:
            polys[i] = cand
        else:
            polys[i] = orig[i]
            warnings.append(f"'{zones[i].get('entity_id')}' left unchanged (adjust would have distorted it).")

    # 3) Remove overlaps (larger rooms win contested boundaries).
    order = sorted(idxs, key=lambda i: polys[i].area, reverse=True)
    polys = _remove_overlaps(polys, order)

    # Build output zones + change report.
    out_zones, changes = [], []
    new_parent_poly = {}
    for i, z in enumerate(zones):
        if orig[i] is None:  # passed through unchanged
            out_zones.append(dict(z))
            continue
        final = polys[i] or orig[i]
        nz = dict(z)
        nz["cords"] = _out_cords(final)
        nz["poly"] = True
        out_zones.append(nz)
        zid = z.get("zone_id") or z.get("entity_id")
        new_parent_poly[zid] = final
        moved = _max_vertex_move(orig[i], final)
        changes.append({
            "zone_id": zid,
            "name": z.get("entity_id"),
            "squared": squared_flags.get(i, False),
            "max_move_px": round(moved, 1),
            "area_change_pct": round((final.area - orig[i].area) / orig[i].area * 100, 1) if orig[i].area else 0.0,
            "vertices_before": len(orig[i].exterior.coords) - 1,
            "vertices_after": len(final.exterior.coords) - 1,
        })

    # 4) Re-clamp sub-zones into their (possibly moved) parent.
    out_subs = []
    for s in subzones:
        ns = dict(s)
        parent = new_parent_poly.get(s.get("parent"))
        spoly = _polygon(_ring(s.get("cords", []), s.get("poly")))
        if parent is not None and spoly is not None and not parent.covers(spoly):
            clipped = _largest_polygon(make_valid(spoly.intersection(parent)))
            if clipped is not None and not clipped.is_empty and clipped.area > 1:
                ns["cords"] = _out_cords(clipped)
                ns["poly"] = True
                warnings.append(f"Sub-zone '{s.get('entity_id')}' re-clamped into its parent.")
            else:
                warnings.append(f"Sub-zone '{s.get('entity_id')}' fell outside its parent after adjust.")
        out_subs.append(ns)

    return {"zones": out_zones, "subzones": out_subs, "changes": changes, "warnings": warnings}


def _max_vertex_move(a, b):
    """Largest distance from an original vertex to the nearest new-boundary point."""
    bb = b.exterior
    return max((Point(p).distance(bb) for p in list(a.exterior.coords)[:-1]), default=0.0)
