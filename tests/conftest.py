"""Test bootstrap: stub the Home Assistant runtime so the real `bps` package
imports without a full HA install.

The BPS backend pulls in a handful of `homeassistant.*` modules (plus aiofiles,
aiohttp, voluptuous, watchdog) purely for type/registration plumbing that the
unit tests never exercise. We install lightweight fakes for those in
`sys.modules` BEFORE anything imports `bps`, then let the genuinely numeric
dependencies (numpy, scipy, shapely) load for real — the positioning and
geometry maths under test run against the actual libraries.
"""
import sys
import types
from pathlib import Path


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


async def _aiofiles_makedirs(*a, **k):
    return None


class _Response:
    def __init__(self, status=200, text="", **k):
        self.status = status
        self.body_text = text


def _json_response(data=None, status=200):
    r = _Response(status=status)
    r.json_body = data
    return r


def _install_homeassistant_stubs():
    _module("aiofiles", open=_aiofiles_open)
    _module("aiofiles.os", makedirs=_aiofiles_makedirs)
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
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.event", async_track_state_change_event=lambda *a, **k: None)
    _module("homeassistant.helpers.template", Template=object)
    _module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _module("homeassistant.helpers.entity_registry")
    _module("homeassistant.helpers.device_registry")
    _module("homeassistant.const", UnitOfLength=types.SimpleNamespace(METERS="m"))
    _module("homeassistant.util", slugify=lambda s: s)
    _module("homeassistant.util.unit_conversion", DistanceConverter=_FakeDistanceConverter)
    _module(
        "voluptuous",
        Schema=lambda *a, **k: None, Required=lambda *a, **k: None,
        Optional=lambda *a, **k: None, Coerce=lambda *a, **k: None,
        All=lambda *a, **k: None, Length=lambda *a, **k: None, Range=lambda *a, **k: None,
    )
    _module("watchdog")
    _module("watchdog.observers", Observer=object)
    _module("watchdog.events", FileSystemEventHandler=object)


_install_homeassistant_stubs()
# custom_components/ on the path so `import bps` resolves to the integration.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))
