import aiofiles
import aiofiles.os
import time
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel
from homeassistant.components.websocket_api import async_register_command, ActiveConnection, websocket_command
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.template import Template
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.const import UnitOfLength
from homeassistant.util.unit_conversion import DistanceConverter
from homeassistant.util import slugify
import numpy as np
from scipy.optimize import least_squares
import voluptuous as vol
import logging
import asyncio
import os
import json
import re
import copy
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points, unary_union
try:
    from shapely.validation import make_valid as shapely_make_valid
except ImportError:  # very old shapely
    shapely_make_valid = None
from asyncio import Lock, Queue, wait_for, TimeoutError

from .calibration import (
    BPS_FILE_LOCK,
    BPSCalibrationAPI,
    async_restore_calibration_state,
    async_shutdown_calibration,
    async_start_auto_if_enabled,
)
from .zone_adjust import adjust_zones

_LOGGER = logging.getLogger(__name__)

DOMAIN = "bps"
OPTION_SHOW_SIDEBAR_PANEL = "show_sidebar_panel"
FRONTEND_PATH = Path(__file__).parent / "frontend"
LEGACY_BPS_ENTITY_PATTERN = re.compile(r"^sensor\.(.+)_\1_bps_(zone|floor)$")

# Global data
global_data = []
state_change_lock = Lock()
state_change_counter = {}
update_queue = Queue()
tracked_listeners = {}
tracked_entities = []
new_global_data = {}
secToUpdate = 1
# A scanner Bermuda hasn't heard for this long is treated as offline; the
# liveness is polled from dump_devices every RECEIVER_DUMP_INTERVAL seconds.
RECEIVER_OFFLINE_SECS = 30
RECEIVER_DUMP_INTERVAL = 15
apitricords = []
# A tracker not detected by any receiver for this long disappears from the
# map and its zone/floor sensors go to unknown. Override with a top-level
# "position_timeout" (seconds) in bpsdata.txt.
STALE_POSITION_SECS = 300


def cleanup_legacy_bps_registry_and_states(hass: HomeAssistant):
    """Remove legacy duplicated BPS ids from entity registry and state machine."""
    entity_registry = er.async_get(hass)
    legacy_registry_ids = [
        entry.entity_id
        for entry in entity_registry.entities.values()
        if LEGACY_BPS_ENTITY_PATTERN.match(entry.entity_id)
    ]
    for entity_id in legacy_registry_ids:
        _LOGGER.info("Removing legacy BPS registry entity: %s", entity_id)
        entity_registry.async_remove(entity_id)

    legacy_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if LEGACY_BPS_ENTITY_PATTERN.match(state.entity_id)
    ]
    for entity_id in legacy_state_ids:
        _LOGGER.info("Removing legacy BPS state: %s", entity_id)
        hass.states.async_remove(entity_id)

class FileWatcher(FileSystemEventHandler):
    """A class to handle file changes"""
    def __init__(self, file_path, callback, hass: HomeAssistant):
        self.file_path = file_path
        self.callback = callback
        self.hass = hass  # Reference to the Home Assistant instance

    def on_modified(self, event):
        """Called when the file changes"""
        if event.src_path == self.file_path:
            asyncio.run_coroutine_threadsafe(self.callback(), self.hass.loop)

async def read_file(file_path):
    """Read data asynchronously from the file"""
    try:
        async with aiofiles.open(file_path, mode="r") as file:
            content = await file.read()
        return content
    except FileNotFoundError:
        _LOGGER.warning("File not found: %s", file_path)
        return ""
    except Exception as e:
        _LOGGER.error("Error reading file %s: %s", file_path, e)
        return ""

def setup_file_watcher(file_path, update_callback, hass: HomeAssistant):
    """Set up a file watcher to monitor changes"""
    event_handler = FileWatcher(file_path, update_callback, hass)
    observer = Observer()
    observer.schedule(event_handler, os.path.dirname(file_path), recursive=False)
    observer.start()
    return observer

async def update_global_data(file_path):
    """Update global_data with the contents of the file"""
    global global_data
    new_data = await read_file(file_path) 
    try:
        global_data = json.loads(new_data) if new_data else []

        _LOGGER.info("Updated global_data: %s", global_data)
    except json.JSONDecodeError as e:
        _LOGGER.error("Error parsing JSON data: %s", e)

async def update_tracked_entities(hass, jinja_code):
    """Update tracked_entities with the result of the Jinja code once per second."""
    global tracked_entities, tracked_listeners, global_data, new_global_data
    global secToUpdate
    while True:
        try:
            template = Template(jinja_code, hass)
            tracked_entities = template.async_render()

            await prune_stale_positions(hass)

            num_points = len(tracked_entities)
            if num_points == 0:
                _LOGGER.info("There are no devices present to track, sleep 10 seconds")
                await asyncio.sleep(10)
                continue  # Skip and start over
            if num_points < 3:
                _LOGGER.info("There are not enough trackers with available data to track, sleep 10 seconds")
                await asyncio.sleep(10)
                continue  # Skip and start over
            
            cleaned = [item.split("_distance_to_")[0].replace("sensor.", "") for item in tracked_entities]
            unique_values = list(set(cleaned))
            # Use a separate copy per entity to avoid cross-entity mutation side effects.
            new_global_data = [{"entity": ent, "data": copy.deepcopy(global_data)} for ent in unique_values]
            
            await process_entities(hass, new_global_data)

        except Exception as e:
            _LOGGER.info(f"Error executing Jinja code: {e}")

        # Refresh receiver liveness on its own slower cadence.
        now_ts = time.time()
        if now_ts - getattr(update_tracked_entities, "last_liveness", 0.0) >= RECEIVER_DUMP_INTERVAL:
            update_tracked_entities.last_liveness = now_ts
            await update_receiver_liveness(hass)

        await asyncio.sleep(secToUpdate)  # Run every X seconds, set timer in global variables


# State strings that mean "not working" when a status/availability entity is read.
_OFFLINE_STATES = {
    "", "unavailable", "unknown", "none", "off", "false",
    "not_home", "offline", "disconnected", "no",
}


def _state_looks_online(state):
    """Whether a status/availability entity's state reads as online."""
    if state is None:
        return False
    return str(state).strip().lower() not in _OFFLINE_STATES


def _scanner_slugs_and_readings(hass):
    """Single pass over distance sensors: every scanner slug Bermuda exposes,
    and the subset that currently has a live reading (for the heuristic tier)."""
    slugs = set()
    with_reading = set()
    for st in hass.states.async_all("sensor"):
        eid = st.entity_id
        if "_distance_to_" not in eid:
            continue
        slug = eid.split("_distance_to_", 1)[1]
        slugs.add(slug)
        if st.state not in (None, "", "unknown", "unavailable"):
            with_reading.add(slug)
    return slugs, with_reading


def _refresh_dump_ages(hass, dom, devices):
    """Update the cached per-scanner Bermuda-liveness ages from a dump payload.

    last_seen is monotonic (seconds since HA boot), not epoch; the freshest
    stamp in the payload is "now" on that clock. Re-anchor only when the payload
    aged forward — if newest didn't advance (every scanner stopped hearing
    adverts, a real fleet-wide outage) keep the previous ages so they grow with
    wall time and cross the timeout; a large backward jump is a monotonic clock
    reset (HA reboot), so accept it.
    """
    newest = 0.0
    for dev in devices.values():
        ls = dev.get("last_seen") if isinstance(dev, dict) else None
        if isinstance(ls, (int, float)) and ls > newest:
            newest = ls
    prev_newest = dom.get("rl_newest")
    now = time.time()
    if not (prev_newest is not None and prev_newest - 60 < newest <= prev_newest):
        ages = {}
        for dev in devices.values():
            if not isinstance(dev, dict) or dev.get("_is_scanner") is not True:
                continue
            slug = slugify(str(dev.get("name") or ""))
            if not slug:
                continue
            ls = dev.get("last_seen")
            ages[slug] = (newest - ls) if isinstance(ls, (int, float)) else float("inf")
        dom["rl_newest"] = newest
        dom["rl_anchor_wall"] = now
        dom["rl_ages"] = ages


def _build_device_availability(hass):
    """Maps for the device-availability tier: slug -> device, device -> entities.

    A device slug that isn't unique is dropped (ambiguous) so a stale/duplicate
    device can't decide a receiver's status.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    slug_to_device = {}
    ambiguous = set()
    for d in dev_reg.devices.values():
        name = d.name_by_user or d.name
        if not name:
            continue
        slug = slugify(name)
        if not slug:
            continue
        if slug in slug_to_device and slug_to_device[slug] != d.id:
            ambiguous.add(slug)
        else:
            slug_to_device[slug] = d.id
    for slug in ambiguous:
        slug_to_device.pop(slug, None)
    device_entities = {}
    for ent in ent_reg.entities.values():
        if ent.device_id:
            device_entities.setdefault(ent.device_id, []).append(ent.entity_id)
    return slug_to_device, device_entities


async def update_receiver_liveness(hass):
    """Recompute the set of offline receivers, mirroring the Lovelace card's
    tiered status so the panel and the card agree.

    For each receiver slug Bermuda exposes, the first tier that resolves wins:
      1. Bermuda scanner liveness — last advert heard within RECEIVER_OFFLINE_SECS.
         Proximity-independent: the probes advertise iBeacons the scanners hear
         from each other, so a live scanner ages fresh even with no tracked
         device home. But Bermuda drops a downed proxy from the scanner list, so
         this tier can't see it at all — hence the fallbacks below.
      2. A `binary_sensor.<slug>_status` connectivity sensor.
      3. The receiver's HA device: online while any of its entities is not
         `unavailable`; a connectivity entity on the device is authoritative.
      4. Distance heuristic (last resort): working if some tracker got a reading
         through it recently. Only reached for a scanner with no liveness and no
         mapped device (6 of this install's receivers, whose device name doesn't
         slugify to the receiver id). An *up* scanner is caught by tier 1, so
         this can't misfire when every tracked device leaves home — it only
         decides a scanner that is both absent from the dump and deviceless.
    A receiver no tier can resolve at all is left ONLINE (never flagged).
    """
    dom = hass.data.setdefault(DOMAIN, {})
    try:
        devices = await hass.services.async_call(
            "bermuda", "dump_devices", {"configured_devices": True},
            blocking=True, return_response=True,
        ) or {}
    except Exception as e:
        _LOGGER.info(f"Receiver liveness: dump_devices unavailable: {e}")
        devices = None

    if isinstance(devices, dict):
        _refresh_dump_ages(hass, dom, devices)
    ages = dom.get("rl_ages", {})
    elapsed = max(0.0, time.time() - dom["rl_anchor_wall"]) if dom.get("rl_anchor_wall") else 0.0

    receivers, with_reading = _scanner_slugs_and_readings(hass)
    slug_to_device, device_entities = _build_device_availability(hass)

    def device_online(slug):
        device_id = slug_to_device.get(slug)
        if not device_id:
            return None
        eids = device_entities.get(device_id)
        if not eids:
            return None
        # A connectivity entity reports link state directly and is authoritative:
        # ESPHome keeps its status sensor available with state "off" when the
        # proxy disconnects, so "any entity not unavailable" would miss it.
        for eid in eids:
            st = hass.states.get(eid)
            if st and st.attributes.get("device_class") == "connectivity" and st.state is not None:
                return _state_looks_online(st.state)
        saw_state = False
        for eid in eids:
            st = hass.states.get(eid)
            if st is None or st.state is None:
                continue
            saw_state = True
            if st.state != "unavailable":
                return True
        return False if saw_state else None

    def is_online(slug):
        age = ages.get(slug)
        if age is not None:
            return (age + elapsed) <= RECEIVER_OFFLINE_SECS
        st = hass.states.get(f"binary_sensor.{slug}_status")
        if st and st.attributes.get("device_class") == "connectivity":
            return _state_looks_online(st.state)
        resolved = device_online(slug)
        if resolved is not None:
            return resolved
        # tier 4: distance heuristic (last resort). Matches the card: a receiver
        # here is online only while some tracker reads a distance through it.
        return slug in with_reading

    dom["rl_offline"] = sorted(s for s in receivers if not is_online(s))


async def update_receiver_radii(hass, eids):
    """Update receiver 'r' values (pixels) and raw 'distance' (meters) for an entity"""
    for floor in (f for f in eids["data"]["floor"] if f["scale"] is not None):
        for receiver in floor["receivers"]:
            entity_id = "sensor." + eids["entity"] + "_distance_to_" + receiver["entity_id"]
            rec_value = hass.states.get(entity_id)
            if rec_value is not None:
                try:
                    distance = float(rec_value.state)
                    # Bermuda's distance_to sensors can report in feet or
                    # meters, chosen per entity. The floor scale and the
                    # calibration corrections are both in meters, so normalize
                    # to meters first — otherwise a feet sensor reads ~3.28x too
                    # far (treated as metres), inflating its circle and pulling
                    # the trilateration toward it.
                    unit = rec_value.attributes.get("unit_of_measurement")
                    if unit in DistanceConverter.VALID_UNITS and unit != UnitOfLength.METERS:
                        distance = DistanceConverter.convert(distance, unit, UnitOfLength.METERS)
                    # Per-receiver correction factor learned by the
                    # calibration (calibration.py); equivalent to a
                    # per-scanner RSSI offset in Bermuda's exponential model.
                    correction = receiver.get("correction")
                    if isinstance(correction, (int, float)) and correction > 0:
                        distance = distance * correction
                    receiver["cords"]["r"] = floor["scale"] * distance
                    # Raw distance for the floor election: radii are in
                    # per-floor pixel scales and must not be compared across
                    # floors.
                    receiver["distance"] = distance
                except ValueError:
                    #_LOGGER.info(f"Invalid numerical value: {rec_value.state}")
                    pass
            else:
                #_LOGGER.info(f"Entity had no value: {receiver['entity_id']}")
                pass

async def update_trilateration_and_zone(hass, new_global_data, entity):
    """Trilateration with r-value filtering and moving average filtering."""
    global apitricords
    filter_percent = 0.5  # 50% change in r-value
    filter_value_high = 1 * (1 + filter_percent)
    filter_value_low = 1 * (1 - filter_percent)

    # Store last r-values per sensor and entity
    if not hasattr(update_trilateration_and_zone, "last_r_values"):
        update_trilateration_and_zone.last_r_values = {}
    # Store last positions for moving average filtering
    if not hasattr(update_trilateration_and_zone, "position_history"):
        update_trilateration_and_zone.position_history = {}

    lowest_floor_name, filtered_cords = extract_floor_and_receivers(new_global_data, entity)
    # filtered_cords: list of (x, y, r)

    if lowest_floor_name is None:
        # No receiver reports any distance for this device: it is out of
        # range. The zone/floor sensors keep their last value (historical
        # behavior), but nearest-zone explicitly reports unknown.
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_nearest_zone", "unknown")
        return

    if not hasattr(update_trilateration_and_zone, "last_floor"):
        update_trilateration_and_zone.last_floor = {}
    if update_trilateration_and_zone.last_floor.get(entity) != lowest_floor_name:
        # Fixes in the history are pixel coordinates in the previously elected
        # floor's map space; they must not be averaged with fixes from the
        # newly elected floor.
        update_trilateration_and_zone.position_history.pop(entity, None)
        update_trilateration_and_zone.last_floor[entity] = lowest_floor_name

    # Get previous r-values for this entity
    last_r = update_trilateration_and_zone.last_r_values.get(entity, {})

    # Filter out points where r has changed too much. The key includes the
    # floor: radii are in per-floor pixel scales, so the same pixel coords on
    # two floors must not be compared against each other.
    filtered = []
    for idx, (x, y, r) in enumerate(filtered_cords):
        key = (lowest_floor_name, x, y)
        prev_r = last_r.get(key)
        if prev_r is not None:
            if r > prev_r * filter_value_high or r < prev_r * filter_value_low:  # e.g. max 100% change
                continue  # skip this point
        filtered.append((x, y, r))

    # Store current r-values for next time
    update_trilateration_and_zone.last_r_values[entity] = {(lowest_floor_name, x, y): r for (x, y, r) in filtered_cords}

    if len(filtered) < 3:
        # Too few points left for trilateration
        return

    # The device cannot be outside the floor: bound the solver to the extent
    # of the floor's receivers and zones (with some margin) so the fitted
    # position is the best point WITHIN the map, not a runaway fix that would
    # need clamping afterwards.
    zone_polys = list(_floor_zone_polygons(new_global_data, entity, lowest_floor_name))
    xs = [p[0] for p in filtered]
    ys = [p[1] for p in filtered]
    for _zone_id, polygon, _buffer_size in zone_polys:
        minx, miny, maxx, maxy = polygon.bounds
        xs.extend((minx, maxx))
        ys.extend((miny, maxy))
    margin = 0.1 * max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    floor_bounds = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)

    tricords = trilaterate(filtered, bounds=floor_bounds)
    if tricords is not None:
        # Moving average filtering
        history = update_trilateration_and_zone.position_history.setdefault(entity, [])
        history.append(tricords)
        if len(history) > 3:  # Keep only the last 3 positions
            history.pop(0)
        avg_x = sum(pos[0] for pos in history) / len(history)
        avg_y = sum(pos[1] for pos in history) / len(history)

        # A fix outside every zone is physically implausible (BLE noise pushed
        # it into a wall or off the apartment): publish the nearest point on
        # the zone union instead. The raw fixes stay in the moving-average
        # history so the smoothing is not biased toward the boundary.
        test_point = Point(float(avg_x), float(avg_y))
        snapped = snap_point_into_zones(zone_polys, test_point)
        if snapped is not None:
            test_point = snapped
            avg_x, avg_y = float(snapped.x), float(snapped.y)
        zone = find_zone_for_point(new_global_data, entity, lowest_floor_name, test_point)
        nearest_zone = find_nearest_zone(new_global_data, entity, lowest_floor_name, test_point)
        sub_zone, sub_parent = find_sub_zone_for_point(new_global_data, entity, lowest_floor_name, test_point)
        # parent_zone always names the enclosing main zone: the sub-zone's
        # declared parent when inside one, otherwise the current main zone.
        parent_zone = sub_parent if sub_parent else zone
        apitricords = update_or_add_entry(
            apitricords,
            {
                "ent": entity,
                "cords": [avg_x, avg_y],
                "zone": zone,
                "floor": lowest_floor_name,
                # The exact solver input (post-correction, post-filter), for
                # the panel's trilateration circles.
                "radii": [[float(px), float(py), float(pr)] for (px, py, pr) in filtered],
                "updated": time.time(),
            },
        )
        await update_apitricords(hass, apitricords)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_zone", zone)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_nearest_zone", nearest_zone)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_floor", lowest_floor_name)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_sub_zone", sub_zone, {"parent_zone": parent_zone})

def update_or_add_entry(data, new_entry):
    for item in data:
        if item["ent"] == new_entry["ent"]:  # Check if "ent" already exists
            item["cords"] = new_entry["cords"]  # Update "cords"
            item["zone"] = new_entry["zone"]  # Update "zone"
            item["floor"] = new_entry["floor"]  # Floor the fix belongs to
            item["radii"] = new_entry["radii"]  # Solver input, for circles
            item["updated"] = new_entry["updated"]  # Freshness for pruning
            return data

    # If "ent" was not found, add as new post
    data.append(new_entry)
    return data


async def prune_stale_positions(hass):
    """Drop trackers not detected by any receiver for the timeout period.

    Without this, a person who left home stayed on the map at their last
    position forever, and the zone/floor sensors kept the stale values.
    """
    global apitricords
    timeout = STALE_POSITION_SECS
    if isinstance(global_data, dict):
        configured = global_data.get("position_timeout")
        if isinstance(configured, (int, float)) and configured > 0:
            timeout = configured

    now = time.time()
    stale_ents = {e["ent"] for e in apitricords if now - e.get("updated", now) > timeout}
    if not stale_ents:
        return
    apitricords = [e for e in apitricords if e["ent"] not in stale_ents]
    await update_apitricords(hass, apitricords)
    for ent in sorted(stale_ents):
        _LOGGER.info("Tracker %s not seen for %ss; clearing its position", ent, timeout)
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_zone", "unknown")
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_floor", "unknown")
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_nearest_zone", "unknown")
        update_bps_sensor_state(hass, f"sensor.{ent}_bps_sub_zone", "unknown", {"parent_zone": "unknown"})

async def update_apitricords(hass, new_data):
    """Update apitricords in hass.data"""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["apitricords"] = new_data


def update_bps_sensor_state(hass, entity_id, state, attributes=None):
    """Update state (and optional extra attributes) on a registered BPS SensorEntity."""
    sensors_cache = hass.data.get("bps_sensors")
    if not sensors_cache:
        return
    sensor = sensors_cache.get(entity_id)
    if sensor is None:
        return
    sensor._state = state
    if attributes is not None:
        sensor._attrs = attributes
    sensor.async_write_ha_state()

async def process_single_entity(hass, new_global_data, eids):
    """Process a single entity: first receivers, then trilateration"""
    await update_receiver_radii(hass, eids)  # Wait for the receivers to update
    await update_trilateration_and_zone(hass, new_global_data, eids["entity"])  # When it is complete → perform trilateration

async def process_entities(hass, new_global_data):
    """Process multiple entities in parallel, but ensure the correct order for each individual entity"""
    tasks = [process_single_entity(hass, new_global_data, eids) for eids in new_global_data]
    await asyncio.gather(*tasks)  # Run all entities in parallel, but maintain the correct internal order

def extract_floor_and_receivers(new_global_data, tmpentity):
    """Pick the floor of the physically closest receiver, then collect that floor's cords.

    The election compares raw distances (meters), not radii: radii are scaled
    into each floor's own pixel space, so comparing them across floors would
    let the floor with the smallest scale win regardless of where the tracker
    actually is. Ties (the same receiver placed on several floors) go to the
    floor listed first in the data file.
    """
    lowest_floor, lowest_distance = None, float("inf")

    for entity in new_global_data:
        if entity["entity"] == tmpentity:
            for floor in entity["data"]["floor"]:
                for receiver in floor["receivers"]:
                    distance = receiver.get("distance")
                    if distance is None or "r" not in receiver.get("cords", {}):
                        continue
                    if distance < lowest_distance:
                        lowest_distance, lowest_floor = distance, floor

    if lowest_floor is None:
        return None, []

    # Only the winning floor's receivers feed the trilateration: cords from
    # different floors live in different pixel coordinate systems.
    filtered_cords = [
        (receiver["cords"]["x"], receiver["cords"]["y"], receiver["cords"]["r"])
        for receiver in lowest_floor["receivers"]
        if "r" in receiver.get("cords", {})
    ]
    return lowest_floor["name"], filtered_cords

def _floor_zone_polygons(data, entity, floor_name):
    """Yield (zone entity_id, polygon, buffer_size) for the entity's floor."""
    buffer_percent = 0.05  # set to 5%

    def order_zone_points(coords):
        """Order polygon points clockwise around centroid to avoid self-intersections.

        Only for legacy rectangle zones, whose four corners were stored in
        scan order. Polygon zones (poly: true) keep their drawn order — a
        centroid sort would corrupt concave shapes.
        """
        if len(coords) < 3:
            return coords
        center_x = sum(coord["x"] for coord in coords) / len(coords)
        center_y = sum(coord["y"] for coord in coords) / len(coords)
        return sorted(
            coords,
            key=lambda coord: np.arctan2(coord["y"] - center_y, coord["x"] - center_x)
        )

    for entity_data in data:
        if entity_data["entity"] == entity:
            for floor in entity_data["data"]["floor"]:
                if floor["name"] == floor_name:
                    for zone in floor["zones"]:
                        coords = zone.get("cords") or []
                        if len(coords) < 3:
                            continue
                        if not zone.get("poly") and len(coords) == 4:
                            coords = order_zone_points(coords)
                        polygon = Polygon([(coord["x"], coord["y"]) for coord in coords])
                        if not polygon.is_valid:
                            # Self-intersecting drawing; make_valid keeps every
                            # lobe (buffer(0) can drop one). Keep only the
                            # areal parts of the repair.
                            repaired = (
                                shapely_make_valid(polygon)
                                if shapely_make_valid
                                else polygon.buffer(0)
                            )
                            if repaired.geom_type == "GeometryCollection":
                                parts = [
                                    g for g in repaired.geoms
                                    if g.geom_type in ("Polygon", "MultiPolygon")
                                ]
                                repaired = unary_union(parts) if parts else None
                            if repaired is None or repaired.is_empty:
                                continue
                            polygon = repaired
                        xs = [coord["x"] for coord in coords]
                        ys = [coord["y"] for coord in coords]
                        width = max(xs) - min(xs)
                        height = max(ys) - min(ys)
                        buffer_size = ((width + height) / 2) * buffer_percent
                        yield zone["entity_id"], polygon, buffer_size


def find_zone_for_point(data, entity, floor_name, point):
    """Find zone for point, prioritize correct polygon, select nearest buffer if no correct zone matches."""
    buffer_candidates = []
    for zone_id, polygon, buffer_size in _floor_zone_polygons(data, entity, floor_name):
        # covers() also matches points on the polygon boundary.
        if polygon.covers(point):
            return zone_id  # Prioritize correct polygon
        if polygon.buffer(buffer_size).contains(point):
            # Save candidate: (distance to edge, entity_id)
            # boundary works for Polygon and MultiPolygon alike.
            buffer_candidates.append((polygon.boundary.distance(point), zone_id))
    if buffer_candidates:
        # Select zone whose edge is closest to the point
        buffer_candidates.sort()
        return buffer_candidates[0][1]
    return "unknown"


def snap_point_into_zones(zone_polys, point):
    """Project a point outside every zone onto the nearest zone boundary.

    Returns the snapped Point, or None when the point is already inside a
    zone (or the floor has no zones) and should be used as-is.
    """
    polygons = [polygon for _zone_id, polygon, _buffer_size in zone_polys]
    if not polygons:
        return None
    union = unary_union(polygons)
    if union.is_empty or union.covers(point):
        return None
    snapped, _ = nearest_points(union, point)
    return snapped


def find_nearest_zone(data, entity, floor_name, point):
    """The zone closest to the point, no matter how far away.

    Trilateration jitter can land a fix between two zones or outside the map
    entirely; this always names the closest zone on the elected floor (a point
    inside a zone has distance 0, so it matches find_zone_for_point there).
    Returns "unknown" only when the floor has no usable zones.
    """
    nearest_id = "unknown"
    nearest_distance = None
    for zone_id, polygon, _buffer_size in _floor_zone_polygons(data, entity, floor_name):
        distance = polygon.distance(point)
        if nearest_distance is None or distance < nearest_distance:
            nearest_id, nearest_distance = zone_id, distance
    return nearest_id


def _floor_sub_zone_polygons(data, entity, floor_name):
    """Yield (sub-zone name, parent zone name, polygon) for the entity's floor.

    Sub-zones are small precise areas drawn inside a zone (a couch, a desk), so
    they are matched strictly (no soft buffer). They live in a separate
    "subzones" list, so the main-zone election/snap/nearest logic is untouched.
    """
    for entity_data in data:
        if entity_data["entity"] != entity:
            continue
        for floor in entity_data["data"]["floor"]:
            if floor["name"] != floor_name:
                continue
            # Sub-zones link to their parent by the zone's stable id; resolve it
            # to the zone's display name for the parent_zone attribute.
            zone_name_by_id = {}
            for z in floor.get("zones") or []:
                zone_name_by_id[z.get("zone_id") or z.get("entity_id")] = z.get("entity_id")
            for sub in floor.get("subzones") or []:
                coords = sub.get("cords") or []
                if len(coords) < 3:
                    continue
                polygon = Polygon([(c["x"], c["y"]) for c in coords])
                if not polygon.is_valid:
                    repaired = (
                        shapely_make_valid(polygon)
                        if shapely_make_valid
                        else polygon.buffer(0)
                    )
                    if repaired is None or repaired.is_empty:
                        continue
                    polygon = repaired
                parent_ref = sub.get("parent")
                yield sub.get("entity_id"), zone_name_by_id.get(parent_ref, parent_ref), polygon


def find_sub_zone_for_point(data, entity, floor_name, point):
    """The sub-zone containing the point and its parent zone name.

    Returns (sub_zone_name, parent_zone_name); ("unknown", None) when the point
    is in no sub-zone.
    """
    for sub_id, parent_id, polygon in _floor_sub_zone_polygons(data, entity, floor_name):
        if polygon.covers(point):
            return sub_id, parent_id
    return "unknown", None


async def async_setup(hass, config):
    """Set up the BPS integration."""
    _LOGGER.info("BPS integration initierad.")

    if hass.data.get("bps_initialized", False):
        _LOGGER.debug("BPS already initialized in current runtime; skipping duplicate init.")
        return True  # Abort if already running

    hass.data["bps_initialized"] = True  # Set flag

    async def initialize_bps():
        """Initialize the BPS component"""
        _LOGGER.info("Initializing BPS...")

        if "bps_views_registered" not in hass.data:
            hass.http.register_view(BPSFrontendView())
            hass.http.register_view(BPSSaveAPIText())
            hass.http.register_view(BPSMapsListAPI())
            hass.http.register_view(BPSTrackerIconsListAPI())
            hass.http.register_view(BPSUploadTrackerIconAPI())
            hass.http.register_view(BPSReadAPIText())
            hass.http.register_view(BPSAdjustZonesAPI())
            hass.http.register_view(BPSReceiverStatusAPI())
            hass.http.register_view(BPSCordsAPI(hass))
            hass.http.register_view(BPSCalibrationAPI())
            hass.data["bps_views_registered"] = True

        if "bps_websocket" not in hass.data:
            websocket = BPSEntityWebSocket(hass)
            websocket.register()
            hass.data["bps_websocket"] = websocket

        config_path = hass.config.path()
        target_dir = os.path.join(config_path, "www", "bps_maps")
        tracker_icons_dir = os.path.join(config_path, "www", "bps_icons")
        target_file = os.path.join(target_dir, "bpsdata.txt")

        try:
            await aiofiles.os.makedirs(target_dir, exist_ok=True)
            _LOGGER.info(f"Folder {target_dir} has been created or already existed")
        except Exception as e:
            _LOGGER.error(f"Could not create the folder {target_dir}: {e}")
            return

        try:
            await aiofiles.os.makedirs(tracker_icons_dir, exist_ok=True)
            _LOGGER.info(f"Folder {tracker_icons_dir} has been created or already existed")
        except Exception as e:
            _LOGGER.error(f"Could not create the folder {tracker_icons_dir}: {e}")
            return

        show_sidebar_panel = True
        if hasattr(config, "options"):
            show_sidebar_panel = config.options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
        else:
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                show_sidebar_panel = entries[0].options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
        panels = hass.data.get("frontend_panels", {})
        if "bps" in panels:
            async_remove_panel(hass, "bps")

        if show_sidebar_panel:
            try:
                _LOGGER.debug("Registering the built-in panel for BPS...")
                async_register_built_in_panel(
                    hass=hass,
                    component_name="iframe",
                    sidebar_title="BPS",
                    sidebar_icon="mdi:map",
                    frontend_url_path="bps",
                    config={"url": "/bps/index.html"},
                )
                _LOGGER.info("Panel registered successfully.")
            except Exception as e:
                _LOGGER.error(f"Failed to register panel: {e}")
        else:
            _LOGGER.info("BPS sidebar panel is disabled by integration options.")

        try:
            if not os.path.exists(target_file):
                async with aiofiles.open(target_file, mode="w") as file:
                    await file.write("")  # Skapa en tom fil
                _LOGGER.info(f"File {target_file} has been created.")
            else:
                _LOGGER.info(f"File {target_file} already exist.")
        except Exception as e:
            _LOGGER.error(f"Could not create file {target_file}: {e}")

        await update_global_data(target_file)

        # Stop any previous watcher before creating a new one on reload.
        old_observer = hass.data.get("bps_observer")
        if old_observer:
            old_observer.stop()
            old_observer.join(timeout=2)

        observer = setup_file_watcher(target_file, lambda: update_global_data(target_file), hass)
        hass.data["bps_observer"] = observer

        jinja_code = """
        {{
            states.sensor
            | selectattr("entity_id", "search", "_distance_to_")
            | map(attribute="entity_id")
            | unique
            | list
        }}
        """

        old_task = hass.data.get("bps_update_task")
        if old_task:
            old_task.cancel()
        hass.data["bps_update_task"] = hass.async_create_task(update_tracked_entities(hass, jinja_code))

        async def handle_homeassistant_stop(event):
            """Stop background work promptly so shutdown cannot drag or leave
            the unload half-done (which strands stale registry entries)."""
            observer.stop()
            update_task = hass.data.pop("bps_update_task", None)
            if update_task:
                update_task.cancel()
            await async_shutdown_calibration(hass)

        hass.bus.async_listen_once("homeassistant_stop", handle_homeassistant_stop)

        await async_restore_calibration_state(hass)
        await async_start_auto_if_enabled(hass)

        _LOGGER.info("The BPS integration is fully initialized")

    async def handle_homeassistant_started(event):
        """Handles the 'homeassistant_started' event"""
        await initialize_bps()

    if hass.is_running:
        await initialize_bps()
    else:
        hass.bus.async_listen_once("homeassistant_started", handle_homeassistant_started)

    return True

async def async_unload_entry(hass: HomeAssistant, entry):
    """Remove a configuration entry"""
    _LOGGER.info("Attempting to offload platforms for entry: %s", entry.entry_id)

    state_listener_unsub = hass.data.pop("bps_state_listener_unsub", None)
    if state_listener_unsub:
        state_listener_unsub()

    cleanup_legacy_bps_registry_and_states(hass)

    entity_registry = er.async_get(hass)

    # Find and remove all entities that belong to "bps"
    entities_to_remove = [
        entity.entity_id for entity in entity_registry.entities.values()
        if entity.platform == "bps"
    ]

    for entity_id in entities_to_remove:
        _LOGGER.info(f"Removes sensor: {entity_id}")
        entity_registry.async_remove(entity_id)

    try: # Attempt to unload platforms
        unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    except Exception as e:
        _LOGGER.error(f"Error during offloading of platforms for entry {entry.entry_id}: {e}")
        return False

    if not unload_ok:
        _LOGGER.error("Failed to offload platforms for entry: %s", entry.entry_id)
        return False

    try: #Remove the frontend panel
        async_remove_panel(hass, frontend_url_path="bps")
        _LOGGER.info("Frontend-panel removed for entry: %s", entry.entry_id)
    except Exception as e:
        _LOGGER.error(f"Error when removing frontend-panel for entry {entry.entry_id}: {e}")
        return False

    observer = hass.data.pop("bps_observer", None)
    if observer:
        observer.stop()
        observer.join(timeout=2)

    update_task = hass.data.pop("bps_update_task", None)
    if update_task:
        update_task.cancel()

    await async_shutdown_calibration(hass)

    # Remove BPS states from the state machine when integration is unloaded.
    bps_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.")
        and state.entity_id.endswith(("_bps_zone", "_bps_floor", "_bps_nearest_zone", "_bps_sub_zone"))
    ]
    for entity_id in bps_state_ids:
        hass.states.async_remove(entity_id)

    # Allow clean setup after integration reload/removal.
    hass.data.pop("bps_initialized", None)
    hass.data.pop("bps_sensors", None)

    return True


async def async_setup_entry(hass, entry):
    """Set the integration from a configuration entry"""
    _LOGGER.info("async_setup_entry called")
    cleanup_legacy_bps_registry_and_states(hass)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    """Set up BPS from a config entry."""
    return await async_setup(hass, entry)


async def async_update_options(hass, entry):
    """Reload integration when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)

class BPSFrontendView(HomeAssistantView):
    """Serve the frontend files."""

    url = "/bps/{file_name}"
    name = "bps:frontend"
    requires_auth = False

    async def get(self, request, file_name):
        """Serve static files from the frontend folder."""
        frontend_path = FRONTEND_PATH / file_name

        _LOGGER.info(f"Serving file: {frontend_path}")

        if not frontend_path.is_file():
            _LOGGER.error(f"Requested file not found: {frontend_path}")
            return web.Response(status=404, text="File not found")

        response = web.FileResponse(path=str(frontend_path))
        # The panel's script.js/CSS change on every integration update. Without
        # an explicit directive, browsers cache these heuristically and keep
        # serving the old panel after an update. "no-cache" keeps the cached
        # copy but forces revalidation (a cheap 304 when unchanged, the new file
        # when it changed), so updates show up on a normal reload.
        response.headers["Cache-Control"] = "no-cache"
        return response

class BPSSaveAPIText(HomeAssistantView):
    """Handle saving of BPS coordinates to a text file."""

    url = "/api/bps/save_text"
    name = "api:bps:save_text"
    requires_auth = False

    async def post(self, request):
        """Handle saving coordinates to a text file."""
        hass = request.app["hass"]
        data = await request.post()

        coordinates = data.get("coordinates")
        
        if not coordinates:
            return web.Response(status=400, text="Missing coordinates")
        
        # Define the path to the bpsdata file
        maps_path = hass.config.path("www/bps_maps")
        bpsdata_file_path = Path(maps_path) / "bpsdata.txt"
        
        try: # Save coordinates to the bpsdata file
            # Serialized with the calibration writers so read-modify-write
            # cycles on bpsdata.txt cannot interleave.
            async with BPS_FILE_LOCK:
                error = await self._write_save(bpsdata_file_path, maps_path, data, coordinates)
            if error is not None:
                return error
            _LOGGER.info(f"Saved coordinates to bpsdata: {coordinates}")
            return web.Response(status=200, text="Coordinates saved successfully")

        except Exception as e:
            _LOGGER.error(f"Failed to save coordinates: {e}")
            return web.Response(status=500, text="Failed to save coordinates")

    async def _write_save(self, bpsdata_file_path, maps_path, data, coordinates):
        """Perform the writes; returns an error Response or None on success."""
        async with aiofiles.open(bpsdata_file_path, "w") as f:
            await f.write(coordinates)
        _LOGGER.warning(f"New file: {data.get("new_floor")}")
        if data.get("new_floor") == "true": # If it is a new floor then save the file
            map_file = data.get("file")
            if not map_file:
                return web.Response(status=400, text="Missing file")
            map_file_path = Path(maps_path) / map_file.filename
            try:
                async with aiofiles.open(map_file_path, "wb") as f:
                    await f.write(map_file.file.read())
            except Exception as e:
                _LOGGER.error(f"Failed to save maps: {e}")
                return web.Response(status=500, text="Failed to save maps")

        # Check if "remove" key exists and delete the specified file
        remove_file = data.get("remove")
        if remove_file:
            remove_file_path = Path(maps_path) / remove_file
            if remove_file_path.exists():
                _LOGGER.warning(f"File exist: {remove_file_path}")
                try:
                    remove_file_path.unlink()  # Delete the file
                    _LOGGER.info(f"Removed file: {remove_file_path}")
                except Exception as e:
                    _LOGGER.error(f"Failed to remove file {remove_file_path}: {e}")
                    return web.Response(status=500, text="Failed to remove file")
        return None


class BPSAdjustZonesAPI(HomeAssistantView):
    """Propose a cleaned-up set of zones (square boxy rooms, snap shared
    boundaries incl. T-junctions, remove overlaps, re-clamp sub-zones).

    Pure-geometry PREVIEW: computes and returns a proposal plus a per-zone
    change report; it does NOT save. The panel renders the proposal, lets the
    user accept/reject, then persists via the existing save_text endpoint.
    """

    url = "/api/bps/adjust_zones"
    name = "api:bps:adjust_zones"
    requires_auth = False

    async def post(self, request):
        hass = request.app["hass"]
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body")
        zones = body.get("zones") or []
        subzones = body.get("subzones") or []
        options = body.get("options") or {}
        if not isinstance(zones, list) or not zones:
            return web.Response(status=400, text="No zones to adjust")
        try:
            # shapely work is synchronous; keep it off the event loop.
            result = await hass.async_add_executor_job(
                adjust_zones, zones, subzones, options
            )
        except Exception as e:
            _LOGGER.error(f"adjust_zones failed: {e}")
            return web.Response(status=500, text="Zone adjustment failed")
        return web.json_response(result)


class BPSReadAPIText(HomeAssistantView):
    """Handle reading of BPS coordinates from a text file."""

    url = "/api/bps/read_text"
    name = "api:bps:read_text"
    requires_auth = False

    async def get(self, request):
        """Handle reading coordinates from the text file."""
        hass = request.app["hass"]
        maps_path = hass.config.path("www/bps_maps") # Define the path to the bpsdata file
        bpsdata_file_path = Path(maps_path) / "bpsdata.txt"
        entityjinja = """
        {{
            states.sensor
            | selectattr("entity_id", "search", "_distance_to_")
            | map(attribute="entity_id")
            | map("replace", "sensor.", "")
            | map("regex_replace", "_distance_to_.*", "")
            | unique
            | list
        }}
        """
        # The receiver (scanner) names are the part after "_distance_to_" in
        # the same Bermuda sensors the tracked entities come from.
        receiverjinja = """
        {{
            states.sensor
            | selectattr("entity_id", "search", "_distance_to_")
            | map(attribute="entity_id")
            | map("regex_replace", "^.*?_distance_to_", "")
            | unique
            | sort
            | list
        }}
        """

        entities = []
        receivers = []
        try:
            template = Template(entityjinja, hass) # Render Jinja code
            entities = template.async_render()
        except Exception as e:
            _LOGGER.info(f"Error during the execution of the Jinja code: {e}")

        try:
            template = Template(receiverjinja, hass)
            receivers = template.async_render()
        except Exception as e:
            _LOGGER.info(f"Error during the execution of the receiver Jinja code: {e}")

        # Offline scanners come from the Bermuda-liveness poller (proximity-
        # independent), refreshed on a slower cadence by the background loop.
        offline_receivers = list(hass.data.get(DOMAIN, {}).get("rl_offline", []))

        try:
            if not bpsdata_file_path.is_file(): # Check if the file exists
                return web.Response(status=404, text="bpsdata.txt not found")

            async with aiofiles.open(bpsdata_file_path, "r") as f: # Read the content of the file
                content = await f.read()

            _LOGGER.info(f"Read coordinates from bpsdata: {content}")
            return web.json_response({
                "coordinates": content,
                "entities": entities,
                "receivers": receivers,
                "offline_receivers": offline_receivers
            })
        
        except Exception as e:
            _LOGGER.error(f"Failed to read coordinates: {e}")
            return web.Response(status=500, text="Failed to read coordinates")

class BPSReceiverStatusAPI(HomeAssistantView):
    """Current offline receivers (Bermuda liveness), polled live by the panel."""
    url = "/api/bps/receiver_status"
    name = "api:bps:receiver_status"
    requires_auth = False

    async def get(self, request):
        hass = request.app["hass"]
        offline = hass.data.get(DOMAIN, {}).get("rl_offline", [])
        return web.json_response({"offline": list(offline)})


class BPSMapsListAPI(HomeAssistantView):
    """API to list map files in /www/bps_maps."""
    url = "/api/bps/maps"
    name = "api:bps:maps"
    requires_auth = False

    @staticmethod
    def _list_map_files(maps_path):
        """List map file names from disk (runs in executor)."""
        with os.scandir(maps_path) as entries:
            return [
                entry.name
                for entry in entries
                if entry.is_file() and entry.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
            ]

    async def get(self, request):
        """Return a list of map files as JSON."""
        hass = request.app["hass"]
        maps_path = hass.config.path("www/bps_maps")

        try:
            file_names = await hass.async_add_executor_job(self._list_map_files, maps_path)
            return web.json_response(file_names)
        except Exception as e:
            _LOGGER.error(f"Error listing map files: {e}")
            return web.Response(status=500, text="Error listing map files")


class BPSTrackerIconsListAPI(HomeAssistantView):
    """API to list tracker icon files."""

    url = "/api/bps/tracker_icons"
    name = "api:bps:tracker_icons"
    requires_auth = False

    @staticmethod
    def _list_tracker_icons(icons_path):
        if not os.path.isdir(icons_path):
            return []
        with os.scandir(icons_path) as entries:
            return [
                {"value": f"/local/bps_icons/{entry.name}", "label": entry.name}
                for entry in entries
                if entry.is_file() and entry.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg"))
            ]

    async def get(self, request):
        hass = request.app["hass"]
        icons_path = hass.config.path("www/bps_icons")
        try:
            custom_icons = await hass.async_add_executor_job(self._list_tracker_icons, icons_path)
            defaults = [
                {"value": "/bps/person.svg", "label": "Person (default)"},
                {"value": "/bps/beacon.svg", "label": "Beacon"},
            ]
            return web.json_response(defaults + custom_icons)
        except Exception as e:
            _LOGGER.error(f"Error listing tracker icons: {e}")
            return web.Response(status=500, text="Error listing tracker icons")


class BPSUploadTrackerIconAPI(HomeAssistantView):
    """API to upload custom tracker icons."""

    url = "/api/bps/upload_tracker_icon"
    name = "api:bps:upload_tracker_icon"
    requires_auth = False

    async def post(self, request):
        hass = request.app["hass"]
        data = await request.post()
        icon_file = data.get("icon")
        if not icon_file:
            return web.Response(status=400, text="Missing icon")

        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(icon_file.filename).name)
        if not safe_name:
            return web.Response(status=400, text="Invalid filename")

        icons_path = hass.config.path("www/bps_icons")
        try:
            await aiofiles.os.makedirs(icons_path, exist_ok=True)
            target_path = Path(icons_path) / safe_name
            async with aiofiles.open(target_path, "wb") as f:
                await f.write(icon_file.file.read())
        except Exception as e:
            _LOGGER.error(f"Failed to upload tracker icon: {e}")
            return web.Response(status=500, text="Failed to upload icon")

        return web.json_response({
            "icon_url": f"/local/bps_icons/{safe_name}",
            "icon_name": safe_name,
        })


class BPSCordsAPI(HomeAssistantView):
    """API endpoint that returns apitricords."""

    url = "/api/bps/cords"
    name = "api:bps:cords"
    requires_auth = False

    def __init__(self, hass):
        """Spara referens till hass"""
        self.hass = hass

    async def get(self, request):
        """Return apitricords from hass.data."""
        apitricords = self.hass.data.get(DOMAIN, {}).get("apitricords", {})

        if not apitricords:
            return web.json_response({"error": "No data available"}, status=404)

        return web.json_response(apitricords)

class BPSEntityWebSocket:
    def __init__(self, hass):
        self.hass = hass
        self.tracked_entities = {}
        self.connections = []

    async def handle_subscribe(self, hass, connection: ActiveConnection, msg: dict):
        """Managing subscription for entities"""
        _LOGGER.debug(f"Received subscription request: {msg}")
        entity_ids = msg["entities"]
        if not entity_ids:
            connection.send_message({
                "id": msg["id"],
                "type": "result",
                "success": False,
                "error": {"code": "invalid_request", "message": "No entities provided."},
            })
            return

        self.connections.append(connection) # Add a connection to subscribed entities
        for entity_id in entity_ids:
            if entity_id not in self.tracked_entities:
                self.tracked_entities[entity_id] = []
            self.tracked_entities[entity_id].append(connection)

        current_states = [] # Send the current state for all subscribed entities
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state:
                current_states.append({
                    "entity_id": entity_id,
                    "state": state.state,
                    "attributes": state.attributes,
                })

        connection.send_message({
            "id": msg["id"],
            "type": "result",
            "success": True,
            "message": f"Subscribed to entities: {entity_ids}",
            "current_states": current_states,  
        })
        async_track_state_change_event(hass, entity_ids, self.state_change_listener) # Listen for state_change


    async def handle_unsubscribe(self, hass, connection: ActiveConnection, msg: dict):
        """Managing unsubscription"""
        _LOGGER.debug(f"Received unsubscribe request: {msg}")
        entity_ids = msg.get("entities", [])
        for entity_id in entity_ids:
            if entity_id in self.tracked_entities:
                if connection in self.tracked_entities[entity_id]:
                    self.tracked_entities[entity_id].remove(connection)
                if not self.tracked_entities[entity_id]:
                    del self.tracked_entities[entity_id]

        connection.send_message({
            "id": msg["id"],
            "type": "result",
            "success": True,
            "message": f"Unsubscribed from entities: {entity_ids}",
        })

    async def handle_known_points(self, hass, connection: ActiveConnection, msg: dict):
        try:
            known_points = msg.get("knownPoints") # Read knownPoints from the message
            if not known_points:
                connection.send_message({
                    "id": msg["id"],
                    "type": "tri_result",
                    "success": False,
                    "error": {"code": "invalid_request", "message": "No knownPoints provided."}
                })
                return

            result = trilaterate(known_points) # Perform trilateration

            if result is None: # If the result is None, return an error
                connection.send_message({
                    "id": msg["id"],
                    "type": "tri_result",
                    "success": False,
                    "error": {"code": "calculation_error", "message": "Trilateration failed."}
                })
                return

            tracker_key = msg.get("tracker")
            result_payload = {
                "x": result[0],
                "y": result[1],
            }
            if tracker_key:
                result_payload["ent"] = tracker_key

            connection.send_message({ # Send back the result
                "id": msg["id"],
                "type": "tri_result",
                "success": True,
                "result": result_payload
            })

        except Exception as e:
            _LOGGER.error(f"Error processing knownPoints: {e}")
            connection.send_message({
                "id": msg["id"],
                "type": "tri_result",
                "success": False,
                "error": {"code": "server_error", "message": str(e)}
            })

    async def state_change_listener(self, event):
        """Listens for status changes and sends them to connections."""
        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        _LOGGER.debug(f"State change for {entity_id}: {old_state} -> {new_state}")

        # Create the message to send to subscribing clients
        message = {
            "type": "state_changed",
            "entity_id": entity_id,
            "old_state": old_state.state if old_state else None,
            "new_state": new_state.state if new_state else None,
        }

        # Send to all connected clients who are subscribed to this entity
        for connection in self.tracked_entities.get(entity_id, []):
            connection.send_message(message)


    def register(self):
        """Registers WebSocket commands"""
        _LOGGER.debug("Registering WebSocket commands")

        def subscribe_wrapper(hass, connection, msg):
            """Wrapper to invoke handle_subscribe."""
            hass.async_create_task(self.handle_subscribe(hass, connection, msg))
        def unsubscribe_wrapper(hass, connection, msg):
            """Wrapper to invoke handle_unsubscribe."""
            hass.async_create_task(self.handle_unsubscribe(hass, connection, msg))
        def known_points_wrapper(hass, connection, msg):
            """Wrapper to invoke handle_known_points."""
            hass.async_create_task(self.handle_known_points(hass, connection, msg))

        async_register_command(
            self.hass,
            "bps/subscribe",
            subscribe_wrapper,  # The wrapper handles async
            schema=vol.Schema({
                vol.Required("type"): "bps/subscribe", # Type for API
                vol.Required("entities"): [str],
                vol.Optional("id"): int,
            }),
        )
        async_register_command(
            self.hass,
            "bps/unsubscribe",
            unsubscribe_wrapper,  # The wrapper handles async
            schema=vol.Schema({
                vol.Required("type"): "bps/unsubscribe", # Type for API
                vol.Required("entities"): [str],
                vol.Optional("id"): int,
            }),
        )
        async_register_command(
            self.hass,
            "bps/known_points",
            known_points_wrapper,  # The wrapper handles async
            schema=vol.Schema({
                vol.Required("type"): "bps/known_points",  # Type for API
                vol.Required("knownPoints"): vol.All(
                    list,
                    [vol.All([float, float, float])]  
                ),
                vol.Optional("tracker"): str,
                vol.Optional("id"): int,  
                }),
        )

        _LOGGER.info("All WebSocket commands registered successfully.")
    
# Trilateration function
def trilaterate(known_points, bounds=None):
    """Weighted least-squares position fit.

    bounds, when given as (minx, miny, maxx, maxy), constrains the solution to
    the floor's extent: the fit then finds the best position WITHIN the map,
    which lands on the boundary when an unconstrained fit would escape it.
    """
    num_points = len(known_points)

    if num_points < 3: # Make sure there are enough points (min 3) to do a trilataration
        _LOGGER.error("At least three known points are required for trilateration.")
        return None

    def objective_function(X, known_points): # Define the objective function loss for the least squares method.
        x, y = X
        residuals = []
        for xi, yi, ri in known_points:
            residual = np.sqrt((xi - x)**2 + (yi - y)**2) - ri
            residuals.append(residual)
        weights = 1.0 / np.array([ri**2 for _, _, ri in known_points])
        return np.sqrt(weights) * np.array(residuals)

    # Start from the receiver centroid: it is always a plausible position,
    # unlike the map corner, and it must lie inside any given bounds.
    x0 = np.array([
        float(np.mean([p[0] for p in known_points])),
        float(np.mean([p[1] for p in known_points])),
    ])
    if bounds is not None:
        minx, miny, maxx, maxy = bounds
        x0[0] = np.clip(x0[0], minx, maxx)
        x0[1] = np.clip(x0[1], miny, maxy)
        result = least_squares(
            objective_function, x0, args=(known_points,),
            bounds=([minx, miny], [maxx, maxy]),
        )
    else:
        result = least_squares(objective_function, x0, args=(known_points,)) # Perform weighting adjustment for the least squares method.

    if not result.success: # Check if the fitting was successful
        _LOGGER.error("Weighted nonlinear least squares fitting did not converge.")
        return None
    x, y = result.x # Extract the calculated coordinates
    return x, y # return the result
