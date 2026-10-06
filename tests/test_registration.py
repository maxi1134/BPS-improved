"""Stacking floors from shared pins: the fit, its audit of the scale, and its refusals."""
import math

import pytest

from sextant import registration as reg

# A 12 m x 9 m house. Corner names are the pins.
HOUSE = {"NW": (0.0, 0.0), "NE": (12.0, 0.0), "SE": (12.0, 9.0), "SW": (0.0, 9.0), "Stair": (5.0, 4.0)}


def _floor(name, level, scale, *, shift=(0.0, 0.0), turn_deg=0.0, pins=HOUSE, drawn_scale=None, **extra):
    """A plan of the house drawn at `scale` px/m, cropped by `shift` px and
    rotated by `turn_deg`. `drawn_scale` is what the user *believes* the scale
    to be, when that differs from the truth."""
    t = math.radians(turn_deg)
    out = []
    for pin, (hx, hy) in pins.items():
        x = (math.cos(t) * hx - math.sin(t) * hy) * scale + shift[0]
        y = (math.sin(t) * hx + math.cos(t) * hy) * scale + shift[1]
        out.append({"pin_id": f"{name}-{pin}", "name": pin, "cords": {"x": x, "y": y}})
    return {"name": name, "level": level, "scale": drawn_scale or scale, "pins": out, **extra}


def test_floors_at_different_scales_and_crops_land_in_one_frame():
    layout = {"floor": [
        _floor("Ground", 0, 102.0, shift=(340, 210)),
        _floor("Second", 1, 127.8, shift=(95, 60)),
        _floor("Basement", -1, 104.9, shift=(40, 400), turn_deg=90),
    ]}
    solved = reg.solve(layout)
    assert solved["reference"] == "Ground"
    for name in ("Second", "Basement"):
        frame = solved["floors"][name]
        assert frame["ok"] and frame["shared"] == 5 and frame["rms_m"] < 1e-9
    # The same physical spot, 3 m east and 2 m south of the NW corner, drawn on
    # each plan, must come out as one house position.
    spots = []
    for f, turn in zip(layout["floor"], (0, 0, 90)):
        t = math.radians(turn)
        shift = {"Ground": (340, 210), "Second": (95, 60), "Basement": (40, 400)}[f["name"]]
        x = (math.cos(t) * 3 - math.sin(t) * 2) * f["scale"] + shift[0]
        y = (math.sin(t) * 3 + math.cos(t) * 2) * f["scale"] + shift[1]
        spots.append(reg.to_house(solved["floors"][f["name"]], x, y))
    for hx, hy in spots[1:]:
        assert hx == pytest.approx(spots[0][0], abs=1e-6) and hy == pytest.approx(spots[0][1], abs=1e-6)
    assert math.degrees(solved["floors"]["Basement"]["theta"]) == pytest.approx(-90, abs=1e-6)


def test_round_trip_through_the_house_frame():
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 130.0, shift=(77, -12), turn_deg=7)]}
    frame = reg.solve(layout)["floors"]["Second"]
    hx, hy = reg.to_house(frame, 640.0, 410.0)
    back = reg.from_house(frame, hx, hy)
    assert back == pytest.approx((640.0, 410.0), abs=1e-6)


def test_a_wrong_scale_is_reported_not_absorbed():
    """Second is really drawn at 130 px/m but the user measured 126. A
    similarity fit would hide that; the rigid fit must leave it visible, both
    as misses that grow away from the centre and as the scale the pins imply."""
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 130.0, drawn_scale=126.0)]}
    frame = reg.solve(layout)["floors"]["Second"]
    assert frame["implied_scale"] == pytest.approx(130.0, rel=1e-6)
    assert 0.1 < frame["rms_m"] < 0.4 and frame["ok"]      # visibly off, still usable
    assert frame["misses"]["Stair"] < frame["misses"]["SE"]  # worst at the far corners


def test_one_bad_pin_is_named():
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 100.0)]}
    next(p for p in layout["floor"][1]["pins"] if p["name"] == "NE")["cords"]["x"] += 120   # 1.2 m off
    frame = reg.solve(layout)["floors"]["Second"]
    assert frame["worst"] == "NE" and frame["max_m"] > 2 * sorted(frame["misses"].values())[-2]


def test_a_floor_is_reached_through_another_floor():
    """The attic shares no pin with the ground floor, only with the second."""
    second_only = {"Chimney": (8.0, 3.0), "Hatch": (2.0, 7.0)}
    second = _floor("Second", 1, 120.0, shift=(30, 30), pins={**HOUSE, **second_only})
    attic = _floor("Attic", 2, 90.0, shift=(500, 10), pins=second_only)
    solved = reg.solve({"floor": [_floor("Ground", 0, 100.0), attic, second]})
    assert solved["floors"]["Attic"]["ok"] and solved["floors"]["Attic"]["rms_m"] < 1e-9
    x, y = 8.0 * 90.0 + 500, 3.0 * 90.0 + 10
    assert reg.to_house(solved["floors"]["Attic"], x, y) == pytest.approx((8.0, 3.0), abs=1e-6)


def test_floors_that_cannot_be_registered_say_why():
    layout = {"floor": [
        _floor("Ground", 0, 100.0),
        _floor("Loft", 2, 100.0, pins={"NW": HOUSE["NW"]}),
        {"name": "Shed", "level": 0, "scale": 100.0},
        {**_floor("Cellar", -1, 100.0), "scale": None},
    ]}
    rep = reg.report(layout)
    assert rep["floors"]["Loft"]["ok"] is False and "shares 1 pin" in rep["floors"]["Loft"]["why"]
    assert rep["floors"]["Shed"]["why"] == "no pins" and rep["floors"]["Cellar"]["why"] == "no scale"
    assert reg.to_house(reg.solve(layout)["floors"]["Loft"], 1, 1) is None


def test_pins_bunched_together_do_not_count_as_a_registration():
    close = {"A": (5.0, 5.0), "B": (5.3, 5.1)}
    layout = {"floor": [_floor("Ground", 0, 100.0, pins={**HOUSE, **close}), _floor("Second", 1, 100.0, pins=close)]}
    frame = reg.solve(layout)["floors"]["Second"]
    assert frame["ok"] is False and frame["implied_scale"] is None


def test_unlinked_pins_and_no_pins_at_all():
    layout = {"floor": [_floor("Ground", 0, 100.0, pins={**HOUSE, "Porch": (6.0, -2.0)}), _floor("Second", 1, 100.0)]}
    assert reg.report(layout)["unlinked"] == ["Porch"]
    empty = reg.report({"floor": [{"name": "Ground", "scale": 100.0}]})
    assert empty["reference"] is None and empty["floors"] == {} and empty["pins"] == {}
    assert reg.report({}) == {"reference": None, "floors": {}, "pins": {}, "unlinked": []}


def test_junk_pins_are_skipped_and_the_first_of_a_name_wins():
    floor = {"name": "G", "scale": 100.0, "pins": [
        {"name": "NW", "cords": {"x": 100, "y": 200}}, {"name": "NW", "cords": {"x": 999, "y": 999}},
        {"name": "", "cords": {"x": 1, "y": 1}}, {"name": "Bad", "cords": {"x": float("nan"), "y": 1}},
        "nonsense", {"name": "NoCords"},
    ]}
    assert reg.floor_pins(floor) == {"NW": (1.0, 2.0)}


def test_elevation_is_explicit_or_a_storey_per_level():
    assert reg.elevation({"level": 1}) == 3.0 and reg.elevation({"level": -1}) == -3.0
    assert reg.elevation({"level": 1, "elevation": 3.66}) == 3.66
    assert reg.elevation({"level": 1, "elevation": "tall"}) == 3.0 and reg.elevation({}) == 0.0


def test_misplaced_pins_are_set_aside_instead_of_smeared_across_the_fit():
    """Two pins on the wrong corner must be NAMED, and must not drag the five
    good ones off. Plain least squares blamed a good pin and hid the bad ones."""
    seven = {**HOUSE, "Chimney": (8.0, 2.0), "Bay": (3.0, 9.0)}
    layout = {"floor": [_floor("Ground", 0, 100.0, pins=seven), _floor("Second", 1, 100.0, shift=(50, 80), pins=seven)]}
    pins = {p["name"]: p for p in layout["floor"][1]["pins"]}
    pins["SE"]["cords"]["y"] += 300      # 3 m south: the room upstairs is longer
    pins["SW"]["cords"]["y"] += 310
    frame = reg.solve(layout)["floors"]["Second"]
    assert sorted(frame["suspects"]) == ["SE", "SW"]
    assert frame["ok"] and frame["rms_m"] < 1e-6                      # the agreeing five fit perfectly
    assert frame["misses"]["SE"] == pytest.approx(3.0, abs=1e-6)      # its real error, not a share
    assert frame["misses"]["NW"] < 1e-6
    rep = reg.report(layout)["floors"]
    assert rep["Second"]["suspects"] == frame["suspects"]


def test_a_wrong_scale_does_not_make_good_pins_into_suspects():
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 130.0, drawn_scale=112.0)]}
    frame = reg.solve(layout)["floors"]["Second"]
    assert frame["suspects"] == [] and frame["implied_scale"] == pytest.approx(130.0, rel=1e-6)


def test_the_pins_scale_is_not_offered_when_the_pins_do_not_agree():
    """Every pin off in a different direction: no small set explains it, so
    nothing is set aside - and no scale may be read out of the mess."""
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 100.0)]}
    for pin, (dx, dy) in zip(layout["floor"][1]["pins"], ((90, 0), (-80, 60), (0, -110), (70, 90), (-100, -40))):
        pin["cords"]["x"] += dx; pin["cords"]["y"] += dy
    frame = reg.solve(layout)["floors"]["Second"]
    assert frame["suspects"] == [] and frame["implied_scale"] is None and frame["ok"] is False


def test_ghosts_put_the_other_floors_pins_on_this_floors_plan():
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 125.0, shift=(40, 10))]}
    next(p for p in layout["floor"][1]["pins"] if p["name"] == "NE")["cords"]["x"] += 250   # 2 m east upstairs
    rep = reg.report(layout)["floors"]
    on_ground = {g["name"]: g for g in rep["Ground"]["ghosts"]}
    assert on_ground["NW"]["floor"] == "Second"
    assert (on_ground["NW"]["x"], on_ground["NW"]["y"]) == pytest.approx((0, 0), abs=0.2)
    assert on_ground["NE"]["x"] == pytest.approx(1200 + 200, abs=0.2)     # 2 m east of the real corner, in ground px
    upstairs = {g["name"]: g for g in rep["Second"]["ghosts"]}
    assert (upstairs["NE"]["x"], upstairs["NE"]["y"]) == pytest.approx((12 * 125 + 40, 10), abs=0.2)  # where it SHOULD be


def test_with_few_pins_only_one_may_be_set_aside():
    """Five pins, two bad: setting both aside would leave three, too few to
    call the rest an agreement. It must say the floor disagrees, not guess."""
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 100.0)]}
    pins = {p["name"]: p for p in layout["floor"][1]["pins"]}
    pins["SE"]["cords"]["y"] += 300
    pins["SW"]["cords"]["y"] += 310
    frame = reg.solve(layout)["floors"]["Second"]
    assert frame["suspects"] == [] and frame["ok"] is False


def test_pins_that_agree_at_the_wrong_scale_are_not_blamed_for_it():
    """At 15 % out the floor cannot be lined up - but the report must make it
    possible to say WHY: the pins agree with each other, the scale does not."""
    layout = {"floor": [_floor("Ground", 0, 100.0), _floor("Second", 1, 109.0, drawn_scale=127.8)]}
    rep = reg.report(layout)["floors"]["Second"]
    assert rep["ok"] is False and rep["rms_m"] > 0.75          # rigid fit at the set scale: unusable
    assert rep["agree_rms_m"] < 0.01 and rep["suspects"] == []  # the pins themselves: fine
    assert rep["implied_scale"] == pytest.approx(109.0, abs=0.01)


def test_a_densely_pinned_floor_keeps_the_suspect_search_cheap():
    import time
    many = {f"P{i}": (float(i % 10) * 2.0, float(i // 10) * 2.0) for i in range(60)}
    layout = {"floor": [_floor("Ground", 0, 100.0, pins=many), _floor("Second", 1, 100.0, pins=many)]}
    next(p for p in layout["floor"][1]["pins"] if p["name"] == "P7")["cords"]["x"] += 400
    started = time.perf_counter()
    frame = reg.solve(layout)["floors"]["Second"]
    assert time.perf_counter() - started < 1.0
    assert frame["suspects"] == ["P7"]                      # a single bad pin is still found
