import aiofiles
import aiofiles.os
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel
from homeassistant.components.websocket_api import async_register_command, ActiveConnection, websocket_command
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.template import Template
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
from asyncio import Lock, Queue, wait_for, TimeoutError

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
apitricords = []


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

        await asyncio.sleep(secToUpdate)  # Run every X seconds, set timer in global variables

async def update_receiver_radii(hass, eids):
    """Update receiver 'r' values (pixels) and raw 'distance' (meters) for an entity"""
    for floor in (f for f in eids["data"]["floor"] if f["scale"] is not None):
        for receiver in floor["receivers"]:
            entity_id = "sensor." + eids["entity"] + "_distance_to_" + receiver["entity_id"]
            rec_value = hass.states.get(entity_id)
            if rec_value is not None:
                try:
                    distance = float(rec_value.state)
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

    tricords = trilaterate(filtered)
    if tricords is not None:
        # Moving average filtering
        history = update_trilateration_and_zone.position_history.setdefault(entity, [])
        history.append(tricords)
        if len(history) > 3:  # Keep only the last 3 positions
            history.pop(0)
        avg_x = sum(pos[0] for pos in history) / len(history)
        avg_y = sum(pos[1] for pos in history) / len(history)

        test_point = Point(float(avg_x), float(avg_y))
        zone = find_zone_for_point(new_global_data, entity, lowest_floor_name, test_point)
        apitricords = update_or_add_entry(apitricords, {"ent": entity, "cords": [avg_x, avg_y], "zone": zone})
        await update_apitricords(hass, apitricords)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_zone", zone)
        update_bps_sensor_state(hass, f"sensor.{entity}_bps_floor", lowest_floor_name)

def update_or_add_entry(data, new_entry):
    for item in data:
        if item["ent"] == new_entry["ent"]:  # Check if "ent" already exists
            item["cords"] = new_entry["cords"]  # Update "cords"
            item["zone"] = new_entry["zone"]  # Update "zone"
            return data

    # If "ent" was not found, add as new post
    data.append(new_entry)
    return data

async def update_apitricords(hass, new_data):
    """Update apitricords in hass.data"""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["apitricords"] = new_data


def update_bps_sensor_state(hass, entity_id, state):
    """Update state on registered BPS SensorEntity instead of raw hass state."""
    sensors_cache = hass.data.get("bps_sensors")
    if not sensors_cache:
        return
    sensor = sensors_cache.get(entity_id)
    if sensor is None:
        return
    sensor._state = state
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

def find_zone_for_point(data, entity, floor_name, point):
    """Find zone for point, prioritize correct polygon, select nearest buffer if no correct zone matches."""
    buffer_percent = 0.05  # set to 5%
    buffer_candidates = []

    def order_zone_points(coords):
        """Order polygon points clockwise around centroid to avoid self-intersections."""
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
                        ordered_coords = order_zone_points(zone["cords"])
                        polygon = Polygon([(coord["x"], coord["y"]) for coord in ordered_coords])
                        xs = [coord["x"] for coord in ordered_coords]
                        ys = [coord["y"] for coord in ordered_coords]
                        width = max(xs) - min(xs)
                        height = max(ys) - min(ys)
                        buffer_size = ((width + height) / 2) * buffer_percent
                        # covers() also matches points on the polygon boundary.
                        if polygon.covers(point):
                            return zone["entity_id"]  # Prioritize correct polygon
                        elif polygon.buffer(buffer_size).contains(point):
                            # Save candidate: (distance to edge, entity_id)
                            distance_to_edge = polygon.exterior.distance(point)
                            buffer_candidates.append((distance_to_edge, zone["entity_id"]))
    if buffer_candidates:
        # Select zone whose edge is closest to the point
        buffer_candidates.sort()
        return buffer_candidates[0][1]
    return "unknown"

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
            hass.http.register_view(BPSCordsAPI(hass))
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
            expand(states.sensor)
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

        hass.bus.async_listen_once("homeassistant_stop", lambda event: observer.stop())

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

    # Remove BPS states from the state machine when integration is unloaded.
    bps_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.") and state.entity_id.endswith(("_bps_zone", "_bps_floor"))
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

        return web.FileResponse(path=str(frontend_path))

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
               
            _LOGGER.info(f"Saved coordinates to bpsdata: {coordinates}")
            return web.Response(status=200, text="Coordinates saved successfully")
        
        except Exception as e:
            _LOGGER.error(f"Failed to save coordinates: {e}")
            return web.Response(status=500, text="Failed to save coordinates")
        
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
            expand(states.sensor)
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
            expand(states.sensor)
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

        try:
            if not bpsdata_file_path.is_file(): # Check if the file exists
                return web.Response(status=404, text="bpsdata.txt not found")

            async with aiofiles.open(bpsdata_file_path, "r") as f: # Read the content of the file
                content = await f.read()

            _LOGGER.info(f"Read coordinates from bpsdata: {content}")
            return web.json_response({
                "coordinates": content,
                "entities": entities,
                "receivers": receivers
            })
        
        except Exception as e:
            _LOGGER.error(f"Failed to read coordinates: {e}")
            return web.Response(status=500, text="Failed to read coordinates")

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
def trilaterate(known_points):
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

    x0 = np.array([0, 0]) # Initial guess value for unknown coordinates

    result = least_squares(objective_function, x0, args=(known_points,)) # Perform weighting adjustment for the least squares method.

    if not result.success: # Check if the fitting was successful
        _LOGGER.error("Weighted nonlinear least squares fitting did not converge.")
        return None
    x, y = result.x # Extract the calculated coordinates
    return x, y # return the result
