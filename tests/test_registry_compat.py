"""The device-registry deprecations HA 2026.9/2026.10 started warning about.

Two things BPS did now log on every start, and both stop working in 2027:

* ``dev_reg.devices.values()`` - from 2026.9 ``devices`` is a read-only
  collection of entries, and every mapping use warns (gone in 2027.9).
* ``DeviceInfo["via_device"]`` - deprecated in 2026.9 for ``via_device_id``
  (gone in 2027.8, and with it every per-tracker sensor, because the platform
  refuses a device it can't create).

Neither fix may break an older core, where the new spellings either yield
device ids instead of entries or crash the entity add outright. The fakes
below copy the two shapes HA actually ships, so each test runs against both.
"""
import importlib
import sys
from collections import UserDict

import pytest

import bps
from bps import sensor as sn
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er


# --------------------------------------------------------------------------- #
# Fakes shaped like HA's own containers
# --------------------------------------------------------------------------- #

class Device(dr.DeviceEntry):
    """A main DeviceEntry. Like the real one (a slotted attrs class with no
    __getattr__), it has NO parent_device_id on any core - that field exists
    only on the 2026.9+ ChildDeviceEntry - so reading it bare would raise."""

    def __init__(self, id, name, identifiers=(), composite_device_id=None,
                 name_by_user=None, legacy=False):
        self.id = id
        self.name = name
        self.name_by_user = name_by_user
        self.identifiers = set(identifiers)
        if not legacy:  # composite_device_id arrived with the 2026.8 split
            self.composite_device_id = composite_device_id


class ChildDevice:
    """2026.9+ ChildDeviceEntry: a sibling of DeviceEntry, not a subclass."""

    def __init__(self, id, parent_device_id):
        self.id = id
        self.parent_device_id = parent_device_id
        self.identifiers = {("bermuda", "child")}


class LegacyDevices(UserDict):
    """<= 2026.8: a UserDict keyed by device id. Iterating yields IDS."""


class DevicesView:
    """2026.9+: _DeprecatedDeviceRegistryItemsView. Iterating yields ENTRIES;
    every mapping use reports - modelled here as a recorded call, so a test can
    assert none happened. Attribute probes (hasattr/getattr) report too, which
    is why the fix must not feature-detect that way."""

    def __init__(self, entries, reports):
        self._entries = dict(entries)
        self._reports = reports

    def __iter__(self):
        return iter(self._entries.values())

    def __len__(self):
        return len(self._entries)

    def _report(self, what):
        self._reports.append(what)

    def __getitem__(self, key):
        self._report(f"[{key!r}]")
        return self._entries[key]

    def __contains__(self, key):
        self._report(f"in {key!r}")
        return key in self._entries

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self._report(f".{name}")
        return getattr(self._entries, name)


class DevReg:
    def __init__(self, devices, shape, children=()):
        self.reports = []
        by_id = {d.id: d for d in devices}
        self._lookup = dict(by_id)
        self._lookup.update({c.id: c for c in children})
        self.devices = LegacyDevices(by_id) if shape == "legacy" else DevicesView(by_id, self.reports)

    def async_get(self, device_id):  # NOT deprecated on any core
        return self._lookup.get(device_id)


class Ent:
    def __init__(self, entity_id, device_id=None, platform="esphome"):
        self.entity_id = entity_id
        self.device_id = device_id
        self.platform = platform


class EntReg:
    def __init__(self, ents):
        self.entities = UserDict({e.entity_id: e for e in ents})


@pytest.fixture
def registries(monkeypatch):
    """Install a device and entity registry for the module under test."""
    state = {}

    def install(dev_reg, ent_reg):
        state["dev"], state["ent"] = dev_reg, ent_reg
        # The conftest stub's slugify is the identity; device names here need
        # HA's real behaviour ("Kitchen Proxy" -> "kitchen_proxy").
        monkeypatch.setattr(bps, "slugify", lambda s: "_".join(s.lower().split()))
        monkeypatch.setattr(dr, "async_get", lambda hass: state["dev"], raising=False)
        monkeypatch.setattr(er, "async_get", lambda hass: state["ent"], raising=False)
        return dev_reg

    return install


SHAPES = ["legacy", "view"]


# --------------------------------------------------------------------------- #
# dev_reg.devices: iterate entries on every core, never trip the report
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("shape", SHAPES)
def test_every_device_is_reached_without_a_report(shape):
    devs = [Device("d1", "Kitchen"), Device("d2", "Hall")]
    reg = DevReg(devs, shape)
    got = list(bps._iter_registry_devices(reg))
    assert sorted(d.id for d in got) == ["d1", "d2"]
    assert all(isinstance(d, dr.DeviceEntry) for d in got), "an id leaked through"
    assert reg.reports == []


def test_the_old_spelling_is_what_reported():
    # Positive control: the fake must report .values() the way HA 2026.10.1's
    # _DeprecatedDeviceRegistryItemsView.__getattr__ does - the user's log line.
    # If this goes red the fake has lost its reporter, and every
    # `reports == []` assertion in this file passes vacuously.
    reg = DevReg([Device("d1", "Kitchen")], "view")
    list(reg.devices.values())
    assert reg.reports == [".values"]


@pytest.mark.parametrize("shape", SHAPES)
def test_availability_maps_are_unchanged_for_a_plain_registry(registries, shape):
    reg = registries(
        DevReg([Device("d1", "Kitchen Proxy"), Device("d2", "Hall Proxy")], shape),
        EntReg([Ent("binary_sensor.kitchen_proxy_status", "d1"),
                Ent("sensor.hall_proxy_rssi", "d2"), Ent("sensor.loose")]),
    )
    slug_to_device, device_entities = bps._build_device_availability(object())
    assert slug_to_device == {"kitchen_proxy": "d1", "hall_proxy": "d2"}
    assert device_entities == {"d1": ["binary_sensor.kitchen_proxy_status"],
                               "d2": ["sensor.hall_proxy_rssi"]}
    assert reg.reports == []


# --------------------------------------------------------------------------- #
# The 2026.8 split: one physical device, now one copy per integration
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")])
def test_copies_of_one_split_device_stay_ambiguous(registries, order):
    # Pooling them was tried and rejected: the sibling copy of a proxy is
    # usually a router's device_tracker, never "unavailable", so a dead proxy
    # read alive - and the Lovelace card this tier mirrors can't see the split,
    # so the panel and the card would disagree. Dropped like any shared name.
    copies = {
        "a": Device("esp", "Kitchen Proxy", composite_device_id="orig"),
        "b": Device("router", "Kitchen Proxy", composite_device_id="orig"),
    }
    registries(DevReg([copies[k] for k in order], "view"), EntReg([]))
    slug_to_device, _ = bps._build_device_availability(object())
    assert "kitchen_proxy" not in slug_to_device


@pytest.mark.parametrize("shape", SHAPES)
def test_two_unrelated_devices_with_one_name_stay_ambiguous(registries, shape):
    legacy = shape == "legacy"
    registries(
        DevReg([Device("d1", "Kitchen Proxy", legacy=legacy),
                Device("d2", "Kitchen Proxy", legacy=legacy)], shape),
        EntReg([]),
    )
    slug_to_device, _ = bps._build_device_availability(object())
    assert "kitchen_proxy" not in slug_to_device


# --------------------------------------------------------------------------- #
# via_device -> via_device_id
# --------------------------------------------------------------------------- #

def _bermuda_house(shape="view", device=None, children=()):
    device = device or Device("berm-1", "Phone", identifiers={("bermuda", "aa:bb")})
    dev_reg = DevReg([device], shape, children=children)
    ent_reg = EntReg([Ent("sensor.phone_distance_to_kitchen", device.id, platform="bermuda")])
    return dev_reg, ent_reg


@pytest.mark.parametrize("shape", SHAPES)
def test_the_bermuda_device_is_found_by_id_and_identifier(registries, shape):
    device = Device("berm-1", "Phone", identifiers={("bermuda", "aa:bb")},
                    legacy=shape == "legacy")
    registries(*_bermuda_house(shape, device=device))
    assert sn.find_bermuda_via_device(object(), "phone") == ("berm-1", ("bermuda", "aa:bb"))


def test_a_device_missing_from_the_registry_is_not_offered(registries):
    # An unknown via_device_id is fatal for the entity (the old via_device only
    # logged), so nothing that isn't in the registry right now may be returned.
    dev_reg, _ = _bermuda_house()
    registries(dev_reg, EntReg([Ent("sensor.phone_distance_to_kitchen", "gone", platform="bermuda")]))
    assert sn.find_bermuda_via_device(object(), "phone") is None


def test_a_child_device_links_its_parent_instead(registries):
    # 2026.9 child devices can't be via devices (DeviceInfoError).
    parent = Device("berm-1", "Phone", identifiers={("bermuda", "aa:bb")})
    child = ChildDevice("child-1", parent_device_id="berm-1")
    dev_reg = DevReg([parent], "view", children=[child])
    registries(dev_reg, EntReg([Ent("sensor.phone_distance_to_kitchen", "child-1", platform="bermuda")]))
    assert sn.find_bermuda_via_device(object(), "phone") == ("berm-1", ("bermuda", "aa:bb"))


def test_an_orphaned_child_is_not_offered(registries):
    child = ChildDevice("child-1", parent_device_id="nowhere")
    dev_reg = DevReg([], "view", children=[child])
    registries(dev_reg, EntReg([Ent("sensor.phone_distance_to_kitchen", "child-1", platform="bermuda")]))
    assert sn.find_bermuda_via_device(object(), "phone") is None


def test_only_bermudas_own_sensors_count(registries):
    dev_reg, _ = _bermuda_house()
    registries(dev_reg, EntReg([Ent("sensor.phone_distance_to_kitchen", "berm-1", platform="esphome")]))
    assert sn.find_bermuda_via_device(object(), "phone") is None


def _sensor(via):
    return sn.CustomDistanceSensor("phone BPS Zone", "bps_zone_phone", "sensor.phone_bps_zone",
                                   "phone", via)


@pytest.mark.parametrize("supported, key, value", [
    (True, "via_device_id", "berm-1"),
    (False, "via_device", ("bermuda", "aa:bb")),
])
def test_exactly_one_via_key_for_the_running_core(monkeypatch, supported, key, value):
    # 2026.8+ raises when given both; before 2026.8 via_device_id is a TypeError
    # inside async_get_or_create(**info). Either way the sensor is not added.
    monkeypatch.setattr(sn, "VIA_DEVICE_ID_SUPPORTED", supported)
    info = _sensor(("berm-1", ("bermuda", "aa:bb")))._attr_device_info
    via_keys = {k for k in info if k.startswith("via_device")}
    assert via_keys == {key}
    assert info[key] == value


def test_no_bermuda_device_means_no_via_key_at_all(monkeypatch):
    # Never None either: an explicit via_device_id=None CLEARS an existing link.
    monkeypatch.setattr(sn, "VIA_DEVICE_ID_SUPPORTED", True)
    info = _sensor(None)._attr_device_info
    assert not any(k.startswith("via_device") for k in info)


def test_a_via_device_that_vanished_before_the_add_is_dropped(monkeypatch, registries):
    monkeypatch.setattr(sn, "VIA_DEVICE_ID_SUPPORTED", True)
    s = _sensor(("berm-1", ("bermuda", "aa:bb")))
    s.hass = object()
    registries(DevReg([], "view"), EntReg([]))  # Bermuda's device is gone
    info = s.device_info
    assert "via_device_id" not in info
    assert info["identifiers"] == {("bps", "phone")}, "the rest of the device must survive"


def test_a_via_device_still_present_is_kept(monkeypatch, registries):
    monkeypatch.setattr(sn, "VIA_DEVICE_ID_SUPPORTED", True)
    s = _sensor(("berm-1", ("bermuda", "aa:bb")))
    s.hass = object()
    registries(*_bermuda_house())
    assert s.device_info["via_device_id"] == "berm-1"


# --------------------------------------------------------------------------- #
# The version boundary itself
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("version, expected", [
    ((2025, 12), False), ((2026, 7), False), ((2026, 8), True),
    ((2026, 10), True), ((2027, 1), True),
])
def test_via_device_id_starts_at_2026_8(monkeypatch, version, expected):
    # Both the DeviceInfo key and the async_get_or_create parameter arrived in
    # 2026.8.0 (core #175785). 2026.7.x has neither.
    const = sys.modules["homeassistant.const"]
    monkeypatch.setattr(const, "MAJOR_VERSION", version[0])
    monkeypatch.setattr(const, "MINOR_VERSION", version[1])
    try:
        assert importlib.reload(sn).VIA_DEVICE_ID_SUPPORTED is expected
    finally:
        monkeypatch.undo()
        importlib.reload(sn)


def test_the_yaml_platform_entry_point_is_gone():
    # It handed device_info-carrying entities to a platform with no config
    # entry, which 2026.8 reports and 2027.8 refuses. BPS is set up from the UI.
    assert not hasattr(sn, "async_setup_platform")
