"""Placement advice (sextant.advice.advise): per-room issues and suggested wall spots."""
import sextant  # noqa: F401
from sextant import advice


def _rect(name, x0, y0, x1, y1):
    return {"zone_id": name, "entity_id": name, "poly": True,
            "cords": [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]}


def _layout(receivers, zones):
    return {"floor": [{"name": "F", "scale": 100.0, "zones": zones,
                       "receivers": [{"entity_id": s, "cords": {"x": x, "y": y}} for s, x, y in receivers]}]}


def _selftest(rows):
    return {"receivers": [{"entity": s, "floor": "F", "room": room, "error_m": e, "heard_by": 5} for s, room, e in rows]}


def test_a_room_with_no_proxy_gets_a_wall_spot_inside_it():
    layout = _layout([("a", 50, 50), ("b", 350, 50), ("c", 200, 350)], [_rect("Hall", 0, 0, 400, 400), _rect("Closet", 400, 0, 700, 400)])
    out = advice.advise(layout, _selftest([("a", "Hall", 1.0), ("b", "Hall", 1.2), ("c", "Hall", 0.8)]))
    closet = next(r for r in out["rooms"] if r["room"] == "Closet")
    assert closet["issue"] == "no proxy" and closet["add"] >= 1
    x, y = closet["spots"][0]["x"], closet["spots"][0]["y"]
    assert 400 <= x <= 700 and 0 <= y <= 400
    # An outlet is on a wall: the spot sits just inside the room's edge.
    assert min(abs(x - 400), abs(x - 700), abs(y - 0), abs(y - 400)) <= advice.WALL_INSET_M * 100 + 1
    assert out["rooms"][0]["room"] == "Closet"  # worst first
    assert out["summary"]["to_add"] >= 1


def test_a_well_covered_well_measured_room_is_fine():
    layout = _layout([("a", 20, 20), ("b", 380, 20), ("c", 200, 380), ("d", 20, 380)], [_rect("Hall", 0, 0, 400, 400)])
    out = advice.advise(layout, _selftest([("a", "Hall", 1.0), ("b", "Hall", 1.2), ("c", "Hall", 0.8), ("d", "Hall", 1.1)]))
    hall = out["rooms"][0]
    assert hall["issue"] == "ok" and hall["add"] == 0 and hall["proxies"] == 4 and hall["median_m"] == 1.05


def test_one_bad_proxy_among_good_ones_is_called_out_not_papered_over():
    layout = _layout([("a", 20, 20), ("b", 380, 20), ("c", 200, 380), ("d", 20, 380)], [_rect("Hall", 0, 0, 400, 400)])
    out = advice.advise(layout, _selftest([("a", "Hall", 1.0), ("b", "Hall", 6.5), ("c", "Hall", 0.8), ("d", "Hall", 1.1)]))
    hall = out["rooms"][0]
    assert hall["issue"] == "weak proxy" and hall["worst"] == "b" and hall["add"] == 0 and "b" in hall["note"]


def test_a_long_room_with_all_proxies_at_one_end_gets_spots_at_the_other():
    # 12 m x 3 m room, three proxies bunched in the first 2 m: the far end has its third proxy 10 m away.
    layout = _layout([("a", 20, 20), ("b", 180, 20), ("c", 100, 280)], [_rect("Corridor", 0, 0, 1200, 300)])
    out = advice.advise(layout, _selftest([("a", "Corridor", 2.5), ("b", "Corridor", 2.8), ("c", "Corridor", 2.4)]))
    corr = out["rooms"][0]
    assert corr["issue"] in ("coverage", "one-sided") and 1 <= corr["add"] <= advice.MAX_ADD
    assert max(s["x"] for s in corr["spots"]) > 400  # at least one spot well into the uncovered end
    assert corr["covered"] < 0.9 and corr["third_proxy_m"] > advice.TARGET_3RD_M


def test_unplaced_proxies_are_passed_through_and_floors_without_scale_skipped():
    layout = _layout([], [_rect("Hall", 0, 0, 400, 400)])
    layout["floor"].append({"name": "Unscaled", "scale": None, "zones": [_rect("X", 0, 0, 10, 10)], "receivers": []})
    out = advice.advise(layout, {"receivers": []}, unplaced=[{"slug": "kitchen_s3_f249cc", "name": "Kitchen S3 f249cc"}])
    assert out["unplaced"][0]["slug"] == "kitchen_s3_f249cc" and out["unplaced"][0]["suggest"]["room"] == "Hall"
    assert [r["floor"] for r in out["rooms"]] == ["F"] and out["rooms"][0]["issue"] == "no proxy"


def test_two_bad_proxies_among_three_are_both_named():
    layout = _layout([("a", 20, 20), ("b", 380, 20), ("c", 200, 380)], [_rect("Jack", 0, 0, 400, 400)])
    out = advice.advise(layout, _selftest([("a", "Jack", 0.7), ("b", "Jack", 7.0), ("c", "Jack", 4.7)]))
    room = out["rooms"][0]
    assert room["issue"] == "weak proxy" and room["weak"] == ["b"]
    # Errors that are merely bad everywhere are "noisy" and get one far-wall spot for a fourth opinion.
    out = advice.advise(layout, _selftest([("a", "Jack", 4.1), ("b", "Jack", 4.9), ("c", "Jack", 4.7)]))
    room = out["rooms"][0]
    assert room["issue"] == "noisy" and room["add"] == 1 and len(room["spots"]) == 1
