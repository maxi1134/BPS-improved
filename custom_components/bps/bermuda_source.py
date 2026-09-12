"""
Read Bermuda's per-scanner distances without enabling its entities.

BPS historically sourced its tracker<->receiver distances by enumerating
``sensor.<device>_distance_to_<scanner>`` from the state machine. Bermuda
creates those entities *disabled by default* precisely because there is one per
(tracked device x scanner) pair: on a 60-proxy install that is thousands of
entities, each writing to the recorder and fanning ``state_changed`` out to
every websocket client - to surface data Bermuda already holds in memory.

Bermuda exposes that data directly via ``custom_components.bermuda.api``
(SNAPSHOT_VERSION 1). This module adapts it to the shape BPS already speaks -
keyed by device prefix and scanner slug that BPS floorplans are stored
against - so the rest of BPS is unchanged and existing saved configs keep
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
# The slug map only changes when a device/scanner is newly seen, which is rare
# after the first few minutes of a boot.
_SLUG_MAP_TTL = 30.0

_CACHE_KEY = "bps_bermuda_source_cache"


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
    return data.setdefault(
        _CACHE_KEY,
        {"readings_at": 0.0, "readings": None, "slug_map_at": 0.0, "slug_map": None},
    )


def async_invalidate_cache(hass) -> None:
    """Drop cached lookups (call after entities are added or removed)."""
    cache = _cache_for(hass)
    if cache is not None:
        cache.update({"readings_at": 0.0, "readings": None, "slug_map_at": 0.0, "slug_map": None})

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

    Read directly from the live snapshot: each tracked device contributes
    exactly its own CURRENT slug. Unlike a registry-based join over frozen,
    one-per-rename entity_ids, a renamed device can never appear under more
    than one prefix at once, because there is only one live slug to read -
    no history to accumulate duplicates from. This is also the only source
    available once entity creation is switched off (create_scanner_entities),
    since there is then nothing in the registry to join against at all.

    Filtered to devices Bermuda currently reports as tracked, so a device the
    user has since removed from Bermuda's config stops being tracked here
    immediately.

    Returns None when the Bermuda API is unavailable, so callers fall back.
    """
    snapshot = _snapshot(hass)
    if snapshot is None:
        return None
    return {
        device["slug"]
        for device in snapshot["devices"].values()
        if device.get("tracked") and device.get("slug")
    }


def _snapshot(hass):
    """A version-checked snapshot, or None."""
    api = _bermuda_api()
    if api is None:
        return None
    snapshot = api.async_get_advert_snapshot(hass)
    if snapshot is None:
        return None
    if snapshot.get("version") not in _SUPPORTED_SNAPSHOT_VERSIONS:
        _LOGGER.warning(
            "Bermuda advert snapshot version %s is not supported by this build of BPS "
            "(understands %s); falling back to reading distance entities",
            snapshot.get("version"),
            sorted(_SUPPORTED_SNAPSHOT_VERSIONS),
        )
        return None
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
    device_prefix being mapped: a newly-tracked device (a replaced tracker
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


def async_get_readings(hass) -> dict[tuple[str, str], dict] | None:
    """
    Current distances keyed by ``(device_prefix, scanner_slug)``.

    Each value is ``{"distance": metres|None, "age": seconds|None}``.

    ``distance`` is metres always - unlike the entities, which render feet or
    metres per the user's unit settings and which BPS therefore had to convert.
    ``age`` is seconds since that scanner last actually *heard* the device,
    which is a stronger stale-reading signal than an entity's ``last_updated``
    (that only moves when the value changes, so a frozen reading looked fresh).

    Returns None when Bermuda or its API is unavailable, so the caller can fall
    back to reading entities.
    """
    cache = _cache_for(hass)
    now = time.monotonic()
    if cache is not None and cache["readings"] is not None and now - cache["readings_at"] <= _READINGS_TTL:
        return cache["readings"]

    snapshot = _snapshot(hass)
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
        readings[(device_prefix, scanner_slug)] = {
            "distance": scanner.get("distance"),
            "age": scanner.get("age"),
        }
    if cache is not None:
        cache["readings"] = readings
        cache["readings_at"] = now
    return readings
