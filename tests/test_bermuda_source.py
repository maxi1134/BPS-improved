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


def _snapshot(distance=2.3, age=1.0, version=1):
    return {
        "version": version,
        "stamp": 1000.0,
        "devices": {
            "aa:bb:cc:dd:ee:ff": {
                "name": "Phone",
                "slug": "phone",
                "unique_id": "aa:bb:cc:dd:ee:ff",
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


def test_slug_map_joins_entity_ids_to_scanner_ids(monkeypatch):
    _install_registry(monkeypatch, _ENTRIES)

    mapping = bermuda_source.async_build_slug_map(object())

    assert mapping == {
        ("phone", "probe"): ("aa:bb:cc:dd:ee:ff", "99:88:77:66:55:44")
    }


def test_slug_map_survives_underscores_in_ibeacon_addresses(monkeypatch):
    """An iBeacon metadevice address contains underscores, so the unique_id
    must be split from the RIGHT or the device id is truncated."""
    ibeacon_uid = "426c7565_1_2"
    _install_registry(
        monkeypatch,
        [
            _RegEntry(
                "sensor.beacon_distance_to_probe",
                f"{ibeacon_uid}_99:88:77:66:55:44_range",
            )
        ],
    )

    mapping = bermuda_source.async_build_slug_map(object())

    assert mapping[("beacon", "probe")] == (ibeacon_uid, "99:88:77:66:55:44")


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
