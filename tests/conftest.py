"""Test bootstrap: stub the Home Assistant runtime so the real `sextant` package
imports without a full HA install.

The Sextant backend pulls in a handful of `homeassistant.*` modules (plus aiofiles,
aiohttp, voluptuous, watchdog) purely for type/registration plumbing that the
unit tests never exercise. We install lightweight fakes for those in
`sys.modules` BEFORE anything imports `sextant`, then let the genuinely numeric
dependencies (numpy, scipy, shapely) load for real — the positioning and
geometry maths under test run against the actual libraries.
"""
import json
import os
import sys
import types
from pathlib import Path

import pytest


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _FakeDistanceConverter:
    VALID_UNITS = {"m", "ft"}

    @staticmethod
    def convert(value, from_unit, to_unit):
        # Only feet->metres is exercised by the tests.
        return value * 0.3048 if from_unit == "ft" else value


# --- Functional-enough aiofiles / aiohttp.web fakes -------------------------- #
# The save/read handlers do real file I/O and build aiohttp responses; back
# those with a tiny synchronous-under-the-hood async shim so tests can exercise
# the actual handler flow (e.g. the write-after-validate ordering, audit #2).
class _AsyncFile:
    def __init__(self, path, mode):
        self._f = open(path, mode)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self._f.close()

    async def read(self):
        return self._f.read()

    async def write(self, data):
        return self._f.write(data)


def _aiofiles_open(path, mode="r", *a, **k):
    return _AsyncFile(path, mode)


async def _aiofiles_makedirs(path, *a, exist_ok=False, **k):
    # Real directories: setup creates www/sextant_maps and www/sextant_icons.
    os.makedirs(path, exist_ok=exist_ok)


async def _aiofiles_remove(path):
    os.remove(path)


# --- Fake homeassistant.helpers.storage.Store -------------------------------
# Backed by a per-`hass` dict so a "restart" (fresh cache, same hass) still
# loads what was saved. Records save calls so tests can assert async_save is
# used (never async_delay_save) and that only complete dicts are persisted.
class _FakeStore:
    def __init__(self, hass, version, key):
        self._hass = hass
        self._key = key
        if not hasattr(hass, "_store_backing"):
            hass._store_backing = {}
            hass._store_saves = []
            hass._store_delay_saves = []

    async def async_load(self):
        import copy as _copy
        # Simulate a corrupt/unreadable store file (HA raises HomeAssistantError).
        if self._key in getattr(self._hass, "_store_raise_on_load", ()):
            raise ValueError(f"corrupt store {self._key}")
        return _copy.deepcopy(self._hass._store_backing.get(self._key))

    async def async_save(self, data):
        # Round-trip through JSON like real HA Store, so a non-JSON-serializable
        # value (e.g. a numpy scalar or set) fails the test as it would in prod.
        self._hass._store_saves.append((self._key, data))
        self._hass._store_backing[self._key] = json.loads(json.dumps(data))

    async def async_delay_save(self, data_func, delay=None):
        self._hass._store_delay_saves.append(self._key)


class _Response:
    def __init__(self, status=200, text="", **k):
        self.status = status
        self.body_text = text


def _json_response(data=None, status=200):
    r = _Response(status=status)
    r.json_body = data
    return r


class FakeEntityEntry:
    def __init__(self, entity_id, platform="sextant", unique_id=None, device_id=None):
        self.entity_id = entity_id
        self.platform = platform
        self.unique_id = unique_id
        self.device_id = device_id
        self.disabled_by = None
        self.original_name = None


class FakeEntityRegistry:
    def __init__(self):
        self.entities = {}

    def add(self, entity_id, platform="sextant", unique_id=None, device_id=None):
        self.entities[entity_id] = FakeEntityEntry(entity_id, platform, unique_id, device_id)
        return self.entities[entity_id]

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def async_remove(self, entity_id):
        self.entities.pop(entity_id, None)

    def async_update_entity(self, entity_id, new_unique_id=None, new_entity_id=None, **_kw):
        entry = self.entities.pop(entity_id)
        if new_unique_id:
            entry.unique_id = new_unique_id
        if new_entity_id:
            entry.entity_id = new_entity_id
        self.entities[entry.entity_id] = entry
        return entry


class FakeDevice:
    def __init__(self, device_id, identifiers):
        self.id = device_id
        self.identifiers = set(identifiers)
        self.name = None


class FakeDeviceRegistry:
    def __init__(self):
        self.devices = {}

    def add(self, identifiers, device_id=None):
        device_id = device_id or f"dev{len(self.devices) + 1}"
        self.devices[device_id] = FakeDevice(device_id, identifiers)
        return self.devices[device_id]

    def async_get(self, device_id):
        return self.devices.get(device_id)

    def async_get_device(self, identifiers=None, connections=None):
        wanted = set(identifiers or ())
        return next((d for d in self.devices.values() if d.identifiers & wanted), None)

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.by_identifier_calls = getattr(self, "by_identifier_calls", 0) + 1
        return next((d for d in self.devices.values() if identifier in d.identifiers), None)

    def async_remove_device(self, device_id):
        self.devices.pop(device_id, None)


def _install_homeassistant_stubs():
    aiofiles_mod = _module("aiofiles", open=_aiofiles_open)
    # `import aiofiles.os` reads `.os` off the parent; attach it explicitly.
    aiofiles_mod.os = _module("aiofiles.os", makedirs=_aiofiles_makedirs, remove=_aiofiles_remove)
    _module("homeassistant.helpers.storage", Store=_FakeStore)
    _module("aiohttp", web=types.SimpleNamespace(
        Response=_Response, json_response=_json_response, FileResponse=object,
    ))
    _module("homeassistant")
    components = _module("homeassistant.components")
    _module("homeassistant.components.http", HomeAssistantView=object)

    async def _async_register_panel(*a, **k):
        return None

    panel_custom = _module("homeassistant.components.panel_custom",
                           async_register_panel=_async_register_panel)
    # `from homeassistant.components import panel_custom` reads the attribute
    # off the parent package, so expose it there too.
    components.panel_custom = panel_custom
    _module(
        "homeassistant.components.frontend",
        async_register_built_in_panel=lambda *a, **k: None,
        async_remove_panel=lambda *a, **k: None,
    )
    helpers = _module("homeassistant.helpers")
    # `from homeassistant.helpers.storage import Store` — expose the submodule
    # on its parent (as done for panel_custom above).
    helpers.storage = sys.modules["homeassistant.helpers.storage"]
    _module("homeassistant.helpers.event", async_track_state_change_event=lambda *a, **k: None)

    # A working in-memory dispatcher, keyed per hass, so the websocket
    # subscription and the per-cycle push can be exercised for real.
    def _dispatch_bucket(hass):
        return hass.data.setdefault("_test_dispatcher", {})

    def _async_dispatcher_connect(hass, signal, target):
        _dispatch_bucket(hass).setdefault(signal, []).append(target)

        def _unsub():
            try:
                _dispatch_bucket(hass)[signal].remove(target)
            except (KeyError, ValueError):
                pass
        return _unsub

    def _async_dispatcher_send(hass, signal, *args):
        for target in list(_dispatch_bucket(hass).get(signal, [])):
            target(*args)

    _module("homeassistant.helpers.dispatcher",
            async_dispatcher_connect=_async_dispatcher_connect,
            async_dispatcher_send=_async_dispatcher_send)

    # websocket_api: the decorators are pass-through, messages are plain dicts
    # a fake connection can record.
    def _require_admin(func):
        func._ws_admin = True  # tests assert which commands carry the gate
        return func

    def _websocket_command(schema):
        def deco(func):
            func._ws_schema = schema
            return func
        return deco

    components.websocket_api = _module(
        "homeassistant.components.websocket_api",
        websocket_command=_websocket_command,
        require_admin=_require_admin,
        async_response=lambda f: f,
        async_register_command=lambda hass, func: hass.data.setdefault("_ws_commands", []).append(func),
        event_message=lambda msg_id, event: {"id": msg_id, "type": "event", "event": event},
        result_message=lambda msg_id, result=None: {"id": msg_id, "type": "result", "success": True, "result": result},
    )
    _module("homeassistant.helpers.template", Template=object)

    def _register_admin_service(hass, domain, service, func, schema=None, supports_response=None, **_kw):
        # Recorded so tests can assert which services carry the admin gate.
        hass.data.setdefault("_admin_services", set()).add(service)
        hass.services.async_register(domain, service, func, schema=schema)

    _module("homeassistant.helpers.service", async_register_admin_service=_register_admin_service)
    _module(
        "homeassistant.core", HomeAssistant=object, ServiceCall=object, callback=lambda f: f,
        ServiceResponse=dict, SupportsResponse=types.SimpleNamespace(NONE="none", ONLY="only", OPTIONAL="optional"),
    )
    _module("homeassistant.exceptions", HomeAssistantError=Exception)
    # `from homeassistant.helpers import config_validation as cv` reads the
    # attribute off the parent package (like panel_custom above).
    helpers.config_validation = _module(
        "homeassistant.helpers.config_validation",
        string=str, boolean=bool, positive_int=int, entity_id=str,
        ensure_list=lambda v: v if isinstance(v, list) else [v],
    )
    # Entity and device registries: in-memory fakes, one pair per hass, with
    # the handful of methods the integration uses (entries are plain objects
    # with entity_id / platform / unique_id / device_id).
    _module("homeassistant.helpers.entity_registry",
            async_get=lambda hass: hass.data.setdefault("_test_entity_registry", FakeEntityRegistry()))
    _module("homeassistant.helpers.device_registry",
            async_get=lambda hass: hass.data.setdefault("_test_device_registry", FakeDeviceRegistry()))

    async def _async_get_integration(hass, domain):
        return types.SimpleNamespace(domain=domain, version="9.9.9-test")

    _module("homeassistant.loader", async_get_integration=_async_get_integration)
    # Enough of the sensor platform for sensor.py to import (sensor tests
    # exercise the cache/creation logic, never HA's entity machinery).
    _module("homeassistant.components.sensor", SensorEntity=object,
            SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"))
    _module("homeassistant.helpers.entity", DeviceInfo=dict)

    # Config-entry flows: just enough of ConfigFlow/OptionsFlow for the
    # integration's flow module to import and for tests to drive its steps.
    class _FakeConfigFlow:
        def __init_subclass__(cls, domain=None, **kw):
            cls.domain = domain

        def __init__(self):
            self.hass = None
            self.unique_id = None

        async def async_set_unique_id(self, uid):
            self.unique_id = uid

        def _abort_if_unique_id_configured(self):
            if self.unique_id in getattr(self.hass, "configured_unique_ids", ()):
                raise _AbortFlow("already_configured")

        def async_create_entry(self, title, data):
            return {"type": "create_entry", "title": title, "data": data}

        def async_show_form(self, step_id, data_schema=None, errors=None):
            return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors}

    class _FakeOptionsFlow(_FakeConfigFlow):
        pass

    class _AbortFlow(Exception):
        pass

    import enum
    _module("homeassistant.config_entries", ConfigFlow=_FakeConfigFlow, OptionsFlow=_FakeOptionsFlow,
            AbortFlow=_AbortFlow, ConfigEntryState=enum.Enum("ConfigEntryState", "LOADED NOT_LOADED SETUP_ERROR"))
    sys.modules["homeassistant.helpers.event"].async_call_later = lambda *a, **k: None
    _module("homeassistant.const", UnitOfLength=types.SimpleNamespace(METERS="m"),
            EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
            EVENT_HOMEASSISTANT_FINAL_WRITE="homeassistant_final_write")
    _module("homeassistant.util", slugify=lambda s: s)
    _module("homeassistant.util.unit_conversion", DistanceConverter=_FakeDistanceConverter)
    _module(
        "voluptuous",
        Schema=lambda *a, **k: None, Required=lambda *a, **k: None,
        Optional=lambda *a, **k: None, Coerce=lambda *a, **k: None,
        All=lambda *a, **k: None, Any=lambda *a, **k: None, Length=lambda *a, **k: None, Range=lambda *a, **k: None,
        In=lambda *a, **k: None,
    )
    # NOTE: no watchdog stub — the integration no longer imports it (the file
    # watcher was removed when the layout moved to the Store). If a stray import
    # comes back, the suite will fail loudly here.


_install_homeassistant_stubs()
# custom_components/ on the path so `import sextant` resolves to the integration.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))


def make_hass(config_dir=None):
    """A minimal fake Home Assistant for storage/handler tests.

    Carries `.data`, a `.config.path()` rooted at config_dir (or cwd), and the
    per-hass Store backing the fake Store reads/writes.
    """
    root = Path(config_dir) if config_dir else Path(".")
    hass = types.SimpleNamespace()
    hass.data = {}
    hass.config = types.SimpleNamespace(path=lambda *p: str(root.joinpath(*p)))
    hass._store_backing = {}
    hass._store_saves = []
    hass._store_delay_saves = []
    hass._store_raise_on_load = set()   # store keys whose async_load should raise

    async def _executor(func, *args):
        # Run inline: the tests are single-threaded and only need the awaitable
        # contract, not real off-loop execution.
        return func(*args)

    hass.async_add_executor_job = _executor
    return hass


@pytest.fixture
def hass(tmp_path):
    return make_hass(tmp_path)
