"""
Compare the pure-numpy solver against scipy on REAL floorplan geometry.

Answers the only question that matters before dropping scipy: does the
replacement put trackers in the same place?

Receiver coordinates and per-floor scales come from an actual install
(`real_layout.json`, exported from `.storage/bps`), so the geometry —
receiver spacing, collinearity, map extent — is the real thing rather than a
synthetic grid. For each trial a true position is drawn inside the map,
distances to every receiver are computed, and realistic corruption is applied:

  * log-normal multiplicative noise, because BLE range error is proportional
    rather than additive;
  * a configurable fraction of gross outliers (a receiver reading several
    times too far — through-wall, or a stale reading), which is exactly what
    the soft_l1 loss exists to survive;
  * a limited receiver subset, since only some receivers hear a given tracker.

Both solvers get identical inputs, the same analytic Jacobian and the same
1/r^2 weighting used in `trilaterate()`. Deltas are reported in METRES via the
floor scale, since pixels are meaningless for judging "is this good enough".

Usage:
    python tools/solver_bench.py [--trials 2000] [--outlier-rate 0.15] [--seed 7]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from scipy.optimize import least_squares

# Load solver_numpy by PATH rather than as `bps.solver_numpy`: importing the
# package would execute bps/__init__.py, which pulls in aiofiles and the whole
# Home Assistant runtime. The solver module itself only needs numpy.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "bps_solver_numpy",
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "bps", "solver_numpy.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
least_squares_bounded_soft_l1 = _mod.least_squares_bounded_soft_l1
_soft_l1_cost = _mod._soft_l1_cost

# Mirrors the constants in bps/__init__.py.
SOLVER_ROBUST_F_SCALE = 0.3
MIN_WEIGHT_RADIUS_M = 0.5
_JAC_MIN_DIST = 1e-9


def build_problem(rng, receivers, scale, n_heard, noise_sigma, outlier_rate):
    """One synthetic-but-realistic fix: returns (known_points, bounds, truth)."""
    minx, maxx = receivers[:, 0].min(), receivers[:, 0].max()
    miny, maxy = receivers[:, 1].min(), receivers[:, 1].max()
    truth = np.array([rng.uniform(minx, maxx), rng.uniform(miny, maxy)])

    idx = rng.choice(len(receivers), size=min(n_heard, len(receivers)), replace=False)
    pts = receivers[idx]
    true_d = np.hypot(pts[:, 0] - truth[0], pts[:, 1] - truth[1])

    # Proportional (log-normal) error, as BLE range error scales with range.
    meas = true_d * np.exp(rng.normal(0.0, noise_sigma, size=len(pts)))
    # Gross outliers: a receiver reading 2-6x too far.
    n_out = int(round(outlier_rate * len(pts)))
    if n_out:
        out_idx = rng.choice(len(pts), size=n_out, replace=False)
        meas[out_idx] *= rng.uniform(2.0, 6.0, size=n_out)

    known = [(float(p[0]), float(p[1]), float(r)) for p, r in zip(pts, meas)]
    return known, (minx, miny, maxx, maxy), truth


def make_callables(known_points, min_weight_radius):
    """The exact residual/Jacobian/x0 construction used by trilaterate()."""
    n = len(known_points)
    px = np.fromiter((p[0] for p in known_points), dtype=float, count=n)
    py = np.fromiter((p[1] for p in known_points), dtype=float, count=n)
    pr = np.fromiter((p[2] for p in known_points), dtype=float, count=n)
    rel = np.ones(n)
    wrad = pr.copy()
    sqrt_w = np.sqrt(rel / np.maximum(wrad, min_weight_radius) ** 2)

    def objective(X):
        return sqrt_w * (np.hypot(px - X[0], py - X[1]) - pr)

    def jacobian(X):
        dx = X[0] - px
        dy = X[1] - py
        d = np.maximum(np.hypot(dx, dy), _JAC_MIN_DIST)
        return np.column_stack((sqrt_w * dx / d, sqrt_w * dy / d))

    x0 = np.array([float(px.mean()), float(py.mean())])
    # Extra starting points for the multi-start solver. The receiver reporting
    # the SMALLEST radius is the strongest single prior on where the tracker
    # is; the 1/r^2-weighted centroid is a cheap second opinion that leans the
    # same way without committing to one receiver.
    i_near = int(np.argmin(pr))
    w = 1.0 / np.maximum(pr, 1e-9) ** 2
    extra = [
        np.array([px[i_near], py[i_near]]),
        np.array([float((px * w).sum() / w.sum()), float((py * w).sum() / w.sum())]),
    ]
    return objective, jacobian, x0, extra


def run(trials, outlier_rate, noise_sigma, seed, layout_path):
    with open(layout_path) as fh:
        floors = json.load(fh)

    print(f"trials/floor={trials}  outlier_rate={outlier_rate}  "
          f"noise_sigma={noise_sigma}  seed={seed}\n")

    grand = {"delta_m": [], "scipy_err_m": [], "numpy_err_m": [], "cost_rel": [],
             "numpy_worse_cost": 0, "numpy_better_cost": 0,
             "only_scipy_ok": 0, "only_numpy_ok": 0, "scipy_ms_err_m": []}
    t_scipy = t_numpy = 0.0
    disagree = 0
    total = 0

    for floor in floors:
        receivers = np.array(floor["receivers"], dtype=float)
        scale = float(floor["scale"])  # pixels per metre
        if len(receivers) < 3:
            continue
        rng = np.random.default_rng(seed)
        min_weight_radius = MIN_WEIGHT_RADIUS_M * scale

        deltas, e_sp, e_np = [], [], []
        for _ in range(trials):
            n_heard = int(rng.integers(3, min(len(receivers), 12) + 1))
            known, bounds, truth = build_problem(
                rng, receivers, scale, n_heard, noise_sigma, outlier_rate
            )
            objective, jacobian, x0, extra = make_callables(known, min_weight_radius)
            minx, miny, maxx, maxy = bounds
            x0c = np.clip(x0, [minx, miny], [maxx, maxy])
            lo, hi = [minx, miny], [maxx, maxy]

            t0 = time.perf_counter()
            rs = least_squares(objective, x0c, jac=jacobian, bounds=(lo, hi),
                               method="trf", loss="soft_l1",
                               f_scale=SOLVER_ROBUST_F_SCALE)
            t_scipy += time.perf_counter() - t0

            # scipy given the SAME multi-start treatment, so the comparison
            # isolates the solver rather than crediting numpy for a starting
            # strategy scipy was never offered.
            best_sp_ms = rs
            best_c = _soft_l1_cost(objective(rs.x), SOLVER_ROBUST_F_SCALE)
            for e in extra:
                ec = np.clip(e, [minx, miny], [maxx, maxy])
                r2 = least_squares(objective, ec, jac=jacobian, bounds=(lo, hi),
                                   method="trf", loss="soft_l1",
                                   f_scale=SOLVER_ROBUST_F_SCALE)
                if r2.success:
                    c2 = _soft_l1_cost(objective(r2.x), SOLVER_ROBUST_F_SCALE)
                    if c2 < best_c:
                        best_sp_ms, best_c = r2, c2
            grand["scipy_ms_err_m"].append(float(np.hypot(*(best_sp_ms.x - truth))) / scale)

            t0 = time.perf_counter()
            rn = least_squares_bounded_soft_l1(
                objective, x0c, jacobian, (lo, hi),
                f_scale=SOLVER_ROBUST_F_SCALE,
                extra_starts=[np.clip(e, [minx, miny], [maxx, maxy]) for e in extra],
            )
            t_numpy += time.perf_counter() - t0

            total += 1
            if bool(rs.success) and not bool(rn.success):
                grand["only_scipy_ok"] += 1
                disagree += 1
            elif bool(rn.success) and not bool(rs.success):
                grand["only_numpy_ok"] += 1
                disagree += 1
            if not (rs.success and rn.success):
                continue

            # The objective decides who is "better" on a disagreement: both
            # solvers minimise the same soft_l1 cost, so a lower cost is a
            # strictly better fit regardless of where truth happens to be.
            c_sp = _soft_l1_cost(objective(rs.x), SOLVER_ROBUST_F_SCALE)
            c_np = _soft_l1_cost(objective(rn.x), SOLVER_ROBUST_F_SCALE)
            rel = (c_np - c_sp) / max(abs(c_sp), 1e-12)
            grand["cost_rel"].append(rel)
            if rel > 1e-6:
                grand["numpy_worse_cost"] += 1
            elif rel < -1e-6:
                grand["numpy_better_cost"] += 1

            deltas.append(float(np.hypot(*(rs.x - rn.x))) / scale)
            e_sp.append(float(np.hypot(*(rs.x - truth))) / scale)
            e_np.append(float(np.hypot(*(rn.x - truth))) / scale)

        grand["delta_m"] += deltas
        grand["scipy_err_m"] += e_sp
        grand["numpy_err_m"] += e_np

        d = np.array(deltas)
        print(f"{floor['name']}  ({len(receivers)} receivers, {scale:.1f} px/m)")
        print(f"  solver-vs-solver delta : median {np.median(d)*100:.2f} cm | "
              f"p95 {np.percentile(d,95)*100:.2f} cm | max {d.max()*100:.2f} cm")
        print(f"  error vs truth (scipy) : median {np.median(e_sp):.3f} m")
        print(f"  error vs truth (numpy) : median {np.median(e_np):.3f} m\n")

    d = np.array(grand["delta_m"])
    sp = np.array(grand["scipy_err_m"])
    npy = np.array(grand["numpy_err_m"])
    print("=" * 62)
    print(f"ALL FLOORS  ({len(d)} converged fits, {total} attempted)")
    print(f"  convergence disagreements : {disagree}")
    print(f"  delta median / p95 / max  : {np.median(d)*100:.2f} / "
          f"{np.percentile(d,95)*100:.2f} / {d.max()*100:.2f} cm")
    print(f"  accuracy vs truth  scipy  : median {np.median(sp):.3f} m, "
          f"p95 {np.percentile(sp,95):.3f} m")
    print(f"  accuracy vs truth  numpy  : median {np.median(npy):.3f} m, "
          f"p95 {np.percentile(npy,95):.3f} m")
    cr = np.array(grand["cost_rel"])
    print(f"  disagreement direction    : scipy-only-ok {grand['only_scipy_ok']}, "
          f"numpy-only-ok {grand['only_numpy_ok']}")
    print(f"  objective (lower is better): numpy worse {grand['numpy_worse_cost']}, "
          f"numpy better {grand['numpy_better_cost']}, tie "
          f"{len(cr)-grand['numpy_worse_cost']-grand['numpy_better_cost']}")
    if len(cr):
        print(f"  relative cost delta        : median {np.median(cr):+.2e} | "
              f"p95 {np.percentile(cr,95):+.2e} | max {cr.max():+.2e}")
    spms = np.array(grand["scipy_ms_err_m"])
    if len(spms):
        print(f"  accuracy scipy+multistart : median {np.median(spms):.3f} m, "
              f"p95 {np.percentile(spms,95):.3f} m")
        print(f"     -> vs numpy+multistart : "
              f"{(np.median(npy)-np.median(spms))*100:+.2f} cm")
    worse = float(np.median(npy) - np.median(sp))
    print(f"  numpy median accuracy     : {worse*100:+.2f} cm vs scipy "
          f"({'worse' if worse > 0 else 'better/equal'})")
    print(f"  time  scipy / numpy       : {t_scipy*1000:.0f} ms / "
          f"{t_numpy*1000:.0f} ms  ({t_scipy/max(t_numpy,1e-9):.2f}x)")
    print("=" * 62)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument("--outlier-rate", type=float, default=0.15)
    ap.add_argument("--noise-sigma", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--layout", default=os.path.join(os.path.dirname(__file__), "real_layout.json"))
    a = ap.parse_args()
    run(a.trials, a.outlier_rate, a.noise_sigma, a.seed, a.layout)
