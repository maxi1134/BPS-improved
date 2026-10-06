"""The sensor platform: setup creates a room/floor/nearest-room/spot sensor per
tracked device plus the accuracy diagnostic, migrates pre-3.8 names, drops
legacy duplicated-name entities and stale entries, and picks up things that
appear later through the state bus or Bermuda's coordinator."""
import asyncio
import types

import sextant  # noqa: F401
from sextant import sensor as sn
from sextant.const import ACCURACY_ENTITY_ID, GLOBAL_ENTITY_IDS, UNTRACKED_ENTITY_ID

from conftest import make_hass
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _States:
    def __init__(self):
        self.states = {}

    def async_all(self):
        return [types.SimpleNamespace(entity_id=e) for e in self.states]

    def async_remove(self, entity_id):
        self.states.pop(entity_id, None)


class _Bus:
    def __init__(self):
        self.listeners = {}

    def async_listen(self, event, cb):
        self.listeners[event] = cb
        return lambda: self.listeners.pop(event, None)


def _hass(tmp_path, tracked=None):
    hass = make_hass(tmp_path)
    hass.states = _States()
    hass.bus = _Bus()
    hass._tracked = tracked
    return hass


def _patch_bermuda(monkeypatch, hass, subscribe=None):
    monkeypatch.setattr(sn.bermuda_source, "async_get_tracked_device_prefixes", lambda h: h._tracked)
    monkeypatch.setattr(sn.bermuda_source, "async_subscribe", subscribe or (lambda h, cb: None))


def _added():
    calls = []

    def add(entities, update_before_add=False):
        calls.extend(entities)
    return calls, add


def test_setup_creates_sensors_and_the_accuracy_diagnostic(tmp_path, monkeypatch):
    hass = _hass(tmp_path, tracked={"cat", "watch"})
    _patch_bermuda(monkeypatch, hass)
    added, add = _added()
    run(sn.async_setup_entry(hass, None, add))
    ids = sorted(e.entity_id for e in added)
    assert ids == sorted(list(GLOBAL_ENTITY_IDS) + [f"sensor.{t}_{k}" for t in ("cat", "watch") for k, _ in sn.SENSOR_KINDS])
    cat_room = hass.data["sextant_sensors"]["sensor.cat_sextant_room"]
    assert cat_room.name == "cat Sextant Room" and cat_room.unique_id == "sextant_room_cat"
    assert cat_room.state == "unknown" and cat_room.extra_state_attributes == {"area_id": None}
    assert cat_room._attr_device_info["identifiers"] == {("sextant", "cat")}
    acc = hass.data["sextant_sensors"][ACCURACY_ENTITY_ID]
    assert acc.native_value is None and acc.extra_state_attributes == {}
    assert hass.data["sextant_add_entities"] is add
    # Second setup: nothing is created twice, the old listener is replaced.
    run(sn.async_setup_entry(hass, None, add))
    # One per kind per thing, plus the global sensors (accuracy, untracked
    # people). Derived rather than counted, so adding a kind does not fail here.
    assert len(added) == 2 * len(sn.SENSOR_KINDS) + len(GLOBAL_ENTITY_IDS) and "state_changed" in hass.bus.listeners
    unt = hass.data["sextant_sensors"][UNTRACKED_ENTITY_ID]
    assert unt.native_value is None and unt._attr_unique_id == "sextant_untracked_people"


def test_setup_nests_under_the_bermuda_device_when_it_can_find_one(tmp_path, monkeypatch):
    hass = _hass(tmp_path, tracked={"cat"})
    _patch_bermuda(monkeypatch, hass)
    dev = dr.async_get(hass).add({("other", "x"), ("bermuda", "aa:bb")})
    er.async_get(hass).add("sensor.cat_distance_to_kitchen", platform="bermuda", device_id=dev.id)
    added, add = _added()
    run(sn.async_setup_entry(hass, None, add))
    assert hass.data["sextant_sensors"]["sensor.cat_sextant_floor"]._attr_device_info["via_device"] == ("bermuda", "aa:bb")
    assert sn.find_bermuda_via_device(hass, "dog") is None


def test_setup_migrates_renamed_kinds_and_removes_legacy_and_stale_entries(tmp_path, monkeypatch):
    hass = _hass(tmp_path, tracked={"cat"})
    _patch_bermuda(monkeypatch, hass)
    reg = er.async_get(hass)
    reg.add("sensor.cat_sextant_zone", unique_id="sextant_zone_cat")  # pre-3.8 name
    reg.add("sensor.cat_sextant_sub_zone", unique_id="sextant_sub_zone_cat")
    reg.add("sensor.cat_sextant_room", unique_id="sextant_room_cat")  # blocks the zone rename
    reg.add("sensor.cat_cat_sextant_floor")  # legacy duplicated-name id
    hass.states.states["sensor.cat_cat_sextant_floor"] = "x"
    reg.add("sensor.gone_sextant_floor", unique_id="sextant_floor_gone")  # no longer tracked
    reg.add("sensor.cat_sextant_floor_2", unique_id="sextant_floor_cat")  # wrong entity_id for its unique_id
    reg.add(ACCURACY_ENTITY_ID, unique_id="sextant_position_accuracy")
    reg.add("sensor.someone_else", platform="other", unique_id="sextant_zone_other")
    added, add = _added()
    run(sn.async_setup_entry(hass, None, add))
    ids = set(reg.entities)
    assert "sensor.cat_sextant_spot" in ids and "sensor.cat_sextant_sub_zone" not in ids
    assert "sensor.cat_sextant_zone" not in ids  # rename blocked -> removed
    assert "sensor.cat_cat_sextant_floor" not in ids and "sensor.cat_cat_sextant_floor" not in hass.states.states
    assert "sensor.gone_sextant_floor" not in ids
    assert "sensor.cat_sextant_floor" in ids and "sensor.cat_sextant_floor_2" not in ids
    assert ACCURACY_ENTITY_ID in ids and "sensor.someone_else" in ids


def test_legacy_entity_id_detection():
    assert sn.is_legacy_sextant_entity_id("sensor.cat_cat_sextant_floor")
    assert sn.is_legacy_sextant_entity_id("sensor.my_watch_my_watch_sextant_zone")
    assert not sn.is_legacy_sextant_entity_id("sensor.cat_sextant_floor")
    assert not sn.is_legacy_sextant_entity_id("sensor.cat_dog_sextant_floor")
    assert not sn.is_legacy_sextant_entity_id("sensor.cat_cat_sextant_spot")
    assert not sn.is_legacy_sextant_entity_id("binary_sensor.cat_cat_sextant_floor")
    assert not sn.is_legacy_sextant_entity_id("sensor.cat_cat_other")


def test_filtered_entities_fall_back_to_bermuda_distance_states(tmp_path, monkeypatch):
    hass = _hass(tmp_path, tracked=None)
    _patch_bermuda(monkeypatch, hass)
    reg = er.async_get(hass)
    reg.add("sensor.cat_distance_to_kitchen", platform="bermuda")
    reg.add("sensor.mmwave_distance_to_detection_object", platform="esphome")
    for e in ("sensor.cat_distance_to_kitchen", "sensor.cat_distance_to_hall", "sensor.mmwave_distance_to_detection_object", "sensor.unrelated"):
        hass.states.states[e] = "1"
    assert sn.get_filtered_entities(hass) == ["cat"]
    hass._tracked = {"b", "a"}
    assert sn.get_filtered_entities(hass) == ["a", "b"]


def test_new_distance_entity_and_bermuda_updates_create_sensors_later(tmp_path, monkeypatch):
    hass = _hass(tmp_path, tracked=None)
    subs = []

    def subscribe(h, cb):
        subs.append(cb)
        return lambda: subs.clear()

    _patch_bermuda(monkeypatch, hass, subscribe)
    added, add = _added()
    run(sn.async_setup_entry(hass, None, add))
    assert sorted(e.entity_id for e in added) == sorted(GLOBAL_ENTITY_IDS)

    # A brand-new Bermuda distance entity appears on the state bus.
    er.async_get(hass).add("sensor.dog_distance_to_hall", platform="bermuda")
    hass.states.states["sensor.dog_distance_to_hall"] = "2"
    listener = hass.bus.listeners["state_changed"]
    listener(types.SimpleNamespace(data={"entity_id": "sensor.dog_distance_to_hall", "old_state": "1"}))  # an update: ignored
    assert len(added) == len(GLOBAL_ENTITY_IDS)
    listener(types.SimpleNamespace(data={"entity_id": "sensor.dog_distance_to_hall", "old_state": None}))
    assert sorted(e.entity_id for e in added if e.entity_id not in GLOBAL_ENTITY_IDS) == sorted(f"sensor.dog_{k}" for k, _ in sn.SENSOR_KINDS)

    # Bermuda's coordinator reports a new tracked set: only the change is acted on.
    hass._tracked = {"dog", "cat"}
    subs[0]()
    assert "sensor.cat_sextant_room" in hass.data["sextant_sensors"]
    n = len(added)
    subs[0]()  # same set: no registry walk, nothing added
    assert len(added) == n

    # Unloading: late events are ignored.
    hass.data["sextant_sensors"] = None
    listener(types.SimpleNamespace(data={"entity_id": "sensor.x_distance_to_y", "old_state": None}))
    subs[0]()
    assert len(added) == n


def test_subscribe_retries_until_bermuda_is_there(tmp_path, monkeypatch):
    hass = _hass(tmp_path, tracked=set())
    attempts = []
    monkeypatch.setattr(sn.bermuda_source, "async_get_tracked_device_prefixes", lambda h: h._tracked)
    monkeypatch.setattr(sn.bermuda_source, "async_subscribe", lambda h, cb: attempts.append(cb) or (None if len(attempts) < 2 else (lambda: None)))
    scheduled = []
    monkeypatch.setattr(sn, "async_call_later", lambda h, delay, fn: scheduled.append((delay, fn)) or "unsub")
    run(sn.async_setup_entry(hass, None, lambda *a, **k: None))
    assert len(attempts) == 1 and scheduled and scheduled[0][0] == 30
    assert hass.data["sextant_bermuda_retry_unsub"] == "unsub"
    scheduled[0][1]()  # the retry lands: subscribed
    assert len(attempts) == 2 and callable(hass.data["sextant_bermuda_listener_unsub"])
    # platform setup from YAML goes the same way
    run(sn.async_setup_platform(hass, {}, lambda *a, **k: None))


def test_a_never_seen_thing_still_has_usable_location_attributes():
    """
    An entity that has never been published must not report missing attributes.

    A thing added but not yet heard - a FindMy tag with a flat battery, say -
    exists as entities from the moment it is tracked. An automation reading
    state_attr(..., "kind") on one should get the shape it will always get
    rather than None, which is indistinguishable from a bug in its own template.
    """
    loc = sn.CustomDistanceSensor("tag Sextant Location", "sextant_location_tag",
                                  "sensor.tag_sextant_location", "tag",
                                  attrs=sn.INITIAL_ATTRS["sextant_location"])
    assert loc.state == "unknown"
    assert loc.extra_state_attributes == {"kind": "room", "room": "unknown", "spot": None, "floor": "unknown",
                                          "area_id": None, "floor_id": None}

    # And the seeded dict must not be shared between sensors.
    other = sn.CustomDistanceSensor("x", "sextant_location_x", "sensor.x_sextant_location", "x",
                                    attrs=sn.INITIAL_ATTRS["sextant_location"])
    other._attrs["room"] = "Kitchen"
    assert loc.extra_state_attributes["room"] == "unknown"
    assert sn.INITIAL_ATTRS["sextant_location"]["room"] == "unknown"


def test_sextant_sensors_are_pushed_not_polled():
    """Polled, Home Assistant wrote each sensor's state every 30 s whatever
    the cycle had decided to write."""
    from sextant import sensor as sensor_mod

    for cls in (sensor_mod.CustomDistanceSensor, sensor_mod.SextantUntrackedSensor, sensor_mod.SextantAccuracySensor):
        assert cls._attr_should_poll is False
