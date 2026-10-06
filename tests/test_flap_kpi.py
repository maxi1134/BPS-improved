"""Unit tests for the room-stability KPI scorer (tools/flap_kpi.py)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# tools/ is not a package; put it on the path so `import flap_kpi` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import flap_kpi as kpi  # noqa: E402

T0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def _rows(*items):
    """(seconds offset, state) pairs -> HA-style history rows."""
    return [
        {"state": state, "last_changed": (T0 + timedelta(seconds=sec)).isoformat()}
        for sec, state in items
    ]


def test_a_stationary_thing_scores_zero_changes():
    m = kpi.compute_metrics(_rows((0, "Kitchen")), window_hours=1)
    assert m["changes"] == 0
    assert m["changes_per_hour"] == 0
    assert m["flip_ratio"] is None
    assert m["median_dwell_s"] is None


def test_a_restart_is_not_a_change():
    """Kitchen, unavailable, unknown, Kitchen is the sensor going away and
    coming back where it was: no change, the dead rows counted on their own."""
    m = kpi.compute_metrics(
        _rows((0, "Kitchen"), (100, "unavailable"), (101, "unknown"), (102, "Kitchen"), (200, "Office")),
        window_hours=1,
    )
    assert m["changes"] == 1
    assert m["dead"] == 2
    assert m["top_pairs"] == [{"pair": ["Kitchen", "Office"], "count": 1}]


def test_flips_and_dwells_are_counted():
    # Kitchen 30s -> Dining 20s -> Kitchen 600s -> Office: two flips? No - one
    # A-B-A (Kitchen/Dining/Kitchen); Dining/Kitchen/Office is not a round trip.
    m = kpi.compute_metrics(
        _rows((0, "Kitchen"), (30, "Dining"), (50, "Kitchen"), (650, "Office")),
        window_hours=1,
    )
    assert m["changes"] == 3
    assert m["changes_per_hour"] == 3.0
    assert m["flips"] == 1
    assert m["flip_ratio"] == round(1 / 3, 3)
    # dwells: 30, 20, 600 -> median 30, two of three under a minute
    assert m["median_dwell_s"] == 30.0
    assert m["short_dwell_ratio"] == round(2 / 3, 3)
    assert m["top_pairs"][0] == {"pair": ["Dining", "Kitchen"], "count": 2}


def test_repeated_reports_of_the_same_state_are_not_changes():
    m = kpi.compute_metrics(_rows((0, "Kitchen"), (10, "Kitchen"), (20, "Kitchen")), window_hours=1)
    assert m["changes"] == 0


def test_dead_states_count_separately_and_do_not_make_pairs():
    m = kpi.compute_metrics(
        _rows((0, "Kitchen"), (30, "unknown"), (40, "Kitchen"), (100, "unavailable")),
        window_hours=1,
    )
    assert m["dead"] == 2
    assert m["top_pairs"] == []
    assert m["states"] == 1


def test_window_defaults_to_the_span_of_the_rows():
    m = kpi.compute_metrics(_rows((0, "A"), (1800, "B"), (3600, "A")))
    assert m["hours"] == 1.0
    assert m["changes_per_hour"] == 2.0


def test_summary_rolls_up_by_kind():
    per = {
        "sensor.phone_sextant_zone": kpi.compute_metrics(_rows((0, "A"), (60, "B"), (120, "A")), 1),
        "sensor.watch_sextant_zone": kpi.compute_metrics(_rows((0, "A")), 1),
        "sensor.phone_sextant_floor": kpi.compute_metrics(_rows((0, "G"), (600, "S")), 1),
    }
    s = kpi.summarise(per)
    assert s["sextant_room"]["entities"] == 2
    assert s["sextant_room"]["changes"] == 2
    assert s["sextant_room"]["changes_per_thing_hour"] == 1.0  # 2 changes over 2 thing-hours
    assert s["sextant_room"]["flip_ratio"] == 0.5
    assert s["sextant_floor"]["changes"] == 1


def test_report_prints_without_error(capsys):
    per = {"sensor.phone_sextant_zone": kpi.compute_metrics(_rows((0, "A"), (60, "B"), (120, "A")), 1)}
    base = {"entities": {"sensor.phone_sextant_zone": {"changes_per_hour": 5.0, "flip_ratio": 0.9}},
            "summary": {"sextant_zone": {"changes_per_thing_hour": 5.0, "flip_ratio": 0.9}}}
    kpi.print_report(per, kpi.summarise(per), base)
    out = capsys.readouterr().out
    assert "sensor.phone_sextant_zone" in out
    assert "-3.00" in out  # 2.0 - 5.0 change-rate delta
    assert "was 5.0/h" in out
