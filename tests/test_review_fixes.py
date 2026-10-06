"""Regressions for the fixes from a whole-codebase review."""

from shapely.geometry import Polygon

import sextant
from sextant import advice
from sextant import history as H
from sextant import registration
from sextant import truth


def test_a_silence_ending_on_another_floor_is_a_dropout():
    h = H.PositionHistory(H.history_config({}))
    t0 = 1_000_000.0
    h.record("tag", t0, 1.0, 1.0, "A", 40.0)
    # Five minutes unheard: past the dropout threshold, short of the prune.
    h.record("tag", t0 + 300, 1.0, 1.0, "B", 50.0)
    assert h.query("tag", 0, 1e12, 100)["gap"][-1] == H.GAP_DROPOUT


def test_a_quick_floor_change_is_only_a_frame_break():
    h = H.PositionHistory(H.history_config({}))
    t0 = 1_000_000.0
    h.record("tag", t0, 1.0, 1.0, "A", 40.0)
    h.record("tag", t0 + 5, 1.0, 1.0, "B", 50.0)
    assert h.query("tag", 0, 1e12, 100)["gap"][-1] == H.GAP_FRAME


def test_mark_reference_divides_each_sample_by_its_own_gain():
    mark = {"id": 1, "floor": "F", "x": 0, "y": 0, "samples": [
        {"gain": 1.0, "thing_vec": {"a": 2.0}},
        {"gain": 1.0, "thing_vec": {"a": 2.0}},
        {"gain": 2.0, "thing_vec": {"a": 4.0}},
    ]}
    # Every sample is 2.0 in the probe scale; dividing the median by the last
    # gain alone made it 1.0.
    assert truth.mark_reference(mark)["vector"]["a"] == 2.0


def test_mac_suffix_match_is_not_overwritten_by_the_token_search():
    directory = {
        "dc:06:75:4e:89:3e": {"slug": "sewing_room_rrn00_4e893c_dc_06_75_4e_89_3e",
                              "unique_id": "zz", "address_wifi_mac": "zz"},
        "dc:06:75:4e:89:4e": {"slug": "other_rrn00_4e893c",
                              "unique_id": "yy", "address_wifi_mac": "yy"},
    }
    # Renamed since, so the slug alone no longer matches; the suffix still
    # names the address, while the token ("4e893c") matches two scanners.
    directory["dc:06:75:4e:89:3e"]["slug"] = "sewing_room_rrn00_4e893c_renamed"
    layout = {"floor": [{"name": "F", "receivers": [
        {"entity_id": "sewing_room_rrn00_4e893c_dc_06_75_4e_89_3e", "cords": {"x": 0, "y": 0}},
    ]}]}
    sextant._resolve_receiver_addresses(layout, directory)
    assert layout["floor"][0]["receivers"][0].get("address") == "dc:06:75:4e:89:3e"


def test_zone_membership_vectorised_matches_the_plain_walk():
    tuples = [
        ("a", Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), 1, False),
        ("b", Polygon([(10, 0), (20, 0), (20, 10), (10, 10)]), 1, False),
        ("n", Polygon([(0, 10), (20, 10), (20, 20), (0, 20)]), 1, True),
    ]
    samples = [(5, 5, 1.0), (15, 5, 2.0), (10, 5, 1.0), (25, 5, 1.0), (5, 15, 3.0)]
    fast = sextant._zone_membership(sextant._FloorZones(tuples), samples)
    plain = sextant._zone_membership(list(tuples), samples)
    assert fast == plain


def test_advice_survives_a_room_with_a_narrow_neck():
    # Two rooms joined by a 10 cm neck: the wall inset splits it in two.
    ring = [(0, 0), (10, 0), (10, 4.95), (12, 4.95), (12, 0), (22, 0),
            (22, 10), (12, 10), (12, 5.05), (10, 5.05), (10, 10), (0, 10)]
    rooms = advice._rooms_of({"zones": [{"entity_id": "r", "cords": [{"x": x, "y": y} for x, y in ring]}]})
    assert advice._wall_candidates(rooms[0][1], 1.0)


def test_a_rejected_floor_does_not_place_pins_for_the_rest():
    def floor(name, pins, level=None):
        return {"name": name, "scale": 1.0, "level": level,
                "pins": [{"name": p, "cords": {"x": x, "y": y}} for p, (x, y) in pins.items()]}
    # B shares only two pins with A, half a metre apart: too close to fix its
    # rotation, so B is rejected - and it is C's only link to the house.
    a = floor("A", {"p1": (0, 0), "p2": (0.5, 0)}, level=0)
    b = floor("B", {"p1": (0, 0), "p2": (0.5, 0), "q1": (5, 5), "q2": (9, 5), "q3": (5, 9)})
    c = floor("C", {"q1": (5, 5), "q2": (9, 5), "q3": (5, 9)})
    frames = registration.solve({"floor": [a, b, c]})["floors"]
    assert frames["B"]["ok"] is False
    assert not frames.get("C", {}).get("ok")
