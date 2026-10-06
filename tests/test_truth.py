"""Truth marks (truth.py) and the per-thing blend weight."""
import math

import sextant
from sextant import truth, fingerprint as fp


def _sample(t, floor="F", vec=None, gain=1.0, x=500.0, y=500.0):
    # Four proxies on a 10 m square (100 px/m) ranging a thing at (x, y), exactly.
    pts = [(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)]
    weighted = [(px, py, math.hypot(px - x, py - y), 1.0, math.hypot(px - x, py - y)) for px, py in pts]
    jobs = [{"floor": floor, "weighted": weighted, "bounds": (-100, -100, 1100, 1100), "min_wr": 50.0, "scale": 100.0}]
    return jobs, vec or {"a": 1.0}, gain


def test_buffer_keeps_recent_cycles_per_thing_and_floor():
    b = truth.Buffer(maxlen=3)
    for i in range(5):
        jobs, vec, gain = _sample(i)
        b.remember("phone", jobs, vec, gain, "fused", now=100 + i)
    assert len(b.samples("phone")) == 3                         # ring
    assert [s["t"] for s in b.samples("phone", since=103)] == [103, 104]
    assert b.samples("phone", floor="G") == []                  # not solved on that floor
    assert b.samples("nobody") == []
    b.forget("phone")
    assert b.samples("phone") == []


def test_estimator_for_weight():
    assert truth.estimator_for(0.0) == "geometric" and truth.estimator_for(1.0) == "fingerprint"
    assert truth.estimator_for(0.4) == "fused" and truth.estimator_for(None) is None


def test_evaluate_scores_the_geometric_fit_against_the_mark():
    b = truth.Buffer()
    for i in range(4):
        jobs, vec, gain = _sample(i, x=500.0, y=500.0)
        b.remember("phone", jobs, vec, gain, "geometric", now=i)
    rows = truth.evaluate(b.samples("phone"), "F", (500.0, 500.0), 100.0, lambda p: "room" if 400 <= p[0] <= 600 else None,
                          sextant._solve_floor_jobs, lambda gain: None)
    assert rows and rows[0]["estimator"] == "geometric" and rows[0]["samples"] == 4
    assert rows[0]["mean_m"] < 0.2 and rows[0]["room_ok"] == 1.0
    # With no references only the geometric row exists; a mark elsewhere reads the error honestly.
    far = truth.evaluate(b.samples("phone"), "F", (900.0, 500.0), 100.0, lambda p: None, sextant._solve_floor_jobs, lambda gain: None)
    assert far[0]["mean_m"] > 3.5


def test_evaluate_sweeps_blend_and_gain_when_references_exist():
    b = truth.Buffer()
    for i in range(3):
        jobs, vec, gain = _sample(i, vec={"a": 2.0, "b": 8.0}, gain=1.0)
        b.remember("tag", jobs, vec, gain, "fused", now=i)
    refs = [{"slug": "r1", "address": "a", "x": 500.0, "y": 500.0, "vector": {"a": 0.5, "b": 8.0}}]
    rows = truth.evaluate(b.samples("tag"), "F", (500.0, 500.0), 100.0, lambda p: None, sextant._solve_floor_jobs, lambda gain: refs)
    weights = {r["weight"] for r in rows}
    assert weights == {0.0, 0.25, 0.5, 0.75, 1.0}
    assert len([r for r in rows if r["weight"] == 0.0]) == 1              # gain is meaningless for geometric
    assert len([r for r in rows if r["weight"] == 0.5]) == len(truth.GAIN_STEPS)
    assert rows == sorted(rows, key=lambda r: (r["mean_m"], -r["room_ok"]))


def test_mark_reference_is_the_median_vector_in_probe_scale():
    mark = {"id": 7, "floor": "F", "x": 1.0, "y": 2.0, "samples": [
        {"gain": 2.0, "thing_vec": {"a": 4.0, "b": 6.0, "c": 1.0}},
        {"gain": 2.0, "thing_vec": {"a": 6.0, "b": 6.0}},
        {"gain": 2.0, "thing_vec": {"a": 5.0, "b": 6.0}},
    ]}
    ref = truth.mark_reference(mark)
    assert ref["slug"] == "mark:7" and ref["floor"] == "F" and ref["address"] is None
    assert ref["vector"] == {"a": 2.5, "b": 3.0}                          # median / gain; "c" read once -> dropped
    assert truth.mark_reference({"id": 1, "floor": "F", "x": 0, "y": 0, "samples": []}) is None
    layout = {"floor": [{"name": "F", "scale": 100.0, "receivers": []}]}
    refs = fp.build_references(layout, {}, gain=3.0, extra=[ref])
    assert refs["F"][0]["vector"] == {"a": 7.5, "b": 9.0} and refs["F"][0]["slug"] == "mark:7"


def test_summarize_averages_marks_per_thing():
    out = truth.summarize({1: ("a", {"mean_m": 1.0, "room_ok": 1.0}), 2: ("a", {"mean_m": 3.0, "room_ok": 0.5}), 3: ("b", None)})
    assert out == {"a": {"marks": 2, "mean_m": 2.0, "room_ok": 0.75}}


def test_blend_weight_decides_the_estimator_and_is_validated():
    layout = {"tuning": {"position_estimator": "fused"}, "thing_fp_weights": {"tile": 0.0, "watch": 1.0, "cat": 0.3, "bad": 7}, "thing_estimators": {"bad": "geometric"}}
    assert sextant._thing_fp_weight(layout, "cat") == 0.3 and sextant._thing_fp_weight(layout, "bad") is None
    assert sextant._thing_estimator(layout, "tile") == "geometric"
    assert sextant._thing_estimator(layout, "watch") == "fingerprint"
    assert sextant._thing_estimator(layout, "cat") == "fused"
    assert sextant._thing_estimator(layout, "bad") == "geometric"      # invalid weight: the override still counts
    assert sextant._fingerprint_wanted({"tuning": {"position_estimator": "geometric"}, "thing_fp_weights": {"cat": 0.3}})
    db = fp.ReferenceDB()
    saved = sextant._fingerprint_db
    sextant._fingerprint_db = db
    try:
        sextant._seed_thing_gain({"thing_fp_gains": {"tile": 1.8}}, "tile")
        assert db.gain_for("tile") == 1.8
        db.thing_gain["tile"] = 1.2
        sextant._seed_thing_gain({"thing_fp_gains": {"tile": 1.8}}, "tile")
        assert db.gain_for("tile") == 1.2                                  # a learned gain is not overwritten
    finally:
        sextant._fingerprint_db = saved


# --- rebase: marks follow the corrections in force now --------------------- #
def _rebase_sample(raw=True):
    # Receiver "a" at (0, 0) stretched x2 when the mark was made, "b" at (300, 0) x1.
    s = {"t": 1.0, "gain": 1.0, "estimator": "geometric",
         "thing_vec": {"a": 2.0, "b": 3.0},
         "floors": {"F": {"weighted": [[0.0, 0.0, 200.0, 1.0, 200.0], [300.0, 0.0, 300.0, 1.0, 300.0]],
                          "bounds": None, "min_wr": 50.0, "scale": 100.0}}}
    if raw:
        s["raw_vec"] = {"a": 1.0, "b": 3.0}
    return s


def _fns(corr):
    def mult(rx, raw):
        return corr.get(rx)
    def rx_at(_floor, x, _y):
        return ("a", None) if x == 0.0 else ("b", None)
    return mult, rx_at


def test_rebase_applies_todays_corrections_to_the_raw_readings():
    mult, rx_at = _fns({"a": 1.5, "b": 1.0})
    (s,) = truth.rebase([_rebase_sample()], mult, rx_at, 0.5)
    assert s["thing_vec"] == {"a": 1.5, "b": 3.0}
    assert s["floors"]["F"]["weighted"][0] == [0.0, 0.0, 150.0, 1.0, 150.0]
    assert s["floors"]["F"]["weighted"][1][2] == 300.0
    assert _rebase_sample()["thing_vec"]["a"] == 2.0  # the stored mark is not touched


def test_rebase_recovers_raw_from_an_old_mark_with_the_correction_it_ran_with():
    # No raw_vec: divide out the full correction (mult(rx, None)), then apply
    # the one in force now for that raw range - here faded to nothing.
    def mult(rx, raw):
        full = {"a": 2.0, "b": 1.0}[rx]
        return full if raw is None or raw > 2.5 else 1.0
    _m, rx_at = _fns({})
    (s,) = truth.rebase([_rebase_sample(raw=False)], mult, rx_at, 0.5)
    assert s["thing_vec"]["a"] == 1.0
    assert s["floors"]["F"]["weighted"][0][2] == 100.0


def test_rebase_keeps_what_it_cannot_place():
    (s,) = truth.rebase([_rebase_sample()], lambda rx, raw: None, lambda *a: None, 0.5)
    assert s["thing_vec"] == {"a": 2.0, "b": 3.0}
    assert s["floors"]["F"]["weighted"][0][2] == 200.0


def test_rebase_takes_the_vertical_leg_off_again():
    mult = lambda rx, raw: 1.0  # noqa: E731
    rx_at = lambda _f, x, _y: ("a", 1.2) if x == 0.0 else ("b", None)  # noqa: E731
    s = _rebase_sample()
    s["raw_vec"] = {"a": 2.0, "b": 3.0}
    (out,) = truth.rebase([s], mult, rx_at, 0.5)
    assert abs(out["floors"]["F"]["weighted"][0][2] - 100.0 * (2.0 ** 2 - 1.2 ** 2) ** 0.5) < 1e-9
    assert out["floors"]["F"]["weighted"][0][4] == 200.0  # the slant stays the slant


def test_the_buffer_holds_its_points_as_tuples_the_collector_ignores():
    """A tuple that holds only numbers is dropped from the cyclic collector's
    books at its first collection; a list never is. The buffer is 48 samples
    per thing of these, replaced continuously."""
    import gc

    from sextant import truth as truth_mod

    buf = truth_mod.Buffer()
    buf.remember("e", [{"floor": "F", "weighted": [[1, 2, 3, 0.5], [4.0, 5.0, 6.0, 1.0]], "bounds": [0, 0, 10, 10],
                        "min_wr": 0.001, "scale": 100.0}], {"rx": 1.5}, 1.0, "fused", now=100.0)
    fj = buf.samples("e")[0]["floors"]["F"]
    assert fj["weighted"] == ((1.0, 2.0, 3.0, 0.5), (4.0, 5.0, 6.0, 1.0))
    assert fj["bounds"] == (0.0, 0.0, 10.0, 10.0)
    gc.collect()
    assert not gc.is_tracked(fj["weighted"]) and not gc.is_tracked(fj["weighted"][0])
    assert not gc.is_tracked(fj["bounds"])
