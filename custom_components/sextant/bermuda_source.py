"""
Read Bermuda's per-scanner distances without enabling its entities.

Sextant historically sourced its thing<->receiver distances by enumerating
``sensor.<device>_distance_to_<scanner>`` from the state machine. Bermuda
creates those entities *disabled by default* precisely because there is one per
(tracked device x scanner) pair: on a 60-proxy install that is thousands of
entities, each writing to the recorder and fanning ``state_changed`` out to
every websocket client - to surface data Bermuda already holds in memory.

Bermuda exposes that data directly via ``custom_components.bermuda.api``
(SNAPSHOT_VERSION 1). This module adapts it to the shape Sextant already speaks -
keyed by device prefix and scanner slug that Sextant floorplans are stored
against - so the rest of Sextant is unchanged and existing saved configs keep
working.

Key detail: the device/scanner slug map is resolved from the live snapshot,
not the entity registry. An earlier version of this module read it from the
registry instead, on the theory that Bermuda's frozen, once-assigned
``_distance_to_<slug>`` entity_ids were a more stable join key than a slug
recomputed from the current name. That reasoning stops applying the moment a
user turns off `create_scanner_entities` (Bermuda's own opt-out for exactly
this entity-count cost) - with no entities being created, there is nothing in
the registry to join against, disabled or not, and every registry-based
lookup here silently returned nothing. Reading current slugs directly from
the snapshot is the only join source that actually works in that
configuration, so it is the only one this module uses now.

If Bermuda is missing, too old to have the API, or the snapshot version is
unknown, every helper here returns None and callers fall back to the original
entity-scraping path.
"""

from __future__ import annotations

import logging
import time

_LOGGER = logging.getLogger(__name__)

# The positioning loop calls in once per tracked device per cycle, and building
# a snapshot walks every device Bermuda knows about. Cache within a cycle so
# that is done once rather than N times. Bermuda's own coordinator updates about
# once a second, so a sub-second TTL costs no freshness.
_READINGS_TTL = 0.5
# The raw snapshot itself is shared by the readings, the slug map, the
# distance-pair listing and (on older Bermuda builds) the tracked-set check,
# several of which run in the same cycle. Build it once per cycle.
_SNAPSHOT_TTL = 0.5
# The slug map only changes when a device/scanner is newly seen, which is rare
# after the first few minutes of a boot.
_SLUG_MAP_TTL = 30.0
# The tracked-device set is asked for on EVERY Bermuda coordinator tick (~1 s,
# see sensor.py's bermuda_updated) purely to notice a device being added or
# removed. A few seconds of staleness there is invisible to the user; the
# ~1 Hz rebuild it replaced was the single largest recurring cost in the
# Bermuda/Sextant pairing.
_TRACKED_TTL = 5.0

_CACHE_KEY = "sextant_bermuda_source_cache"
_EMPTY_CACHE = {
    "readings_at": 0.0, "readings": None, "readings_history": False,
    "slug_map_at": 0.0, "slug_map": None,
    "snapshot_at": 0.0, "snapshot": None, "snapshot_history": False,
    "tracked_at": 0.0, "tracked": None,
    "readings_addr_at": 0.0, "readings_addr": None, "readings_addr_history": False,
}


def _cache_for(hass) -> dict | None:
    """
    Per-hass cache bucket, or None when caching is not possible.

    Deliberately held in ``hass.data`` rather than a module global: a module
    global is shared by every hass in the process, which silently leaks state
    between them (and between tests, where it produced stale readings because a
    whole suite runs well inside the TTL).
    """
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return None
    cache = data.setdefault(_CACHE_KEY, dict(_EMPTY_CACHE))
    # A bucket created by an older build of this module lacks the newer keys.
    for key, value in _EMPTY_CACHE.items():
        cache.setdefault(key, value)
    return cache


def async_invalidate_cache(hass) -> None:
    """Drop cached lookups (call after entities are added or removed)."""
    cache = _cache_for(hass)
    if cache is not None:
        cache.update(_EMPTY_CACHE)

_DISTANCE_TO = "_distance_to_"

# Snapshot shapes this module understands. Bermuda bumps its SNAPSHOT_VERSION
# on an incompatible change; anything outside this set falls back rather than
# silently misreading.
_SUPPORTED_SNAPSHOT_VERSIONS = frozenset({1})


def _bermuda_api():
    """The Bermuda api module, or None when unavailable."""
    try:
        from custom_components.bermuda import api  # noqa: PLC0415
    except ImportError:
        return None
    # Older Bermuda builds may ship without the snapshot helpers.
    if not hasattr(api, "async_get_advert_snapshot"):
        return None
    return api


def _features(api) -> frozenset:
    """
    The additive snapshot features this Bermuda build advertises.

    Bermuda layers optional capabilities (a tracked-only snapshot, a cheap
    tracked-device accessor, a scanner liveness map) on SNAPSHOT_VERSION 1 and
    names them in ``SNAPSHOT_FEATURES``. A build without the attribute is a
    plain v1 build and gets the original, heavier code paths.
    """
    features = getattr(api, "SNAPSHOT_FEATURES", None)
    try:
        return frozenset(features or ())
    except TypeError:
        return frozenset()


def async_api_available(hass) -> bool:
    """Whether the direct Bermuda API can be used right now."""
    api = _bermuda_api()
    if api is None:
        return False
    return api.async_get_coordinator(hass) is not None


def async_subscribe(hass, callback_) -> object | None:
    """
    Subscribe to Bermuda's update cycle.

    Bermuda's coordinator is a normal DataUpdateCoordinator, so this is the
    supported push path - no bespoke event, nothing on the websocket. Returns an
    unsubscribe callable, or None when unavailable.
    """
    api = _bermuda_api()
    if api is None:
        return None
    coordinator = api.async_get_coordinator(hass)
    if coordinator is None:
        return None
    return coordinator.async_add_listener(callback_)


def async_get_snapshot_distance_pairs(hass) -> list[str] | None:
    """
    Synthetic ``sensor.<device>_distance_to_<scanner>`` id strings for every
    (tracked device, scanner) pair Bermuda has an advert for RIGHT NOW.

    These are not real entity_ids - nothing is registered or looked up by
    them - they exist only so the receiver picker and the receiver/beacon
    debug views, which parse ids of this shape into (device, scanner) pairs,
    keep working with `create_scanner_entities=False` and zero matching
    entities in the registry. Built fresh from the live snapshot every call,
    so unlike a registry-derived id list this reflects exactly what Bermuda
    currently reports - a device that stops being heard by a scanner drops
    out immediately rather than lingering as a stale registry row.

    Returns None when the Bermuda API is unavailable, so callers fall back.
    """
    snapshot = _snapshot(hass)
    if snapshot is None:
        return None
    ids: list[str] = []
    for device in snapshot["devices"].values():
        if not device.get("tracked"):
            continue
        device_slug = device.get("slug") or ""
        if not device_slug:
            continue
        for scanner in device["scanners"].values():
            scanner_slug = scanner.get("slug") or ""
            if scanner_slug:
                ids.append(f"sensor.{device_slug}{_DISTANCE_TO}{scanner_slug}")
    return ids


def async_get_tracked_device_prefixes(hass) -> set[str] | None:
    """
    Current slugs of the devices Bermuda is configured to TRACK.

    Each tracked device contributes exactly its own CURRENT slug. Unlike a
    registry-based join over frozen, one-per-rename entity_ids, a renamed
    device can never appear under more than one prefix at once, because there
    is only one live slug to read - no history to accumulate duplicates from.
    This is also the only source available once entity creation is switched
    off (create_scanner_entities), since there is then nothing in the registry
    to join against at all.

    Filtered to devices Bermuda currently reports as tracked, so a device the
    user has since removed from Bermuda's config stops being tracked here
    within _TRACKED_TTL seconds.

    On a Bermuda build that advertises the ``tracked_devices`` feature this
    reads the cheap tracked-set accessor (one attribute per known device, no
    advert walk). Older builds fall back to filtering a snapshot. Either way
    the answer is cached for _TRACKED_TTL, because the only caller that needs
    it more often than once a cycle is a ~1 Hz change detector.

    Returns None when the Bermuda API is unavailable, so callers fall back.
    """
    cache = _cache_for(hass)
    now = time.monotonic()
    if cache is not None and cache["tracked"] is not None and now - cache["tracked_at"] <= _TRACKED_TTL:
        return cache["tracked"]

    api = _bermuda_api()
    if api is None:
        return None
    if "tracked_devices" in _features(api):
        tracked = api.async_get_tracked_devices(hass)
        if tracked is None:
            return None
        prefixes = {device["slug"] for device in tracked.values() if device.get("slug")}
    else:
        snapshot = _snapshot(hass)
        if snapshot is None:
            return None
        prefixes = {
            device["slug"]
            for device in snapshot["devices"].values()
            if device.get("tracked") and device.get("slug")
        }
    if cache is not None:
        cache["tracked"] = prefixes
        cache["tracked_at"] = now
    return prefixes


def async_get_scanner_ages(hass) -> dict[str, float] | None:
    """
    Seconds since each scanner last relayed ANY advert, keyed by scanner slug.

    This is the proximity-independent liveness signal the receiver-status
    poller needs: a proxy that is up ages fresh even when no tracked device is
    anywhere near it, because the probes hear each other's iBeacons. It used
    to be read by calling the ``bermuda.dump_devices`` service every poll,
    which serialises every scanner's whole advert table just to reach one
    ``last_seen`` per scanner. Bermuda's ``scanners`` feature exposes exactly
    that field from the scanner set, with no advert walk and no JSON dump.

    A scanner Bermuda has never heard relay anything reads as ``inf`` (never
    seen) rather than 0 (seen just now).

    Returns None when the Bermuda API is unavailable or predates the
    ``scanners`` feature, so callers fall back to the service call.
    """
    api = _bermuda_api()
    if api is None or "scanners" not in _features(api):
        return None
    scanners = api.async_get_scanners(hass)
    if scanners is None:
        return None
    ages: dict[str, float] = {}
    for scanner in scanners.values():
        slug = scanner.get("slug")
        if not slug:
            continue
        age = scanner.get("last_seen_age")
        ages[slug] = float(age) if isinstance(age, (int, float)) else float("inf")
    return ages


def _snapshot(hass, include_history=False):
    """
    A version-checked snapshot, or None.

    Every consumer in this module only ever reads TRACKED devices (the slug
    map, the readings and the distance-pair listing all filter on
    ``tracked``), so on a Bermuda build that supports it the snapshot is
    requested tracked-only: Bermuda then skips the untracked majority before
    doing any per-advert work, instead of serialising every device in range
    for this module to discard. Cached for _SNAPSHOT_TTL so the several
    callers within one positioning cycle share a single build.

    ``include_history`` asks for each advert's recent raw RSSI samples (the
    ``rssi_history`` feature; silently absent on older builds). A cached
    snapshot without history is rebuilt when history is wanted; one with
    history serves either request.
    """
    cache = _cache_for(hass)
    now = time.monotonic()
    if (
        cache is not None
        and cache["snapshot"] is not None
        and now - cache["snapshot_at"] <= _SNAPSHOT_TTL
        and (cache.get("snapshot_history") or not include_history)
    ):
        return cache["snapshot"]

    api = _bermuda_api()
    if api is None:
        return None
    features = _features(api)
    kwargs = {}
    if "tracked_only" in features:
        kwargs["tracked_only"] = True
    if include_history and "rssi_history" in features:
        kwargs["include_history"] = True
    snapshot = api.async_get_advert_snapshot(hass, **kwargs)
    if snapshot is None:
        return None
    if snapshot.get("version") not in _SUPPORTED_SNAPSHOT_VERSIONS:
        _LOGGER.warning(
            "Bermuda advert snapshot version %s is not supported by this build of Sextant "
            "(understands %s); falling back to reading distance entities",
            snapshot.get("version"),
            sorted(_SUPPORTED_SNAPSHOT_VERSIONS),
        )
        return None
    if cache is not None:
        cache["snapshot"] = snapshot
        cache["snapshot_at"] = now
        cache["snapshot_history"] = bool(kwargs.get("include_history"))
    return snapshot


def _slug_map(hass) -> dict[tuple[str, str], tuple[str, str]]:
    """Cached `async_build_slug_map`."""
    cache = _cache_for(hass)
    if cache is None:
        return async_build_slug_map(hass)
    now = time.monotonic()
    if cache["slug_map"] is None or now - cache["slug_map_at"] > _SLUG_MAP_TTL:
        cache["slug_map"] = async_build_slug_map(hass)
        cache["slug_map_at"] = now
    return cache["slug_map"]


def async_build_slug_map(hass) -> dict[tuple[str, str], tuple[str, str]]:
    """
    Map ``(device_prefix, scanner_slug) -> (device_uid, scanner_uid)``.

    Built entirely from Bermuda's live API snapshot - no entity registry
    involved. This is the only join source available once entity creation is
    switched off (`create_scanner_entities=False`): there is then nothing in
    the registry to read at all, disabled or not.

    device_prefix is the tracked device's CURRENT slug. scanner_slug is
    resolved GLOBALLY across every device's adverts, not just the one
    device_prefix being mapped: a newly-tracked device (a replaced thing
    collar, say) may only have been HEARD by a handful of scanners so far,
    while a long-tracked device has been heard by nearly all of them. Since a
    scanner's slug depends only on its own name, borrowing it from whichever
    device's adverts happened to include that scanner first gives every
    tracked device the same scanner coverage instead of only its own -
    without needing an entity to exist at all.

    Because this recomputes slugs from CURRENT names on every call rather
    than reading a frozen, once-assigned entity_id, a rename takes effect
    immediately instead of leaving a stale key behind. That is the correct
    tradeoff here (not merely an accepted one): with no entities being
    created, there is no frozen historical id to prefer over the live name in
    the first place.
    """
    snapshot = _snapshot(hass)
    if snapshot is None:
        return {}

    device_prefix_to_uid: dict[str, str] = {}
    scanner_slug_to_uid: dict[str, str] = {}
    for address, device in snapshot["devices"].items():
        if not device.get("tracked"):
            continue
        slug = device.get("slug") or ""
        if slug:
            device_prefix_to_uid.setdefault(slug, device.get("unique_id") or address)
        for scanner in device["scanners"].values():
            scanner_slug = scanner.get("slug") or ""
            scanner_uid = scanner.get("unique_id") or scanner.get("address_wifi_mac") or scanner.get("address")
            if scanner_slug and scanner_uid:
                scanner_slug_to_uid.setdefault(scanner_slug, scanner_uid)

    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    for device_prefix, device_uid in device_prefix_to_uid.items():
        for scanner_slug, scanner_uid in scanner_slug_to_uid.items():
            mapping[(device_prefix, scanner_slug)] = (device_uid, scanner_uid)
    return mapping


def _index_snapshot(snapshot):
    """
    Index a snapshot for lookup by the ids the entity registry uses.

    Bermuda keys its per-scanner entity unique_ids on
    ``address_wifi_mac or address``, so a scanner is registered under every id
    it is known by; likewise devices under address and unique_id.
    """
    devices: dict[str, dict] = {}
    for address, device in snapshot["devices"].items():
        scanners: dict[str, dict] = {}
        for scanner_address, scanner in device["scanners"].items():
            for key in (
                scanner_address,
                scanner.get("address_wifi_mac"),
                scanner.get("unique_id"),
            ):
                if key:
                    scanners.setdefault(key.lower(), scanner)
        for key in (address, device.get("unique_id")):
            if key:
                devices.setdefault(key.lower(), scanners)
    return devices


def async_get_readings(hass, include_history=False) -> dict[tuple[str, str], dict] | None:
    """
    Current distances keyed by ``(device_prefix, scanner_slug)``.

    Each value is ``{"distance": metres|None, "age": seconds|None}`` plus,
    when the Bermuda build provides them, the path-loss parameters it applied
    (``ref_power``, ``attenuation``, ``rssi_offset``) and - only with
    ``include_history`` - ``history``: the recent raw ``[rssi, stamp]`` samples,
    newest first. Those let a caller run its own estimator on Bermuda's scale.

    ``distance`` is metres always - unlike the entities, which render feet or
    metres per the user's unit settings and which Sextant therefore had to convert.
    ``age`` is seconds since that scanner last actually *heard* the device,
    which is a stronger stale-reading signal than an entity's ``last_updated``
    (that only moves when the value changes, so a frozen reading looked fresh).

    Returns None when Bermuda or its API is unavailable, so the caller can fall
    back to reading entities.
    """
    cache = _cache_for(hass)
    now = time.monotonic()
    if (
        cache is not None
        and cache["readings"] is not None
        and now - cache["readings_at"] <= _READINGS_TTL
        and (cache.get("readings_history") or not include_history)
    ):
        return cache["readings"]

    snapshot = _snapshot(hass, include_history=include_history)
    if snapshot is None:
        return None

    indexed = _index_snapshot(snapshot)
    readings: dict[tuple[str, str], dict] = {}
    for (device_prefix, scanner_slug), (device_uid, scanner_uid) in _slug_map(hass).items():
        scanners = indexed.get(device_uid.lower())
        if scanners is None:
            continue
        scanner = scanners.get(scanner_uid.lower())
        if scanner is None:
            continue
        reading = {
            "distance": scanner.get("distance"),
            "age": scanner.get("age"),
        }
        for key in ("ref_power", "attenuation", "rssi_offset"):
            if key in scanner:
                reading[key] = scanner[key]
        if include_history and "history" in scanner:
            reading["history"] = scanner["history"]
        readings[(device_prefix, scanner_slug)] = reading
    if cache is not None:
        cache["readings"] = readings
        cache["readings_at"] = now
        cache["readings_history"] = bool(include_history)
    return readings


# --- scanner-to-scanner ranging (fingerprint references) ----------------------


def async_get_scanner_ranging(hass, max_age=None) -> dict | None:
    """
    How every scanner hears every other scanner's own advert (see fingerprint.py).

    Needs a Bermuda build with the ``scanner_ranging`` feature; None otherwise
    (and when Bermuda is absent), so the caller can leave fingerprinting off.
    Not cached: it is fetched on its own slow cadence, not per cycle.
    """
    api = _bermuda_api()
    if api is None or "scanner_ranging" not in _features(api):
        return None
    kwargs = {} if max_age is None else {"max_age": max_age}
    return api.async_get_scanner_ranging(hass, **kwargs)


# --- device management (feature "device_management") ----------------------------


def async_features(hass) -> frozenset:
    """The feature names this Bermuda build advertises (empty when absent)."""
    api = _bermuda_api()
    return _features(api) if api is not None else frozenset()


def _mgmt_api():
    api = _bermuda_api()
    if api is None or "device_management" not in _features(api):
        return None
    return api


def async_get_device_candidates(hass, max_age=None) -> list | None:
    api = _mgmt_api()
    if api is None:
        return None
    kwargs = {} if max_age is None else {"max_age": max_age}
    return api.async_get_device_candidates(hass, **kwargs)


def async_get_tracked_devices(hass) -> dict | None:
    api = _bermuda_api()
    if api is None or "tracked_devices" not in _features(api):
        return None
    return api.async_get_tracked_devices(hass)


async def async_set_tracked_devices(hass, add=(), remove=()) -> list | None:
    api = _mgmt_api()
    if api is None:
        return None
    result = await api.async_set_tracked_devices(hass, add=add, remove=remove)
    async_invalidate_cache(hass)
    return result


def async_get_findmy_accessories(hass) -> list | None:
    api = _mgmt_api()
    return None if api is None else api.async_get_findmy_accessories(hass)


async def async_add_findmy_accessory(hass, accessory_json, name=None) -> dict | None:
    api = _mgmt_api()
    return None if api is None else await api.async_add_findmy_accessory(hass, accessory_json, name)


async def async_remove_findmy_accessory(hass, address) -> bool | None:
    api = _mgmt_api()
    return None if api is None else await api.async_remove_findmy_accessory(hass, address)


def async_get_options(hass) -> dict | None:
    api = _mgmt_api()
    return None if api is None else api.async_get_options(hass)


async def async_set_options(hass, changes) -> dict | None:
    api = _mgmt_api()
    return None if api is None else await api.async_set_options(hass, changes)


def async_get_tile_diagnostics(hass) -> dict | None:
    """The Tile manager's diagnostics (bindings, learned ids, probes), or None."""
    api = _bermuda_api()
    if api is None:
        return None
    coordinator = api.async_get_coordinator(hass)
    manager = getattr(coordinator, "tile_manager", None)
    if manager is None:
        return None
    return manager.diagnostics()


# --- rssi offsets (calibration_target = "bermuda") ---------------------------


def async_get_rssi_offsets(hass) -> dict | None:
    """
    Bermuda's per-scanner rssi offsets plus its global path-loss parameters:
    ``{"offsets": {address: dB}, "attenuation": n, "ref_power": p}``.

    None when the Bermuda build lacks the ``rssi_offsets`` feature.
    """
    api = _bermuda_api()
    if api is None or "rssi_offsets" not in _features(api):
        return None
    return api.async_get_rssi_offsets(hass)


def async_set_rssi_offsets(hass, offsets: dict) -> dict | None:
    """
    Merge per-scanner rssi offsets (dB, keyed by scanner address) into
    Bermuda, applied live and persisted without a reload. Returns the
    resulting full map, or None when unsupported.
    """
    api = _bermuda_api()
    if api is None or "rssi_offsets" not in _features(api):
        return None
    return api.async_set_rssi_offsets(hass, offsets)


def async_get_scanner_addresses_by_slug(hass) -> dict[str, str] | None:
    """``{scanner slug: scanner address}`` from Bermuda's scanner map, or None."""
    api = _bermuda_api()
    if api is None or "scanners" not in _features(api):
        return None
    scanners = api.async_get_scanners(hass)
    if scanners is None:
        return None
    return {
        scanner["slug"]: address
        for address, scanner in scanners.items()
        if scanner.get("slug") and address
    }


# --- receivers identified by scanner ADDRESS --------------------------------


def async_get_scanner_directory(hass) -> dict[str, dict] | None:
    """
    ``{scanner address: {"slug", "name", "unique_id", "address_wifi_mac",
    "last_seen_age"}}`` for every scanner Bermuda knows (the ``scanners``
    feature), lower-cased addresses. None when unsupported.

    This is the join table for placements keyed by address: a placement's
    label (its slug) can follow a rename, its identity cannot.
    """
    api = _bermuda_api()
    if api is None or "scanners" not in _features(api):
        return None
    scanners = api.async_get_scanners(hass)
    if scanners is None:
        return None
    out = {}
    for address, scanner in scanners.items():
        if not address:
            continue
        out[str(address).lower()] = {
            "slug": scanner.get("slug") or "",
            "name": scanner.get("name") or "",
            "unique_id": (scanner.get("unique_id") or "") or None,
            "address_wifi_mac": (scanner.get("address_wifi_mac") or "") or None,
            "last_seen_age": scanner.get("last_seen_age"),
        }
    return out


def async_get_readings_by_address(hass, include_history=False) -> dict[tuple[str, str], dict] | None:
    """
    Current distances keyed by ``(device_prefix, scanner_address)``.

    Same values as ``async_get_readings``, built straight from the tracked
    devices' adverts with no slug map at all: the scanner address IS the key
    Bermuda stores the advert under, so nothing here can drift when a scanner
    is renamed. Returns None when Bermuda's API is unavailable.
    """
    cache = _cache_for(hass)
    now = time.monotonic()
    if (
        cache is not None
        and cache.get("readings_addr") is not None
        and now - cache.get("readings_addr_at", 0.0) <= _READINGS_TTL
        and (cache.get("readings_addr_history") or not include_history)
    ):
        return cache["readings_addr"]

    snapshot = _snapshot(hass, include_history=include_history)
    if snapshot is None:
        return None
    readings: dict[tuple[str, str], dict] = {}
    for device in snapshot["devices"].values():
        if not device.get("tracked"):
            continue
        prefix = device.get("slug") or ""
        if not prefix:
            continue
        for address, scanner in device["scanners"].items():
            if not address:
                continue
            reading = {"distance": scanner.get("distance"), "age": scanner.get("age")}
            for key in ("ref_power", "attenuation", "rssi_offset"):
                if key in scanner:
                    reading[key] = scanner[key]
            if include_history and "history" in scanner:
                reading["history"] = scanner["history"]
            readings[(prefix, str(address).lower())] = reading
    if cache is not None:
        cache["readings_addr"] = readings
        cache["readings_addr_at"] = now
        cache["readings_addr_history"] = bool(include_history)
    return readings


# --- Tile identity (bind a configured Tile to the ID read from the tag) ------


def async_get_tile_identities(hass) -> dict | None:
    """Every Tile ID Bermuda has read and where that Tile is now, or None
    when this Bermuda build cannot (feature "tile_identity")."""
    api = _bermuda_api()
    if api is None or "tile_identity" not in _features(api):
        return None
    return api.async_get_tile_identities(hass)


async def async_bind_tile(hass, tile_id, uid) -> dict | None:
    """Declare that configured Tile ``tile_id`` is the tag with Tile ID ``uid``."""
    api = _bermuda_api()
    if api is None or "tile_identity" not in _features(api):
        return None
    result = await api.async_bind_tile(hass, tile_id, uid)
    async_invalidate_cache(hass)
    return result


async def async_bind_tile_address(hass, tile_id, address) -> dict | None:
    """Declare that configured Tile ``tile_id`` is the tag at ``address`` now."""
    api = _bermuda_api()
    if api is None or not hasattr(api, "async_bind_tile_address"):
        return None
    result = await api.async_bind_tile_address(hass, tile_id, address)
    async_invalidate_cache(hass)
    return result
