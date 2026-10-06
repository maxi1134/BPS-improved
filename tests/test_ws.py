"""Websocket commands behind the rebuilt panel (sextant/ws.py)."""
import asyncio
import sys
import types

import sextant
from sextant import ws
from sextant import storage as st

from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Conn:
    def __init__(self):
        self.results = []
        self.errors = []

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))


def _hass_with_layout(tmp_path, layout=None):
    hass = make_hass(tmp_path)
    hass.states = types.SimpleNamespace(async_all=lambda domain=None: [])
    hass.data["sextant"] = {}
    if layout is not None:
        run(st.save_layout(hass, layout))
    else:
        run(st.load_layout(hass))
    return hass


def _layout():
    return {"floor": [{"name": "F", "scale": 100.0, "receivers": [{"entity_id": "r0", "cords": {"x": 0, "y": 0}}],
                       "zones": [], "subzones": []}], "tuning": {"zone_switch_secs": 30}}


def test_layout_get_reports_layout_maps_and_tuning_spec(tmp_path):
    hass = _hass_with_layout(tmp_path, _layout())
    maps = tmp_path / "sextant_maps"
    maps.mkdir(parents=True)
    (maps / "F.png").write_bytes(b"png")
    (maps / "notes.txt").write_text("x")
    conn = _Conn()
    run(ws.ws_layout_get(hass, conn, {"id": 1, "type": "sextant/layout/get"}))
    _id, result = conn.results[0]
    assert result["layout"]["floor"][0]["name"] == "F"
    assert result["maps"] == ["F.png"]
    assert result["tuning_spec"]["zone_switch_secs"]["type"] == "float"
    assert result["tuning_spec"]["position_estimator"]["choices"] == ["geometric", "fingerprint", "fused"]
    assert result["entities"] == [] and result["features"] == []
    # Installed (on disk) and running (loaded at start-up) are both reported,
    # so the panel can tell "restart Home Assistant" from "reload the page".
    assert result["running_version"] == ws.RUNNING_VERSION


def test_layout_save_validates_then_persists(tmp_path):
    hass = _hass_with_layout(tmp_path)
    conn = _Conn()
    run(ws.ws_layout_save(hass, conn, {"id": 2, "type": "sextant/layout/save", "layout": {"floor": "nope"}}))
    assert conn.errors and "floor" in conn.errors[0][2]
    run(ws.ws_layout_save(hass, conn, {"id": 3, "type": "sextant/layout/save", "layout": _layout()}))
    assert conn.results[-1][1]["version"] >= 1
    assert st.get_layout(hass)["floor"][0]["name"] == "F"


def test_layout_save_clips_each_spot_to_its_one_room(tmp_path):
    hass = _hass_with_layout(tmp_path)
    layout = _layout()
    square = lambda x0, x1: [{"x": x0, "y": 0}, {"x": x1, "y": 0}, {"x": x1, "y": 100}, {"x": x0, "y": 100}]  # noqa: E731
    layout["floor"][0]["zones"] = [{"zone_id": "gr", "entity_id": "Great Room", "poly": True, "cords": square(0, 400)}]
    layout["floor"][0]["subzones"] = [{"sub_zone_id": "c", "entity_id": "Couch", "parent": "gr", "poly": True,
                                       "cords": square(300, 500)}]
    conn = _Conn()
    run(ws.ws_layout_save(hass, conn, {"id": 3, "type": "sextant/layout/save", "layout": layout}))
    assert conn.results[-1][1]["confined"] == ["Couch"]
    xs = {c["x"] for c in st.get_layout(hass)["floor"][0]["subzones"][0]["cords"]}
    assert xs == {300, 400}


def test_tuning_set_and_thing_tune_write_the_layout(tmp_path):
    hass = _hass_with_layout(tmp_path, _layout())
    conn = _Conn()
    run(ws.ws_tuning_set(hass, conn, {"id": 4, "type": "sextant/tuning/set", "settings": {"position_estimator": "fused"}}))
    assert conn.results[-1][1]["tuning"] == {"zone_switch_secs": 30, "position_estimator": "fused"}
    run(ws.ws_tuning_set(hass, conn, {"id": 5, "type": "sextant/tuning/set", "settings": {"nope": 1}}))
    assert "unknown tuning key" in conn.errors[-1][2]
    run(ws.ws_thing_tune(hass, conn, {"id": 6, "type": "sextant/thing/tune", "entity": "fry",
                                        "ref_offset_db": 3.0, "height": 0.3, "icon": "/local/sextant_icons/cat.png"}))
    layout = st.get_layout(hass)
    assert layout["thing_ref_offsets"] == {"fry": 3.0} and layout["thing_heights"] == {"fry": 0.3}
    assert layout["thing_icons"] == {"fry": "/local/sextant_icons/cat.png"}
    run(ws.ws_thing_tune(hass, conn, {"id": 7, "type": "sextant/thing/tune", "entity": "fry", "ref_offset_db": None, "height": None}))
    layout = st.get_layout(hass)
    assert layout["thing_ref_offsets"] == {} and layout["thing_heights"] == {}
    run(ws.ws_thing_tune(hass, conn, {"id": 8, "type": "sextant/thing/tune", "entity": "fry", "height": 9.0}))
    assert "height" in conn.errors[-1][2]


def test_bermuda_commands_pass_through_the_management_api(tmp_path, monkeypatch):
    hass = _hass_with_layout(tmp_path)
    calls = []
    api = types.ModuleType("custom_components.bermuda.api")
    api.SNAPSHOT_VERSION = 1
    api.SNAPSHOT_FEATURES = frozenset({"device_management", "tracked_devices", "tile_identity"})
    api.async_get_tile_identities = lambda _h: {"cafe01": {"uid": "cafe01", "addresses": ["aa"], "tile_id": None}}

    async def bind_tile(_h, tile_id, uid):
        if uid == "taken":
            raise ValueError("Tile ID taken is already declared as tile_x")
        calls.append(("bind", tile_id, uid))
        return {"tile_id": tile_id, "uid": uid, "address": "aa"}

    api.async_bind_tile = bind_tile
    api.async_get_advert_snapshot = lambda *a, **k: None
    api.async_get_coordinator = lambda _h: types.SimpleNamespace(tile_manager=types.SimpleNamespace(diagnostics=lambda: {"handovers": 2}))
    api.async_get_device_candidates = lambda _h, **kw: [{"address": "aa", "config_value": "AA"}]
    api.async_get_tracked_devices = lambda _h: {"aa": {"name": "A"}}

    async def set_tracked(_h, add=(), remove=()):
        calls.append(("track", list(add), list(remove)))
        return ["AA"]

    api.async_set_tracked_devices = set_tracked
    api.async_get_findmy_accessories = lambda _h: []
    api.async_get_options = lambda _h: {"ref_power": -55}

    async def set_options(_h, changes):
        if "bad" in changes:
            raise ValueError("not a managed option: bad")
        return {"ref_power": changes.get("ref_power", -55)}

    api.async_set_options = set_options
    pkg = types.ModuleType("custom_components.bermuda"); pkg.api = api
    parent = sys.modules.get("custom_components") or types.ModuleType("custom_components"); parent.bermuda = pkg
    monkeypatch.setitem(sys.modules, "custom_components", parent)
    monkeypatch.setitem(sys.modules, "custom_components.bermuda", pkg)
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", api)

    conn = _Conn()
    run(ws.ws_bermuda_candidates(hass, conn, {"id": 1, "type": "sextant/bermuda/candidates"}))
    assert conn.results[-1][1]["candidates"][0]["config_value"] == "AA"
    run(ws.ws_bermuda_track(hass, conn, {"id": 2, "type": "sextant/bermuda/track", "add": ["aa"], "remove": []}))
    assert calls == [("track", ["aa"], [])] and conn.results[-1][1]["configured_devices"] == ["AA"]
    # Untracking takes the device's Sextant sensors and device with it, resolved
    # from the address the Things page sends to the slug the sensors carry.
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    api.async_get_tracked_devices = lambda _h: {"aa": {"name": "A", "slug": "phone"}, "bb": {"name": "B", "slug": "watch"}}
    for slug in ("phone", "watch"):
        dr.async_get(hass).add({("sextant", slug)}, device_id=f"dev_{slug}")
        for suffix in ("sextant_room", "sextant_floor", "sextant_nearest_room", "sextant_spot"):
            er.async_get(hass).add(f"sensor.{slug}_{suffix}", unique_id=f"{suffix}_{slug}", device_id=f"dev_{slug}")
    run(ws.ws_bermuda_track(hass, conn, {"id": 20, "type": "sextant/bermuda/track", "add": [], "remove": ["AA"]}))
    assert calls[-1] == ("track", [], ["AA"])
    assert not any(e.startswith("sensor.phone_") for e in er.async_get(hass).entities)
    assert dr.async_get(hass).async_get_device(identifiers={("sextant", "phone")}) is None
    assert sum(e.startswith("sensor.watch_") for e in er.async_get(hass).entities) == 4
    run(ws.ws_bermuda_options_set(hass, conn, {"id": 3, "type": "sextant/bermuda/options/set", "options": {"bad": 1}}))
    assert "managed option" in conn.errors[-1][2]
    run(ws.ws_bermuda_tiles(hass, conn, {"id": 4, "type": "sextant/bermuda/tiles"}))
    assert conn.results[-1][1]["tiles"] == {"handovers": 2}
    run(ws.ws_bermuda_tile_identities(hass, conn, {"id": 5, "type": "sextant/bermuda/tile_identities"}))
    assert conn.results[-1][1]["identities"]["cafe01"]["addresses"] == ["aa"]
    run(ws.ws_bermuda_tile_bind(hass, conn, {"id": 6, "type": "sextant/bermuda/tile/bind", "tile_id": "tile_1", "uid": "cafe01"}))
    assert calls[-1] == ("bind", "tile_1", "cafe01") and conn.results[-1][1]["address"] == "aa"
    run(ws.ws_bermuda_tile_bind(hass, conn, {"id": 7, "type": "sextant/bermuda/tile/bind", "tile_id": "tile_1", "uid": "taken"}))
    assert "already declared" in conn.errors[-1][2]


def test_bermuda_commands_explain_a_missing_api(tmp_path, monkeypatch):
    hass = _hass_with_layout(tmp_path)
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", None)
    conn = _Conn()
    run(ws.ws_bermuda_candidates(hass, conn, {"id": 1, "type": "sextant/bermuda/candidates"}))
    assert "update Bermuda" in conn.errors[-1][2]


def test_kpi_metrics_match_the_command_line_tool():
    from sextant import kpi
    sys.path.insert(0, "tools")
    import flap_kpi
    rows = [{"state": s, "last_changed": f"2026-09-17T05:{m:02d}:00+00:00"}
            for m, s in enumerate(["Kitchen", "Kitchen", "Office", "Kitchen", "unknown", "Office"])]
    assert kpi.compute_metrics(rows, 1.0) == flap_kpi.compute_metrics(rows, 1.0)
    per = {"sensor.a_sextant_zone": kpi.compute_metrics(rows, 1.0)}
    assert kpi.summarise(per) == flap_kpi.summarise(per)
    # Recorder rows come as objects or minimal dicts; both feed the metrics.
    from datetime import datetime, timezone
    objs = [types.SimpleNamespace(state="A", last_changed=datetime(2026, 9, 17, tzinfo=timezone.utc)), {"state": "B", "last_changed": "2026-09-17T00:10:00+00:00"}]
    assert kpi.compute_metrics(kpi.rows_from_recorder(objs), 1.0)["changes"] == 1


def test_kpi_deltas_compare_entities_and_summaries_present_on_both_sides():
    from sextant import kpi
    current = {"entities": {"sensor.a_sextant_zone": {"changes_per_hour": 4.0, "flip_ratio": 0.2, "median_dwell_s": 300.0, "dead": 0},
                            "sensor.new_sextant_zone": {"changes_per_hour": 1.0}},
               "summary": {"sextant_zone": {"changes_per_thing_hour": 5.0, "flip_ratio": 0.3, "median_of_median_dwell_s": 200.0}}}
    baseline = {"entities": {"sensor.a_sextant_zone": {"changes_per_hour": 10.0, "flip_ratio": 0.5, "median_dwell_s": 100.0, "dead": 1}},
                "summary": {"sextant_zone": {"changes_per_thing_hour": 8.0, "flip_ratio": 0.4, "median_of_median_dwell_s": 150.0}}}
    d = kpi.deltas(current, baseline)
    # keys come back under the current names, whatever the recording called them
    assert d["entities"] == {"sensor.a_sextant_room": {"changes_per_hour": -6.0, "flip_ratio": -0.3, "median_dwell_s": 200.0, "dead": -1}}
    assert d["summary"] == {"sextant_room": {"changes_per_thing_hour": -3.0, "flip_ratio": -0.1, "median_of_median_dwell_s": 50.0}}
    assert kpi.deltas({}, None) == {"entities": {}, "summary": {}}


def test_kpi_baselines_are_saved_listed_compared_and_deleted(tmp_path, monkeypatch):
    hass = _hass_with_layout(tmp_path, _layout())
    windows = iter([
        {"hours": 12.0, "generated_at": "2026-09-17T05:00:00+00:00",
         "entities": {"sensor.a_sextant_zone": {"changes_per_hour": 10.0, "flip_ratio": 0.5, "median_dwell_s": 100.0, "dead": 0}},
         "summary": {"sextant_zone": {"changes_per_thing_hour": 10.0, "flip_ratio": 0.5, "median_of_median_dwell_s": 100.0}}},
        {"hours": 12.0, "generated_at": "2026-09-17T17:00:00+00:00",
         "entities": {"sensor.a_sextant_zone": {"changes_per_hour": 4.0, "flip_ratio": 0.2, "median_dwell_s": 300.0, "dead": 0}},
         "summary": {"sextant_zone": {"changes_per_thing_hour": 4.0, "flip_ratio": 0.2, "median_of_median_dwell_s": 300.0}}},
    ])

    windows = list(windows)

    async def fake_compute(hass, hours):
        return windows.pop(0) if len(windows) > 1 else windows[0]   # the last window repeats
    monkeypatch.setattr(ws, "_compute_kpi", fake_compute)
    conn = _Conn()
    run(ws.ws_kpi_baseline_save(hass, conn, {"id": 1, "type": "sextant/kpi/baseline/save", "name": "  ", "hours": 12}))
    assert conn.errors and "name" in conn.errors[-1][2]
    run(ws.ws_kpi_baseline_save(hass, conn, {"id": 2, "type": "sextant/kpi/baseline/save", "name": "geometric", "hours": 12}))
    assert conn.results[-1][1] == {"name": "geometric", "saved_at": "2026-09-17T05:00:00+00:00", "things": 1}
    assert (tmp_path / ".storage" / "sextant_kpi_baselines").exists() or run(st.load_kpi_baselines(hass))["geometric"]["hours"] == 12
    run(ws.ws_kpi_baselines(hass, conn, {"id": 3, "type": "sextant/kpi/baselines"}))
    rows = conn.results[-1][1]["baselines"]
    assert [r["name"] for r in rows] == ["geometric"] and rows[0]["things"] == 1 and rows[0]["hours"] == 12
    run(ws.ws_kpi(hass, conn, {"id": 4, "type": "sextant/kpi", "hours": 12, "baseline": "nope"}))
    assert conn.errors[-1][2].startswith("no KPI baseline")
    run(ws.ws_kpi(hass, conn, {"id": 5, "type": "sextant/kpi", "hours": 12, "baseline": "geometric"}))
    result = conn.results[-1][1]
    assert result["baseline"]["name"] == "geometric"
    assert result["deltas"]["entities"]["sensor.a_sextant_room"]["changes_per_hour"] == -6.0
    assert result["deltas"]["summary"]["sextant_room"]["median_of_median_dwell_s"] == 200.0
    run(ws.ws_kpi_baseline_delete(hass, conn, {"id": 6, "type": "sextant/kpi/baseline/delete", "name": "geometric"}))
    assert conn.results[-1][1] == {"deleted": "geometric"}
    run(ws.ws_kpi_baseline_delete(hass, conn, {"id": 7, "type": "sextant/kpi/baseline/delete", "name": "geometric"}))
    assert conn.errors[-1][2].startswith("no KPI baseline")
    assert run(st.load_kpi_baselines(hass)) == {}


def test_scanner_ranging_passes_through_and_explains_a_missing_api(tmp_path, monkeypatch):
    hass = _hass_with_layout(tmp_path, _layout())
    conn = _Conn()
    payload = {"stamp": 1.0, "scanners": {"aa": {"bb": {"distance": 3.2, "age": 1.0}}}}
    monkeypatch.setattr(ws.bermuda_source, "async_get_scanner_ranging", lambda hass, max_age=None: payload)
    run(ws.ws_bermuda_scanner_ranging(hass, conn, {"id": 1, "type": "sextant/bermuda/scanner_ranging", "max_age": 30}))
    assert conn.results[-1][1] == payload
    monkeypatch.setattr(ws.bermuda_source, "async_get_scanner_ranging", lambda hass, max_age=None: None)
    run(ws.ws_bermuda_scanner_ranging(hass, conn, {"id": 2, "type": "sextant/bermuda/scanner_ranging"}))
    assert "scanner_ranging" in conn.errors[-1][2]


def test_thing_names_come_from_bermuda_tidied_and_user_renames_win(tmp_path, monkeypatch):
    hass = _hass_with_layout(tmp_path, _layout())
    monkeypatch.setattr(ws.bermuda_source, "async_get_tracked_devices", lambda _h: {
        "aa": {"slug": "fry", "name": "Fry"},
        "bb": {"slug": "private_ble_device_david_s_phone", "name": "Private BLE Device David's Phone"},
        "cc": {"slug": "private_ble_jack_watch", "name": "Private BLE Jack Watch"},
    })
    names = ws._thing_names(hass, ["fry", "private_ble_device_david_s_phone", "private_ble_jack_watch"])
    assert names == {"fry": "Fry", "private_ble_device_david_s_phone": "David's Phone", "private_ble_jack_watch": "Jack Watch"}
    assert ws._tidy_device_name("Private BLE Device ").strip()   # a prefix alone never tidies to nothing
    assert ws._tidy_device_name("Fry") == "Fry"


def test_truth_marks_are_recorded_evaluated_listed_applied_and_deleted(tmp_path, monkeypatch):
    import math
    from sextant import truth
    layout = {"floor": [{"name": "F", "scale": 100.0, "receivers": [{"entity_id": f"r{i}", "cords": {"x": x, "y": y}} for i, (x, y) in enumerate([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])],
                         "zones": [{"entity_id": "Room", "cords": [{"x": 300, "y": 300}, {"x": 700, "y": 300}, {"x": 700, "y": 700}, {"x": 300, "y": 700}]}], "subzones": []}]}
    hass = _hass_with_layout(tmp_path, layout)
    hass.async_add_executor_job = lambda fn, *a: asyncio.sleep(0, result=fn(*a))
    buf = truth.Buffer()
    monkeypatch.setattr(sextant, "_truth_buffer", buf)
    pts = [(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)]
    for i in range(4):
        weighted = [(px, py, math.hypot(px - 500, py - 500), 1.0, math.hypot(px - 500, py - 500)) for px, py in pts]
        buf.remember("phone", [{"floor": "F", "weighted": weighted, "bounds": (-100, -100, 1100, 1100), "min_wr": 50.0, "scale": 100.0}], {"a": 1.0}, 1.0, "geometric")
    conn = _Conn()
    run(ws.ws_truth_mark(hass, conn, {"id": 1, "type": "sextant/truth/mark", "entity": "phone", "floor": "F", "x": 500.0, "y": 500.0, "window_secs": 300}))
    assert not conn.errors, conn.errors
    result = conn.results[-1][1]
    assert result["mark"]["id"] == 1 and result["mark"]["samples"] == 4
    assert result["rows"][0]["estimator"] == "geometric" and result["rows"][0]["mean_m"] < 0.2 and result["rows"][0]["room_ok"] == 1.0
    run(ws.ws_truth_mark(hass, conn, {"id": 2, "type": "sextant/truth/mark", "entity": "ghost", "floor": "F", "x": 1, "y": 1, "window_secs": 300}))
    assert conn.errors and "cycle" in conn.errors[-1][2]
    run(ws.ws_truth_list(hass, conn, {"id": 3, "type": "sextant/truth/list", "entity": "phone"}))
    assert [m["id"] for m in conn.results[-1][1]["marks"]] == [1] and "samples" in conn.results[-1][1]["marks"][0]
    run(ws.ws_truth_evaluate(hass, conn, {"id": 4, "type": "sextant/truth/evaluate"}))
    summary = conn.results[-1][1]
    assert summary["things"]["phone"]["marks"] == 1 and summary["things"]["phone"]["mean_m"] < 0.2
    run(ws.ws_truth_apply(hass, conn, {"id": 5, "type": "sextant/truth/apply", "entity": "phone", "weight": 0.25, "gain": 1.4}))
    applied = conn.results[-1][1]
    assert applied["fp_weight"] == 0.25 and applied["estimator"] == "fused" and abs(applied["thing_gain"] - 1.4) < 1e-6
    saved = st.get_layout(hass)
    assert saved["thing_fp_weights"]["phone"] == 0.25 and saved["thing_fp_gains"]["phone"] == 1.4
    run(ws.ws_thing_tune(hass, conn, {"id": 6, "type": "sextant/thing/tune", "entity": "phone", "fp_weight": None}))
    assert "phone" not in st.get_layout(hass)["thing_fp_weights"]
    run(ws.ws_thing_tune(hass, conn, {"id": 7, "type": "sextant/thing/tune", "entity": "phone", "fp_weight": 1.5}))
    assert conn.errors[-1][2].startswith("fp_weight")
    run(ws.ws_truth_evaluate(hass, conn, {"id": 8, "type": "sextant/truth/evaluate", "mark_id": 1}))
    assert conn.results[-1][1]["mark"]["id"] == 1 and len(conn.results[-1][1]["rows"]) >= 1
    run(ws.ws_truth_delete(hass, conn, {"id": 9, "type": "sextant/truth/delete", "mark_id": 1}))
    assert conn.results[-1][1]["removed"] == 1 and sextant._truth_marks == []


def test_every_websocket_handler_is_registered():
    handlers = {name for name in dir(ws) if name.startswith("ws_")}
    registered = {fn.__name__ for fn in ws.COMMANDS}
    assert handlers <= registered, sorted(handlers - registered)


def test_thing_colour_is_validated_and_stored(tmp_path):
    hass = _hass_with_layout(tmp_path, _layout())
    conn = _Conn()
    run(ws.ws_thing_tune(hass, conn, {"id": 1, "type": "sextant/thing/tune", "entity": "fry", "color": "#6D4C41"}))
    assert st.get_layout(hass)["thing_colors"]["fry"] == "#6d4c41" and conn.results[-1][1]["color"] == "#6d4c41"
    run(ws.ws_thing_tune(hass, conn, {"id": 2, "type": "sextant/thing/tune", "entity": "fry", "color": "brown"}))
    assert conn.errors[-1][2].startswith("color")
    run(ws.ws_thing_tune(hass, conn, {"id": 3, "type": "sextant/thing/tune", "entity": "fry", "color": None}))
    assert "fry" not in st.get_layout(hass)["thing_colors"]


def test_layout_save_keeps_what_the_server_owns(tmp_path):
    """A Save from the Edit page merges its floors into the current layout: tuning,
    thing settings, calibration stamps and per-proxy corrections survive."""
    current = {
        "floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [],
                   "calibration": {"applied_at": "t0", "auto": True},
                   "receivers": [{"entity_id": "r0", "cords": {"x": 0, "y": 0}, "correction": 0.9},
                                 {"entity_id": "gone", "cords": {"x": 9, "y": 9}, "correction": 1.2}]}],
        "tuning": {"zone_switch_secs": 45}, "thing_colors": {"willow": "#6d4c41"}, "auto_calibration": True,
    }
    hass = _hass_with_layout(tmp_path, current)
    editor_copy = {  # cloned before the colour and the corrections existed, receiver moved, one added, one deleted
        "floor": [{"name": "F", "scale": 100.0, "zones": [{"zone_id": "z", "entity_id": "Hall", "poly": True, "cords": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}], "subzones": [],
                   "receivers": [{"entity_id": "r0", "cords": {"x": 50, "y": 60}, "height": 1.5, "correction": 0.5},
                                 {"entity_id": "new", "cords": {"x": 1, "y": 2}}]}],
        "tuning": {}, "thing_colors": {},
    }
    conn = _Conn()
    run(ws.ws_layout_save(hass, conn, {"id": 9, "type": "sextant/layout/save", "layout": editor_copy}))
    assert conn.results and not conn.errors
    saved = st.get_layout(hass)
    assert saved["tuning"] == {"zone_switch_secs": 45} and saved["thing_colors"] == {"willow": "#6d4c41"} and saved["auto_calibration"] is True
    floor = saved["floor"][0]
    assert floor["calibration"] == {"applied_at": "t0", "auto": True}
    assert [z["entity_id"] for z in floor["zones"]] == ["Hall"]
    recs = {r["entity_id"]: r for r in floor["receivers"]}
    assert set(recs) == {"r0", "new"}                       # the editor decides which proxies exist and where
    assert recs["r0"]["cords"] == {"x": 50, "y": 60} and recs["r0"]["height"] == 1.5
    assert recs["r0"]["correction"] == 0.9                  # the server's correction, not the editor's stale copy
    assert "correction" not in recs["new"]


def test_a_proxy_out_of_calibration_owns_its_correction_through_a_save(tmp_path):
    """The server owns corrections so a stale editor cannot undo calibration -
    but a proxy switched out of calibration is the exception, or its number
    could be looked at and never changed. The laundry tablet, 2026-09-22."""
    current = {"floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [],
                          "receivers": [{"entity_id": "tablet", "cords": {"x": 0, "y": 0}, "correction": 5.0},
                                        {"entity_id": "r0", "cords": {"x": 1, "y": 1}, "correction": 0.9}]}]}
    hass = _hass_with_layout(tmp_path, current)
    editor = {"floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [],
                         "receivers": [{"entity_id": "tablet", "cords": {"x": 0, "y": 0}, "correction": 1.0, "calibrate": False},
                                       {"entity_id": "r0", "cords": {"x": 1, "y": 1}, "correction": 0.5}]}]}
    conn = _Conn()
    run(ws.ws_layout_save(hass, conn, {"id": 9, "type": "sextant/layout/save", "layout": editor}))
    recs = {r["entity_id"]: r for r in st.get_layout(hass)["floor"][0]["receivers"]}
    assert recs["tablet"]["correction"] == 1.0 and recs["tablet"]["calibrate"] is False
    assert recs["r0"]["correction"] == 0.9      # everything else still the server's


def test_layout_save_on_a_fresh_install_takes_the_editor_layout_whole(tmp_path):
    hass = _hass_with_layout(tmp_path)
    conn = _Conn()
    run(ws.ws_layout_save(hass, conn, {"id": 10, "type": "sextant/layout/save", "layout": _layout()}))
    assert st.get_layout(hass)["floor"][0]["receivers"][0]["entity_id"] == "r0"


def test_advice_reports_rooms_and_unplaced_scanners(tmp_path, monkeypatch):
    from sextant import bermuda_source
    layout = {"floor": [{"name": "F", "scale": 100.0, "subzones": [],
                         "zones": [{"zone_id": "z1", "entity_id": "Hall", "poly": True, "cords": [{"x": 0, "y": 0}, {"x": 400, "y": 0}, {"x": 400, "y": 400}, {"x": 0, "y": 400}]},
                                   {"zone_id": "z2", "entity_id": "Far", "poly": True, "cords": [{"x": 1200, "y": 0}, {"x": 1600, "y": 0}, {"x": 1600, "y": 400}, {"x": 1200, "y": 400}]}],
                         "receivers": [{"entity_id": "a", "address": "aa:aa:aa:aa:aa:01", "cords": {"x": 20, "y": 20}},
                                       {"entity_id": "b", "cords": {"x": 380, "y": 20}}, {"entity_id": "c", "cords": {"x": 200, "y": 380}}]}]}
    hass = _hass_with_layout(tmp_path, layout)
    monkeypatch.setattr(bermuda_source, "async_get_scanner_directory", lambda _h: {
        "aa:aa:aa:aa:aa:01": {"slug": "a", "name": "A", "last_seen_age": 3.0},          # placed by address
        "bb:bb:bb:bb:bb:02": {"slug": "b", "name": "B", "last_seen_age": 3.0},          # placed by slug
        "cc:cc:cc:cc:cc:03": {"slug": "spare", "name": "Spare S3", "last_seen_age": 40.0},
        "dd:dd:dd:dd:dd:04": {"slug": "gone", "name": "Gone", "last_seen_age": 90000.0},  # not heard for a day
    })
    conn = _Conn()
    run(ws.ws_advice(hass, conn, {"id": 30, "type": "sextant/advice"}))
    assert not conn.errors, conn.errors
    out = conn.results[-1][1]
    assert [u["name"] for u in out["unplaced"]] == ["Spare S3"] and out["unplaced"][0]["suggest"]["room"] == "Far"
    rooms = {r["room"]: r for r in out["rooms"]}
    assert rooms["Far"]["issue"] == "no proxy" and rooms["Far"]["add"] >= 1 and out["rooms"][0]["room"] == "Far"
    assert rooms["Hall"]["proxies"] == 3 and out["summary"]["rooms"] == 2
    # No plan at all is a clear error, not a traceback.
    empty = _hass_with_layout(tmp_path / "empty")
    run(ws.ws_advice(empty, conn, {"id": 31, "type": "sextant/advice"}))
    assert conn.errors and "floor plan" in conn.errors[-1][2]


def test_every_write_and_bermuda_command_requires_admin():
    """Any signed-in user can open a websocket; only administrators may change
    the layout, tuning, things, history or Bermuda, list every address Bermuda
    hears, or read where people have been. The live feed and the layout stay
    open so the Live page and the dashboard map card work for everyone."""
    admin = {f.__name__ for f in ws.COMMANDS if getattr(f, "_ws_admin", False)}
    open_ = {f.__name__ for f in ws.COMMANDS if not getattr(f, "_ws_admin", False)}
    assert {"ws_layout_save", "ws_tuning_set", "ws_thing_tune", "ws_truth_mark", "ws_truth_delete",
            "ws_truth_apply", "ws_history_clear", "ws_calibration_action", "ws_adjust_zones",
            "ws_kpi_baseline_save", "ws_kpi_baseline_delete"} <= admin
    assert not any(name.startswith("ws_bermuda_") for name in open_)
    assert {"ws_history_get", "ws_history_timeline", "ws_history_index", "ws_truth_list"} <= admin
    assert {"ws_layout_get", "ws_calibration_status", "ws_selftest",
            "ws_advice", "ws_receivers", "ws_kpi"} <= open_


def test_ignored_scanners_leave_the_unplaced_lists_and_survive_an_editor_save(tmp_path, monkeypatch):
    from sextant import bermuda_source
    layout = {"floor": [{"name": "F", "scale": 100.0, "subzones": [], "zones": [],
                         "receivers": [{"entity_id": "a", "address": "aa:aa:aa:aa:aa:01", "cords": {"x": 20, "y": 20}}]}]}
    hass = _hass_with_layout(tmp_path, layout)
    monkeypatch.setattr(bermuda_source, "async_get_scanner_directory", lambda _h: {
        "aa:aa:aa:aa:aa:01": {"slug": "a", "name": "A", "last_seen_age": 3.0},
        "cc:cc:cc:cc:cc:03": {"slug": "kiosk", "name": "Kiosk", "last_seen_age": 4.0},
        "dd:dd:dd:dd:dd:04": {"slug": "shed", "name": "Shed", "last_seen_age": 5.0},
    })
    monkeypatch.setattr(bermuda_source, "async_get_scanner_ages", lambda _h: {})
    assert [u["slug"] for u in ws._unplaced_scanners(hass, st.get_layout(hass))] == ["kiosk", "shed"]

    conn = _Conn()
    run(ws.ws_scanner_ignore(hass, conn, {"id": 1, "type": "sextant/scanner/ignore", "address": "CC:CC:CC:CC:CC:03", "ignored": True}))
    assert conn.results[-1][1] == {"ignored": [{"address": "cc:cc:cc:cc:cc:03", "slug": "kiosk", "name": "Kiosk"}]}
    assert st.get_layout(hass)["ignored_scanners"] == ["cc:cc:cc:cc:cc:03"]
    assert [u["slug"] for u in ws._unplaced_scanners(hass, st.get_layout(hass))] == ["shed"]

    run(ws.ws_receivers(hass, conn, {"id": 2, "type": "sextant/receivers"}))
    rx = conn.results[-1][1]
    assert [u["slug"] for u in rx["unplaced"]] == ["shed"] and [u["slug"] for u in rx["ignored"]] == ["kiosk"]

    # The editor owns floor geometry only: a save from it keeps the list.
    run(ws.ws_layout_save(hass, conn, {"id": 3, "type": "sextant/layout/save", "layout": {"floor": st.get_layout(hass)["floor"]}}))
    assert st.get_layout(hass)["ignored_scanners"] == ["cc:cc:cc:cc:cc:03"]

    run(ws.ws_scanner_ignore(hass, conn, {"id": 4, "type": "sextant/scanner/ignore", "address": "cc:cc:cc:cc:cc:03", "ignored": False}))
    assert conn.results[-1][1] == {"ignored": []}
    assert [u["slug"] for u in ws._unplaced_scanners(hass, st.get_layout(hass))] == ["kiosk", "shed"]
    run(ws.ws_scanner_ignore(hass, conn, {"id": 5, "type": "sextant/scanner/ignore", "address": "  ", "ignored": True}))
    assert conn.errors
    assert ws.ignored_scanners({"ignored_scanners": "nope"}) == set() and ws.ignored_scanners(None) == set()
    assert getattr(ws.ws_scanner_ignore, "_ws_admin", False)


def _flow_hass(tmp_path, result, *, init_raises=None):
    """A hass whose config-entry flow answers with `result`, recording the calls."""
    hass = _hass_with_layout(tmp_path, _layout())
    calls = []

    async def async_init(domain, context=None):
        calls.append(("init", domain, context))
        if init_raises:
            raise init_raises
        return {"flow_id": "flow1"}

    async def async_configure(flow_id, data):
        calls.append(("configure", flow_id, data))
        return result

    async def async_abort(flow_id):
        calls.append(("abort", flow_id))

    hass.config_entries = types.SimpleNamespace(
        flow=types.SimpleNamespace(async_init=async_init, async_configure=async_configure, async_abort=async_abort))
    hass._flow_calls = calls
    return hass


def test_adding_a_phone_by_irk_drives_the_home_assistant_flow(tmp_path):
    hass = _flow_hass(tmp_path, {"type": "create_entry", "title": "Pixel 9"})
    conn = _Conn()
    run(ws.ws_irk_add(hass, conn, {"id": 1, "type": "sextant/irk/add", "irk": "  irk:aabb  "}))
    assert conn.results[-1][1] == {"title": "Pixel 9", "irk": "irk:aabb"}
    assert hass._flow_calls[0] == ("init", "private_ble_device", {"source": "user"})
    assert hass._flow_calls[1] == ("configure", "flow1", {"irk": "irk:aabb"})
    assert not conn.errors


def test_irk_failures_are_explained_and_the_flow_is_not_left_open(tmp_path):
    for reason, expect in (("irk_not_valid", "32 hex characters"),
                           ("irk_not_found", "near a proxy"),
                           ("bluetooth_not_available", "no Bluetooth scanner"),
                           ("something_else", "something_else")):
        hass = _flow_hass(tmp_path, {"type": "form", "errors": {"irk": reason}})
        conn = _Conn()
        run(ws.ws_irk_add(hass, conn, {"id": 1, "type": "sextant/irk/add", "irk": "aabb"}))
        assert conn.errors, reason
        assert expect in str(conn.errors[-1]), (reason, conn.errors[-1])
        assert ("abort", "flow1") in hass._flow_calls, reason  # no half-open flow left behind

    # An abort (rather than a form) carries its reason in another key.
    hass = _flow_hass(tmp_path, {"type": "abort", "reason": "bluetooth_not_available"})
    conn = _Conn()
    run(ws.ws_irk_add(hass, conn, {"id": 1, "type": "sextant/irk/add", "irk": "aabb"}))
    assert "no Bluetooth scanner" in str(conn.errors[-1])


def test_irk_add_rejects_an_empty_key_and_survives_a_broken_flow(tmp_path):
    hass = _flow_hass(tmp_path, {})
    conn = _Conn()
    run(ws.ws_irk_add(hass, conn, {"id": 1, "type": "sextant/irk/add", "irk": "   "}))
    assert "Paste the key" in str(conn.errors[-1]) and not hass._flow_calls

    hass = _flow_hass(tmp_path, {}, init_raises=RuntimeError("no such integration"))
    conn = _Conn()
    run(ws.ws_irk_add(hass, conn, {"id": 2, "type": "sextant/irk/add", "irk": "aabb"}))
    assert "could not take the key" in str(conn.errors[-1])
    assert getattr(ws.ws_irk_add, "_ws_admin", False)


def test_a_stale_editor_save_cannot_erase_a_bias_field_and_pins_go_through(tmp_path):
    """The bias field is written by its service while the Edit page sits open
    on an older copy. Its Save must not take the field out with it."""
    from sextant import floor_field
    layout = _layout()
    hass = _hass_with_layout(tmp_path, layout)
    stale = st.get_layout_for_edit(hass)                       # what the editor loaded
    fresh = st.get_layout_for_edit(hass)
    fresh["floor"][0]["bias_field"] = floor_field.flat((0, 0, 300, 200), 100.0, value=1.5)
    run(st.save_layout(hass, fresh))                           # ... the service paints meanwhile
    stale["floor"][0]["pins"] = [{"pin_id": "p1", "name": "NW", "cords": {"x": 10, "y": 20}}]
    stale["floor"][0]["elevation"] = 3.66
    conn = _Conn()
    run(ws.ws_layout_save(hass, conn, {"id": 9, "type": "sextant/layout/save", "layout": stale}))
    saved = st.get_layout(hass)["floor"][0]
    assert floor_field.describe(saved)["max"] == 1.5           # the field survived
    assert saved["pins"][0]["name"] == "NW" and saved["elevation"] == 3.66
    # And the editor cannot smuggle a field in either: it does not own the key.
    stale["floor"][0]["bias_field"] = {"cell_m": 1.0, "x0": 0, "y0": 0, "values": [[9.0]]}
    run(ws.ws_layout_save(hass, conn, {"id": 10, "type": "sextant/layout/save", "layout": stale}))
    assert floor_field.describe(st.get_layout(hass)["floor"][0])["max"] == 1.5


def test_registration_grades_a_draft_or_the_stored_layout(tmp_path):
    def floor(name, level, scale, dx):
        pts = {"NW": (0, 0), "NE": (10, 0), "SE": (10, 8)}
        return {"name": name, "level": level, "scale": scale, "receivers": [], "zones": [], "subzones": [],
                "pins": [{"pin_id": f"{name}{k}", "name": k, "cords": {"x": x * scale + dx, "y": y * scale}} for k, (x, y) in pts.items()]}
    hass = _hass_with_layout(tmp_path, {"floor": [floor("G", 0, 100.0, 0), floor("U", 1, 125.0, 40)]})
    conn = _Conn()
    run(ws.ws_registration(hass, conn, {"id": 1, "type": "sextant/registration"}))
    stored = conn.results[-1][1]
    assert stored["reference"] == "G" and stored["floors"]["U"]["ok"] and stored["floors"]["U"]["rms_m"] == 0.0
    assert stored["floors"]["U"]["elevation"] == 3.0
    draft = {"floor": [floor("G", 0, 100.0, 0), floor("U", 1, 125.0, 40)]}
    draft["floor"][1]["pins"][1]["cords"]["x"] += 100          # drag one pin 0.8 m in the unsaved draft
    run(ws.ws_registration(hass, conn, {"id": 2, "type": "sextant/registration", "layout": draft}))
    graded = conn.results[-1][1]["floors"]["U"]
    assert graded["worst"] == "NE" and graded["rms_m"] > 0.2


def test_history_timeline_is_served_for_the_last_hours(tmp_path):
    import time as _time
    hass = _hass_with_layout(tmp_path, _layout())
    h = sextant.get_position_history(hass)
    now = _time.time()
    for dt in range(-7200, -59, 60):                  # heard every minute for two hours
        h.record("cat", now + dt, 1.0, 1.0, "F", 100.0, "Office", "Desk" if dt >= -3600 else None)
    conn = _Conn()
    run(ws.ws_history_timeline(hass, conn, {"id": 1, "type": "sextant/history/timeline", "entity": "cat", "hours": 1.5}))
    result = conn.results[-1][1]
    # 1.5 h back reaches into the "no spot" stretch but not to where the record starts.
    assert [(s["room"], s["spot"]) for s in result["stays"]] == [("Office", None), ("Office", "Desk")]
    assert result["stays"][0]["partial"] is False
    assert result["stays"][1]["start"] == round(now - 3600, 1)
    assert result["last_heard"] == round(now - 60, 1)


def test_thing_tune_sets_and_clears_an_owner_and_pronouns(tmp_path):
    hass = _hass_with_layout(tmp_path, _layout())
    conn = _Conn()
    run(ws.ws_thing_tune(hass, conn, {"id": 1, "type": "sextant/thing/tune", "entity": "watch",
                                        "owner": "person.david", "pronouns": "it"}))
    layout = st.get_layout(hass)
    assert layout["thing_owners"] == {"watch": "person.david"} and layout["thing_pronouns"] == {"watch": "it"}
    run(ws.ws_thing_tune(hass, conn, {"id": 2, "type": "sextant/thing/tune", "entity": "watch", "owner": ""}))
    assert st.get_layout(hass)["thing_owners"] == {}


def test_thing_tune_sets_and_clears_an_on_charger_sensor(tmp_path):
    hass = _hass_with_layout(tmp_path, _layout())
    conn = _Conn()
    run(ws.ws_thing_tune(hass, conn, {"id": 1, "type": "sextant/thing/tune", "entity": "watch",
                                        "charging_entity": "sensor.david_apple_watch_battery_status"}))
    assert st.get_layout(hass)["thing_charging_entity"] == {"watch": "sensor.david_apple_watch_battery_status"}
    # Not a sensor: refused, nothing stored.
    run(ws.ws_thing_tune(hass, conn, {"id": 2, "type": "sextant/thing/tune", "entity": "watch",
                                        "charging_entity": "light.kitchen"}))
    assert conn.errors and st.get_layout(hass)["thing_charging_entity"] == {"watch": "sensor.david_apple_watch_battery_status"}
    run(ws.ws_thing_tune(hass, conn, {"id": 3, "type": "sextant/thing/tune", "entity": "watch", "charging_entity": None}))
    assert st.get_layout(hass)["thing_charging_entity"] == {}


def test_thing_tune_sets_and_clears_a_battery_sensor(tmp_path):
    """Socks's tag sat at 1% and nothing on the Live page said so: he just
    went quiet, which reads exactly like a cat that has left the house."""
    hass = _hass_with_layout(tmp_path, _layout())
    conn = _Conn()
    run(ws.ws_thing_tune(hass, conn, {"id": 1, "type": "sextant/thing/tune", "entity": "socks",
                                        "battery_entity": "sensor.great_room_holy_iot_sensors_socks_battery"}))
    assert st.get_layout(hass)["thing_battery_entity"] == {"socks": "sensor.great_room_holy_iot_sensors_socks_battery"}
    # A battery LEVEL is a sensor; anything else is refused and nothing changes.
    run(ws.ws_thing_tune(hass, conn, {"id": 2, "type": "sextant/thing/tune", "entity": "socks",
                                        "battery_entity": "binary_sensor.socks_low"}))
    assert conn.errors and st.get_layout(hass)["thing_battery_entity"] == {"socks": "sensor.great_room_holy_iot_sensors_socks_battery"}
    run(ws.ws_thing_tune(hass, conn, {"id": 3, "type": "sextant/thing/tune", "entity": "socks", "battery_entity": None}))
    assert st.get_layout(hass)["thing_battery_entity"] == {}


def test_history_can_be_kept_to_admins(tmp_path):
    layout = _layout()
    layout["tuning"] = {"history_admin_only": True}
    hass = _hass_with_layout(tmp_path, layout)
    guest, admin = _Conn(), _Conn()
    guest.user = types.SimpleNamespace(is_admin=False)
    admin.user = types.SimpleNamespace(is_admin=True)
    run(ws.ws_history_index(hass, guest, {"id": 1, "type": "sextant/history/index"}))
    assert guest.errors and "administrators" in guest.errors[0][2] and not guest.results
    run(ws.ws_history_index(hass, admin, {"id": 2, "type": "sextant/history/index"}))
    assert admin.results and not admin.errors
    # Off (the default): everyone signed in may read it.
    open_hass = _hass_with_layout(tmp_path / "open", _layout())
    anyone = _Conn()
    anyone.user = types.SimpleNamespace(is_admin=False)
    run(ws.ws_history_index(open_hass, anyone, {"id": 3, "type": "sextant/history/index"}))
    assert anyone.results and not anyone.errors

