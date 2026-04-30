from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
import logging

_LOGGER = logging.getLogger(__name__)

DOMAIN = "bps_sensors"


def is_legacy_bps_entity_id(entity_id):
    """Detect old duplicated-name entity IDs like sensor.name_name_bps_floor."""
    if not entity_id.startswith("sensor.") or "_bps_" not in entity_id:
        return False

    if not (entity_id.endswith("_bps_floor") or entity_id.endswith("_bps_zone")):
        return False

    object_id = entity_id.replace("sensor.", "")
    base_name = object_id.rsplit("_bps_", 1)[0]
    parts = base_name.split("_")

    # Legacy format duplicates the full object id: <name>_<name>
    if len(parts) % 2 != 0:
        return False

    half = len(parts) // 2
    return parts[:half] == parts[half:]

def get_filtered_entities(hass):
    """Fetch and filter sensors based on their entity_id"""
    sensors = [state for state in hass.states.async_all() if state.entity_id.startswith("sensor.")]
    filtered = [
        state.entity_id.replace("sensor.", "").split("_distance_to_")[0]
        for state in sensors
        if "_distance_to_" in state.entity_id
    ]
    return list(set(filtered))

class CustomDistanceSensor(SensorEntity):
    """A representation of a custom sensor"""
    def __init__(self, name, unique_id, entity_id):
        self._name = name
        self._unique_id = unique_id
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._state = "unknown"
        self.entity_id = entity_id
    
    @property
    def name(self):
        return self._name
    
    @property
    def unique_id(self):
        return self._unique_id
    
    @property
    def state(self):
        return self._state

def cleanup_legacy_bps_entities(hass):
    """Remove old duplicated-name BPS entities from entity registry."""
    entity_registry = er.async_get(hass)
    stale_entities = [
        entry.entity_id
        for entry in entity_registry.entities.values()
        if is_legacy_bps_entity_id(entry.entity_id)
    ]

    for entity_id in stale_entities:
        _LOGGER.info("Removing legacy BPS entity: %s", entity_id)
        entity_registry.async_remove(entity_id)

    cleanup_legacy_bps_states(hass)


def cleanup_legacy_bps_states(hass):
    """Remove lingering legacy states from the state machine."""
    legacy_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if is_legacy_bps_entity_id(state.entity_id)
    ]
    for entity_id in legacy_state_ids:
        _LOGGER.info("Removing legacy BPS state: %s", entity_id)
        hass.states.async_remove(entity_id)


def normalize_bps_registry_entity_ids(hass, entities):
    """Ensure BPS registry entries use stable non-legacy entity_id by unique_id."""
    entity_registry = er.async_get(hass)
    expected_by_uid = {}
    for entity in entities:
        expected_by_uid[f"bps_zone_{entity}"] = f"sensor.{entity}_bps_zone"
        expected_by_uid[f"bps_floor_{entity}"] = f"sensor.{entity}_bps_floor"

    for entry in list(entity_registry.entities.values()):
        expected_entity_id = expected_by_uid.get(entry.unique_id)
        if expected_entity_id and entry.entity_id != expected_entity_id:
            _LOGGER.info(
                "Migrating BPS entity_id from %s to %s",
                entry.entity_id,
                expected_entity_id,
            )
            try:
                entity_registry.async_update_entity(
                    entry.entity_id,
                    new_entity_id=expected_entity_id,
                )
            except ValueError:
                # If the target id is blocked by stale data/entry, remove old entry and recreate.
                _LOGGER.info("Removing conflicting BPS registry entity: %s", entry.entity_id)
                entity_registry.async_remove(entry.entity_id)


def normalize_bps_registry_entity_ids_from_cache(hass):
    """Normalize BPS entity_ids using the in-memory sensor cache unique_ids."""
    sensors_cache = hass.data.get("bps_sensors", {})
    if not sensors_cache:
        return

    expected_by_uid = {}
    for expected_entity_id, sensor in sensors_cache.items():
        uid = getattr(sensor, "unique_id", None)
        if uid and expected_entity_id.startswith("sensor."):
            expected_by_uid[uid] = expected_entity_id

    if not expected_by_uid:
        return

    entity_registry = er.async_get(hass)
    for entry in list(entity_registry.entities.values()):
        expected_entity_id = expected_by_uid.get(entry.unique_id)
        if expected_entity_id and entry.entity_id != expected_entity_id:
            _LOGGER.info(
                "Post-add migration of BPS entity_id from %s to %s",
                entry.entity_id,
                expected_entity_id,
            )
            try:
                entity_registry.async_update_entity(
                    entry.entity_id,
                    new_entity_id=expected_entity_id,
                )
            except ValueError:
                _LOGGER.info("Removing conflicting BPS registry entity: %s", entry.entity_id)
                entity_registry.async_remove(entry.entity_id)

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set dynamic sensors based on the filtered entities"""
    _LOGGER.info("async_setup_entry in sensor.py has been called")
    
    if "bps_sensors" not in hass.data:
        hass.data["bps_sensors"] = {}

    cleanup_legacy_bps_entities(hass)

    entities = get_filtered_entities(hass)
    _LOGGER.info(f"Creating sensors for entities: {entities}")
    normalize_bps_registry_entity_ids(hass, entities)

    expected_entity_ids = set()
    for entity in entities:
        expected_entity_ids.add(f"sensor.{entity}_bps_zone")
        expected_entity_ids.add(f"sensor.{entity}_bps_floor")

    # Remove stale BPS registry entries that are no longer expected.
    entity_registry = er.async_get(hass)
    if expected_entity_ids:
        stale_bps_ids = [
            entry.entity_id
            for entry in entity_registry.entities.values()
            if entry.platform == "bps" and entry.entity_id not in expected_entity_ids
        ]
        for entity_id in stale_bps_ids:
            _LOGGER.info("Removing stale BPS registry entity: %s", entity_id)
            entity_registry.async_remove(entity_id)

    existing_sensors = {
        entry.entity_id
        for entry in entity_registry.entities.values()
        if entry.platform == "bps"
    }

    new_sensors = []
    for entity in entities:
        unique_zone_id = f"sensor.{entity}_bps_zone"
        unique_zone_uid = f"bps_zone_{entity}"
        unique_floor_id = f"sensor.{entity}_bps_floor"
        unique_floor_uid = f"bps_floor_{entity}"

        zone_exists = (
            any(s.startswith(unique_zone_id) for s in existing_sensors)
            or unique_zone_id in hass.data["bps_sensors"]
        )
        if not zone_exists:
            sensor = CustomDistanceSensor(f"{entity} BPS Zone", unique_zone_uid, unique_zone_id)
            hass.data["bps_sensors"][unique_zone_id] = sensor
            new_sensors.append(sensor)

        floor_exists = (
            any(s.startswith(unique_floor_id) for s in existing_sensors)
            or unique_floor_id in hass.data["bps_sensors"]
        )
        if not floor_exists:
            sensor = CustomDistanceSensor(f"{entity} BPS Floor", unique_floor_uid, unique_floor_id)
            hass.data["bps_sensors"][unique_floor_id] = sensor
            new_sensors.append(sensor)

    if new_sensors:
        async_add_entities(new_sensors, update_before_add=True)
        normalize_bps_registry_entity_ids_from_cache(hass)

    @callback
    def state_changed_listener(event):
        """Listen for state changes to update dynamic sensors"""
        sensors_cache = hass.data.get("bps_sensors")
        if sensors_cache is None:
            # Integration is unloading/reloading; ignore late state events.
            return

        new_entities = get_filtered_entities(hass)
        new_sensors = []

        entity_registry = er.async_get(hass)
        existing_sensors = {
            entry.entity_id
            for entry in entity_registry.entities.values()
            if entry.platform == "bps"
        }

        for entity in new_entities:
            unique_zone_id = f"sensor.{entity}_bps_zone"
            unique_zone_uid = f"bps_zone_{entity}"
            unique_floor_id = f"sensor.{entity}_bps_floor"
            unique_floor_uid = f"bps_floor_{entity}"

            zone_exists = (
                any(s.startswith(unique_zone_id) for s in existing_sensors)
                or unique_zone_id in sensors_cache
            )
            if not zone_exists:
                sensor = CustomDistanceSensor(f"{entity} BPS Zone", unique_zone_uid, unique_zone_id)
                sensors_cache[unique_zone_id] = sensor
                new_sensors.append(sensor)

            floor_exists = (
                any(s.startswith(unique_floor_id) for s in existing_sensors)
                or unique_floor_id in sensors_cache
            )
            if not floor_exists:
                sensor = CustomDistanceSensor(f"{entity} BPS Floor", unique_floor_uid, unique_floor_id)
                sensors_cache[unique_floor_id] = sensor
                new_sensors.append(sensor)

        if new_sensors:
            async_add_entities(new_sensors, update_before_add=True)
            normalize_bps_registry_entity_ids_from_cache(hass)

    old_unsub = hass.data.pop("bps_state_listener_unsub", None)
    if old_unsub:
        old_unsub()
    hass.data["bps_state_listener_unsub"] = hass.bus.async_listen("state_changed", state_changed_listener)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """If using configuration in configuration.yaml"""
    await async_setup_entry(hass, config, async_add_entities)
