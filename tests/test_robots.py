"""Robot vacuums on the plan (robots.py, and the runtime in __init__).

The fit is tested on maps made from a known transform, and on the real maps
of the house it was built for: Rocky's Roborock map against the Ground Floor.
The runtime runs against a stub Home Assistant: states, the Roborock action,
background tasks.
"""
import asyncio
import math
import types

import sextant
from sextant import robots
from sextant import storage as st
from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def box(name, x0, y0, x1, y1, **extra):
    return {"entity_id": name, "poly": True, "cords": [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}], **extra}


# --- names ---------------------------------------------------------------------

def test_room_names_match_however_they_are_spelt():
    assert robots.norm("Sun Room") == robots.norm("Sunroom") == robots.norm("sun_room")
    assert robots.norm(None) == ""


def test_rooms_match_by_name_or_by_linked_area_once_each():
    floor = {"zones": [
        box("Jack Room", 0, 0, 100, 100, area_id="jack_bedroom"),
        box("Kitchen", 200, 0, 300, 100),
        box("Void", 100, 0, 200, 100, no_go=True),
    ]}
    rooms = [
        {"name": "Jack Bedroom", "x0": 0, "y0": 0, "x1": 4000, "y1": 4000},
        {"name": "kitchen", "x0": 0, "y0": 0, "x1": 2000, "y1": 2000},
        {"name": "Kitchen", "x0": 0, "y0": 0, "x1": 2000, "y1": 2000},   # a second robot room of the same name: plan room used once
        {"name": "Void", "x0": 0, "y0": 0, "x1": 10, "y1": 10},        # no-go areas are never rooms
        {"name": None, "x0": 0, "y0": 0, "x1": 10, "y1": 10},
    ]
    m = robots.match_rooms(rooms, floor)
    assert [(a, b) for a, b, _m, _p in m] == [("Jack Bedroom", "Jack Room"), ("kitchen", "Kitchen")]
    assert m[0][2] == (2000.0, 2000.0) and m[0][3] == (50.0, 50.0)


# --- the fit -------------------------------------------------------------------

def _truth(theta_deg, reflect, tx, ty, px_per_m):
    return {"scale": px_per_m / 1000.0, "theta": math.radians(theta_deg), "reflect": reflect, "tx": tx, "ty": ty}


def test_a_mirrored_turned_map_is_recovered_from_three_rooms():
    truth = _truth(30.0, True, 5000.0, 4000.0, 100.0)
    map_pts = [(20000.0, 30000.0), (26000.0, 31000.0), (23000.0, 36000.0)]
    matches = [(f"R{i}", f"P{i}", m, robots.apply(truth, m)) for i, m in enumerate(map_pts)]
    r = robots.fit(matches, 100.0)
    assert r["rms_m"] < 1e-6 and r["transform"]["reflect"] is True
    assert abs(math.degrees(r["transform"]["theta"]) - 30.0) < 1e-3
    assert abs(r["scale_ratio"] - 1.0) < 1e-3
    q = robots.apply(r["transform"], (24000.0, 33000.0))
    t = robots.apply(truth, (24000.0, 33000.0))
    assert math.hypot(q[0] - t[0], q[1] - t[1]) < 1e-3


def test_an_unmirrored_map_is_recognised_too():
    truth = _truth(-80.0, False, 900.0, 200.0, 50.0)
    map_pts = [(1000.0, 1000.0), (9000.0, 2000.0), (4000.0, 8000.0), (7000.0, 7000.0)]
    r = robots.fit([(str(i), str(i), m, robots.apply(truth, m)) for i, m in enumerate(map_pts)], 50.0)
    assert r["transform"]["reflect"] is False and r["rms_m"] < 1e-6


def test_a_room_the_maps_disagree_about_is_left_out_and_the_dock_never():
    truth = _truth(0.0, True, 0.0, 3000.0, 100.0)
    map_pts = [(10000.0, 10000.0), (16000.0, 10000.0), (10000.0, 16000.0), (16000.0, 16000.0), (13000.0, 13000.0)]
    matches = [(f"R{i}", f"P{i}", m, robots.apply(truth, m)) for i, m in enumerate(map_pts)]
    bad = matches[4]
    matches[4] = (bad[0], bad[1], bad[2], (bad[3][0] + 400.0, bad[3][1]))   # 4 m off: Roborock's Foyer
    r = robots.fit(matches, 100.0)
    assert [d["pair"] for d in r["dropped"]] == ["R4 = P4"] and r["rms_m"] < 1e-6
    # A dock marked 0.3 m off is kept (it is never dropped) and pulls the fit.
    dock_map = (12000.0, 11000.0)
    dock_plan = robots.apply(truth, dock_map)
    r2 = robots.fit(matches[:2], 100.0, dock=(dock_map, (dock_plan[0] + 30.0, dock_plan[1])))
    assert "dock" in [p["pair"] for p in r2["pairs"]] and r2["dropped"] == []


def test_fewer_than_two_points_is_no_fit():
    assert robots.fit([], 100.0) is None
    assert robots.fit([("A", "A", (0.0, 0.0), (0.0, 0.0))], 100.0) is None


def test_rockys_map_lines_up_with_the_ground_floor():
    """Rocky's real map (2026-09-26) against the real Ground Floor plan: eight
    rooms agree to under half a metre; the Foyer, which is the front hall on
    his map and the whole open middle of the house on ours, is left out."""
    rooms = [
        ("Morning room", 20900, 34700, 26950, 38050), ("Sunroom", 32150, 34750, 36000, 39000),
        ("Office", 32150, 29950, 36700, 34700), ("Dining room", 24800, 24050, 29200, 28800),
        ("Foyer", 28800, 23300, 32850, 28900), ("Sewing room", 31650, 24450, 36700, 28900),
        ("Laundry room", 21750, 29000, 24800, 31450), ("Kitchen", 18700, 29400, 23500, 34650),
        ("Great room", 27050, 32650, 32100, 38300),
    ]
    plan = {  # bounding-box centre (px) and size (m) of the plan's rooms
        "Great Room": (1272, 413, 4.7, 5.7), "Morning Room": (718, 315, 6.2, 3.1), "Office": (1711, 705, 3.9, 4.7),
        "Sun Room": (1714, 250, 4.0, 4.3), "Laundry Room": (647, 937, 2.9, 2.4), "Kitchen": (458, 741, 7.3, 5.8),
        "Sewing Room": (1682, 1259, 4.3, 4.1), "Dining Room": (1015, 1258, 4.3, 4.0), "Foyer": (1307, 967, 11.7, 9.7),
    }
    s = 102.03586
    floor = {"zones": [box(n, cx - w * s / 2, cy - h * s / 2, cx + w * s / 2, cy + h * s / 2) for n, (cx, cy, w, h) in plan.items()]}
    matches = robots.match_rooms([{"name": n, "x0": a, "y0": b, "x1": c, "y1": d} for n, a, b, c, d in rooms], floor)
    assert len(matches) == 9
    r = robots.fit(matches, s)
    assert r["transform"]["reflect"] is True and abs(math.degrees(r["transform"]["theta"])) < 3
    assert r["rms_m"] < 0.6 and len(r["pairs"]) == 8
    assert [d["pair"] for d in r["dropped"]] == ["Foyer = Foyer"]
    # His dock lands at the Dining Room's west wall ("Dining Room Rocky").
    x, y = robots.apply(r["transform"], (24939, 25673))
    din = plan["Dining Room"]
    assert abs(x - (din[0] - din[2] * s / 2)) < 0.5 * s and din[1] - din[3] * s / 2 < y < din[1] + din[3] * s / 2


# --- the runtime ---------------------------------------------------------------

def _hass_with_robot(tmp_path, state, position=None, fail=False):
    hass = make_hass(tmp_path)
    layout = {"floor": [{"name": "G", "scale": 100.0, "receivers": [], "subzones": [],
                         "zones": [box("Dining Room", 0, 0, 500, 500), box("Kitchen", 500, 0, 1000, 500)]}],
              "robots": {"vacuum.rocky": {"floor": "G", "dock_map": [1000.0, 1000.0],
                                          "fit": {"transform": {"scale": 0.1, "theta": 0.0, "reflect": False, "tx": 0.0, "ty": 0.0}, "rms_m": 0.4}}}}
    run(st.save_layout(hass, layout))
    hass.states = types.SimpleNamespace(get=lambda e: types.SimpleNamespace(state=state, attributes={}) if e == "vacuum.rocky" else None)
    calls, tasks = [], []

    async def async_call(domain, service, data, blocking=False, return_response=False):
        calls.append((domain, service, data))
        if fail:
            raise RuntimeError("Something went wrong creating the map")
        return {"vacuum.rocky": {"x": position[0], "y": position[1]}}

    hass.services = types.SimpleNamespace(async_call=async_call, has_service=lambda d, s: True)
    hass.async_create_background_task = lambda coro, name=None: tasks.append(coro)
    sextant._robot_state.clear()
    sextant.apitricords = []
    return hass, calls, tasks


def _row(thing="vacuum_rocky"):
    return next((r for r in sextant.apitricords if r["ent"] == thing), None)


def test_a_docked_robot_is_at_its_dock_and_never_asked(tmp_path):
    hass, calls, tasks = _hass_with_robot(tmp_path, "docked")
    run(sextant._robot_cycle(hass, st.get_layout(hass)))
    row = _row()
    assert row["cords"] == [100.0, 100.0] and row["zone"] == "Dining Room" and row["robot"] == "vacuum.rocky"
    assert calls == [] and tasks == []
    assert sextant.robot_things(st.get_layout(hass)) == ["vacuum_rocky"]


def test_a_cleaning_robot_is_asked_in_the_background_then_placed(tmp_path):
    hass, calls, tasks = _hass_with_robot(tmp_path, "cleaning", position=(7000.0, 2000.0))
    layout = st.get_layout(hass)
    run(sextant._robot_cycle(hass, layout))
    assert len(tasks) == 1 and _row() is None, "nothing to place until it has answered"
    run(tasks.pop())
    assert calls == [("roborock", "get_vacuum_current_position", {"entity_id": "vacuum.rocky"})]
    run(sextant._robot_cycle(hass, layout))
    assert tasks == [], "not asked again within robot_poll_secs"
    assert _row()["cords"] == [700.0, 200.0] and _row()["zone"] == "Kitchen"


def test_a_failed_read_keeps_the_last_position_and_an_unavailable_robot_is_not_placed(tmp_path):
    hass, calls, tasks = _hass_with_robot(tmp_path, "cleaning", fail=True)
    run(sextant._robot_cycle(hass, st.get_layout(hass)))
    run(tasks.pop())
    assert sextant._robot_state["vacuum.rocky"]["fails"] == 1 and sextant._robot_state["vacuum.rocky"]["inflight"] is False
    assert _row() is None
    hass2, _calls, tasks2 = _hass_with_robot(tmp_path, "unavailable")
    run(sextant._robot_cycle(hass2, st.get_layout(hass2)))
    assert tasks2 == [] and _row() is None


def test_a_robot_without_a_fit_is_not_a_thing(tmp_path):
    hass, _calls, _tasks = _hass_with_robot(tmp_path, "docked")
    layout = st.get_layout(hass)
    layout["robots"]["vacuum.rocky"]["fit"] = None
    assert sextant.robot_things(layout) == []
    run(sextant._robot_cycle(hass, layout))
    assert _row() is None


# --- the commands ----------------------------------------------------------------

class _Conn:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))


def test_line_up_then_mark_the_dock(tmp_path):
    from sextant import ws
    hass = make_hass(tmp_path)
    run(st.save_layout(hass, {"floor": [{"name": "G", "scale": 100.0, "receivers": [], "subzones": [], "zones": [
        box("Kitchen", 0, 0, 400, 400), box("Office", 600, 0, 1000, 400), box("Den", 0, 600, 400, 1000)]}]}))
    # The robot's map: the plan mirrored (its y points up), 1 px = 10 mm.
    rooms = [{"name": "Kitchen", "segment_id": 1, "x0": 0, "y0": -4000, "x1": 4000, "y1": 0},
             {"name": "Office", "segment_id": 2, "x0": 6000, "y0": -4000, "x1": 10000, "y1": 0},
             {"name": "Hallway", "segment_id": 3, "x0": 0, "y0": -1, "x1": 1, "y1": 0}]

    async def async_call(domain, service, data, blocking=False, return_response=False):
        assert (domain, service) == ("roborock", "get_vacuum_map_rooms")
        return {"vacuum.rocky": {"map_name": "Main floor", "charger": {"x": 2000, "y": -8000}, "vacuum": None, "rooms": rooms}}

    hass.services = types.SimpleNamespace(async_call=async_call, has_service=lambda d, s: True)
    hass.states = types.SimpleNamespace(get=lambda e: None)
    conn = _Conn()
    run(ws.ws_robot_align(hass, conn, {"id": 1, "type": "sextant/robot/align", "vacuum": "vacuum.rocky", "floor": "G"}))
    assert not conn.errors, conn.errors
    r = conn.results[-1][1]
    assert r["thing"] == "vacuum_rocky" and len(r["fit"]["pairs"]) == 2 and not r["dock_marked"]
    assert r["unmatched"] == {"robot": ["Hallway"], "plan": ["Den"]}
    layout = st.get_layout(hass)
    assert layout["thing_classes"]["vacuum_rocky"] == "robot"
    assert layout["robots"]["vacuum.rocky"]["dock_map"] == [2000.0, -8000.0]
    # The dock is at (200, 800) on the plan: marking it settles the mirror.
    run(ws.ws_robot_dock(hass, conn, {"id": 2, "type": "sextant/robot/dock", "vacuum": "vacuum.rocky", "floor": "G", "x": 200, "y": 800}))
    r = conn.results[-1][1]
    assert r["dock_marked"] and len(r["fit"]["pairs"]) == 3 and r["fit"]["rms_m"] < 0.01
    assert r["fit"]["transform"]["reflect"] is True
    # A floor without a scale cannot be lined up with.
    run(ws.ws_robot_align(hass, conn, {"id": 3, "type": "sextant/robot/align", "vacuum": "vacuum.rocky", "floor": "Nowhere"}))
    assert conn.errors and "no scale" in conn.errors[-1][2]


def test_reads_are_counted_ok_and_failed(tmp_path):
    hass, _calls, tasks = _hass_with_robot(tmp_path, "cleaning", position=(1000.0, 1000.0))
    run(sextant._robot_cycle(hass, st.get_layout(hass)))
    run(tasks.pop())
    rs = sextant._robot_state["vacuum.rocky"]
    assert (rs["reads_ok"], rs["reads_failed"], rs["last_error"]) == (1, 0, None)

    async def broken(*_a, **_k):
        raise RuntimeError("Something went wrong creating the map")
    hass.services.async_call = broken
    rs["last_poll"] = 0.0
    run(sextant._robot_cycle(hass, st.get_layout(hass)))
    run(tasks.pop())
    assert (rs["reads_ok"], rs["reads_failed"]) == (1, 1) and "went wrong" in rs["last_error"]
    assert rs["map"] == (1000.0, 1000.0), "the last good position is kept"
    from sextant import ws
    pub = ws._robot_public(hass, "vacuum.rocky", st.get_layout(hass)["robots"]["vacuum.rocky"])
    assert pub["reads"]["ok"] == 1 and pub["reads"]["failed"] == 1
