"""Regression tests for the position history (the map's time scrubber).

Three things have to hold or the feature is worse than useless:

* the recording gate must keep a walk legible while a stationary device costs
  almost nothing,
* a query must span the WHOLE requested window after decimation (a trail that
  silently stops half-way is indistinguishable from the device stopping), and
* the on-disk record must survive a restart, a torn line, and a clear.
"""
import asyncio
import json
import os

import sextant
from sextant import history as H
from conftest import make_hass


ENT = "phone"
FLOOR = "Main"
SCALE = 40.0  # px per metre


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def hist(**over):
    cfg = H.history_config({})
    cfg.update(over)
    return H.PositionHistory(cfg)


def all_points(h, ent=ENT):
    return h.query(ent, 0, 1e12, 10 ** 9)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_defaults_are_the_documented_ones():
    cfg = H.history_config(None)
    assert cfg["max_age"] == H.DEFAULT_MAX_AGE
    assert cfg["min_interval"] == H.DEFAULT_MIN_INTERVAL
    assert cfg["min_move_m"] == H.DEFAULT_MIN_MOVE_M
    assert cfg["heartbeat"] == H.DEFAULT_HEARTBEAT
    assert cfg["enabled"] is True


def test_max_age_is_clamped_to_the_supported_ceiling():
    # An unbounded window would grow the ring until the process dies.
    assert H.history_config({"history_max_age": 99_999_999})["max_age"] == H.MAX_AGE_LIMIT
    assert H.history_config({"history_max_age": -5})["max_age"] > 0


def test_booleans_are_not_accepted_as_numbers():
    # isinstance(True, int) holds in Python: a hand-edited `true` must fall
    # back to the default, not silently configure a 1-metre move threshold.
    assert H.history_config({"history_min_move_m": True})["min_move_m"] == H.DEFAULT_MIN_MOVE_M


def test_disabled_records_nothing():
    h = hist(enabled=False)
    assert h.record(ENT, 1000.0, 0.0, 0.0, FLOOR, SCALE) is False
    assert h.entities() == []


# --------------------------------------------------------------------------- #
# The recording gate
# --------------------------------------------------------------------------- #
def test_a_stationary_device_only_costs_its_heartbeat():
    h = hist()
    t0 = 1_000_000.0
    for i in range(120):           # 120 s of standing still, 1 Hz
        h.record(ENT, t0 + i, 5.0, 5.0, FLOOR, SCALE)
    # First point + one per 30 s heartbeat.
    assert all_points(h)["count"] == 4


def test_a_walk_is_kept_at_the_minimum_interval():
    h = hist()
    t0 = 1_000_000.0
    for i in range(60):            # 1 m/s for a minute
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    assert all_points(h)["count"] == 30   # every 2 s


def test_jitter_below_the_move_threshold_is_dropped():
    h = hist()
    t0 = 1_000_000.0
    for i in range(30):            # 10 cm of noise every 2 s for a minute
        h.record(ENT, t0 + i * 2, 0.1 * (i % 2), 0.0, FLOOR, SCALE)
    # Only the first point and the 30 s heartbeat: none of the jitter earns a
    # point of its own, so a device sitting on a table costs almost nothing.
    assert all_points(h)["count"] == 2


def test_a_floor_change_is_always_kept_and_breaks_the_line():
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 1.0, 1.0, "A", 40.0)
    # Sub-interval and sub-movement, but a different floor: the two points live
    # in different pixel frames, so joining them would draw a nonsense segment.
    assert h.record(ENT, t0 + 0.5, 1.0, 1.0, "B", 50.0) is True
    got = all_points(h)
    assert got["count"] == 2
    assert got["gap"] == [1, 1]
    assert got["floors"] == ["A", "B"]
    assert got["scales"] == [40.0, 50.0]


def test_clock_going_backwards_restarts_the_buffer():
    # The arrays are searched with bisect everywhere, so one out-of-order
    # append would make query() and evict() return nonsense. A clock that steps
    # backwards therefore starts the buffer over rather than corrupting it.
    h = hist()
    for i in range(5):
        h.record(ENT, 1_000_000.0 + i * 5, float(i), 0.0, FLOOR, SCALE)
    h.record(ENT, 999_000.0, 9.0, 9.0, FLOOR, SCALE)
    got = all_points(h)
    assert got["t"] == [999_000.0]
    assert got["gap"] == [1]
    assert list(got["t"]) == sorted(got["t"])


def test_a_dropout_breaks_the_line_before_the_prune_timeout():
    # A device unheard for a couple of minutes was not standing still; joining
    # across the silence would draw a straight line through whatever it
    # actually did. The explicit mark_gap only arrives after the much longer
    # position_timeout, so the recorder has to notice this itself.
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 0.0, 0.0, FLOOR, SCALE)
    h.record(ENT, t0 + 20, 5.0, 0.0, FLOOR, SCALE)     # normal, no break
    h.record(ENT, t0 + 200, 9.0, 0.0, FLOOR, SCALE)    # after a silence
    assert all_points(h)["gap"] == [1, 0, H.GAP_DROPOUT]


def test_mark_gap_breaks_the_next_segment():
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 0.0, 0.0, FLOOR, SCALE)
    h.record(ENT, t0 + 5, 5.0, 0.0, FLOOR, SCALE)
    h.mark_gap(ENT)                      # thing pruned for absence
    h.record(ENT, t0 + 600, 40.0, 0.0, FLOOR, SCALE)
    # 2 = the device really was unheard across it, not merely a new polyline.
    assert all_points(h)["gap"] == [1, 0, H.GAP_DROPOUT]


def test_non_finite_and_unnamed_input_is_refused():
    h = hist()
    assert h.record(ENT, float("nan"), 0.0, 0.0, FLOOR, SCALE) is False
    assert h.record(ENT, 1000.0, float("inf"), 0.0, FLOOR, SCALE) is False
    assert h.record("", 1000.0, 0.0, 0.0, FLOOR, SCALE) is False
    assert h.entities() == []


# --------------------------------------------------------------------------- #
# Eviction
# --------------------------------------------------------------------------- #
def test_points_older_than_the_window_are_evicted():
    # Regression: compaction used to be gated on `max_points // 10`, so a ring
    # holding far fewer points than the cap (6 h at one point per 2 s is ~10k
    # against a 200k cap) never compacted at all and max_age was ignored.
    h = hist(max_age=100.0)
    t0 = 1_000_000.0
    for i in range(0, 4000, 4):        # 1000 fixes; ~25 fit the window
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    got = all_points(h)
    assert got["t"][-1] == t0 + 3996   # the newest fix is always kept
    # Compaction is amortised, so a few expired points may linger; what must
    # hold is that the buffer tracks the WINDOW and not the length of the run.
    assert got["count"] < 40
    assert got["t"][-1] - got["t"][0] <= 100.0 * 1.5


def test_max_points_bounds_memory_independently_of_age():
    h = hist(max_age=10 ** 9, max_points=50)
    t0 = 1_000_000.0
    for i in range(500):
        h.record(ENT, t0 + i * 4, float(i), 0.0, FLOOR, SCALE)
    assert all_points(h)["count"] <= 50


def test_the_surviving_head_starts_a_new_line_after_eviction():
    # Whatever preceded the retained head is gone, so the first kept point must
    # not be joined to a predecessor that no longer exists.
    h = hist(max_age=50.0)
    t0 = 1_000_000.0
    for i in range(0, 200, 2):
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    assert all_points(h)["gap"][0] == 1


# --------------------------------------------------------------------------- #
# Query + decimation
# --------------------------------------------------------------------------- #
def test_query_windows_to_the_requested_range():
    h = hist(max_age=10 ** 9)
    t0 = 1_000_000.0
    for i in range(0, 600, 3):
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    got = h.query(ENT, t0 + 100, t0 + 200, 10 ** 9)
    assert got["count"] > 0
    assert all(t0 + 100 <= t <= t0 + 200 for t in got["t"])


def test_decimation_still_spans_the_whole_window():
    # The failure this guards against: a naive "first N points" cap makes the
    # trail stop early, which reads as the device having stopped moving.
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    t0 = 1_000_000.0
    for i in range(1000):
        h.record(ENT, t0 + i * 3, float(i), 0.0, FLOOR, SCALE)
    got = h.query(ENT, t0, t0 + 10 ** 6, 50)
    assert got["count"] <= 60
    assert got["t"][0] == t0
    assert got["t"][-1] == t0 + 999 * 3
    assert got["stride"] > 1


def test_decimation_never_drops_a_gap_or_a_floor_change():
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    t0 = 1_000_000.0
    for i in range(300):
        floor = "A" if i < 150 else "B"
        h.record(ENT, t0 + i * 3, float(i), 0.0, floor, SCALE)
    got = h.query(ENT, t0, t0 + 10 ** 6, 20)
    # The floor change is a gap, and every gap must survive the stride.
    assert sum(got["gap"]) >= 2
    assert set(got["floors"][i] for i in got["f"]) == {"A", "B"}


def test_query_of_an_unknown_thing_is_empty_not_an_error():
    got = hist().query("nobody", 0, 1e12, 100)
    assert got["count"] == 0 and got["t"] == []


def test_retained_reports_the_actual_span():
    h = hist(max_age=10 ** 9)
    t0 = 1_000_000.0
    for i in range(0, 100, 5):
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    span = h.retained(ENT)
    assert span["from"] == t0 and span["to"] == t0 + 95 and span["points"] == 20
    assert h.retained("nobody") is None


# --------------------------------------------------------------------------- #
# Disk segments
# --------------------------------------------------------------------------- #
def flush_to(h, dirpath):
    return H.append_segments(dirpath, h.drain_pending())


def test_round_trip_through_disk_is_lossless(tmp_path):
    d = str(tmp_path / "hist")
    h = hist(max_age=10 ** 9)
    import time as _t
    t0 = _t.time() - 500
    for i in range(100):
        h.record(ENT, t0 + i * 5, float(i) * 0.5, float(i) * 0.25, FLOOR, SCALE)
    assert flush_to(h, d) == all_points(h)["count"]

    before = all_points(h)
    back = hist(max_age=10 ** 9)
    back.load_rows(H.restore_recent(d, back.cfg))
    after = all_points(back)
    assert after["count"] == before["count"]
    assert max(abs(a - b) for a, b in zip(after["t"], before["t"])) == 0
    assert max(abs(a - b) for a, b in zip(after["x_m"], before["x_m"])) < 1e-3
    assert after["floors"] == before["floors"]


def test_a_torn_final_line_costs_only_that_line(tmp_path):
    d = str(tmp_path / "hist")
    h = hist(max_age=10 ** 9)
    import time as _t
    t0 = _t.time() - 200
    for i in range(20):
        h.record(ENT, t0 + i * 5, float(i), 0.0, FLOOR, SCALE)
    flush_to(h, d)
    day = H.day_key(t0)
    path = H.segment_path(d, day)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"e":"phone","t":123')      # power cut mid-append
    back = hist(max_age=10 ** 9)
    back.load_rows(H.restore_recent(d, back.cfg))
    assert all_points(back)["count"] == 20


def test_restore_ignores_rows_outside_the_window(tmp_path):
    d = str(tmp_path / "hist")
    os.makedirs(d, exist_ok=True)
    import time as _t
    now = _t.time()
    lines = [json.dumps({"e": ENT, "t": now - 100, "x": 1.0, "y": 1.0, "f": FLOOR, "s": SCALE}),
             json.dumps({"e": ENT, "t": now - 10 ** 6, "x": 9.0, "y": 9.0, "f": FLOOR, "s": SCALE})]
    H.append_segments(d, {H.day_key(now): lines})
    back = hist()
    back.load_rows(H.read_segments(d, H.list_day_keys(d)))
    assert all_points(back)["count"] == 1


def test_expired_day_segments_are_pruned(tmp_path):
    d = str(tmp_path / "hist")
    import time as _t
    now = _t.time()
    H.append_segments(d, {
        "20200101": [json.dumps({"e": ENT, "t": 1.0, "x": 0, "y": 0, "f": FLOOR})],
        H.day_key(now): [json.dumps({"e": ENT, "t": now, "x": 0, "y": 0, "f": FLOOR})],
    })
    removed = H.prune_segments(d, 6 * 3600, now)
    assert removed == ["20200101"]
    assert H.list_day_keys(d) == [H.day_key(now)]


def test_drop_entity_rewrites_segments_without_that_thing(tmp_path):
    d = str(tmp_path / "hist")
    import time as _t
    now = _t.time()
    day = H.day_key(now)
    H.append_segments(d, {day: [
        json.dumps({"e": "a", "t": now - 3, "x": 1, "y": 1, "f": FLOOR}),
        json.dumps({"e": "b", "t": now - 2, "x": 2, "y": 2, "f": FLOOR}),
        json.dumps({"e": "a", "t": now - 1, "x": 3, "y": 3, "f": FLOOR}),
    ]})
    assert H.drop_entity(d, "a") == 2
    rows = H.read_segments(d, H.list_day_keys(d))
    assert [r["e"] for r in rows] == ["b"]


def test_drop_entity_removes_a_segment_it_empties(tmp_path):
    d = str(tmp_path / "hist")
    import time as _t
    now = _t.time()
    H.append_segments(d, {H.day_key(now): [
        json.dumps({"e": "a", "t": now, "x": 1, "y": 1, "f": FLOOR})]})
    H.drop_entity(d, "a")
    assert H.list_day_keys(d) == []
    assert not os.path.exists(H.segment_path(d, H.day_key(now)) + ".tmp")


def test_forget_drops_only_that_things_queued_rows():
    # A clear must not be undone by a flush of rows queued moments earlier —
    # and must not take the other things' rows down with it.
    h = hist()
    t0 = 1_000_000.0
    h.record("a", t0, 0.0, 0.0, FLOOR, SCALE)
    h.record("b", t0, 0.0, 0.0, FLOOR, SCALE)
    h.forget("a")
    grouped = h.drain_pending()
    rows = [json.loads(line) for lines in grouped.values() for line in lines]
    assert [r["e"] for r in rows] == ["b"]
    assert h.entities() == ["b"]


def test_pending_queue_is_bounded():
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    h.MAX_PENDING = 10
    t0 = 1_000_000.0
    for i in range(100):
        h.record(ENT, t0 + i * 5, float(i), 0.0, FLOOR, SCALE)
    assert h.pending_count() == 10
    assert h.dropped_pending > 0


# The websocket commands + integration wiring
# --------------------------------------------------------------------------- #
from sextant import ws as ws_mod


class _Conn:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append(result)

    def send_error(self, msg_id, code, message):
        self.errors.append(message)


def _call(handler, hass, **msg):
    conn = _Conn()
    run(handler(hass, conn, {"id": 1, "type": "x", **msg}))
    assert not conn.errors, conn.errors
    return conn.results[-1]


def seeded_hass(tmp_path, **layout):
    hass = make_hass(tmp_path)
    hass.data["sextant"] = {"layout": dict(layout)}
    h = sextant.get_position_history(hass)
    import time as _t
    t0 = _t.time() - 300
    for i in range(60):
        h.record(ENT, t0 + i * 5, float(i) * 0.5, 0.0, FLOOR, SCALE)
    return hass, h, t0


def test_index_lists_what_is_retained(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    body = _call(ws_mod.ws_history_index, hass)
    assert [t["ent"] for t in body["things"]] == [ENT]
    assert body["things"][0]["points"] == h.retained(ENT)["points"]
    assert body["config"]["max_age"] == H.DEFAULT_MAX_AGE


def test_query_returns_metres_and_the_recording_scale(tmp_path):
    hass, _, t0 = seeded_hass(tmp_path)
    body = _call(ws_mod.ws_history_get, hass, entity=ENT, **{"from": t0, "to": t0 + 10 ** 6})
    assert body["count"] > 0
    assert body["floors"] == [FLOOR] and body["scales"] == [SCALE]
    assert body["x_m"][0] == 0.0            # metres, not pixels
    assert body["retained"]["points"] >= body["count"]


def test_query_honours_max_points(tmp_path):
    hass, _, t0 = seeded_hass(tmp_path)
    body = _call(ws_mod.ws_history_get, hass, entity=ENT, max_points=5, **{"from": t0, "to": t0 + 10 ** 6})
    assert body["count"] <= 6


def test_reversed_window_is_normalised(tmp_path):
    hass, _, t0 = seeded_hass(tmp_path)
    body = _call(ws_mod.ws_history_get, hass, entity=ENT, **{"from": t0 + 10 ** 6, "to": t0})
    assert body["count"] > 0


def test_layout_settings_reach_the_recorder(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    hass.data["sextant"]["layout"]["history_max_age"] = 900
    _call(ws_mod.ws_history_index, hass)
    assert h.cfg["max_age"] == 900.0


def test_clear_forgets_memory_and_disk(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    run(sextant.flush_position_history(hass))
    assert H.list_day_keys(sextant.history_dir(hass))
    body = _call(ws_mod.ws_history_clear, hass)
    assert body["cleared"] == "*"
    assert h.entities() == []
    assert H.list_day_keys(sextant.history_dir(hass)) == []


# --------------------------------------------------------------------------- #
# Spots, and the timeline the Live page draws
# --------------------------------------------------------------------------- #

def _walk(h, steps, start=1_000_000.0):
    """steps: [(seconds_after_start, room, spot), ...] recorded at one position."""
    for dt, room, spot in steps:
        h.record(ENT, start + dt, 1.0, 1.0, FLOOR, SCALE, room, spot)


def test_a_spot_change_is_recorded_even_when_nothing_moved():
    h = hist(min_interval=2, heartbeat=600, min_move_m=0.5)
    _walk(h, [(0, "Bedroom", None), (10, "Bedroom", "Bed"), (11, "Bedroom", "Bed")])
    q = all_points(h)
    assert [q["spots"][i] for i in q["sp"]] == ["", "Bed"]    # the change kept; the repeat 1 s later not


def test_spots_survive_the_disk_round_trip_and_old_rows_read_as_none():
    h = hist(min_interval=2, heartbeat=600)
    _walk(h, [(0, "Bedroom", "Bed"), (30, "Bedroom", None)])
    rows = [json.loads(line) for lines in h.drain_pending().values() for line in lines]
    assert rows[0]["sp"] == "Bed" and "sp" not in rows[1]
    rows.append({"e": ENT, "t": 1_000_060.0, "x": 1, "y": 1, "f": FLOOR, "z": "Hall"})   # written before spots existed
    back = hist(min_interval=2, heartbeat=600)
    back.load_rows(rows, now=1_000_100.0)
    q = all_points(back)
    assert [q["spots"][i] for i in q["sp"]] == ["Bed", "", ""]


def test_timeline_collapses_points_into_stays_that_meet_at_the_crossing():
    h = hist(min_interval=2, heartbeat=60)
    _walk(h, [(0, "Bedroom", "Bed"), (60, "Bedroom", "Bed"), (120, "Bedroom", None),
              (180, "Kitchen", None), (240, "Kitchen", None)])
    tl = h.timeline(ENT, 0, 2e6)
    stays = [(s["room"], s["spot"], s["start"] - 1_000_000, s["end"] - 1_000_000) for s in tl["stays"]]
    assert stays == [("Bedroom", "Bed", 0, 120), ("Bedroom", None, 120, 180), ("Kitchen", None, 180, 240)]
    assert tl["stays"][0]["partial"] is True        # it starts where the record starts
    assert tl["last_heard"] == 1_000_240.0


def test_timeline_never_claims_a_place_across_a_silence():
    h = hist(min_interval=2, heartbeat=30)
    _walk(h, [(0, "Office", None), (30, "Office", None)])
    _walk(h, [(3000, "Office", None), (3030, "Office", None)])   # unheard for most of an hour
    stays = h.timeline(ENT, 0, 2e6)["stays"]
    assert [bool(s.get("unheard")) for s in stays] == [False, True, False]
    assert stays[0]["end"] - 1_000_000 == 30 and stays[1]["end"] - 1_000_000 == 3000


def test_timeline_window_and_empty_cases():
    h = hist(min_interval=2, heartbeat=60)
    assert h.timeline("nobody", 0, 1e12)["stays"] == []
    _walk(h, [(0, "A", None), (100, "B", None), (200, "C", None)])
    tl = h.timeline(ENT, 1_000_150.0, 2e6)
    assert [s["room"] for s in tl["stays"]] == ["C"] and tl["stays"][0]["partial"] is False


def test_history_hours_from_the_tuning_page_sets_the_window():
    from sextant import history as hm
    assert hm.history_config({})["max_age"] == hm.DEFAULT_MAX_AGE
    assert hm.history_config({"tuning": {"history_hours": 24}})["max_age"] == 24 * 3600
    assert hm.history_config({"tuning": {"history_hours": 9999}})["max_age"] == hm.MAX_AGE_LIMIT
    # A hand-set history_max_age is more specific and wins.
    assert hm.history_config({"history_max_age": 7200, "tuning": {"history_hours": 24}})["max_age"] == 7200
