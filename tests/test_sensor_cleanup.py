"""Untracking a device removes its Sextant sensors (sensor.py reconcile + removal).

Before this, untrack only told Bermuda; the four per-thing sensors and the
"<device> (Sextant)" device stayed in the registry as `unavailable` forever
(twenty of them on the reference house after four Tiles and a phone were
untracked). The removal is used by the untrack command, the reconcile by the
positioning loop against Bermuda's full tracked set.
"""
import types

import sextant  # noqa: F401  (installs the package on the path)
from sextant import sensor as sn
from sextant.const import ACCURACY_ENTITY_ID

from conftest import make_hass
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


class _States:
    def __init__(self):
        self.states = {}

    def get(self, entity_id):
        return self.states.get(entity_id)

    def async_remove(self, entity_id):
        self.states.pop(entity_id, None)

    def async_all(self, domain=None):
        return list(self.states.values())


def _house(tmp_path, things=("phone", "tile_1")):
    """A hass whose registry, cache and states carry four sensors per thing."""
    hass = make_hass(tmp_path)
    hass.states = _States()
    reg, devs = er.async_get(hass), dr.async_get(hass)
    hass.data["sextant_sensors"] = {}
    for thing in things:
        devs.add({("sextant", thing)}, device_id=f"dev_{thing}")
        for suffix, _label in sn.SENSOR_KINDS:
            entity_id, unique_id = f"sensor.{thing}_{suffix}", f"{suffix}_{thing}"
            reg.add(entity_id, unique_id=unique_id, device_id=f"dev_{thing}")
            hass.data["sextant_sensors"][entity_id] = types.SimpleNamespace(unique_id=unique_id)
            hass.states.states[entity_id] = "Kitchen"
    devs.add({("sextant", "sextant_system")}, device_id="dev_sys")
    reg.add(ACCURACY_ENTITY_ID, unique_id="sextant_position_accuracy", device_id="dev_sys")
    hass.data["sextant_sensors"][ACCURACY_ENTITY_ID] = types.SimpleNamespace(unique_id="sextant_position_accuracy")
    return hass


def _entries_of(hass, thing):
    return [e for e in er.async_get(hass).entities if e.startswith(f"sensor.{thing}_")]


def test_thing_of_unique_id_recognises_every_kind_and_nothing_else():
    assert sn.thing_of_unique_id("sextant_room_phone") == "phone"
    assert sn.thing_of_unique_id("sextant_nearest_room_tile_1") == "tile_1"
    assert sn.thing_of_unique_id("sextant_spot_a_b") == "a_b"
    assert sn.thing_of_unique_id("sextant_position_accuracy") is None
    assert sn.thing_of_unique_id(None) is None


def test_remove_sensors_for_things_drops_cache_registry_state_and_device(tmp_path):
    hass = _house(tmp_path)
    assert sn.remove_sensors_for_things(hass, ["phone"]) == len(sn.SENSOR_KINDS)
    assert _entries_of(hass, "phone") == []
    assert not any(k.startswith("sensor.phone_") for k in hass.data["sextant_sensors"])
    assert not any(k.startswith("sensor.phone_") for k in hass.states.states)
    assert dr.async_get(hass).async_get_device(identifiers={("sextant", "phone")}) is None
    # The other thing and the global diagnostic are untouched.
    assert len(_entries_of(hass, "tile_1")) == len(sn.SENSOR_KINDS)
    assert ACCURACY_ENTITY_ID in er.async_get(hass).entities and "dev_sys" in dr.async_get(hass).devices
    # Nothing to remove is not an error.
    assert sn.remove_sensors_for_things(hass, ["phone", "", None]) == 0


def test_device_stays_while_a_foreign_entity_still_lives_on_it(tmp_path):
    hass = _house(tmp_path)
    er.async_get(hass).add("sensor.phone_battery", platform="bermuda", unique_id="b", device_id="dev_phone")
    sn.remove_sensors_for_things(hass, ["phone"])
    assert dr.async_get(hass).async_get_device(identifiers={("sextant", "phone")}) is not None


def test_prune_acts_only_on_a_tracked_set_reported_twice(tmp_path):
    hass = _house(tmp_path)
    # First sighting of the set: remembered, nothing removed yet.
    assert sn.prune_sensors_for_untracked(hass, {"phone"}) == 0
    assert len(_entries_of(hass, "tile_1")) == len(sn.SENSOR_KINDS)
    # Same set again: tile_1 is an orphan and goes, phone stays.
    assert sn.prune_sensors_for_untracked(hass, {"phone"}) == len(sn.SENSOR_KINDS)
    assert _entries_of(hass, "tile_1") == [] and len(_entries_of(hass, "phone")) == len(sn.SENSOR_KINDS)
    assert dr.async_get(hass).async_get_device(identifiers={("sextant", "tile_1")}) is None
    # Steady state: the same set is a no-op (no registry scan, nothing removed).
    assert sn.prune_sensors_for_untracked(hass, {"phone"}) == 0


def test_prune_never_acts_on_an_empty_or_flapping_set(tmp_path):
    hass = _house(tmp_path)
    for tracked in (set(), None, frozenset()):
        assert sn.prune_sensors_for_untracked(hass, tracked) == 0
        assert sn.prune_sensors_for_untracked(hass, tracked) == 0
    # A set that changes between cycles is never acted on.
    assert sn.prune_sensors_for_untracked(hass, {"phone"}) == 0
    assert sn.prune_sensors_for_untracked(hass, {"phone", "tile_1"}) == 0
    assert sn.prune_sensors_for_untracked(hass, {"phone"}) == 0
    assert len(_entries_of(hass, "tile_1")) == len(sn.SENSOR_KINDS) and len(_entries_of(hass, "phone")) == len(sn.SENSOR_KINDS)


def test_prune_finds_orphans_that_only_exist_in_the_registry(tmp_path):
    """The real-world case: registry rows with no live object (Bermuda was not up
    when the sensor platform reconciled at setup)."""
    hass = _house(tmp_path, things=("phone",))
    reg = er.async_get(hass)
    for suffix, _label in sn.SENSOR_KINDS:
        reg.add(f"sensor.tile_9_{suffix}", unique_id=f"{suffix}_tile_9", device_id="dev_tile_9")
    dr.async_get(hass).add({("sextant", "tile_9")}, device_id="dev_tile_9")
    sn.prune_sensors_for_untracked(hass, {"phone"})
    assert sn.prune_sensors_for_untracked(hass, {"phone"}) == len(sn.SENSOR_KINDS)
    assert _entries_of(hass, "tile_9") == [] and "dev_tile_9" not in dr.async_get(hass).devices


def test_device_lookup_uses_the_per_entry_api_when_the_core_has_it(tmp_path):
    hass = _house(tmp_path, things=("phone",))
    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [types.SimpleNamespace(entry_id="entry-1")])
    assert sn.remove_sensors_for_things(hass, ["phone"]) == len(sn.SENSOR_KINDS)
    devs = dr.async_get(hass)
    assert devs.by_identifier_calls == 1 and "dev_phone" not in devs.devices
