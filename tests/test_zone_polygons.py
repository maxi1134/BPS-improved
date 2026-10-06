"""Zone/sub-zone polygon lookup and its cache.

Every position update used to rebuild every zone's shapely Polygon from
scratch, for every tracked device, up to four times per cycle (the solve
step's zone_polys, then find_zone_for_point, find_nearest_zone and
find_sub_zone_for_point each called _floor_zone_polygons/
_floor_sub_zone_polygons independently) - even though the geometry is
identical across devices and cycles until the floorplan is actually edited.

These tests lock down two things: the lookups still return the right answer
(unchanged behaviour), and the cache actually avoids rebuilding when nothing
changed while still picking up a real edit (get_layout_version bump).
"""
from shapely.geometry import Point

import sextant
from sextant import storage as st
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
    assert sextant.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(5, 5)) == "Kitchen"
    assert sextant.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(500, 500)) == "unknown"


def test_find_nearest_zone_matches_even_outside_every_zone(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    assert sextant.find_nearest_zone(hass, data, "pet", "Ground Floor", Point(15, 5)) == "Kitchen"


def test_find_sub_zone_for_point_matches_the_containing_sub_zone(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    sub, parent = sextant.find_sub_zone_for_point(hass, data, "pet", "Ground Floor", Point(2, 2))
    assert (sub, parent) == ("Sink", "Kitchen")
    sub, parent = sextant.find_sub_zone_for_point(hass, data, "pet", "Ground Floor", Point(8, 8))
    assert (sub, parent) == ("unknown", None)


# --- caching -------------------------------------------------------------

def test_zone_polygons_are_not_rebuilt_within_the_same_layout_version(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    first = sextant._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    second = sextant._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    # Same list object back: a cache hit, not a rebuild.
    assert first is second


def test_sub_zone_polygons_are_not_rebuilt_within_the_same_layout_version(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_layout())
    first = sextant._floor_sub_zone_polygons(hass, data, "pet", "Ground Floor")
    second = sextant._floor_sub_zone_polygons(hass, data, "pet", "Ground Floor")
    assert first is second


def test_zone_polygon_cache_invalidates_when_the_layout_is_saved(tmp_path):
    hass = make_hass(tmp_path)
    data_v1 = _new_global_data(_layout(kitchen_offset=0.0))
    original = Point(5, 5)
    assert sextant.find_zone_for_point(hass, data_v1, "pet", "Ground Floor", original) == "Kitchen"
    before = sextant._floor_zone_polygons(hass, data_v1, "pet", "Ground Floor")

    # Edit and save the floorplan: the Kitchen zone moves away from (5, 5).
    run = __import__("asyncio").new_event_loop().run_until_complete
    run(st.save_layout(hass, _layout(kitchen_offset=1000.0)))
    data_v2 = _new_global_data(_layout(kitchen_offset=1000.0))

    # A stale cache would still answer "Kitchen" here (or worse, silently
    # keep returning the v1 polygon list) - it must not.
    after = sextant._floor_zone_polygons(hass, data_v2, "pet", "Ground Floor")
    assert before is not after
    assert sextant.find_zone_for_point(hass, data_v2, "pet", "Ground Floor", original) == "unknown"
    assert sextant.find_zone_for_point(hass, data_v2, "pet", "Ground Floor", Point(1005, 5)) == "Kitchen"


def test_zone_polygon_cache_is_per_hass_not_module_global(tmp_path_factory):
    hass_a = make_hass(tmp_path_factory.mktemp("a"))
    hass_b = make_hass(tmp_path_factory.mktemp("b"))
    data_a = _new_global_data(_layout(kitchen_offset=0.0))
    data_b = _new_global_data(_layout(kitchen_offset=1000.0))

    assert sextant.find_zone_for_point(hass_a, data_a, "pet", "Ground Floor", Point(5, 5)) == "Kitchen"
    # hass_b must compile its own polygons from data_b, not reuse hass_a's cached ones.
    assert sextant.find_zone_for_point(hass_b, data_b, "pet", "Ground Floor", Point(5, 5)) == "unknown"
    assert sextant.find_zone_for_point(hass_b, data_b, "pet", "Ground Floor", Point(1005, 5)) == "Kitchen"


# --- derived geometry (rings, snap union, nearest array) -----------------------
#
# The per-cycle lookups used to rebuild a buffer ring per zone, the allowed
# minus no-go union and one distance call per zone on EVERY call. That
# geometry is now part of the cached per-floor object; these lock down that
# the answers are unchanged and that the derived pieces are built once.

def _two_room_layout():
    """Kitchen (0..10) and Hall (20..30) on x, a no-go Void at 12..18 between
    them, all 0..10 on y."""
    def box(x0, x1, **extra):
        return {"poly": True, "cords": [{"x": x0, "y": 0}, {"x": x1, "y": 0}, {"x": x1, "y": 10}, {"x": x0, "y": 10}], **extra}
    return {"floor": [{"name": "Ground Floor", "zones": [
        {"zone_id": "z-k", "entity_id": "Kitchen", **box(0, 10)},
        {"zone_id": "z-v", "entity_id": "Void", "no_go": True, **box(12, 18)},
        {"zone_id": "z-h", "entity_id": "Hall", **box(20, 30)},
    ], "subzones": []}]}


def test_floor_zones_carry_their_derived_geometry_once(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_two_room_layout())
    zones = sextant._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    assert isinstance(zones, sextant._FloorZones)
    assert [z[0] for z in zones] == ["Kitchen", "Void", "Hall"]  # tuples unchanged for other callers
    assert zones.allowed_ids == ["Kitchen", "Hall"]  # no-go zones never in the lookups
    assert len(zones.allowed_geoms) == len(zones.allowed_rings) == len(zones.allowed_boundaries) == 2
    valid, nogo = zones.snap
    assert nogo is not None and valid is not None and not valid.covers(Point(15, 5))
    # Same object, same derived geometry across lookups in the same version.
    again = sextant._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    assert again is zones and again.snap is zones.snap


def test_zone_lookup_soft_buffer_picks_the_closest_edge(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_two_room_layout())
    # Just outside the Kitchen (buffer is 5% of its mean side = 0.5).
    assert sextant.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(10.3, 5)) == "Kitchen"
    assert sextant.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(19.7, 5)) == "Hall"
    assert sextant.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(15, 5)) == "unknown"  # in the void
    assert sextant.find_zone_for_point(hass, data, "pet", "Ground Floor", (25, 5)) == "Hall"  # bare pair accepted
    assert sextant.find_nearest_zone(hass, data, "pet", "Ground Floor", Point(14, 5)) == "Kitchen"
    assert sextant.find_nearest_zone(hass, data, "pet", "Ground Floor", Point(16, 5)) == "Hall"
    assert sextant.find_nearest_zone(hass, data, "pet", "Ground Floor", (15, 5)) == "Kitchen"  # first of equals


def test_snap_uses_the_cached_union_and_still_takes_a_plain_list(tmp_path):
    hass = make_hass(tmp_path)
    data = _new_global_data(_two_room_layout())
    zones = sextant._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    assert sextant.snap_point_into_zones(zones, Point(5, 5)) is None  # already in a room
    snapped = sextant.snap_point_into_zones(zones, Point(15, 5))  # in the void: to a room edge
    assert snapped is not None and (snapped.x <= 10 or snapped.x >= 20)
    # Same answer from the tuples alone (no cached geometry).
    plain = sextant.snap_point_into_zones(list(zones), Point(15, 5))
    assert (plain.x, plain.y) == (snapped.x, snapped.y)
    # A floor with only dead space pushes the point just off it.
    only_void = [z for z in zones if z[3]]
    off = sextant.snap_point_into_zones(only_void, Point(15, 5))
    assert off is not None and (off.x < 12 or off.x > 18)
    assert sextant.snap_point_into_zones([], Point(15, 5)) is None


def test_out_of_a_void_the_snap_keeps_the_room_the_thing_is_in(tmp_path):
    """Eilee's watch: a fix in the foyer void about equally far from her wall
    and Jack's. Memoryless, it snaps to whichever is nearer this cycle; with
    her room preferred it stays hers until his wall is clearly nearer."""
    hass = make_hass(tmp_path)
    data = _new_global_data(_two_room_layout())
    zones = sextant._floor_zone_polygons(hass, data, "pet", "Ground Floor")
    near_hall = Point(16, 5)  # 6 from the Kitchen's wall, 4 from the Hall's
    assert sextant.snap_point_into_zones(zones, near_hall).x >= 20, "memoryless: the nearer wall"
    kept = sextant.snap_point_into_zones(zones, near_hall, prefer="Kitchen", prefer_margin_px=3.0)
    assert kept.x <= 10, "the Kitchen is only 2 farther: stay in the Kitchen"
    left = sextant.snap_point_into_zones(zones, near_hall, prefer="Kitchen", prefer_margin_px=1.0)
    assert left.x >= 20, "past the margin the nearer wall wins after all"
    # No preference, an unknown or a no-go "room", or a point already in a
    # room: exactly the old behaviour.
    for prefer in (None, "Nowhere", "Void"):
        assert sextant.snap_point_into_zones(zones, near_hall, prefer=prefer, prefer_margin_px=3.0).x >= 20
    assert sextant.snap_point_into_zones(zones, Point(25, 5), prefer="Kitchen", prefer_margin_px=100.0) is None
    # The plain tuple list takes the same path.
    assert sextant.snap_point_into_zones(list(zones), near_hall, prefer="Kitchen", prefer_margin_px=3.0).x <= 10


def test_floor_without_allowed_zones_answers_unknown(tmp_path):
    hass = make_hass(tmp_path)
    layout = _two_room_layout()
    layout["floor"][0]["zones"] = [z for z in layout["floor"][0]["zones"] if z.get("no_go")]
    data = _new_global_data(layout)
    assert sextant.find_zone_for_point(hass, data, "pet", "Ground Floor", Point(15, 5)) == "unknown"
    assert sextant.find_nearest_zone(hass, data, "pet", "Ground Floor", Point(15, 5)) == "unknown"
    assert sextant._floor_zone_polygons(hass, data, "pet", "Ground Floor").allowed_ids == []
