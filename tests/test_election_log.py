"""The opt-in election log (custom_components/sextant/election_log.py).

The naming, thinning and slimming decisions are pure and tested directly; the
write path runs against a real temporary directory.
"""
import asyncio
import json
import time

import sextant.election_log as el
from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


HOUR = 3600
NOW = 1_790_000_000  # 2026-09-21T14:13:20Z


def _row(ent="thing", **extra):
    return {"ent": ent, "floor": "Second Floor", "floors": {"Second Floor": 0.7}, "floor_cands": {"Second Floor": {"score": 0.5}},
            "radii": [[1, 2, 3], [4, 5, 6]], "fp": {"big": "telemetry"}, "updated": NOW, **extra}


# --- names -------------------------------------------------------------------

def test_a_cycle_goes_in_the_file_of_its_utc_hour():
    name = el.file_for(NOW)
    assert name == time.strftime("%Y-%m-%dT%HZ.jsonl", time.gmtime(NOW))
    assert el.hour_of(name) == (NOW // HOUR) * HOUR


def test_anything_that_is_not_one_of_our_names_is_not_ours():
    for name in ("layout.json", "2026-09-21.jsonl", "2026-09-21T03Z.txt", "../x", "2026-13-40T99Z.jsonl"):
        assert el.hour_of(name) is None


# --- thinning ----------------------------------------------------------------

def test_files_older_than_the_window_expire_and_the_rest_stay():
    names = [el.file_for(NOW - h * HOUR) for h in range(0, 30)] + ["notes.txt"]
    gone = el.expired(names, NOW, hours=24)
    # The hour in progress and the 24 whole hours before it survive.
    assert el.file_for(NOW) not in gone and el.file_for(NOW - 24 * HOUR) not in gone
    assert el.file_for(NOW - 26 * HOUR) in gone and el.file_for(NOW - 29 * HOUR) in gone
    assert "notes.txt" not in gone


# --- slimming ----------------------------------------------------------------

def test_a_row_keeps_the_election_and_drops_the_panel_only_bulk():
    rec = el.slim(_row(), NOW + 0.5)
    assert rec["t"] == NOW + 0.5 and rec["ent"] == "thing" and rec["floor_cands"] == {"Second Floor": {"score": 0.5}}
    assert "radii" not in rec and "fp" not in rec and rec["radii_n"] == 2


# --- writing -----------------------------------------------------------------

def test_off_writes_nothing_and_on_writes_one_line_per_row(tmp_path):
    log = el.ElectionLog(str(tmp_path / "log"))
    log.add(_row("a"), NOW)
    assert run(log.flush(0, now=NOW)) == 0 and not (tmp_path / "log").exists(), "off: discarded, no directory"
    log.add(_row("a"), NOW)
    log.add(_row("b"), NOW)
    log.add({"ent": "c", "floor": "F"}, NOW)                # no floor_cands: not an election
    assert run(log.flush(24, now=NOW)) == 2
    lines = (tmp_path / "log" / el.file_for(NOW)).read_text().splitlines()
    assert [json.loads(l)["ent"] for l in lines] == ["a", "b"]
    assert run(log.flush(24, now=NOW)) == 0, "the buffer was emptied by the flush"


def test_expired_files_are_removed_on_the_first_flush_of_an_hour(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    old, kept, foreign = d / el.file_for(NOW - 30 * HOUR), d / el.file_for(NOW - 2 * HOUR), d / "keep-me.txt"
    for p in (old, kept, foreign):
        p.write_text("x\n")
    log = el.ElectionLog(str(d))
    log.add(_row(), NOW)
    run(log.flush(24, now=NOW))
    assert not old.exists() and kept.exists() and foreign.exists()


def test_one_log_per_hass_under_the_config_directory(tmp_path):
    hass = make_hass(tmp_path)
    log = el.get(hass)
    assert log is el.get(hass)
    assert log.dirpath == str(tmp_path / el.LOG_DIRNAME)


def test_clear_removes_our_files_and_nothing_else(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    ours = [d / el.file_for(NOW - h * HOUR) for h in range(3)]
    for p in ours:
        p.write_text("x" * 10)
    other = d / "notes.txt"
    other.write_text("keep")
    files, size = run(el.clear(str(d)))
    assert files == 3 and size == 30
    assert not any(p.exists() for p in ours) and other.exists()
    assert run(el.clear(str(tmp_path / "missing"))) == (0, 0)
