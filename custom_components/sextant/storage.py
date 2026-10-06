"""Persistent storage for Sextant layout + calibration state.

The floor/zone/receiver layout and the calibration state used to live as flat
files in ``www/sextant_maps`` (``bpsdata.txt`` / ``sextant_calibration_state.json``),
written with ``open(path, "w")`` — which truncates to zero *before* writing, so
an interrupted write left a 0-byte file and wiped the config (issue #104).
Being under ``www/`` also made them readable unauthenticated via ``/local/``.

Both now live in Home Assistant's ``Store`` (``config/.storage/``):
``Store.async_save`` writes atomically (temp file + ``os.replace`` — the live
file is never truncated), and ``.storage`` is not web-served. This module is
the single owner of that data and imports nothing from the rest of the package
(so both ``__init__`` and ``calibration`` can import it without a cycle).
"""
import asyncio
import copy
import json
import logging
from pathlib import Path

import os
import shutil

import aiofiles
import aiofiles.os
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

DOMAIN = "sextant"
STORAGE_VERSION = 1
STORAGE_KEY_LAYOUT = "sextant"                       # -> config/.storage/sextant
STORAGE_KEY_CALIB = "sextant_calibration_state"      # -> config/.storage/sextant_calibration_state
STORAGE_KEY_KPI = "sextant_kpi_baselines"            # -> config/.storage/sextant_kpi_baselines
STORAGE_KEY_TRUTH = "sextant_truth"                  # -> config/.storage/sextant_truth (marks + their samples)
STORAGE_KEY_FP_GAINS = "sextant_fingerprint_gains"   # -> the learned reference gains, so a restart starts warm
STORAGE_KEY_RUNTIME = "sextant_runtime"              # -> each thing's elections and filter, to survive a restart

# Serializes read-modify-write sequences on the layout across every writer
# (panel save + calibration). Lives here so both importers share one lock
# without an import cycle (was previously defined in calibration.py).
LAYOUT_LOCK = asyncio.Lock()

# The integration was called "bps" before it became Sextant. Its Store keys,
# history directory and maps directory all carried that name.
LEGACY_DOMAIN = "bps"


def maps_dir(hass) -> str:
    """Where the floor-plan images live: ``config/sextant_maps``.

    Not under ``www/``: everything there is served unauthenticated at
    ``/local/``, and a house's floor plans are not for whoever can reach the
    port. The panel and the card fetch them through the authenticated
    ``/api/sextant/map/<file>`` view instead.
    """
    return hass.config.path("sextant_maps")


def legacy_www_maps_dir(hass) -> str:
    """Where the images lived up to 3.11.10 (publicly served)."""
    return hass.config.path("www/sextant_maps")


_MAP_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".jfif", ".jpe", ".gif", ".webp", ".bmp", ".svg", ".avif")


async def migrate_maps_out_of_www(hass) -> int:
    """Move floor-plan images from ``www/sextant_maps`` to ``sextant_maps``, once.

    Runs every boot (a backup restore can bring the old folder back). Files
    already present in the new folder are left alone and the old copy
    removed; the old folder goes when it is empty. Returns how many moved.
    """
    old, new = legacy_www_maps_dir(hass), maps_dir(hass)

    def _move() -> int:
        if not os.path.isdir(old):
            return 0
        os.makedirs(new, exist_ok=True)
        moved = 0
        for name in os.listdir(old):
            src = os.path.join(old, name)
            if not os.path.isfile(src) or not name.lower().endswith(_MAP_IMAGE_EXTS):
                continue
            dst = os.path.join(new, name)
            if os.path.exists(dst):
                os.remove(src)
            else:
                shutil.move(src, dst)
                moved += 1
        if not os.listdir(old):
            os.rmdir(old)
        return moved

    try:
        moved = await hass.async_add_executor_job(_move)
    except Exception as e:
        _LOGGER.warning("Could not move floor-plan images out of www/: %s", e)
        return 0
    if moved:
        _LOGGER.info("Moved %d floor-plan image(s) from www/sextant_maps to sextant_maps (no longer public)", moved)
    return moved


def _legacy_layout_path(hass) -> Path:
    return Path(hass.config.path("www/sextant_maps")) / "bpsdata.txt"


def _legacy_calib_path(hass) -> Path:
    return Path(hass.config.path("www/sextant_maps")) / "sextant_calibration_state.json"


def _bucket(hass) -> dict:
    return hass.data.setdefault(DOMAIN, {})


def _layout_store(hass) -> Store:
    bucket = _bucket(hass)
    store = bucket.get("_layout_store")
    if store is None:
        store = bucket["_layout_store"] = Store(hass, STORAGE_VERSION, STORAGE_KEY_LAYOUT)
    return store


def _calib_store(hass) -> Store:
    bucket = _bucket(hass)
    store = bucket.get("_calib_store")
    if store is None:
        store = bucket["_calib_store"] = Store(hass, STORAGE_VERSION, STORAGE_KEY_CALIB)
    return store


# --- Layout (bpsdata) --------------------------------------------------------

def get_layout(hass):
    """The cached layout for READ-ONLY consumers.

    Defaults to ``[]`` on a fresh install, matching the old empty-file
    behaviour (a populated layout is a dict; consumers guard with isinstance).
    """
    return _bucket(hass).get("layout", [])


def get_layout_version(hass) -> int:
    """Monotonic counter bumped every time the layout is (re)loaded or saved.

    Derived per-floor data compiled from the layout (e.g. shapely zone/sub-zone
    polygons in ``__init__.py``, rebuilt from scratch on every lookup call
    otherwise) can cache against this instead: a cached entry is valid exactly
    as long as this counter hasn't moved, with no need to hash or diff the
    layout content itself.
    """
    return _bucket(hass).get("layout_version", 0)


def _bump_layout_version(hass) -> None:
    _bucket(hass)["layout_version"] = _bucket(hass).get("layout_version", 0) + 1


def get_layout_for_edit(hass):
    """A deep copy of the layout for read-modify-write callers (calibration).

    Returns ``None`` when there is no layout yet — matching the old
    ``_read_coords`` contract — so callers must never mutate the live cache
    the tracking loop reads.
    """
    data = get_layout(hass)
    return copy.deepcopy(data) if isinstance(data, dict) else None


# --- "tracker" became "thing" (3.12.0) --------------------------------------
# Four stores carry the old word in their keys. Each load path below renames
# what it finds, so an install that has been running since before the rename
# keeps its names, colours, icons, heights, learned gains, truth marks and KPI
# baselines. The layout is written back once; the rest are renamed in memory,
# since they are rewritten in the new shape on their next save anyway.
THING_KEY_RENAMES = {
    "tracker_names": "thing_names",
    "tracker_classes": "thing_classes",
    "tracker_colors": "thing_colors",
    "tracker_icons": "thing_icons",
    "tracker_heights": "thing_heights",
    "tracker_height": "thing_height",
    "tracker_estimators": "thing_estimators",
    "tracker_fp_weights": "thing_fp_weights",
    "tracker_fp_gains": "thing_fp_gains",
    "tracker_ref_offsets": "thing_ref_offsets",
    "tracker_gain": "thing_gain",
    "tracker_vec": "thing_vec",
    "changes_per_tracker_hour": "changes_per_thing_hour",
}


def rename_thing_keys(data, _renames=THING_KEY_RENAMES):
    """Every ``tracker_*`` key in `data`, at any depth, under its new name.

    Returns (data, renamed) - the same object, mutated in place, and how many
    keys moved, so a caller can decide whether the store is worth rewriting.
    """
    renamed = 0
    if isinstance(data, dict):
        for old, new in _renames.items():
            if old in data and new not in data:
                data[new] = data.pop(old)
                renamed += 1
        for value in data.values():
            _, n = rename_thing_keys(value)
            renamed += n
    elif isinstance(data, list):
        for item in data:
            _, n = rename_thing_keys(item)
            renamed += n
    return data, renamed


async def load_layout(hass):
    """Load the layout from the store into the in-memory cache (at setup).

    A corrupt/unreadable store must not abort setup — the old flat-file reader
    tolerated bad JSON and ran with an empty layout, so keep that resilience.
    """
    try:
        data = await _layout_store(hass).async_load()
    except Exception as e:
        _LOGGER.error("Could not load layout from storage; starting empty: %s", e)
        data = None
    data, renamed = rename_thing_keys(data)
    if renamed:
        _LOGGER.info("Renamed %d tracker_* layout key(s) to thing_*", renamed)
        try:
            await _layout_store(hass).async_save(data)
        except Exception as e:  # the in-memory rename still stands for this run
            _LOGGER.warning("Could not persist the thing_* layout keys: %s", e)
    _bucket(hass)["layout"] = data if data is not None else []
    _bump_layout_version(hass)
    return _bucket(hass)["layout"]


async def save_layout(hass, data) -> list:
    """Persist the layout dict atomically, THEN refresh the cache.

    Uses ``async_save`` (immediate atomic write), never ``async_delay_save`` —
    a debounced write could still be lost on a crash inside the delay window.
    Persist first so a raising write leaves the cache matching what survives a
    restart rather than running ahead of disk.

    The layout on its way out is snapshotted first, so any save can be undone
    — every writer goes through here, which makes this the one place that has
    both the old and the new layout in hand. Returns whatever geometry this
    save destroys (see ``snapshots.degraded``), empty in the normal case; the
    save still goes through, because refusing it would be guessing at what the
    person meant, and the snapshot means nothing is lost either way.
    """
    from .snapshots import degraded, take  # noqa: PLC0415 - keeps storage importable on its own

    before = get_layout(hass)
    losses = degraded(before, data)
    for line in losses:
        _LOGGER.warning("Layout save: %s", line)
    await take(hass, before, get_layout_version(hass))
    await _layout_store(hass).async_save(data)
    _bucket(hass)["layout"] = data
    _bump_layout_version(hass)
    return losses


# --- Calibration state -------------------------------------------------------

async def load_calib_state(hass):
    """The persisted calibration state, or None on first run / if unreadable."""
    try:
        return await _calib_store(hass).async_load()
    except Exception as e:
        _LOGGER.warning("Could not load calibration state; starting cold: %s", e)
        return None


async def save_calib_state(hass, payload) -> None:
    await _calib_store(hass).async_save(payload)


# --- Stability (KPI) baselines -----------------------------------------------

def _kpi_store(hass) -> Store:
    bucket = _bucket(hass)
    store = bucket.get("_kpi_store")
    if store is None:
        store = bucket["_kpi_store"] = Store(hass, STORAGE_VERSION, STORAGE_KEY_KPI)
    return store


async def load_kpi_baselines(hass) -> dict:
    """``{name: {"saved_at", "hours", "entities", "summary"}}``; empty when none."""
    try:
        data = await _kpi_store(hass).async_load()
    except Exception as e:
        _LOGGER.warning("Could not load KPI baselines: %s", e)
        return {}
    return rename_thing_keys(data)[0] if isinstance(data, dict) else {}


async def save_kpi_baselines(hass, baselines: dict) -> None:
    await _kpi_store(hass).async_save(baselines)


# --- Truth marks ------------------------------------------------------------

def _truth_store(hass) -> Store:
    bucket = _bucket(hass)
    store = bucket.get("_truth_store")
    if store is None:
        store = bucket["_truth_store"] = Store(hass, STORAGE_VERSION, STORAGE_KEY_TRUTH)
    return store


async def load_truth(hass) -> dict:
    """``{"marks": [...], "next_id": int}``; empty when none. Marks carry their samples,
    so they stay re-evaluable after a restart (see truth.py)."""
    try:
        data = await _truth_store(hass).async_load()
    except Exception as e:
        _LOGGER.warning("Could not load truth marks: %s", e)
        return {"marks": [], "next_id": 1}
    if not isinstance(data, dict):
        return {"marks": [], "next_id": 1}
    rename_thing_keys(data)
    data.setdefault("marks", [])
    data.setdefault("next_id", 1)
    return data


async def save_truth(hass, data: dict) -> None:
    await _truth_store(hass).async_save(data)


# --- Learned fingerprint gains ----------------------------------------------

def _fp_gain_store(hass) -> Store:
    bucket = _bucket(hass)
    store = bucket.get("_fp_gain_store")
    if store is None:
        store = bucket["_fp_gain_store"] = Store(hass, STORAGE_VERSION, STORAGE_KEY_FP_GAINS)
    return store


async def load_fp_gains(hass) -> dict:
    """``{"learned_gain": float, "thing_gain": {entity: float}}``; empty when none."""
    try:
        data = await _fp_gain_store(hass).async_load()
    except Exception as e:
        _LOGGER.warning("Could not load fingerprint gains: %s", e)
        return {}
    return rename_thing_keys(data)[0] if isinstance(data, dict) else {}


async def save_fp_gains(hass, data: dict) -> None:
    await _fp_gain_store(hass).async_save(data)


# --- Runtime state (see runtime.py) -----------------------------------------

def _runtime_store(hass) -> Store:
    bucket = _bucket(hass)
    store = bucket.get("_runtime_store")
    if store is None:
        store = bucket["_runtime_store"] = Store(hass, STORAGE_VERSION, STORAGE_KEY_RUNTIME)
    return store


async def load_runtime(hass) -> dict:
    """The last snapshot, or empty. A restart that cannot read it just starts cold."""
    try:
        data = await _runtime_store(hass).async_load()
    except Exception as e:
        _LOGGER.warning("Could not load the saved runtime state: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


async def save_runtime(hass, data: dict) -> None:
    await _runtime_store(hass).async_save(data)


# --- One-time migration of the old flat files -------------------------------

async def _read_legacy(path: Path):
    """Read a legacy file. Returns (content, existed)."""
    try:
        async with aiofiles.open(path, "r") as f:
            return await f.read(), True
    except FileNotFoundError:
        return None, False
    except OSError as e:
        _LOGGER.warning("Could not read legacy file %s: %s", path, e)
        return None, True  # exists but unreadable — don't delete it


async def _remove_legacy(path: Path) -> None:
    if not path.exists():
        return
    try:
        await aiofiles.os.remove(path)
        _LOGGER.info("Removed legacy file %s (data now lives in .storage)", path.name)
    except OSError as e:
        _LOGGER.error(
            "Could not remove legacy %s — it stays readable via /local until "
            "removed by hand: %s", path.name, e)


async def migrate_from_bps(hass) -> None:
    """Copy everything the pre-rename "bps" integration kept, once.

    Runs before ``migrate_legacy`` and only fills what is still empty, so a
    fresh install and an already-migrated one are both no-ops. Everything is
    copied rather than moved: rolling back to the old integration must still
    find its data where it left it.
    """
    for store, old_key, new_key, label in (
        (_layout_store(hass), LEGACY_DOMAIN, STORAGE_KEY_LAYOUT, "layout"),
        (_calib_store(hass), f"{LEGACY_DOMAIN}_calibration_state", STORAGE_KEY_CALIB, "calibration state"),
    ):
        try:
            if await store.async_load() is not None:
                continue
            old = await Store(hass, STORAGE_VERSION, old_key).async_load()
        except Exception as e:
            _LOGGER.warning("Could not read the %s while migrating from bps: %s", label, e)
            continue
        if old is None:
            continue
        await store.async_save(old)
        _LOGGER.info("Copied the %s from .storage/%s (bps) into .storage/%s", label, old_key, new_key)

    def _copy_dirs() -> list[str]:
        copied = []
        for old, new in (
            (hass.config.path(f"www/{LEGACY_DOMAIN}_maps"), maps_dir(hass)),
            (hass.config.path(".storage", f"{LEGACY_DOMAIN}_history"), hass.config.path(".storage", f"{DOMAIN}_history")),
        ):
            # Setup creates the (empty) target directories before this runs,
            # so "already there" means "has files", not "exists".
            if os.path.isdir(old) and not (os.path.isdir(new) and os.listdir(new)):
                shutil.copytree(old, new, dirs_exist_ok=True)
                copied.append(new)
        return copied

    try:
        for path in await hass.async_add_executor_job(_copy_dirs):
            _LOGGER.info("Copied bps data directory to %s", path)
    except Exception as e:
        _LOGGER.warning("Could not copy bps data directories: %s", e)


async def migrate_legacy(hass) -> None:
    """Move the old ``www/sextant_maps`` files into the store, once.

    Idempotent: only runs while the store is still empty. For each file:
    a blank/whitespace file (the issue-#104 artifact — no recoverable data) is
    deleted; a corrupt-but-non-empty file is LEFT in place (may be
    hand-recoverable); a valid file is saved to the store, verified, and only
    then deleted — closing the ``/local/`` exposure. Map images stay in
    ``www/sextant_maps`` (served via ``/local/``) and are untouched.
    """
    await _migrate_one(
        _layout_store(hass), _legacy_layout_path(hass), "layout",
        lambda parsed: isinstance(parsed, (dict, list)),
    )
    await _migrate_one(
        _calib_store(hass), _legacy_calib_path(hass), "calibration state",
        lambda parsed: isinstance(parsed, dict),
    )


async def _migrate_one(store: Store, legacy: Path, label: str, is_valid) -> None:
    try:
        existing = await store.async_load()
    except Exception as e:
        # Corrupt/unreadable store: don't migrate over it (may be recoverable)
        # and don't touch the legacy file — just don't crash setup.
        _LOGGER.warning("Could not read %s store; leaving legacy file: %s", label, e)
        return
    if existing is not None:
        # Store already owns the data. Retry the cleanup every boot so a
        # previously-failed delete (or a legacy file restored from a backup)
        # can't linger /local-exposed forever.
        await _remove_legacy(legacy)
        return
    content, existed = await _read_legacy(legacy)
    if not existed or content is None:
        return  # fresh install, or unreadable — leave it
    if not content.strip():
        # 0-byte / whitespace: the data-loss artifact, nothing to recover.
        await _remove_legacy(legacy)
        return
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        _LOGGER.warning(
            "Legacy %s file %s is not valid JSON; leaving it in place for "
            "manual recovery and starting empty", label, legacy.name)
        return
    if not is_valid(parsed):
        _LOGGER.warning("Legacy %s file %s has unexpected shape; leaving it", label, legacy.name)
        return
    await store.async_save(parsed)
    if await store.async_load() is None:
        _LOGGER.error("Migrating %s to storage failed to verify; keeping %s", label, legacy.name)
        return
    _LOGGER.info("Migrated %s from %s into .storage", label, legacy.name)
    await _remove_legacy(legacy)
