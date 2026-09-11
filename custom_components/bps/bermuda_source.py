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
keyed by the entity-derived device prefix and scanner slug that BPS floorplans
are stored against - so the rest of BPS is unchanged and existing saved configs
keep working.

Key detail: the slug->scanner mapping is resolved from the ENTITY REGISTRY, not
from the snapshot. Bermuda's ``_distance_to_<slug>`` entity_ids are frozen when
the entity is first created and do not follow later scanner renames, so a slug
recomputed from the current name can differ from the stored one. Registry
entries survive with the entities disabled, and Bermuda's unique_id embeds the
scanner MAC, which gives an exact join.

If Bermuda is missing, too old to have the API, or the snapshot version is
unknown, every helper here returns None and callers fall back to the original
entity-scraping path.
"""

from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

BERMUDA_DOMAIN = "bermuda"
_DISTANCE_TO = "_distance_to_"
_RANGE_SUFFIX = "_range"

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


def async_build_slug_map(hass) -> dict[tuple[str, str], tuple[str, str]]:
    """
    Map ``(device_prefix, scanner_slug) -> (device_uid, scanner_uid)``.

    Both halves of the key are exactly what BPS already stores: the entity
    object_id either side of ``_distance_to_``. Built from the entity registry
    so it is unaffected by the entities being disabled.

    Only the filtered range entities are considered - Bermuda's unfiltered
    variants end ``_range_raw`` and have no timeout of their own.
    """
    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    ent_reg = er.async_get(hass)
    for entry in ent_reg.entities.values():
        if entry.platform != BERMUDA_DOMAIN:
            continue
        unique_id = entry.unique_id or ""
        # "_range_raw" does not end with "_range", so this excludes the
        # unfiltered twins without a second check.
        if not unique_id.endswith(_RANGE_SUFFIX):
            continue
        object_id = entry.entity_id.partition(".")[2]
        if _DISTANCE_TO not in object_id:
            continue
        device_prefix, _, scanner_slug = object_id.partition(_DISTANCE_TO)
        # rsplit from the right: an iBeacon metadevice address contains
        # underscores, a scanner MAC and the "range" tail do not.
        parts = unique_id.rsplit("_", 2)
        if len(parts) != 3:
            continue
        device_uid, scanner_uid, _tail = parts
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

    indexed = _index_snapshot(snapshot)
    readings: dict[tuple[str, str], dict] = {}
    for (device_prefix, scanner_slug), (device_uid, scanner_uid) in async_build_slug_map(hass).items():
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
    return readings
