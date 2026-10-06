"""presence / last_heard on every per-thing sensor.

here while heard within stale_after_secs, quiet until away_after_secs, away
after that - the Live page's own three states, so an automation can read
home/away off a sensor and fall back to GPS when it says away.
"""
import time

import sextant
from sextant import storage as st
from conftest import make_hass

LAYOUT = {"floor": [], "tuning": {"stale_after_secs": 120.0, "away_after_secs": 900.0}}


def test_the_three_states_from_age():
    now = 1_000_000.0
    assert sextant._presence_of(now - 10, now, LAYOUT) == "here"
    assert sextant._presence_of(now - 120, now, LAYOUT) == "here"
    assert sextant._presence_of(now - 121, now, LAYOUT) == "quiet"
    assert sextant._presence_of(now - 900, now, LAYOUT) == "quiet"
    assert sextant._presence_of(now - 901, now, LAYOUT) == "away"
    assert sextant._presence_of(None, now, LAYOUT) == "away", "never heard is away"


def test_last_heard_is_an_iso_timestamp_or_none():
    a = sextant._presence_attrs(1_790_000_000, 1_790_000_005, LAYOUT)
    assert a["presence"] == "here" and a["last_heard"].startswith("2026-09-")
    assert sextant._presence_attrs(None, 1.0, LAYOUT) == {"presence": "away", "last_heard": None}


class _Sensor:
    def __init__(self, state, attrs):
        self._state, self._attrs, self.hass = state, attrs, None


def _hass_with_sensors(ent):
    hass = make_hass()
    hass.data["sextant_sensors"] = {
        f"sensor.{ent}{s}": _Sensor("Office", {"kind": "room"}) for s in sextant.THING_SENSOR_SUFFIXES
    }
    return hass


def test_a_thing_that_goes_quiet_gets_the_transition_written_once(monkeypatch):
    ent = "thing"
    hass = _hass_with_sensors(ent)
    sextant._last_seen.clear(); sextant._presence_published.clear()
    now = time.time()
    sextant._last_seen[ent] = {"updated": now - 300}
    calls = []
    orig = sextant.update_sextant_sensor_state
    monkeypatch.setattr(sextant, "update_sextant_sensor_state", lambda h, e, s, a=None: (calls.append(e), orig(h, e, s, a)))
    sextant._publish_presence(hass, LAYOUT)
    assert len(calls) == len(sextant.THING_SENSOR_SUFFIXES)
    loc = hass.data["sextant_sensors"][f"sensor.{ent}_sextant_location"]
    assert loc._attrs["presence"] == "quiet" and loc._attrs["kind"] == "room", "other attributes kept"
    assert loc._state == "Office", "the state is untouched: it is still the last known place"
    calls.clear()
    sextant._publish_presence(hass, LAYOUT)
    assert calls == [], "the same presence is not written again every cycle"
    sextant._last_seen[ent] = {"updated": now - 2000}
    sextant._publish_presence(hass, LAYOUT)
    assert loc._attrs["presence"] == "away"


def test_a_thing_still_heard_is_written_once_not_every_cycle(monkeypatch):
    ent = "thing"
    hass = _hass_with_sensors(ent)
    sextant._last_seen.clear(); sextant._presence_published.clear()
    sextant._last_seen[ent] = {"updated": time.time() - 5}
    calls = []
    orig = sextant.update_sextant_sensor_state
    monkeypatch.setattr(sextant, "update_sextant_sensor_state", lambda h, e, s, a=None: (calls.append(e), orig(h, e, s, a)))
    sextant._publish_presence(hass, LAYOUT); sextant._publish_presence(hass, LAYOUT)
    assert len(calls) == len(sextant.THING_SENSOR_SUFFIXES)


def test_a_thing_with_sensors_but_no_remembered_sighting_is_away():
    """Jack's phone: not heard since before the sightings were first kept, so
    it has no entry - and its sensors must still say away, once."""
    ent = "never_heard"
    hass = _hass_with_sensors(ent)
    sextant._last_seen.clear(); sextant._presence_published.clear()
    sextant._publish_presence(hass, LAYOUT)
    loc = hass.data["sextant_sensors"][f"sensor.{ent}_sextant_location"]
    assert loc._attrs["presence"] == "away" and loc._attrs["last_heard"] is None
    assert sextant._presence_published[ent] == "away"


def test_a_thing_heard_but_not_located_still_reads_here():
    """Eilee's watch: heard, so its sighting is fresh, but too few proxies for
    a fix, so its sensors read unknown and the per-thing write never runs."""
    ent = "heard_unlocated"
    hass = _hass_with_sensors(ent)
    sextant._last_seen.clear(); sextant._presence_published.clear()
    sextant._last_seen[ent] = {"updated": time.time() - 5}
    sextant._publish_presence(hass, LAYOUT)
    loc = hass.data["sextant_sensors"][f"sensor.{ent}_sextant_location"]
    assert loc._attrs["presence"] == "here" and loc._attrs["last_heard"]


def test_forgetting_clears_the_published_presence():
    sextant._presence_published["gone"] = "away"
    sextant._forget_thing_state("gone")
    assert "gone" not in sextant._presence_published


def test_last_heard_is_not_recorded_but_presence_is():
    from sextant import sensor as sensor_mod
    assert "last_heard" in sensor_mod.CustomDistanceSensor._unrecorded_attributes
    assert "presence" not in sensor_mod.CustomDistanceSensor._unrecorded_attributes


# --- last_heard does not cost a state write every cycle ---------------------------

class _CountingSensor:
    def __init__(self):
        self.hass = object()
        self._state = None
        self._attrs = {}
        self.writes = []

    def async_write_ha_state(self):
        self.writes.append((self._state, dict(self._attrs)))


def _write(hass, state, heard, now, presence="here", **extra):
    sextant.update_sextant_sensor_state(
        hass, "sensor.e_sextant_room", state,
        {"presence": presence, "last_heard": heard, **extra}, now=now)


def test_a_cycle_that_only_moves_last_heard_writes_once_a_minute():
    hass = make_hass()
    s = _CountingSensor()
    hass.data["sextant_sensors"] = {"sensor.e_sextant_room": s}
    t0 = 1_000_000.0
    for k in range(5):                                   # 0, 15, 30, 45, 60 s
        _write(hass, "Kitchen", f"heard+{15 * k}", t0 + 15 * k)
    assert [w[1]["last_heard"] for w in s.writes] == ["heard+0", "heard+60"]
    assert s._attrs["last_heard"] == "heard+60"


def test_between_writes_the_entity_holds_what_was_written():
    """Home Assistant polls a sensor every 30 s and writes whatever it holds:
    an entity holding a newer last_heard than the state machine would put the
    write straight back."""
    hass = make_hass()
    s = _CountingSensor()
    hass.data["sextant_sensors"] = {"sensor.e_sextant_room": s}
    t0 = 1_000_000.0
    _write(hass, "Kitchen", "a", t0)
    _write(hass, "Kitchen", "b", t0 + 15)
    _write(hass, "Kitchen", "c", t0 + 30)
    assert len(s.writes) == 1 and s._attrs["last_heard"] == "a"


def test_anything_else_changing_is_written_at_once_with_the_exact_last_heard():
    hass = make_hass()
    s = _CountingSensor()
    hass.data["sextant_sensors"] = {"sensor.e_sextant_room": s}
    t0 = 1_000_000.0
    _write(hass, "Kitchen", "a", t0)
    _write(hass, "Dining", "b", t0 + 15)                       # the room changed
    _write(hass, "Dining", "c", t0 + 30, presence="quiet")     # the presence changed
    _write(hass, "Dining", "d", t0 + 45, presence="quiet", area_id="dining")   # another attribute
    _write(hass, "Dining", "e", t0 + 50, presence="quiet", area_id="dining")   # only last_heard: waits
    assert [(w[0], w[1]["last_heard"]) for w in s.writes] == [
        ("Kitchen", "a"), ("Dining", "b"), ("Dining", "c"), ("Dining", "d")]


def test_a_sensor_not_yet_in_home_assistant_keeps_the_value_without_writing():
    hass = make_hass()
    s = _CountingSensor()
    s.hass = None
    hass.data["sextant_sensors"] = {"sensor.e_sextant_room": s}
    _write(hass, "Kitchen", "a", 1_000_000.0)
    assert s.writes == [] and s._state == "Kitchen" and s._attrs["last_heard"] == "a"
