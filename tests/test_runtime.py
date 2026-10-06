"""Keeping a thing's state across a restart (runtime.py)."""
import math

import numpy as np
import pytest

from sextant import runtime


def _kf(ts=1000.0, floor="Ground Floor"):
    return {"x": np.array([1.0, 2.0, 0.1, -0.2]), "P": np.diag([1.0, 1.0, 4.0, 4.0]), "ts": ts, "floor": floor}


def _rows():
    return [{"ent": "phone", "zone": "Kitchen", "sub_zone": "Peninsula", "floor": "Ground Floor",
             "updated": 999.0, "cords": [123.4, 567.8]}]


def test_a_short_restart_brings_everything_back():
    now = 1000.0
    snap = runtime.snapshot(
        now,
        kf={"phone": _kf()},
        zones={"phone": {"floor": "Ground Floor", "zone": "Kitchen", "probs": {"Kitchen": 0.9}, "locked": True, "born": 940.0}},
        spots={"phone": {"floor": "Ground Floor", "zone": "Kitchen", "value": ["Peninsula", "Kitchen"], "probs": {"Peninsula": 0.8}}},
        arrivals={"phone": {"floor": "Ground Floor", "x": 1.2, "y": 3.4, "since": 800.0, "provisional": False}},
        rows=_rows(),
    )
    back = runtime.restore(snap, now + 30)          # half a minute down
    assert back["age"] == 30
    assert back["kf"]["phone"]["x"] == [1.0, 2.0, 0.1, -0.2]
    assert back["kf"]["phone"]["P"][2][2] == 4.0
    assert back["zone"]["phone"]["locked"] is True and back["zone"]["phone"]["born"] == 940.0
    assert back["spot"]["phone"]["value"] == ["Peninsula", "Kitchen"]
    assert back["arrivals"]["phone"]["since"] == 800.0      # it did not just arrive
    assert back["last"]["phone"]["zone"] == "Kitchen"


def test_a_long_gap_keeps_only_the_last_sighting():
    """An hour later the house has moved on: the elections are meaningless,
    but where a thing was last seen is exactly what "away since" needs."""
    snap = runtime.snapshot(1000.0, kf={"phone": _kf()}, zones={"phone": {"zone": "Kitchen"}},
                            spots={"phone": {"value": ["Peninsula", "Kitchen"]}},
                            arrivals={"phone": {"since": 800.0}}, rows=_rows())
    back = runtime.restore(snap, 1000.0 + 3600)
    assert back["kf"] == {} and back["zone"] == {} and back["spot"] == {} and back["arrivals"] == {}
    assert back["last"]["phone"] == {"zone": "Kitchen", "spot": "Peninsula", "floor": "Ground Floor",
                                     "updated": 999.0, "cords": [123.4, 567.8]}


def test_a_clock_that_moved_backwards_is_not_fresh():
    snap = runtime.snapshot(5000.0, kf={"phone": _kf()}, rows=_rows())
    back = runtime.restore(snap, 1000.0)
    assert back["kf"] == {} and back["last"]["phone"]["updated"] == 999.0


@pytest.mark.parametrize("data", [None, {}, [], {"saved_at": "soon", "things": {}}, {"saved_at": 1.0},
                                  {"saved_at": float("nan"), "things": {}}, {"things": {}}])
def test_rubbish_restores_as_nothing(data):
    back = runtime.restore(data, 1000.0)
    assert back["kf"] == {} and back["last"] == {}


def test_broken_entries_are_dropped_not_fatal():
    snap = {"saved_at": 1000.0, "things": {
        "short_vector": {"kf": {"x": [1.0, 2.0], "P": [[1.0]], "ts": 1.0, "floor": "F"}},
        "no_floor": {"kf": {"x": [1.0] * 4, "P": [[1.0] * 4] * 4, "ts": 1.0}},
        "not_a_dict": "nonsense",
        "empty_zone": {"zone": {}},
        "good": {"kf": {"x": [1.0] * 4, "P": [[1.0] * 4] * 4, "ts": 1.0, "floor": "F"}, "zone": {"zone": "Kitchen"}},
    }}
    back = runtime.restore(snap, 1000.0)
    assert list(back["kf"]) == ["good"] and list(back["zone"]) == ["good"]


def test_nothing_unwritable_reaches_the_store():
    """A store that cannot be serialised is a restart that loses everything,
    so numpy is flattened and anything else is dropped."""
    class Odd:
        pass

    snap = runtime.snapshot(1000.0, kf={"phone": _kf()},
                            zones={"phone": {"probs": {"Kitchen": np.float64(0.5)}, "odd": Odd(),
                                             "nan": float("nan"), "deep": {"a": {"b": {"c": {"d": 1}}}}}},
                            rows=_rows())
    import json
    text = json.dumps(snap)          # must not raise
    assert "Odd" not in text and "NaN" not in text
    assert snap["things"]["phone"]["zone"]["probs"]["Kitchen"] == 0.5
    assert "odd" not in snap["things"]["phone"]["zone"]
    assert "nan" not in snap["things"]["phone"]["zone"]


def test_a_none_is_a_value_not_a_missing_key():
    """"No challenger", "not moving since" and "nothing pending" are Nones the
    elections index by name. Dropping those keys was a restore that raised on
    the first cycle and threw every thing back to a cold start."""
    snap = runtime.snapshot(1000.0, zones={"phone": {"zone": "Kitchen", "challenge": None,
                                                     "still_since": None, "moving_since": None,
                                                     "away_since": None, "outvoted_since": None}},
                            spots={"phone": {"value": ["Peninsula", "Kitchen"], "pending": None}})
    back = runtime.restore(snap, 1000.0 + 10)
    assert back["zone"]["phone"]["challenge"] is None
    assert back["zone"]["phone"]["still_since"] is None
    assert back["spot"]["phone"]["pending"] is None
    assert set(back["zone"]["phone"]) == {"zone", "challenge", "still_since", "moving_since",
                                          "away_since", "outvoted_since"}


def test_the_floor_election_comes_back_so_the_floor_is_not_new():
    """A thing whose floor the first cycle elects from scratch reads as having
    CHANGED floor, and a floor change clears the filter and the room and spot
    elections - the restore undone a second after it ran."""
    import json

    import sextant

    sextant._floor_since.clear(); sextant._floor_probability.clear()
    sextant._thing_floors().clear()
    sextant._thing_floors()["watch"] = "Second Floor"
    sextant._floor_since["watch"] = 4100.0
    sextant._floor_probability["watch"] = {"Second Floor": 0.96, "Ground Floor": 0.04}

    saved = json.loads(json.dumps(runtime.snapshot(5000.0, floors=sextant._floor_elections())))
    back = runtime.restore(saved, 5030.0)

    sextant._floor_since.clear(); sextant._floor_probability.clear()
    sextant._thing_floors().clear()
    sextant._restore_floor_elections(back["floors"])

    assert sextant._thing_floors()["watch"] == "Second Floor"      # not a floor change
    assert sextant._floor_since["watch"] == 4100.0                 # keeps its tenure
    assert sextant._floor_probability["watch"]["Second Floor"] == 0.96


def test_a_thing_with_no_sighting_still_keeps_its_election():
    snap = runtime.snapshot(1000.0, zones={"watch": {"zone": "Office"}})
    back = runtime.restore(snap, 1000.0 + 10)
    assert back["zone"]["watch"]["zone"] == "Office" and back["last"] == {}


def test_the_default_gap_is_five_minutes():
    assert runtime.DEFAULT_MAX_AGE_SECS == 300.0
    snap = runtime.snapshot(1000.0, zones={"watch": {"zone": "Office"}})
    assert runtime.restore(snap, 1000.0 + 299)["zone"] != {}
    assert runtime.restore(snap, 1000.0 + 301)["zone"] == {}
    assert math.isclose(runtime.restore(snap, 1000.0 + 301)["age"], 301)


def test_the_live_dicts_go_out_and_come_back(monkeypatch):
    """The round trip that matters: the module's own state, through JSON,
    into the module's own state - a restart without the cold start."""
    import json

    import sextant

    sextant._kf_position_state.clear(); sextant._zone_state.clear()
    sextant._subzone_state.clear(); sextant._arrivals.clear(); sextant._last_seen.clear()
    now = 5000.0
    sextant._kf_position_state["watch"] = _kf(ts=now - 5, floor="Second Floor")
    sextant._zone_state["watch"] = {"floor": "Second Floor", "zone": "Master Bedroom", "since": now - 900,
                                    "probs": {"Master Bedroom": 0.94}, "locked": True, "born": now - 900,
                                    "challenge": None, "still_since": now - 800}
    sextant._subzone_state["watch"] = {"floor": "Second Floor", "zone": "Master Bedroom",
                                       "value": ("David Bedside Table", "Master Bedroom"),
                                       "probs": {"David Bedside Table": 0.7}, "pending": None}
    sextant._arrivals["watch"] = {"floor": "Second Floor", "x": 5.3, "y": 6.2, "since": now - 40000,
                                  "provisional": False, "first_seen": now - 40000, "away_since": None}
    rows = [{"ent": "watch", "zone": "Master Bedroom", "sub_zone": "David Bedside Table",
             "floor": "Second Floor", "updated": now - 5, "cords": [540.0, 632.0]}]

    saved = json.loads(json.dumps(sextant.runtime_mod.snapshot(
        now, kf=sextant._kf_position_state, zones=sextant._zone_state,
        spots=sextant._subzone_state, arrivals=sextant._arrivals, rows=rows)))

    # Down for twenty seconds, and restarted: the dicts are empty again.
    sextant._kf_position_state.clear(); sextant._zone_state.clear()
    sextant._subzone_state.clear(); sextant._arrivals.clear()
    back = sextant.runtime_mod.restore(saved, now + 20)
    for entity, kf in back["kf"].items():
        sextant._kf_position_state[entity] = {"x": np.array(kf["x"]), "P": np.array(kf["P"]),
                                              "ts": kf["ts"], "floor": kf["floor"]}
    for entity, state in back["zone"].items():
        sextant._zone_state[entity] = {**sextant._new_zone_state(state.get("floor"), now), **state}
    for entity, state in back["spot"].items():
        value = state.get("value")
        state = {**state, "value": tuple(value) if isinstance(value, list) else value}
        sextant._subzone_state[entity] = {**sextant._new_subzone_state(state.get("floor"), state.get("zone"), now), **state}
    sextant._arrivals.update(back["arrivals"])

    # Every key the elections index by name, or the first cycle raises.
    assert set(sextant._zone_state["watch"]) >= set(sextant._new_zone_state("Second Floor", now))
    assert set(sextant._subzone_state["watch"]) >= set(sextant._new_subzone_state("Second Floor", "Master Bedroom", now))
    assert sextant._zone_state["watch"]["zone"] == "Master Bedroom"
    assert sextant._zone_state["watch"]["locked"] is True
    # The spot election compares this against its own answer, so it has to be
    # the tuple it was, not the list JSON made of it.
    assert sextant._subzone_state["watch"]["value"] == ("David Bedside Table", "Master Bedroom")
    # It has been on the table for eleven hours; it did not just arrive.
    assert now - sextant._arrivals["watch"]["since"] > 39000
    assert sextant._kf_position_state["watch"]["x"].tolist() == [1.0, 2.0, 0.1, -0.2]
    assert back["last"]["watch"]["updated"] == now - 5


def test_a_quiet_thing_keeps_its_last_sighting_through_the_snapshot():
    """Socks, 2026-09-21: his tag battery ran down, he went quiet at 5:42 AM
    and was pruned out of the published rows as stale. The snapshot built its
    record of last sightings from those rows alone, so the 15:34 restart lost
    him completely and the Live page said he was last heard 20,717 days ago."""
    import sextant

    rows = [{"ent": "meg", "zone": "Catwalk", "sub_zone": "Meg's Cafe", "floor": "Second Floor",
             "updated": 5000.0, "cords": [1.0, 2.0]}]
    last_seen = {
        "meg": {"zone": "Catwalk", "spot": "Meg's Cafe", "floor": "Second Floor", "updated": 5000.0, "cords": [1.0, 2.0]},
        "socks": {"zone": "Dining Room", "spot": None, "floor": "Ground Floor", "updated": 1000.0, "cords": [5.0, 6.0]},
    }
    merged = sextant._rows_with_last_seen(rows, last_seen)
    assert [r["ent"] for r in merged] == ["meg", "socks"]          # the live row is not duplicated

    back = runtime.restore(runtime.snapshot(5000.0, rows=merged), 5000.0 + 3600)
    assert back["last"]["socks"]["zone"] == "Dining Room"          # an hour down: only sightings kept...
    assert back["last"]["socks"]["updated"] == 1000.0               # ...and the quiet one is among them


def test_the_history_fills_in_a_last_sighting_once_it_has_loaded():
    """The restore asked the history before the history was read off disk, so
    this fallback never fired on a real restart. It now runs again after the
    load - this is that second call, with the room and floor recovered too."""
    import sextant
    from sextant import history as history_mod

    hist = history_mod.PositionHistory(history_mod.history_config({}))
    hist.record("socks", 1000.0, 3.0, 4.0, "Ground Floor", 100.0, zone="Dining Room")
    hist.record("socks", 1060.0, 3.1, 4.0, "Ground Floor", 100.0, zone="Dining Room")

    class Hass:
        data = {sextant.DOMAIN: {"_history": hist}}

    sextant._last_seen.clear()
    sextant._seed_last_seen_from_history(Hass())
    seen = sextant._last_seen["socks"]
    assert seen["updated"] == 1060.0
    assert seen["floor"] == "Ground Floor" and seen["zone"] == "Dining Room"

    # Something already remembered is never overwritten by the history's guess.
    sextant._last_seen["socks"] = {"zone": "Kitchen", "spot": None, "floor": "Ground Floor", "updated": 2000.0, "cords": None}
    sextant._seed_last_seen_from_history(Hass())
    assert sextant._last_seen["socks"]["zone"] == "Kitchen"
