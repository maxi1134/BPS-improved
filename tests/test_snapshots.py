"""Layout snapshots: what is kept, what is thinned, and what a bad save reports.

The retention and damage-detection decisions are pure and are tested directly.
The write/prune/restore path runs against the fake hass from conftest, using a
real temporary directory (snapshots are plain files, not a Store).
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

import sextant.snapshots as sn
import sextant.storage as st
from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


HOUR, DAY = 3600, 86400


def _floor(spots=(), rooms=()):
    return {
        "name": "Second Floor",
        "zones": [{"zone_id": f"z{i}", "entity_id": n, "cords": [{"x": 0, "y": 0}] * c}
                  for i, (n, c) in enumerate(rooms)],
        "subzones": [{"sub_zone_id": f"s{i}", "entity_id": n, "cords": [{"x": 0, "y": 0}] * c}
                     for i, (n, c) in enumerate(spots)],
    }


def _layout(spots=(), rooms=()):
    return {"floor": [_floor(spots, rooms)]}


# --- names -------------------------------------------------------------------

def test_a_snapshot_name_round_trips():
    assert sn.parse_name(sn.snapshot_name(1790000000, 42)) == (1790000000, 42)


@pytest.mark.parametrize("name", [
    "layout.json", "1790000000.json", "v3.json", "", "1790000000-v3.txt",
    "../sextant", "../../.storage/sextant", "sub/1790000000-v3.json",
])
def test_anything_that_is_not_one_of_our_names_is_rejected(name):
    assert sn.parse_name(name) is None


# --- thinning ----------------------------------------------------------------

def test_nothing_in_nothing_out():
    assert sn.keepers([], time.time()) == set()


def test_everything_from_the_last_day_is_kept_exactly_as_saved():
    now = 1_790_000_000
    stamps = [now - m * 60 for m in range(0, 60 * 23, 7)]   # 23 hours of dense edits
    assert sn.keepers(stamps, now) == set(stamps)


def test_the_previous_week_is_thinned_to_one_an_hour():
    now = 1_790_000_000
    # Four saves inside one hour, three days back.
    base = now - 3 * DAY
    stamps = [base, base + 60, base + 120, base + 180]
    kept = sn.keepers(stamps, now)
    assert len(kept) == 1
    assert kept == {base + 180}, "the newest of the hour is the one worth keeping"


def test_beyond_a_week_is_thinned_to_one_a_day():
    now = 1_790_000_000
    # Day buckets are epoch-aligned, so pin the saves inside one of them.
    base = ((now - 30 * DAY) // DAY) * DAY
    stamps = [base + h * HOUR for h in (1, 4, 7, 10)]
    kept = sn.keepers(stamps, now)
    assert len(kept) == 1
    assert max(stamps) in kept


def test_older_than_the_daily_window_is_dropped():
    now = 1_790_000_000
    old = now - (sn.KEEP_DAILY_DAYS + 5) * DAY
    recent = now - HOUR
    assert sn.keepers([old, recent], now) == {recent}


def test_the_newest_is_always_kept_however_old_it_is():
    now = 1_790_000_000
    ancient = now - 5 * 365 * DAY
    assert sn.keepers([ancient], now) == {ancient}


def test_a_year_of_daily_edits_stays_a_few_hundred_files():
    now = 1_790_000_000
    stamps = [now - d * DAY - h * HOUR for d in range(365) for h in range(4)]
    assert len(sn.keepers(stamps, now)) < 300


# --- what a save destroys ----------------------------------------------------

def test_an_untouched_layout_reports_nothing():
    lay = _layout(spots=[("David Bedside Table", 4)])
    assert sn.degraded(lay, json.loads(json.dumps(lay))) == []


def test_a_spot_collapsing_to_one_point_is_reported():
    """The 2026-09-22 bug: two bedside tables lost their corners and vanished."""
    before = _layout(spots=[("David Bedside Table", 4), ("Michelle Bedside Table", 4)])
    after = _layout(spots=[("David Bedside Table", 1), ("Michelle Bedside Table", 1)])
    lost = sn.degraded(before, after)
    assert len(lost) == 2
    assert any("David Bedside Table" in line and "4 corners to 1" in line for line in lost)


def test_a_room_collapsing_is_reported_too():
    before = _layout(rooms=[("Master Bedroom", 8)])
    after = _layout(rooms=[("Master Bedroom", 2)])
    assert len(sn.degraded(before, after)) == 1


def test_reshaping_a_polygon_is_not_a_loss():
    before = _layout(spots=[("Couch", 8)])
    after = _layout(spots=[("Couch", 4)])
    assert sn.degraded(before, after) == []


def test_deleting_a_spot_outright_is_not_reported():
    """People delete spots on purpose; warning about it would be noise."""
    before = _layout(spots=[("Couch", 8), ("Pantry", 4)])
    after = _layout(spots=[("Couch", 8)])
    assert sn.degraded(before, after) == []


def test_adding_a_spot_is_not_a_loss():
    before = _layout(spots=[("Couch", 8)])
    after = _layout(spots=[("Couch", 8), ("New Spot", 4)])
    assert sn.degraded(before, after) == []


def test_junk_in_does_not_raise():
    assert sn.degraded(None, None) == []
    assert sn.degraded([], {"floor": None}) == []
    assert sn.degraded({"floor": [None, "x"]}, {"floor": []}) == []


# --- writing, thinning and reading back --------------------------------------

def _hass(tmp_path):
    """A hass whose config dir is this test's own, so snapshots land in tmp."""
    return make_hass(str(tmp_path))


def test_a_save_leaves_the_outgoing_layout_behind(tmp_path):
    hass = _hass(tmp_path)
    good = _layout(spots=[("David Bedside Table", 4)])
    broken = _layout(spots=[("David Bedside Table", 1)])
    run(st.save_layout(hass, good))
    lost = run(st.save_layout(hass, broken))

    assert lost and "David Bedside Table" in lost[0]
    rows = run(sn.listing(hass))
    assert rows, "the good layout should be recoverable"
    back = run(sn.read(hass, rows[0]["id"]))
    assert len(back["floor"][0]["subzones"][0]["cords"]) == 4


def test_the_bad_save_still_goes_through(tmp_path):
    """A warning, not a veto: refusing the save would be guessing."""
    hass = _hass(tmp_path)
    run(st.save_layout(hass, _layout(spots=[("Couch", 8)])))
    run(st.save_layout(hass, _layout(spots=[("Couch", 1)])))
    assert len(st.get_layout(hass)["floor"][0]["subzones"][0]["cords"]) == 1


def test_an_identical_save_is_not_snapshotted_twice(tmp_path):
    hass = _hass(tmp_path)
    lay = _layout(spots=[("Couch", 8)])
    run(st.save_layout(hass, lay))
    run(st.save_layout(hass, json.loads(json.dumps(lay))))
    run(st.save_layout(hass, json.loads(json.dumps(lay))))
    assert len(run(sn.listing(hass))) == 1


def test_the_very_first_save_has_nothing_to_snapshot(tmp_path):
    hass = _hass(tmp_path)
    run(st.save_layout(hass, _layout(spots=[("Couch", 8)])))
    assert run(sn.listing(hass)) == []


def test_restoring_is_itself_snapshotted_so_it_can_be_undone(tmp_path):
    hass = _hass(tmp_path)
    run(st.save_layout(hass, _layout(spots=[("Couch", 8)])))
    run(st.save_layout(hass, _layout(spots=[("Couch", 1)])))
    rows = run(sn.listing(hass))
    run(st.save_layout(hass, run(sn.read(hass, rows[0]["id"]))))
    assert len(st.get_layout(hass)["floor"][0]["subzones"][0]["cords"]) == 8
    assert len(run(sn.listing(hass))) == 2, "the broken one is kept too"


def test_two_saves_in_the_same_second_do_not_collide(tmp_path):
    hass = _hass(tmp_path)
    for n in (8, 7, 6, 5):
        run(st.save_layout(hass, _layout(spots=[("Couch", n)])))
    rows = run(sn.listing(hass))
    assert len({r["id"] for r in rows}) == len(rows) == 3


def test_reading_a_name_that_is_not_ours_is_refused(tmp_path):
    hass = _hass(tmp_path)
    Path(sn.snapshot_dir(hass)).mkdir(parents=True, exist_ok=True)
    for bad in ("../../.storage/sextant", "sextant", "1790000000-v1.json.bak"):
        with pytest.raises(ValueError):
            run(sn.read(hass, bad))


def test_a_snapshot_failure_never_fails_the_save(tmp_path, monkeypatch):
    """Insurance that can fail the thing it protects is worse than none."""
    hass = _hass(tmp_path)
    run(st.save_layout(hass, _layout(spots=[("Couch", 8)])))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sn, "_scan", boom)
    run(st.save_layout(hass, _layout(spots=[("Couch", 4)])))
    assert len(st.get_layout(hass)["floor"][0]["subzones"][0]["cords"]) == 4


def test_old_snapshots_are_thinned_as_they_are_written(tmp_path):
    hass = _hass(tmp_path)
    path = Path(sn.snapshot_dir(hass))
    path.mkdir(parents=True, exist_ok=True)
    now = time.time()
    # Four saves in one hour, a fortnight ago: only one should survive.
    stale = int(now - 14 * DAY)
    for i in range(4):
        (path / sn.snapshot_name(stale + i * 60, i)).write_text("{}")
    run(st.save_layout(hass, _layout(spots=[("Couch", 8)])))
    run(st.save_layout(hass, _layout(spots=[("Couch", 4)])))
    kept = [r for r in run(sn.listing(hass)) if r["at"] < now - DAY]
    assert len(kept) == 1
