"""The panel's stability KPI (custom_components/sextant/kpi.py).

The metric code is duplicated from tools/flap_kpi.py on purpose (see the
module docstring); tests/test_flap_kpi.py covers that copy, this covers the
integration's. The restart case is the one that matters: a day with three
restarts used to read as a fleet-wide flap.
"""
from datetime import datetime, timedelta, timezone

from sextant import kpi

T0 = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


def _rows(*items):
    return [{"state": s, "last_changed": (T0 + timedelta(seconds=sec)).isoformat()} for sec, s in items]


def test_a_restart_is_not_a_change():
    """Kitchen, unavailable, unknown, Kitchen: the sensor went away and came
    back where it was. Zero changes, and the dead rows counted separately."""
    m = kpi.compute_metrics(_rows((0, "Kitchen"), (100, "unavailable"), (101, "unknown"), (102, "Kitchen")), window_hours=1)
    assert m["changes"] == 0
    assert m["flips"] == 0
    assert m["dead"] == 2
    assert m["states"] == 1
    assert m["top_pairs"] == []


def test_a_move_across_a_restart_is_one_change():
    m = kpi.compute_metrics(_rows((0, "Kitchen"), (100, "unavailable"), (102, "Office")), window_hours=1)
    assert m["changes"] == 1
    assert m["dead"] == 1
    assert m["top_pairs"] == [{"pair": ["Kitchen", "Office"], "count": 1}]


def test_three_restarts_on_a_stationary_thing_score_zero():
    """What today looked like: three restarts, the wallet never moved. It used
    to read as 6 changes with a median dwell of a few seconds."""
    rows = [(0, "Kitchen")]
    for t in (3600, 7200, 10800):
        rows += [(t, "unavailable"), (t + 20, "unknown"), (t + 21, "Kitchen")]
    m = kpi.compute_metrics(_rows(*rows), window_hours=4)
    assert m["changes"] == 0
    assert m["changes_per_hour"] == 0
    assert m["dead"] == 6
    assert m["median_dwell_s"] is None


def test_a_flip_around_a_restart_still_counts():
    """Kitchen, Office, restart, Kitchen: the flip is real, the restart is not."""
    m = kpi.compute_metrics(_rows((0, "Kitchen"), (30, "Office"), (60, "unavailable"), (70, "Kitchen")), window_hours=1)
    assert m["changes"] == 2
    assert m["flips"] == 1
    assert m["dead"] == 1
    # The dwell in Office runs to when Kitchen came back, not to the restart.
    assert m["median_dwell_s"] == 35.0


def test_a_window_that_opens_dead_starts_at_the_first_live_state():
    m = kpi.compute_metrics(_rows((0, "unavailable"), (5, "Kitchen"), (65, "Office")), window_hours=1)
    assert m["changes"] == 1
    assert m["dead"] == 1


def test_summary_rolls_up_the_live_changes_only():
    per = {
        "sensor.a_sextant_room": kpi.compute_metrics(_rows((0, "Kitchen"), (100, "unavailable"), (102, "Kitchen")), window_hours=1),
        "sensor.b_sextant_room": kpi.compute_metrics(_rows((0, "Kitchen"), (100, "Office")), window_hours=1),
    }
    s = kpi.summarise(per)["sextant_room"]
    assert s["changes"] == 1
    assert s["changes_per_thing_hour"] == 0.5


def _layout():
    """One floor, 100 px/m: Kitchen and Foyer touch, the Office is 4 m from both; a Basement room downstairs."""
    def rect(x0, y0, x1, y1):
        return [{"x": x0 * 100, "y": y0 * 100}, {"x": x1 * 100, "y": y0 * 100}, {"x": x1 * 100, "y": y1 * 100}, {"x": x0 * 100, "y": y1 * 100}]
    return {"floor": [
        {"name": "Ground", "scale": 100.0, "zones": [
            {"entity_id": "Kitchen", "cords": rect(0, 0, 5, 5)},
            {"entity_id": "Foyer", "cords": rect(5, 0, 7, 5)},   # shares a wall with the Kitchen
            {"entity_id": "Office", "cords": rect(11, 0, 15, 5)},  # 4 m beyond the Foyer
            {"entity_id": "Dot", "cords": [{"x": 0, "y": 0}]},     # not a polygon: ignored
        ]},
        {"name": "Basement", "scale": 100.0, "zones": [{"entity_id": "Basement", "cords": rect(0, 0, 5, 5)}]},
    ]}


def test_room_neighbours_are_rooms_within_reach_on_the_same_floor():
    n = kpi.room_neighbours(_layout())
    assert n["floor"] == {"Kitchen": "Ground", "Foyer": "Ground", "Office": "Ground", "Basement": "Basement"}
    assert n["pairs"] == {frozenset(("Kitchen", "Foyer"))}
    # A wider reach makes the Office a neighbour of the Foyer (4 m) but not of the Kitchen (6 m).
    assert kpi.room_neighbours(_layout(), reach_m=4.5)["pairs"] == {frozenset(("Kitchen", "Foyer")), frozenset(("Foyer", "Office"))}


def test_far_moves_are_same_floor_moves_between_non_neighbours():
    n = kpi.room_neighbours(_layout())
    rows = _rows((0, "Kitchen"), (100, "Foyer"), (200, "Office"), (300, "Kitchen"), (400, "Basement"), (500, "Kitchen"), (600, "Attic"))
    m = kpi.compute_metrics(rows, window_hours=12, neighbours=n)
    # Kitchen->Foyer: neighbours. Foyer->Office: far. Office->Kitchen: far.
    # Kitchen->Basement and back: floor changes, not far moves. ->Attic: not on the plan.
    assert m["changes"] == 6
    assert m["far_moves"] == 2
    assert m["far_moves_per_day"] == 4.0
    assert m["far_move_ratio"] == round(2 / 6, 3)
    # Without the plan the fields stay None, so the command-line tool's output matches.
    assert kpi.compute_metrics(rows, window_hours=12)["far_moves"] is None


def test_summary_rolls_far_moves_up_per_thing_day():
    n = kpi.room_neighbours(_layout())
    a = kpi.compute_metrics(_rows((0, "Kitchen"), (100, "Office")), window_hours=24, neighbours=n)
    b = kpi.compute_metrics(_rows((0, "Kitchen"), (100, "Foyer")), window_hours=24, neighbours=n)
    f = kpi.compute_metrics(_rows((0, "Ground"), (100, "Basement")), window_hours=24)
    s = kpi.summarise({"sensor.a_sextant_room": a, "sensor.b_sextant_room": b, "sensor.a_sextant_floor": f})
    assert s["sextant_room"]["far_moves"] == 1
    assert s["sextant_room"]["far_moves_per_thing_day"] == 0.5  # 1 far move over two thing-days
    assert s["sextant_floor"]["far_moves"] is None
    d = kpi.deltas({"entities": {"sensor.a_sextant_room": a}, "summary": s},
                   {"entities": {"sensor.a_sextant_room": {**a, "far_moves_per_day": 3.0}}, "summary": {"sextant_room": {**s["sextant_room"], "far_moves_per_thing_day": 2.5}}})
    assert d["entities"]["sensor.a_sextant_room"]["far_moves_per_day"] == -2.0
    assert d["summary"]["sextant_room"]["far_moves_per_thing_day"] == -2.0
