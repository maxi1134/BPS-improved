"""Truth marks: "it is actually here", and what to make of that.

A mark is a thing, a floor and a point the user vouches for, taken with
the solver inputs of the cycles around it (the per-proxy radii the fit
used and the thing's vector of ranges). With those inputs kept, the
same cycles can be re-solved under any settings, so a mark answers three
questions:

* which blend of geometric fit and fingerprint match, and which reference
  gain, puts THIS thing nearest the truth (``evaluate`` sweeps them);
* how far off the current settings are, in metres, per thing: an
  accuracy figure the stability KPI cannot give (``evaluate`` with the
  current settings only);
* what this thing's ranges look like at a known point: a fingerprint
  reference where no probe sits (``mark_reference``), in the same probe
  scale the receivers' references use.

Everything here is pure except ``Buffer``, which only holds recent
samples in memory; the solve itself is the caller's (``_solve_floor_jobs``
in __init__), passed in so this module never imports the integration.
"""

from __future__ import annotations

import math
import time
from collections import deque

from . import fingerprint

# Cycles kept per thing: twelve minutes at the default 15 s.
BUFFER_SAMPLES = 48
# The blend weights and gain multipliers a mark is evaluated over.
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)
GAIN_STEPS = (0.5, 0.7, 1.0, 1.4, 2.0)
# A mark needs at least this many usable cycles to say anything.
MIN_SAMPLES = 3
# Readings this long before a mark still describe the same spot (the user
# has been at the mark a while; the stationary lock says so too).
DEFAULT_WINDOW_SECS = 300.0


def estimator_for(weight):
    """The estimator a blend weight means: 0 is geometric alone, 1 fingerprint alone."""
    if weight is None:
        return None
    if weight <= 0.0:
        return "geometric"
    if weight >= 1.0:
        return "fingerprint"
    return "fused"


class Buffer:
    """The last cycles' solver inputs per thing, so a mark can be re-solved."""

    def __init__(self, maxlen=BUFFER_SAMPLES):
        self._by_entity = {}
        self._maxlen = maxlen

    def remember(self, entity, jobs, thing_vec, gain, estimator, now=None, raw_vec=None):
        floors = {}
        for job in jobs:
            # Tuples of floats, not lists: CPython's cyclic collector stops
            # tracking a tuple that holds only numbers, so these cost it nothing.
            # As lists, the buffer's 48 samples per thing were ~26,000 objects
            # for every full collection to walk (a quarter of a second each, on
            # a house with 400,000 of them in play).
            weighted = tuple(tuple(float(v) for v in pt) for pt in job.get("weighted") or ())
            if not weighted:
                continue
            bounds = job.get("bounds")
            floors[job["floor"]] = {
                "weighted": weighted,
                "bounds": None if bounds is None else tuple(float(v) for v in bounds),
                "min_wr": float(job.get("min_wr") or 1e-3),
                "scale": float(job.get("scale") or 0.0),
            }
        if not floors:
            return
        sample = {
            "t": float(now if now is not None else time.time()),
            "gain": float(gain),
            "estimator": estimator,
            "thing_vec": {str(k): float(v) for k, v in (thing_vec or {}).items()},
            "floors": floors,
        }
        if raw_vec:
            # The same readings before any calibration or per-thing trim, so a
            # mark can be re-based on whatever corrections are current (rebase).
            sample["raw_vec"] = {str(k): float(v) for k, v in raw_vec.items()}
        self._by_entity.setdefault(entity, deque(maxlen=self._maxlen)).append(sample)

    def samples(self, entity, since=None, floor=None):
        return [
            s for s in self._by_entity.get(entity, ())
            if (since is None or s["t"] >= since) and (floor is None or floor in s["floors"])
        ]

    def forget(self, entity=None):
        if entity is None:
            self._by_entity.clear()
        else:
            self._by_entity.pop(entity, None)


def rebase(samples, mult, rx_at, min_radius_m):
    """A mark's samples with the corrections in force NOW applied to their readings.

    A sample stores its ranges as the cycle used them, with that day's
    calibration and trims folded in; re-solving them after the corrections
    change would judge (and match against) the old corrections. Samples
    recorded since 3.17.7 carry ``raw_vec``, the readings before any of that;
    older ones have it recovered by dividing out the correction and trim in
    force now, which is exact unless those changed since the mark (and the
    correction then was applied in full: there was no close-range fade).

    ``mult(address, raw_m)`` is the multiplier the live path would apply to
    that reading now, or None for a receiver no longer placed;
    ``rx_at(floor, x, y)`` is ``(address, dz_m)`` for the receiver placed at
    that point (``dz_m`` None without a mount height), or None. Rows and
    readings it cannot place are kept as recorded.
    """
    out = []
    for s in samples or []:
        raw = s.get("raw_vec")
        if raw is None:
            raw = {}
            for rx, d in (s.get("thing_vec") or {}).items():
                m = mult(rx, None)
                if m and isinstance(d, (int, float)) and d > 0:
                    raw[rx] = float(d) / m
        if not raw:
            out.append(s)
            continue
        now_m = {}
        for rx, r in raw.items():
            m = mult(rx, r)
            if m:
                now_m[rx] = r * m
        floors = {}
        for name, fj in (s.get("floors") or {}).items():
            scale = float(fj.get("scale") or 0.0)
            rows = []
            for row in fj.get("weighted") or []:
                hit = rx_at(name, row[0], row[1]) if scale else None
                d = now_m.get(hit[0]) if hit else None
                if d is None:
                    rows.append(list(row))
                    continue
                dz = hit[1]
                h = d if dz is None else math.sqrt(max(d * d - dz * dz, min(d * d, min_radius_m * min_radius_m)))
                rows.append([row[0], row[1], h * scale, row[3], d * scale])
            floors[name] = {**fj, "weighted": rows}
        thing_vec = {rx: now_m.get(rx, d) for rx, d in (s.get("thing_vec") or {}).items()}
        out.append({**s, "thing_vec": thing_vec, "floors": floors})
    return out


def evaluate(samples, floor, mark, scale, zone_of, solve, refs_for_gain, k=3, missing_m=12.0,
             base_gain=1.0, weights=WEIGHTS, gains=GAIN_STEPS):
    """Re-solve ``samples`` (Buffer samples holding ``floor``) under every
    blend weight and gain multiplier, scored against ``mark`` (x, y in
    floor pixels; ``scale`` px per metre).

    ``zone_of(point)`` names the room a point is in (None outside every
    room); ``solve(jobs)`` is the per-floor solver; ``refs_for_gain(gain)``
    returns this floor's fingerprint references built with that gain, or
    None. Returns rows sorted best first: weight, gain multiplier, the
    estimator that weight means, samples used, mean and median error in
    metres, and the share of cycles that landed in the mark's room.
    """
    usable = [s for s in samples if floor in s.get("floors", {})]
    if not usable or not scale:
        return []
    mark_zone = zone_of(mark)
    rows = []
    for w in weights:
        for g in (gains if w > 0 else (1.0,)):
            gain = base_gain * g
            refs = refs_for_gain(gain) if w > 0 else None
            if w > 0 and not refs:
                continue
            jobs = []
            for s in usable:
                fj = s["floors"][floor]
                spec = None
                if w > 0 and s.get("thing_vec"):
                    spec = {
                        "mode": estimator_for(w), "thing": s["thing_vec"], "refs": refs,
                        "k": k, "missing_m": missing_m, "weight": w, "floor_weight": w, "gain": gain,
                    }
                jobs.append({
                    "floor": floor, "weighted": [tuple(pt) for pt in fj["weighted"]],
                    "bounds": None if fj.get("bounds") is None else tuple(fj["bounds"]),
                    "min_wr": fj["min_wr"], "stable_hint": None, "scale": fj["scale"] or scale,
                    "zone_polys": [], "fingerprint": spec,
                })
            errors, right = [], 0
            for outcome in solve(jobs):
                if outcome is None:
                    continue
                fix = outcome[0]
                errors.append(math.hypot(fix[0] - mark[0], fix[1] - mark[1]) / scale)
                if mark_zone is not None and zone_of(fix) == mark_zone:
                    right += 1
            if not errors:
                continue
            errors.sort()
            rows.append({
                "weight": w, "gain": round(g, 2), "estimator": estimator_for(w),
                "samples": len(errors),
                "mean_m": round(sum(errors) / len(errors), 2),
                "median_m": round(errors[len(errors) // 2], 2),
                "room_ok": round(right / len(errors), 2),
            })
    rows.sort(key=lambda r: (r["mean_m"], -r["room_ok"]))
    return rows


def mark_reference(mark, samples=None):
    """A fingerprint reference from a mark: the median of its samples' range
    vectors per receiving proxy, divided by the gain those cycles ran with
    so it sits in the probe scale ``build_references`` multiplies by the
    gain in force. None when the samples share fewer than two readings.
    ``samples`` overrides the mark's own (pass them rebased)."""
    per_rx = {}
    gain = 1.0
    for s in (samples if samples is not None else mark.get("samples")) or []:
        gain = float(s.get("gain") or gain)
        for rx, d in (s.get("thing_vec") or {}).items():
            if isinstance(d, (int, float)) and d > 0:
                # Each sample by its own cycle's gain: auto-gain moves it
                # between cycles, and dividing all of them by the last one
                # scaled the whole reference by that one cycle's value.
                per_rx.setdefault(rx, []).append(float(d) / max(gain, 1e-6))
    vector = {rx: fingerprint._median(v) for rx, v in per_rx.items() if len(v) >= 2}
    if not vector:
        return None
    return {
        "slug": f"mark:{mark['id']}", "address": None, "floor": mark["floor"],
        "x": float(mark["x"]), "y": float(mark["y"]), "vector": vector,
    }


def summarize(rows_by_mark):
    """Per-thing accuracy from {mark id: (entity, row)} evaluated at the
    current settings: marks, mean of the mean errors, share of cycles in the
    right room."""
    per_entity = {}
    for _mark_id, (entity, row) in rows_by_mark.items():
        if row is None:
            continue
        acc = per_entity.setdefault(entity, {"marks": 0, "mean_m": 0.0, "room_ok": 0.0})
        acc["marks"] += 1
        acc["mean_m"] += row["mean_m"]
        acc["room_ok"] += row["room_ok"]
    for acc in per_entity.values():
        acc["mean_m"] = round(acc["mean_m"] / acc["marks"], 2)
        acc["room_ok"] = round(acc["room_ok"] / acc["marks"], 2)
    return per_entity
