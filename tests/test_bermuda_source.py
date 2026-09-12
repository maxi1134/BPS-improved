"""Tests for the direct Bermuda data path (bps.bermuda_source).

BPS used to source tracker<->receiver distances by scraping
``sensor.<device>_distance_to_<scanner>`` out of the state machine, which
forces every one of those entities to be enabled. These cover the replacement
path that reads Bermuda's in-memory snapshot instead, and — importantly — that
BPS still falls back to entity scraping when Bermuda's API is not available.

Everything here is built from the live snapshot alone. An earlier version of
this module (and these tests) mocked the entity registry too, on the theory
that Bermuda's frozen entity_ids were a more reliable join key than a
recomputed slug. That stopped being true once `create_scanner_entities=False`
became a supported Bermuda configuration: with no entities being created,
there is nothing in the registry to mock meaningfully, and every test that
still faked one would just be exercising code this module no longer has.
"""

import json
import sys
import types

import bps
from bps import bermuda_source

from test_positioning import SCALE, run


def _floor_json(scanner_slug):
    """A minimal coordinates_json with one floor and one placed receiver."""
    return json.dumps({"floor": [{"name": "F1", "receivers": [{"entity_id": scanner_slug, "scanner_uid": None}]}]})


# --- fakes ------------------------------------------------------------------ #


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
    _install_bermuda_api(monkeypatch, _snapshot(version=999))

    assert bermuda_source.async_get_readings(object()) is None


def test_readings_none_when_bermuda_not_set_up(monkeypatch):
    """Bermuda installed but no config entry -> snapshot is None."""
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
    _install_bermuda_api(monkeypatch, _snapshot(tracked=True))

    assert bermuda_source.async_get_tracked_device_prefixes(object()) == {"phone"}


def test_untracked_devices_are_not_offered(monkeypatch):
    """Bermuda knows about hundreds of transient MACs; only the ones the user
    configured it to track (create_sensor) get distance entities, and only
    those should reach BPS."""
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
    a stale entity_id under the device's old name is never consulted at all
    (this module does not read the entity registry for anything), so there
    is nothing left that could produce a second prefix - proven here simply
    by there being only one live slug, "newname", regardless of what history
    the (untouched) entity registry might still be holding onto."""
    snapshot = _snapshot()
    snapshot["devices"]["aa:bb:cc:dd:ee:ff"]["slug"] = "newname"
    _install_bermuda_api(monkeypatch, snapshot)

    prefixes = bermuda_source.async_get_tracked_device_prefixes(object())

    assert prefixes == {"newname"}


def test_tracked_prefixes_none_without_api(monkeypatch):
    monkeypatch.setitem(sys.modules, "custom_components.bermuda", None)
    assert bermuda_source.async_get_tracked_device_prefixes(object()) is None


def test_snapshot_distance_pairs_need_no_registry_entities(monkeypatch):
    """The receiver picker and debug views used to read these ids from the
    entity registry, which survived entities being disabled. That breaks
    entirely once create_scanner_entities=False stops any from being created
    at all - so this must work with a completely empty (unpatched) registry,
    built only from the live snapshot's tracked devices and their adverts."""
    _install_bermuda_api(monkeypatch, _snapshot())

    ids = bermuda_source.async_get_snapshot_distance_pairs(object())

    assert ids == ["sensor.phone_distance_to_probe"]


def test_snapshot_distance_pairs_none_without_api(monkeypatch):
    monkeypatch.setitem(sys.modules, "custom_components.bermuda", None)
    assert bermuda_source.async_get_snapshot_distance_pairs(object()) is None


def test_discovery_survives_with_an_empty_state_machine(monkeypatch):
    """End-to-end through BPS's own helper: zero entities in hass.states, yet
    the tracked device and its receiver slug are still discovered."""
    _install_bermuda_api(monkeypatch, _snapshot())

    ids = bps._bermuda_distance_sensor_ids(_NoStates())
    assert ids == ["sensor.phone_distance_to_probe"]

    slugs, with_reading = bps._scanner_slugs_and_readings(_NoStates())
    assert slugs == {"probe"}
    # "has a live reading" now means Bermuda reports a distance, not that an
    # entity state is non-unknown.
    assert with_reading == {"probe"}


def test_receiver_with_no_distance_is_not_counted_as_live(monkeypatch):
    _install_bermuda_api(monkeypatch, _snapshot(distance=None))

    slugs, with_reading = bps._scanner_slugs_and_readings(_NoStates())
    assert slugs == {"probe"}
    assert with_reading == set()


# --- debug views (receiver linking / beacon links) --------------------------- #


def test_scanner_linking_uses_live_readings_not_entity_state(monkeypatch):
    """The receivers debug view used to read hass.states directly, which
    cannot work at all once distance entities are disabled or, with
    create_scanner_entities=False, never created. It must report "live" from
    Bermuda's own reading instead."""
    _install_bermuda_api(monkeypatch, _snapshot(distance=2.3))
    coordinates_json = _floor_json("probe")

    result = bps._scanner_linking(_NoStates(), coordinates_json)

    assert len(result["placed"]) == 1
    row = result["placed"][0]
    assert row["status"] == "live"
    assert row["sensors"] == [{"device": "phone", "entity_id": "sensor.phone_distance_to_probe", "state": "2.3"}]


def test_scanner_linking_reports_silent_when_reading_times_out(monkeypatch):
    """distance=None (Bermuda's own timeout) is a receiver that is linked but
    not currently reporting - "silent", not "unmatched"."""
    _install_bermuda_api(monkeypatch, _snapshot(distance=None))
    coordinates_json = _floor_json("probe")

    result = bps._scanner_linking(_NoStates(), coordinates_json)

    assert result["placed"][0]["status"] == "silent"


def test_beacon_links_uses_live_readings_not_entity_state(monkeypatch):
    """Same data-source switch as the receivers view, for the beacons view."""
    _install_bermuda_api(monkeypatch, _snapshot(distance=2.3))

    result = bps._beacon_links(_NoStates())

    assert result == [{"device": "phone", "receivers": [{"scanner": "probe", "distance": 2.3, "unit": "m"}]}]


def test_cache_is_per_hass_not_module_global(monkeypatch):
    """The cache must live in hass.data. A module global is shared by every
    hass in the process and leaks stale readings between them."""
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
