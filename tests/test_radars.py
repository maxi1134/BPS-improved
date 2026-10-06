"""mmWave radars on the plan (radars.py, and the runtime in __init__).

The geometry and pairing are pure and tested directly; the cycle runs against
a stub Home Assistant (states, and the radar specs primed in the cache so no
registry is needed).
"""
import asyncio
import math
import time
import types

import sextant
from sextant import radars
from sextant import storage as st
from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def state(value, unit=None):
    return types.SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit} if unit else {})


# --- units and discovery ---------------------------------------------------------

def test_lengths_in_any_unit_become_metres():
    assert radars.to_metres("39.37", "in") == 39.37 * 0.0254
    assert radars.to_metres("1500", "mm") == 1.5
    assert radars.to_metres("600", "cm") == 6.0
    assert radars.to_metres("unknown", "in") is None
    assert radars.to_metres("5", "lx") is None, "not a length"


def test_a_device_with_target_coordinates_is_a_radar_and_a_plain_sensor_is_not():
    ids = ["sensor.ep_target_1_x", "sensor.ep_target_1_y", "sensor.ep_target_1_speed", "binary_sensor.ep_target_1_active",
           "sensor.ep_target_2_x", "sensor.ep_target_2_y", "sensor.ep_target_3_x",      # target 3 has no y: not usable
           "number.ep_tracking_detection_range", "number.ep_installation_angle", "sensor.ep_illuminance"]
    spec = radars.device_spec(ids)
    assert [t["x"] for t in spec["targets"]] == ["sensor.ep_target_1_x", "sensor.ep_target_2_x"]
    assert spec["targets"][0]["active"] == "binary_sensor.ep_target_1_active" and spec["targets"][1]["active"] is None
    assert spec["range_entity"] == "number.ep_tracking_detection_range" and spec["angle_entity"] == "number.ep_installation_angle"
    assert radars.device_spec(["binary_sensor.kitchen_occupancy", "sensor.kitchen_temperature"]) is None


# --- reading targets ---------------------------------------------------------------

SPEC = {"targets": [{"x": "sensor.t1x", "y": "sensor.t1y", "speed": "sensor.t1s", "active": "binary_sensor.t1a"},
                    {"x": "sensor.t2x", "y": "sensor.t2y", "speed": None, "active": None}],
        "range_entity": "number.range", "angle_entity": None}


def test_targets_are_read_in_metres_and_the_empty_ones_skipped():
    states = {"sensor.t1x": state(-20, "in"), "sensor.t1y": state(80, "in"), "sensor.t1s": state(0, "mph"),
              "binary_sensor.t1a": state("on"), "sensor.t2x": state(0, "in"), "sensor.t2y": state(0, "in"),
              "number.range": state(600, "cm")}
    got = radars.read_targets(SPEC, states.get)
    assert len(got) == 1, "target 2 at the origin is the radar's 'none'"
    i, x, y, speed = got[0]
    assert i == 1 and abs(x + 0.508) < 1e-9 and abs(y - 2.032) < 1e-9 and speed == 0.0
    states["binary_sensor.t1a"] = state("off")
    assert radars.read_targets(SPEC, states.get) == [], "inactive"
    states["binary_sensor.t1a"] = state("on")
    states["sensor.t1y"] = state(400, "in")      # 10 m: beyond a 6 m range
    assert radars.read_targets(SPEC, states.get) == []
    assert radars.radar_range_m(SPEC, states.get) == 6.0


# --- the plan ----------------------------------------------------------------------

def test_heading_turns_the_radar_frame_onto_the_plan():
    r = {"cords": {"x": 500.0, "y": 500.0}, "heading": 0.0}
    x, y = radars.to_plan(r, 0.0, 2.0, 100.0)
    assert (round(x), round(y)) == (500, 300), "facing up the plan: 2 m ahead is 200 px up"
    x, y = radars.to_plan({**r, "heading": 90.0}, 0.0, 2.0, 100.0)
    assert (round(x), round(y)) == (700, 500), "facing right"
    x, y = radars.to_plan(r, 1.0, 0.0, 100.0)
    assert (round(x), round(y)) == (600, 500), "positive x is to the right of the heading"
    x, y = radars.to_plan({**r, "flip": True}, 1.0, 0.0, 100.0)
    assert (round(x), round(y)) == (400, 500), "flipped: to the left"


def test_pairing_is_one_to_one_nearest_first_within_the_radius():
    targets = [("a", (0.0, 0.0)), ("b", (10.0, 0.0)), ("c", (50.0, 0.0))]
    things = [("phone", (1.0, 0.0)), ("watch", (2.0, 0.0)), ("cat", (11.0, 0.0))]
    pairs, free = radars.pair(targets, things, radius=5.0)
    assert pairs == {"a": "phone", "b": "cat"} and free == ["c"]


# --- the cycle ----------------------------------------------------------------------

def _hass(tmp_path, target_xy_in=(0.0, 39.37), active="on", speed_mph=0.0):
    hass = make_hass(tmp_path)
    layout = {"floor": [{"name": "Office Floor", "scale": 100.0, "receivers": [], "subzones": [],
                         "zones": [{"entity_id": "Office", "poly": True, "cords": [{"x": 0, "y": 0}, {"x": 1000, "y": 0}, {"x": 1000, "y": 1000}, {"x": 0, "y": 1000}]}],
                         "radars": [{"radar_id": "r1", "device_id": "dev1", "cords": {"x": 500.0, "y": 900.0}, "heading": 0.0}]}],
              "tuning": {}}
    run(st.save_layout(hass, layout))
    states = {"sensor.t1x": state(target_xy_in[0], "in"), "sensor.t1y": state(target_xy_in[1], "in"),
              "sensor.t1s": state(speed_mph, "mph"), "binary_sensor.t1a": state(active)}
    hass.states = types.SimpleNamespace(get=states.get)
    sextant._radar_specs_cache.update(at=time.time(), specs={"dev1": {**SPEC, "range_entity": None, "name": "Office Presence"}})
    sextant._radar_frame.update(t=0.0, targets=[], claims={})
    sextant._radar_still.clear(); sextant._radar_last_pin.clear()
    sextant.apitricords = []
    return hass, states


def _thing(ent, raw, speed=0.0, robot=None, at=None):
    row = {"ent": ent, "floor": "Office Floor", "raw": list(raw), "cords": list(raw), "updated": at or time.time(), "speed": speed}
    if robot:
        row["robot"] = robot
    return row


def test_a_thing_near_a_target_claims_it_and_is_placed_on_it(tmp_path):
    hass, _ = _hass(tmp_path)                     # target 1 m ahead of the radar: (500, 800)
    sextant.apitricords = [_thing("phone", (560, 830)), _thing("watch", (900, 200))]
    run(sextant._radar_cycle(hass, st.get_layout(hass)))
    frame = sextant._radar_frame
    assert frame["targets"][0]["cords"] == [500.0, 800.0] and frame["targets"][0]["thing"] == "phone"
    claim = sextant._radar_claim("phone", "Office Floor", st.get_layout(hass))
    assert claim["cords"] == [500.0, 800.0]
    assert sextant._radar_claim("watch", "Office Floor", st.get_layout(hass)) is None
    assert sextant._radar_claim("phone", "Another Floor", st.get_layout(hass)) is None
    layout = st.get_layout(hass); layout["tuning"]["mmwave_fusion"] = False
    assert sextant._radar_claim("phone", "Office Floor", layout) is None, "fusion off"


def test_a_target_nobody_claims_is_counted_as_untracked(tmp_path):
    hass, _ = _hass(tmp_path)
    sextant.apitricords = [_thing("watch", (900, 200))]           # 7 m away: no claim
    written = {}
    orig = sextant.update_sextant_sensor_state
    sextant.update_sextant_sensor_state = lambda h, e, s, a=None: written.__setitem__(e, (s, a))
    try:
        run(sextant._radar_cycle(hass, st.get_layout(hass)))
    finally:
        sextant.update_sextant_sensor_state = orig
    count, attrs = written[sextant.UNTRACKED_ENTITY_ID]
    assert count == 1 and attrs["rooms"] == ["Office"] and attrs["targets"][0]["radar"] == "Office Presence"


def test_a_robot_claims_its_target_but_is_not_moved_by_it(tmp_path):
    hass, _ = _hass(tmp_path)
    sextant.apitricords = [_thing("vacuum_rocky", (500, 810), robot="vacuum.rocky")]
    run(sextant._radar_cycle(hass, st.get_layout(hass)))
    assert sextant._radar_frame["targets"][0]["thing"] == "vacuum_rocky", "not an untracked person"
    assert sextant._radar_claim("vacuum_rocky", "Office Floor", st.get_layout(hass)) is None


def _cycle(hass, layout, at):
    """One cycle at time ``at``, the phone having been heard in it (as it is
    every real cycle)."""
    sextant.apitricords = [_thing("phone", (520, 810), at=at)]
    sextant._radar_specs_cache["at"] = at      # the stub has no registry to refresh the radar list from
    run(sextant._radar_cycle(hass, layout, now=at))


def test_one_still_target_and_one_still_thing_become_a_pin_once(tmp_path, monkeypatch):
    hass, _ = _hass(tmp_path)
    samples = [{"t": time.time() - 10 * i, "floors": {"Office Floor": {}}} for i in range(5)]
    monkeypatch.setattr(sextant._truth_buffer, "samples", lambda ent, since=None, floor=None: samples if ent == "phone" else [])
    layout = st.get_layout(hass)
    t0 = time.time()
    _cycle(hass, layout, t0)                                           # starts the still clock
    _cycle(hass, layout, t0 + 30)                                      # not long enough
    assert run(st.load_truth(hass)).get("marks", []) == []
    _cycle(hass, layout, t0 + 61)
    marks = run(st.load_truth(hass))["marks"]
    assert len(marks) == 1 and marks[0]["entity"] == "phone" and marks[0]["source"] == "mmwave:Office Presence"
    assert (marks[0]["x"], marks[0]["y"]) == (500.0, 800.0)
    _cycle(hass, layout, t0 + 200)                                     # within the half hour
    assert len(run(st.load_truth(hass))["marks"]) == 1


def test_a_moving_target_makes_no_pin(tmp_path, monkeypatch):
    hass, states = _hass(tmp_path, speed_mph=2.0)                      # the target is walking
    monkeypatch.setattr(sextant._truth_buffer, "samples", lambda ent, since=None, floor=None: [{"t": 0, "floors": {}}] * 5)
    t0 = time.time()
    for dt in (0, 70, 140):
        _cycle(hass, st.get_layout(hass), t0 + dt)
    assert run(st.load_truth(hass)).get("marks", []) == []


# --- mounting height and the room limit -------------------------------------------

def test_the_mounting_height_turns_the_slant_into_floor_distance():
    assert radars.floor_factor(0.0, 3.0, None) == 1.0, "unset: as reported"
    assert radars.floor_factor(0.0, 3.0, 0.9) == 1.0, "no higher than a chest"
    k = radars.floor_factor(0.0, 3.1623, 2.0)                    # 2 m up, 1 m above a chest
    assert abs(3.1623 * k - 3.0) < 1e-3
    assert radars.floor_factor(0.0, 0.8, 2.0) == 0.0, "closer than the height difference: under the sensor"
    x, y = radars.to_plan({"cords": {"x": 0.0, "y": 0.0}, "heading": 180.0, "height_m": 2.0}, 0.0, 3.1623, 100.0)
    assert (round(x), round(y)) == (0, 300), "facing down the plan, 3 m out across the floor"
    assert abs(radars.floor_range_m(3.5, 2.0) - 3.354) < 1e-3


def _two_rooms(tmp_path, room=None, target_in=(0.0, 196.85)):
    """An Office above a Hall; the radar is in the Hall at (500, 900) facing
    up, and its target is ``target_in`` inches ahead (5 m: in the Office)."""
    hass, states = _hass(tmp_path, target_xy_in=target_in)
    layout = st.get_layout(hass)
    fl = layout["floor"][0]
    fl["zones"] = [
        {"entity_id": "Office", "poly": True, "cords": [{"x": 0, "y": 0}, {"x": 1000, "y": 0}, {"x": 1000, "y": 500}, {"x": 0, "y": 500}]},
        {"entity_id": "Hall", "poly": True, "cords": [{"x": 0, "y": 500}, {"x": 1000, "y": 500}, {"x": 1000, "y": 1000}, {"x": 0, "y": 1000}]},
    ]
    if room is not None:
        fl["radars"][0]["room"] = room
    run(st.save_layout(hass, layout))
    return hass


def _untracked(hass):
    written = {}
    orig = sextant.update_sextant_sensor_state
    sextant.update_sextant_sensor_state = lambda h, e, s, a=None: written.__setitem__(e, (s, a))
    try:
        run(sextant._radar_cycle(hass, st.get_layout(hass)))
    finally:
        sextant.update_sextant_sensor_state = orig
    return written[sextant.UNTRACKED_ENTITY_ID][0]


def test_a_target_through_the_wall_is_not_counted(tmp_path):
    hass = _two_rooms(tmp_path)
    assert _untracked(hass) == 0, "the radar is in the Hall; the target is in the Office"
    assert sextant._radar_frame["targets"] == []


def test_a_target_in_its_own_room_is_counted(tmp_path):
    hass = _two_rooms(tmp_path, target_in=(0.0, 78.74))            # 2 m ahead: still the Hall
    assert _untracked(hass) == 1


def test_the_limit_can_be_another_room_or_none(tmp_path):
    assert _untracked(_two_rooms(tmp_path, room="*")) == 1
    assert _untracked(_two_rooms(tmp_path, room="Office")) == 1
    assert _untracked(_two_rooms(tmp_path, room="Hall")) == 0


def test_a_sensor_on_the_wall_belongs_to_the_room_beside_it(tmp_path):
    hass = _two_rooms(tmp_path)
    layout = st.get_layout(hass)
    fl = layout["floor"][0]
    radar = {**fl["radars"][0], "cords": {"x": 500.0, "y": 1030.0}}  # 0.3 m outside the Hall's bottom wall
    assert sextant._radar_room(hass, layout, fl, radar)[0] == "Hall"
    far = {**radar, "cords": {"x": 500.0, "y": 1100.0}}              # 1 m out: no room
    assert sextant._radar_room(hass, layout, fl, far) == (None, None)


def test_an_object_on_the_desk_does_not_claim_the_person_at_it(tmp_path):
    hass, _ = _hass(tmp_path)                     # target 1 m ahead of the radar: (500, 800)
    layout = st.get_layout(hass)
    layout["thing_classes"] = {"airpods_case": "headphones", "tile": "tag", "phone": "phone"}
    run(st.save_layout(hass, layout))
    sextant.apitricords = [_thing("airpods_case", (505, 805)), _thing("tile", (495, 810)), _thing("phone", (560, 880))]
    run(sextant._radar_cycle(hass, st.get_layout(hass)))
    assert sextant._radar_frame["targets"][0]["thing"] == "phone", "the phone, not the nearer case or tag"
    sextant.apitricords = [_thing("airpods_case", (505, 805))]
    run(sextant._radar_cycle(hass, st.get_layout(hass)))
    assert sextant._radar_frame["targets"][0]["thing"] is None, "a case alone leaves the person untracked"
    sextant.apitricords = [_thing("unclassed", (505, 805))]
    run(sextant._radar_cycle(hass, st.get_layout(hass)))
    assert sextant._radar_frame["targets"][0]["thing"] == "unclassed", "no class: usually a phone nobody classed"
