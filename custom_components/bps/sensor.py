from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
import logging

from .const import ACCURACY_ENTITY_ID  # single source of truth (shared with __init__)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "bps_sensors"

# How a BPS device names the Bermuda device it nests under.
#
# HA 2026.8 added DeviceInfo["via_device_id"] - a device-registry id - because
# once every device belongs to a single config entry, an identifier tuple no
# longer names exactly one device. 2026.9 deprecated DeviceInfo["via_device"]
# (the identifier tuple); it stops working in 2027.8, and when it does the
# entity platform refuses the device and every per-tracker BPS sensor with it.
#
# The new key cannot just be sent everywhere: before 2026.8 the entity platform
# splats DeviceInfo straight into async_get_or_create(**info), which has no such
# parameter, so the TypeError drops the sensor instead of the key. And 2026.8+
# raises outright when given both. Exactly one key per core, then. The version
# is the exact boundary - both the key and the parameter arrived in one commit.
VIA_DEVICE_ID_SUPPORTED = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 8)

# (entity_id suffix / unique_id prefix, display label) per tracked device.
SENSOR_KINDS = [
    ("bps_zone", "BPS Zone"),
    ("bps_floor", "BPS Floor"),
    ("bps_nearest_zone", "BPS Nearest Zone"),
    ("bps_sub_zone", "BPS Sub-Zone"),
]


def find_bermuda_via_device(hass, entity):
    """The Bermuda device that owns this tracker's distance_to sensors, so the
    BPS device can nest under it: ``(device_id, identifier)``, one for each way
    a core can be told (see VIA_DEVICE_ID_SUPPORTED). None when it can't be
    resolved (e.g. Bermuda not loaded yet) — the BPS device then just stands on
    its own.

    Only a device found in the registry right now is returned. That matters
    more than it did: an unknown via_device only got logged, but an unknown
    via_device_id makes the entity platform refuse the device, and the sensor
    with it.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    prefix = f"sensor.{entity}_distance_to_"
    for e in ent_reg.entities.values():
        if not (e.platform == "bermuda" and e.device_id and e.entity_id.startswith(prefix)):
            continue
        dev = dev_reg.async_get(e.device_id)
        # A child device (HA 2026.9+) can't be a via device - that raises too -
        # so link to its parent. Nothing on today's Bermuda makes one; cheap to
        # be ready.
        parent_id = getattr(dev, "parent_device_id", None)
        if parent_id:
            dev = dev_reg.async_get(parent_id)
        if not isinstance(dev, dr.DeviceEntry) or not dev.identifiers:
            continue
        # Prefer a bermuda identifier so the link points at the tracker
        # device even if it carries identifiers from several integrations
        # (only possible before 2026.8, which split shared devices).
        berm = [i for i in dev.identifiers if i[0] == "bermuda"]
        return dev.id, (berm[0] if berm else next(iter(dev.identifiers)))
    return None


def ensure_sensors_for_entity(hass, entity, sensors_cache, new_sensors):
    """Create any missing BPS sensors for a tracked device.

    Only the in-memory cache decides whether a sensor exists. A registry
    entry without a live entity object is exactly the situation to recover
    from: it survives a reboot whenever the previous shutdown could not run
    the unload cleanly, and skipping creation for it would leave the sensor
    permanently dead (updates are dropped when the cache has no object).
    async_add_entities re-claims the registry entry via unique_id.
    """
    # Resolve the Bermuda parent (a full entity-registry scan) ONLY when a
    # sensor actually needs creating. In steady state every sensor is already
    # cached, so this returns before touching the registry — the old
    # unconditional scan here ran on every state_changed event and stalled HA
    # (issue #51).
    missing = [(suffix, label) for suffix, label in SENSOR_KINDS
               if f"sensor.{entity}_{suffix}" not in sensors_cache]
    if not missing:
        return
    via_device = find_bermuda_via_device(hass, entity)
    for suffix, label in missing:
        entity_id = f"sensor.{entity}_{suffix}"
        sensor = CustomDistanceSensor(f"{entity} {label}", f"{suffix}_{entity}", entity_id, entity, via_device)
        sensors_cache[entity_id] = sensor
        new_sensors.append(sensor)


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
    """Tracked-device slugs from Bermuda's per-scanner distance sensors.

    Only entities from the `bermuda` integration count. Other integrations also
    expose `_distance_to_` sensors (e.g. an ESPHome mmWave presence sensor's
    `..._distance_to_detection_object`); those aren't trackers and must not get
    BPS zone/floor sensors or a device.
    """
    ent_reg = er.async_get(hass)
    filtered = set()
    for state in hass.states.async_all():
        eid = state.entity_id
        if not (eid.startswith("sensor.") and "_distance_to_" in eid):
            continue
        entry = ent_reg.async_get(eid)
        if entry is None or entry.platform != "bermuda":
            continue
        filtered.add(eid.replace("sensor.", "").split("_distance_to_")[0])
    return list(filtered)

class CustomDistanceSensor(SensorEntity):
    """A representation of a custom sensor"""
    def __init__(self, name, unique_id, entity_id, device_key=None, via_device=None):
        self._name = name
        self._unique_id = unique_id
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._state = "unknown"
        self._attrs = {}
        self.entity_id = entity_id
        # Group each tracked device's BPS sensors under their own device rather
        # than one shared "BLE Positioning System" bucket. All four sensors for
        # a tracked device share the same identifier, so they land together, and
        # via_device nests that device under its Bermuda tracker device.
        if device_key:
            info = DeviceInfo(
                identifiers={("bps", device_key)},
                name=f"{device_key} (BPS)",
                manufacturer="BPS",
                model="BLE Positioning System",
            )
            if via_device:
                via_device_id, via_identifier = via_device
                if VIA_DEVICE_ID_SUPPORTED:
                    info["via_device_id"] = via_device_id
                else:
                    info["via_device"] = via_identifier
            self._attr_device_info = info

    @property
    def device_info(self):
        """The device info, minus a via link whose device has since gone.

        The Bermuda device is resolved when this sensor is built, but the BPS
        device is only created a few awaits later, inside async_add_entities.
        On 2026.8+ a via_device_id that stopped existing in between makes the
        platform refuse the device and drop the sensor - and the sensor object
        is already cached, so nothing would try again until a reload. Read
        once, at add time, so the re-check costs one dict lookup.
        """
        info = getattr(self, "_attr_device_info", None)
        via_id = info.get("via_device_id") if info else None
        hass = getattr(self, "hass", None)
        if via_id and hass is not None and dr.async_get(hass).async_get(via_id) is None:
            return {k: v for k, v in info.items() if k != "via_device_id"}
        return info

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        # Used by the sub-zone sensor to carry "parent_zone"; empty for the rest.
        return self._attrs

class BPSAccuracySensor(SensorEntity):
    """Receiver self-localization accuracy (CEP95 in metres), a global diagnostic.

    Its value is pushed by the backend loop (update_bps_sensor_state sets
    ``_state`` -> native_value); None reads as "unknown" until the first solve.
    """

    _attr_native_unit_of_measurement = "m"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:target"

    def __init__(self):
        self._attr_name = "BPS Position Accuracy"
        self._attr_unique_id = "bps_position_accuracy"
        self.entity_id = ACCURACY_ENTITY_ID
        self._state = None
        self._attrs = {}
        self._attr_device_info = DeviceInfo(
            identifiers={("bps", "bps_system")},
            name="BPS System",
            manufacturer="BPS",
            model="BLE Positioning System",
        )

    @property
    def native_value(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attrs


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
        for suffix, _label in SENSOR_KINDS:
            expected_by_uid[f"{suffix}_{entity}"] = f"sensor.{entity}_{suffix}"

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
        for suffix, _label in SENSOR_KINDS:
            expected_entity_ids.add(f"sensor.{entity}_{suffix}")

    # Remove stale BPS registry entries that are no longer expected.
    entity_registry = er.async_get(hass)
    if expected_entity_ids:
        stale_bps_ids = [
            entry.entity_id
            for entry in entity_registry.entities.values()
            if entry.platform == "bps" and entry.entity_id not in expected_entity_ids
            and entry.entity_id != ACCURACY_ENTITY_ID  # keep the global diagnostic
        ]
        for entity_id in stale_bps_ids:
            _LOGGER.info("Removing stale BPS registry entity: %s", entity_id)
            entity_registry.async_remove(entity_id)

    new_sensors = []
    # The global accuracy diagnostic (once), before the per-tracker sensors.
    if ACCURACY_ENTITY_ID not in hass.data["bps_sensors"]:
        accuracy = BPSAccuracySensor()
        hass.data["bps_sensors"][ACCURACY_ENTITY_ID] = accuracy
        new_sensors.append(accuracy)
    for entity in entities:
        ensure_sensors_for_entity(hass, entity, hass.data["bps_sensors"], new_sensors)

    if new_sensors:
        async_add_entities(new_sensors, update_before_add=True)
        normalize_bps_registry_entity_ids_from_cache(hass)

    @callback
    def state_changed_listener(event):
        """Create BPS sensors when a NEW distance sensor appears.

        This is bound to the GLOBAL state bus, so it fires for every entity's
        every state change in all of HA (the busiest event there is). It must be
        O(1) for the overwhelming majority of those events. Only a newly-ADDED
        ``sensor.*_distance_to_*`` entity (``old_state`` is None) can introduce a
        new tracker; the constant value-updates of existing distance sensors and
        every unrelated entity are skipped cheaply. Without this filter the
        handler ran a full states + entity-registry scan on every state change
        and stalled the event loop (issue #51).
        """
        sensors_cache = hass.data.get("bps_sensors")
        if sensors_cache is None:
            # Integration is unloading/reloading; ignore late state events.
            return

        entity_id = event.data.get("entity_id") or ""
        if "_distance_to_" not in entity_id or event.data.get("old_state") is not None:
            return

        new_entities = get_filtered_entities(hass)
        new_sensors = []

        for entity in new_entities:
            ensure_sensors_for_entity(hass, entity, sensors_cache, new_sensors)

        if new_sensors:
            async_add_entities(new_sensors, update_before_add=True)
            normalize_bps_registry_entity_ids_from_cache(hass)

    old_unsub = hass.data.pop("bps_state_listener_unsub", None)
    if old_unsub:
        old_unsub()
    hass.data["bps_state_listener_unsub"] = hass.bus.async_listen("state_changed", state_changed_listener)
