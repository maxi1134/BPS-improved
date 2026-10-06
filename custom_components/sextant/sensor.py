from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
import logging

from homeassistant.helpers.event import async_call_later

from .const import ACCURACY_ENTITY_ID, GLOBAL_ENTITY_IDS, UNTRACKED_ENTITY_ID  # single source of truth (shared with __init__)
from . import bermuda_source

_LOGGER = logging.getLogger(__name__)

DOMAIN = "sextant_sensors"

# (entity_id suffix / unique_id prefix, display label) per tracked device.
SENSOR_KINDS = [
    ("sextant_room", "Sextant Room"),
    ("sextant_floor", "Sextant Floor"),
    ("sextant_nearest_room", "Sextant Nearest Room"),
    ("sextant_spot", "Sextant Spot"),
    # The one an automation should usually read: the spot when the thing is in
    # one, the room when it is not. Without it every automation that wants "so
    # where is it" has to write the same template over the two sensors above,
    # once per thing, and get the unknown handling right each time.
    ("sextant_location", "Sextant Location"),
]
# What a sensor's attributes read before it has ever been published, keyed by
# kind. A thing that has never been heard still has its entities, and an
# automation reading state_attr(..., "kind") on one should get the same shape it
# will get later rather than None - "unknown" is an answer, a missing attribute
# is a bug in whatever reads it.
INITIAL_ATTRS = {
    "sextant_location": {"kind": "room", "room": "unknown", "spot": None, "floor": "unknown",
                         "area_id": None, "floor_id": None},
    "sextant_room": {"area_id": None},
    "sextant_spot": {"room": "unknown"},
}

# 3.8.0 renamed zones to rooms and sub-zones to spots in the entity ids too.
# Registry entries with the old unique_id prefixes are moved to the new ones
# (id, name and history follow), so nothing is orphaned by the rename.
RENAMED_KINDS = {
    "sextant_zone": "sextant_room",
    "sextant_nearest_zone": "sextant_nearest_room",
    "sextant_sub_zone": "sextant_spot",
}


def migrate_renamed_sensor_kinds(hass):
    """Move registry entries from the pre-3.8 unique_ids / entity_ids to the new names."""
    entity_registry = er.async_get(hass)
    for entry in list(entity_registry.entities.values()):
        if entry.platform != "sextant" or not entry.unique_id:
            continue
        for old, new in RENAMED_KINDS.items():
            if not entry.unique_id.startswith(old + "_"):
                continue
            entity = entry.unique_id[len(old) + 1:]
            new_uid, new_eid = f"{new}_{entity}", f"sensor.{entity}_{new}"
            _LOGGER.info("Renaming Sextant sensor %s -> %s", entry.entity_id, new_eid)
            try:
                entity_registry.async_update_entity(entry.entity_id, new_unique_id=new_uid, new_entity_id=new_eid)
            except ValueError:
                _LOGGER.info("Removing Sextant registry entity %s that blocks the rename", entry.entity_id)
                entity_registry.async_remove(entry.entity_id)
            break


def find_bermuda_via_device(hass, entity):
    """Identifier of the Bermuda device that owns this thing's distance_to
    sensors, so the Sextant device can nest under it (via_device). None when it
    can't be resolved (e.g. Bermuda not loaded yet) — the Sextant device then just
    stands on its own.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    prefix = f"sensor.{entity}_distance_to_"
    for e in ent_reg.entities.values():
        if e.platform == "bermuda" and e.device_id and e.entity_id.startswith(prefix):
            dev = dev_reg.async_get(e.device_id)
            if dev and dev.identifiers:
                # Prefer a bermuda identifier so the link points at the thing
                # device even if it carries identifiers from several integrations.
                berm = [i for i in dev.identifiers if i[0] == "bermuda"]
                return berm[0] if berm else next(iter(dev.identifiers))
    return None


def ensure_sensors_for_entity(hass, entity, sensors_cache, new_sensors):
    """Create any missing Sextant sensors for a tracked device.

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
        sensor = CustomDistanceSensor(f"{entity} {label}", f"{suffix}_{entity}", entity_id, entity, via_device,
                                      attrs=INITIAL_ATTRS.get(suffix))
        sensors_cache[entity_id] = sensor
        new_sensors.append(sensor)


def thing_of_unique_id(unique_id):
    """The thing slug behind a per-thing Sextant unique_id, else None."""
    if not unique_id:
        return None
    for suffix, _label in SENSOR_KINDS:
        if unique_id.startswith(suffix + "_"):
            return unique_id[len(suffix) + 1:]
    return None


def _sextant_device(hass, dev_reg, thing):
    """The ``<thing> (Sextant)`` device, looked up the way the running core wants.

    2026.9 deprecates async_get_device(identifiers=...) in favour of the
    per-config-entry lookup; older cores only have the former.
    """
    identifier = ("sextant", thing)
    by_identifier = getattr(dev_reg, "async_get_device_by_identifier", None)
    entries = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    if by_identifier is not None and entries is not None:
        for entry in entries("sextant"):
            device = by_identifier(identifier, entry.entry_id)
            if device is not None:
                return device
        return None
    return dev_reg.async_get_device(identifiers={identifier})


def _remove_sextant_device(hass, thing):
    """Drop the ``<thing> (Sextant)`` device once none of its entities remain."""
    dev_reg = dr.async_get(hass)
    device = _sextant_device(hass, dev_reg, thing)
    if device is None:
        return False
    ent_reg = er.async_get(hass)
    if any(e.device_id == device.id for e in ent_reg.entities.values()):
        return False
    dev_reg.async_remove_device(device.id)
    return True


@callback
def remove_sensors_for_things(hass, things, reason="untracked"):
    """Remove the Sextant sensors and device of each thing in ``things``.

    Cache object, registry entry, state and the per-thing device all go.
    Called from the Things page's untrack (ws bermuda/track with ``remove``)
    and from the reconcile below. Removing the registry entry makes HA retire
    the live entity too, so this is the whole cleanup. Returns the number of
    registry entries removed.
    """
    things = [t for t in things if t]
    if not things:
        return 0
    sensors_cache = hass.data.get("sextant_sensors") or {}
    ent_reg = er.async_get(hass)
    states = getattr(hass, "states", None)
    removed = 0
    for thing in things:
        for suffix, _label in SENSOR_KINDS:
            entity_id = f"sensor.{thing}_{suffix}"
            sensors_cache.pop(entity_id, None)
            if ent_reg.async_get(entity_id) is not None:
                ent_reg.async_remove(entity_id)
                removed += 1
            if getattr(states, "get", None) is not None and states.get(entity_id) is not None:
                states.async_remove(entity_id)
        _remove_sextant_device(hass, thing)
    _LOGGER.info("Removed the Sextant sensors of %d %s thing(s): %s", len(things), reason, ", ".join(things))
    return removed


@callback
def prune_sensors_for_untracked(hass, tracked):
    """Reconcile Sextant's sensors against the set of things Bermuda reports.

    ``tracked`` must be Bermuda's FULL tracked set (never the things heard
    this cycle: a phone that is out for the day is still tracked). Anything
    Sextant still carries for a device outside it is an orphan: a device
    untracked while HA was down, or before this reconcile existed, whose
    four sensors would otherwise sit in the registry as ``unavailable``
    forever. Two safety rails: an empty set never prunes (a Bermuda reload
    can report nothing for a moment), and a set has to be reported twice in
    a row before it is acted on, so a single odd answer changes nothing.
    Cheap in steady state: one set comparison per cycle, the registry is
    only scanned when the tracked set changed.
    """
    if not tracked:
        return 0
    tracked = frozenset(tracked)
    if hass.data.get("sextant_pruned_for") == tracked:
        return 0
    seen = hass.data.get("sextant_prune_candidate")
    if seen != tracked:
        hass.data["sextant_prune_candidate"] = tracked
        return 0
    ent_reg = er.async_get(hass)
    stale = set()
    for entry in list(ent_reg.entities.values()):
        if entry.platform != "sextant" or entry.entity_id in GLOBAL_ENTITY_IDS:
            continue
        thing = thing_of_unique_id(entry.unique_id)
        if thing is not None and thing not in tracked:
            stale.add(thing)
    for entity_id, sensor in list((hass.data.get("sextant_sensors") or {}).items()):
        if entity_id in GLOBAL_ENTITY_IDS:
            continue
        thing = thing_of_unique_id(getattr(sensor, "unique_id", None))
        if thing is not None and thing not in tracked:
            stale.add(thing)
    removed = remove_sensors_for_things(hass, sorted(stale), reason="no longer tracked") if stale else 0
    hass.data["sextant_pruned_for"] = tracked
    return removed


def is_legacy_sextant_entity_id(entity_id):
    """Detect old duplicated-name entity IDs like sensor.name_name_sextant_floor."""
    if not entity_id.startswith("sensor.") or "_sextant_" not in entity_id:
        return False

    if not entity_id.endswith(("_sextant_floor", "_sextant_zone", "_sextant_room")):
        return False

    object_id = entity_id.replace("sensor.", "")
    base_name = object_id.rsplit("_sextant_", 1)[0]
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
    `..._distance_to_detection_object`); those aren't things and must not get
    Sextant zone/floor sensors or a device.
    """
    # Prefer what Bermuda says it is TRACKING over what happens to have an
    # entity. Bermuda ships its per-scanner distance entities disabled, so a
    # states scan finds none of them and Sextant would create no per-thing
    # sensors at all. The API answer is the same set, minus that dependency.
    tracked = bermuda_source.async_get_tracked_device_prefixes(hass)
    if tracked is not None:
        return sorted(tracked)

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
    # Pushed by the positioning cycle; there is nothing for Home Assistant to poll.
    _attr_should_poll = False

    # last_heard moves every cycle a thing is heard. Left out of the recorder's
    # attribute rows so a day of it does not fill the database with copies of
    # attributes that differ only by a timestamp; presence is kept, it is the
    # one automations and history care about.
    _unrecorded_attributes = frozenset({"last_heard"})

    """A representation of a custom sensor"""
    def __init__(self, name, unique_id, entity_id, device_key=None, via_device=None, attrs=None):
        self._name = name
        self._unique_id = unique_id
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._state = "unknown"
        self._attrs = dict(attrs) if attrs else {}
        self.entity_id = entity_id
        # Group each tracked device's Sextant sensors under their own device rather
        # than one shared "BLE Positioning System" bucket. All four sensors for
        # a tracked device share the same identifier, so they land together, and
        # via_device nests that device under its Bermuda thing device.
        if device_key:
            info = DeviceInfo(
                identifiers={("sextant", device_key)},
                name=f"{device_key} (Sextant)",
                manufacturer="Sextant",
                model="Sextant (BLE Positioning)",
            )
            if via_device:
                info["via_device"] = via_device
            self._attr_device_info = info

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
        # Used by the spot sensor to carry "room"; empty for the rest.
        return self._attrs

class SextantUntrackedSensor(SensorEntity):
    # Pushed by the positioning cycle; there is nothing for Home Assistant to poll.
    _attr_should_poll = False

    """How many people the mmWave radars see that no tracked thing accounts for.

    A guest, or someone whose phone is on the charger in another room. The
    rooms they are in are an attribute. 0 with no radars placed.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-question"

    def __init__(self):
        self._attr_name = "Untracked People"
        self._attr_unique_id = "sextant_untracked_people"
        self.entity_id = UNTRACKED_ENTITY_ID
        self._state = None
        self._attrs = {}
        self._attr_device_info = DeviceInfo(
            identifiers={("sextant", "sextant_system")},
            name="Sextant",
            manufacturer="Sextant",
            model="Sextant (BLE Positioning)",
        )

    @property
    def native_value(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attrs


class SextantAccuracySensor(SensorEntity):
    # Pushed by the positioning cycle; there is nothing for Home Assistant to poll.
    _attr_should_poll = False

    """Receiver self-localization accuracy (CEP95 in metres), a global diagnostic.

    Its value is pushed by the backend loop (update_sextant_sensor_state sets
    ``_state`` -> native_value); None reads as "unknown" until the first solve.
    """

    _attr_native_unit_of_measurement = "m"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:target"

    def __init__(self):
        self._attr_name = "Position Accuracy"
        self._attr_unique_id = "sextant_position_accuracy"
        self.entity_id = ACCURACY_ENTITY_ID
        self._state = None
        self._attrs = {}
        self._attr_device_info = DeviceInfo(
            identifiers={("sextant", "sextant_system")},
            name="Sextant",
            manufacturer="Sextant",
            model="Sextant (BLE Positioning)",
        )

    @property
    def native_value(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attrs


@callback
def ensure_sensors_for_things(hass, things):
    """Create the sensors of any thing in ``things`` that has none yet.

    Called from the positioning loop with the things Bermuda reports, so
    a device added to Bermuda after setup gets its zone/floor sensors on the
    next cycle. O(things) dictionary lookups in steady state; the registry
    is only touched when something is actually missing.
    """
    sensors_cache = hass.data.get("sextant_sensors")
    add_entities = hass.data.get("sextant_add_entities")
    if sensors_cache is None or add_entities is None:
        return
    new_sensors = []
    for entity in things:
        ensure_sensors_for_entity(hass, entity, sensors_cache, new_sensors)
    if new_sensors:
        _LOGGER.info("Creating Sextant sensors for %d thing(s) added since setup", len(new_sensors) // len(SENSOR_KINDS) or 1)
        add_entities(new_sensors, update_before_add=True)
        normalize_sextant_registry_entity_ids_from_cache(hass)


@callback
def ensure_person_sensors(hass, people):
    """Create the location sensors of any owner (a person.* entity) that has none yet.

    sensor.<person>_sextant_person_location / _room / _floor, grouped under a
    Sextant device for the person. "_person_" keeps them apart from a thing
    that shares the person's name (Meg the cat is person.meg and her tag meg).
    """
    from .persons import PERSON_SENSOR_KINDS  # noqa: PLC0415

    sensors_cache = hass.data.get("sextant_sensors")
    add_entities = hass.data.get("sextant_add_entities")
    if sensors_cache is None or add_entities is None:
        return
    new_sensors = []
    for person in people:
        slug = person.split(".", 1)[1]
        for suffix, label in PERSON_SENSOR_KINDS:
            entity_id = f"sensor.{slug}_{suffix}"
            if entity_id in sensors_cache:
                continue
            name = (hass.states.get(person).attributes.get("friendly_name") if hass.states.get(person) else None) or slug
            sensor = CustomDistanceSensor(f"{name} {label}", f"{suffix}_{slug}", entity_id, f"person_{slug}",
                                          attrs={"via": None})
            sensors_cache[entity_id] = sensor
            new_sensors.append(sensor)
    if new_sensors:
        _LOGGER.info("Creating Sextant location sensors for %d person(s)", len(new_sensors) // 3 or 1)
        add_entities(new_sensors, update_before_add=True)


@callback
def prune_person_sensors(hass, people):
    """Remove the location sensors of anyone who no longer owns a thing.

    ``people`` are the person.* entity ids that still own something. Cache
    object, registry entry, state and the person's Sextant device all go,
    the same way a thing's do in remove_sensors_for_things.
    """
    from .persons import PERSON_SENSOR_KINDS  # noqa: PLC0415

    keep = {p.split(".", 1)[1] for p in people}
    sensors_cache = hass.data.get("sextant_sensors") or {}
    ent_reg = er.async_get(hass)
    gone = set()
    for entry in list(ent_reg.entities.values()):
        if entry.platform != "sextant" or not isinstance(entry.unique_id, str):
            continue
        for suffix, _label in PERSON_SENSOR_KINDS:
            if entry.unique_id.startswith(suffix + "_") and entry.unique_id[len(suffix) + 1:] not in keep:
                gone.add(entry.unique_id[len(suffix) + 1:])
                sensors_cache.pop(entry.entity_id, None)
                ent_reg.async_remove(entry.entity_id)
    for slug in gone:
        _remove_sextant_device(hass, f"person_{slug}")
    if gone:
        _LOGGER.info("Removed the Sextant location sensors of %d person(s) who own nothing now", len(gone))
    return len(gone)


def cleanup_legacy_sextant_entities(hass):
    """Remove old duplicated-name Sextant entities from entity registry."""
    entity_registry = er.async_get(hass)
    stale_entities = [
        entry.entity_id
        for entry in entity_registry.entities.values()
        if is_legacy_sextant_entity_id(entry.entity_id)
    ]

    for entity_id in stale_entities:
        _LOGGER.info("Removing legacy Sextant entity: %s", entity_id)
        entity_registry.async_remove(entity_id)

    cleanup_legacy_sextant_states(hass)


def cleanup_legacy_sextant_states(hass):
    """Remove lingering legacy states from the state machine."""
    legacy_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if is_legacy_sextant_entity_id(state.entity_id)
    ]
    for entity_id in legacy_state_ids:
        _LOGGER.info("Removing legacy Sextant state: %s", entity_id)
        hass.states.async_remove(entity_id)


def normalize_sextant_registry_entity_ids(hass, entities):
    """Ensure Sextant registry entries use stable non-legacy entity_id by unique_id."""
    entity_registry = er.async_get(hass)
    expected_by_uid = {}
    for entity in entities:
        for suffix, _label in SENSOR_KINDS:
            expected_by_uid[f"{suffix}_{entity}"] = f"sensor.{entity}_{suffix}"

    for entry in list(entity_registry.entities.values()):
        expected_entity_id = expected_by_uid.get(entry.unique_id)
        if expected_entity_id and entry.entity_id != expected_entity_id:
            _LOGGER.info(
                "Migrating Sextant entity_id from %s to %s",
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
                _LOGGER.info("Removing conflicting Sextant registry entity: %s", entry.entity_id)
                entity_registry.async_remove(entry.entity_id)


def normalize_sextant_registry_entity_ids_from_cache(hass):
    """Normalize Sextant entity_ids using the in-memory sensor cache unique_ids."""
    sensors_cache = hass.data.get("sextant_sensors", {})
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
                "Post-add migration of Sextant entity_id from %s to %s",
                entry.entity_id,
                expected_entity_id,
            )
            try:
                entity_registry.async_update_entity(
                    entry.entity_id,
                    new_entity_id=expected_entity_id,
                )
            except ValueError:
                _LOGGER.info("Removing conflicting Sextant registry entity: %s", entry.entity_id)
                entity_registry.async_remove(entry.entity_id)

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set dynamic sensors based on the filtered entities"""
    _LOGGER.info("async_setup_entry in sensor.py has been called")
    
    if "sextant_sensors" not in hass.data:
        hass.data["sextant_sensors"] = {}

    cleanup_legacy_sextant_entities(hass)
    migrate_renamed_sensor_kinds(hass)

    entities = get_filtered_entities(hass)
    _LOGGER.info(f"Creating sensors for entities: {entities}")
    normalize_sextant_registry_entity_ids(hass, entities)

    expected_entity_ids = set()
    for entity in entities:
        for suffix, _label in SENSOR_KINDS:
            expected_entity_ids.add(f"sensor.{entity}_{suffix}")

    # Remove stale Sextant registry entries that are no longer expected.
    # A person's sensors are not a thing's and are pruned by
    # prune_person_sensors; swept here, every setup deleted them and the
    # user's name, area and disabled flag with them.
    from .persons import PERSON_SENSOR_KINDS  # noqa: PLC0415
    person_prefixes = tuple(f"{suffix}_" for suffix, _label in PERSON_SENSOR_KINDS)
    entity_registry = er.async_get(hass)
    if expected_entity_ids:
        stale_sextant_ids = [
            entry.entity_id
            for entry in entity_registry.entities.values()
            if entry.platform == "sextant" and entry.entity_id not in expected_entity_ids
            and entry.entity_id not in GLOBAL_ENTITY_IDS  # keep the global sensors
            and not (isinstance(entry.unique_id, str) and entry.unique_id.startswith(person_prefixes))
        ]
        for entity_id in stale_sextant_ids:
            _LOGGER.info("Removing stale Sextant registry entity: %s", entity_id)
            entity_registry.async_remove(entity_id)

    # Kept so the positioning loop can create sensors for a thing that
    # appears AFTER setup (a device added in Bermuda while HA runs): with
    # Bermuda's distance entities disabled, the state listener below never
    # sees such a device, and it would be positioned but have no sensors
    # until the next restart.
    hass.data["sextant_add_entities"] = async_add_entities

    new_sensors = []
    # The global accuracy diagnostic (once), before the per-thing sensors.
    if ACCURACY_ENTITY_ID not in hass.data["sextant_sensors"]:
        accuracy = SextantAccuracySensor()
        hass.data["sextant_sensors"][ACCURACY_ENTITY_ID] = accuracy
        new_sensors.append(accuracy)
    if UNTRACKED_ENTITY_ID not in hass.data["sextant_sensors"]:
        untracked = SextantUntrackedSensor()
        hass.data["sextant_sensors"][UNTRACKED_ENTITY_ID] = untracked
        new_sensors.append(untracked)
    for entity in entities:
        ensure_sensors_for_entity(hass, entity, hass.data["sextant_sensors"], new_sensors)

    if new_sensors:
        async_add_entities(new_sensors, update_before_add=True)
        normalize_sextant_registry_entity_ids_from_cache(hass)

    @callback
    def state_changed_listener(event):
        """Create Sextant sensors when a NEW distance sensor appears.

        This is bound to the GLOBAL state bus, so it fires for every entity's
        every state change in all of HA (the busiest event there is). It must be
        O(1) for the overwhelming majority of those events. Only a newly-ADDED
        ``sensor.*_distance_to_*`` entity (``old_state`` is None) can introduce a
        new thing; the constant value-updates of existing distance sensors and
        every unrelated entity are skipped cheaply. Without this filter the
        handler ran a full states + entity-registry scan on every state change
        and stalled the event loop (issue #51).
        """
        sensors_cache = hass.data.get("sextant_sensors")
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
            normalize_sextant_registry_entity_ids_from_cache(hass)

    old_unsub = hass.data.pop("sextant_state_listener_unsub", None)
    if old_unsub:
        old_unsub()
    hass.data["sextant_state_listener_unsub"] = hass.bus.async_listen("state_changed", state_changed_listener)

    # The state_changed hook above can only spot a new thing when a distance
    # ENTITY appears. With those entities disabled none ever appears, so a
    # device newly tracked in Bermuda would never get Sextant sensors. Subscribe to
    # Bermuda's coordinator as well - it is a plain DataUpdateCoordinator, so
    # this is its supported listener, not a bespoke event, and nothing crosses
    # the websocket.
    @callback
    def bermuda_updated():
        sensors_cache = hass.data.get("sextant_sensors")
        if sensors_cache is None:
            return  # unloading/reloading
        # Cheap guard: only do the (registry-walking) discovery when the set of
        # tracked devices has actually changed.
        tracked = bermuda_source.async_get_tracked_device_prefixes(hass)
        if tracked is None or tracked == hass.data.get("sextant_known_things"):
            return
        hass.data["sextant_known_things"] = set(tracked)

        new_sensors = []
        for entity in sorted(tracked):
            ensure_sensors_for_entity(hass, entity, sensors_cache, new_sensors)
        if new_sensors:
            async_add_entities(new_sensors, update_before_add=True)
            normalize_sextant_registry_entity_ids_from_cache(hass)

    old_berm_unsub = hass.data.pop("sextant_bermuda_listener_unsub", None)
    if old_berm_unsub:
        old_berm_unsub()
    old_retry_unsub = hass.data.pop("sextant_bermuda_retry_unsub", None)
    if old_retry_unsub:
        old_retry_unsub()  # a pending retry from before a reload would subscribe twice

    @callback  # without it async_call_later runs the retry on a worker thread
    def _try_subscribe(_now=None):
        """Attach to Bermuda's coordinator, retrying until it exists.

        Sextant and Bermuda both load at startup and the order is not guaranteed.
        If Bermuda's config entry is not ready when this platform sets up,
        async_subscribe returns None - and without a retry Sextant would sit with
        no per-thing sensors forever, because the state_changed hook it used
        to rely on never fires for disabled distance entities.
        """
        hass.data.pop("sextant_bermuda_retry_unsub", None)
        if hass.data.get("sextant_sensors") is None:
            return  # unloading/reloading; stop retrying
        unsub = bermuda_source.async_subscribe(hass, bermuda_updated)
        if unsub is None:
            hass.data["sextant_bermuda_retry_unsub"] = async_call_later(hass, 30, _try_subscribe)
            return
        hass.data["sextant_bermuda_listener_unsub"] = unsub
        # Deliberately NOT calling bermuda_updated() here. It ends in
        # async_add_entities, which must run on the event loop, and this
        # function can be reached from a non-loop context - doing so raised
        # "RuntimeError: loop ... is not the running loop" and left the
        # sensors uncreated. Bermuda's coordinator fires roughly once a
        # second, and that callback *is* on the loop, so the first discovery
        # pass happens a moment later through the normal path.

    _try_subscribe()


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """If using configuration in configuration.yaml"""
    await async_setup_entry(hass, config, async_add_entities)
