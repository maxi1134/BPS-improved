"""The bias field on its own: sampling, laying, painting, and refusing junk."""
import math

import pytest

from sextant import floor_field as ff


def _floor(values, cell_m=1.0, scale=100.0, x0=0.0, y0=0.0):
    return {"name": "F", "scale": scale, "bias_field": {"cell_m": cell_m, "x0": x0, "y0": y0, "values": values}}


def test_no_field_and_a_flat_field_are_both_exactly_neutral():
    assert ff.sample({"name": "F", "scale": 100.0}, (10, 10)) == 1.0
    flat = {"name": "F", "scale": 100.0}
    flat["bias_field"] = ff.flat((0, 0, 1000, 700), 100.0)
    for pt in ((0, 0), (-500, -500), (333.3, 471.9), (999, 699), (5000, 5000)):
        assert ff.sample(flat, pt) == 1.0  # ==, not approx: the no-op must be bit-exact


def test_sampling_is_bilinear_between_cell_centres_and_holds_at_the_edges():
    floor = _floor([[1.0, 2.0], [3.0, 4.0]])
    assert ff.sample(floor, (50, 50)) == 1.0 and ff.sample(floor, (150, 150)) == 4.0
    assert ff.sample(floor, (100, 50)) == pytest.approx(1.5)
    assert ff.sample(floor, (100, 100)) == pytest.approx(2.5)
    assert ff.sample(floor, (-999, -999)) == 1.0 and ff.sample(floor, (999, 999)) == 4.0


def test_sampling_is_continuous_across_a_cell_boundary():
    floor = _floor([[1.0, 3.0]])
    left, right = ff.sample(floor, (99.999, 50)), ff.sample(floor, (100.001, 50))
    assert abs(left - right) < 1e-3


def test_the_grid_is_in_metres_so_it_means_the_same_at_any_plan_resolution():
    coarse = _floor([[1.0, 2.0]], scale=50.0)
    fine = _floor([[1.0, 2.0]], scale=200.0)
    # 1.0 m from the grid's left edge: the boundary between the two cells.
    assert ff.sample(coarse, (50, 25)) == pytest.approx(ff.sample(fine, (200, 100)))


@pytest.mark.parametrize("broken", [
    {"cell_m": 1.0, "x0": 0, "y0": 0, "values": [[1, 2], [3]]},          # ragged
    {"cell_m": 0, "x0": 0, "y0": 0, "values": [[1]]},                     # nonsense cell
    {"cell_m": 1.0, "x0": float("nan"), "y0": 0, "values": [[1]]},
    {"cell_m": 1.0, "x0": 0, "y0": 0, "values": []},
    {"cell_m": 1.0, "x0": 0, "y0": 0, "values": "1,2,3"},
    "not a dict",
])
def test_a_broken_field_behaves_exactly_like_no_field(broken):
    floor = {"name": "F", "scale": 100.0, "bias_field": broken}
    assert ff.parse(floor) is None and ff.sample(floor, (10, 10)) == 1.0


def test_junk_cells_read_as_neutral_and_values_are_clamped():
    floor = _floor([[None, "x", float("inf"), -2, 0, True, 1e9, 1e-9]])
    got = [ff.sample(floor, (c * 100 + 50, 50)) for c in range(8)]
    assert got == [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, ff.FIELD_MAX, ff.FIELD_MIN]


def test_a_floor_without_a_scale_cannot_carry_a_field():
    floor = _floor([[2.0]])
    floor["scale"] = None
    assert ff.sample(floor, (10, 10)) == 1.0
    with pytest.raises(ValueError, match="no scale"):
        ff.flat((0, 0, 100, 100), None)


def test_flat_covers_the_bounds_and_refuses_an_absurd_grid():
    field = ff.flat((100, 50, 1130, 760), 100.0, cell_m=1.0)
    assert (field["x0"], field["y0"]) == (100, 50)
    assert len(field["values"]) == 8 and len(field["values"][0]) == 11  # ceil(7.1), ceil(10.3)
    with pytest.raises(ValueError, match="larger cell_m"):
        ff.flat((0, 0, 100000, 100), 100.0, cell_m=0.25)
    with pytest.raises(ValueError, match="no extent"):
        ff.flat((5, 5, 5, 5), 100.0)


def test_paint_takes_cells_by_their_centres_and_reports_a_miss_honestly():
    floor = {"name": "F", "scale": 100.0}
    floor["bias_field"] = ff.flat((0, 0, 400, 300), 100.0)
    # An L-shaped (concave) room: the ray cast must not fill its notch.
    ell = [(0, 0), (400, 0), (400, 100), (100, 100), (100, 300), (0, 300)]
    assert ff.paint(floor, ell, 2.0) == 6
    assert ff.sample(floor, (350, 50)) == 2.0 and ff.sample(floor, (50, 250)) == 2.0
    assert ff.sample(floor, (350, 250)) == 1.0  # the notch
    # Smaller than a cell and between centres: nothing painted, and it says so.
    assert ff.paint(floor, [(90, 90), (110, 90), (110, 110), (90, 110)], 5.0) == 0
    with pytest.raises(ValueError, match="three points"):
        ff.paint(floor, [(0, 0), (1, 1)], 2.0)
    with pytest.raises(ValueError, match="positive"):
        ff.paint(floor, ell, 0)


def test_describe_tells_flat_from_shaped():
    floor = {"name": "F", "scale": 100.0}
    assert ff.describe(floor) is None
    floor["bias_field"] = ff.flat((0, 0, 300, 200), 100.0)
    assert ff.describe(floor)["flat"] is True
    ff.paint(floor, [(0, 0), (100, 0), (100, 100), (0, 100)], 1.5)
    d = ff.describe(floor)
    assert d["flat"] is False and d["shaped_cells"] == 1 and d["max"] == 1.5 and math.isclose(d["min"], 1.0)


def test_correcting_the_floors_scale_does_not_slide_the_painted_cells():
    """Found in use: alignment pins showed a floor's scale was 15 % out and it
    was corrected. The plan's pixels did not move and neither did the rooms,
    so a cell painted under the catwalk has to stay under the catwalk."""
    floor = {"name": "F", "scale": 127.8}
    floor["bias_field"] = ff.flat((0, 0, 1600, 1200), 127.8)
    catwalk = [(900, 600), (1300, 600), (1300, 900), (900, 900)]
    assert ff.paint(floor, catwalk, 1.5) > 0
    inside, outside = (1100, 750), (300, 300)
    before = (ff.sample(floor, inside), ff.sample(floor, outside))
    floor["scale"] = 107.4                                  # the correction
    assert (ff.sample(floor, inside), ff.sample(floor, outside)) == before == (1.5, 1.0)
    assert ff.paint(floor, catwalk, 2.0) > 0                # and painting still lands on the same cells
    assert ff.sample(floor, inside) == 2.0 and ff.sample(floor, outside) == 1.0


def test_a_field_laid_before_cell_px_was_recorded_still_reads():
    legacy = {"name": "F", "scale": 100.0, "bias_field": {"cell_m": 1.0, "x0": 0, "y0": 0, "values": [[1.0, 2.0]]}}
    assert ff.sample(legacy, (150, 50)) == 2.0
    assert ff.flat((0, 0, 300, 200), 100.0, cell_m=0.5)["cell_px"] == 50.0


def test_floor_bias_map_compares_this_floors_prior_with_anothers_place_by_place():
    import sextant
    from sextant import floor_field as ff
    room = {"zone_id": "r", "entity_id": "Room", "cords": [{"x": 0, "y": 0}, {"x": 400, "y": 0}, {"x": 400, "y": 200}, {"x": 0, "y": 200}]}
    up = {"name": "Up", "scale": 100.0, "zones": [room], "receivers": []}
    down = {"name": "Down", "scale": 100.0, "bias": 1.25, "zones": [dict(room)], "receivers": []}
    up["bias_field"] = ff.flat((0, 0, 400, 200), 100.0, cell_m=1.0)
    ff.paint(up, [(0, 0), (200, 0), (200, 200), (0, 200)], 2.5)   # the left half of Up is favoured
    layout = {"floor": [up, down]}
    out = sextant.floor_bias_map(layout, {}, "Up", "Down", cell_m=0.5)
    assert out["registered"] is False and len(out["cells"]) == 8 * 4
    right = [c for c in out["cells"] if c[0] > 350]
    left = [c for c in out["cells"] if c[0] < 50]
    assert all(abs(c[2] - 1 / 1.25) < 1e-6 for c in right)    # scalar only: Down leans
    assert all(abs(c[2] - 2.5 / 1.25) < 1e-6 for c in left)   # the painted half leans to Up
    assert sextant.floor_bias_map(layout, {}, "Up", "Nowhere") is None
