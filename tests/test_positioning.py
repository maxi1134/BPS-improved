"""Regression tests for the positioning maths, run against the real `sextant`
module (Home Assistant stubbed by conftest). Covers the pieces most likely to
regress silently: the trilateration solver, receiver mount-height slant
correction, 3D calibration ground truth, and the floor hypothesis-competition
election helpers.
"""
import asyncio
import types
import math

import pytest

import sextant
from sextant import calibration as cal_mod
from conftest import make_hass

SCALE = 40.0  # px per metre


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# Trilateration solver
# --------------------------------------------------------------------------- #
def test_trilaterate_recovers_known_point():
    d = math.hypot(5, 5)
    pts = [(0, 0, d), (10, 0, d), (0, 10, d)]
    x, y = sextant.trilaterate(pts)
    assert abs(x - 5) < 0.05 and abs(y - 5) < 0.05


def test_trilaterate_zero_radius_survives():
    # Regression: a 0 radius must not divide-by-zero and abort the solve.
    res = sextant.trilaterate([(0, 0, 0.0), (10, 0, 10.0), (0, 10, 10.0)])
    assert res is not None


def test_stable_hint_solves_once_instead_of_the_full_battery():
    """A good stable_hint must skip straight to one solve rather than paying
    for the full 3-start battery every cycle even when nothing moved."""
    d = math.hypot(5, 5)
    pts = [(0, 0, d), (10, 0, d), (0, 10, d)]
    calls = []
    real_least_squares = sextant.least_squares_bounded_soft_l1
    try:
        sextant.least_squares_bounded_soft_l1 = lambda *a, **kw: calls.append(1) or real_least_squares(*a, **kw)
        x, y = sextant.trilaterate(pts, stable_hint=(5.0, 5.0))
    finally:
        sextant.least_squares_bounded_soft_l1 = real_least_squares
    assert len(calls) == 1
    assert abs(x - 5) < 0.05 and abs(y - 5) < 0.05


def test_stable_hint_falls_back_to_full_battery_when_it_fails():
    """A hint whose solve reports non-convergence must not cost accuracy: the
    caller falls through to exactly the same multi-start result as if no hint
    were given, not the failed hint solve's own (unreliable) answer."""
    d = math.hypot(5, 5)
    pts = [(0, 0, d), (10, 0, d), (0, 10, d)]
    real_least_squares = sextant.least_squares_bounded_soft_l1
    calls = []

    def fake(*a, **kw):
        r = real_least_squares(*a, **kw)
        calls.append(r)
        if len(calls) == 1:
            r.success = False  # force just the hint attempt to report failure
        return r

    try:
        sextant.least_squares_bounded_soft_l1 = fake
        x_hint, y_hint = sextant.trilaterate(pts, stable_hint=(5.0, 5.0))
    finally:
        sextant.least_squares_bounded_soft_l1 = real_least_squares

    assert len(calls) > 1  # fell through to the full battery, not just the hint
    x_plain, y_plain = sextant.trilaterate(pts)
    assert abs(x_hint - x_plain) < 1e-6 and abs(y_hint - y_plain) < 1e-6


def test_min_weight_radius_tames_a_spuriously_short_reading():
    truth = (140.0, 140.0)
    far = [
        (400.0, 140.0, 260.0),
        (140.0, 400.0, 260.0),
        (400.0, 400.0, math.hypot(260, 260)),
    ]
    liar = (100.0, 100.0, 0.01)          # claims the thing is basically on it
    honest = (140.0, 140.0, 20.0)        # 0.5 m at 40 px/m, corroborated
    pts = far + [liar, honest]
    d_unclamped = math.dist(sextant.trilaterate(pts), truth)
    d_clamped = math.dist(sextant.trilaterate(pts, min_weight_radius=20.0), truth)
    assert d_clamped < d_unclamped - 15.0  # the clamp pulls the fit off the liar


# --------------------------------------------------------------------------- #
# Thing height + slant correction
# --------------------------------------------------------------------------- #
def test_thing_height_default_and_override():
    assert sextant._thing_height({}) == sextant.THING_HEIGHT_M
    assert sextant._thing_height({"thing_height": 0.3}) == 0.3
    assert sextant._thing_height({"thing_height": 99}) == sextant.THING_HEIGHT_M  # out of range


def _run_radii(state, unit="m", height=None, thing_height=None):
    class St:
        def __init__(self):
            self.state = state
            self.attributes = {"unit_of_measurement": unit}

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}}
    if height is not None:
        rec["height"] = height
    data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
    if thing_height is not None:
        data["thing_height"] = thing_height
    run(sextant.update_receiver_radii(Hass(), {"entity": "phone", "data": data}))
    return rec


def test_no_height_leaves_distance_alone():
    r = _run_radii("2.3")
    assert abs(r["distance"] - 2.3) < 1e-9
    assert abs(r["cords"]["r"] - 92.0) < 1e-6


def test_height_removes_vertical_leg_from_radius_only():
    # dz = 1.2 -> horizontal sqrt(2.3^2 - 1.2^2) = 1.962 m into the radius;
    # the election "distance" stays the calibrated slant (cross-floor safe).
    r = _run_radii("2.3", height=2.2)
    assert abs(r["cords"]["r"] - 1.9621 * SCALE) < 0.1
    assert abs(r["distance"] - 2.3) < 1e-9


def test_height_underneath_floors_radius_at_min_weight_radius():
    # slant < vertical leg used to collapse to EXACTLY 0 px — the singularity
    # behind the 1.7.0 regression. Now floored at MIN_WEIGHT_RADIUS_M (0.5 m).
    r = _run_radii("1.0", height=2.2)
    assert abs(r["cords"]["r"] - sextant.MIN_WEIGHT_RADIUS_M * SCALE) < 1e-9


def test_nan_and_out_of_range_height_ignored():
    assert abs(_run_radii("2.3", height=float("nan"))["cords"]["r"] - 92.0) < 1e-6
    assert abs(_run_radii("2.3", height=float("inf"))["cords"]["r"] - 92.0) < 1e-6
    assert abs(_run_radii("2.3", height=25)["cords"]["r"] - 92.0) < 1e-6


# --------------------------------------------------------------------------- #
# Calibration ground truth (2D vs 3D)
# --------------------------------------------------------------------------- #
def _cal(ha=None, hb=None):
    return {"receivers": {
        "a": {"x": 0.0, "y": 0.0, "scale": SCALE, "floor": "F", "height": ha},
        "b": {"x": 120.0, "y": 0.0, "scale": SCALE, "floor": "F", "height": hb},
    }}


def test_true_distance_2d_when_heights_absent_or_partial():
    assert abs(cal_mod._true_distance_m(_cal(), "a", "b") - 3.0) < 1e-9
    assert abs(cal_mod._true_distance_m(_cal(ha=2.2), "a", "b") - 3.0) < 1e-9


def test_true_distance_3d_when_both_heights_present():
    # 3 m apart on the map, 0.3 m vs 2.2 m high -> sqrt(9 + 1.9^2) = 3.551 m.
    assert abs(cal_mod._true_distance_m(_cal(ha=0.3, hb=2.2), "a", "b") - 3.5511) < 1e-3


# --------------------------------------------------------------------------- #
# Floor election helpers
# --------------------------------------------------------------------------- #
def test_score_rewards_agreement_and_coverage():
    good = [(x, y, math.hypot(x - 400, y - 400), 1.0)
            for (x, y) in [(0, 0), (800, 0), (0, 800), (800, 800), (400, 0)]]
    conf, rms, cov = sextant._score_floor_fit((400.0, 400.0), good, SCALE)
    assert rms < 1e-6 and cov == 1.0 and conf > 0.99

    bad = [(0, 0, 40.0, 1.0), (800, 0, 40.0, 1.0), (0, 800, 40.0, 1.0)]
    conf_bad, rms_bad, _ = sextant._score_floor_fit((400.0, 400.0), bad, SCALE)
    assert rms_bad > 5.0 and conf_bad < conf


def test_probabilities_converge_and_drop_renamed():
    sextant._floor_probability.clear()
    for _ in range(30):
        probs = sextant._update_floor_probabilities("e", {"a": 0.8, "b": 0.2})
    assert abs(probs["a"] - 0.8) < 0.02
    probs = sextant._update_floor_probabilities("e", {"c": 1.0}, valid_floors={"c"})
    assert "a" not in probs and "b" not in probs


def test_elect_hysteresis_and_dwell():
    # No incumbent: adopt the best immediately.
    floor, ch = sextant._elect_floor({"a": 0.6, "b": 0.4}, None, {"a", "b"}, None, now=0.0)
    assert floor == "a" and ch is None
    # Incumbent holds within the margin.
    floor, _ = sextant._elect_floor({"a": 0.52, "b": 0.48}, "b", {"a", "b"}, None, now=0.0)
    assert floor == "b"
    # A leading challenger must persist for switch_secs of WALL CLOCK before
    # switching - however many cycles that takes.
    ch, incumbent = None, "b"
    seen = []
    for t in (0.0, 10.0, 20.0, 59.0, 60.0, 70.0):
        incumbent, ch = sextant._elect_floor({"a": 0.7, "b": 0.3}, incumbent, {"a", "b"}, ch, now=t, switch_secs=60.0)
        seen.append(incumbent)
    assert seen == ["b", "b", "b", "b", "a", "a"]
    # A lapse in the lead ends the challenge; the clock restarts.
    ch = None
    _, ch = sextant._elect_floor({"a": 0.7, "b": 0.3}, "b", {"a", "b"}, ch, now=0.0, switch_secs=60.0)
    _, ch = sextant._elect_floor({"a": 0.5, "b": 0.5}, "b", {"a", "b"}, ch, now=30.0, switch_secs=60.0)
    assert ch is None
    floor, ch = sextant._elect_floor({"a": 0.7, "b": 0.3}, "b", {"a", "b"}, ch, now=61.0, switch_secs=60.0)
    assert floor == "b" and ch["since"] == 61.0
    # A wider margin (incumbent tenure bonus) can hold off the same lead.
    floor, ch = sextant._elect_floor({"a": 0.56, "b": 0.44}, "b", {"a", "b"}, None, now=0.0, margin=0.15)
    assert floor == "b" and ch is None


# --------------------------------------------------------------------------- #
# Receiver leave-one-out self-localization (run_selftest)
# --------------------------------------------------------------------------- #
def _hass_with(receivers, samples, scale=SCALE, floor="F"):
    """Fake hass carrying a Sextant layout + calibration samples for run_selftest."""
    hass = make_hass()
    recs = []
    for r in receivers:
        d = {"entity_id": r[0], "cords": {"x": r[1], "y": r[2]}}
        if len(r) > 3 and r[3] is not None:
            d["height"] = r[3]
        recs.append(d)
    hass.data.setdefault("sextant", {})["layout"] = {
        "floor": [{"name": floor, "scale": scale, "receivers": recs}]
    }
    hass.data["sextant"]["calibration"] = {"samples": samples}
    return hass


def _exact_samples(receivers, scale=SCALE):
    """samples["target|rx"] = the true slant distance, so a faithful solve
    recovers each receiver exactly (heights, when set, are corrected back out)."""
    pos = {r[0]: (r[1], r[2], (r[3] if len(r) > 3 else None)) for r in receivers}
    s = {}
    for a in pos:
        for b in pos:
            if a == b:
                continue
            ax, ay, ah = pos[a]
            bx, by, bh = pos[b]
            horiz = math.hypot(ax - bx, ay - by) / scale
            dz = (bh - ah) if (ah is not None and bh is not None) else 0.0
            slant = math.hypot(horiz, dz)
            s[f"{a}|{b}"] = [slant, slant, slant]
    return s


SQUARE = [("r1", 0, 0), ("r2", 100, 0), ("r3", 0, 100), ("r4", 100, 100)]


def test_selftest_recovers_receivers():
    res = sextant.run_selftest(_hass_with(SQUARE, _exact_samples(SQUARE)))
    assert res["counts"]["solved"] == 4
    assert all(r["error_m"] < 0.5 for r in res["receivers"])


def test_selftest_recovers_with_mount_heights():
    recs = [("r1", 0, 0, 1.0), ("r2", 100, 0, 3.0), ("r3", 0, 100, 2.0), ("r4", 100, 100, 3.0)]
    res = sextant.run_selftest(_hass_with(recs, _exact_samples(recs)))
    assert res["counts"]["solved"] == 4          # slant correction round-trips
    assert all(r["error_m"] < 0.5 for r in res["receivers"])


def test_selftest_unsolved_when_too_few_neighbors():
    recs = SQUARE + [("r5", 200, 200)]           # r5 has no samples at all
    res = sextant.run_selftest(_hass_with(recs, _exact_samples(SQUARE)))
    solved = {r["entity"] for r in res["receivers"]}
    unsolved = {u["entity"] for u in res["unsolved"]}
    assert solved == {"r1", "r2", "r3", "r4"} and "r5" in unsolved


def test_selftest_error_grows_with_bad_distance():
    s = _exact_samples(SQUARE)
    for o in ("r2", "r3", "r4"):                  # inflate only r1's incoming links
        s[f"r1|{o}"] = [v * 1.6 for v in s[f"r1|{o}"]]
    res = sextant.run_selftest(_hass_with(SQUARE, s))
    err = {r["entity"]: r["error_m"] for r in res["receivers"]}
    # Only the distorted receiver is dragged off; the others still solve clean.
    assert err["r1"] > 0.2 and max(err["r2"], err["r3"], err["r4"]) < 0.05


def test_selftest_empty_layout_is_safe():
    res = sextant.run_selftest(make_hass())
    assert res["counts"]["placed"] == 0 and res["counts"]["solved"] == 0


def test_selftest_bounds_include_left_out_perimeter_receiver():
    # A receiver far outside the OTHER receivers' hull must still be recoverable:
    # the solver bounds cover ALL placed receivers (mirroring the live path), not
    # just the ones feeding this solve — otherwise it would clamp and over-report.
    recs = [("a", 0, 0), ("b", 50, 0), ("c", 0, 50), ("r", 200, 200)]
    res = sextant.run_selftest(_hass_with(recs, _exact_samples(recs)))
    err = {x["entity"]: x["error_m"] for x in res["receivers"]}
    assert err["r"] < 0.5   # not clamped to the a/b/c bounding box


def test_selftest_summary_reports_cep_and_worst():
    result = {
        "counts": {"placed": 3, "solved": 2, "unsolved": 1},
        "receivers": [
            {"entity": "a", "floor": "F", "error_m": 1.0},
            {"entity": "b", "floor": "F", "error_m": 3.0},
        ],
        "unsolved": [{"entity": "c"}],
    }
    state, attrs = sextant._selftest_summary(result)
    assert abs(state - attrs["cep95_m"]) < 1e-9        # state is CEP95
    assert abs(attrs["cep50_m"] - 2.0) < 1e-9          # median of [1, 3]
    assert abs(attrs["max_m"] - 3.0) < 1e-9 and abs(attrs["mean_m"] - 2.0) < 1e-9
    assert attrs["solved"] == 2 and attrs["placed"] == 3
    assert attrs["worst"].startswith("b")
    assert "F" in attrs["per_floor_cep95_m"]


def test_selftest_summary_unknown_when_none_solved():
    state, attrs = sextant._selftest_summary(
        {"counts": {"placed": 4, "solved": 0, "unsolved": 4}, "receivers": [], "unsolved": []})
    assert state is None and "cep95_m" not in attrs and attrs["solved"] == 0


def test_selftest_summary_end_to_end_near_zero():
    res = sextant.run_selftest(_hass_with(SQUARE, _exact_samples(SQUARE)))
    state, attrs = sextant._selftest_summary(res)
    assert state is not None and state < 0.5 and attrs["solved"] == 4


def test_selftest_accepts_explicit_samples_snapshot():
    # The executor path passes a pre-snapshotted samples dict; run_selftest must
    # use it instead of reading the (possibly concurrently-mutated) live deques.
    hass = _hass_with(SQUARE, {})            # empty LIVE samples
    res = sextant.run_selftest(hass, samples=_exact_samples(SQUARE))
    assert res["counts"]["solved"] == 4      # solved from the snapshot, not live state


def _linear_fit(points):
    """A plain (non-robust) weighted least-squares fit with trilaterate's exact
    objective, to prove soft_l1 does better on the SAME points."""
    import numpy as np
    import pytest
    least_squares = pytest.importorskip("scipy.optimize").least_squares

    def obj(X):
        x, y = X
        res = [math.hypot(xi - x, yi - y) - ri for xi, yi, ri in points]
        w = [1.0 / max(ri, 1e-3) ** 2 for _xi, _yi, ri in points]
        return np.sqrt(np.array(w)) * np.array(res)

    c = [float(np.mean([p[0] for p in points])), float(np.mean([p[1] for p in points]))]
    return least_squares(obj, c, method="trf").x


def test_robust_loss_beats_linear_on_an_outlier():
    # Four good receivers pin (200,200); one comparably-weighted outlier at
    # (400,400) reports ~141 px when it is really ~283 px away (a ~2x short,
    # through-wall-style read). soft_l1 must pull the fit off it more than plain
    # linear least-squares does — measured against a linear fit on identical pts.
    truth = (200.0, 200.0)
    good = [(0.0, 200.0, 200.0), (400.0, 200.0, 200.0),
            (200.0, 0.0, 200.0), (200.0, 400.0, 200.0)]
    pts = good + [(400.0, 400.0, 141.0)]
    d_soft = math.dist(sextant.trilaterate(pts), truth)
    d_linear = math.dist(_linear_fit(pts), truth)
    assert d_soft < d_linear              # robust loss helps...
    assert d_soft < 0.75 * d_linear       # ...by a clear margin (here ~0.62x)


# --------------------------------------------------------------------------- #
# Slant-singularity regression (the 1.7.0 accuracy collapse)
# --------------------------------------------------------------------------- #
def test_collapsed_radius_cannot_hijack_the_fix():
    # THE 1.7.0 regression scenario: a height-corrected receiver whose filtered
    # slant latched below dz collapses its projected radius to the 0.5 m floor
    # while the thing is really ~5.7 m away. In a realistic mesh (8 honest
    # receivers at 1.5-5 m), weighting by the PROJECTION (old, 4-tuple
    # behaviour) hands the collapsed receiver dominant 1/r^2 weight and drags
    # the fix ~3 m toward it — the observed live swings. Weighting by the
    # measured SLANT (5th element) keeps the fix on the honest majority.
    truth = (200.0, 200.0)
    recs = [(140, 200), (260, 200), (200, 120), (200, 300),
            (80, 80), (340, 100), (100, 340), (360, 300)]
    honest = [(x, y, math.dist((x, y), truth)) for (x, y) in recs]
    liar_at = (360.0, 360.0)
    r_floor = sextant.MIN_WEIGHT_RADIUS_M * SCALE   # collapsed projection (20 px)
    slant_px = 1.5 * SCALE                      # measured slant ~ dz = 1.5 m
    min_wr = sextant.MIN_WEIGHT_RADIUS_M * SCALE

    old = [(x, y, r, 1.0) for (x, y, r) in honest]
    old.append((liar_at[0], liar_at[1], r_floor, 1.0))            # weight from projection
    new = [(x, y, r, 1.0, r) for (x, y, r) in honest]             # honest: slant == radius
    new.append((liar_at[0], liar_at[1], r_floor, 1.0, slant_px))  # weight from slant

    d_old = math.dist(sextant.trilaterate(old, min_weight_radius=min_wr), truth)
    d_new = math.dist(sextant.trilaterate(new, min_weight_radius=min_wr), truth)
    assert d_old > 2.0 * SCALE      # the old weighting really was hijacked (~3 m)
    assert d_new < 1.0 * SCALE      # the fix now stays within 1 m of truth


def test_jump_weight_ignores_sub_clamp_noise():
    min_wr = 20.0  # 0.5 m at 40 px/m
    # First sighting: fully trusted.
    assert sextant._jump_weight(10.0, None, min_wr) == 1.0
    # Steady radius: fully trusted (above or below the clamp).
    assert sextant._jump_weight(100.0, 100.0, min_wr) == 1.0
    assert sextant._jump_weight(2.0, 2.0, min_wr) == 1.0
    # Sub-clamp bouncing is RSSI noise, not motion: a thing genuinely next
    # to a receiver (readings jittering 0.05 <-> 0.45 m) must keep its most
    # informative receiver at full weight — the clamp exists to protect this.
    # (The slant-collapse case needs no gate: the projection floor keeps a
    # collapsed radius constant, and the slant weight radius bounds its pull.)
    assert sextant._jump_weight(2.0, 18.0, min_wr) == 1.0
    assert sextant._jump_weight(0.0, 18.0, min_wr) == 1.0
    # Genuine above-clamp jumps register as before.
    assert sextant._jump_weight(100.0, 150.0, min_wr) < 1.0
    assert sextant._jump_weight(100.0, 102.0, min_wr) > 0.9
    # A sub-clamp <-> far transition still reads as a big jump.
    assert sextant._jump_weight(10.0, 200.0, min_wr) < 0.05


def test_projection_floor_never_exceeds_raw_slant():
    # A receiver at ~thing height (dz ~ 0) has no singularity: an honest
    # 0.2 m reading must stay 0.2 m, not get inflated to the 0.5 m floor.
    r = _run_radii("0.2", height=1.0)  # thing_height default 1.0 -> dz = 0
    assert abs(r["cords"]["r"] - 0.2 * SCALE) < 1e-9


# --------------------------------------------------------------------------- #
# Per-thing height
# --------------------------------------------------------------------------- #
def test_thing_height_per_thing_precedence():
    data = {"thing_height": 0.7, "thing_heights": {"ankle": 0.1, "bogus": 99}}
    # Per-thing entry wins over the global override.
    assert sextant._thing_height(data, "ankle") == 0.1
    # Unknown / no entity falls back to the global override.
    assert sextant._thing_height(data, "phone") == 0.7
    assert sextant._thing_height(data) == 0.7
    # Out-of-range per-thing value falls through to the global.
    assert sextant._thing_height(data, "bogus") == 0.7
    # Nothing configured at all: the 1.0 m default.
    assert sextant._thing_height({}, "ankle") == sextant.THING_HEIGHT_M
    assert sextant._thing_height({"thing_heights": "junk"}, "ankle") == sextant.THING_HEIGHT_M
    # Bools are ints in Python: a hand-edited true/false must fall through,
    # not read as a valid 1.0/0.0 m height (frontend rejects them too).
    assert sextant._thing_height({"thing_height": 0.7,
                                "thing_heights": {"x": False}}, "x") == 0.7
    assert sextant._thing_height({"thing_height": True}) == sextant.THING_HEIGHT_M


def test_per_thing_height_feeds_slant_correction():
    # Same reading, receiver at 2.2 m: an ankle beacon (0.1 m) has a larger
    # vertical leg than the default 1.0 m, so its horizontal radius is shorter.
    class St:
        state = "2.3"
        attributes = {"unit_of_measurement": "m"}

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    def radius(data):
        rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}, "height": 2.2}
        d = dict(data)
        d["floor"] = [{"name": "F", "scale": SCALE, "receivers": [rec]}]
        run(sextant.update_receiver_radii(Hass(), {"entity": "ankle", "data": d}))
        return rec["cords"]["r"]

    r_default = radius({})                                   # dz = 1.2
    r_ankle = radius({"thing_heights": {"ankle": 0.1}})    # dz = 2.1
    assert abs(r_default - math.sqrt(2.3**2 - 1.2**2) * SCALE) < 0.1
    assert abs(r_ankle - math.sqrt(2.3**2 - 2.1**2) * SCALE) < 0.1
    assert r_ankle < r_default


# --------------------------------------------------------------------------- #
# Per-thing ref-power trim (issue #92)
# --------------------------------------------------------------------------- #
def test_ref_offset_reads_and_validates():
    data = {"thing_ref_offsets": {"cat": -6.0, "big": 99, "boolish": True, "txt": "3"}}
    assert sextant._thing_ref_offset(data, "cat") == -6.0
    assert sextant._thing_ref_offset(data, "big") == 0.0        # out of range
    assert sextant._thing_ref_offset(data, "boolish") == 0.0    # bool is not a number here
    assert sextant._thing_ref_offset(data, "txt") == 0.0        # wrong type
    assert sextant._thing_ref_offset(data, "unknown") == 0.0    # no entry
    assert sextant._thing_ref_offset({}, "cat") == 0.0
    assert sextant._thing_ref_offset({"thing_ref_offsets": "junk"}, "cat") == 0.0


def test_ref_offset_distance_factor_matches_path_loss_model():
    # delta dB scales distance by 10 ** (delta / (10 * attenuation)).
    n = sextant.PATH_LOSS_EXPONENT
    assert sextant._thing_distance_factor({}, "cat") == 1.0     # unset = no-op
    f_up = sextant._thing_distance_factor({"thing_ref_offsets": {"cat": 6.0}}, "cat")
    f_dn = sextant._thing_distance_factor({"thing_ref_offsets": {"cat": -6.0}}, "cat")
    assert abs(f_up - 10 ** (6.0 / (10 * n))) < 1e-12
    assert f_up > 1.0 and f_dn < 1.0                          # + reads farther, - nearer
    assert abs(f_up * f_dn - 1.0) < 1e-12                     # symmetric in dB


def test_ref_trim_scales_the_live_radius():
    # A -6 dB trim must shrink the radius by the model's factor; the election
    # distance is scaled the same way (a per-thing constant).
    class St:
        state = "4.0"
        attributes = {"unit_of_measurement": "m"}

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    def run_with(offsets):
        rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}}
        data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
        if offsets is not None:
            data["thing_ref_offsets"] = offsets
        run(sextant.update_receiver_radii(Hass(), {"entity": "cat", "data": data}))
        return rec

    plain = run_with(None)
    trimmed = run_with({"cat": -6.0})
    factor = 10 ** (-6.0 / (10 * sextant.PATH_LOSS_EXPONENT))
    assert abs(plain["cords"]["r"] - 4.0 * SCALE) < 1e-6
    assert abs(trimmed["cords"]["r"] - 4.0 * factor * SCALE) < 1e-6
    assert abs(trimmed["distance"] - 4.0 * factor) < 1e-9
    # Another thing's trim must not leak onto this one.
    other = run_with({"dog": -6.0})
    assert abs(other["cords"]["r"] - 4.0 * SCALE) < 1e-6


# --------------------------------------------------------------------------- #
# Stale distance readings (stuck values from a scanner that stopped hearing)
# --------------------------------------------------------------------------- #
class _Stamp:
    """Minimal stand-in for a state's tz-aware timestamp."""

    def __init__(self, age_secs):
        import time as _t
        self._ts = _t.time() - age_secs

    def timestamp(self):
        return self._ts


def _run_radii_aged(state, age_secs, max_age=None, stamp_attr="last_updated"):
    class St:
        def __init__(self):
            self.state = state
            self.attributes = {"unit_of_measurement": "m"}
            setattr(self, stamp_attr, _Stamp(age_secs))

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}}
    data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
    if max_age is not None:
        data["reading_max_age"] = max_age
    run(sextant.update_receiver_radii(Hass(), {"entity": "cat", "data": data}))
    return rec


def test_fresh_reading_is_used():
    rec = _run_radii_aged("3.0", age_secs=2)
    assert abs(rec["distance"] - 3.0) < 1e-9
    assert abs(rec["cords"]["r"] - 3.0 * SCALE) < 1e-6


def test_stuck_reading_is_dropped_from_the_solve():
    # Older than READING_MAX_AGE_SECS: no "distance" key, so
    # extract_candidate_floors leaves this receiver out of the fix entirely.
    rec = _run_radii_aged("3.0", age_secs=sextant.READING_MAX_AGE_SECS + 10)
    assert "distance" not in rec


def test_stale_receiver_is_excluded_from_candidates():
    fresh = {"entity_id": "a", "cords": {"x": 0, "y": 0, "r": 40.0}, "distance": 1.0}
    stale = {"entity_id": "b", "cords": {"x": 80, "y": 0, "r": 40.0}}  # gate popped it
    data = [{"entity": "cat", "data": {"floor": [
        {"name": "F", "scale": SCALE, "receivers": [fresh, stale]}]}}]
    cands = sextant.extract_candidate_floors(data, "cat")
    assert len(cands) == 1 and len(cands[0]["cords"]) == 1   # only the fresh one


def test_reading_max_age_override_and_disable():
    # A tighter override drops a reading the default would have accepted.
    assert "distance" not in _run_radii_aged("3.0", age_secs=10, max_age=5)
    # 0 disables the gate: even an ancient reading is used (opt-out).
    assert _run_radii_aged("3.0", age_secs=9999, max_age=0)["distance"] == 3.0
    # Garbage override falls back to the default (still gates).
    assert "distance" not in _run_radii_aged("3.0", age_secs=9999, max_age=True)


def test_age_falls_back_to_last_changed_and_fails_open():
    # Only last_changed available: still gated.
    assert "distance" not in _run_radii_aged(
        "3.0", age_secs=9999, stamp_attr="last_changed")

    # No usable timestamp at all: fail OPEN (never blank the map on an
    # unexpected state object).
    class St:
        state = "3.0"
        attributes = {"unit_of_measurement": "m"}

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}}
    run(sextant.update_receiver_radii(
        Hass(), {"entity": "cat", "data": {"floor": [
            {"name": "F", "scale": SCALE, "receivers": [rec]}]}}))
    assert rec["distance"] == 3.0


# --------------------------------------------------------------------------- #
# The vectorised objective + analytic Jacobian
# --------------------------------------------------------------------------- #
def _weighted_cost(pts, x, y, min_weight_radius=1e-3):
    """The cost trilaterate() minimises, written independently of it.

    Mirrors scipy's soft_l1: rho(z) = 2*(sqrt(1+z) - 1) applied to the squared
    residual scaled by f_scale.
    """
    total = 0.0
    fs = sextant.SOLVER_ROBUST_F_SCALE
    for pt in pts:
        xi, yi, ri = pt[0], pt[1], pt[2]
        wi = pt[3] if len(pt) > 3 else 1.0
        wri = pt[4] if len(pt) > 4 else ri
        w = wi / max(wri, min_weight_radius) ** 2
        res = (w ** 0.5) * (math.hypot(xi - x, yi - y) - ri)
        z = (res / fs) ** 2
        total += 2.0 * ((1.0 + z) ** 0.5 - 1.0)
    return total


def test_the_fit_is_a_local_minimum_of_the_weighted_cost():
    # Implementation-independent: whatever the objective and Jacobian are
    # written in, the point that comes back must be a minimum of the cost the
    # docstring describes. This is what protects the analytic Jacobian — a
    # wrong derivative still converges, just to the wrong place.
    cases = [
        [(0.0, 0.0, 141.4), (200.0, 0.0, 141.4), (100.0, 200.0, 100.0)],
        [(120.0, 120.0, 210.0, 0.9, 210.0), (640.0, 140.0, 300.0, 0.8, 300.0),
         (380.0, 420.0, 190.0, 1.0, 190.0), (80.0, 400.0, 330.0, 0.7, 330.0)],
        [(50.0, 50.0, 90.0), (400.0, 60.0, 260.0), (240.0, 380.0, 180.0),
         (600.0, 300.0, 400.0), (120.0, 300.0, 130.0)],
    ]
    for pts in cases:
        got = sextant.trilaterate(pts)
        assert got is not None
        x, y = got
        here = _weighted_cost(pts, x, y)
        for dx, dy in ((1.0, 0), (-1.0, 0), (0, 1.0), (0, -1.0),
                       (0.7, 0.7), (-0.7, -0.7)):
            assert _weighted_cost(pts, x + dx, y + dy) >= here - 1e-9, (
                f"moving by ({dx}, {dy}) lowered the cost: not a minimum")


def test_a_fit_sitting_exactly_on_a_receiver_is_finite():
    # The analytic Jacobian divides by the distance from the fit to each
    # receiver, which is 0 when the fit lands exactly on one. Without the floor
    # that row is NaN and poisons the whole step.
    pts = [(100.0, 100.0, 0.0), (300.0, 100.0, 200.0), (100.0, 300.0, 200.0)]
    got = sextant.trilaterate(pts)
    assert got is not None
    x, y = got
    assert math.isfinite(x) and math.isfinite(y)
    assert abs(x - 100.0) < 1.0 and abs(y - 100.0) < 1.0


def test_the_weight_radius_override_still_governs_the_weight():
    # The 5th element must keep overriding the radius used in the 1/r^2 weight
    # after vectorisation — this is the 1.7.0 regression guard.
    truth = [(0.0, 0.0, 300.0), (600.0, 0.0, 300.0), (300.0, 500.0, 250.0)]
    # A receiver whose projected radius collapsed to ~0 but whose MEASURED
    # slant was large must not be allowed to dominate.
    hijack = truth + [(600.0, 500.0, 0.001, 1.0, 400.0)]
    naive = truth + [(600.0, 500.0, 0.001)]
    with_override = sextant.trilaterate(hijack)
    without = sextant.trilaterate(naive)
    assert with_override is not None and without is not None
    # Without the override the fit is dragged onto the collapsed receiver.
    assert math.hypot(without[0] - 600.0, without[1] - 500.0) < \
        math.hypot(with_override[0] - 600.0, with_override[1] - 500.0)


def test_multistart_never_worse_than_centroid_only_and_sometimes_better():
    """trilaterate() solves from several starts and keeps the lowest cost.

    The soft_l1 objective is multi-modal once gross outliers are present, so a
    single descent from the receiver centroid can settle in a worse basin. This
    asserts the invariant that makes multi-start safe — it is never worse than
    the centroid-only fit it replaced — and that it does actually escape a
    worse basin on at least some inputs, so the extra solves are earning their
    keep rather than silently doing nothing.
    """
    import numpy as np

    rng = np.random.default_rng(4)
    # A deliberately awkward ring of receivers: symmetric layouts are where
    # multiple minima live.
    ang = np.linspace(0, 2 * np.pi, 9, endpoint=False)
    recv = np.column_stack((500 + 400 * np.cos(ang), 500 + 400 * np.sin(ang)))
    bounds = (recv[:, 0].min(), recv[:, 1].min(), recv[:, 0].max(), recv[:, 1].max())

    strictly_better = 0
    for _ in range(60):
        truth = rng.uniform([bounds[0], bounds[1]], [bounds[2], bounds[3]])
        d = np.hypot(recv[:, 0] - truth[0], recv[:, 1] - truth[1])
        meas = d * np.exp(rng.normal(0, 0.25, size=len(recv)))
        # Two gross outliers, which is what creates the extra minima.
        meas[rng.choice(len(recv), size=2, replace=False)] *= rng.uniform(2.5, 6.0)
        known = [(float(p[0]), float(p[1]), float(r)) for p, r in zip(recv, meas)]

        got = sextant.trilaterate(known, bounds=bounds, min_weight_radius=0.5 * 100)
        assert got is not None

        # Rebuild the same residual to score both fits on one objective.
        px, py = recv[:, 0], recv[:, 1]
        pr = np.array([k[2] for k in known])
        sqrt_w = np.sqrt(1.0 / np.maximum(pr, 0.5 * 100) ** 2)

        def obj(X, px=px, py=py, pr=pr, sqrt_w=sqrt_w):
            return sqrt_w * (np.hypot(px - X[0], py - X[1]) - pr)

        centroid = np.array([px.mean(), py.mean()])

        def jac(X, px=px, py=py, sqrt_w=sqrt_w):
            dx, dy = X[0] - px, X[1] - py
            dd = np.maximum(np.hypot(dx, dy), 1e-9)
            return np.column_stack((sqrt_w * dx / dd, sqrt_w * dy / dd))

        # Same solver, single start from the centroid: the comparison must be
        # apples to apples, or a solver's own convergence tolerance (scipy's
        # trf polishes ~1e-5 tighter than the numpy LM) masquerades as a
        # multi-start regression.
        single = sextant.least_squares_bounded_soft_l1(
            obj, centroid, jac,
            ([bounds[0], bounds[1]], [bounds[2], bounds[3]]),
            f_scale=sextant.SOLVER_ROBUST_F_SCALE,
        )

        def cost(res):
            z = (res / sextant.SOLVER_ROBUST_F_SCALE) ** 2
            return 0.5 * float(np.sum(sextant.SOLVER_ROBUST_F_SCALE ** 2
                                      * 2.0 * (np.sqrt(1.0 + z) - 1.0)))

        c_multi = cost(obj(np.array(got)))
        c_single = cost(obj(single.x))

        # Never worse (tiny tolerance for float noise).
        assert c_multi <= c_single * (1 + 1e-9) + 1e-9
        if c_multi < c_single * (1 - 1e-6):
            strictly_better += 1

    assert strictly_better > 0, "multi-start never improved on centroid-only"


# ---------------------------------------------------------------------------
# Sensor writes must tolerate an entity that was never added to HA
# ---------------------------------------------------------------------------


class _FakeSensor:
    def __init__(self, live):
        self.hass = object() if live else None
        self._state = None
        self._attrs = {}
        self.writes = 0

    def async_write_ha_state(self):
        self.writes += 1


def test_sensor_state_write_skips_an_entity_without_hass():
    """A sensor the user disabled in the registry is cached but never added,
    so it has no hass; writing to it raised every self-test cycle."""
    hass = make_hass()
    dead, live = _FakeSensor(False), _FakeSensor(True)
    hass.data["sextant_sensors"] = {"sensor.dead": dead, "sensor.live": live}

    sextant.update_sextant_sensor_state(hass, "sensor.dead", 1.5, {"a": 1})
    sextant.update_sextant_sensor_state(hass, "sensor.live", 2.5)

    assert dead.writes == 0
    assert dead._state == 1.5 and dead._attrs == {"a": 1}  # kept for a later enable
    assert live.writes == 1 and live._state == 2.5
    assert sextant._sensor_is_live(hass, "sensor.dead") is False
    assert sextant._sensor_is_live(hass, "sensor.live") is True
    assert sextant._sensor_is_live(hass, "sensor.missing") is False


def test_registry_device_iteration_handles_both_registry_shapes():
    """Newer HA yields DeviceEntry objects from `dev_reg.devices`; older HA
    yielded device ids from a mapping. Both must produce entries."""
    entry_a, entry_b = object(), object()
    new_style = types.SimpleNamespace(devices=[entry_a, entry_b])
    assert list(sextant._iter_registry_devices(new_style)) == [entry_a, entry_b]

    old_style = types.SimpleNamespace(devices={"id-a": entry_a, "id-b": entry_b})
    assert list(sextant._iter_registry_devices(old_style)) == [entry_a, entry_b]


# ---------------------------------------------------------------------------
# Tuning knobs (sextant.set_tuning)
# ---------------------------------------------------------------------------


def test_tuning_reads_validated_values_and_falls_back():
    layout = {"tuning": {
        "zone_switch_secs": 45, "distance_estimator": "median", "zone_hysteresis": False,
        "solver_max_receivers": 999,      # out of range -> default
        "stationary_speed": True,         # bool is not a number -> default
        "median_min_samples": "3",        # wrong type -> default
    }}
    assert sextant._tuning(layout, "zone_switch_secs") == 45.0
    assert isinstance(sextant._tuning(layout, "zone_switch_secs"), float)
    assert sextant._tuning(layout, "distance_estimator") == "median"
    assert sextant._tuning(layout, "zone_hysteresis") is False
    assert sextant._tuning(layout, "solver_max_receivers") == 8
    assert sextant._tuning(layout, "stationary_speed") == 0.3
    assert sextant._tuning(layout, "median_min_samples") == 3
    assert sextant._tuning([], "floor_switch_secs") == 60.0
    assert sextant._tuning({"tuning": "junk"}, "floor_switch_secs") == 60.0
    assert sextant._coerce_tuning("distance_estimator", "mean", None) is None
    assert sextant._coerce_tuning("zone_hysteresis", False, None) is False


# ---------------------------------------------------------------------------
# Nearest-receiver cap
# ---------------------------------------------------------------------------


def _entries(*distances):
    return [(d, ("pt", d), f"proxy_{d}") for d in distances]


def test_select_receivers_keeps_near_plus_nearest_k_and_drops_far():
    kept, heard = sextant._select_receivers(_entries(1, 2, 2.5, 4, 5, 6, 7, 9, 14, 20), 4, 12.0, 3.0)
    assert [p[1] for p in kept] == [1, 2, 2.5, 4]           # near-always + nearest 4
    kept, heard = sextant._select_receivers(_entries(0.5, 2.9, 2.95, 4, 5, 6), 3, 12.0, 3.0)
    assert [p[1] for p in kept] == [0.5, 2.9, 2.95]          # K coincides with the near set
    kept, heard = sextant._select_receivers(_entries(9, 14, 20, 30), 8, 12.0, 3.0)
    assert [p[1] for p in kept] == [9, 14, 20]               # never starved below three
    kept, heard = sextant._select_receivers(_entries(1, 2, 3, 14, 20), 8, 12.0, 3.0)
    assert [p[1] for p in kept] == [1, 2, 3]                 # beyond range dropped once 3 kept
    kept, heard = sextant._select_receivers(_entries(5, 4, 3, 2, 1), 0, 0.0, 0.0)
    assert [p[1] for p in kept] == [1, 2, 3, 4, 5]           # 0 = unlimited, sorted nearest-first
    kept, heard = sextant._select_receivers(_entries(1, 2, 3, 4, 5), 1, 0.0, 0.0)
    assert [p[1] for p in kept] == [1, 2, 3]                 # K clamps to the solver minimum
    # Which proxy said what rides along with the points it kept, nearest-first:
    # the points are anonymous coordinates, and a floor election that goes
    # wrong is diagnosed by who stopped being heard.
    assert heard == [("proxy_1", 1.0), ("proxy_2", 2.0), ("proxy_3", 3.0)]
    kept, heard = sextant._select_receivers([(2.0, ("pt", 2.0))], 0, 0.0, 0.0)
    assert heard == [(None, 2.0)]                            # a pair with no name still works


def test_extract_candidate_floors_applies_the_cap_and_carries_quality():
    floor = {"name": "F", "scale": SCALE, "receivers": []}
    for i, d in enumerate([1.0, 2.0, 4.0, 6.0, 8.0, 15.0, 30.0]):
        floor["receivers"].append({"entity_id": f"r{i}", "cords": {"x": i * 100.0, "y": 0.0, "r": d * SCALE},
                                   "distance": d, "quality": 0.5 if i == 0 else None})
    layout = {"floor": [floor], "tuning": {"solver_max_receivers": 4, "solver_max_range": 12.0}}
    cands = sextant.extract_candidate_floors([{"entity": "e", "data": layout}], "e")
    assert len(cands) == 1 and cands[0]["nearest_m"] == 1.0
    pts = cands[0]["cords"]
    assert [p[0] for p in pts] == [0.0, 100.0, 200.0, 300.0]
    assert pts[0][4] == 0.5 and pts[1][4] == 1.0


# ---------------------------------------------------------------------------
# Median RSSI estimator
# ---------------------------------------------------------------------------


def _reading(history, age=1.0, ref_power=-55.0, attenuation=3.0, offset=0):
    return {"distance": 9.9, "age": age, "history": history,
            "ref_power": ref_power, "attenuation": attenuation, "rssi_offset": offset}


def test_median_distance_matches_bermudas_path_loss_model():
    hist = [[-65, 100.0], [-67, 99.0], [-63, 98.0], [-90, 97.0], [-65, 96.5]]
    dist, quality = sextant._median_distance(_reading(hist), 15.0, 3)
    # median rssi is -65 -> 10 ** ((-55 + 65) / 30)
    assert abs(dist - 10 ** (10 / 30)) < 1e-9
    assert quality == 1.0                                    # 5 samples saturate
    # The per-scanner rssi offset is added before conversion, like Bermuda.
    dist_off, _ = sextant._median_distance(_reading(hist, offset=2), 15.0, 3)
    assert abs(dist_off - 10 ** ((-55 + 63) / 30)) < 1e-9


def test_median_distance_respects_window_and_minimum():
    # age 1 s; samples at 100, 99 are fresh, 80 is 21 s old.
    hist = [[-60, 100.0], [-62, 99.0], [-99, 80.0]]
    assert sextant._median_distance(_reading(hist), 15.0, 3) is None          # only 2 fresh
    dist, quality = sextant._median_distance(_reading(hist), 15.0, 2)
    assert abs(dist - 10 ** ((-55 + 61) / 30)) < 1e-9                    # median of -60,-62
    assert quality == 2 / 5
    assert sextant._median_distance(_reading([]), 15.0, 1) is None
    assert sextant._median_distance({"history": hist, "age": 0}, 15.0, 1) is None  # no parameters


# ---------------------------------------------------------------------------
# Zone election: hysteresis, dwell, stationary lock
# ---------------------------------------------------------------------------

from shapely.geometry import Point, Polygon  # noqa: E402


def _two_rooms():
    # Kitchen x in [0, 100), Dining x in [100, 200]; boundary at x = 100. One
    # pixel = 1 cm (scale 100 px/m).
    kitchen = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    dining = Polygon([(100, 0), (200, 0), (200, 100), (100, 100)])
    return [("Kitchen", kitchen, 5.0, False), ("Dining", dining, 5.0, False)]


def _kf(x, y, vx=0.0, vy=0.0, sigma_px=10.0, floor="F"):
    import numpy as np
    return {"x": np.array([x, y, vx, vy], float), "P": np.diag([sigma_px ** 2] * 2 + [1.0, 1.0]), "ts": 0.0, "floor": floor}


def _elect(entity, x, t, vx=0.0, layout=None, sigma=10.0):
    layout = layout if layout is not None else {}
    # These tests start the clock at 0; the warm-up has its own test below.
    tuning = {"zone_lock_warmup_secs": 0.0, **(layout.get("tuning") or {})}
    layout = {**layout, "tuning": tuning}
    zone, locked, _speed = sextant._elect_zone(
        entity, "F", "Kitchen" if x < 100 else "Dining", Point(x, 50.0),
        _kf(x, 50.0, vx=vx, sigma_px=sigma), _two_rooms(), 100.0, layout, now=t,
    )
    return zone, locked


def test_zone_election_ignores_a_brief_excursion_and_follows_a_sustained_one():
    sextant._zone_state.clear()
    layout = {"tuning": {"stationary_secs": 600.0}}  # keep the lock out of this test
    assert _elect("e", 50, 0.0, layout=layout) == ("Kitchen", False)
    # One cycle across the line: not published.
    assert _elect("e", 130, 10.0, layout=layout)[0] == "Kitchen"
    assert _elect("e", 50, 20.0, layout=layout)[0] == "Kitchen"
    # Sustained on the other side: switches once the dwell has elapsed.
    seen = [_elect("e", 140, t, layout=layout)[0] for t in (30.0, 40.0, 50.0, 60.0, 70.0)]
    assert seen[0] == "Kitchen" and seen[-1] == "Dining"


def test_zone_election_off_publishes_the_instant_zone():
    sextant._zone_state.clear()
    layout = {"tuning": {"zone_hysteresis": False}}
    assert _elect("e", 50, 0.0, layout=layout) == ("Kitchen", False)
    assert _elect("e", 130, 10.0, layout=layout) == ("Dining", False)


def test_stationary_lock_holds_the_zone_and_releases_when_clearly_away():
    sextant._zone_state.clear()
    assert _elect("e", 90, 0.0)[0] == "Kitchen"          # resting 10 cm from the boundary
    for t in (10.0, 20.0, 30.0):
        zone, locked = _elect("e", 90, t)
    assert zone == "Kitchen" and locked is True            # still for 30 s (> stationary_secs)
    # Boundary jitter: the fix wanders 30 cm into Dining and back. Locked, so
    # nothing changes, however long it wanders within the unlock margin (1 m).
    for t in (40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0):
        zone, locked = _elect("e", 130, t)
        assert (zone, locked) == ("Kitchen", True)
    # Now genuinely gone: 2.5 m into Dining. Unlocks after zone_unlock_secs
    # and the switch follows at once (the time away counts toward the dwell).
    seen = [_elect("e", 250 - 100 + 100, t) for t in (110.0, 120.0, 130.0, 140.0, 150.0)]
    assert seen[0] == ("Kitchen", True)
    assert seen[-1] == ("Dining", False)


def test_stationary_lock_releases_when_the_thing_keeps_moving():
    sextant._zone_state.clear()
    for t in (0.0, 10.0, 20.0, 30.0):
        zone, locked = _elect("e", 50, t)
    assert locked is True
    # Walking (1 m/s) inside the same room for longer than stationary_secs.
    for t in (40.0, 50.0, 60.0, 70.0):
        zone, locked = _elect("e", 50, t, vx=100.0)
    assert (zone, locked) == ("Kitchen", False)


def test_a_zone_election_survives_a_trip_through_the_restart_store():
    """The election carries on from a restored state, rather than raising on it.

    Every None in that dict - no challenger, not moving since - used to be
    dropped on the way to disk, and the first st["still_since"] after a restart
    raised KeyError inside a handler that logged at INFO. Every thing went back
    to a cold start and nothing said so.
    """
    import json

    sextant._zone_state.clear()
    for t in (0.0, 10.0, 20.0, 30.0):
        zone, locked = _elect("e", 90, t)
    assert (zone, locked) == ("Kitchen", True)
    since, born = sextant._zone_state["e"]["since"], sextant._zone_state["e"]["born"]

    def round_trip(at, now):
        saved = json.loads(json.dumps(sextant.runtime_mod.snapshot(at, zones=sextant._zone_state)))
        back = sextant.runtime_mod.restore(saved, now)
        sextant._zone_state.clear()
        for entity, state in back["zone"].items():
            sextant._zone_state[entity] = {**sextant._new_zone_state(state.get("floor"), now), **state}

    round_trip(30.0, 40.0)
    # Back on its shelf 10 cm from the boundary, still locked, still since then.
    assert _elect("e", 90, 50.0) == ("Kitchen", True)
    assert sextant._zone_state["e"]["since"] == since and sextant._zone_state["e"]["born"] == born

    # And the other way round: carried through the door when the snapshot was
    # taken, put down after the restart. That is the case that actually broke -
    # a thing on the move has no still_since to write.
    sextant._zone_state.clear()
    _elect("e", 50, 100.0, vx=100.0)
    _elect("e", 50, 110.0, vx=100.0)
    round_trip(110.0, 120.0)
    assert _elect("e", 50, 130.0)[0] == "Kitchen"


def test_zone_election_resets_on_floor_change_and_prune():
    sextant._zone_state.clear()
    _elect("e", 50, 0.0)
    assert sextant._zone_state["e"]["floor"] == "F"
    zone, _, _ = sextant._elect_zone("e", "G", "Dining", Point(150, 50), _kf(150, 50, floor="G"), _two_rooms(), 100.0, {}, now=10.0)
    assert zone == "Dining" and sextant._zone_state["e"]["floor"] == "G"


# ---------------------------------------------------------------------------
# End to end: one positioning cycle through election, filter, zones, publish
# ---------------------------------------------------------------------------


class _Sensor:
    def __init__(self):
        self.hass = object()
        self._state = None
        self._attrs = {}

    def async_write_ha_state(self):
        pass


def _square_layout(tuning=None):
    # 10 m x 10 m floor at 100 px/m, receivers on the corners, Kitchen on the
    # left half and Dining on the right half.
    layout = {
        "floor": [{
            "name": "F", "scale": 100.0,
            "receivers": [
                {"entity_id": "r0", "cords": {"x": 0.0, "y": 0.0}},
                {"entity_id": "r1", "cords": {"x": 1000.0, "y": 0.0}},
                {"entity_id": "r2", "cords": {"x": 0.0, "y": 1000.0}},
                {"entity_id": "r3", "cords": {"x": 1000.0, "y": 1000.0}},
            ],
            "zones": [
                {"zone_id": "k", "entity_id": "Kitchen", "poly": True,
                 "cords": [{"x": 0, "y": 0}, {"x": 500, "y": 0}, {"x": 500, "y": 1000}, {"x": 0, "y": 1000}]},
                {"zone_id": "d", "entity_id": "Dining", "poly": True,
                 "cords": [{"x": 500, "y": 0}, {"x": 1000, "y": 0}, {"x": 1000, "y": 1000}, {"x": 500, "y": 1000}]},
            ],
            "subzones": [],
        }],
    }
    if tuning:
        layout["tuning"] = tuning
    return layout


def _cycle(hass, layout, x_m, y_m):
    """Feed exact distances for a true position and run one cycle."""
    import copy
    data = copy.deepcopy(layout)
    for rx in data["floor"][0]["receivers"]:
        d = math.hypot(rx["cords"]["x"] / 100.0 - x_m, rx["cords"]["y"] / 100.0 - y_m)
        rx["distance"] = d
        rx["cords"]["r"] = d * 100.0
    ngd = [{"entity": "e", "data": data}]
    run(sextant.update_trilateration_and_zone(hass, ngd, "e"))
    return next(item for item in sextant.apitricords if item["ent"] == "e")


def _reset_thing_state():
    for d in (sextant._floor_probability, sextant._floor_probability_at, sextant._floor_challenge, sextant._floor_dark_cycles, sextant._floor_since,
              sextant._kf_position_state, sextant._zone_state, sextant._subzone_state):
        d.clear()
    sextant.apitricords = []
    for attr in ("last_r_values", "last_floor"):
        if hasattr(sextant.update_trilateration_and_zone, attr):
            getattr(sextant.update_trilateration_and_zone, attr).clear()


def test_full_cycle_publishes_a_stable_zone_and_the_raw_one(monkeypatch):
    _reset_thing_state()
    hass = make_hass()
    sensors = {f"sensor.e_sextant_{k}": _Sensor() for k in ("room", "nearest_room", "floor", "spot")}
    hass.data["sextant_sensors"] = sensors
    layout = _square_layout({"stationary_secs": 600.0})
    # Pin the clock so the dwell is deterministic: each cycle is 10 s.
    clock = {"t": 1000.0}
    monkeypatch.setattr(sextant.time, "time", lambda: clock["t"])

    entry = _cycle(hass, layout, 2.0, 5.0)
    assert entry["floor"] == "F" and entry["zone"] == "Kitchen" and entry["zone_raw"] == "Kitchen"
    assert entry["zone_locked"] is False and "speed" in entry
    assert sensors["sensor.e_sextant_room"]._state == "Kitchen"
    assert sensors["sensor.e_sextant_floor"]._state == "F"

    # One cycle 1.5 m over the line: raw says Dining, published stays Kitchen.
    clock["t"] += 10
    entry = _cycle(hass, layout, 6.5, 5.0)
    assert entry["zone_raw"] == "Dining" and entry["zone"] == "Kitchen"
    assert sensors["sensor.e_sextant_nearest_room"]._state == "Dining"   # raw sensor still instant

    # Sustained on the Dining side: published follows after the dwell.
    for _ in range(6):
        clock["t"] += 10
        entry = _cycle(hass, layout, 8.0, 5.0)
    assert entry["zone"] == "Dining"
    assert sensors["sensor.e_sextant_room"]._state == "Dining"
    assert sensors["sensor.e_sextant_spot"]._attrs["room"] == "Dining"
    assert sensors["sensor.e_sextant_spot"]._attrs["presence"] == "here"   # just heard


def test_full_cycle_with_hysteresis_off_publishes_instantly(monkeypatch):
    _reset_thing_state()
    hass = make_hass()
    hass.data["sextant_sensors"] = {f"sensor.e_sextant_{k}": _Sensor() for k in ("zone", "nearest_zone", "floor", "sub_zone")}
    layout = _square_layout({"zone_hysteresis": False})
    clock = {"t": 1000.0}
    monkeypatch.setattr(sextant.time, "time", lambda: clock["t"])
    assert _cycle(hass, layout, 2.0, 5.0)["zone"] == "Kitchen"
    clock["t"] += 10
    entry = _cycle(hass, layout, 8.0, 5.0)
    # The Kalman filter lags the raw fix, so the published point may still be
    # near the line on this cycle; what matters is that zone == zone_raw.
    assert entry["zone"] == entry["zone_raw"]


def test_proximity_weighting_favours_the_floor_with_the_nearest_receiver():
    scores = {"Ground": 0.85, "Second": 0.86}
    nearest = {"Ground": 1.5, "Second": 3.5}
    out = sextant._proximity_weighted_scores(scores, nearest, 0.5)
    assert out["Ground"] == 0.85                       # nearest floor: unchanged
    assert abs(out["Second"] - 0.86 * (0.5 + 0.5 * 1.5 / 3.5)) < 1e-12
    assert out["Ground"] > out["Second"]
    # Weight 0, a single floor, or unusable distances pass straight through.
    assert sextant._proximity_weighted_scores(scores, nearest, 0.0) == scores
    assert sextant._proximity_weighted_scores({"Ground": 0.85}, nearest, 0.5) == {"Ground": 0.85}
    assert sextant._proximity_weighted_scores(scores, {"Ground": None, "Second": float("inf")}, 0.5) == scores
    # A floor with no usable distance is not penalised, only the others are ranked.
    out = sextant._proximity_weighted_scores(scores, {"Ground": 2.0, "Second": None}, 0.5)
    assert out == scores


def test_geometric_blend_lets_the_nearest_proxies_outweigh_a_poor_fit():
    """Eilee's phone at 05:37: its own floor's fit was cut to 0.15 by the
    no-go penalty (a fix a metre over the foyer void) while the floor below,
    hearing it through the slab at much the same range, fitted at 0.72.
    Gated, the poor fit caps her floor (0.27 against 0.56); geometric at the
    same weight, the nearer proxies carry it (0.77 against 0.73)."""
    scores, nearest = {"Second": 0.273, "Ground": 0.717}, {"Second": 2.4, "Ground": 3.3}
    gated = sextant._proximity_weighted_scores(scores, nearest, 0.8, "gated")
    assert gated["Ground"] > gated["Second"]
    geo = sextant._proximity_weighted_scores(scores, nearest, 0.8, "geometric")
    assert geo["Second"] > geo["Ground"]
    assert abs(geo["Second"] - 0.273 ** 0.2) < 1e-9, "prox 1 on the nearest floor: conf^(1-w)"
    assert abs(geo["Ground"] - 0.717 ** 0.2 * (2.4 / 3.3) ** 0.8) < 1e-9
    # Weight 0 and a lone floor pass through under either blend; a negative
    # confidence (never produced, but never a complex number either) is floored.
    assert sextant._proximity_weighted_scores(scores, nearest, 0.0, "geometric") == scores
    assert sextant._proximity_weighted_scores({"Ground": 0.5}, nearest, 0.7, "geometric") == {"Ground": 0.5}
    assert sextant._proximity_weighted_scores({"A": -0.1, "B": 0.5}, {"A": 1.0, "B": 2.0}, 0.5, "geometric")["A"] == 0.0
    assert sextant.TUNING_SPEC["floor_proximity_blend"][0] == "gated", "the default is unchanged"


def test_full_cycle_floor_switches_on_proximity_when_fits_tie(monkeypatch):
    """Two floors explain the receivers equally well (open foyer); the one
    whose receivers are nearest must win the election within the dwell."""
    _reset_thing_state()
    hass = make_hass()
    hass.data["sextant_sensors"] = {f"sensor.e_sextant_{k}": _Sensor() for k in ("zone", "nearest_zone", "floor", "sub_zone")}
    import copy
    layout = _square_layout({"stationary_secs": 600.0, "floor_switch_secs": 60.0})
    # Second floor: same geometry, same zone names suffixed, placed 3 m "above".
    up = copy.deepcopy(layout["floor"][0]); up["name"] = "U"
    for z in up["zones"]:
        z["entity_id"] += " Up"; z["zone_id"] += "u"
    layout["floor"].append(up)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sextant.time, "time", lambda: clock["t"])

    def cycle(x_m, y_m, near_floor):
        data = copy.deepcopy(layout)
        for fl in data["floor"]:
            for rx in fl["receivers"]:
                d = math.hypot(rx["cords"]["x"] / 100.0 - x_m, rx["cords"]["y"] / 100.0 - y_m)
                if fl["name"] != near_floor:
                    d = math.hypot(d, 3.0)   # through-slab: 3 m of extra slant
                rx["distance"] = d
                rx["cords"]["r"] = d * 100.0
        run(sextant.update_trilateration_and_zone(hass, [{"entity": "e", "data": data}], "e"))
        return next(i for i in sextant.apitricords if i["ent"] == "e")

    # Start upstairs, settle there.
    for _ in range(3):
        clock["t"] += 15   # the real cycle: smoothing is per 15 s of wall clock
        entry = cycle(2.0, 5.0, "U")
    assert entry["floor"] == "U"
    # Move to the floor below: the incumbent keeps solving (all its receivers
    # still hear the thing through the slab), so only proximity separates them.
    seen = []
    for _ in range(12):
        clock["t"] += 15
        seen.append(cycle(2.0, 5.0, "F")["floor"])
    assert seen[-1] == "F", seen


def _void_election(monkeypatch, shape):
    """A thing beside a void: once settled on F, both floors hear it with the
    SAME distances (no slab between them), so fit and proximity tie exactly
    and the election has nothing to go on. Returns every cycle's payload."""
    import copy
    from sextant import floor_field
    _reset_thing_state()
    hass = make_hass()
    hass.data["sextant_sensors"] = {f"sensor.e_sextant_{k}": _Sensor() for k in ("zone", "nearest_zone", "floor", "sub_zone")}
    layout = _square_layout({"stationary_secs": 600.0, "floor_switch_secs": 60.0})
    up = copy.deepcopy(layout["floor"][0]); up["name"] = "U"
    for z in up["zones"]:
        z["entity_id"] += " Up"; z["zone_id"] += "u"
    layout["floor"].append(up)
    shape(layout, floor_field)
    clock = {"t": 1000.0}
    monkeypatch.setattr(sextant.time, "time", lambda: clock["t"])
    out = []
    for n in range(16):
        clock["t"] += 15   # the real cycle: smoothing is per 15 s of wall clock
        data = copy.deepcopy(layout)
        for fl in data["floor"]:
            for rx in fl["receivers"]:
                if n < 3 and fl["name"] == "U":
                    continue                 # settle on F first: upstairs does not hear it yet
                d = math.hypot(rx["cords"]["x"] / 100.0 - 2.0, rx["cords"]["y"] / 100.0 - 5.0)
                rx["distance"] = d
                rx["cords"]["r"] = d * 100.0
        run(sextant.update_trilateration_and_zone(hass, [{"entity": "e", "data": data}], "e"))
        entry = next(i for i in sextant.apitricords if i["ent"] == "e")
        out.append({k: copy.deepcopy(entry[k]) for k in ("floor", "floors", "floor_cands", "cords")})
    return out


def test_a_flat_bias_field_leaves_a_whole_election_untouched(monkeypatch):
    """The rollout plan is "lay it flat, confirm nothing moved". Nothing must
    move: not the winner, not the odds, not one digit of any score."""
    bare = _void_election(monkeypatch, lambda layout, ff: None)

    def lay_flat(layout, ff):
        for fl in layout["floor"]:
            fl["bias_field"] = ff.flat((0, 0, 1000, 1000), fl["scale"])
    assert _void_election(monkeypatch, lay_flat) == bare
    assert {e["floor"] for e in bare} == {"F"}          # the tie never unseats the incumbent
    assert bare[-1]["floor_cands"]["U"]["bias"] == 1.0


def test_a_shaped_bias_field_breaks_a_tie_the_evidence_cannot(monkeypatch):
    """Same void, but U's field says a fix landing here is to be believed.
    That is the only difference between the floors, and it decides it."""
    def shape(layout, ff):
        up = layout["floor"][1]
        up["bias_field"] = ff.flat((0, 0, 1000, 1000), up["scale"])
        ff.paint(up, [(0, 300), (400, 300), (400, 700), (0, 700)], 1.6)   # a landing around (2, 5) m
    seen = _void_election(monkeypatch, shape)
    assert seen[2]["floor"] == "F" and seen[-1]["floor"] == "U", [e["floor"] for e in seen]
    cands = seen[-1]["floor_cands"]
    assert cands["U"]["bias"] == 1.6 and cands["F"]["bias"] == 1.0
    assert cands["U"]["score"] == pytest.approx(cands["U"]["prox"] * 1.6, abs=1e-3)
    # Each contender reports its OWN fix, in its own floor's pixels.
    assert all(abs(c["fix"][0] - 200) < 5 and abs(c["fix"][1] - 500) < 5 for c in cands.values())
    assert all("house" not in c for c in cands.values())        # no pins: no house frame claimed


def test_registered_floors_report_their_fixes_in_one_house_frame(monkeypatch):
    """With pins, each contender's fix is also published in house metres - and
    here, where both floors hear the thing equally, they must agree."""
    def shape(layout, ff):
        for fl, (dx, dy) in zip(layout["floor"], ((0, 0), (0, 0))):
            fl["pins"] = [{"pin_id": n, "name": n, "cords": {"x": x + dx, "y": y + dy}}
                          for n, (x, y) in {"NW": (0, 0), "NE": (1000, 0), "SE": (1000, 1000)}.items()]
        layout["floor"][0]["level"], layout["floor"][1]["level"] = 0, 1
        layout["floor"][1]["elevation"] = 3.66
    cands = _void_election(monkeypatch, shape)[-1]["floor_cands"]
    assert cands["F"]["house"][2] == 0.0 and cands["U"]["house"][2] == 3.66
    assert cands["F"]["house"][:2] == pytest.approx([2.0, 5.0], abs=0.05)
    assert cands["U"]["house"][:2] == pytest.approx(cands["F"]["house"][:2], abs=0.05)
# ---------------------------------------------------------------------------
# Solves run in the executor; positions are pushed over the websocket
# ---------------------------------------------------------------------------


def test_solves_run_through_the_executor(monkeypatch):
    """The per-floor fits are the cycle's CPU work and must go through
    hass.async_add_executor_job, with the election and publish staying on
    the loop (they touch hass state)."""
    _reset_thing_state()
    hass = make_hass()
    hass.data["sextant_sensors"] = {f"sensor.e_sextant_{k}": _Sensor() for k in ("zone", "nearest_zone", "floor", "sub_zone")}
    calls = []
    real = hass.async_add_executor_job

    async def spy(func, *args):
        calls.append(func.__name__)
        return await real(func, *args)

    hass.async_add_executor_job = spy
    entry = _cycle(hass, _square_layout(), 2.0, 5.0)
    assert entry["zone"] == "Kitchen"
    assert calls == ["_solve_floor_jobs"]


class _Conn:
    def __init__(self):
        self.subscriptions = {}
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)

    def send_result(self, msg_id, result=None):
        self.sent.append({"id": msg_id, "type": "result", "result": result})


def test_websocket_subscribe_streams_each_cycle_and_unsubscribes():
    hass = make_hass()
    hass.data[sextant.DOMAIN] = {"apitricords": [{"ent": "e", "zone": "Kitchen"}], "rl_offline": ["dead_rx"]}
    conn = _Conn()

    run(sextant._ws_subscribe(hass, conn, {"id": 7, "type": "sextant/subscribe"}))

    # Result first, then an immediate event carrying the current state.
    assert conn.sent[0]["type"] == "result" and conn.sent[0]["id"] == 7
    first = conn.sent[1]
    assert first["type"] == "event" and first["id"] == 7
    assert first["event"]["positions"] == [{"ent": "e", "zone": "Kitchen"}]
    assert first["event"]["offline_receivers"] == ["dead_rx"]
    assert "stamp" in first["event"]

    # A cycle's push reaches the subscriber.
    sextant.async_dispatcher_send(hass, sextant.SIGNAL_BPS_UPDATE, {"stamp": 1, "positions": [], "offline_receivers": []})
    assert len(conn.sent) == 3 and conn.sent[2]["event"]["stamp"] == 1

    # Unsubscribing (what HA's unsubscribe_events does) stops the stream.
    conn.subscriptions[7]()
    sextant.async_dispatcher_send(hass, sextant.SIGNAL_BPS_UPDATE, {"stamp": 2})
    assert len(conn.sent) == 3


def test_websocket_command_is_registered_once(monkeypatch):
    hass = make_hass()
    sextant._register_websocket(hass)
    sextant._register_websocket(hass)
    from sextant import ws as ws_module
    commands = hass.data["_ws_commands"]
    assert commands[0] is sextant._ws_subscribe
    assert len(commands) == 1 + len(ws_module.COMMANDS)          # every panel command, once
    assert all(hasattr(c, "_ws_schema") for c in commands)          # each carries its schema


def test_sensors_are_created_for_a_thing_added_after_setup(monkeypatch):
    """A device added to Bermuda while HA runs is positioned from the next
    cycle; its zone/floor sensors must appear then too, not at the next restart."""
    import sextant.sensor as sensor_mod
    hass = make_hass()
    added = []
    hass.data["sextant_sensors"] = {}
    hass.data["sextant_add_entities"] = lambda ents, update_before_add=False: added.extend(ents)
    monkeypatch.setattr(sensor_mod, "find_bermuda_via_device", lambda *a, **k: None)
    monkeypatch.setattr(sensor_mod, "normalize_sextant_registry_entity_ids_from_cache", lambda *a, **k: None)
    sensor_mod.ensure_sensors_for_things(hass, ["tile_24d1093b0211"])
    assert sorted(hass.data["sextant_sensors"]) == sorted(
        f"sensor.tile_24d1093b0211_{suffix}" for suffix, _ in sensor_mod.SENSOR_KINDS)
    assert len(added) == len(sensor_mod.SENSOR_KINDS)
    # Steady state: nothing new, nothing added, no registry work.
    sensor_mod.ensure_sensors_for_things(hass, ["tile_24d1093b0211"])
    assert len(added) == len(sensor_mod.SENSOR_KINDS)
    # Without the callback (platform not set up yet) it is a no-op.
    hass.data.pop("sextant_add_entities")
    sensor_mod.ensure_sensors_for_things(hass, ["other"])
    assert "sensor.other_sextant_room" not in hass.data["sextant_sensors"]


# ---------------------------------------------------------------------------
# Sub-zone election: smoothed membership, exit hysteresis, dwell, zone lock
# ---------------------------------------------------------------------------


def _sofa_polys():
    from shapely.geometry import Polygon
    # Living room 0..10 m; the sofa is a 2 m x 1 m box at (2..4, 2..3); the
    # desk is at (7..9, 7..8). 100 px/m.
    sofa = Polygon([(200, 200), (400, 200), (400, 300), (200, 300)])
    desk = Polygon([(700, 700), (900, 700), (900, 800), (700, 800)])
    # The fourth item is the classes the spot takes; empty means any of them.
    return [("Sofa", "Living", sofa, frozenset()), ("Desk", "Living", desk, frozenset()),
            ("Hook", "Hall", Polygon([(0, 0), (10, 0), (10, 10)]), frozenset())]


def _sub(entity, point, now, *, zone="Living", locked=False, layout=None, scale=100.0, fp=None):
    from shapely.geometry import Point
    layout = layout or {"tuning": {"subzone_switch_secs": 20.0, "zone_prob_smoothing": 0.6}}
    return sextant._elect_subzone(entity, "F", zone, locked, Point(*point), None, _sofa_polys(), scale, layout, now=now, fp=fp)


# The two ways a spot went wrong on 2026-09-19, end to end through the
# election rather than one helper at a time.

def test_a_still_thing_keeps_a_small_spot_while_its_fix_wanders():
    """David's watch on a 0.7 x 0.5 m bedside table: the fix wandered one to
    two metres all night, and 3.17.49 dropped it off the spot."""
    sextant._subzone_state.clear()
    t = 1000.0
    # The watch's real spread on 2026-09-20: 1.0 to 2.4 m from a spot 0.7 m wide.
    wander = [(300, 250), (250, 380), (380, 450), (300, 540), (120, 260), (300, 250),
              (500, 180), (260, 520), (300, 300), (350, 480)]
    for i in range(4):                        # put on the table: fixes land on it
        _sub("w", (300, 250), t + i * 10)
    assert _sub("w", (300, 250), t + 60) == ("Sofa", "Living")
    for i, pt in enumerate(wander * 3):       # half an hour of lying there
        got = _sub("w", pt, t + 100 + i * 20, locked=True)
        assert got == ("Sofa", "Living"), f"left the spot at {pt}"


def test_a_pin_in_a_spot_gets_a_blocked_thing_in_but_not_one_across_the_room():
    """A cat on the couch blocks the couch's own proxies, so its pins carry
    it in; the same pins must not hold another cat that walked away."""
    sextant._subzone_state.clear()
    t = 2000.0
    # One pin in the middle of the sofa, matched perfectly.
    sextant._set_truth_marks([{"id": 7, "entity": "cat", "floor": "F", "x": 300.0, "y": 250.0,
                               "samples": [{"t": 1.0, "gain": 1.0, "estimator": "fingerprint",
                                            "thing_vec": {"aa": 2.0}, "raw_vec": {"aa": 2.0}, "floors": {}}] * 3}])
    try:
        fp = {"refs": [("mark:7", 0.4)]}
        # On the sofa, weak membership: the pin is what gets it in.
        for dt in (0, 20, 40, 60):
            got = _sub("cat", (300, 250), t + dt, fp=fp)
        assert got == ("Sofa", "Living")
        # Three metres away, still matching that pin: it must not be held.
        for dt in (100, 120, 140, 160, 180, 200, 220):
            got = _sub("cat", (300, 900), t + dt, fp=fp)
        assert got == ("unknown", "Living")
    finally:
        sextant._set_truth_marks([])


def test_subzone_needs_smoothed_membership_and_dwell_to_enter():
    sextant._subzone_state.clear()
    t = 1000.0
    assert _sub("e", (300, 250), t) == ("unknown", "Living")        # share 0.4 after one cycle: not yet
    assert _sub("e", (300, 250), t + 10) == ("unknown", "Living")   # 0.64 >= 0.5: pending
    assert _sub("e", (300, 250), t + 29) == ("unknown", "Living")   # dwell not served
    assert _sub("e", (300, 250), t + 31) == ("Sofa", "Living")      # 20 s after the challenge began


def test_subzone_is_left_only_beyond_the_margin_and_after_the_dwell():
    sextant._subzone_state.clear()
    t = 1000.0
    for dt in (0, 10, 30, 31):
        out = _sub("e", (300, 250), t + dt)
    assert out == ("Sofa", "Living")
    # Half a metre outside the sofa polygon: within the 1 m margin, hold.
    for dt in range(40, 200, 10):
        assert _sub("e", (300, 350), t + dt) == ("Sofa", "Living")
    # Two metres away: leaves, but only after the dwell.
    assert _sub("e", (300, 500), t + 200) == ("Sofa", "Living")
    assert _sub("e", (300, 500), t + 210) == ("Sofa", "Living")
    assert _sub("e", (300, 500), t + 221) == ("unknown", "Living")


def test_subzone_holds_while_the_zone_is_locked_and_follows_the_zone():
    sextant._subzone_state.clear()
    t = 1000.0
    for dt in (0, 10, 30, 31):
        _sub("e", (300, 250), t + dt)
    # The thing is declared still by the zone election: a fix that wobbles
    # just outside (within subzone_unlock_margin) keeps the sub-zone.
    assert _sub("e", (300, 350), t + 100, locked=True) == ("Sofa", "Living")
    assert _sub("e", (300, 380), t + 200, locked=True) == ("Sofa", "Living")
    # A fix that wanders further, but not far (a watch on a bedside table
    # wanders a metre or two while it lies there), still keeps the spot.
    assert _sub("e", (300, 500), t + 250, locked=True) == ("Sofa", "Living")
    # But the room lock holds the ROOM, not the sofa: a fix well away
    # (subzone_lock_release_m) leaves, once the smoothed membership has fallen
    # and the dwell has passed. (Leela crossed the Great Room while the
    # couch's pins still matched her; she stayed on the couch.)
    for dt in (300, 320, 340, 360, 380, 400, 420):
        got = _sub("e", (300, 900), t + dt, locked=True)
    assert got == ("unknown", "Living")
    # A different elected zone: its sub-zones only, state starts over.
    assert _sub("e", (300, 250), t + 300, zone="Hall") == ("unknown", "Hall")
    # A zone with no sub-zones at all publishes unknown immediately.
    assert _sub("e", (300, 250), t + 310, zone="Kitchen") == ("unknown", "Kitchen")


def test_subzone_switches_to_a_clearly_better_neighbour():
    sextant._subzone_state.clear()
    t = 1000.0
    for dt in (0, 10, 30, 31):
        _sub("e", (300, 250), t + dt)
    # Straight onto the desk: the desk's share overtakes and, after the
    # dwell, wins outright without passing through unknown.
    seen = [_sub("e", (800, 750), t + 40 + dt)[0] for dt in range(0, 80, 10)]
    assert seen[0] == "Sofa" and seen[-1] == "Desk" and "unknown" not in seen


# --- Floor election: k-nearest proximity and the per-floor bias ---------------

def test_candidates_carry_the_mean_of_the_k_nearest_slants():
    def layout(k=None):
        lay = {"floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [], "receivers": [
            {"entity_id": f"r{i}", "cords": {"x": i * 100.0, "y": 0.0, "r": d * 100.0}, "distance": d}
            for i, d in enumerate((1.0, 2.0, 3.0, 10.0))
        ]}]}
        if k is not None:
            lay["tuning"] = {"floor_proximity_k": k}
        return lay
    cand = sextant.extract_candidate_floors([{"entity": "e", "data": layout()}], "e")[0]
    assert cand["nearest_m"] == 1.0
    assert abs(cand["near_k_m"] - 2.0) < 1e-9            # default k = 3: (1 + 2 + 3) / 3
    cand = sextant.extract_candidate_floors([{"entity": "e", "data": layout(1)}], "e")[0]
    assert cand["near_k_m"] == 1.0                        # k = 1 is the old nearest-only behaviour
    cand = sextant.extract_candidate_floors([{"entity": "e", "data": layout(8)}], "e")[0]
    assert abs(cand["near_k_m"] - 4.0) < 1e-9             # more than the floor has: all of them


def test_floor_bias_reads_the_layout_and_ignores_junk():
    layout = {"floor": [{"name": "Ground", "bias": 1.2}, {"name": "Up", "bias": "hot"}, {"name": "Down", "bias": 0}, {"name": "Attic"}]}
    assert sextant._floor_bias(layout, "Ground") == 1.2
    assert sextant._floor_bias(layout, "Up") == 1.0
    assert sextant._floor_bias(layout, "Down") == 1.0
    assert sextant._floor_bias(layout, "Attic") == 1.0
    assert sextant._floor_bias(layout, "Nowhere") == 1.0
    assert sextant._floor_bias([], "Ground") == 1.0


def test_subzone_probs_come_from_the_election_state():
    sextant._subzone_state.pop("e", None)
    assert sextant._subzone_probs("e") is None
    sextant._subzone_state["e"] = {"floor": "F", "zone": "Z", "value": ("Couch", "Z"), "probs": {"Couch": 0.66666, "unknown": 0.33334}, "pending": None}
    assert sextant._subzone_probs("e") == {"Couch": 0.667, "unknown": 0.333}
    sextant._subzone_state.pop("e", None)


# --- Near-field anchor -----------------------------------------------------------

def test_anchor_snaps_a_thing_sitting_on_one_proxy_and_releases_with_hysteresis():
    layout = {"floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [], "receivers": []}], "tuning": {"anchor_secs": 20}}
    def rx(near, far=2.0, third=2.5):
        return [
            {"entity_id": "bedside", "cords": {"x": 100.0, "y": 100.0}, "distance": near},
            {"entity_id": "wall", "cords": {"x": 400.0, "y": 100.0}, "distance": far},
            {"entity_id": "door", "cords": {"x": 100.0, "y": 500.0}, "distance": third},
        ]
    sextant._anchor_state.pop("w", None)
    t = 1000.0
    assert sextant._elect_anchor("w", "F", rx(0.5), layout, now=t) is None          # first sighting: pending
    assert sextant._elect_anchor("w", "F", rx(0.5), layout, now=t + 10) is None     # dwell not met
    a = sextant._elect_anchor("w", "F", rx(0.6), layout, now=t + 21)
    assert a == {"slug": "bedside", "x": 100.0, "y": 100.0}
    # Reads open to 1.2 m (under the 1.5 m release): still anchored.
    assert sextant._elect_anchor("w", "F", rx(1.2), layout, now=t + 30)["slug"] == "bedside"
    # Past the release distance, but only for a moment: held.
    assert sextant._elect_anchor("w", "F", rx(2.0), layout, now=t + 40)["slug"] == "bedside"
    assert sextant._elect_anchor("w", "F", rx(1.0), layout, now=t + 45)["slug"] == "bedside"
    # Past the release distance for the whole dwell: released.
    assert sextant._elect_anchor("w", "F", rx(2.0), layout, now=t + 50)["slug"] == "bedside"
    assert sextant._elect_anchor("w", "F", rx(2.0), layout, now=t + 71) is None
    assert "w" not in sextant._anchor_state


def test_anchor_needs_a_clear_nearest_and_can_be_disabled():
    layout = {"floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [], "receivers": []}], "tuning": {"anchor_secs": 0}}
    two_close = [
        {"entity_id": "a", "cords": {"x": 0.0, "y": 0.0}, "distance": 0.5},
        {"entity_id": "b", "cords": {"x": 100.0, "y": 0.0}, "distance": 0.8},   # not twice as far: ambiguous
    ]
    sextant._anchor_state.pop("w", None)
    assert sextant._elect_anchor("w", "F", two_close, layout, now=1.0) is None
    clear = [{"entity_id": "a", "cords": {"x": 0.0, "y": 0.0}, "distance": 0.5}, {"entity_id": "b", "cords": {"x": 100.0, "y": 0.0}, "distance": 1.6}]
    assert sextant._elect_anchor("w", "F", clear, layout, now=2.0)["slug"] == "a"      # anchor_secs 0: at once
    # A second proxy earning the anchor takes over immediately.
    swapped = [{"entity_id": "a", "cords": {"x": 0.0, "y": 0.0}, "distance": 1.6}, {"entity_id": "b", "cords": {"x": 100.0, "y": 0.0}, "distance": 0.4}]
    assert sextant._elect_anchor("w", "F", swapped, layout, now=3.0)["slug"] == "b"
    # A floor change forgets the anchor.
    assert sextant._elect_anchor("w", "G", swapped, layout, now=4.0)["slug"] == "b" and sextant._anchor_state["w"]["floor"] == "G"
    off = {"floor": layout["floor"], "tuning": {"anchor_max_m": 0}}
    assert sextant._elect_anchor("w", "G", swapped, off, now=5.0) is None and "w" not in sextant._anchor_state


def test_selftest_reports_rooms_and_the_breakdown_by_floor_and_room():
    recs = SQUARE + [("r5", 300, 300)]            # r5 has no samples: unsolved
    hass = _hass_with(recs, _exact_samples(SQUARE))
    hass.data["sextant"]["layout"]["floor"][0]["zones"] = [
        {"zone_id": "z1", "entity_id": "West", "poly": True, "cords": [{"x": -10, "y": -10}, {"x": 50, "y": -10}, {"x": 50, "y": 110}, {"x": -10, "y": 110}]},
        {"zone_id": "z2", "entity_id": "East", "poly": True, "cords": [{"x": 50, "y": -10}, {"x": 110, "y": -10}, {"x": 110, "y": 110}, {"x": 50, "y": 110}]},
        {"zone_id": "z3", "entity_id": "Empty", "poly": True, "cords": [{"x": 200, "y": 0}, {"x": 250, "y": 0}, {"x": 250, "y": 50}, {"x": 200, "y": 50}]},
        {"zone_id": "z4", "entity_id": "Void", "no_go": True, "poly": True, "cords": [{"x": 290, "y": 290}, {"x": 310, "y": 290}, {"x": 310, "y": 310}, {"x": 290, "y": 310}]},
    ]
    res = sextant.run_selftest(hass)
    assert {r["entity"]: r["room"] for r in res["receivers"]} == {"r1": "West", "r3": "West", "r2": "East", "r4": "East"}
    assert res["unsolved"] and res["unsolved"][0]["entity"] == "r5" and res["unsolved"][0]["room"] is None  # a no-go area is not a room
    assert res["floors"] == ["F"] and res["rooms"] == {"F": ["West", "East", "Empty"]}

    bd = sextant.selftest_breakdown(res)
    assert [f["floor"] for f in bd["floors"]] == ["F"]
    assert bd["floors"][0]["solved"] == 4 and bd["floors"][0]["unsolved"] == 1 and bd["floors"][0]["cep95_m"] < 0.5
    by_room = {(r["floor"], r["room"]): r for r in bd["rooms"]}
    assert by_room[("F", "West")]["solved"] == 2 and by_room[("F", "East")]["solved"] == 2
    assert by_room[("F", "West")]["worst"] in ("r1", "r3")
    assert by_room[("F", "Empty")] == {"floor": "F", "room": "Empty", "solved": 0, "unsolved": 0}  # a room with no proxy
    assert by_room[("F", None)]["unsolved"] == 1 and by_room[("F", None)]["solved"] == 0
    # Rooms with proxies come before the empty one.
    assert [r["room"] for r in bd["rooms"]][-2:] == ["Empty", None] or [r["room"] for r in bd["rooms"]][-1] == "Empty"

    state, attrs = sextant._selftest_summary(res)
    assert state == attrs["cep95_m"] and set(attrs["per_room_cep95_m"]) == {"F / West", "F / East"}
    assert attrs["per_floor_cep95_m"] == {"F": attrs["cep95_m"]}


def test_selftest_breakdown_of_an_empty_result_is_empty():
    assert sextant.selftest_breakdown({"receivers": [], "unsolved": [], "counts": {}}) == {"floors": [], "rooms": []}


def test_legacy_rectangle_zone_corners_are_ordered_before_the_point_test():
    # Scan-order corners (a bow tie if joined as stored) still make a rectangle.
    coords = {"floor": [{"name": "F", "scale": 1, "receivers": [], "zones": [
        {"entity_id": "R", "poly": False, "cords": [{"x": 0, "y": 0}, {"x": 10, "y": 10}, {"x": 10, "y": 0}, {"x": 0, "y": 10}]}]}]}
    rooms = sextant._selftest_rooms(coords)
    assert sextant._room_at(rooms["F"], 5, 5) == "R" and sextant._room_at(rooms["F"], 15, 5) is None


def test_selftest_takes_candidate_corrections_and_a_floor_filter():
    base = sextant.run_selftest(_hass_with(SQUARE, _exact_samples(SQUARE)))
    assert max(r["error_m"] for r in base["receivers"]) < 0.05
    # A candidate that inflates every reading r2 takes moves the others off (never written anywhere).
    skewed = sextant.run_selftest(_hass_with(SQUARE, _exact_samples(SQUARE)), corrections={"r2": 1.6})
    assert max(r["error_m"] for r in skewed["receivers"]) > 0.2
    hass = _hass_with(SQUARE, _exact_samples(SQUARE))
    hass.data["sextant"]["layout"]["floor"].append({"name": "Up", "scale": SCALE, "receivers": [{"entity_id": "u1", "cords": {"x": 0, "y": 0}}]})
    only = sextant.run_selftest(hass, floors={"F"})
    assert {r["entity"] for r in only["receivers"]} == {"r1", "r2", "r3", "r4"} and not any(u["entity"] == "u1" for u in only["unsolved"])


# --- spots that only take certain classes ------------------------------------

def _class_polys():
    """A bedside table for a phone, a watch or keys, and a cat bed for the cat,
    a couple of metres apart in the same room."""
    from shapely.geometry import Polygon
    bedside = Polygon([(200, 200), (400, 200), (400, 300), (200, 300)])
    catbed = Polygon([(700, 700), (900, 700), (900, 800), (700, 800)])
    return [("Bedside", "Bedroom", bedside, frozenset({"phone", "watch", "keys"})),
            ("Cat bed", "Bedroom", catbed, frozenset({"cat"}))]


LAYOUT_CLASSES = {
    "tuning": {"subzone_switch_secs": 20.0, "zone_prob_smoothing": 0.6},
    "thing_classes": {"phone": "phone", "watch": "watch", "meg": "cat", "nameless": ""},
}


def _settle(entity, point, polys, layout=LAYOUT_CLASSES, t=1000.0):
    from shapely.geometry import Point
    sextant._subzone_state.pop(entity, None)
    out = None
    for dt in (0, 10, 20, 31, 41):
        out = sextant._elect_subzone(entity, "F", "Bedroom", False, Point(*point), None,
                                     polys, 100.0, layout, now=t + dt)
    return out


BEDSIDE, CATBED = (300, 250), (800, 750)


def test_a_spot_only_takes_the_classes_it_is_given():
    """The phone on the bedside table, the cat in her bed, and neither in the other."""
    polys = _class_polys()
    assert _settle("phone", BEDSIDE, polys) == ("Bedside", "Bedroom")
    assert _settle("meg", CATBED, polys) == ("Cat bed", "Bedroom")
    # Sitting exactly where the other's spot is still gets nothing.
    assert _settle("meg", BEDSIDE, polys) == ("unknown", "Bedroom")
    assert _settle("phone", CATBED, polys) == ("unknown", "Bedroom")


def test_a_spot_restricted_to_several_classes_takes_any_of_them():
    polys = _class_polys()
    assert _settle("watch", BEDSIDE, polys) == ("Bedside", "Bedroom")


def test_a_thing_with_no_class_only_gets_the_open_spots():
    restricted = _class_polys()
    assert _settle("nameless", BEDSIDE, restricted) == ("unknown", "Bedroom")
    # The same spot without a class list takes it, as every spot did before.
    open_spot = [("Bedside", "Bedroom", restricted[0][2], frozenset())]
    assert _settle("nameless", BEDSIDE, open_spot) == ("Bedside", "Bedroom")
    assert _settle("meg", BEDSIDE, open_spot) == ("Bedside", "Bedroom")


def test_a_family_class_takes_its_members():
    """Person on a spot means a person however they are classed; Pet the dog or cat."""
    polys = [("Sofa", "Bedroom", _class_polys()[0][2], frozenset({"person"})),
             ("Basket", "Bedroom", _class_polys()[1][2], frozenset({"paw"}))]
    layout = {"tuning": {"subzone_switch_secs": 20.0, "zone_prob_smoothing": 0.6},
              "thing_classes": {"dad": "man", "kid": "child", "her": "woman", "someone": "person",
                                "meg": "cat", "rex": "dog", "phone": "phone"}}
    for entity in ("dad", "kid", "her", "someone"):
        assert _settle(entity, BEDSIDE, polys, layout) == ("Sofa", "Bedroom"), entity
    for entity in ("meg", "rex"):
        assert _settle(entity, CATBED, polys, layout) == ("Basket", "Bedroom"), entity
    # The phone is in neither family, and a family member is not a family.
    assert _settle("phone", BEDSIDE, polys, layout) == ("unknown", "Bedroom")
    assert _settle("meg", BEDSIDE, polys, layout) == ("unknown", "Bedroom")


def test_a_specific_class_is_not_satisfied_by_the_family():
    """A spot asking for Man is not met by something classed merely Person."""
    assert sextant.spot_accepts(frozenset({"man"}), "person") is False
    assert sextant.spot_accepts(frozenset({"person"}), "man") is True
    assert sextant.spot_accepts(frozenset({"paw"}), "cat") is True
    assert sextant.spot_accepts(frozenset({"cat"}), "paw") is False
    assert sextant.spot_accepts(frozenset({"person"}), "cat") is False
    # A hook or a shelf drawn for Bag takes the backpack, the purse and the
    # suitcase; a spot drawn for Luggage takes only luggage.
    assert sextant.spot_accepts(frozenset({"bag"}), "purse") is True
    assert sextant.spot_accepts(frozenset({"bag"}), "luggage") is True
    assert sextant.spot_accepts(frozenset({"bag"}), "backpack") is True
    assert sextant.spot_accepts(frozenset({"luggage"}), "bag") is False
    assert sextant.spot_accepts(frozenset({"bag"}), "keys") is False
    # The families are exactly the three documented ones.
    assert set(sextant.CLASS_FAMILIES) == {"person", "paw", "bag"}


def test_spot_class_helpers():
    assert sextant.spot_classes({"classes": ["cat", "dog"]}) == frozenset({"cat", "dog"})
    assert sextant.spot_classes({"classes": []}) == frozenset()
    assert sextant.spot_classes({}) == frozenset() and sextant.spot_classes(None) == frozenset()
    assert sextant.spot_classes({"classes": "cat"}) == frozenset()          # a bad shape is "any"
    assert sextant.spot_classes({"classes": ["cat", "", 7]}) == frozenset({"cat"})
    assert sextant.spot_accepts(frozenset(), "anything") is True            # unrestricted
    assert sextant.spot_accepts(frozenset({"cat"}), "cat") is True
    assert sextant.spot_accepts(frozenset({"cat"}), "phone") is False
    assert sextant.thing_class({"thing_classes": {"meg": "cat"}}, "meg") == "cat"
    assert sextant.thing_class({"thing_classes": {}}, "meg") == ""
    assert sextant.thing_class(None, "meg") == "" and sextant.thing_class({}, "meg") == ""


def test_a_fix_resting_on_the_unlock_margin_still_releases():
    """
    A lock must not stall on a fix that sits exactly at the unlock margin.

    From a real case: a bag in the laundry room solved 1.02 m from the foyer
    against a 1.00 m margin, so the away clock started, and any cycle that
    wobbled a centimetre closer wiped it. The dwell never completed, the room
    sensor read foyer for as long as the bag sat there, and the map drew the
    bag in the laundry room the whole time.
    """
    sextant._zone_state.clear()
    for t in (0.0, 10.0, 20.0, 30.0):
        zone, locked = _elect("e", 90, t)
    assert (zone, locked) == ("Kitchen", True)

    # Now parked just past the margin (1 m = 100 px), jittering across it by a
    # couple of centimetres either way - never coming properly back.
    seen = []
    for i, t in enumerate((40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0)):
        x = 202 if i % 2 else 199  # 1.02 m and 0.99 m past the boundary
        seen.append(_elect("e", x, t))
    assert seen[0] == ("Kitchen", True), "the lock should hold at first"
    assert seen[-1][0] == "Dining", f"the lock must release: {seen}"


def test_a_wrong_first_guess_is_not_locked_in():
    """
    From a real case: after a restart a watch on a couch - a metre from three
    room edges - had its first cycle land in the foyer. It was sitting still,
    so the lock froze that guess 20 s later, and the fix sitting half a metre
    into the right room could never release it (inside the 1 m margin).
    """
    sextant._zone_state.clear()
    assert _elect("e", 60, 0.0)[0] == "Kitchen"        # one noisy first fit
    seen = [_elect("e", 150, t) for t in (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0)]   # really 0.5 m into Dining, still
    assert ("Kitchen", True) not in seen, f"a guess the evidence contradicts must not lock: {seen}"
    assert seen[-1][0] == "Dining"
    # ...and once it has earned the room, it locks as before.
    later = [_elect("e", 150, t) for t in (80.0, 90.0, 100.0, 110.0)]
    assert later[-1] == ("Dining", True)


def test_a_lock_yields_when_the_evidence_has_left_it_even_inside_the_margin():
    """Parked 0.7 m into the next room - inside the 1 m unlock margin, so the
    distance rule never fires - with the locked room holding almost none of
    the evidence. That is a thing that moved, not a thing jittering."""
    sextant._zone_state.clear()
    for t in (0.0, 10.0, 20.0, 30.0):
        zone, locked = _elect("e", 50, t)
    assert (zone, locked) == ("Kitchen", True)
    seen = [_elect("e", 170, 40.0 + 10 * i) for i in range(14)]   # 40 s .. 170 s
    assert seen[0] == ("Kitchen", True)                  # held at first: could be jitter
    # The smoothed share takes ~50 s to fall under 10 %, then 2 x zone_unlock_secs.
    assert ("Kitchen", True) in seen[:8], "must not give way in the first minute"
    assert seen[-1][0] == "Dining", f"the lock must yield to evidence that has left it: {seen}"


def test_boundary_jitter_well_inside_the_margin_still_holds_the_lock():
    """The hysteresis must not cost the protection it was added around."""
    sextant._zone_state.clear()
    for t in (0.0, 10.0, 20.0, 30.0):
        zone, locked = _elect("e", 90, t)
    assert locked is True
    # Wandering 30 cm over the line and back, as a resting thing's fix does.
    for i, t in enumerate((40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0, 120.0)):
        zone, locked = _elect("e", 130 if i % 2 else 95, t)
        assert (zone, locked) == ("Kitchen", True)


# ---------------------------------------------------------------------------
# The fused location sensor
# ---------------------------------------------------------------------------


def test_location_publishes_the_spot_when_there_is_one():
    """In a spot, the state is the spot and the room is still available."""
    state, attrs = sextant._location_state("Master Bedroom", "David Bedside Table", "Master Bedroom", "Second Floor")
    assert state == "David Bedside Table"
    assert attrs == {
        "kind": "spot",
        "room": "Master Bedroom",
        "spot": "David Bedside Table",
        "floor": "Second Floor",
        "area_id": None,
        "floor_id": None,
    }


def test_location_falls_back_to_the_room():
    """Out of any spot, the room is the finest answer there is."""
    state, attrs = sextant._location_state("Kitchen", "unknown", "unknown", "Ground Floor")
    assert state == "Kitchen"
    assert attrs["kind"] == "room" and attrs["room"] == "Kitchen" and attrs["spot"] is None


def test_location_takes_a_spots_room_from_its_parent():
    """
    A spot's room comes from the spot, not from the elected room.

    The two disagree for a cycle or two while a thing crosses a boundary, and
    publishing "Couch" with the room it is not in is worse than lagging.
    """
    state, attrs = sextant._location_state("Dining Room", "Couch", "Great Room", "Ground Floor")
    assert state == "Couch" and attrs["room"] == "Great Room"


def test_a_spot_with_no_parent_room_still_reports_the_room_the_thing_is_in():
    """A spot drawn outside every room has no parent. The thing's room is
    still known from the election, and "unknown" would be a worse answer."""
    for orphan in (None, "unknown", ""):
        state, attrs = sextant._location_state("Foyer", "Shoe Rack", orphan, "Ground Floor")
        assert state == "Shoe Rack" and attrs["kind"] == "spot" and attrs["room"] == "Foyer"


def test_location_is_unknown_when_nothing_is_known():
    """A thing that has gone dark reads unknown, not blank."""
    state, attrs = sextant._location_state("unknown", "unknown", "unknown", "unknown")
    assert state == "unknown"
    assert attrs == {"kind": "room", "room": "unknown", "spot": None, "floor": "unknown", "area_id": None, "floor_id": None}


def test_location_never_publishes_an_empty_state():
    """Missing values must not reach the state machine as an empty string."""
    state, attrs = sextant._location_state(None, None, None, None)
    assert state == "unknown" and attrs["room"] == "unknown" and attrs["floor"] == "unknown"


# ---------------------------------------------------------------------------
# The cycle must not hold the event loop
# ---------------------------------------------------------------------------


def test_a_things_working_copy_isolates_what_the_cycle_writes_and_shares_the_rest():
    layout = _square_layout({"zone_switch_secs": 30})
    layout["floor"][0]["bias_field"] = {"cell_m": 1.0, "x0": 0, "y0": 0, "values": [[1.0] * 10] * 10}
    a, b = sextant._thing_layout(layout), sextant._thing_layout(layout)
    rx = a["floor"][0]["receivers"][0]
    rx["distance"], rx["quality"], rx["cords"]["r"] = 2.5, 0.8, 250.0     # everything a cycle writes
    for other in (layout, b):
        theirs = other["floor"][0]["receivers"][0]
        assert "distance" not in theirs and "quality" not in theirs and "r" not in theirs["cords"]
    # ...and nothing else is copied: that was the cost.
    assert a["floor"][0]["zones"] is layout["floor"][0]["zones"]
    assert a["floor"][0]["bias_field"] is layout["floor"][0]["bias_field"]
    assert a["tuning"] is layout["tuning"]
    # A receiver without coordinates (hand-edited file) must not break it.
    odd = {"floor": [{"name": "F", "receivers": [{"entity_id": "x"}, {"entity_id": "y", "cords": None}]}]}
    assert [r["entity_id"] for r in sextant._thing_layout(odd)["floor"][0]["receivers"]] == ["x", "y"]


def test_the_cycle_gives_the_event_loop_a_turn_between_things(monkeypatch):
    """Gathering every thing ran all their synchronous chunks back to back and
    stalled Home Assistant for a quarter of a second a cycle. Something else
    waiting on the loop must get to run between one thing and the next."""
    order = []

    async def fake_single(hass, data, eids):
        order.append(eids["entity"])        # no await inside: the worst case, a thing with nothing to solve

    monkeypatch.setattr(sextant, "process_single_entity", fake_single)

    async def bystander():
        for _ in range(3):
            await asyncio.sleep(0)
            order.append("loop")

    async def main():
        other = asyncio.ensure_future(bystander())
        await sextant.process_entities(None, [{"entity": e} for e in ("a", "b", "c", "d")])
        await other

    run(main())
    assert order[0] == "a" and "loop" in order[1:3], order   # the loop ran before thing c, not after thing d
    assert [o for o in order if o != "loop"] == ["a", "b", "c", "d"]


def test_a_failing_thing_is_named_not_dumped_and_rate_limited(monkeypatch, caplog):
    """One thing's bad data must not stop the rest - and its log line must not
    print the thing's working copy of the layout (tens of KB) every cycle."""
    monkeypatch.setattr(sextant, "_thing_error_counts", {}, raising=False)
    done = []

    async def fake_single(hass, data, eids):
        if eids["entity"] == "bad":
            raise ValueError("broken reading")
        done.append(eids["entity"])

    monkeypatch.setattr(sextant, "process_single_entity", fake_single)
    marker = "LAYOUT-PAYLOAD-" + "x" * 200
    batch = [{"entity": "bad", "data": {"floor": [{"name": marker}]}}, {"entity": "good", "data": {}}]
    caplog.set_level("ERROR")
    cycles = sextant.CYCLE_ERROR_REPEAT_EVERY + 1
    for _ in range(cycles):
        run(sextant.process_entities(None, batch))

    assert done == ["good"] * cycles, "the good thing still ran every cycle"
    lines = [r for r in caplog.records if "Positioning failed" in r.getMessage()]
    assert len(lines) == 2, "the first failure and one repeat, not one per cycle"
    assert all("bad" in r.getMessage() and marker not in r.getMessage() for r in lines)

    # Recovering resets the count, so a fresh failure is reported straight away.
    monkeypatch.setattr(sextant, "process_single_entity", lambda *a: asyncio.sleep(0))
    run(sextant.process_entities(None, batch))
    assert "bad" not in sextant._thing_error_counts


# --------------------------------------------------------------------------- #
# Close-range fade of a stretching calibration correction
# --------------------------------------------------------------------------- #
def test_close_range_fade_shape():
    f = sextant._close_range_correction
    assert f(1.7, 0.5, {}) == 1.0            # beside the proxy: no stretch
    assert f(1.7, 1.0, {}) == 1.0
    assert f(1.7, 2.5, {}) == 1.7            # across the room: all of it
    mid = f(1.7, 1.75, {})
    assert abs(mid - 1.7 ** 0.5) < 1e-9      # halfway in, geometrically
    assert f(0.8, 0.5, {}) == 0.8            # a shrinking correction is never faded
    assert f(1.7, 0.5, {"tuning": {"correction_close_fade": False}}) == 1.7
    tuned = {"tuning": {"correction_fade_near_m": 0.2, "correction_fade_far_m": 0.6}}
    assert f(1.7, 0.5, tuned) > 1.4 and f(1.7, 0.7, tuned) == 1.7
    # A far edge set inside the near one cannot divide by zero.
    assert f(1.7, 1.0, {"tuning": {"correction_fade_near_m": 2.0, "correction_fade_far_m": 1.0}}) == 1.0


def _radii_with_correction(state, correction, tuning=None):
    class St:
        def __init__(self):
            self.state = state
            self.attributes = {"unit_of_measurement": "m"}

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}, "correction": correction}
    data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
    if tuning is not None:
        data["tuning"] = tuning
    run(sextant.update_receiver_radii(Hass(), {"entity": "watch", "data": data}))
    return rec


def test_a_watch_beside_a_stretched_proxy_keeps_its_short_reading():
    # The bedside C5: calibration x1.73, the watch 30 cm away reads 0.9 m.
    assert abs(_radii_with_correction("0.9", 1.73)["distance"] - 0.9) < 1e-9
    assert abs(_radii_with_correction("0.9", 1.73, {"correction_close_fade": False})["distance"] - 0.9 * 1.73) < 1e-9
    assert abs(_radii_with_correction("4.0", 1.73)["distance"] - 4.0 * 1.73) < 1e-9


def test_mark_references_follow_a_change_of_correction():
    import copy
    layout = {"floor": [{"name": "F", "scale": SCALE, "receivers": [
        {"entity_id": "p", "address": "AA", "cords": {"x": 0.0, "y": 0.0}, "correction": 2.0},
        {"entity_id": "q", "address": "BB", "cords": {"x": 100.0, "y": 0.0}},
    ]}]}
    mark = {"id": 1, "entity": "watch", "floor": "F", "x": 10.0, "y": 0.0, "samples": [
        {"t": 1.0, "gain": 1.0, "estimator": "fingerprint", "thing_vec": {"aa": 8.0, "bb": 3.0},
         "raw_vec": {"aa": 4.0, "bb": 3.0},
         "floors": {"F": {"weighted": [[0.0, 0.0, 320.0, 1.0, 320.0], [100.0, 0.0, 120.0, 1.0, 120.0]],
                          "bounds": None, "min_wr": 20.0, "scale": SCALE}}}] * 3}
    sextant._set_truth_marks([mark])
    try:
        assert sextant._mark_refs(layout)[0]["vector"]["aa"] == 8.0
        recal = copy.deepcopy(layout)
        recal["floor"][0]["receivers"][0]["correction"] = 1.25
        assert sextant._mark_refs(recal)[0]["vector"]["aa"] == 5.0
        # Only the reading itself carries the fade: 4 m is past it, so no change there.
        assert sextant._mark_refs(recal)[0]["vector"]["bb"] == 3.0
    finally:
        sextant._set_truth_marks([])


# --- a proxy on the spot, and a spot's own entry share ---------------------- #
def test_spot_proxy_evidence_fades_with_distance_and_with_a_close_runner_up():
    def lay(mine, other):
        return {"floor": [{"name": "F", "receivers": [
            {"entity_id": "table", "distance": mine}, {"entity_id": "wall", "distance": other}]}]}
    ev = sextant._spot_proxy_evidence
    assert ev(lay(1.0, 3.0), "table") == 1.0                 # close, clearly nearest
    assert abs(ev(lay(1.6, 4.0), "table") - 0.5) < 1e-9      # halfway out of 1.2..2.0 m
    assert ev(lay(3.0, 9.0), "table") == 0.0                 # nearest, but ten feet away
    assert ev(lay(1.0, 1.2), "table") == 0.0                 # another proxy nearly as close
    assert 0.0 < ev(lay(1.0, 1.6), "table") < 1.0
    assert ev(lay(1.0, 3.0), "other") == 0.0 and ev(lay(1.0, 3.0), None) == 0.0


def _spot_layout(mine, other, **spot):
    return {"tuning": {"subzone_switch_secs": 20.0, "zone_prob_smoothing": 0.6},
            "floor": [{"name": "F", "subzones": [{"entity_id": "Sofa", **spot}],
                       "receivers": [{"entity_id": "sofa_px", "distance": mine}, {"entity_id": "wall", "distance": other}]}]}


def test_a_proxy_on_the_spot_puts_a_thing_there_that_the_estimate_misses():
    # The fix sits 0.2 m off the sofa: on geometry alone, never the sofa.
    sextant._subzone_state.clear()
    lay = _spot_layout(0.9, 3.0, proxy="sofa_px")
    t = 1000.0
    outs = [_sub("e", (300, 320), t + dt, layout=lay) for dt in (0, 10, 31)]
    assert outs[-1] == ("Sofa", "Living")
    # The same, with the sofa's proxy three metres off: nothing.
    sextant._subzone_state.clear()
    far = _spot_layout(3.0, 6.0, proxy="sofa_px")
    assert all(_sub("e", (300, 320), t + dt, layout=far) == ("unknown", "Living") for dt in (0, 10, 31, 60))


def test_a_spot_can_set_its_own_entry_share():
    # Half the sofa proxy's evidence (1.6 m): 0.5 smoothed up to ~0.39 over three
    # cycles - short of the default 0.5, enough for a spot that asks for 0.3.
    sextant._subzone_state.clear()
    t = 1000.0
    default = _spot_layout(1.6, 5.0, proxy="sofa_px")
    assert all(_sub("e", (300, 320), t + dt, layout=default) == ("unknown", "Living") for dt in (0, 10, 31, 60))
    sextant._subzone_state.clear()
    own = _spot_layout(1.6, 5.0, proxy="sofa_px", enter_prob=0.3)
    outs = [_sub("e", (300, 320), t + dt, layout=own) for dt in (0, 10, 20, 31, 45)]
    assert outs[-1] == ("Sofa", "Living")


def test_two_proxies_on_one_spot_are_not_each_others_runner_up():
    # A couch with an outlet at each end: a phone on it is close to both.
    lay = {"floor": [{"name": "F", "receivers": [
        {"entity_id": "left", "distance": 0.9}, {"entity_id": "right", "distance": 1.1},
        {"entity_id": "wall", "distance": 3.0}]}]}
    ev = sextant._spot_proxy_evidence
    assert ev(lay, "left") == 0.0                   # alone, "right" is its close rival
    assert ev(lay, ("left", "right")) == 1.0        # together, only "wall" is a rival
    assert ev(lay, ["right", "left"]) == 1.0
    assert sextant._spot_proxies({"proxy": ["a", "", 3, "b"]}) == ("a", "b")
    assert sextant._spot_proxies({"proxy": "a"}) == ("a",) and sextant._spot_proxies({}) == ()


def test_no_lock_in_the_first_minutes_after_a_start():
    # A phone on a kitchen counter against the foyer wall was locked into the
    # foyer by the wandering first fixes after a restart.
    sextant._zone_state.clear()
    warm = {"tuning": {"zone_lock_warmup_secs": 120.0}}
    for t in (0.0, 10.0, 20.0, 30.0, 60.0, 110.0):
        assert _elect("e", 90, t, layout=warm) == ("Kitchen", False)
    assert _elect("e", 90, 130.0, layout=warm) == ("Kitchen", True)



def test_marks_guide_their_own_thing_and_its_class_not_everything():
    # Michelle's phone marked on the couch must not drag David's watch there.
    layout = {"thing_classes": {"m_phone": "phone", "d_phone": "phone", "d_watch": "watch", "meg": "cat", "socks": "cat"},
              "floor": [{"name": "F", "scale": SCALE, "receivers": []}]}
    sample = {"t": 1.0, "gain": 1.0, "estimator": "fingerprint", "thing_vec": {"aa": 2.0, "bb": 3.0},
              "raw_vec": {"aa": 2.0, "bb": 3.0}, "floors": {}}
    sextant._set_truth_marks([
        {"id": 1, "entity": "m_phone", "floor": "F", "x": 10.0, "y": 0.0, "samples": [sample] * 3},
        {"id": 2, "entity": "meg", "floor": "F", "x": 50.0, "y": 0.0, "samples": [sample] * 3},
    ])
    try:
        slugs = lambda ent, lay=layout: sorted(r["slug"] for r in (sextant._mark_refs(lay, ent) or []))  # noqa: E731
        assert slugs("m_phone") == ["mark:1"]          # its own
        assert slugs("d_phone") == ["mark:1"]          # same class
        assert slugs("d_watch") == []                  # a different kind of device
        assert slugs("socks") == ["mark:2"]            # the cats share Meg's
        assert slugs("unclassified") == []
        own = {**layout, "tuning": {"fingerprint_marks_scope": "own"}}
        assert slugs("d_phone", own) == [] and slugs("m_phone", own) == ["mark:1"]
        everyone = {**layout, "tuning": {"fingerprint_marks_scope": "all"}}
        assert slugs("d_watch", everyone) == ["mark:1", "mark:2"]
    finally:
        sextant._set_truth_marks([])


def test_a_room_linked_to_an_area_publishes_its_area_and_floor_ids():
    layout = {"floor": [{"name": "Ground Floor", "floor_id": "ground", "zones": [
        {"entity_id": "Kitchen", "area_id": "kitchen"},
        {"entity_id": "Hall"},
        {"entity_id": "Void", "no_go": True, "area_id": "nope"},
    ]}]}
    assert sextant.room_area(layout, "Ground Floor", "Kitchen") == ("kitchen", "ground")
    assert sextant.room_area(layout, "Ground Floor", "Hall") == (None, "ground")      # unlinked room
    assert sextant.room_area(layout, "Ground Floor", "Void") == (None, "ground")      # a no-go area is nowhere
    assert sextant.room_area(layout, "Attic", "Kitchen") == (None, None)
    assert sextant.room_area(None, "Ground Floor", "Kitchen") == (None, None)
    # On a spot, the area is the spot's room's.
    _state, attrs = sextant._location_state("Hall", "Peninsula", "Kitchen", "Ground Floor", layout)
    assert attrs["room"] == "Kitchen" and attrs["area_id"] == "kitchen" and attrs["floor_id"] == "ground"



def test_a_pin_speaks_only_for_a_thing_that_is_there():
    """Pins are shared by a class, so a cat across the room matches the couch
    pins too - without this it would be held on a couch it had left."""
    from shapely.geometry import Polygon
    couch = Polygon([(0, 0), (300, 0), (300, 300), (0, 300)])   # 100 px per metre
    pins = {"mark:1": ("Ground", 150.0, 150.0)}
    refs = [("mark:1", 0.5)]
    ev = lambda at: sextant.spot_pin_evidence({"refs": refs}, "Ground", couch, pins, at=at, margin_px=100.0)  # noqa: E731
    assert ev((150.0, 150.0)) == 1.0          # on the couch
    assert ev((320.0, 150.0)) == 0.8          # 0.2 m outside, nearly all of it
    assert ev((350.0, 150.0)) == 0.5          # half a metre outside, half
    assert ev((450.0, 150.0)) == 0.0          # a metre and a half away: nothing
    # Leela's case: 2.86 m from the couch while its pins still match.
    assert ev((586.0, 150.0)) == 0.0
    # No position given: the old behaviour, evidence wherever the match is.
    assert sextant.spot_pin_evidence({"refs": refs}, "Ground", couch, pins) == 1.0


def test_pins_inside_a_spot_are_evidence_of_being_in_it():
    """A cat on a couch blocks the couch's own proxies; the pins do not care."""
    from shapely.geometry import Polygon
    couch = Polygon([(0, 0), (300, 0), (300, 300), (0, 300)])   # pixels
    pins = {"mark:1": ("Ground", 150.0, 150.0), "mark:2": ("Ground", 900.0, 900.0),
            "mark:3": ("Second", 150.0, 150.0)}
    ev = lambda refs: sextant.spot_pin_evidence({"refs": refs}, "Ground", couch, pins)  # noqa: E731
    # The only match is a pin on the couch: all of the fix came from it.
    assert ev([("mark:1", 0.5)]) == 1.0
    # A proxy of the same quality alongside it: about half.
    assert 0.45 < ev([("mark:1", 0.5), ("great_room_rrn00", 0.5)]) < 0.55
    # A closer match counts for more (weights go as 1/(score + 0.05)^2).
    assert ev([("mark:1", 0.3), ("great_room_rrn00", 0.9)]) > 0.85
    assert ev([("mark:1", 0.9), ("great_room_rrn00", 0.3)]) < 0.15
    # Pins elsewhere, on another floor, or no pins at all say nothing.
    assert ev([("mark:2", 0.3)]) == 0.0
    assert ev([("mark:3", 0.3)]) == 0.0
    assert ev([("great_room_rrn00", 0.3)]) == 0.0
    assert sextant.spot_pin_evidence(None, "Ground", couch, pins) == 0.0
    assert sextant.spot_pin_evidence({"refs": []}, "Ground", couch, pins) == 0.0
    assert sextant.spot_pin_evidence({"refs": [("mark:1", "bad")]}, "Ground", couch, pins) == 0.0


def test_a_spot_remembers_when_it_was_entered():
    """The Live list says how long a thing has been where it is, and that has
    to survive a restart - so it comes from the election, not a sensor."""
    sextant._subzone_state.clear()
    t = 6000.0
    for i in range(4):
        _sub("e", (300, 250), t + i * 10)
    assert _sub("e", (300, 250), t + 60) == ("Sofa", "Living")
    entered = sextant._subzone_state["e"]["since"]
    assert t <= entered <= t + 60
    # Still there a while later: the time it arrived does not move.
    for dt in (100, 200, 300):
        _sub("e", (300, 250), t + dt)
    assert sextant._subzone_state["e"]["since"] == entered
    # Off the sofa, and it is a new answer with a new time.
    for dt in (400, 420, 440, 460, 480, 500):
        got = _sub("e", (300, 900), t + dt)
    assert got == ("unknown", "Living")
    assert sextant._subzone_state["e"]["since"] > entered


def test_the_floor_switch_margin_is_tunable():
    """A near-tie between two floors drifted on a half-hour period at 0.05;
    the margin has to be settable without a release."""
    assert sextant.TUNING_SPEC["floor_switch_margin"][0] == sextant.FLOOR_SWITCH_MARGIN == 0.05
    assert sextant._tuning({"tuning": {"floor_switch_margin": 0.1}}, "floor_switch_margin") == 0.1


# --- "Use in positioning" off -------------------------------------------------

def _solve_layout(idle_slug=None):
    """Three placed proxies on one floor, all hearing the thing; one may be
    kept out of positioning with "solve": false."""
    rxs = [
        {"entity_id": "a", "cords": {"x": 100.0, "y": 100.0, "r": 100.0}, "distance": 1.0},
        {"entity_id": "b", "cords": {"x": 400.0, "y": 100.0, "r": 200.0}, "distance": 2.0},
        {"entity_id": "c", "cords": {"x": 100.0, "y": 500.0, "r": 300.0}, "distance": 3.0},
    ]
    for r in rxs:
        if r["entity_id"] == idle_slug:
            r["solve"] = False
    return {"floor": [{"name": "F", "scale": 100.0, "receivers": rxs, "zones": [], "subzones": []}], "tuning": {"floor_proximity_k": 1}}


def test_a_proxy_kept_out_of_positioning_is_not_a_solver_input_nor_the_nearest():
    """The kitchen Shelly: placed and heard at 1 m, self-tested 9 m from its
    plug. Off, the solve and the proximity term no longer see it."""
    on = sextant.extract_candidate_floors([{"entity": "e", "data": _solve_layout()}], "e")[0]
    assert len(on["cords"]) == 3 and on["nearest_m"] == 1.0
    off = sextant.extract_candidate_floors([{"entity": "e", "data": _solve_layout("a")}], "e")[0]
    assert len(off["cords"]) == 2 and off["nearest_m"] == 2.0
    assert [h[0] for h in off["heard"]] == ["b", "c"]


def test_a_proxy_kept_out_of_positioning_gives_no_spot_or_anchor_evidence():
    layout = _solve_layout("a")
    assert sextant._spot_proxy_evidence(layout, "a") == 0.0, "its own spot gets nothing from it"
    layout_on = _solve_layout()
    assert sextant._spot_proxy_evidence(layout_on, "a") > 0.0
    sextant._anchor_state.pop("w", None)
    rxs = [{"entity_id": "a", "cords": {"x": 100.0, "y": 100.0}, "distance": 0.5, "solve": False},
           {"entity_id": "b", "cords": {"x": 400.0, "y": 100.0}, "distance": 2.0},
           {"entity_id": "c", "cords": {"x": 100.0, "y": 500.0}, "distance": 2.5}]
    lay = {"floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [], "receivers": rxs}], "tuning": {"anchor_secs": 20}}
    for dt in (0, 10, 21, 30):
        assert sextant._elect_anchor("w", "F", rxs, lay, now=1000.0 + dt) is None, "never anchors to a proxy that is off"
    sextant._anchor_state.pop("w", None)
    assert sextant._solves({"entity_id": "x"}) and sextant._solves({"solve": True}) and not sextant._solves({"solve": False})
