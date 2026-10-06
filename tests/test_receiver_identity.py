"""Receivers identified by scanner address: resolution, relabel on rename,
address-keyed readings, liveness and calibration matching."""

import asyncio
import copy

import sextant
from sextant import bermuda_source
from sextant import calibration as cal_mod
from sextant import storage as st
from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


DIRECTORY = {
    "dc:06:75:4e:89:4a": {"slug": "eilee_bedroom_rrn00_4e8948", "name": "Eilee Bedroom RRN00 4e8948",
                          "unique_id": "dc:06:75:4e:89:48", "address_wifi_mac": "dc:06:75:4e:89:48", "last_seen_age": 2.0},
    "cc:8d:a2:dd:31:a2": {"slug": "eilee_bedroom_rrn00_dd31a0", "name": "Eilee Bedroom RRN00 dd31a0",
                          "unique_id": "cc:8d:a2:dd:31:a0", "address_wifi_mac": "cc:8d:a2:dd:31:a0", "last_seen_age": 400.0},
    "e4:b3:23:6d:84:4a": {"slug": "foyer_s2224_6d8448_renamed", "name": "Foyer s2224 6d8448 renamed",
                          "unique_id": "e4:b3:23:6d:84:48", "address_wifi_mac": "e4:b3:23:6d:84:48", "last_seen_age": 1.0},
}


def _layout():
    return {"floor": [{"name": "F", "scale": 100.0, "zones": [], "subzones": [], "receivers": [
        {"entity_id": "eilee_bedroom_rrn00_4e8948", "cords": {"x": 0, "y": 0}},           # exact slug
        {"entity_id": "eilee_bedroom_rrn00_dd31a0", "cords": {"x": 1, "y": 0}, "address": "CC:8D:A2:DD:31:A2"},  # stored, upper-case
        {"entity_id": "foyer_s2224_6d8448", "cords": {"x": 2, "y": 0}},                   # renamed in Bermuda: token match
        {"entity_id": "ghost_rrn00_000000", "cords": {"x": 3, "y": 0}},                   # gone
    ]}]}


def test_resolver_assigns_addresses_and_relabels_renamed_receivers():
    layout = _layout()
    changed, unresolved = sextant._resolve_receiver_addresses(layout, DIRECTORY)
    recs = {r["cords"]["x"]: r for r in layout["floor"][0]["receivers"]}
    assert changed is True
    assert recs[0]["address"] == "dc:06:75:4e:89:4a"
    assert recs[1]["address"] == "cc:8d:a2:dd:31:a2"                     # normalised
    assert recs[2]["address"] == "e4:b3:23:6d:84:4a"
    assert recs[2]["entity_id"] == "foyer_s2224_6d8448_renamed"           # label follows the rename
    assert "address" not in recs[3] and unresolved == ["ghost_rrn00_000000"]
    # Idempotent: a second pass changes nothing.
    assert sextant._resolve_receiver_addresses(layout, DIRECTORY) == (False, ["ghost_rrn00_000000"])


def test_resolver_never_assigns_one_scanner_to_two_placements():
    layout = {"floor": [{"name": "F", "scale": 100.0, "receivers": [
        {"entity_id": "eilee_bedroom_rrn00_4e8948", "cords": {"x": 0, "y": 0}},
        {"entity_id": "old_name_rrn00_4e8948", "cords": {"x": 1, "y": 0}},   # same token, duplicate placement
    ]}]}
    sextant._resolve_receiver_addresses(layout, DIRECTORY)
    addrs = [r.get("address") for r in layout["floor"][0]["receivers"]]
    assert addrs == ["dc:06:75:4e:89:4a", None]


def test_async_resolver_persists_only_when_something_changed(monkeypatch, tmp_path):
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, _layout()))
    monkeypatch.setattr(bermuda_source, "async_get_scanner_directory", lambda h: DIRECTORY)
    saves_before = len(hass._store_saves)
    assert run(sextant.async_resolve_receiver_addresses(hass)) is True
    assert len(hass._store_saves) == saves_before + 1
    assert st.get_layout(hass)["floor"][0]["receivers"][0]["address"] == "dc:06:75:4e:89:4a"
    assert run(sextant.async_resolve_receiver_addresses(hass)) is False
    assert len(hass._store_saves) == saves_before + 1
    monkeypatch.setattr(bermuda_source, "async_get_scanner_directory", lambda h: None)
    assert run(sextant.async_resolve_receiver_addresses(hass)) is False


def test_readings_by_address_need_no_slug_map(monkeypatch):
    from test_bermuda_source import _install_featureful_api, _snapshot, _hass_with_data
    api = _install_featureful_api(monkeypatch, _snapshot(distance=2.3, age=1.0))
    readings = bermuda_source.async_get_readings_by_address(_hass_with_data())
    assert readings[("phone", "11:22:33:44:55:66")] == {"distance": 2.3, "age": 1.0}
    assert api.calls[-1] == {"tracked_only": True}


def test_radii_use_the_address_when_the_slug_has_drifted(monkeypatch):
    import types
    hass = make_hass()
    hass.states = types.SimpleNamespace(get=lambda eid: None)  # entity fallback finds nothing
    monkeypatch.setattr(bermuda_source, "async_get_readings", lambda h, include_history=False: {})
    monkeypatch.setattr(bermuda_source, "async_get_readings_by_address",
                        lambda h, include_history=False: {("e", "dc:06:75:4e:89:4a"): {"distance": 3.0, "age": 1.0}})
    data = {"floor": [{"name": "F", "scale": 100.0, "receivers": [
        {"entity_id": "stale_label", "address": "DC:06:75:4E:89:4A", "cords": {"x": 0, "y": 0}},
        {"entity_id": "unresolved", "cords": {"x": 1, "y": 0}},
    ]}]}
    run(sextant.update_receiver_radii(hass, {"entity": "e", "data": data}))
    r0, r1 = data["floor"][0]["receivers"]
    assert r0["distance"] == 3.0 and r0["cords"]["r"] == 300.0
    assert "distance" not in r1


def test_diagnostics_do_not_flag_a_placement_identified_by_address(monkeypatch):
    import json
    hass = make_hass()
    monkeypatch.setattr(bermuda_source, "async_get_scanner_directory", lambda h: DIRECTORY)
    monkeypatch.setattr(sextant, "_scanner_slugs_and_readings", lambda h: ({"eilee_bedroom_rrn00_dd31a0"}, set()))
    layout = {"floor": [{"name": "F", "receivers": [
        {"entity_id": "old_label", "address": "dc:06:75:4e:89:4a"},   # linked by address, label stale
        {"entity_id": "ghost_rrn00_000000"},                            # genuinely unmatched
    ]}]}
    diag = sextant._scanner_diagnostics(hass, json.dumps(layout))
    assert [u["entity_id"] for u in diag["unmatched_receivers"]] == ["ghost_rrn00_000000"]


def test_calibration_matches_scanners_by_address_first():
    cal = {"receivers": {
        "kitchen_label": {"address": "aa:aa:aa:aa:aa:01", "floor": "F", "x": 0, "y": 0, "scale": 100.0},
        "office_rrn00_aaaa02": {"address": None, "floor": "F", "x": 1, "y": 0, "scale": 100.0},
    }, "all_placed_slugs": {"kitchen_label", "office_rrn00_aaaa02"}}
    devices = {
        "aa:aa:aa:aa:aa:01": {"_is_scanner": True, "address": "aa:aa:aa:aa:aa:01", "name": "Kitchen Renamed Probe"},
        "aa:aa:aa:aa:aa:02": {"_is_scanner": True, "address": "aa:aa:aa:aa:aa:02", "name": "Office RRN00 aaaa02"},
    }
    mapping = cal_mod._match_scanners(cal, devices)
    assert mapping == {"aa:aa:aa:aa:aa:01": "kitchen_label", "aa:aa:aa:aa:aa:02": "office_rrn00_aaaa02"}
    assert set(cal["matched_placed"]) == {"kitchen_label", "office_rrn00_aaaa02"}


def test_resolver_handles_bermudas_mac_suffixed_slugs():
    """Bermuda appends the MAC to a duplicate scanner name, and drops it again
    once the duplicate is gone. Both forms must resolve to the address."""
    directory = {"dc:06:75:4e:89:3e": {"slug": "sewing_room_rrn00_4e893c", "name": "Sewing Room RRN00 4e893c",
                                        "unique_id": "dc:06:75:4e:89:3c", "address_wifi_mac": "dc:06:75:4e:89:3c",
                                        "last_seen_age": 1.0}}
    layout = {"floor": [{"name": "F", "receivers": [
        {"entity_id": "sewing_room_rrn00_4e893c_dc_06_75_4e_89_3e", "cords": {"x": 0, "y": 0}},
    ]}]}
    changed, unresolved = sextant._resolve_receiver_addresses(layout, directory)
    rec = layout["floor"][0]["receivers"][0]
    assert changed and unresolved == []
    assert rec["address"] == "dc:06:75:4e:89:3e"
    assert rec["entity_id"] == "sewing_room_rrn00_4e893c"       # label follows the un-suffixed name
    # And the other way round: the directory has the suffixed name, the
    # placement the plain one.
    directory["dc:06:75:4e:89:3e"]["slug"] = "sewing_room_rrn00_4e893c_dc_06_75_4e_89_3e"
    layout = {"floor": [{"name": "F", "receivers": [{"entity_id": "sewing_room_rrn00_4e893c", "cords": {"x": 0, "y": 0}}]}]}
    assert sextant._resolve_receiver_addresses(layout, directory)[0]
    assert layout["floor"][0]["receivers"][0]["address"] == "dc:06:75:4e:89:3e"
