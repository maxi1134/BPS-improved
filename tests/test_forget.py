"""sextant/thing/forget: a thing whose identity has gone stops haunting the list.

Two zombie Tiles and a pre-IRK-swap phone lived on as "away" rows for ever,
held only by their entries in the layout's thing_* maps (the Live page lists
every key of thing_classes), with nothing in the UI able to remove them.
"""
import asyncio

import sextant
from sextant import ws
from sextant import storage as st
from sextant import bermuda_source
from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Conn:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))


def _hass(tmp_path, monkeypatch, tracked_slugs):
    hass = make_hass(tmp_path)
    layout = {
        "floor": [{"name": "F", "scale": 100.0, "receivers": [], "zones": [], "subzones": []}],
        "thing_names": {"tile_dead": "Office Tile", "tile_live": "Office Tile"},
        "thing_classes": {"tile_dead": "tag", "tile_live": "tag", "old_phone": "phone"},
        "thing_owners": {"old_phone": "person.david"},
    }
    run(st.save_layout(hass, layout))
    monkeypatch.setattr(bermuda_source, "async_get_tracked_devices",
                        lambda h: {f"aa:{i}": {"slug": s} for i, s in enumerate(tracked_slugs)})

    async def _noop(h):
        return None
    monkeypatch.setattr(sextant, "_save_runtime", _noop)
    sextant._last_seen["tile_dead"] = {"zone": "Office", "updated": 1.0}
    sextant._last_seen["tile_live"] = {"zone": "Office", "updated": 2.0}
    sextant._zone_state["tile_dead"] = {"zone": "Office"}
    return hass


def _forget(hass, ent):
    conn = _Conn()
    run(ws.ws_thing_forget(hass, conn, {"id": 1, "type": "sextant/thing/forget", "entity": ent}))
    assert not conn.errors, conn.errors
    return conn.results[0][1]


def test_a_dead_identity_is_purged_from_every_thing_map(tmp_path, monkeypatch):
    hass = _hass(tmp_path, monkeypatch, tracked_slugs=["tile_live"])
    r = _forget(hass, "tile_dead")
    assert r["tracked"] is False
    assert sorted(r["settings_dropped"]) == ["thing_classes", "thing_names"]
    lay = st.get_layout(hass)
    assert "tile_dead" not in lay["thing_names"] and "tile_dead" not in lay["thing_classes"]
    assert lay["thing_names"]["tile_live"] == "Office Tile", "the live twin keeps its name"
    assert "tile_dead" not in sextant._last_seen and "tile_dead" not in sextant._zone_state


def test_a_tracked_thing_that_is_only_away_keeps_its_settings(tmp_path, monkeypatch):
    hass = _hass(tmp_path, monkeypatch, tracked_slugs=["tile_live"])
    r = _forget(hass, "tile_live")
    assert r["tracked"] is True
    assert r["settings_dropped"] == []
    lay = st.get_layout(hass)
    assert lay["thing_names"]["tile_live"] == "Office Tile"
    assert "tile_live" not in sextant._last_seen, "but its remembered sighting is gone"


def test_the_pre_swap_phone_goes_from_owners_too(tmp_path, monkeypatch):
    hass = _hass(tmp_path, monkeypatch, tracked_slugs=[])
    r = _forget(hass, "old_phone")
    assert sorted(r["settings_dropped"]) == ["thing_classes", "thing_owners"]
    lay = st.get_layout(hass)
    assert "old_phone" not in lay["thing_owners"]


def test_forgetting_is_a_save_so_it_leaves_a_snapshot(tmp_path, monkeypatch):
    from sextant import snapshots
    hass = _hass(tmp_path, monkeypatch, tracked_slugs=[])
    _forget(hass, "tile_dead")
    assert run(snapshots.listing(hass)), "the plan before the forget is kept"


def test_an_empty_name_is_refused(tmp_path, monkeypatch):
    hass = _hass(tmp_path, monkeypatch, tracked_slugs=[])
    conn = _Conn()
    run(ws.ws_thing_forget(hass, conn, {"id": 1, "type": "sextant/thing/forget", "entity": "  "}))
    assert conn.errors
