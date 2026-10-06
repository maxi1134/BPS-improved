"""Websocket commands: everything the Sextant panel reads and writes.

The old panel ran inside an iframe and fetched ``/api/sextant/*`` with a
couriered token. The rebuilt panel is a native Home Assistant panel, so it
talks over the authenticated websocket like the rest of the frontend: one
subscription for positions (``sextant/subscribe``, in __init__) and the
request/response commands below for everything else. The HTTP views stay
for file uploads (a map image, a thing icon), the legacy editor and the
eval tools.

Every command answers with a plain JSON-able dict, or an error whose
message is meant to be shown to the user as is.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from importlib import import_module

import voluptuous as vol
from homeassistant.components import websocket_api

from . import bermuda_source, kpi
from . import history as history_mod
from .const import PROBE_BEACON_UUID
from . import fingerprint as fingerprint_mod
from . import truth as truth_mod
from . import robots as robots_mod
from .storage import (
    maps_dir,
    LAYOUT_LOCK, get_layout, get_layout_for_edit, get_layout_version, load_kpi_baselines, save_kpi_baselines,
    load_truth, save_truth,
    save_layout,
)

_LOGGER = logging.getLogger(__name__)


def _core():
    """The integration module (sextant/__init__), imported lazily: it imports us."""
    return import_module(__package__)


def _error(connection, msg, message, code="sextant_error"):
    connection.send_error(msg["id"], code, str(message))


def _safe(func, fallback):
    """Call a diagnostics helper; a failure (no Bermuda, no registry) is the fallback, never an error."""
    try:
        return func()
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("panel diagnostics unavailable: %s", e)
        return fallback


def _tuning_spec_json(spec):
    out = {}
    for key, entry in spec.items():
        kind = entry[1]
        if kind is bool:
            out[key] = {"default": entry[0], "type": "bool"}
        elif kind is str:
            out[key] = {"default": entry[0], "type": "str", "choices": list(entry[2])}
        else:
            out[key] = {"default": entry[0], "type": "int" if kind is int else "float", "min": entry[2], "max": entry[3]}
    return out


def _manifest_version() -> str | None:
    try:
        from pathlib import Path  # noqa: PLC0415

        return json.loads((Path(__file__).parent / "manifest.json").read_text()).get("version")
    except Exception:  # noqa: BLE001
        return None


# The version this Home Assistant process loaded, set at setup from the
# integration Home Assistant loaded (reading the manifest here, at import,
# would block the event loop). HACS replaces the files on disk but the running
# code (and the panel URL, which carries the version) stays the old one until
# Home Assistant restarts; comparing the two tells the panel whether a reload
# is enough or a restart is needed.
RUNNING_VERSION = None
# A digest of the integration's Python sources as loaded (set at setup). The
# frontend is served from disk and needs only a page reload after an update;
# only when this differs from the files on disk does Home Assistant have to
# restart, and the panel says so.
RUNNING_CODE = None


_signature_cache = [None, None]  # [(name, mtime_ns, size) per file, digest]


def code_signature() -> str | None:
    """sha256 over the package's .py files, in name order (blocking: call in the executor)."""
    import hashlib  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    try:
        paths = sorted(Path(__file__).parent.glob("*.py"))
        # Every panel load asks; re-hash only when a file's size or mtime moved.
        key = tuple((p.name, st.st_mtime_ns, st.st_size) for p in paths for st in (p.stat(),))
        if _signature_cache[0] == key:
            return _signature_cache[1]
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        _signature_cache[:] = [key, digest.hexdigest()]
        return _signature_cache[1]
    except OSError:
        return None


def _thing_names(hass, entities, layout=None) -> dict:
    """{slug: display name} for the tracked entities.

    Bermuda's device name first ("Fry", "David's Phone"), then whatever the
    user renamed the device to in Home Assistant, then the name typed in
    Sextant's own thing dialog (layout "thing_names"), which wins.
    """
    names = {}
    for info in (bermuda_source.async_get_tracked_devices(hass) or {}).values():
        slug, name = info.get("slug"), info.get("name")
        if slug and name:
            names[slug] = _tidy_device_name(name)
    try:
        from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: PLC0415

        ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
        for ent in entities:
            # Only a name the user typed counts here: the registry's own name
            # is Sextant's "<slug> (Sextant)" device or Bermuda's, which the
            # tracked list already gave us in a nicer form.
            for entity_id in (f"sensor.{ent}_sextant_room", f"sensor.{ent}_area", f"device_tracker.{ent}"):
                entry = ent_reg.async_get(entity_id)
                device = dev_reg.async_get(entry.device_id) if entry and entry.device_id else None
                if device is not None and device.name_by_user:
                    # A rename in HA wins, minus the integration prefix HA's
                    # own rename dialog pre-fills ("Private BLE Device ...").
                    names[ent] = _tidy_device_name(device.name_by_user)
                    break
    except Exception:  # noqa: BLE001 - no registries (tests), Bermuda's names stand
        pass
    overrides = layout.get("thing_names") if isinstance(layout, dict) else None
    if isinstance(overrides, dict):
        for slug, name in overrides.items():
            if isinstance(name, str) and name.strip():
                names[slug] = name.strip()
    return names


def _tidy_device_name(name: str) -> str:
    """"Private BLE Device David's Phone" -> "David's Phone": the integration
    prefix Bermuda copies into the device name is not part of the name."""
    for prefix in ("Private BLE Device ", "Private BLE "):
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix):]
    return name


# --- layout ----------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "sextant/layout/get"})
@websocket_api.async_response
async def ws_layout_get(hass, connection, msg):
    core = _core()
    layout = get_layout(hass)
    layout_json = json.dumps(layout) if layout else ""
    maps_path = maps_dir(hass)
    icons_path = hass.config.path("www/sextant_icons")
    try:
        maps = await hass.async_add_executor_job(core.list_map_files, maps_path)
    except Exception:  # noqa: BLE001 - a missing folder is an empty list
        maps = []
    try:
        icons = await hass.async_add_executor_job(core.list_thing_icons, icons_path)
    except Exception:  # noqa: BLE001
        icons = []
    tracked = bermuda_source.async_get_tracked_device_prefixes(hass)
    if tracked is None:
        try:
            allowed = core._bermuda_distance_sensor_ids(hass)
            tracked = {eid[len("sensor."):].split("_distance_to_")[0] for eid in allowed}
        except Exception:  # noqa: BLE001 - no Bermuda, no registry: nothing tracked
            tracked = set()
    directory = bermuda_source.async_get_scanner_directory(hass) or {}
    dom = hass.data.get(core.DOMAIN, {})
    robot_names = {}
    for vac in ((layout or {}).get("robots") or {}) if isinstance(layout, dict) else {}:
        st = hass.states.get(vac)
        robot_names[core.robot_thing(vac)] = (st.attributes.get("friendly_name") if st else None) or vac
    names = _safe(lambda: _thing_names(hass, tracked, layout), {})
    names = {**robot_names, **(names if isinstance(names, dict) else {})}
    tracked = set(tracked) | set(core.robot_things(layout))
    connection.send_result(msg["id"], {
        "layout": layout if isinstance(layout, dict) else None,
        "version": get_layout_version(hass),
        "maps": sorted(maps),
        "icons": icons,
        "entities": sorted(tracked),
        # Display names: what Bermuda calls the device, overridden by the name
        # the user gave the device in Home Assistant (device registry); a
        # robot is called what its vacuum entity is called.
        "names": names,
        # The installed integration version, and the one Home Assistant is
        # running: installed but not running needs a restart; running but
        # newer than the page only needs a reload.
        "app_version": await hass.async_add_executor_job(_manifest_version),
        # Where each thing was last heard, kept across restarts: what the Live
        # page says for a thing nothing is hearing, which its sensors cannot
        # answer after a restart (see runtime.py).
        "last_seen": {ent: last for ent, last in (getattr(core, "_last_seen", None) or {}).items()
                      if isinstance(last, dict)},
        "running_version": RUNNING_VERSION,
        "restart_needed": RUNNING_CODE is not None and await hass.async_add_executor_job(code_signature) != RUNNING_CODE,
        "scanners": {
            addr: {"slug": info.get("slug"), "name": info.get("name"), "area": info.get("area_name"),
                   "is_remote": info.get("is_remote")}
            for addr, info in directory.items()
        },
        "offline_receivers": list(dom.get("rl_offline") or []),
        "scanner_diagnostics": _safe(lambda: core._scanner_diagnostics(hass, layout_json), {"unmatched_receivers": [], "unplaced_scanners": []}),
        "tuning_spec": _tuning_spec_json(core.TUNING_SPEC),
        "features": sorted(bermuda_source.async_features(hass)),
    })


def merge_editor_layout(current, incoming):
    """The editor's floors over the current layout, keeping what the server owns.

    The Edit page works on a copy of the layout taken when it loaded and
    sends the whole copy back on Save. Everything outside ``floor`` (tuning,
    thing names, classes, colours, heights, the auto-calibration flag) is
    written by other pages and by the backend, and inside a floor the
    per-proxy ``correction`` and the ``calibration`` stamp are written by
    calibration, and the ``bias_field`` by its service - none of which the
    editor edits. Taking them from the
    current layout means a Save can no longer wipe corrections that auto
    calibration applied five minutes earlier, or a colour picked on the
    Things page while the editor sat open (that is what happened).
    Receivers are matched by entity_id; one the editor added has no
    correction yet, one it deleted takes its correction with it.
    """
    if not isinstance(current, dict):
        return incoming
    merged = {k: v for k, v in current.items() if k != "floor"}
    by_name = {str(f.get("name")): f for f in current.get("floor", []) if isinstance(f, dict)}
    floors = []
    for floor in incoming.get("floor", []):
        floor = dict(floor)
        old = by_name.get(str(floor.get("name")))
        if old is not None:
            # Server-owned per-floor keys: the editor's copy is whatever it
            # loaded, possibly hours ago, so the store's version always wins.
            for owned in ("calibration", "bias_field"):
                floor.pop(owned, None)
                if isinstance(old.get(owned), dict):
                    floor[owned] = old[owned]
            corrections = {str(r.get("entity_id")): r.get("correction") for r in old.get("receivers", []) if isinstance(r, dict)}
            receivers = []
            for r in floor.get("receivers", []):
                r = dict(r)
                # A proxy the user has taken out of calibration owns its own
                # correction - that is the whole point of the switch - so the
                # editor's number is the one that counts. Everything else
                # keeps the stored value: calibration may have written it
                # since this editor copy was loaded.
                if r.get("calibrate") is False:
                    receivers.append(r)
                    continue
                r.pop("correction", None)
                kept = corrections.get(str(r.get("entity_id")))
                if kept is not None:
                    r["correction"] = kept
                receivers.append(r)
            floor["receivers"] = receivers
        floors.append(floor)
    merged["floor"] = floors
    return merged


def _confine_spots(layout):
    """Clip every spot to its one room; names of the spots that changed."""
    from .zone_adjust import confine_spots  # noqa: PLC0415 - shapely is heavy

    return [name for floor in layout.get("floor") or [] for name in confine_spots(floor)]


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/layout/save",
    vol.Required("layout"): dict,
    vol.Optional("remove_map"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_layout_save(hass, connection, msg):
    core = _core()
    layout = msg["layout"]
    floors = layout.get("floor")
    if not isinstance(floors, list):
        return _error(connection, msg, "layout.floor must be a list")
    for floor in floors:
        if not isinstance(floor, dict) or not floor.get("name"):
            return _error(connection, msg, "every floor needs a name")
    remove_target = None
    remove = msg.get("remove_map")
    if remove:
        maps_path = maps_dir(hass)
        remove_target = core._safe_maps_child(maps_path, remove, None)
        if remove_target is None or remove_target.name in core._PROTECTED_MAPS_FILES:
            return _error(connection, msg, "invalid map to remove")
    async with LAYOUT_LOCK:
        layout = merge_editor_layout(get_layout(hass), layout)
        # A spot belongs to one room: clip each to its room before it is stored.
        confined = await hass.async_add_executor_job(_confine_spots, layout)
        losses = await save_layout(hass, layout)
    if remove_target is not None and remove_target.exists():
        try:
            await hass.async_add_executor_job(remove_target.unlink)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Could not remove map %s: %s", remove_target.name, e)
    try:
        core.refresh_receivers_from_coords(hass, json.dumps(layout))
    except Exception as e:  # noqa: BLE001 - calibration bookkeeping must not fail a save
        _LOGGER.debug("refresh_receivers_from_coords: %s", e)
    connection.send_result(
        msg["id"],
        {"version": get_layout_version(hass), "confined": confined, "lost": losses},
    )


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/tuning/set",
    vol.Optional("settings", default=dict): dict,
    vol.Optional("reset", default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_tuning_set(hass, connection, msg):
    core = _core()
    try:
        tuning = await core.async_apply_tuning(hass, msg.get("settings") or {}, bool(msg.get("reset")))
    except Exception as e:  # noqa: BLE001 - HomeAssistantError or ValueError: message for the user
        return _error(connection, msg, e)
    connection.send_result(msg["id"], {"tuning": tuning})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/thing/tune",
    vol.Required("entity"): str,
    vol.Optional("ref_offset_db"): vol.Any(None, vol.Coerce(float)),
    vol.Optional("height"): vol.Any(None, vol.Coerce(float)),
    vol.Optional("icon"): vol.Any(None, str),
    vol.Optional("name"): vol.Any(None, str),
    vol.Optional("thing_class"): vol.Any(None, str),
    # How the panel refers to the thing: he, she, they or it. None or "" falls
    # back to its class (a man is he, a phone is it, a pet or person they).
    vol.Optional("pronouns"): vol.Any(None, "", "he", "she", "they", "it"),
    # Whose it is: a Home Assistant person (person.david). Groups the Live
    # list, and is what a per-person location will be built from.
    vol.Optional("owner"): vol.Any(None, str),
    # Whether this thing's place may stand for its owner's (None: its class decides).
    vol.Optional("locates_owner"): vol.Any(None, bool),
    # A battery sensor whose "Charging" takes the thing out of its owner's location.
    vol.Optional("charging_entity"): vol.Any(None, str),
    # The thing's own battery level, as a percentage sensor: shown on the Live
    # page, and as a badge on the thing once it runs low. A pet's tag that goes
    # quiet reads exactly like a pet that has left, unless the battery says why.
    vol.Optional("battery_entity"): vol.Any(None, str),
    vol.Optional("estimator"): vol.Any(None, "", "geometric", "fingerprint", "fused"),
    vol.Optional("fp_weight"): vol.Any(None, vol.Coerce(float)),
    vol.Optional("color"): vol.Any(None, str),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_thing_tune(hass, connection, msg):
    """Per-thing settings, each applied on its own: ref-power trim (dB),
    carry height (m), map icon, display name, class (person, dog, phone...
    the panel draws an icon per class) and position estimator (geometric,
    fingerprint or fused for this thing alone; null follows the tuning).
    A null clears the field."""
    core = _core()
    entity = msg["entity"]
    changes = {}
    owner = msg.get("owner")
    if owner and not re.fullmatch(r"person\.[a-z0-9_]+", owner):
        return _error(connection, msg, "owner must be a Home Assistant person, like person.david")
    charging = msg.get("charging_entity")
    if charging and not re.fullmatch(r"(sensor|binary_sensor)\.[a-z0-9_]+", charging):
        return _error(connection, msg, "the on-charger sensor must be a sensor or binary_sensor")
    battery = msg.get("battery_entity")
    if battery and not re.fullmatch(r"sensor\.[a-z0-9_]+", battery):
        return _error(connection, msg, "the battery sensor must be a sensor")
    async with LAYOUT_LOCK:
        data = get_layout_for_edit(hass)
        if not isinstance(data, dict):
            return _error(connection, msg, "No layout saved yet")
        if "ref_offset_db" in msg:
            raw = msg["ref_offset_db"]
            offsets = data.get("thing_ref_offsets")
            if not isinstance(offsets, dict):
                offsets = {}
            if raw is None or raw == 0.0:
                offsets.pop(entity, None)
                changes["ref_offset_db"] = 0.0
            else:
                if not math.isfinite(raw) or abs(raw) > core.THING_REF_OFFSET_MAX_DB:
                    return _error(connection, msg, f"ref_offset_db must be within +/-{core.THING_REF_OFFSET_MAX_DB} dB")
                offsets[entity] = float(raw)
                changes["ref_offset_db"] = float(raw)
            data["thing_ref_offsets"] = offsets
        if "height" in msg:
            raw = msg["height"]
            heights = data.get("thing_heights")
            if not isinstance(heights, dict):
                heights = {}
            if raw is None:
                heights.pop(entity, None)
                changes["height"] = None
            else:
                if not math.isfinite(raw) or not 0 <= raw <= 5:
                    return _error(connection, msg, "height must be between 0 and 5 m")
                heights[entity] = float(raw)
                changes["height"] = float(raw)
            data["thing_heights"] = heights
        if "color" in msg:
            raw = (msg["color"] or "").strip().lower()
            colors = data.get("thing_colors")
            if not isinstance(colors, dict):
                colors = {}
            if raw:
                if not re.fullmatch(r"#[0-9a-f]{6}", raw):
                    return _error(connection, msg, "color must be #rrggbb")
                colors[entity] = raw
            else:
                colors.pop(entity, None)
            data["thing_colors"] = colors
            changes["color"] = raw or None
        if "icon" in msg:
            icons = data.get("thing_icons")
            if not isinstance(icons, dict):
                icons = {}
            if msg["icon"]:
                icons[entity] = msg["icon"]
            else:
                icons.pop(entity, None)
            data["thing_icons"] = icons
            changes["icon"] = msg["icon"] or None
        if "fp_weight" in msg:
            raw = msg["fp_weight"]
            weights = data.get("thing_fp_weights")
            if not isinstance(weights, dict):
                weights = {}
            if raw is None:
                weights.pop(entity, None)
                changes["fp_weight"] = None
            else:
                if not math.isfinite(raw) or not 0.0 <= raw <= 1.0:
                    return _error(connection, msg, "fp_weight must be between 0 (geometric) and 1 (fingerprint)")
                weights[entity] = round(float(raw), 3)
                changes["fp_weight"] = weights[entity]
            data["thing_fp_weights"] = weights
        if "estimator" in msg:
            estimators = data.get("thing_estimators")
            if not isinstance(estimators, dict):
                estimators = {}
            if msg["estimator"]:
                estimators[entity] = msg["estimator"]
            else:
                estimators.pop(entity, None)
            data["thing_estimators"] = estimators
            changes["estimator"] = msg["estimator"] or None
        for key, store in (("name", "thing_names"), ("thing_class", "thing_classes"), ("pronouns", "thing_pronouns"),
                           ("owner", "thing_owners")):
            if key in msg:
                values = data.get(store)
                if not isinstance(values, dict):
                    values = {}
                value = (msg[key] or "").strip()
                if value:
                    values[entity] = value[:60]
                else:
                    values.pop(entity, None)
                data[store] = values
                changes[key] = value or None
        if "locates_owner" in msg:
            values = data.get("thing_locates_owner")
            if not isinstance(values, dict):
                values = {}
            if isinstance(msg["locates_owner"], bool):
                values[entity] = msg["locates_owner"]
            else:
                values.pop(entity, None)
            data["thing_locates_owner"] = values
            changes["locates_owner"] = msg["locates_owner"]
        if "charging_entity" in msg:
            values = data.get("thing_charging_entity")
            if not isinstance(values, dict):
                values = {}
            if msg["charging_entity"]:
                values[entity] = msg["charging_entity"]
            else:
                values.pop(entity, None)
            data["thing_charging_entity"] = values
            changes["charging_entity"] = msg["charging_entity"]
        if "battery_entity" in msg:
            values = data.get("thing_battery_entity")
            if not isinstance(values, dict):
                values = {}
            if msg["battery_entity"]:
                values[entity] = msg["battery_entity"]
            else:
                values.pop(entity, None)
            data["thing_battery_entity"] = values
            changes["battery_entity"] = msg["battery_entity"]
        await save_layout(hass, data)
    connection.send_result(msg["id"], {"entity": entity, **changes})


# --- truth marks ("it is actually here") -------------------------------------


def _zone_lookup(core, hass, data, entity, floor):
    """A function point -> room entity id (None outside every room) for one floor."""
    from shapely.geometry import Point  # noqa: PLC0415

    polys = [(zid, poly) for zid, poly, _b, no_go in core._floor_zone_polygons(hass, data, entity, floor) if not no_go]

    def zone_of(point):
        pt = Point(float(point[0]), float(point[1]))
        return next((zid for zid, poly in polys if poly.covers(pt)), None)
    return zone_of


def _mark_public(mark):
    return {k: v for k, v in mark.items() if k != "samples"} | {"samples": len(mark.get("samples") or [])}


async def _evaluate_mark(hass, core, mark, weights=None, gains=None):
    """Re-solve one mark's samples under the sweep (or the current settings only)."""
    layout = get_layout(hass)
    layout = layout if isinstance(layout, dict) else {}
    # The positioning helpers take the per-cycle list of {entity, data}; one entry is enough here.
    cycle = [{"entity": mark["entity"], "data": layout}]
    floor = mark["floor"]
    scale = core._floor_scale(cycle, mark["entity"], floor) or 0.0
    zone_of = _zone_lookup(core, hass, cycle, mark["entity"], floor)
    base_gain = core._tuning(layout, "fingerprint_ref_gain")
    if core._tuning(layout, "fingerprint_auto_gain"):
        base_gain *= core._fingerprint_db.gain_for(mark["entity"])
    vectors = core._fingerprint_db.vectors()
    extra = core._mark_refs(layout, mark["entity"])
    # A mark must not match its own reference, or the score would be circular.
    own = f"mark:{mark['id']}"
    extra = [r for r in extra if r.get("slug") != own] if extra else None

    def refs_for_gain(gain):
        return fingerprint_mod.build_references(layout, vectors, gain, extra=extra).get(floor)

    kwargs = {"k": core._tuning(layout, "fingerprint_k"), "missing_m": core._tuning(layout, "fingerprint_missing_m"), "base_gain": base_gain}
    if weights is not None:
        kwargs["weights"] = weights
    if gains is not None:
        kwargs["gains"] = gains
    return await hass.async_add_executor_job(
        lambda: truth_mod.evaluate(core._rebased_samples(layout, mark), floor, (mark["x"], mark["y"]), scale, zone_of, core._solve_floor_jobs, refs_for_gain, **kwargs)
    )


def _current_weight(core, layout, entity):
    w = core._thing_fp_weight(layout, entity)
    if w is not None:
        return w
    est = core._thing_estimator(layout, entity)
    return 0.0 if est == "geometric" else 1.0 if est == "fingerprint" else core._tuning(layout, "fingerprint_weight")


def _refresh_mark_refs(core, store):
    core._set_truth_marks(store.get("marks", []))


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/truth/mark",
    vol.Required("entity"): str,
    vol.Required("floor"): str,
    vol.Required("x"): vol.Coerce(float),
    vol.Required("y"): vol.Coerce(float),
    vol.Optional("window_secs", default=truth_mod.DEFAULT_WINDOW_SECS): vol.Coerce(float),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_truth_mark(hass, connection, msg):
    """Record that ``entity`` is really at (x, y) on ``floor`` right now, from the
    cycles of the last ``window_secs``, and say which settings fit it best."""
    if not (math.isfinite(msg["x"]) and math.isfinite(msg["y"])):
        return _error(connection, msg, "x and y must be finite numbers")
    core = _core()
    since = time.time() - max(30.0, float(msg["window_secs"]))
    samples = core._truth_buffer.samples(msg["entity"], since=since, floor=msg["floor"])
    if len(samples) < truth_mod.MIN_SAMPLES:
        return _error(connection, msg, f"Only {len(samples)} recent cycle(s) placed this thing on {msg['floor']}; wait a minute with it in place and try again")
    store = await load_truth(hass)
    mark = {
        "id": int(store.get("next_id") or 1), "entity": msg["entity"], "floor": msg["floor"],
        "x": round(float(msg["x"]), 2), "y": round(float(msg["y"]), 2), "t": time.time(), "samples": samples,
    }
    store["next_id"] = mark["id"] + 1
    store.setdefault("marks", []).append(mark)
    await save_truth(hass, store)
    _refresh_mark_refs(core, store)
    rows = await _evaluate_mark(hass, core, mark)
    connection.send_result(msg["id"], {"mark": _mark_public(mark), "rows": rows, "current_weight": _current_weight(core, get_layout(hass), mark["entity"])})


@websocket_api.websocket_command({vol.Required("type"): "sextant/truth/list", vol.Optional("entity"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_truth_list(hass, connection, msg):
    store = await load_truth(hass)
    marks = [m for m in store.get("marks", []) if not msg.get("entity") or m["entity"] == msg["entity"]]
    connection.send_result(msg["id"], {"marks": [_mark_public(m) for m in marks]})


@websocket_api.websocket_command({vol.Required("type"): "sextant/truth/delete", vol.Required("mark_id"): vol.Coerce(int)})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_truth_delete(hass, connection, msg):
    core = _core()
    store = await load_truth(hass)
    before = len(store.get("marks", []))
    store["marks"] = [m for m in store.get("marks", []) if m.get("id") != msg["mark_id"]]
    await save_truth(hass, store)
    _refresh_mark_refs(core, store)
    connection.send_result(msg["id"], {"removed": before - len(store["marks"])})


@websocket_api.websocket_command({vol.Required("type"): "sextant/truth/evaluate", vol.Optional("entity"): str, vol.Optional("mark_id"): vol.Coerce(int)})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_truth_evaluate(hass, connection, msg):
    """Every mark (or one thing's, or one mark) re-solved under the settings in force
    now: the accuracy figure per thing, plus the full sweep for a single mark."""
    core = _core()
    store = await load_truth(hass)
    data = get_layout(hass)
    marks = [m for m in store.get("marks", []) if (not msg.get("entity") or m["entity"] == msg["entity"]) and (msg.get("mark_id") is None or m["id"] == msg["mark_id"])]
    if msg.get("mark_id") is not None:
        if not marks:
            return _error(connection, msg, "No such mark")
        mark = marks[0]
        rows = await _evaluate_mark(hass, core, mark)
        return connection.send_result(msg["id"], {"mark": _mark_public(mark), "rows": rows, "current_weight": _current_weight(core, data, mark["entity"])})
    by_mark, per_mark = {}, []
    for mark in marks:
        w = _current_weight(core, data, mark["entity"])
        rows = await _evaluate_mark(hass, core, mark, weights=(w,), gains=(1.0,))
        row = rows[0] if rows else None
        by_mark[mark["id"]] = (mark["entity"], row)
        per_mark.append({**_mark_public(mark), "current": row})
    connection.send_result(msg["id"], {"things": truth_mod.summarize(by_mark), "marks": per_mark})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/truth/apply",
    vol.Required("entity"): str,
    vol.Required("weight"): vol.Coerce(float),
    vol.Optional("gain", default=1.0): vol.Coerce(float),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_truth_apply(hass, connection, msg):
    """Make an evaluated row the thing's settings: its blend weight, and the gain
    multiplier folded into its learned gain (and remembered across restarts)."""
    core = _core()
    entity, weight, gain = msg["entity"], float(msg["weight"]), float(msg["gain"])
    if not 0.0 <= weight <= 1.0 or not gain > 0:
        return _error(connection, msg, "weight must be 0..1 and gain positive")
    async with LAYOUT_LOCK:
        data = get_layout_for_edit(hass)
        if not isinstance(data, dict):
            return _error(connection, msg, "No layout saved yet")
        weights = data.get("thing_fp_weights") if isinstance(data.get("thing_fp_weights"), dict) else {}
        weights[entity] = round(weight, 3)
        data["thing_fp_weights"] = weights
        seeds = data.get("thing_fp_gains") if isinstance(data.get("thing_fp_gains"), dict) else {}
        new_gain = core._fingerprint_db.thing_gain.get(entity, seeds.get(entity, 1.0)) * gain
        new_gain = min(fingerprint_mod.LEARNED_GAIN_MAX, max(fingerprint_mod.LEARNED_GAIN_MIN, new_gain))
        seeds[entity] = round(new_gain, 4)
        data["thing_fp_gains"] = seeds
        core._fingerprint_db.thing_gain[entity] = new_gain
        await save_layout(hass, data)
    connection.send_result(msg["id"], {"entity": entity, "fp_weight": weights[entity], "thing_gain": seeds[entity], "estimator": truth_mod.estimator_for(weight)})


# --- history -----------------------------------------------------------------


def _history_denied(hass, connection, msg) -> bool:
    """True (and an error sent) when history is for admins only and the caller is not one."""
    if not _core()._tuning(get_layout(hass), "history_admin_only"):
        return False
    if getattr(getattr(connection, "user", None), "is_admin", False):
        return False
    _error(connection, msg, "Location history is for administrators on this install")
    return True


def _history(hass):
    core = _core()
    hist = core.get_position_history(hass)
    hist.configure(history_mod.history_config(get_layout(hass)))
    hist.evict_all()
    return hist


@websocket_api.websocket_command({vol.Required("type"): "sextant/history/index"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_history_index(hass, connection, msg):
    if _history_denied(hass, connection, msg):
        return
    core = _core()
    hist = _history(hass)
    files, size = await hass.async_add_executor_job(history_mod.disk_usage, core.history_dir(hass))
    connection.send_result(msg["id"], {
        "now": time.time(),
        "config": dict(hist.cfg),
        "things": [dict(hist.retained(e) or {}, ent=e) for e in hist.entities()],
        "disk": {"files": files, "bytes": size},
    })


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/history/get",
    vol.Required("entity"): str,
    vol.Optional("from"): vol.Coerce(float),
    vol.Optional("to"): vol.Coerce(float),
    vol.Optional("max_points"): vol.Coerce(int),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_history_get(hass, connection, msg):
    if _history_denied(hass, connection, msg):
        return
    core = _core()
    hist = _history(hass)
    now = time.time()
    to = msg.get("to") or now
    frm = msg.get("from") or (to - hist.cfg["max_age"])
    if frm > to:
        frm, to = to, frm
    max_points = int(min(max(2, msg.get("max_points") or core.HISTORY_DEFAULT_POINTS), core.HISTORY_MAX_QUERY_POINTS))
    data = hist.query(msg["entity"], frm, to, max_points)
    data.update({"now": now, "from": frm, "to": to, "retained": hist.retained(msg["entity"]), "config": dict(hist.cfg)})
    connection.send_result(msg["id"], data)


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/history/timeline",
    vol.Required("entity"): str,
    vol.Optional("hours"): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=24 * 31)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_history_timeline(hass, connection, msg):
    """Where one thing has been, as stays: floor, room, spot, from - to.

    What the Live page's timeline and its "here for" line are drawn from.
    Covers the last ``hours`` (default 24), capped at what history retains.
    """
    if _history_denied(hass, connection, msg):
        return
    hist = _history(hass)
    now = time.time()
    span = min(float(msg.get("hours") or 24.0) * 3600.0, hist.cfg["max_age"])
    data = hist.timeline(msg["entity"], now - span, now)
    data.update({"now": now, "retained": hist.retained(msg["entity"])})
    connection.send_result(msg["id"], data)


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/floor_bias_map",
    vol.Required("floor"): str,
    vol.Required("other"): str,
})
@websocket_api.async_response
async def ws_floor_bias_map(hass, connection, msg):
    """This floor's election prior against another's, per half-metre cell (see floor_bias_map)."""
    core = _core()
    layout = get_layout(hass)
    frames = core._floor_frames(hass, layout)
    result = await hass.async_add_executor_job(core.floor_bias_map, layout, frames, msg["floor"], msg["other"])
    if result is None:
        return _error(connection, msg, "both floors need a scale and this one needs rooms")
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "sextant/thing/readings", vol.Required("entity"): str})
@websocket_api.async_response
async def ws_thing_readings(hass, connection, msg):
    """Per placed proxy, the reading Sextant would use for one thing, and why not.

    The answer to "Bermuda hears it there, so why does Sextant not use it":
    for every placed receiver, whether a reading was found by the proxy's
    address or by its slug, its distance and age, and whether the stale gate
    would drop it. Also lists every prefix Bermuda's tracked devices publish
    that resembles this thing's, since two devices sharing a prefix would
    overwrite each other's readings.
    """
    core = _core()
    ent = msg["entity"]
    layout = get_layout(hass) or {}
    max_age = core._reading_max_age(layout)
    by_address = bermuda_source.async_get_readings_by_address(hass) or {}
    by_slug = bermuda_source.async_get_readings(hass) or {}
    rows = []
    for floor in layout.get("floor", []):
        for rx in floor.get("receivers", []):
            address = str(rx.get("address") or "").lower()
            reading, source = by_address.get((ent, address)), "address"
            if reading is None:
                reading, source = by_slug.get((ent, rx.get("entity_id"))), "slug"
            age = None if reading is None else reading.get("age")
            rows.append({
                "floor": floor.get("name"),
                "receiver": rx.get("entity_id"),
                "address": address or None,
                "source": source if reading is not None else None,
                "distance": None if reading is None else reading.get("distance"),
                "age": age,
                "stale": bool(max_age and age is not None and age > max_age),
            })
    stem = ent.rsplit("_", 1)[0]
    prefixes = sorted({p for p, _a in by_address if p == ent or p.startswith(stem)})
    connection.send_result(msg["id"], {
        "entity": ent, "max_age": max_age, "receivers": rows, "similar_prefixes": prefixes,
        "address_keys_for_entity": sum(1 for p, _a in by_address if p == ent),
    })


@websocket_api.websocket_command({vol.Required("type"): "sextant/history/clear", vol.Optional("entity"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_history_clear(hass, connection, msg):
    core = _core()
    hist = core.get_position_history(hass)
    entity = msg.get("entity")
    dirpath = core.history_dir(hass)
    async with core._history_lock(hass):
        hist.forget(entity or None)
        if entity:
            removed = await hass.async_add_executor_job(history_mod.drop_entity, dirpath, entity)
        else:
            removed = await hass.async_add_executor_job(history_mod.clear_segments, dirpath)
    connection.send_result(msg["id"], {"cleared": entity or "*", "removed": removed})


# --- calibration, self-test, receivers ------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "sextant/calibration/status"})
@websocket_api.async_response
async def ws_calibration_status(hass, connection, msg):
    from .calibration import _status_payload, get_calibration_state  # noqa: PLC0415

    connection.send_result(msg["id"], _status_payload(get_calibration_state(hass)))


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/calibration/action",
    vol.Required("action"): str,
    vol.Optional("floor"): vol.Any(None, str),
    vol.Optional("duration"): vol.Any(None, vol.Coerce(int)),
    vol.Optional("enabled"): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_calibration_action(hass, connection, msg):
    from .calibration import async_calibration_action  # noqa: PLC0415

    try:
        payload = await async_calibration_action(hass, msg)
    except ValueError as e:
        return _error(connection, msg, e)
    connection.send_result(msg["id"], payload)


@websocket_api.websocket_command({vol.Required("type"): "sextant/selftest"})
@websocket_api.async_response
async def ws_selftest(hass, connection, msg):
    from .calibration import get_calibration_state  # noqa: PLC0415

    core = _core()
    samples = {k: list(v) for k, v in get_calibration_state(hass).get("samples", {}).items()}
    result = await hass.async_add_executor_job(core.run_selftest, hass, samples)
    state, attrs = core._selftest_summary(result)
    connection.send_result(msg["id"], {"result": result, "state": state, "summary": attrs, "breakdown": core.selftest_breakdown(result)})


# What a proxy's own entities are called, whatever the integration prefixes
# them with: (key, suffix) pairs matched against the entity id.
PROXY_FACTS = (
    ("esphome_version", "_esphome_version"),
    ("project_version", "_project_version"),
    ("project_name", "_project_name"),
    ("uptime", "_uptime"),
    ("ip", "_ip"),
    ("ssid", "_ssid"),
    ("wifi_signal", "_wifi_signal"),
    ("temperature", "_internal_temperature"),
    ("reset_reason", "_reset_reason"),
    ("chip_mac", "_esp_mac_address"),
    ("generation", "_wifi_generation"),
    ("channel", "_wifi_channel"),
    ("adverts_forwarded", "_ble_adverts_forwarded"),
    ("adverts_dropped", "_ble_adverts_dropped"),
    ("drop_rate", "_ble_advert_drop_rate"),
    ("irks_loaded", "_ble_irks_loaded"),
    ("rpas_dropped", "_ble_rpas_dropped"),
    ("uuid_allowed", "_ble_service_uuid_allowed"),
)


@websocket_api.websocket_command({vol.Required("type"): "sextant/proxy/info", vol.Required("proxy"): str})
@websocket_api.async_response
async def ws_proxy_info(hass, connection, msg):
    """What one proxy is and what it is doing: its own sensors, and how many
    things it is hearing right now.

    A proxy's entities are named after it but prefixed differently by each
    integration (esphome names some for the area too), so they are found by
    matching the proxy's slug and a known suffix rather than by guessing a
    whole entity id.
    """
    core = _core()
    slug = str(msg["proxy"])
    layout = get_layout(hass) or {}
    rx = next((r for f in layout.get("floor", []) for r in f.get("receivers", [])
               if r.get("entity_id") == slug), None)
    if rx is None:
        return _error(connection, msg, f"No proxy called {slug} is placed")
    address = str(rx.get("address") or "").lower()

    # Its own entities, found through its device: a proxy's entity ids are
    # not always named after it (a Shelly running ESPHome publishes its
    # version as ble_<name>_esphome_version), but they all hang off the same
    # device. Anything named after it counts too, for proxies with no device.
    device = _proxy_device(hass, slug, address)
    candidates = set(device.pop("entity_ids", ()))
    facts = {}
    for state in hass.states.async_all():
        eid = state.entity_id
        if eid not in candidates and slug not in eid:
            continue
        for key, suffix in PROXY_FACTS:
            if eid.endswith(suffix) and key not in facts:
                facts[key] = {"entity_id": eid, "state": state.state,
                              "unit": state.attributes.get("unit_of_measurement"),
                              "name": state.attributes.get("friendly_name")}

    # How many things it hears: the readings Bermuda has for it right now,
    # judged by the same staleness gate the solver uses.
    max_age = core._reading_max_age(layout)
    # A set of things: both maps carry the same reading, under the address
    # and under the slug, so adding the two counted every thing twice.
    heard_things = set()
    for readings, want in ((bermuda_source.async_get_readings_by_address(hass), address),
                           (bermuda_source.async_get_readings(hass), slug)):
        for (ent, key), reading in (readings or {}).items():
            if key == want and isinstance(reading, dict):
                age = reading.get("age")
                if age is None or age <= max_age:
                    heard_things.add(ent)
    heard = len(heard_things)

    connection.send_result(msg["id"], {
        "proxy": slug,
        "address": address,
        "height": rx.get("height"),
        "facts": facts,
        "heard": heard,
        "device": device,
    })


def _proxy_device(hass, slug, address):
    """What the device registry knows: the board and firmware of the node
    itself, its Wi-Fi MAC, and the chip, which ESPHome records on the separate
    device it creates for the Bluetooth address Bermuda tracks.
    """
    from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: PLC0415

    devices, entities = dr.async_get(hass), er.async_get(hass)
    out = {}
    # Home Assistant's bluetooth integration gives the address Bermuda tracks
    # a device of its own, carrying the chip and pointing at the node that
    # owns it. Some nodes claim that address too, so the chip is only read
    # from a device that hangs off another - never from the node itself.
    matches = [d for d in devices.devices.values()
               if any(kind == "bluetooth" and str(v).lower() == address for kind, v in d.connections)]
    scanner = next((d for d in matches if d.via_device_id), None)
    node = devices.async_get(scanner.via_device_id) if scanner else next((d for d in matches if not d.via_device_id), None)
    if node is None:
        entry = next((e for e in entities.entities.values() if slug in e.entity_id and e.device_id), None)
        node = devices.async_get(entry.device_id) if entry else None
    if scanner is None and node is not None:
        scanner = next((d for d in devices.devices.values()
                        if d.via_device_id == node.id and any(kind == "bluetooth" for kind, _ in d.connections)), None)
    if scanner:
        out["chip"] = scanner.model
    if node:
        out.update(board=node.model, maker=node.manufacturer, firmware=node.sw_version,
                   wifi_mac=next((v for kind, v in node.connections if kind == "mac"), None),
                   entity_ids=[e.entity_id for e in er.async_entries_for_device(entities, node.id, True)])
    return out


@websocket_api.websocket_command({vol.Required("type"): "sextant/scanner_linking"})
@websocket_api.async_response
async def ws_scanner_linking(hass, connection, msg):
    core = _core()
    layout = get_layout(hass)
    connection.send_result(msg["id"], core._scanner_linking(hass, json.dumps(layout) if layout else ""))


@websocket_api.websocket_command({vol.Required("type"): "sextant/beacon_links"})
@websocket_api.async_response
async def ws_beacon_links(hass, connection, msg):
    """For every tracked device, the receivers currently hearing it, nearest first."""
    core = _core()
    connection.send_result(msg["id"], {"beacons": _safe(lambda: core._beacon_links(hass), [])})


@websocket_api.websocket_command({vol.Required("type"): "sextant/receivers"})
@websocket_api.async_response
async def ws_receivers(hass, connection, msg):
    """Every placed receiver with its liveness, plus scanners Bermuda knows
    that are not placed on any floor."""
    core = _core()
    dom = hass.data.get(core.DOMAIN, {})
    offline = set(dom.get("rl_offline") or [])
    ages = dom.get("rl_ages") or {}
    directory = bermuda_source.async_get_scanner_directory(hass) or {}
    by_slug = {info.get("slug"): (addr, info) for addr, info in directory.items() if info.get("slug")}
    layout = get_layout(hass) or {}
    placed = []
    placed_addresses = set()
    for floor in layout.get("floor", []) if isinstance(layout, dict) else []:
        for rx in floor.get("receivers", []) or []:
            slug = rx.get("entity_id")
            address = (rx.get("address") or "").lower() or None
            info = directory.get(address) if address else None
            if info is None and slug in by_slug:
                address, info = by_slug[slug]
            if address:
                placed_addresses.add(address)
            placed.append({
                "slug": slug,
                "address": address,
                "floor": floor.get("name"),
                "x": (rx.get("cords") or {}).get("x"),
                "y": (rx.get("cords") or {}).get("y"),
                "height": rx.get("height"),
                "correction": rx.get("correction"),
                "online": slug not in offline,
                "age": ages.get(slug),
                "name": info.get("name") if info else None,
                "area": info.get("area_name") if info else None,
                "last_seen_age": info.get("last_seen_age") if info else None,
                "matched": info is not None,
            })
    ignored = ignored_scanners(layout)
    unplaced = [
        {"address": addr, "slug": info.get("slug"), "name": info.get("name"), "area": info.get("area_name"),
         "last_seen_age": info.get("last_seen_age")}
        for addr, info in directory.items() if addr not in placed_addresses and addr not in ignored
    ]
    connection.send_result(msg["id"], {"placed": placed, "unplaced": unplaced, "ignored": _ignored_scanner_rows(hass, layout)})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/adjust_zones",
    vol.Optional("target", default="zones"): str,
    vol.Optional("zones", default=list): list,
    vol.Optional("subzones", default=list): list,
    vol.Optional("options", default=dict): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_adjust_zones(hass, connection, msg):
    from .zone_adjust import adjust_subzones, adjust_zones  # noqa: PLC0415

    target = "subzones" if msg.get("target") == "subzones" else "zones"
    zones, subzones, options = msg.get("zones") or [], msg.get("subzones") or [], msg.get("options") or {}
    if target == "subzones" and not subzones:
        return _error(connection, msg, "No sub-zones to adjust")
    if target == "zones" and not zones:
        return _error(connection, msg, "No zones to adjust")
    func = adjust_subzones if target == "subzones" else adjust_zones
    try:
        result = await hass.async_add_executor_job(func, zones, subzones, options)
    except Exception as e:  # noqa: BLE001
        _LOGGER.error("adjust_zones (%s) failed: %s", target, e)
        return _error(connection, msg, "Zone adjustment failed")
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/registration",
    vol.Optional("layout"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_registration(hass, connection, msg):
    """How the floors stack, from their shared pins (registration.py).

    Given a ``layout`` it grades that - the Edit page sends its unsaved draft,
    so a pin's effect on the fit shows while it is still being dragged.
    """
    from . import registration  # noqa: PLC0415

    layout = msg.get("layout") or get_layout(hass) or {}
    # Off the loop: the suspect search is combinatorial in the pin count, and
    # this is called with whatever draft the editor holds, on every drag.
    connection.send_result(msg["id"], await hass.async_add_executor_job(registration.report, layout))


# --- KPI --------------------------------------------------------------------------


class _KpiUnavailable(Exception):
    """The recorder cannot answer (not loaded, or the query failed)."""


def _kpi_hours(msg):
    return max(0.25, min(float(msg.get("hours") or 12.0), 24 * 14))


async def _compute_kpi(hass, hours):
    """Zone/floor stability over the last ``hours`` from the recorder."""
    entity_ids = sorted(
        s.entity_id for s in hass.states.async_all("sensor") if s.entity_id.endswith(kpi.SUFFIXES)
    )
    if not entity_ids:
        return {"hours": hours, "generated_at": datetime.now(timezone.utc).isoformat(), "entities": {}, "summary": {}}
    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder import history as rec_history  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise _KpiUnavailable("the recorder is not available") from e
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    try:
        states = await get_instance(hass).async_add_executor_job(
            rec_history.get_significant_states, hass, start, end, entity_ids, None, True, True, True, True,
        )
    except Exception as e:  # noqa: BLE001
        raise _KpiUnavailable(f"recorder query failed: {e}") from e
    # Room sensors also get their far moves counted against the plan's rooms.
    # Building the outlines and comparing every pair is geometry, so it runs
    # off the event loop like the recorder query above.
    neighbours = await hass.async_add_executor_job(kpi.room_neighbours, get_layout(hass))
    per_entity = {}
    for eid in entity_ids:
        rows = kpi.rows_from_recorder(states.get(eid))
        is_room = kpi.canonical(eid).endswith("_sextant_room")
        per_entity[eid] = kpi.compute_metrics(rows, hours, neighbours if is_room else None)
    return {
        "hours": hours,
        "generated_at": end.isoformat(),
        "entities": per_entity,
        "summary": kpi.summarise(per_entity),
    }


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/kpi",
    vol.Optional("hours", default=12.0): vol.Coerce(float),
    vol.Optional("baseline"): str,
})
@websocket_api.async_response
async def ws_kpi(hass, connection, msg):
    """Stability KPI for the window; with ``baseline`` also the deltas against
    a saved baseline (sextant/kpi/baseline/save)."""
    hours = _kpi_hours(msg)
    try:
        result = await _compute_kpi(hass, hours)
    except _KpiUnavailable as e:
        return _error(connection, msg, str(e))
    name = (msg.get("baseline") or "").strip()
    if name:
        baselines = await load_kpi_baselines(hass)
        base = baselines.get(name)
        if not isinstance(base, dict):
            return _error(connection, msg, f"no KPI baseline named {name!r}")
        result["baseline"] = {
            "name": name, "saved_at": base.get("saved_at"), "hours": base.get("hours"), "summary": base.get("summary"),
        }
        result["deltas"] = kpi.deltas(result, base)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "sextant/kpi/baselines"})
@websocket_api.async_response
async def ws_kpi_baselines(hass, connection, msg):
    baselines = await load_kpi_baselines(hass)
    rows = [
        {
            "name": name, "saved_at": b.get("saved_at"), "hours": b.get("hours"), "summary": b.get("summary"),
            "things": len(b.get("entities") or {}),
        }
        for name, b in baselines.items() if isinstance(b, dict)
    ]
    rows.sort(key=lambda r: r.get("saved_at") or "")
    connection.send_result(msg["id"], {"baselines": rows})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/kpi/baseline/save",
    vol.Required("name"): str,
    vol.Optional("hours", default=12.0): vol.Coerce(float),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_kpi_baseline_save(hass, connection, msg):
    """Compute the window now and keep it under ``name`` for later comparison."""
    name = msg["name"].strip()
    if not name or len(name) > 60:
        return _error(connection, msg, "baseline name must be 1-60 characters")
    hours = _kpi_hours(msg)
    try:
        result = await _compute_kpi(hass, hours)
    except _KpiUnavailable as e:
        return _error(connection, msg, str(e))
    if not result["entities"]:
        return _error(connection, msg, "no zone sensors in the recorder window; nothing to save")
    baselines = await load_kpi_baselines(hass)
    baselines[name] = {
        "saved_at": result["generated_at"], "hours": hours,
        "entities": result["entities"], "summary": result["summary"],
    }
    await save_kpi_baselines(hass, baselines)
    connection.send_result(msg["id"], {"name": name, "saved_at": result["generated_at"], "things": len(result["entities"])})


@websocket_api.websocket_command({vol.Required("type"): "sextant/kpi/baseline/delete", vol.Required("name"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_kpi_baseline_delete(hass, connection, msg):
    baselines = await load_kpi_baselines(hass)
    if msg["name"] not in baselines:
        return _error(connection, msg, f"no KPI baseline named {msg['name']!r}")
    del baselines[msg["name"]]
    await save_kpi_baselines(hass, baselines)
    connection.send_result(msg["id"], {"deleted": msg["name"]})


# --- Bermuda management ------------------------------------------------------------


def _bermuda_result(connection, msg, value, feature="device_management"):
    if value is None:
        return _error(connection, msg, f"This Bermuda build has no {feature} API; update Bermuda")
    connection.send_result(msg["id"], value)


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/candidates", vol.Optional("max_age"): vol.Coerce(float)})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_candidates(hass, connection, msg):
    rows = bermuda_source.async_get_device_candidates(hass, max_age=msg.get("max_age"))
    if rows:
        # The proxies' own calibration iBeacon is not a device anyone tracks; keep it out of every list.
        rows = [r for r in rows if not str(r.get("address") or r.get("config_value") or "").lower().replace("-", "").startswith(PROBE_BEACON_UUID)]
    _bermuda_result(connection, msg, None if rows is None else {"candidates": rows})


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/tracked"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_tracked(hass, connection, msg):
    tracked = bermuda_source.async_get_tracked_devices(hass)
    _bermuda_result(connection, msg, None if tracked is None else {"tracked": tracked}, feature="tracked_devices")


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/bermuda/track",
    vol.Optional("add", default=list): [str],
    vol.Optional("remove", default=list): [str],
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_track(hass, connection, msg):
    add, remove = msg.get("add") or [], msg.get("remove") or []
    # Resolve the slugs of the devices being removed BEFORE Bermuda forgets
    # them, so their Sextant sensors and devices go too; otherwise Bermuda's
    # reload leaves four "unavailable" sensors per device behind.
    slugs = _tracked_slugs_for(hass, remove) if remove else []
    devices = await bermuda_source.async_set_tracked_devices(hass, add=add, remove=remove)
    if devices is not None and slugs:
        from .sensor import remove_sensors_for_things  # noqa: PLC0415 - sensor imports this package

        remove_sensors_for_things(hass, slugs)
    _bermuda_result(connection, msg, None if devices is None else {"configured_devices": devices})


def _tracked_slugs_for(hass, keys):
    """Thing slugs of the tracked devices named by address, unique_id or slug."""
    wanted = {str(k).lower() for k in keys}
    slugs = []
    for address, dev in (bermuda_source.async_get_tracked_devices(hass) or {}).items():
        ids = {str(address).lower(), str(dev.get("unique_id") or "").lower(), str(dev.get("slug") or "").lower()}
        if ids & wanted and dev.get("slug"):
            slugs.append(dev["slug"])
    return slugs


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/findmy"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_findmy(hass, connection, msg):
    rows = bermuda_source.async_get_findmy_accessories(hass)
    _bermuda_result(connection, msg, None if rows is None else {"accessories": rows})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/bermuda/findmy/add",
    vol.Required("accessory_json"): str,
    vol.Optional("name"): vol.Any(None, str),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_findmy_add(hass, connection, msg):
    try:
        added = await bermuda_source.async_add_findmy_accessory(hass, msg["accessory_json"], msg.get("name"))
    except Exception as e:  # noqa: BLE001 - FindMyKeyError carries the reason
        return _error(connection, msg, f"Invalid accessory keys: {e}")
    _bermuda_result(connection, msg, added)


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/irk/add",
    vol.Required("irk"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_irk_add(hass, connection, msg):
    """Add a phone or watch by its Identity Resolving Key.

    A modern phone changes its Bluetooth address every few minutes, so it can
    only be followed by the key that generates those addresses. Home Assistant
    owns that: private_ble_device resolves the rotating address and Bermuda
    tracks what it resolves. Rather than send people to a different page to
    paste a key, drive that integration's own flow from here and turn its two
    failures into something worth reading.
    """
    irk = str(msg["irk"]).strip()
    if not irk:
        return _error(connection, msg, "Paste the key first")
    flow = hass.config_entries.flow
    try:
        started = await flow.async_init("private_ble_device", context={"source": "user"})
        result = await flow.async_configure(started["flow_id"], {"irk": irk})
    except Exception as e:  # noqa: BLE001 - an unknown flow, or one that changed shape
        return _error(connection, msg, f"Home Assistant could not take the key: {e}")

    if result.get("type") == "create_entry":
        return connection.send_result(msg["id"], {"title": result.get("title"), "irk": irk})
    errors = result.get("errors") or {}
    reason = errors.get("irk") or result.get("reason") or "unknown"
    try:
        await flow.async_abort(started["flow_id"])
    except Exception:  # noqa: BLE001 - already gone
        pass
    if reason == "irk_not_valid":
        return _error(connection, msg, "That is not a valid key: it should be 32 hex characters, or base64 ending in '='")
    if reason == "irk_not_found":
        return _error(connection, msg, "The key is valid, but nothing near a proxy is using it right now. "
                                       "Wake the phone and keep it near a proxy for a moment, then try again")
    if reason == "bluetooth_not_available":
        return _error(connection, msg, "Home Assistant has no Bluetooth scanner, so it cannot check the key")
    return _error(connection, msg, f"Home Assistant refused the key: {reason}")


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/findmy/remove", vol.Required("address"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_findmy_remove(hass, connection, msg):
    removed = await bermuda_source.async_remove_findmy_accessory(hass, msg["address"])
    _bermuda_result(connection, msg, None if removed is None else {"removed": bool(removed)})


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/options"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_options(hass, connection, msg):
    options = bermuda_source.async_get_options(hass)
    _bermuda_result(connection, msg, None if options is None else {"options": options})


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/options/set", vol.Required("options"): dict})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_options_set(hass, connection, msg):
    try:
        options = await bermuda_source.async_set_options(hass, dict(msg["options"]))
    except ValueError as e:
        return _error(connection, msg, e)
    _bermuda_result(connection, msg, None if options is None else {"options": options})


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/scanners"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_scanners(hass, connection, msg):
    directory = bermuda_source.async_get_scanner_directory(hass)
    _bermuda_result(connection, msg, None if directory is None else {"scanners": directory}, feature="scanners")


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/tile_identities"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_tile_identities(hass, connection, msg):
    """Every Tile ID Bermuda has read, with the area / loudest receiver it was last heard at."""
    ids = bermuda_source.async_get_tile_identities(hass)
    _bermuda_result(connection, msg, None if ids is None else {"identities": ids}, feature="tile_identity")


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/bermuda/tile/bind",
    vol.Required("tile_id"): str,
    vol.Required("uid"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_tile_bind(hass, connection, msg):
    """Declare which Tile ID belongs to a configured Tile (the user knows which tag is on which keys)."""
    try:
        result = await bermuda_source.async_bind_tile(hass, msg["tile_id"], msg["uid"])
    except ValueError as e:
        return _error(connection, msg, str(e))
    _bermuda_result(connection, msg, result, feature="tile_identity")


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/bermuda/tile/adopt",
    vol.Required("tile_id"): str,
    vol.Required("address"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_tile_adopt(hass, connection, msg):
    """The user points at the live Tile address a configured Tile is using now."""
    try:
        result = await bermuda_source.async_bind_tile_address(hass, msg["tile_id"], msg["address"])
    except ValueError as e:
        return _error(connection, msg, str(e))
    _bermuda_result(connection, msg, result, feature="tile adoption")


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/scanner_ranging", vol.Optional("max_age"): vol.Coerce(float)})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_scanner_ranging(hass, connection, msg):
    """Receiver-to-receiver ranges from Bermuda: {"scanners": {tx: {rx: {distance, age, ...}}}}.
    What the fingerprint references are built from; also how an unplaced
    receiver's position is estimated."""
    ranging = bermuda_source.async_get_scanner_ranging(hass, max_age=msg.get("max_age"))
    _bermuda_result(connection, msg, ranging, feature="scanner_ranging")


@websocket_api.websocket_command({vol.Required("type"): "sextant/bermuda/tiles"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_bermuda_tiles(hass, connection, msg):
    diag = bermuda_source.async_get_tile_diagnostics(hass)
    _bermuda_result(connection, msg, None if diag is None else {"tiles": diag}, feature="tile")


def ignored_scanners(layout) -> set:
    """Scanner addresses the user has told the reports to leave alone: a
    kiosk, a test board, an outdoor proxy that is deliberately on no floor.
    Kept as a top-level layout key (``ignored_scanners``), so an editor save,
    which owns floor geometry only, never drops it."""
    raw = layout.get("ignored_scanners") if isinstance(layout, dict) else None
    return {str(a).lower() for a in raw if isinstance(a, str) and a} if isinstance(raw, list) else set()


def _unplaced_scanners(hass, layout):
    """Scanners Bermuda hears (in the last hour) that sit on no floor of the layout."""
    ignored = ignored_scanners(layout)
    placed_addresses, placed_slugs = set(), set()
    for floor in layout.get("floor", []):
        for r in floor.get("receivers", []):
            if r.get("address"):
                placed_addresses.add(str(r["address"]).lower())
            if r.get("entity_id"):
                placed_slugs.add(str(r["entity_id"]))
    out = []
    for address, info in (bermuda_source.async_get_scanner_directory(hass) or {}).items():
        age = info.get("last_seen_age")
        if isinstance(age, (int, float)) and age > 3600:
            continue
        if address in placed_addresses or (info.get("slug") and info["slug"] in placed_slugs):
            continue
        if address in ignored:
            continue
        out.append({"slug": info.get("slug") or address, "name": info.get("name") or info.get("slug") or address, "address": address})
    return sorted(out, key=lambda u: u["name"].lower())


def _ignored_scanner_rows(hass, layout):
    """The ignored scanners with their current names, for a report to offer un-ignoring."""
    directory = bermuda_source.async_get_scanner_directory(hass) or {}
    rows = []
    for address in sorted(ignored_scanners(layout)):
        info = directory.get(address) or {}
        rows.append({"address": address, "slug": info.get("slug") or address, "name": info.get("name") or info.get("slug") or address})
    return rows


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/scanner/ignore",
    vol.Required("address"): str,
    vol.Required("ignored"): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_scanner_ignore(hass, connection, msg):
    """Leave a scanner out of (or put it back into) the unplaced lists on the
    Proxies and Advice pages. Returns the ignored list."""
    address = str(msg["address"]).lower().strip()
    if not address:
        return _error(connection, msg, "address required")
    async with LAYOUT_LOCK:
        layout = get_layout_for_edit(hass)
        if not isinstance(layout, dict):
            return _error(connection, msg, "No layout yet")
        current = ignored_scanners(layout)
        if msg["ignored"]:
            current.add(address)
        else:
            current.discard(address)
        layout["ignored_scanners"] = sorted(current)
        await save_layout(hass, layout)
    connection.send_result(msg["id"], {"ignored": _ignored_scanner_rows(hass, layout)})



@websocket_api.websocket_command({vol.Required("type"): "sextant/advice"})
@websocket_api.async_response
async def ws_advice(hass, connection, msg):
    """Which rooms the proxies serve worst and where a proxy would help (sextant.advice)."""
    from .advice import advise  # noqa: PLC0415
    from .calibration import get_calibration_state  # noqa: PLC0415

    core = _core()
    layout = get_layout(hass)
    if not isinstance(layout, dict) or not layout.get("floor"):
        return _error(connection, msg, "No floor plan yet: add a floor and place proxies first")
    samples = {k: list(v) for k, v in get_calibration_state(hass).get("samples", {}).items()}
    selftest = await hass.async_add_executor_job(core.run_selftest, hass, samples)
    unplaced = _unplaced_scanners(hass, layout)
    out = await hass.async_add_executor_job(advise, layout, selftest, unplaced)
    out["ignored"] = _ignored_scanner_rows(hass, layout)
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/thing/forget",
    vol.Required("entity"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_thing_forget(hass, connection, msg):
    """Forget a thing: its remembered last sighting, its position history,
    and - unless Bermuda still tracks it - its settings.

    A thing whose identity has gone (a phone after an IRK swap, a Tile after
    its ID rotated) lived on in the layout's thing_* maps and the list of
    things, as a ghost that was "away" for ever, with nothing in the UI able
    to remove it. A thing Bermuda still tracks is only away: its sighting and
    history go, and it comes back with its name and settings when heard.
    """
    core = _core()
    ent = str(msg["entity"]).strip()
    if not ent:
        return _error(connection, msg, "Which thing?")
    tracked = any(
        str(dev.get("slug") or "").lower() == ent.lower()
        for dev in (bermuda_source.async_get_tracked_devices(hass) or {}).values()
    )
    core._forget_thing_state(ent)
    hist = core.get_position_history(hass)
    async with core._history_lock(hass):
        hist.forget(ent)
        removed_points = await hass.async_add_executor_job(history_mod.drop_entity, core.history_dir(hass), ent)
    dropped = []
    if not tracked:
        async with LAYOUT_LOCK:
            layout = get_layout_for_edit(hass)
            if isinstance(layout, dict):
                for key, table in layout.items():
                    if key.startswith("thing_") and isinstance(table, dict) and ent in table:
                        del table[ent]
                        dropped.append(key)
                if dropped:
                    await save_layout(hass, layout)
    try:
        await core._save_runtime(hass)   # so a restart does not bring the sighting back
    except Exception as e:  # noqa: BLE001 - the in-memory forget still stands
        _LOGGER.debug("Runtime not saved after forgetting %s: %s", ent, e)
    connection.send_result(msg["id"], {"entity": ent, "tracked": tracked, "history_removed": removed_points, "settings_dropped": dropped})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/person/trackers/set",
    vol.Required("person"): str,
    vol.Required("trackers"): [str],
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_person_trackers_set(hass, connection, msg):
    """A person's GPS trackers, in the order they are tried when Sextant has
    lost them (layout person_trackers; see persons.choose_gps)."""
    person = str(msg["person"])
    if not person.startswith("person."):
        return _error(connection, msg, "person must be a person.* entity")
    trackers = list(dict.fromkeys(t for t in msg["trackers"] if isinstance(t, str) and t.startswith("device_tracker.")))
    async with LAYOUT_LOCK:
        layout = get_layout_for_edit(hass)
        if not isinstance(layout, dict):
            return _error(connection, msg, "No layout yet")
        table = layout.setdefault("person_trackers", {})
        if trackers:
            table[person] = trackers
        else:
            table.pop(person, None)
        if not table:
            layout.pop("person_trackers", None)
        await save_layout(hass, layout)
    connection.send_result(msg["id"], {"person": person, "trackers": trackers})


@websocket_api.websocket_command({vol.Required("type"): "sextant/snapshots/list"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_snapshots_list(hass, connection, msg):
    """Every kept copy of the layout, newest first."""
    from .snapshots import listing  # noqa: PLC0415

    connection.send_result(msg["id"], {"snapshots": await listing(hass)})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/snapshots/restore",
    vol.Required("id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_snapshots_restore(hass, connection, msg):
    """Put a kept copy back.

    The restore is itself a save, so the layout being replaced is snapshotted
    on the way out like any other: changing your mind about a restore costs
    nothing.
    """
    from .snapshots import read  # noqa: PLC0415

    try:
        layout = await read(hass, msg["id"])
    except ValueError:
        return _error(connection, msg, "No such snapshot")
    except FileNotFoundError:
        return _error(connection, msg, "That snapshot is no longer there")
    except Exception as e:  # noqa: BLE001
        return _error(connection, msg, f"Could not read that snapshot: {e}")
    if not isinstance(layout, dict) or not layout.get("floor"):
        return _error(connection, msg, "That snapshot has no floors in it")
    async with LAYOUT_LOCK:
        await save_layout(hass, layout)
    try:
        core = _core()
        core.refresh_receivers_from_coords(hass, json.dumps(layout))
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("refresh_receivers_from_coords: %s", e)
    connection.send_result(msg["id"], {"version": get_layout_version(hass)})


# --- Robot vacuums (robots.py) ------------------------------------------------

ROBOT_INTEGRATION = "roborock"


def _robot_vacuums(hass):
    """Every Roborock vacuum entity Home Assistant knows."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    return sorted(e.entity_id for e in er.async_get(hass).entities.values()
                  if e.platform == ROBOT_INTEGRATION and e.domain == "vacuum" and not e.disabled_by)


def _robot_public(hass, vac, cfg):
    st = hass.states.get(vac)
    rs = getattr(_core(), "_robot_state", {}).get(vac) or {}
    return {
        "vacuum": vac,
        # Position reads since Home Assistant started: how often the robot's
        # map fetch fails, which decides whether robot_poll_secs is right.
        "reads": {"ok": rs.get("reads_ok", 0), "failed": rs.get("reads_failed", 0), "last_error": rs.get("last_error")},
        "thing": _core().robot_thing(vac),
        "name": (st.attributes.get("friendly_name") if st else None) or vac,
        "state": st.state if st else None,
        "floor": (cfg or {}).get("floor"),
        "fit": (cfg or {}).get("fit"),
        "dock_marked": bool((cfg or {}).get("dock_plan")),
        "map_name": (cfg or {}).get("map_name"),
        "unmatched": (cfg or {}).get("unmatched"),
        "aligned_at": (cfg or {}).get("aligned_at"),
    }


@websocket_api.websocket_command({vol.Required("type"): "sextant/robot/list"})
@websocket_api.async_response
async def ws_robot_list(hass, connection, msg):
    """The Roborock vacuums, and how each is lined up with the plan (if it is)."""
    robots = (get_layout(hass) or {}).get("robots") or {}
    vacuums = sorted(set(_robot_vacuums(hass)) | set(robots))
    connection.send_result(msg["id"], {
        "robots": [_robot_public(hass, v, robots.get(v)) for v in vacuums],
        "action_available": hass.services.has_service(ROBOT_INTEGRATION, "get_vacuum_map_rooms"),
    })


async def _robot_align(hass, vac, floor_name, dock_plan=None):
    """Read the robot's map, match its rooms to the floor's, fit, and store.
    Returns (config, error). ``dock_plan`` replaces the stored dock mark."""
    try:
        resp = await hass.services.async_call(
            ROBOT_INTEGRATION, "get_vacuum_map_rooms", {"entity_id": vac},
            blocking=True, return_response=True,
        )
    except Exception as e:  # noqa: BLE001 - the integration's own message says what went wrong
        return None, f"{vac} did not give its map: {e}"
    data = (resp or {}).get(vac) if isinstance(resp, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), list):
        return None, f"{vac} did not give its map"
    async with LAYOUT_LOCK:
        layout = get_layout_for_edit(hass)
        if not isinstance(layout, dict):
            return None, "No layout yet"
        floor = next((f for f in layout.get("floor", []) if f.get("name") == floor_name), None)
        if floor is None or not isinstance(floor.get("scale"), (int, float)) or floor["scale"] <= 0:
            return None, f"{floor_name} has no scale yet; set it on the Edit page first"
        table = layout.setdefault("robots", {})
        cfg = dict(table.get(vac) or {})
        if cfg.get("floor") != floor_name:
            cfg.pop("dock_plan", None)  # a dock marked on another floor means nothing here
        if dock_plan is not None:
            cfg["dock_plan"] = [round(float(dock_plan[0]), 2), round(float(dock_plan[1]), 2)]
        charger = data.get("charger")
        dock_map = [float(charger["x"]), float(charger["y"])] if isinstance(charger, dict) else None
        matches = robots_mod.match_rooms(data["rooms"], floor)
        dock = (dock_map, cfg["dock_plan"]) if dock_map and cfg.get("dock_plan") else None
        result = robots_mod.fit(matches, floor["scale"], dock)
        matched = {m[0] for m in matches}
        plan_matched = {m[1] for m in matches}
        cfg.update({
            "floor": floor_name,
            "fit": result,
            "dock_map": dock_map,
            "map_name": data.get("map_name"),
            "unmatched": {
                "robot": [r.get("name") or f"segment {r.get('segment_id')}" for r in data["rooms"] if r.get("name") not in matched],
                "plan": [n for n, _a, _c in robots_mod.plan_rooms(floor) if n not in plan_matched],
            },
            "aligned_at": time.time(),
        })
        table[vac] = cfg
        # A robot's thing is a robot vacuum unless the user has said otherwise.
        classes = layout.setdefault("thing_classes", {})
        classes.setdefault(_core().robot_thing(vac), "robot")
        await save_layout(hass, layout)
    return cfg, None


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/robot/align",
    vol.Required("vacuum"): str,
    vol.Required("floor"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_robot_align(hass, connection, msg):
    """Line a vacuum's map up with a floor from the rooms both name (and the
    dock, once marked). Reads the map once through the Roborock integration's
    get_vacuum_map_rooms action."""
    vac = str(msg["vacuum"])
    if not vac.startswith("vacuum."):
        return _error(connection, msg, "vacuum must be a vacuum.* entity")
    if not hass.services.has_service(ROBOT_INTEGRATION, "get_vacuum_map_rooms"):
        return _error(connection, msg, "The Roborock integration has no get_vacuum_map_rooms action; install the roborock override 2026.9.3.2 or later")
    cfg, err = await _robot_align(hass, vac, str(msg["floor"]))
    if err:
        return _error(connection, msg, err)
    connection.send_result(msg["id"], _robot_public(hass, vac, cfg))


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/robot/dock",
    vol.Required("vacuum"): str,
    vol.Required("floor"): str,
    vol.Required("x"): vol.Coerce(float),
    vol.Required("y"): vol.Coerce(float),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_robot_dock(hass, connection, msg):
    """Where the vacuum's dock is on the plan: the fit's most trusted point.
    The robot reports its dock itself, so it need not be on it."""
    vac = str(msg["vacuum"])
    if not (math.isfinite(msg["x"]) and math.isfinite(msg["y"])):
        return _error(connection, msg, "x and y must be finite numbers")
    cfg, err = await _robot_align(hass, vac, str(msg["floor"]), dock_plan=(msg["x"], msg["y"]))
    if err:
        return _error(connection, msg, err)
    connection.send_result(msg["id"], _robot_public(hass, vac, cfg))


@websocket_api.websocket_command({vol.Required("type"): "sextant/radar/devices"})
@websocket_api.async_response
async def ws_radar_devices(hass, connection, msg):
    """Every device that reports mmWave target coordinates, for the Edit page:
    its name, model, area, range and installation angle as set on the device."""
    core = _core()
    specs = core._radar_specs(hass)
    out = []
    for device_id, spec in specs.items():
        angle = None
        if spec.get("angle_entity"):
            st = hass.states.get(spec["angle_entity"])
            try:
                angle = float(st.state) if st else None
            except (TypeError, ValueError):
                angle = None
        out.append({"device_id": device_id, "name": spec.get("name"), "model": spec.get("model"),
                    "area_id": spec.get("area_id"), "targets": len(spec.get("targets") or []),
                    "range_m": core.radars_mod.radar_range_m(spec, hass.states.get),
                    "installation_angle": angle, "fov_deg": core.radars_mod.FOV_DEG})
    connection.send_result(msg["id"], {"devices": sorted(out, key=lambda d: str(d["name"]))})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/radar/targets",
    vol.Required("device_id"): str,
    vol.Required("floor"): str,
    vol.Required("x"): vol.Coerce(float),
    vol.Required("y"): vol.Coerce(float),
    vol.Optional("heading", default=0.0): vol.Coerce(float),
    vol.Optional("flip", default=False): bool,
    vol.Optional("height_m"): vol.Any(None, vol.Coerce(float)),
    vol.Optional("room"): vol.Any(None, str),
})
@websocket_api.async_response
async def ws_radar_targets(hass, connection, msg):
    """One radar's targets right now, placed as the Edit page has it (which
    may not be saved yet): what you see while you walk past it to check the
    direction."""
    core = _core()
    spec = core._radar_specs(hass).get(msg["device_id"])
    floor = next((f for f in (get_layout(hass) or {}).get("floor", []) if f.get("name") == msg["floor"]), None)
    if spec is None or floor is None or not isinstance(floor.get("scale"), (int, float)):
        return connection.send_result(msg["id"], {"targets": []})
    radar = {"cords": {"x": msg["x"], "y": msg["y"]}, "heading": msg["heading"], "flip": msg["flip"],
             "height_m": msg.get("height_m"), "room": msg.get("room")}
    out = core._radar_targets(hass, get_layout(hass), floor, radar, spec)
    room, _poly = core._radar_room(hass, get_layout(hass), floor, radar)
    connection.send_result(msg["id"], {"targets": out, "room": room})


@websocket_api.websocket_command({vol.Required("type"): "sextant/election_log/clear"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_election_log_clear(hass, connection, msg):
    """Delete the election log's files (config/sextant_election_log)."""
    from . import election_log  # noqa: PLC0415

    files, size = await election_log.clear(election_log.get(hass).dirpath)
    connection.send_result(msg["id"], {"files": files, "bytes": size})


@websocket_api.websocket_command({
    vol.Required("type"): "sextant/interval/set",
    # Seconds between cycles for the next quarter hour; null goes back to the
    # configured interval at once.
    vol.Required("secs"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=1, max=600))),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_interval_set(hass, connection, msg):
    """Refresh faster (or slower) for a while, from the countdown's menu."""
    core = _core()
    secs = msg["secs"]
    if secs is not None and int(secs) not in core.INTERVAL_CHOICES and float(secs) != float(core.secToUpdate):
        return _error(connection, msg, f"Pick one of {sorted(core.INTERVAL_CHOICES)} seconds")
    info = core.set_interval_override(secs)
    connection.send_result(msg["id"], info)


@websocket_api.websocket_command({vol.Required("type"): "sextant/robot/remove", vol.Required("vacuum"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_robot_remove(hass, connection, msg):
    """Stop placing a vacuum. Its sensors go with the next reconcile."""
    vac = str(msg["vacuum"])
    async with LAYOUT_LOCK:
        layout = get_layout_for_edit(hass)
        if isinstance(layout, dict) and vac in (layout.get("robots") or {}):
            layout["robots"].pop(vac, None)
            if not layout["robots"]:
                layout.pop("robots", None)
            await save_layout(hass, layout)
    connection.send_result(msg["id"], {"vacuum": vac, "removed": True})


COMMANDS = (
    ws_advice,
    ws_snapshots_list, ws_snapshots_restore, ws_thing_forget, ws_person_trackers_set,
    ws_robot_list, ws_robot_align, ws_robot_dock, ws_robot_remove, ws_election_log_clear, ws_interval_set,
    ws_radar_devices, ws_radar_targets,
    ws_layout_get, ws_layout_save, ws_tuning_set, ws_thing_tune,
    ws_history_index, ws_history_get, ws_history_timeline, ws_history_clear, ws_thing_readings, ws_floor_bias_map,
    ws_calibration_status, ws_calibration_action, ws_selftest, ws_scanner_linking, ws_receivers, ws_beacon_links,
    ws_proxy_info,
    ws_adjust_zones, ws_registration, ws_scanner_ignore, ws_kpi, ws_kpi_baselines, ws_kpi_baseline_save, ws_kpi_baseline_delete,
    ws_truth_mark, ws_truth_list, ws_truth_delete, ws_truth_evaluate, ws_truth_apply,
    ws_bermuda_candidates, ws_bermuda_tracked, ws_bermuda_track, ws_bermuda_findmy, ws_bermuda_findmy_add,
    ws_bermuda_findmy_remove, ws_bermuda_options, ws_bermuda_options_set, ws_bermuda_scanners, ws_bermuda_tiles,
    ws_bermuda_scanner_ranging, ws_bermuda_tile_identities, ws_bermuda_tile_bind, ws_bermuda_tile_adopt,
    ws_irk_add,
)


def async_register(hass) -> None:
    for command in COMMANDS:
        websocket_api.async_register_command(hass, command)
