"""Adjust rooms / adjust spots: pure geometry (sextant.zone_adjust).

Squares rooms that are nearly rectangles, closes small gaps into shared walls,
leaves real gaps and L-shapes alone, removes overlaps, clamps spots into their
room. Coordinates are the panel's 2000 px space; the default tolerance is 22 px.
"""
from shapely.geometry import Polygon

import sextant  # noqa: F401
from sextant import zone_adjust as za


def _rect(x0, y0, x1, y1, name):
    return {"zone_id": name, "entity_id": name, "poly": True, "type": "zone",
            "cords": [{"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]}


def _poly(z):
    return Polygon([(c["x"], c["y"]) for c in z["cords"]])


def test_nearly_rectangular_room_is_squared_and_reported():
    room = _rect(0, 0, 400, 300, "A")
    room["cords"][2] = {"x": 405, "y": 294}  # one corner a few pixels off
    before = [dict(c) for c in room["cords"]]
    out = za.adjust_zones([room])
    fixed = _poly(out["zones"][0])
    # A rectangle (possibly rotated a hair, following the drawn corners): four
    # vertices, and it fills its own minimum rotated rectangle.
    assert len(out["zones"][0]["cords"]) == 4
    assert fixed.area / fixed.minimum_rotated_rectangle.area > 0.995
    assert abs(fixed.area - 400 * 300) < 0.03 * 400 * 300
    change = out["changes"][0]
    assert change["name"] == "A" and change["squared"] is True and change["max_move_px"] < 10
    assert room["cords"] == before  # input is never mutated
    assert out["warnings"] == []


def test_small_gap_becomes_a_shared_wall_but_a_real_gap_stays():
    a, b = _rect(0, 0, 400, 300, "A"), _rect(410, 0, 800, 300, "B")  # 10 px gap
    out = za.adjust_zones([a, b])
    a_right = max(c["x"] for c in out["zones"][0]["cords"])
    b_left = min(c["x"] for c in out["zones"][1]["cords"])
    assert abs(a_right - b_left) < 0.5

    a, b = _rect(0, 0, 400, 300, "A"), _rect(460, 0, 800, 300, "B")  # 60 px: a real gap
    out = za.adjust_zones([a, b])
    a_right = max(c["x"] for c in out["zones"][0]["cords"])
    b_left = min(c["x"] for c in out["zones"][1]["cords"])
    assert b_left - a_right > 50


def test_overlapping_rooms_no_longer_overlap():
    a, b = _rect(0, 0, 410, 300, "A"), _rect(390, 0, 800, 300, "B")  # 20 px overlap
    out = za.adjust_zones([a, b])
    pa, pb = _poly(out["zones"][0]), _poly(out["zones"][1])
    assert pa.intersection(pb).area < 1.0
    # Both rooms are still (roughly) their old size: nothing was swallowed.
    assert abs(pa.area - 400 * 300) < 0.1 * 400 * 300 and abs(pb.area - 410 * 300) < 0.1 * 410 * 300


def test_l_shaped_room_keeps_its_shape():
    ell = {"zone_id": "L", "entity_id": "L", "poly": True, "type": "zone",
           "cords": [{"x": 0, "y": 0}, {"x": 400, "y": 0}, {"x": 400, "y": 150},
                     {"x": 200, "y": 150}, {"x": 200, "y": 300}, {"x": 0, "y": 300}]}
    out = za.adjust_zones([ell])
    change = out["changes"][0]
    assert change["squared"] is False and change["vertices_after"] == 6 and change["max_move_px"] < 1


def test_unparseable_zone_passes_through_with_a_warning():
    bad = {"zone_id": "X", "entity_id": "X", "poly": True, "type": "zone", "cords": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}
    out = za.adjust_zones([bad, _rect(0, 0, 400, 300, "A")])
    assert out["zones"][0] == bad and out["warnings"]
    assert [c["name"] for c in out["changes"]] == ["A"]


def test_spot_is_clamped_into_its_room_and_rooms_are_untouched():
    room = _rect(0, 0, 400, 300, "A")
    sofa = {"sub_zone_id": "s1", "entity_id": "Sofa", "parent": "A", "poly": True,
            "cords": [{"x": 300, "y": 100}, {"x": 450, "y": 100}, {"x": 450, "y": 200}, {"x": 300, "y": 200}]}
    out = za.adjust_subzones([room], [sofa])
    clamped = out["subzones"][0]
    assert max(c["x"] for c in clamped["cords"]) <= 400.5
    assert _poly(clamped).area > 0.5 * 100 * 100  # the part inside the room survives
    assert out["zones"] == [room] and out["changes"][0]["name"] == "Sofa"


def _spot(x0, y0, x1, y1, name, parent=None):
    s = _rect(x0, y0, x1, y1, name)
    s.pop("zone_id")
    s.update(sub_zone_id=name, type="subzone", parent=parent)
    return s


def _floor(*spots):
    nogo = _rect(0, 0, 1000, 1000, "N")
    nogo["no_go"] = True
    return {"zones": [nogo, _rect(0, 0, 400, 300, "A"), _rect(400, 0, 800, 300, "B")],
            "subzones": list(spots)}


def test_a_spot_drawn_across_a_wall_is_clipped_to_its_room():
    floor = _floor(_spot(300, 100, 500, 200, "couch", parent="A"))
    assert za.confine_spots(floor) == ["couch"]
    s = floor["subzones"][0]
    assert s["parent"] == "A"
    assert _poly(s).equals(Polygon([(300, 100), (400, 100), (400, 200), (300, 200)]))


def test_a_spot_already_inside_its_room_is_left_exactly_as_it_is():
    spot = _spot(100, 100, 200, 200, "desk", parent="A")
    before = [dict(c) for c in spot["cords"]]
    floor = _floor(spot)
    assert za.confine_spots(floor) == []
    assert spot["cords"] == before and spot["parent"] == "A"
    # A corner on the wall itself is inside, not a reason to rewrite the shape.
    edge = _spot(300, 100, 400.3, 200, "bed", parent="A")
    assert za.confine_spots(_floor(edge)) == []


def test_a_spot_without_a_room_takes_the_one_it_overlaps_most():
    floor = _floor(_spot(350, 100, 600, 200, "rug"))
    assert za.confine_spots(floor) == ["rug"]
    s = floor["subzones"][0]
    assert s["parent"] == "B"  # 200 px of it in B, 50 in A; never the no-go
    assert _poly(s).bounds == (400, 100, 600, 200)


def test_a_spot_dragged_into_another_room_follows_it_instead_of_vanishing():
    floor = _floor(_spot(500, 100, 600, 200, "chair", parent="A"))
    assert za.confine_spots(floor) == ["chair"]
    assert floor["subzones"][0]["parent"] == "B"
    assert _poly(floor["subzones"][0]).area == 100 * 100


def test_a_spot_outside_every_room_is_left_alone():
    spot = _spot(900, 900, 950, 950, "shed")
    floor = _floor(spot)
    floor["zones"] = floor["zones"][1:]  # no no-go under it either
    assert za.confine_spots(floor) == []
    assert spot["parent"] is None
