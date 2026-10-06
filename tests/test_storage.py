"""Storage layer: crash-safe atomic writes + one-time legacy migration.

Runs against the fake in-memory Store in conftest. The real atomicity lives in
HA's Store; these lock down our migration/round-trip logic and, crucially,
that the truncate-then-write path that caused issue #104 is gone (we only ever
hand the store a complete dict, via async_save, never async_delay_save).
"""
import asyncio
from pathlib import Path

import sextant.storage as st
from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _legacy_layout(hass):
    d = Path(hass.config.path("www", "sextant_maps"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "bpsdata.txt"


def _legacy_calib(hass):
    d = Path(hass.config.path("www", "sextant_maps"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "sextant_calibration_state.json"


# --- fresh install ----------------------------------------------------------
def test_fresh_install_is_empty(tmp_path):
    hass = make_hass(tmp_path)
    run(st.migrate_legacy(hass))   # nothing to migrate
    run(st.load_layout(hass))
    assert st.get_layout(hass) == []           # matches old empty-file behaviour
    assert st.get_layout_for_edit(hass) is None


# --- migration --------------------------------------------------------------
def test_migrate_valid_layout(tmp_path):
    hass = make_hass(tmp_path)
    legacy = _legacy_layout(hass)
    legacy.write_text('{"floor":[{"name":"Home"}]}')
    run(st.migrate_legacy(hass))
    run(st.load_layout(hass))
    assert st.get_layout(hass) == {"floor": [{"name": "Home"}]}
    assert not legacy.exists()                    # old www copy removed (closes /local exposure)


def test_migrate_zero_byte_is_the_issue_104_guard(tmp_path):
    hass = make_hass(tmp_path)
    legacy = _legacy_layout(hass)
    legacy.write_text("")                         # the 0-byte artifact
    run(st.migrate_legacy(hass))                  # must not raise
    run(st.load_layout(hass))
    assert st.get_layout(hass) == []
    assert not legacy.exists()                    # unrecoverable blank file cleaned up


def test_migrate_corrupt_layout_is_preserved(tmp_path):
    hass = make_hass(tmp_path)
    legacy = _legacy_layout(hass)
    legacy.write_text('{"floor": [ this is not json')
    run(st.migrate_legacy(hass))
    run(st.load_layout(hass))
    assert st.get_layout(hass) == []            # start empty...
    assert legacy.exists()                        # ...but keep the file for manual recovery


def test_migrate_does_not_overwrite_populated_store(tmp_path):
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, {"floor": [{"name": "Already"}]}))   # store already owns data
    legacy = _legacy_layout(hass)
    legacy.write_text('{"floor":[{"name":"Stale"}]}')
    run(st.migrate_legacy(hass))                  # store non-empty: don't re-import
    run(st.load_layout(hass))
    assert st.get_layout(hass) == {"floor": [{"name": "Already"}]}  # store data wins
    assert not legacy.exists()                    # stale exposed copy cleaned up


# --- save / load round-trip -------------------------------------------------
def test_save_survives_restart(tmp_path):
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, {"floor": [{"name": "Home"}]}))
    # Simulate a restart: fresh in-memory cache, same .storage backing.
    hass.data.get(st.DOMAIN, {}).pop("layout", None)
    run(st.load_layout(hass))
    assert st.get_layout(hass) == {"floor": [{"name": "Home"}]}


def test_save_is_immediate_and_never_truncates(tmp_path):
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, {"floor": []}))
    # async_save (atomic), never the debounced async_delay_save.
    assert hass._store_delay_saves == []
    assert [k for k, _ in hass._store_saves] == ["sextant"]
    # The store is only ever handed a complete object — never "" — so the old
    # truncate-then-write 0-byte path (issue #104) cannot recur.
    assert all(isinstance(v, (dict, list)) for _, v in hass._store_saves)


def test_get_for_edit_returns_a_copy(tmp_path):
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, {"floor": [{"name": "Home"}]}))
    editable = st.get_layout_for_edit(hass)
    editable["floor"].append({"name": "Injected"})
    # Mutating the editable copy must not touch the live cache the loop reads.
    assert st.get_layout(hass) == {"floor": [{"name": "Home"}]}


# --- calibration state ------------------------------------------------------
def test_calib_state_roundtrip(tmp_path):
    hass = make_hass(tmp_path)
    run(st.save_calib_state(hass, {"results": {"F": 1}, "saved_at": 123}))
    assert run(st.load_calib_state(hass)) == {"results": {"F": 1}, "saved_at": 123}


def test_calib_state_migrates_and_removes_legacy(tmp_path):
    hass = make_hass(tmp_path)
    legacy = _legacy_calib(hass)
    legacy.write_text('{"results": {"F": 2}}')
    run(st.migrate_legacy(hass))
    assert run(st.load_calib_state(hass)) == {"results": {"F": 2}}
    assert not legacy.exists()


# --- resilience -------------------------------------------------------------
def test_corrupt_store_starts_empty_not_crash(tmp_path):
    # A corrupt/unreadable .storage file must degrade (start empty), not abort
    # setup the way an unguarded Store.async_load() would.
    hass = make_hass(tmp_path)
    hass._store_raise_on_load = {"sextant", "sextant_calibration_state"}
    _legacy_layout(hass).write_text('{"floor":[{"name":"Home"}]}')
    run(st.migrate_legacy(hass))                 # must not raise
    run(st.load_layout(hass))                  # must not raise
    assert st.get_layout(hass) == []
    assert run(st.load_calib_state(hass)) is None
    # Corrupt store: don't touch the possibly-recoverable legacy file.
    assert _legacy_layout(hass).exists()


def test_legacy_cleanup_retries_when_store_already_populated(tmp_path):
    # A leftover www copy (failed prior delete, or restored from backup) must
    # be cleaned up on a later boot even though the store already has the data.
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, {"floor": [{"name": "Home"}]}))
    stale = _legacy_layout(hass)
    stale.write_text('{"floor":[{"name":"Home"}]}')   # reappeared /local-exposed copy
    run(st.migrate_legacy(hass))
    assert not stale.exists()                          # removed despite store non-empty
    assert st.get_layout(hass) == {"floor": [{"name": "Home"}]}  # data untouched


# --- migration from the pre-rename "bps" integration ------------------------
def _bps_dirs(hass):
    maps = Path(hass.config.path("www", "bps_maps"))
    hist = Path(hass.config.path(".storage", "bps_history"))
    maps.mkdir(parents=True, exist_ok=True)
    hist.mkdir(parents=True, exist_ok=True)
    (maps / "ground.png").write_bytes(b"png")
    (hist / "primrose.jsonl").write_text("{}\n")
    return maps, hist


def test_migrate_from_bps_copies_everything(tmp_path):
    hass = make_hass(tmp_path)
    layout = {"floor": [{"name": "Home"}], "tuning": {"calibration_target": "bps"}}
    hass._store_backing["bps"] = layout
    hass._store_backing["bps_calibration_state"] = {"auto_enabled": True}
    maps, hist = _bps_dirs(hass)

    run(st.migrate_from_bps(hass))
    run(st.load_layout(hass))

    assert st.get_layout(hass) == layout
    assert hass._store_backing["sextant_calibration_state"] == {"auto_enabled": True}
    assert (Path(hass.config.path("sextant_maps")) / "ground.png").read_bytes() == b"png"
    assert (Path(hass.config.path(".storage", "sextant_history")) / "primrose.jsonl").exists()
    # Copied, never moved: a rollback to bps must still find its data.
    assert hass._store_backing["bps"] == layout
    assert (maps / "ground.png").exists() and (hist / "primrose.jsonl").exists()


def test_migrate_from_bps_never_overwrites_sextant_data(tmp_path):
    hass = make_hass(tmp_path)
    hass._store_backing["sextant"] = {"floor": [{"name": "New"}]}
    hass._store_backing["bps"] = {"floor": [{"name": "Old"}]}
    _bps_dirs(hass)
    new_maps = Path(hass.config.path("sextant_maps"))
    new_maps.mkdir(parents=True)
    (new_maps / "keep.png").write_bytes(b"keep")

    run(st.migrate_from_bps(hass))

    assert hass._store_backing["sextant"] == {"floor": [{"name": "New"}]}
    assert sorted(p.name for p in new_maps.iterdir()) == ["keep.png"]
    # The history dir had no sextant copy yet, so that one is still filled in.
    assert (Path(hass.config.path(".storage", "sextant_history")) / "primrose.jsonl").exists()


def test_migrate_from_bps_is_a_noop_on_a_fresh_install(tmp_path):
    hass = make_hass(tmp_path)
    run(st.migrate_from_bps(hass))
    run(st.load_layout(hass))
    assert st.get_layout(hass) == []
    assert "sextant_calibration_state" not in hass._store_backing
    assert not Path(hass.config.path("sextant_maps")).exists()


def test_migrate_from_bps_survives_a_corrupt_old_store(tmp_path):
    hass = make_hass(tmp_path)
    hass._store_raise_on_load.add("bps")
    hass._store_backing["bps_calibration_state"] = {"auto_enabled": False}
    run(st.migrate_from_bps(hass))          # must not raise
    assert "sextant" not in hass._store_backing
    assert hass._store_backing["sextant_calibration_state"] == {"auto_enabled": False}


def test_migrate_from_bps_fills_a_target_dir_setup_already_created(tmp_path):
    hass = make_hass(tmp_path)
    _bps_dirs(hass)
    Path(hass.config.path("sextant_maps")).mkdir(parents=True)     # empty, as setup leaves it
    Path(hass.config.path(".storage", "sextant_history")).mkdir(parents=True)
    run(st.migrate_from_bps(hass))
    assert (Path(hass.config.path("sextant_maps")) / "ground.png").exists()
    assert (Path(hass.config.path(".storage", "sextant_history")) / "primrose.jsonl").exists()


def test_maps_move_out_of_www_and_stay_out(tmp_path):
    """3.11.11: floor plans left www/ (public at /local/) for config/sextant_maps."""
    hass = make_hass(tmp_path)
    old = Path(hass.config.path("www", "sextant_maps"))
    old.mkdir(parents=True)
    (old / "Ground.png").write_bytes(b"old")
    (old / "Loft.jpg").write_bytes(b"loft")
    (old / "notes.txt").write_text("keep me")  # not an image: untouched
    new = Path(hass.config.path("sextant_maps"))
    new.mkdir()
    (new / "Ground.png").write_bytes(b"new")  # already migrated copy wins

    assert run(st.migrate_maps_out_of_www(hass)) == 1
    assert (new / "Ground.png").read_bytes() == b"new"
    assert (new / "Loft.jpg").read_bytes() == b"loft"
    assert not (old / "Ground.png").exists() and not (old / "Loft.jpg").exists()
    assert (old / "notes.txt").exists()  # so the old folder stays for it
    assert run(st.migrate_maps_out_of_www(hass)) == 0  # idempotent

    (old / "notes.txt").unlink()
    assert run(st.migrate_maps_out_of_www(hass)) == 0
    assert not old.exists()  # empty old folder removed
    assert run(st.migrate_maps_out_of_www(hass)) == 0  # and nothing to do without it


def test_tracker_keys_are_renamed_to_thing_on_load(tmp_path):
    """An install from before 3.12.0 keeps its names, colours and heights."""
    hass = make_hass(tmp_path)
    hass._store_backing["sextant"] = {
        "floor": [{"name": "F"}],
        "tracker_names": {"cat": "Meg"},
        "tracker_colors": {"cat": "#ff0000"},
        "tracker_heights": {"cat": 0.2},
        "tracker_classes": {"cat": "cat"},
        "tracker_icons": {"cat": "/local/x.png"},
        "tracker_estimators": {"cat": "fused"},
        "tracker_fp_weights": {"cat": 0.5},
        "tracker_ref_offsets": {"cat": 1.0},
        "tuning": {"zone_switch_secs": 20},
    }
    layout = run(st.load_layout(hass))

    assert layout["thing_names"] == {"cat": "Meg"} and "tracker_names" not in layout
    for key in ("thing_colors", "thing_heights", "thing_classes", "thing_icons",
                "thing_estimators", "thing_fp_weights", "thing_ref_offsets"):
        assert key in layout, key
    assert not [k for k in layout if k.startswith("tracker_")]
    assert layout["tuning"] == {"zone_switch_secs": 20} and layout["floor"][0]["name"] == "F"
    # Written back, so the next start has nothing to do.
    assert "tracker_names" not in hass._store_backing["sextant"]
    assert run(st.load_layout(hass))["thing_names"] == {"cat": "Meg"}


def test_thing_keys_are_renamed_at_every_depth_in_the_other_stores(tmp_path):
    """The gains, truth and KPI stores carry the old word nested, not on top."""
    hass = make_hass(tmp_path)
    hass._store_backing["sextant_fingerprint_gains"] = {"learned_gain": 1.1, "tracker_gain": {"cat": 0.9}}
    hass._store_backing["sextant_truth"] = {"next_id": 2, "marks": [{"id": 1, "samples": [{"tracker_vec": [1, 2]}]}]}
    hass._store_backing["sextant_kpi_baselines"] = {
        "b1": {"summary": {"sextant_room": {"changes_per_tracker_hour": 4.0, "flip_ratio": 0.1}}},
    }

    gains = run(st.load_fp_gains(hass))
    assert gains["thing_gain"] == {"cat": 0.9} and "tracker_gain" not in gains and gains["learned_gain"] == 1.1

    truth = run(st.load_truth(hass))
    assert truth["marks"][0]["samples"][0]["thing_vec"] == [1, 2]
    assert "tracker_vec" not in truth["marks"][0]["samples"][0]

    kpi = run(st.load_kpi_baselines(hass))
    summary = kpi["b1"]["summary"]["sextant_room"]
    assert summary["changes_per_thing_hour"] == 4.0 and "changes_per_tracker_hour" not in summary
    assert summary["flip_ratio"] == 0.1


def test_rename_leaves_a_new_style_layout_alone(tmp_path):
    hass = make_hass(tmp_path)
    hass._store_backing["sextant"] = {"floor": [], "thing_names": {"cat": "Meg"}}
    assert run(st.load_layout(hass))["thing_names"] == {"cat": "Meg"}
    assert st.rename_thing_keys({"thing_names": {"a": 1}})[1] == 0
    # An odd shape must not raise.
    assert st.rename_thing_keys(None)[1] == 0 and st.rename_thing_keys([1, "x"])[1] == 0
