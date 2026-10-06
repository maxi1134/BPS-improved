"""Setup and unload round trip (sextant.async_setup_entry / async_unload_entry).

Everything the integration wires at start is asserted from the outside: the
HTTP views, the services in services.yaml, every websocket command, the panel
at its per-release URL, the tracking task and the stop hook; then that unload
takes it all down and a second setup works on the same hass.
"""
import asyncio
import re
import types
from pathlib import Path

import sextant
from sextant import ws
from sextant import storage as st

from conftest import make_hass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Task:
    def __init__(self, coro):
        self.coro, self.cancelled = coro, False

    def cancel(self):
        self.cancelled = True
        self.coro.close()


class _Entry:
    entry_id = "entry-1"
    options = {}

    def __init__(self):
        self.forwarded, self.unloaded, self.on_unload = [], [], []

    def add_update_listener(self, fn):
        return lambda: None

    def async_on_unload(self, fn):
        self.on_unload.append(fn)


def _full_hass(tmp_path, entry):
    hass = make_hass(tmp_path)
    hass.is_running = True
    hass.states = types.SimpleNamespace(async_all=lambda domain=None: [], async_remove=lambda eid: None, get=lambda eid: None)
    hass.http = types.SimpleNamespace(views=[], register_view=lambda v: hass.http.views.append(v))
    hass.services = types.SimpleNamespace(registered=[], async_register=lambda domain, name, *a, **k: hass.services.registered.append((domain, name)))
    hass.bus = types.SimpleNamespace(listeners=[], async_listen_once=lambda event, cb: hass.bus.listeners.append(event),
                                     async_listen=lambda event, cb: (hass.bus.listeners.append(event) or (lambda: None)))
    hass.tasks = []
    hass.async_create_task = lambda coro: (hass.tasks.append(_Task(coro)) or hass.tasks[-1])

    async def forward(e, platforms):
        entry.forwarded.append(list(platforms))

    async def unload_platforms(e, platforms):
        entry.unloaded.append(list(platforms))
        return True

    hass.config_entries = types.SimpleNamespace(async_entries=lambda domain: [entry], async_forward_entry_setups=forward,
                                                async_unload_platforms=unload_platforms)
    return hass


def _service_names():
    text = (Path(__file__).resolve().parents[1] / "custom_components" / "sextant" / "services.yaml").read_text()
    return sorted(re.findall(r"^([a-z_]+):", text, re.M))


def test_setup_and_unload_round_trip(tmp_path, monkeypatch):
    entry = _Entry()
    hass = _full_hass(tmp_path, entry)
    captured = {}

    async def register_panel(_hass, **kw):
        captured.update(kw)

    monkeypatch.setattr(sextant.panel_custom, "async_register_panel", register_panel)
    # A layout saved before start must be what the loop sees after it.
    hass.data["sextant"] = {}
    run(st.save_layout(hass, {"floor": [{"name": "F", "scale": 100.0, "receivers": [], "zones": [], "subzones": []}], "tuning": {}}))

    assert run(sextant.async_setup_entry(hass, entry)) is True
    assert entry.forwarded == [["sensor", "device_tracker"]]
    assert {v.__class__.__name__ for v in hass.http.views} == {
        "SextantFrontendView", "SextantMapImageView", "SextantSaveAPIText", "SextantUploadThingIconAPI",
        "SextantCordsAPI", "SextantSelfTestAPI"}
    assert sorted(name for domain, name in hass.services.registered if domain == "sextant") == _service_names()
    handlers = [n for n in dir(ws) if n.startswith("ws_")]
    assert len(hass.data["_ws_commands"]) == len(handlers) + 1  # + the subscription
    assert captured["frontend_url_path"] == "sextant" and captured["webcomponent_name"] == "sextant-panel"
    assert captured["module_url"] == "/sextant/v/9.9.9-test/sextant-panel.js"
    assert st.get_layout(hass)["floor"][0]["name"] == "F"
    assert hass.data["sextant_update_task"] is hass.tasks[-1] and not hass.tasks[-1].cancelled
    assert "homeassistant_stop" in hass.bus.listeners
    assert (tmp_path / "sextant_maps").is_dir() and (tmp_path / "www" / "sextant_icons").is_dir()

    # A second setup on a running instance is a no-op, not a double registration.
    assert run(sextant.async_setup(hass, {})) is True
    assert len(hass.http.views) == 6 and len(hass.data["_ws_commands"]) == len(handlers) + 1

    assert run(sextant.async_unload_entry(hass, entry)) is True
    assert entry.unloaded == [["sensor", "device_tracker"]] and len(entry.on_unload) == 1
    assert hass.tasks[-1].cancelled
    for key in ("sextant_initialized", "sextant_sensors", "sextant_add_entities", "sextant_update_task"):
        assert key not in hass.data

    # And it comes back after an unload (a reload from the options flow).
    assert run(sextant.async_setup(hass, {})) is True
    assert hass.data["sextant_initialized"] and not hass.tasks[-1].cancelled
    hass.tasks[-1].cancel()
