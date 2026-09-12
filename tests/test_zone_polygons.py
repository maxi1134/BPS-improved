"""Zone/sub-zone polygon lookup and its cache.

Every position update used to rebuild every zone's shapely Polygon from
scratch, for every tracked device, up to four times per cycle (the solve
step's zone_polys, then find_zone_for_point, find_nearest_zone and
find_sub_zone_for_point each called _floor_zone_polygons/
_floor_sub_zone_polygons independently) - even though the geometry is
identical across devices and cycles until the floorplan is actually edited.

These tests lock down two things: the lookups still return the right answer
(unchanged behaviour), and the cache actually avoids rebuilding when nothing
changed while still picking up a real edit (get_bps_data_version bump).
"""
from shapely.geometry import Point

import bps
from bps import storage as st
from conftest import make_hass


def _layout(kitchen_offset=0.0):
    """One floor, one zone ("Kitchen"), one sub-zone ("Sink") inside it.

    kitchen_offset shifts the whole zone along x, so a second call with a
    different offset produces genuinely different geometry - used to prove a
    cache invalidation actually re-reads the new shape rather than serving
    the old one.
    """
    x0 = kitchen_offset
    return {
        "floor": [
            {
                "name": "Ground Floor",
                "zones": [
                    {
                        "zone_id": "z-kitchen",
                        "entity_id": "Kitchen",
                        "poly": True,
                        "cords": [
                            {"x": x0 + 0, "y": 0},
                            {"x": x0 + 10, "y": 0},
                            {"x": x0 + 10, "y": 10},
                            {"x": x0 + 0, "y": 10},
                        ],
                    }
                ],
                "subzones": [
                    {
                        "entity_id": "Sink",
                        "parent": "z-kitchen",
                        "cords": [
                            {"x": x0 + 1, "y": 1},
                            {"x": x0 + 3, "y": 1},
                            {"x": x0 + 3, "y": 3},
                            {"x": x0 + 1, "y": 3},
                        ],
                    }
                ],
            }
        ]
    }


def _new_global_data(layout, entity="pet"):
    return [{"entity": entity, "data": layout}]


# --- correctness -------------------------------------------------------------

def test_find_zone_for_point_matches_the_containing_zone(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    assert bps.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(5, 5)) == "Kitchen"
    assert bps.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(500, 500)) == "unknown"


def test_find_nearest_zone_matches_even_outside_every_zone(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    assert bps.find_nearest_zone(hass, data, "pet", "Ground Floor", Point(15, 5)) == "Kitchen"


def test_find_sub_zone_for_point_matches_the_containing_sub_zone(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    sub, parent = bps.find_sub_zone_for_point(hass, data, "pet", "Ground Floor", Point(2, 2))
    assert (sub, parent) == ("Sink", "Kitchen")
    sub, parent = bps.find_sub_zone_for_point(hass, data, "pet", "Ground Floor", Point(8, 8))
    assert (sub, parent) == ("unknown", None)


# --- caching -------------------------------------------------------------

def test_zone_polygons_are_not_rebuilt_within_the_same_layout_version(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    first = bps._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    second = bps._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    # Same list object back: a cache hit, not a rebuild.
    assert first is second


def test_sub_zone_polygons_are_not_rebuilt_within_the_same_layout_version(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    first = bps._floor_sub_zone_polygons(hass, data, "pet", "Ground Floor")
    second = bps._floor_sub_zone_polygons(hass, data, "pet", "Ground Floor")
    assert first is second


def test_zone_polygon_cache_invalidates_when_the_layout_is_saved(tmp_path):
    hass = make_hass(tmp_path)
    data_v1 = _new_global_data(_layout(kitchen_offset=0.0))
    original = Point(5, 5)
    assert bps.find_zone_for_point(hass, data_v1, "pet", "Ground Floor", original) == "Kitchen"
    before = bps._floor_zone_polygons(hass, data_v1, "pet", "Ground Floor")

    # Edit and save the floorplan: the Kitchen zone moves away from (5, 5).
    run = __import__("asyncio").new_event_loop().run_until_complete
    run(st.save_bps_data(hass, _layout(kitchen_offset=1000.0)))
    data_v2 = _new_global_data(_layout(kitchen_offset=1000.0))

    # A stale cache would still answer "Kitchen" here (or worse, silently
    # keep returning the v1 polygon list) - it must not.
    after = bps._floor_zone_polygons(hass, data_v2, "pet", "Ground Floor")
    assert before is not after
    assert bps.find_zone_for_point(hass, data_v2, "pet", "Ground Floor", original) == "unknown"
    assert bps.find_zone_for_point(hass, data_v2, "pet", "Ground Floor", Point(1005, 5)) == "Kitchen"


def test_zone_polygon_cache_is_per_hass_not_module_global(tmp_path_factory):
    hass_a = make_hass(tmp_path_factory.mktemp("a"))
    hass_b = make_hass(tmp_path_factory.mktemp("b"))
    data_a = _new_global_data(_layout(kitchen_offset=0.0))
    data_b = _new_global_data(_layout(kitchen_offset=1000.0))

    assert bps.find_zone_for_point(hass_a, data_a, "pet", "Ground Floor", Point(5, 5)) == "Kitchen"
    # hass_b must compile its own polygons from data_b, not reuse hass_a's cached ones.
    assert bps.find_zone_for_point(hass_b, data_b, "pet", "Ground Floor", Point(5, 5)) == "unknown"
    assert bps.find_zone_for_point(hass_b, data_b, "pet", "Ground Floor", Point(1005, 5)) == "Kitchen"
