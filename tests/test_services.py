"""The sextant.* services (registered by _register_calibration_services) edit
the layout through the store and turn the calibration layer's refusals into
service errors."""
import asyncio
import re
import types
from pathlib import Path

import pytest

import sextant
from sextant import storage as st

from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Call:
    def __init__(self, **data):
        self.data = data


def _hass(tmp_path):
    hass = make_hass(tmp_path)
    hass.data["sextant"] = {}
    hass.states = types.SimpleNamespace(async_all=lambda domain=None: [])
    recs = [{"entity_id": s, "cords": {"x": i * 300, "y": 0}} for i, s in enumerate(("r0", "r1", "r2"))]
    run(st.save_layout(hass, {"floor": [{"name": "F", "scale": 100.0, "receivers": recs, "zones": [], "subzones": []}], "tuning": {}}))
    handlers = {}
    hass.services = types.SimpleNamespace(async_register=lambda domain, name, handler, schema=None, **_kw: handlers.__setitem__(name, handler))
    sextant._register_calibration_services(hass)
    return hass, handlers


def _receivers(hass):
    return {r["entity_id"]: r for r in st.get_layout(hass)["floor"][0]["receivers"]}


def test_every_documented_service_is_registered(tmp_path):
    _hass_, handlers = _hass(tmp_path)
    documented = set(re.findall(r"^([a-z_]+):", (Path(sextant.__file__).parent / "services.yaml").read_text(), re.M))
    assert set(handlers) == documented


def test_receiver_heights_are_written_with_a_default_and_range_checked(tmp_path):
    hass, h = _hass(tmp_path)
    run(h["set_receiver_heights"](_Call(heights={"r0": 1.2, "typo": 0.5}, default=0.9)))
    recs = _receivers(hass)
    assert recs["r0"]["height"] == 1.2 and recs["r1"]["height"] == 0.9 and recs["r2"]["height"] == 0.9
    with pytest.raises(Exception, match="between 0 and 10"):
        run(h["set_receiver_heights"](_Call(heights={"r0": 42})))
    assert _receivers(hass)["r0"]["height"] == 1.2


def test_thing_heights_accumulate(tmp_path):
    hass, h = _hass(tmp_path)
    run(h["set_thing_heights"](_Call(heights={"phone": 1.1})))
    run(h["set_thing_heights"](_Call(heights={"watch": 0.9})))
    assert st.get_layout(hass)["thing_heights"] == {"phone": 1.1, "watch": 0.9}


def test_tuning_is_validated_and_reset(tmp_path):
    hass, h = _hass(tmp_path)
    run(h["set_tuning"](_Call(settings={"zone_switch_secs": 45})))
    assert st.get_layout(hass)["tuning"]["zone_switch_secs"] == 45
    with pytest.raises(Exception, match="nope"):
        run(h["set_tuning"](_Call(settings={"nope": 1})))
    with pytest.raises(Exception):
        run(h["set_tuning"](_Call(settings={"zone_switch_secs": -5})))
    assert st.get_layout(hass)["tuning"]["zone_switch_secs"] == 45
    run(h["set_tuning"](_Call(reset=True)))
    assert "zone_switch_secs" not in st.get_layout(hass).get("tuning", {})  # reset drops the key entirely


def test_calibration_refusals_become_service_errors(tmp_path):
    hass, h = _hass(tmp_path)
    with pytest.raises(Exception, match="No floor named"):
        run(h["start_calibration"](_Call(floor="Nope", duration=600)))
    with pytest.raises(Exception, match="No calibration result"):
        run(h["apply_corrections"](_Call(floor="F")))
    run(h["cancel_calibration"](_Call()))  # nothing running: not an error
    assert run(h["reset_corrections"](_Call(floor="F"))) is None  # nothing to remove: not an error either


def _fielded(tmp_path):
    hass, h = _hass(tmp_path)
    layout = st.get_layout_for_edit(hass)
    # A 6 m x 4 m floor at 100 px/m with one room in its left half.
    layout["floor"][0]["receivers"] = [
        {"entity_id": s, "cords": {"x": x, "y": y}} for s, x, y in (("r0", 0, 0), ("r1", 600, 0), ("r2", 600, 400))
    ]
    layout["floor"][0]["zones"] = [{"entity_id": "Landing", "poly": True, "cords": [
        {"x": 0, "y": 0}, {"x": 300, "y": 0}, {"x": 300, "y": 400}, {"x": 0, "y": 400}]}]
    layout["floor"][0]["bias"] = 1.15
    run(st.save_layout(hass, layout))
    return hass, h


def test_a_flat_bias_field_changes_no_election_input(tmp_path):
    """Laying the field must be a no-op: that is the whole rollout plan."""
    hass, h = _fielded(tmp_path)
    before = {pt: sextant._floor_bias(st.get_layout(hass), "F", pt) for pt in ((10, 10), (299, 200), (590, 390))}
    out = run(h["set_floor_bias_field"](_Call(floor="F", action="flat")))
    assert out["field"] == {"rows": 4, "cols": 6, "cell_m": 1.0, "min": 1.0, "max": 1.0, "flat": True, "shaped_cells": 0}
    after = {pt: sextant._floor_bias(st.get_layout(hass), "F", pt) for pt in before}
    assert after == before and set(after.values()) == {1.15}  # exactly, not approximately


def test_painting_a_room_shapes_only_that_room(tmp_path):
    hass, h = _fielded(tmp_path)
    # No field yet: paint lays the flat one itself.
    out = run(h["set_floor_bias_field"](_Call(floor="F", action="paint", area="Landing", value=1.4)))
    assert out["cells_painted"] == 12 and out["field"]["shaped_cells"] == 12
    layout = st.get_layout(hass)
    assert sextant._floor_bias(layout, "F", (50, 50)) == pytest.approx(1.15 * 1.4)
    assert sextant._floor_bias(layout, "F", (550, 350)) == pytest.approx(1.15)
    # Between the last painted centre (x=250) and the first unpainted (x=350)
    # the bias ramps rather than steps, so a jittering fix cannot flicker it.
    mid = sextant._floor_bias(layout, "F", (300, 200))
    assert mid == pytest.approx(1.15 * 1.2)
    run(h["set_floor_bias_field"](_Call(floor="F", action="paint", area="Landing", value=0.5, mode="multiply")))
    assert sextant._floor_bias(st.get_layout(hass), "F", (50, 50)) == pytest.approx(1.15 * 0.7)
    run(h["set_floor_bias_field"](_Call(floor="F", action="clear")))
    assert "bias_field" not in st.get_layout(hass)["floor"][0]
    assert sextant._floor_bias(st.get_layout(hass), "F", (50, 50)) == 1.15


def test_bias_field_refusals_name_what_is_wrong(tmp_path):
    hass, h = _fielded(tmp_path)
    with pytest.raises(Exception, match="No floor named 'Attic'"):
        run(h["set_floor_bias_field"](_Call(floor="Attic", action="flat")))
    with pytest.raises(Exception, match="no room or spot named 'Nowhere'.*Landing"):
        run(h["set_floor_bias_field"](_Call(floor="F", action="paint", area="Nowhere", value=2)))
    with pytest.raises(Exception, match="exactly one of"):
        run(h["set_floor_bias_field"](_Call(floor="F", action="paint", value=2)))
    # A refused paint must not leave a half-laid field behind in the store.
    assert "bias_field" not in st.get_layout(hass)["floor"][0]


def test_every_service_is_an_admin_service(tmp_path):
    """Any signed-in user may call an ordinary service; these change the
    layout and calibration, so each must be registered as an admin service."""
    hass, handlers = _hass(tmp_path)
    assert handlers and set(handlers) == hass.data["_admin_services"]
