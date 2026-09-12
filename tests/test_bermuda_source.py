"""Tests for the direct Bermuda data path (bps.bermuda_source).

BPS used to source tracker<->receiver distances by scraping
``sensor.<device>_distance_to_<scanner>`` out of the state machine, which
forces every one of those entities to be enabled. These cover the replacement
path that reads Bermuda's in-memory snapshot instead, and — importantly — that
BPS still falls back to entity scraping when Bermuda's API is not available.
"""

import sys
import types

import pytest

import bps
from bps import bermuda_source

from test_positioning import SCALE, run


# --- fakes ------------------------------------------------------------------ #


class _RegEntry:
    def __init__(self, entity_id, unique_id, platform="bermuda"):
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.platform = platform


class _FakeRegistry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}


def _install_registry(monkeypatch, entries):
    """Point bermuda_source's `er.async_get` at a fake registry."""
    monkeypatch.setattr(
        bermuda_source.er, "async_get", lambda _hass: _FakeRegistry(entries), raising=False
    )


def _install_bermuda_api(monkeypatch, snapshot, coordinator=object()):
    """Install a fake `custom_components.bermuda.api` in sys.modules."""
    parent = sys.modules.get("custom_components") or types.ModuleType("custom_components")
    pkg = types.ModuleType("custom_components.bermuda")
    api = types.ModuleType("custom_components.bermuda.api")
    api.SNAPSHOT_VERSION = 1
    api.async_get_coordinator = lambda _hass: coordinator
    api.async_get_advert_snapshot = lambda _hass, *a, **kw: snapshot
    pkg.api = api
    parent.bermuda = pkg
    monkeypatch.setitem(sys.modules, "custom_components", parent)
    monkeypatch.setitem(sys.modules, "custom_components.bermuda", pkg)
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", api)
    return api


def _snapshot(distance=2.3, age=1.0, version=1, tracked=True):
    return {
        "version": version,
        "stamp": 1000.0,
        "devices": {
            "aa:bb:cc:dd:ee:ff": {
                "name": "Phone",
                "slug": "phone",
                "unique_id": "aa:bb:cc:dd:ee:ff",
                "tracked": tracked,
                "area_id": None,
                "area_name": None,
                "scanners": {
                    "11:22:33:44:55:66": {
                        "name": "Probe",
                        "slug": "probe",
                        "address": "11:22:33:44:55:66",
                        "unique_id": "99:88:77:66:55:44",
                        "address_wifi_mac": "99:88:77:66:55:44",
                        "area_id": None,
                        "area_name": None,
                        "distance": distance,
                        "distance_raw": distance,
                        "rssi": -70,
                        "stamp": 999.0,
                        "age": age,
                    }
                },
            }
        },
    }


# Bermuda keys the per-scanner entity unique_id on the WIFI mac, which is not
# the advert's scanner address — the join has to survive that.
_ENTRIES = [
    _RegEntry(
        "sensor.phone_distance_to_probe",
        "aa:bb:cc:dd:ee:ff_99:88:77:66:55:44_range",
    ),
    # The unfiltered twin must be ignored: it has no timeout of its own.
    _RegEntry(
        "sensor.phone_unfiltered_distance_to_probe",
        "aa:bb:cc:dd:ee:ff_99:88:77:66:55:44_range_raw",
    ),
    # A look-alike from another integration must never be picked up.
    _RegEntry(
        "sensor.mmwave_distance_to_detection_object",
        "whatever_range",
        platform="esphome",
    ),
]


# --- slug map --------------------------------------------------------------- #


def test_slug_map_joins_from_the_snapshot(monkeypatch):
    """Built entirely from the live snapshot - no entity registry involved,
    since with create_scanner_entities=False there may be no registry entries
    to read at all."""
    _install_bermuda_api(monkeypatch, _snapshot())

    mapping = bermuda_source.async_build_slug_map(object())

    assert mapping[("phone", "probe")] == ("aa:bb:cc:dd:ee:ff", "99:88:77:66:55:44")


def _two_device_snapshot():
    """One device (newcat) has only been heard by "probe" so far; another
    (oldcat) has also been heard by "kitchen". Both are tracked. Standalone
    (not layered on `_snapshot()`) so there is no risk of an unrelated fixture
    device also reporting a same-named scanner and making the borrow
    ambiguous."""
    base = {"version": 1, "stamp": 1000.0, "devices": {}}
    base["devices"]["newcat-addr"] = {
        "name": "Newcat",
        "slug": "newcat",
        "unique_id": "newcat-uid",
        "tracked": True,
        "area_id": None,
        "area_name": None,
        "scanners": {
            "probe-addr": {
                "name": "Probe",
                "slug": "probe",
                "address": "probe-addr",
                "unique_id": "probe-addr",
                "address_wifi_mac": None,
                "area_id": None,
                "area_name": None,
                "distance": 1.2,
                "distance_raw": 1.2,
                "rssi": -60,
                "stamp": 999.0,
                "age": 0.5,
            }
        },
    }
    base["devices"]["oldcat-addr"] = {
        "name": "Oldcat",
        "slug": "oldcat",
        "unique_id": "oldcat-uid",
        "tracked": True,
        "area_id": None,
        "area_name": None,
        "scanners": {
            "kitchen-addr": {
                "name": "Kitchen",
                "slug": "kitchen",
                "address": "kitchen-addr",
                "unique_id": "kitchen-addr",
                "address_wifi_mac": None,
                "area_id": None,
                "area_name": None,
                "distance": 3.1,
                "distance_raw": 3.1,
                "rssi": -70,
                "stamp": 999.0,
                "age": 1.5,
            }
        },
    }
    return base


def test_slug_map_borrows_scanner_coverage_across_devices(monkeypatch):
    """A device only recently tracked (a replaced collar, say) may only have
    been HEARD by a handful of scanners so far, while a long-tracked device
    has been heard by nearly all of them. Since a scanner's slug depends only
    on its own name, the map must offer every scanner seen by ANY device to
    every tracked device, not just the ones that device's own adverts happen
    to include yet — measured in production as 18 of 48 receivers reachable
    for a just-swapped tracker versus 45+ for everything else, which silently
    starved that tracker's solve."""
    _install_bermuda_api(monkeypatch, _two_device_snapshot())

    mapping = bermuda_source.async_build_slug_map(object())

    # newcat has not been heard by "kitchen" yet, but can still resolve
    # against it via oldcat's live advert of that scanner.
    assert mapping[("newcat", "kitchen")] == ("newcat-uid", "kitchen-addr")
    assert mapping[("newcat", "probe")] == ("newcat-uid", "probe-addr")
    assert mapping[("oldcat", "probe")] == ("oldcat-uid", "probe-addr")


def test_readings_resolve_for_a_scanner_the_device_has_no_advert_for(monkeypatch):
    """End-to-end: a device hasn't been heard by a scanner yet, but another
    tracked device has — the borrowed slug map must still produce nothing for
    the pair that genuinely has no reading (no advert, no reading), while a
    device WITH an advert for a scanner resolves normally."""
    _install_bermuda_api(monkeypatch, _two_device_snapshot())

    readings = bermuda_source.async_get_readings(object())

    assert readings[("oldcat", "kitchen")] == {"distance": 3.1, "age": 1.5}
    # newcat was never heard by "kitchen", so there is no live data to report
    # even though the slug map offers the pair - readings only exist where
    # Bermuda actually has an advert for that exact (device, scanner) pair.
    assert ("newcat", "kitchen") not in readings


# --- readings --------------------------------------------------------------- #


def test_readings_resolve_distance_and_age(monkeypatch):
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(distance=4.5, age=2.0))

    readings = bermuda_source.async_get_readings(object())

    assert readings == {("phone", "probe"): {"distance": 4.5, "age": 2.0}}


def test_readings_none_when_bermuda_absent(monkeypatch):
    """No Bermuda -> None, so the caller falls back to entity scraping."""
    monkeypatch.setitem(sys.modules, "custom_components.bermuda", None)
    assert bermuda_source.async_get_readings(object()) is None
    assert bermuda_source.async_api_available(object()) is False


def test_readings_none_on_unknown_snapshot_version(monkeypatch):
    """A future, incompatible snapshot must fall back rather than be misread."""
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(version=999))

    assert bermuda_source.async_get_readings(object()) is None


def test_readings_none_when_bermuda_not_set_up(monkeypatch):
    """Bermuda installed but no config entry -> snapshot is None."""
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, None, coordinator=None)

    assert bermuda_source.async_get_readings(object()) is None


def test_subscribe_uses_the_coordinator_listener(monkeypatch):
    """Push path is the coordinator's own listener — no bespoke event, nothing
    crossing the websocket."""
    calls = []

    class _Coord:
        def async_add_listener(self, cb):
            calls.append(cb)
            return "unsub"

    _install_bermuda_api(monkeypatch, _snapshot(), coordinator=_Coord())

    def _cb():
        pass

    assert bermuda_source.async_subscribe(object(), _cb) == "unsub"
    assert calls == [_cb]


# --- end-to-end through update_receiver_radii -------------------------------- #


class _NoStates:
    """A hass whose state machine is EMPTY — proves the direct path needs no
    entities at all."""

    states = type("S", (), {"get": staticmethod(lambda _eid: None)})()


def _run_radii_direct(monkeypatch, distance, age=1.0, max_age=None):
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(distance=distance, age=age))

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}}
    data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
    if max_age is not None:
        data["reading_max_age"] = max_age
    run(bps.update_receiver_radii(_NoStates(), {"entity": "phone", "data": data}))
    return rec


def test_radii_computed_with_no_entities_in_the_state_machine(monkeypatch):
    """The whole point: same result as the entity path, zero entities."""
    rec = _run_radii_direct(monkeypatch, 2.3)

    assert abs(rec["distance"] - 2.3) < 1e-9
    assert abs(rec["cords"]["r"] - SCALE * 2.3) < 1e-6


def test_direct_path_honours_stale_reading_max_age(monkeypatch):
    """`age` from the snapshot is seconds since the scanner last HEARD the
    device, and must gate exactly like the entity path's last_updated did."""
    rec = _run_radii_direct(monkeypatch, 2.3, age=120.0, max_age=30)

    assert "distance" not in rec


def test_direct_path_drops_receiver_when_bermuda_reports_no_distance(monkeypatch):
    """distance None is Bermuda's own 'this scanner can no longer hear it'
    timeout, and must take the receiver out of the solve."""
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(distance=None))

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}, "distance": 9.9}
    data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
    run(bps.update_receiver_radii(_NoStates(), {"entity": "phone", "data": data}))

    assert "distance" not in rec


def test_falls_back_to_entities_when_api_unavailable(monkeypatch):
    """With no Bermuda API, the original entity path must still work — this is
    what keeps existing installs running."""
    monkeypatch.setitem(sys.modules, "custom_components.bermuda", None)

    class St:
        state = "2.3"
        attributes = {"unit_of_measurement": "m"}

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}}
    data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
    run(bps.update_receiver_radii(Hass(), {"entity": "phone", "data": data}))

    assert abs(rec["distance"] - 2.3) < 1e-9


# --- discovery: which devices are trackable ---------------------------------- #


def test_tracked_prefixes_come_from_bermuda_not_from_entities(monkeypatch):
    """The regression that mattered: BPS decided what it could track by
    enumerating distance ENTITIES, so with them disabled it found nothing and
    logged "no devices present to track". Discovery must follow Bermuda's own
    tracked flag instead."""
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(tracked=True))

    assert bermuda_source.async_get_tracked_device_prefixes(object()) == {"phone"}


def test_untracked_devices_are_not_offered(monkeypatch):
    """Bermuda knows about hundreds of transient MACs; only the ones the user
    configured it to track (create_sensor) get distance entities, and only
    those should reach BPS."""
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(tracked=False))

    assert bermuda_source.async_get_tracked_device_prefixes(object()) == set()


def test_renamed_device_is_never_offered_under_more_than_one_prefix(monkeypatch):
    """Regression for a production incident: a device renamed after its
    entities were first created (a generic tag name replaced with a pet's
    name) used to have TWO device_prefixes in the entity registry pointing at
    the same physical device, since Bermuda's entity_ids are frozen at
    creation and never follow a later rename - which spawned a second,
    duplicate BPS tracker for what is physically one device.

    Reading the prefix directly from the snapshot's CURRENT slug makes this
    structurally impossible rather than merely deduplicated after the fact:
    stale entity_ids under the device's old name (still present in the
    registry, since Bermuda never deletes them on a rename) are not consulted
    at all, so there is nothing left that could produce a second prefix."""
    _install_registry(
        monkeypatch,
        [
            # A stale registry entry under the device's OLD name - exactly
            # what Bermuda leaves behind after a rename. Must be ignored.
            _RegEntry("sensor.oldtag_distance_to_probe", "aa:bb:cc:dd:ee:ff_probe-uid_range"),
        ],
    )
    snapshot = _snapshot()
    snapshot["devices"]["aa:bb:cc:dd:ee:ff"]["slug"] = "newname"
    _install_bermuda_api(monkeypatch, snapshot)

    prefixes = bermuda_source.async_get_tracked_device_prefixes(object())

    assert prefixes == {"newname"}


def test_tracked_prefixes_none_without_api(monkeypatch):
    monkeypatch.setitem(sys.modules, "custom_components.bermuda", None)
    assert bermuda_source.async_get_tracked_device_prefixes(object()) is None


def test_registry_enumeration_finds_disabled_entities(monkeypatch):
    """Registry entries persist while an entity is disabled - that is what lets
    every slug-parsing caller keep working with zero entities in the state
    machine. Unfiltered twins and other integrations stay excluded."""
    _install_registry(monkeypatch, _ENTRIES)

    ids = bermuda_source.async_registry_distance_entity_ids(object())

    assert ids == ["sensor.phone_distance_to_probe"]


def test_discovery_survives_with_an_empty_state_machine(monkeypatch):
    """End-to-end through BPS's own helper: zero entities in hass.states, yet
    the tracked device and its receiver slug are still discovered."""
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot())

    ids = bps._bermuda_distance_sensor_ids(_NoStates())
    assert ids == ["sensor.phone_distance_to_probe"]

    slugs, with_reading = bps._scanner_slugs_and_readings(_NoStates())
    assert slugs == {"probe"}
    # "has a live reading" now means Bermuda reports a distance, not that an
    # entity state is non-unknown.
    assert with_reading == {"probe"}


def test_receiver_with_no_distance_is_not_counted_as_live(monkeypatch):
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(distance=None))

    slugs, with_reading = bps._scanner_slugs_and_readings(_NoStates())
    assert slugs == {"probe"}
    assert with_reading == set()


def test_cache_is_per_hass_not_module_global(monkeypatch):
    """The cache must live in hass.data. A module global is shared by every
    hass in the process and leaks stale readings between them."""
    _install_registry(monkeypatch, _ENTRIES)
    _install_bermuda_api(monkeypatch, _snapshot(distance=1.0))

    class _HassWithData:
        def __init__(self):
            self.data = {}

    hass_a = _HassWithData()
    assert bermuda_source.async_get_readings(hass_a)[("phone", "probe")]["distance"] == 1.0
    assert bermuda_source._CACHE_KEY in hass_a.data

    # A different hass must not see hass_a's cached value.
    _install_bermuda_api(monkeypatch, _snapshot(distance=9.0))
    hass_b = _HassWithData()
    assert bermuda_source.async_get_readings(hass_b)[("phone", "probe")]["distance"] == 9.0
