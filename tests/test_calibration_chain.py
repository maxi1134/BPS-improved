"""Calibration end to end (sextant.calibration): a Bermuda dump is ingested,
scanners are matched to placed receivers, the floor is solved, the corrections
are applied to the layout and reset again.

The synthetic house has five proxies on a 10 m square with one (r2) reading
30 % long; every proxy hears every other's beacon in every dump.
"""
import asyncio
import math
import random
import types

import pytest

import sextant  # noqa: F401
from sextant import calibration as cal_mod
from sextant import storage as st

from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


ADDR = {f"r{i}": f"aa:aa:aa:aa:aa:{i:02x}" for i in range(5)}
POS = {"r0": (1, 1), "r1": (9, 1), "r2": (9, 9), "r3": (1, 9), "r4": (5, 5)}  # metres


def _layout():
    recs = [{"entity_id": s, "address": ADDR[s], "cords": {"x": x * 100, "y": y * 100}} for s, (x, y) in POS.items()]
    return {"floor": [{"name": "F", "scale": 100.0, "receivers": recs, "zones": [], "subzones": []}], "tuning": {}}


def _hass(tmp_path):
    hass = make_hass(tmp_path)
    hass.data["sextant"] = {}
    hass.states = types.SimpleNamespace(async_all=lambda domain=None: [])
    run(st.save_layout(hass, _layout()))
    return hass


def _prepared(hass):
    cal = cal_mod.get_calibration_state(hass)
    coords = st.get_layout(hass)
    cal["receivers"] = cal_mod._build_receiver_map(coords, "F")
    cal["all_placed_slugs"] = cal_mod._all_placed_slugs(coords)
    return cal


def _dump(bias, rng, stamp):
    """A bermuda.dump_devices payload: each scanner advertises its beacon and
    the other scanners' readings of it live on its own device."""
    devices = {}
    for tx, a in ADDR.items():
        adverts = {}
        for rx, b in ADDR.items():
            if rx == tx:
                continue
            true = math.hypot(POS[tx][0] - POS[rx][0], POS[tx][1] - POS[rx][1])
            adverts[b] = {"scanner_address": b, "stamp": stamp, "rssi_distance_raw": true * bias[rx] * rng.uniform(0.95, 1.05)}
        devices[a] = {"_is_scanner": True, "name": f"Proxy {tx}", "address": a, "adverts": adverts}
    return devices


def test_ingest_solve_apply_reset_round_trip(tmp_path):
    hass = _hass(tmp_path)
    cal = _prepared(hass)
    rng = random.Random(3)
    bias = {s: 1.0 for s in ADDR}
    bias["r2"] = 1.3
    for k in range(12):
        cal_mod._ingest_dump(cal, _dump(bias, rng, stamp=1000.0 + k))
    assert len(cal["samples"]) == 20 and all(len(v) == 12 for v in cal["samples"].values())
    assert set(cal["matched_placed"]) == set(ADDR)  # every placement matched by its address

    result = cal_mod.solve(cal_mod.solve_snapshot(cal), "F")
    assert result["floor"] == "F" and result["pairs_used"] == 20
    assert result["receivers"]["r2"] < 0.9
    honest = [result["receivers"][s] for s in ADDR if s != "r2"]
    assert max(honest) - min(honest) < 0.1
    assert result["error_factor_after"] < result["error_factor_before"]

    cal["results"]["F"] = result
    assert run(cal_mod.apply_corrections(hass, cal, "F")) == 5
    stored = {r["entity_id"]: r.get("correction") for r in st.get_layout(hass)["floor"][0]["receivers"]}
    assert stored["r2"] < 0.9 and all(v is not None for v in stored.values())
    assert cal["applied"]["F"] == result["receivers"]

    assert run(cal_mod.reset_corrections(hass, cal, "F")) == 5
    assert all("correction" not in r for r in st.get_layout(hass)["floor"][0]["receivers"])
    assert "F" not in cal["applied"]


def test_solve_refuses_too_little_data_and_apply_refuses_without_a_result(tmp_path):
    hass = _hass(tmp_path)
    cal = _prepared(hass)
    cal_mod._ingest_dump(cal, _dump({s: 1.0 for s in ADDR}, random.Random(1), 1000.0))  # one sample per pair
    with pytest.raises(ValueError):
        cal_mod.solve(cal_mod.solve_snapshot(cal), "F")
    with pytest.raises(ValueError, match="No calibration result"):
        run(cal_mod.apply_corrections(hass, cal, "F"))


def test_stale_adverts_strangers_and_self_readings_are_ignored(tmp_path):
    hass = _hass(tmp_path)
    cal = _prepared(hass)
    dump = _dump({s: 1.0 for s in ADDR}, random.Random(2), 1000.0)
    dump[ADDR["r0"]]["adverts"][ADDR["r1"]]["stamp"] = 900.0  # 100 s older than the newest advert
    dump[ADDR["r0"]]["adverts"][ADDR["r0"]] = {"scanner_address": ADDR["r0"], "stamp": 1000.0, "rssi_distance_raw": 0.4}
    dump["ff:ff:ff:ff:ff:ff"] = {"_is_scanner": True, "name": "Stranger", "address": "ff:ff:ff:ff:ff:ff",
                                 "adverts": {ADDR["r0"]: {"scanner_address": ADDR["r0"], "stamp": 1000.0, "rssi_distance_raw": 3.0}}}
    cal_mod._ingest_dump(cal, dump)
    assert "r0|r1" not in cal["samples"] and "r1|r0" in cal["samples"]
    assert not any("r0|r0" in k or "stranger" in k.lower() for k in cal["samples"])
    assert len(cal["samples"]) == 19


def test_scanner_matching_by_address_then_name_then_mac_tail():
    def rec(address=None):
        return {"address": address, "x": 0.0, "y": 0.0, "scale": 100.0, "floor": "F", "uid": None, "height": None}

    cal = {"receivers": {"office": rec("bb:bb:bb:bb:bb:02"), "kitchen_rrn00_aaaa01": rec(), "hall_rrn00_cccc10": rec()},
           "all_placed_slugs": {"office", "kitchen_rrn00_aaaa01", "hall_rrn00_cccc10"}}
    devices = {
        # Tier 0: the placement carries the scanner's address; the name is irrelevant.
        "bb:bb:bb:bb:bb:02": {"_is_scanner": True, "name": "Whatever", "address": "BB:BB:BB:BB:BB:02"},
        # Tier 1: the device name (slugified) is the placed slug.
        "aa:aa:aa:aa:aa:01": {"_is_scanner": True, "name": "kitchen_rrn00_aaaa01", "address": "aa:aa:aa:aa:aa:01"},
        # Tier 2: renamed, but its Bluetooth MAC sits within +3 of the slug's hex tail.
        "cc:cc:cc:cc:cc:12": {"_is_scanner": True, "name": "Renamed Hall", "address": "cc:cc:cc:cc:cc:12"},
        # Not a scanner at all.
        "dd:dd:dd:dd:dd:dd": {"_is_scanner": False, "name": "hall_rrn00_cccc10", "address": "dd:dd:dd:dd:dd:dd"},
    }
    matched = cal_mod._match_scanners(cal, devices)
    assert matched == {"bb:bb:bb:bb:bb:02": "office", "aa:aa:aa:aa:aa:01": "kitchen_rrn00_aaaa01", "cc:cc:cc:cc:cc:12": "hall_rrn00_cccc10"}
    assert set(cal["matched_placed"]) == {"office", "kitchen_rrn00_aaaa01", "hall_rrn00_cccc10"}


def test_ambiguous_mac_tail_matches_nothing():
    cal = {"receivers": {"hall_rrn00_cccc10": {"address": None, "x": 0.0, "y": 0.0, "scale": 100.0, "floor": "F", "uid": None, "height": None}},
           "all_placed_slugs": {"hall_rrn00_cccc10"}}
    devices = {a: {"_is_scanner": True, "name": "Proxy", "address": a} for a in ("cc:cc:cc:cc:cc:11", "cc:cc:cc:cc:cc:12")}
    assert cal_mod._match_scanners(cal, devices) == {}


def test_true_distance_is_3d_only_when_both_heights_are_known():
    cal = {"receivers": {
        "a": {"x": 0.0, "y": 0.0, "scale": 100.0, "floor": "F", "height": 0.3},
        "b": {"x": 300.0, "y": 0.0, "scale": 100.0, "floor": "F", "height": 2.2},
        "c": {"x": 300.0, "y": 0.0, "scale": 100.0, "floor": "F", "height": None},
        "d": {"x": 300.0, "y": 0.0, "scale": 100.0, "floor": "Other", "height": 2.2},
    }}
    assert cal_mod._true_distance_m(cal, "a", "b") == pytest.approx(math.hypot(3.0, 1.9))
    assert cal_mod._true_distance_m(cal, "a", "c") == pytest.approx(3.0)
    assert cal_mod._true_distance_m(cal, "a", "d") is None


def test_status_payload_reports_the_window_start_for_the_panel(tmp_path):
    hass = _hass(tmp_path)
    cal = cal_mod.get_calibration_state(hass)
    cal.update({"state": "sampling", "mode": "auto", "started_at": 1234.5})
    payload = cal_mod._status_payload(cal)
    assert payload["started_at"] == 1234.5 and payload["first_solve_after"] == cal_mod.AUTO_MIN_WINDOW
    assert payload["mode"] == "auto" and "seconds_left" not in payload  # only a manual run has an end


def test_auto_apply_rewrites_corrections_the_layout_lost(tmp_path):
    """cal["applied"] remembers what auto calibration wrote; if a Save from the
    editor removed them from the layout, the next auto solve must write them
    again instead of concluding that nothing moved."""
    hass = _hass(tmp_path)
    hass.async_create_task = lambda coro: coro.close()
    cal = _prepared(hass)
    rng = random.Random(5)
    bias = {s: 1.0 for s in ADDR}
    bias["r2"] = 1.3
    for k in range(12):
        cal_mod._ingest_dump(cal, _dump(bias, rng, stamp=1000.0 + k))
    run(cal_mod._auto_solve_and_apply_locked(hass, cal))
    first = {r["entity_id"]: r["correction"] for r in st.get_layout(hass)["floor"][0]["receivers"]}
    assert first["r2"] < 0.9 and cal["applied"]["F"] == cal["results"]["F"]["receivers"]
    # An older copy of the layout is saved over it: corrections gone, "applied" still remembers them.
    lost = st.get_layout(hass)
    for r in lost["floor"][0]["receivers"]:
        r.pop("correction", None)
    run(st.save_layout(hass, lost))
    run(cal_mod._auto_solve_and_apply_locked(hass, cal))
    again = {r["entity_id"]: r.get("correction") for r in st.get_layout(hass)["floor"][0]["receivers"]}
    assert again["r2"] is not None and abs(again["r2"] - first["r2"]) < 0.05


def test_a_proxy_can_keep_its_own_correction(tmp_path):
    """The laundry tablet, 2026-09-22: its distances read long up close and far
    too short across the room, so one multiplier cannot describe it and the
    solver pinned it at the 5.0 ceiling. "calibrate": false keeps it out of the
    fit and leaves the correction its owner set."""
    hass = _hass(tmp_path)
    hass.async_create_task = lambda coro: coro.close()
    layout = st.get_layout(hass)
    for r in layout["floor"][0]["receivers"]:
        if r["entity_id"] == "r2":
            r["calibrate"] = False
            r["correction"] = 1.0
    run(st.save_layout(hass, layout))

    cal = _prepared(hass)
    rng = random.Random(5)
    bias = {s: 1.0 for s in ADDR}
    bias["r2"] = 1.3          # the solve would very much like to change r2
    for k in range(12):
        cal_mod._ingest_dump(cal, _dump(bias, rng, stamp=1000.0 + k))
    run(cal_mod._auto_solve_and_apply_locked(hass, cal))

    after = {r["entity_id"]: r.get("correction") for r in st.get_layout(hass)["floor"][0]["receivers"]}
    assert after["r2"] == 1.0                      # left exactly as set
    assert "r2" not in (cal["results"].get("F") or {}).get("receivers", {})   # and not even fitted


def test_auto_decision_rules():
    fit_ok = {"error_factor_before": 1.4, "error_factor_after": 1.3}
    fit_bad = {"error_factor_before": 1.4, "error_factor_after": 1.5}
    assert cal_mod._auto_decision(None, fit_ok)[0] == "apply"
    assert cal_mod._auto_decision(None, fit_bad)[0] == "hold"
    # Nothing in place: apply when the new set beats none by the margin.
    assert cal_mod._auto_decision({"none_m": 2.0, "current_m": None, "new_m": 1.8, "solved": 5, "current_auto": False}, fit_bad)[0] == "apply"
    assert cal_mod._auto_decision({"none_m": 2.0, "current_m": None, "new_m": 1.99, "solved": 5, "current_auto": False}, fit_ok)[0] == "hold"
    # Corrections in place: the new set must beat them, not just none.
    assert cal_mod._auto_decision({"none_m": 2.0, "current_m": 1.5, "new_m": 1.6, "solved": 5, "current_auto": True}, fit_ok)[0] == "hold"
    assert cal_mod._auto_decision({"none_m": 2.0, "current_m": 1.5, "new_m": 1.4, "solved": 5, "current_auto": True}, fit_ok)[0] == "apply"
    # Auto's own corrections are taken out when none beats them; a person's are not touched.
    assert cal_mod._auto_decision({"none_m": 1.5, "current_m": 2.0, "new_m": 2.1, "solved": 5, "current_auto": True}, fit_ok)[0] == "revert"
    assert cal_mod._auto_decision({"none_m": 1.5, "current_m": 2.0, "new_m": 2.1, "solved": 5, "current_auto": False}, fit_ok)[0] == "hold"


def _auto_house(tmp_path, judge):
    hass = _hass(tmp_path)
    hass.async_create_task = lambda coro: coro.close()
    cal = _prepared(hass)
    rng = random.Random(9)
    bias = {s: 1.0 for s in ADDR}
    bias["r2"] = 1.3
    for k in range(12):
        cal_mod._ingest_dump(cal, _dump(bias, rng, stamp=1000.0 + k))

    async def fake_judge(_hass, _cal, _coords, floor, result):
        return judge(floor, result)

    return hass, cal, fake_judge


def test_auto_applies_only_what_the_selftest_confirms(tmp_path, monkeypatch):
    verdict = {"none_m": 2.0, "current_m": None, "new_m": 2.4, "solved": 5, "current_auto": False}
    hass, cal, fake = _auto_house(tmp_path, lambda floor, result: dict(verdict))
    monkeypatch.setattr(cal_mod, "_judge_by_selftest", fake)
    run(cal_mod._auto_solve_and_apply_locked(hass, cal))
    assert all(r.get("correction") is None for r in st.get_layout(hass)["floor"][0]["receivers"])
    assert cal["auto_decisions"]["F"]["action"] == "hold" and "F" not in cal["applied"]
    assert cal["results"]["F"]["selftest"] == verdict
    verdict.update(new_m=1.7)
    run(cal_mod._auto_solve_and_apply_locked(hass, cal))
    assert cal["auto_decisions"]["F"]["action"] == "apply"
    stored = {r["entity_id"]: r.get("correction") for r in st.get_layout(hass)["floor"][0]["receivers"]}
    assert stored["r2"] is not None and stored["r2"] < 0.9 and st.get_layout(hass)["floor"][0]["calibration"]["auto"] is True


def test_auto_takes_its_own_corrections_out_when_none_beats_them(tmp_path, monkeypatch):
    state = {"v": {"none_m": 2.0, "current_m": None, "new_m": 1.5, "solved": 5, "current_auto": False}}
    hass, cal, fake = _auto_house(tmp_path, lambda floor, result: dict(state["v"]))
    monkeypatch.setattr(cal_mod, "_judge_by_selftest", fake)
    run(cal_mod._auto_solve_and_apply_locked(hass, cal))
    assert cal["auto_decisions"]["F"]["action"] == "apply"
    # Next window: the self-test now says no corrections would be better than what auto wrote.
    state["v"] = {"none_m": 1.2, "current_m": 1.5, "new_m": 1.6, "solved": 5, "current_auto": True}
    cal["applied"]["F"] = {k: v * 1.5 for k, v in cal["applied"]["F"].items()}  # make the new solve differ by >1 %
    run(cal_mod._auto_solve_and_apply_locked(hass, cal))
    assert cal["auto_decisions"]["F"]["action"] == "revert" and "F" not in cal["applied"]
    floor = st.get_layout(hass)["floor"][0]
    assert all(r.get("correction") is None for r in floor["receivers"]) and floor["calibration"]["reverted"] is True


# --- sampling from the fork's scanner-ranging table ---------------------------


def _ranging(bias, rng, age=1.0):
    """The fork's scanner-ranging table for the same synthetic house as _dump."""
    scanners = {}
    for tx, a in ADDR.items():
        heard_by = {}
        for rx, b in ADDR.items():
            if rx == tx:
                continue
            true = math.hypot(POS[tx][0] - POS[rx][0], POS[tx][1] - POS[rx][1])
            heard_by[b] = {"distance": None, "distance_raw": true * bias[rx] * rng.uniform(0.95, 1.05), "rssi": -70, "age": age}
        scanners[a] = heard_by
    return {"version": 1, "stamp": 1000.0, "scanners": scanners}


def _directory():
    return {a: {"slug": f"proxy_{s}", "name": f"Proxy {s}", "unique_id": None, "address_wifi_mac": None, "last_seen_age": 1.0} for s, a in ADDR.items()}


def test_ranging_ingest_yields_the_same_samples_as_a_dump(tmp_path):
    hass = _hass(tmp_path)
    cal = _prepared(hass)
    bias = {s: 1.0 for s in ADDR}
    cal_mod._ingest_dump(cal, _dump(bias, random.Random(7), 1000.0))
    from_dump = {k: list(v) for k, v in cal["samples"].items()}

    cal["samples"] = {}
    cal_mod._ingest_ranging(cal, _ranging(bias, random.Random(7)), _directory())
    from_ranging = {k: list(v) for k, v in cal["samples"].items()}

    assert from_ranging == from_dump  # same pairs, same values, same rng draw order
    assert len(from_ranging) == 20  # five proxies hearing each other both ways
    assert all(isinstance(v, cal_mod.SampleWindow) for v in cal["samples"].values())


def test_ranging_ingest_drops_stale_unknown_and_bad_readings(tmp_path):
    hass = _hass(tmp_path)
    cal = _prepared(hass)
    bias = {s: 1.0 for s in ADDR}
    table = _ranging(bias, random.Random(1))
    a0, a1, a2 = ADDR["r0"], ADDR["r1"], ADDR["r2"]
    table["scanners"][a0][a1]["age"] = cal_mod.STALE_ADVERT_SECS + 1  # too old
    table["scanners"][a0][a2]["distance_raw"] = 0  # no range
    table["scanners"][a1]["ff:ff:ff:ff:ff:ff"] = {"distance_raw": 3.0, "age": 1.0}  # not a placed scanner
    table["scanners"]["ee:ee:ee:ee:ee:ee"] = {a0: {"distance_raw": 3.0, "age": 1.0}}
    table["scanners"][a1][a1] = {"distance_raw": 3.0, "age": 1.0}  # hears itself

    cal_mod._ingest_ranging(cal, table, _directory())

    assert "r0|r1" not in cal["samples"] and "r0|r2" not in cal["samples"]
    assert "r1|r1" not in cal["samples"]
    assert not any("ff:ff" in k or "ee:ee" in k for k in cal["samples"])
    assert len(cal["samples"]) == 18
    # Garbage shapes are ignored rather than raised.
    cal_mod._ingest_ranging(cal, None, _directory())
    cal_mod._ingest_ranging(cal, {"scanners": "nope"}, _directory())
    cal_mod._ingest_ranging(cal, {"scanners": {a0: "nope"}}, None)
    assert len(cal["samples"]) == 18


def test_collect_samples_prefers_ranging_and_falls_back_to_dump(tmp_path, monkeypatch):
    hass = _hass(tmp_path)
    cal = _prepared(hass)
    bias = {s: 1.0 for s in ADDR}
    dumps = []

    async def fake_dump(_hass):
        dumps.append(1)
        return _dump(bias, random.Random(2), 1000.0)

    monkeypatch.setattr(cal_mod, "_dump_devices", fake_dump)
    monkeypatch.setattr(cal_mod.bermuda_source, "async_get_scanner_ranging", lambda _h, max_age=None: _ranging(bias, random.Random(2)))
    monkeypatch.setattr(cal_mod.bermuda_source, "async_get_scanner_directory", lambda _h: _directory())

    assert run(cal_mod._collect_samples(hass, cal)) == "ranging"
    assert dumps == [] and cal["sample_source"] == "ranging"
    assert len(cal["samples"]) == 20
    assert cal_mod._status_payload(cal)["sample_source"] == "ranging"

    # Stock Bermuda: no ranging table, so the dump service is used.
    monkeypatch.setattr(cal_mod.bermuda_source, "async_get_scanner_ranging", lambda _h, max_age=None: None)
    assert run(cal_mod._collect_samples(hass, cal)) == "dump"
    assert dumps == [1] and cal["sample_source"] == "dump"

    # A table without a directory to name the scanners also falls back.
    monkeypatch.setattr(cal_mod.bermuda_source, "async_get_scanner_ranging", lambda _h, max_age=None: _ranging(bias, random.Random(2)))
    monkeypatch.setattr(cal_mod.bermuda_source, "async_get_scanner_directory", lambda _h: None)
    assert run(cal_mod._collect_samples(hass, cal)) == "dump"
    assert dumps == [1, 1]


def test_sample_window_is_bounded_packed_and_listable():
    w = cal_mod.SampleWindow(maxlen=3)
    assert len(w) == 0 and list(w) == []
    for v in (1.0, 2.5, 3.0, 4.0):
        w.append(v)
    assert list(w) == [2.5, 3.0, 4.0]  # oldest dropped at the cap
    assert len(w) == 3 and w[-1] == 4.0 and list(w[-2:]) == [3.0, 4.0]
    assert w.maxlen == 3 and "SampleWindow" in repr(w)
    assert cal_mod.SampleWindow([0.1, 0.2]).__len__() == 2 and list(cal_mod.SampleWindow([0.1, 0.2])) == [0.1, 0.2]
    over = cal_mod.SampleWindow(range(1000))
    assert len(over) == cal_mod.SAMPLES_MAXLEN and over[0] == 1000 - cal_mod.SAMPLES_MAXLEN
    assert w._buf.itemsize == 8  # packed doubles, not float objects


def test_state_round_trip_keeps_the_persisted_tail_as_windows(tmp_path):
    hass = _hass(tmp_path)
    cal = _prepared(hass)
    cal["samples"]["r0|r1"] = cal_mod.SampleWindow(float(i) for i in range(cal_mod.STATE_SAMPLES_PER_PAIR + 50))
    run(cal_mod.save_calibration_state(hass))
    saved = run(st.load_calib_state(hass))
    assert len(saved["samples"]["r0|r1"]) == cal_mod.STATE_SAMPLES_PER_PAIR == 100
    assert saved["samples"]["r0|r1"][-1] == float(cal_mod.STATE_SAMPLES_PER_PAIR + 49)

    cal["samples"] = {}
    cal["mode"] = "off"
    run(cal_mod.async_restore_calibration_state(hass))
    restored = cal["samples"]["r0|r1"]
    assert isinstance(restored, cal_mod.SampleWindow)
    assert len(restored) == 100 and restored[0] == 50.0
