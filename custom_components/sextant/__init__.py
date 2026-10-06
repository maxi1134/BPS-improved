import aiofiles
import aiofiles.os
import time
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.frontend import async_remove_panel
from homeassistant.components import panel_custom
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_HOMEASSISTANT_STOP, UnitOfLength
from homeassistant.util.unit_conversion import DistanceConverter
from homeassistant.util import slugify
import numpy as np
from .solver_numpy import least_squares_bounded_soft_l1
import voluptuous as vol
from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
import logging
import asyncio
from datetime import datetime, timezone
import math
import os
import json
import re
import difflib
import shapely
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points, unary_union
try:
    from shapely.validation import make_valid as shapely_make_valid
except ImportError:  # very old shapely
    shapely_make_valid = None
from asyncio import Lock, Queue, wait_for, TimeoutError

from .calibration import (
    apply_corrections,
    async_cancel_calibration,
    async_restore_calibration_state,
    async_shutdown_calibration,
    async_start_auto_if_enabled,
    get_calibration_state,
    refresh_receivers_from_coords,
    reset_corrections,
    save_calibration_state,
    set_auto_calibration,
    start_calibration,
)
from .storage import (
    maps_dir,
    migrate_maps_out_of_www,
    LAYOUT_LOCK,
    get_layout,
    get_layout_for_edit,
    get_layout_version,
    load_fp_gains,
    load_layout,
    load_runtime,
    load_truth,
    save_truth,
    save_fp_gains,
    save_runtime,
    migrate_from_bps,
    migrate_legacy,
    save_layout,
)
from .const import ACCURACY_ENTITY_ID, UNTRACKED_ENTITY_ID
from . import history as history_mod
from . import bermuda_source
from . import election_log
from . import robots as robots_mod
from . import radars as radars_mod
from . import fingerprint
from . import floor_field
from . import registration
from . import truth as truth_mod
from . import persons as persons_mod
from . import runtime as runtime_mod
from .zone_adjust import adjust_zones, adjust_subzones

_LOGGER = logging.getLogger(__name__)

DOMAIN = "sextant"
OPTION_SHOW_SIDEBAR_PANEL = "show_sidebar_panel"
OPTION_UPDATE_INTERVAL = "update_interval"
# Trilateration (scipy least_squares, multi-start) is real CPU work. 1s was
# fine for a handful of things but scales linearly with thing count and
# adds up fast - profiling showed it as the largest chunk of custom-component
# CPU time on a live instance. 15s still updates a thing's room/position
# fast enough for presence automations while cutting recompute volume ~15x.
DEFAULT_UPDATE_INTERVAL = 15
FRONTEND_PATH = Path(__file__).parent / "frontend"
LEGACY_BPS_ENTITY_PATTERN = re.compile(r"^sensor\.(.+)_\1_sextant_(zone|floor)$")

# Global data (the layout now lives in the Store; see storage.get_layout)
state_change_lock = Lock()
state_change_counter = {}
update_queue = Queue()
tracked_listeners = {}
tracked_entities = []
new_global_data = {}
secToUpdate = DEFAULT_UPDATE_INTERVAL

# A refresh interval set for a while from the panel's countdown menu, to watch
# something move; it lapses by itself back to the configured interval. The
# smoothing and hold timers are wall clock (see SMOOTHING_REF_S), so a faster
# refresh gives more fixes, not jumpier ones.
INTERVAL_CHOICES = (5, 10, 30, 60)
INTERVAL_OVERRIDE_S = 15 * 60
_interval_override = {"secs": None, "until": 0.0}
_cycle_wake = None          # asyncio.Event: set to cut the current wait short
_cycle_last = {"at": None, "ms": None}   # when the last cycle finished, and how long it took


def cycle_interval(now=None):
    """Seconds between cycles right now: the panel's override while it lasts,
    else the configured interval."""
    now = time.time() if now is None else now
    o = _interval_override
    if o["secs"] and now < o["until"]:
        return float(o["secs"])
    o["secs"], o["until"] = None, 0.0
    return float(secToUpdate)


def set_interval_override(secs, now=None):
    """Refresh every ``secs`` for INTERVAL_OVERRIDE_S, or back to the configured
    interval (None, or the configured value). Takes effect at once: the wait
    for the next cycle is re-timed."""
    now = time.time() if now is None else now
    if secs is None or float(secs) == float(secToUpdate):
        _interval_override.update(secs=None, until=0.0)
    else:
        _interval_override.update(secs=float(secs), until=now + INTERVAL_OVERRIDE_S)
    if _cycle_wake is not None:
        _cycle_wake.set()
    return interval_info(now)


def interval_info(now=None):
    """The interval as the panel shows it."""
    secs = cycle_interval(now)
    return {
        "secs": secs,
        "configured": float(secToUpdate),
        "until": _interval_override["until"] if _interval_override["secs"] else None,
        "choices": sorted({*INTERVAL_CHOICES, int(secToUpdate)}),
        "last_cycle_at": _cycle_last["at"],
        "last_cycle_ms": _cycle_last["ms"],
    }


async def _wait_for_next_cycle():
    """Sleep the interval, re-timing when the panel changes it (a switch from
    60 s to 5 s should not sit out the rest of the minute) and when an
    override lapses mid-wait."""
    global _cycle_wake
    if _cycle_wake is None:
        _cycle_wake = asyncio.Event()
    started = time.time()
    while True:
        _cycle_wake.clear()
        now = time.time()
        left = cycle_interval(now) - (now - started)
        if left <= 0:
            return
        if _interval_override["secs"]:
            left = min(left, max(_interval_override["until"] - now, 0.05))
        try:
            await asyncio.wait_for(_cycle_wake.wait(), timeout=left)
        except asyncio.TimeoutError:
            pass
# A scanner Bermuda hasn't heard for this long is treated as offline; the
# liveness is polled from dump_devices every RECEIVER_DUMP_INTERVAL seconds.
RECEIVER_OFFLINE_SECS = 30
RECEIVER_DUMP_INTERVAL = 15
apitricords = []
# A thing not detected by any receiver for this long disappears from the
# map and its zone/floor sensors go to unknown. Override with a top-level
# "position_timeout" (seconds) in bpsdata.txt.
STALE_POSITION_SECS = 300

# --- Stale distance readings (per receiver, per thing) ----------------------
# A distance_to sensor keeps its last value when its scanner stops hearing the
# thing: the reading goes STUCK rather than unavailable (most visible on
# Bermuda's unfiltered distance entities, which have no timeout of their own).
# Fed to the solver, a stuck radius anchors the fix to a receiver that can no
# longer see the device. Readings older than this are dropped from the solve;
# override with a top-level "reading_max_age" (seconds) in the layout, or set it
# to 0 to disable the gate entirely.
#
# Raised from 30. Observed live on a real 48-receiver install (using the
# direct Bermuda API path in bermuda_source.py): "stale, dropped" rejections
# clustered almost entirely at exactly 30-31s old, with no long tail of much
# older readings, and enough of them that at least one thing went unsolved
# for a full 5 minutes and had its position cleared. That tight clustering
# right on the boundary, rather than a spread of ages, points to real per-pair
# advertise cadence (some scanner/device pairs just don't hear each other more
# often than ~30s - common when a BLE thing throttles its advertise rate
# while stationary to save battery) landing on a threshold with almost no
# margin, not to genuinely dead receivers (which would show much larger ages
# and wouldn't cluster this tightly). 45 gives that normal cadence headroom
# while still dropping anything actually stuck.
#
# Not fully isolated from a second, related variable: this gate's `age` now
# comes from Bermuda's own advert timestamp (the true "last heard" time)
# rather than an entity's last_updated, which is a step change in how this
# value is measured even though the intent - age since last heard - is the
# same. Whether that alone explains the clustering, or the real cadence was
# already this marginal and the entity path happened to mask it, was not
# separately isolated (the entity path cannot run concurrently to A/B, since
# the entities are disabled by design). Either way the fix is the same.
READING_MAX_AGE_SECS = 45

# --- Output-position smoothing (constant-velocity Kalman filter) -------------
# The published position is smoothed with a constant-velocity 2D Kalman filter
# (state [x, y, vx, vy]) instead of a fixed-length moving average. Unlike the
# old 3-sample mean, the filter carries a motion model, so it lags less while a
# thing is walking and settles more while it is still, and it adapts its gain
# to the estimated uncertainty rather than weighting every past fix equally.
#
# The noise parameters are defined in METRES (and metres/second) and converted
# into each floor's pixel space via the floor scale, so the filter behaves the
# same on maps of any resolution. They are deliberately "trusting": Sextant already
# reads Bermuda's smoothed rssi_distance (20-sample average + velocity gate), so
# the trilateration fixes fed in here are not raw-RSSI noisy. Tune KF_MEAS_NOISE_M
# up for more smoothing, or KF_ACCEL_NOISE_MS2 up for a snappier response.
KF_MEAS_NOISE_M = 1.5        # per-fix position uncertainty (m); larger = smoother
KF_ACCEL_NOISE_MS2 = 0.5     # expected acceleration (m/s^2) while moving; larger = more responsive
# The filter runs one step per positioning cycle, fifteen seconds apart, and
# at that step the process noise 0.5 m/s^2 allows is a position spread of
# tens of metres - so the filter trusted every new fix outright, smoothed
# nothing, and its velocity was the fix jitter divided by the cycle. An
# AirPods case on a table read 0.46 m/s, never counted as still, and its
# room flipped a hundred times a day on a 1.2 m jitter across a wall.
#
# So the filter has two speeds. While the fixes stay within what stillness
# explains it runs quiet, with this much smaller acceleration noise, and the
# estimate settles to an average over the last several cycles. When a fix
# lands further from the prediction than KF_MOVE_NIS allows (a normalised
# innovation, chi-squared with two degrees of freedom: 6 is about the 95th
# percentile), the thing is moving: the responsive noise takes over at once
# and stays for KF_MOVE_HOLD_S after the innovations calm down, so a
# pause mid-walk does not freeze the track. The hold is wall clock (it was
# three cycles, 45 s at the 15 s interval) so a faster refresh does not
# shorten it.
KF_ACCEL_NOISE_STILL_MS2 = 0.0005
KF_MOVE_NIS = 4.0
KF_MOVE_HOLD_S = 45.0
KF_INIT_VEL_UNC_MS = 1.0     # initial velocity uncertainty (m/s) at (re)init
KF_MAX_DT_S = 10.0           # cap the prediction step so a gap can't blow up P
# Gap beyond which the state is reset (the thing was away). This was 30 s on
# a 15 s cycle, so ONE missed cycle - a thing not heard for a moment, which
# happens on 14% of an AirPods case's cycles - started the filter over at
# the next raw fix, and nothing ever averaged. At 90 s it takes six missed
# cycles; a replay of a day's tracks found no further gain beyond that.
KF_MAX_GAP_S = 90.0
# Soft-gate scale for spiky per-receiver distances: a reading whose radius
# changed by this fraction versus the previous update is down-weighted to 0.5
# (was a hard 50% discard). Nothing is dropped, so the solver keeps enough
# points to fix a position even while every distance is legitimately changing
# during movement.
RADIUS_JUMP_TOL = 0.5
# Robust-loss knee for the trilateration solver, in units of the objective's
# residual (~relative radius error). soft_l1 down-weights any receiver whose
# radius disagrees with the fit by more than ~this fraction, so one persistently
# wrong (through-wall / body-shadowed) reading can't drag the position — the
# temporal jump gate only sees a one-tick change and is blind to a steady liar.
SOLVER_ROBUST_F_SCALE = 0.3
# Multi-start (see trilaterate()) solves up to 3x per thing per cycle to
# escape local minima. That's only needed when something could have actually
# changed since last cycle; a stationary thing gains nothing from starting
# fresh from the centroid every tick when last cycle's own answer is right
# there. _jump_weight() already scores this per-point (1.0 = unchanged since
# last update); the minimum across a floor's points must clear this before
# trilaterate() is allowed to skip straight to a single solve from the
# previous fix. Comfortably above _jump_weight's own 0.5 spike-gate floor, so
# a receiver that's merely started to drift still forces the full battery.
STABLE_HINT_MIN_JUMP_WEIGHT = 0.85

# Uploaded floor-plan maps: accepted image extensions and a size cap. Client
# filenames are never trusted for filesystem paths (see _safe_maps_child).
# .jfif/.jpe are ordinary JPEG variants a browser's image/jpeg picker yields.
_ALLOWED_MAP_EXTS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".jpe", ".gif", ".webp", ".bmp", ".svg", ".avif",
}
MAX_MAP_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
# Thing icons are served from www/ (public, unauthenticated): raster
# images only — an .svg or .html there would run script on HA's own origin.
_ALLOWED_ICON_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_ICON_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB


def _is_admin_request(request) -> bool:
    """Whether the authenticated user behind a view request is an administrator.

    Home Assistant's views only check for a valid login; anything that
    changes the layout or writes files under www/ is an administrator's job.
    """
    user = request.get("hass_user")
    return bool(user is not None and getattr(user, "is_admin", False))


def _admin_only(request):
    """A 403 response for a non-admin caller, or None."""
    if _is_admin_request(request):
        return None
    return web.Response(status=403, text="Administrators only")
# Non-map files that live in www/sextant_maps and must never be deletable via the
# save_text "remove" field (that field is only meant to drop an old map image).
_PROTECTED_MAPS_FILES = {"bpsdata.txt", "sextant_calibration_state.json"}
# Longest to wait on Bermuda's dump_devices before treating it as unavailable,
# so a hung/slow service can't stall the liveness or calibration loops.
DUMP_DEVICES_TIMEOUT_S = 10.0


def _safe_maps_child(maps_path, raw_name, allowed_exts=None):
    """Resolve raw_name to a path guaranteed to sit directly inside maps_path.

    Returns the resolved Path, or None if raw_name is unsafe. Path(name).name
    strips any directory component (so '../x' and '/etc/x' both collapse to a
    bare filename), and the resolved result is re-checked against maps_path as
    belt-and-suspenders. Client-supplied filenames must never reach the
    filesystem unfiltered (unauthenticated write endpoints, issue: audit #2).
    """
    if not raw_name:
        return None
    name = Path(str(raw_name)).name
    if not name or name in (".", ".."):
        return None
    if allowed_exts is not None and Path(name).suffix.lower() not in allowed_exts:
        return None
    base = Path(maps_path).resolve()
    target = (base / name).resolve()
    try:
        if not target.is_relative_to(base):
            return None
    except AttributeError:  # Python < 3.9 (HA is 3.12+; defensive only)
        if base != target and base not in target.parents:
            return None
    return target

# --- Receiver mount heights (optional per-receiver "height", metres) ---------
# Bermuda's distance estimates are line-of-sight SLANT ranges, but the map
# solve is 2D: a ceiling probe reading 2.3 m to a thing right below it is
# really ~0.6 m away horizontally. When a receiver's mount height is set in
# the panel, the vertical leg is removed before trilateration
# (horizontal = sqrt(slant^2 - dz^2)). Things are assumed to be carried at
# THING_HEIGHT_M above the floor; override with a top-level
# "thing_height" (metres) in bpsdata.txt.
THING_HEIGHT_M = 1.0

# --- Per-thing reference-power trim (issue #92) -----------------------------
# Bermuda turns RSSI into distance with an exponential path-loss model,
# d = 10 ** ((ref_power - rssi) / (10 * attenuation)). Cheap beacons vary in
# transmit power, so one Bermuda ref_power can read consistently long or short
# for a given tag. Sextant can't change Bermuda's config, but an offset of `delta`
# dB on ref_power is exactly a MULTIPLICATIVE scale on every distance from that
# thing: 10 ** (delta / (10 * attenuation)). So a per-thing offset (stored
# in metres-free dB under the top-level "thing_ref_offsets" map) is applied
# here as a distance factor — tunable live from the panel while watching the
# map, and portable back into Bermuda's own ref_power once a value is found.
# The exponent below is Bermuda's default attenuation; a user whose Bermuda
# uses a different one still gets a monotonic trim, just on a slightly
# different dB scale (this is a relative knob, not a calibrated instrument).
PATH_LOSS_EXPONENT = 3.0
THING_REF_OFFSET_MAX_DB = 20.0  # +/- range accepted from the panel/API
# Slant->horizontal legitimately produces very short radii (thing nearly
# under a ceiling probe). The solver's geometric 1/r^2 weight would explode
# there and let that one receiver dominate the fit, so for WEIGHTING (not for
# the residual) radii are clamped to this physical minimum, converted to each
# floor's pixel scale.
MIN_WEIGHT_RADIUS_M = 0.5

# --- Floor election by hypothesis competition ---------------------------------
# The floor used to be elected by the single nearest receiver — one noisy
# reading through a ceiling could steal the thing for a cycle (kitchen <->
# bedroom flapping, issue #94). Now every plausible floor is SOLVED and
# SCORED: the fit's agreement with all of that floor's receivers feeds a
# smoothed per-floor probability, and the elected floor only changes when a
# challenger clearly and persistently outscores the incumbent.
FLOOR_CANDIDATES = 3         # solve at most this many floors per cycle
FLOOR_PROB_SMOOTHING = 0.7   # EMA weight on the previous probability
FLOOR_SWITCH_MARGIN = 0.05   # probability lead that starts/keeps a challenge
# Dwell is WALL-CLOCK, not cycles: "three cycles" was 30 s at the default
# interval, and 20 of 39 floor changes in a 12 h sample were A->B->A flips.
# The default lives in TUNING_SPEC ("floor_switch_secs") so it can be changed
# live; this is the fallback when _elect_floor is called without one.
FLOOR_SWITCH_SECS = 60.0
FLOOR_DARK_GRACE_S = 45.0    # how long a dark incumbent holds everything frozen (was 3 cycles at 15 s)

# The smoothing weights (FLOOR_PROB_SMOOTHING, zone_prob_smoothing) are the
# weight kept per SMOOTHING_REF_S of wall clock - the interval they were tuned
# at - not per cycle. A cycle dt seconds after the last keeps
# weight ** (dt / SMOOTHING_REF_S), so a faster refresh gives more, smaller
# steps and the same smoothing in seconds: the data is not made jumpier.
SMOOTHING_REF_S = 15.0
# A longer gap counts as this long: a thing unheard for ten minutes should not
# have its whole smoothed history replaced by the one fix it comes back with.
SMOOTHING_MAX_STEP_S = 60.0


def _step_weight(weight, last, now):
    """The smoothing weight to keep for a step from ``last`` to ``now`` (see
    SMOOTHING_REF_S). With no previous step, the weight as tuned."""
    if last is None or now is None:
        return weight
    dt = min(max(float(now) - float(last), 0.0), SMOOTHING_MAX_STEP_S)
    return weight ** (dt / SMOOTHING_REF_S)
FLOOR_RESIDUAL_SCALE_M = 2.0 # weighted RMS residual (m) at which fit quality = 0.5
COVERAGE_TARGET_N = 5.0      # heard receivers at which the coverage term saturates

# No-go zones (issue #60): areas a thing can't physically be — the upper
# footprint of a double-height foyer/great room open to the floor below.
# When a floor's fit lands in one of its no-go zones the fit is impossible on
# THAT floor, so its election confidence is multiplied down: the competition
# then prefers the floor where the same spot is a real room (the open space
# means that floor's receivers already hear the thing and solve it as a
# candidate). The penalty only DOWN-WEIGHTS — a no-go floor that is the sole
# candidate still wins and its position is snapped out to the nearest allowed
# zone — so a thing is never left position-less.
NO_GO_CONF_PENALTY = 0.15
# When snapping a fix out of dead space, grow the no-go footprint by this many
# pixels before subtracting it from the allowed region, so the snap target's
# boundary sits clear of the (boundary-inclusive) no-go edge rather than
# exactly on it — otherwise the snapped point still reads as "in the no-go
# zone" to covers()-based tests.
NO_GO_SNAP_MARGIN_PX = 3.0
# A fix inside a no-go void says nothing about which side of the void the
# thing is on, so the snap out of it keeps the thing's current room while that
# room's wall is within this many metres of the nearest one. Eilee's watch on
# her bedside table solved into the foyer void between her room and Jack's,
# about equally far from both walls; snapped memorylessly it went to her wall,
# then his, 2.5 m apart, and the room flipped 50 times in a morning. Really
# crossing a void means walking round it, through rooms, where nothing snaps.
NO_GO_SNAP_STICK_M = 1.5

# Per-thing election state, all reset when the thing is pruned:
# smoothed floor probabilities (entity -> {floor name: P}), the pending
# challenge (a floor out-scoring the incumbent, counted per cycle: entity ->
# {"floor": name, "count": n}), and since when the incumbent floor has been
# dark (unsolvable) while a competitor solved (entity -> wall clock).
_floor_probability = {}
_floor_probability_at = {}   # entity -> when its probabilities last moved
_floor_challenge = {}
_floor_dark_cycles = {}
# When the incumbent floor was elected (wall clock), for the tenure bonus.
_floor_since = {}

# Per-thing Kalman state: entity -> {"x": np.array(4), "P": np.array(4,4),
# "ts": float, "floor": str}. Reset on floor change, long gap, or prune.
_kf_position_state = {}

# Per-thing zone election state (see _elect_zone) and the published
# sub-zone's dwell state (see _elect_subzone). Reset on floor change and
# on prune, like the Kalman state.
_zone_state = {}
_subzone_state = {}


def _new_zone_state(floor_name, now):
    """A thing's room election, before it has any evidence.

    The elections index these dicts by name, so every key has to be here
    whether or not it has a value yet. It doubles as the shape a restored
    state is filled out against (see _restore_runtime): a snapshot written by
    an older release, or one whose keys a later release added to, then comes
    back readable instead of raising on the first cycle.
    """
    return {
        "floor": floor_name, "zone": None, "since": now, "probs": {},
        "challenge": None, "still_since": None, "moving_since": None,
        "away_since": None, "outvoted_since": None, "locked": False, "born": now,
        "probs_at": None,
    }


def _new_subzone_state(floor_name, zone, now):
    """A thing's spot election, before it has any evidence (see _new_zone_state)."""
    return {"floor": floor_name, "zone": zone, "value": ("unknown", zone), "probs": {},
            "pending": None, "since": now, "probs_at": None}


# Near-field anchor per thing: {"slug", "floor", "since", "pending": (slug, since) | None}
_anchor_state = {}

# --- Runtime tuning (sextant.set_tuning) ------------------------------------------
# Knobs for the accuracy work that are safe to flip on a live install without
# a code change. They live under a top-level "tuning" map in the layout store
# and are set through the sextant.set_tuning service (never by editing
# .storage/sextant under a running HA, which is silently lost on the next save).
# Each entry is (default, type, min, max) for numbers, (default, bool) for
# switches, or (default, str, allowed) for choices.
TUNING_SPEC = {
    # Which per-pair distance feeds the solver. "bermuda" is Bermuda's own
    # smoothed distance: a running-minimum-biased average built for "which
    # scanner is nearest", which lags on the way out and reads far receivers
    # short - and a short far receiver drags a least-squares fit toward it.
    # "median" takes the median of the recent raw RSSI samples and converts it
    # with the same path-loss parameters Bermuda used: symmetric, no low bias.
    # It needs a Bermuda build with the rssi_history feature and falls back to
    # "bermuda" per reading when history is missing or too thin.
    "distance_estimator": ("bermuda", str, ("bermuda", "median")),
    "median_window_secs": (15.0, float, 3.0, 120.0),    # samples newer than this
    "median_min_samples": (3, int, 1, 20),              # fewer -> fall back
    # Receivers per solve: everything within solver_near_always metres, plus
    # the nearest solver_max_receivers (0 = unlimited); readings beyond
    # solver_max_range (0 = no cap) are dropped unless needed to reach three
    # points. At 8 m and beyond a BLE range estimate is mostly noise, and the
    # 1/r^2 weight does not zero it out.
    "solver_max_receivers": (8, int, 0, 100),
    "solver_max_range": (12.0, float, 0.0, 100.0),
    "solver_near_always": (3.0, float, 0.0, 50.0),
    # Zone election (see _elect_zone). Off = publish the instantaneous zone.
    "zone_hysteresis": (True, bool),
    "zone_prob_smoothing": (0.6, float, 0.0, 0.95),     # EMA weight on the previous probability
    "zone_switch_margin": (0.15, float, 0.0, 1.0),      # lead a challenger needs
    "zone_switch_secs": (20.0, float, 0.0, 600.0),      # ...held this long, wall clock
    "stationary_speed": (0.3, float, 0.0, 5.0),         # m/s; below this the thing is still
    "stationary_secs": (20.0, float, 0.0, 600.0),       # still this long -> zone locked
    "zone_unlock_margin": (1.0, float, 0.0, 20.0),      # m outside the locked zone...
    "zone_unlock_secs": (30.0, float, 0.0, 600.0),      # ...for this long -> unlocked
    # No lock until a thing has been tracked this long since Sextant started
    # (or since it changed floor): the first fixes after a restart wander, and
    # a phone on a kitchen counter was locked into the foyer next door that way.
    "zone_lock_warmup_secs": (120.0, float, 0.0, 3600.0),
    "subzone_switch_secs": (20.0, float, 0.0, 600.0),
    # Sub-zone election (see _elect_subzone): the smoothed share of the fix's
    # uncertainty that must fall inside a sub-zone before it is entered, and
    # how far (m) outside its polygon the fix must sit before it is left.
    "subzone_enter_prob": (0.5, float, 0.1, 0.95),
    "subzone_unlock_margin": (1.0, float, 0.0, 5.0),
    # How far a still thing's fix must leave its spot before the room lock
    # stops holding it there. A watch on a 0.7 x 0.5 m bedside table wanders
    # 1-2 m while it lies there, so the ordinary margin would drop it every
    # night; a cat that crossed the room is metres away and should be let go.
    "subzone_lock_release_m": (2.5, float, 0.5, 20.0),
    # A spot with a proxy on it (a bedside table, a desk): the proxy hearing the
    # thing close, and clearly closer than every other proxy, counts as the thing
    # being in the spot - direct evidence, where the position estimate is as
    # wide as the furniture. Full weight within spot_proxy_near_m, none from
    # spot_proxy_far_m; full when every other proxy reads spot_proxy_ratio times
    # farther, none when one reads within 1.25x (see _spot_proxy_evidence).
    "spot_proxy_near_m": (1.2, float, 0.1, 5.0),
    "spot_proxy_far_m": (2.0, float, 0.2, 10.0),
    "spot_proxy_ratio": (2.0, float, 1.3, 10.0),
    # Floor election dwell (see _elect_floor).
    "floor_switch_secs": (FLOOR_SWITCH_SECS, float, 0.0, 3600.0),
    # The lead a challenger floor needs before its dwell starts. 0.05 lets a
    # near-tie (a phone on the floor above a strong proxy) drift between two
    # floors on a half-hour period; 0.10 held Eilee's phone on its floor all
    # night in replay with the cats' moves untouched.
    "floor_switch_margin": (FLOOR_SWITCH_MARGIN, float, 0.0, 0.5),
    "floor_tenure_bonus": (0.05, float, 0.0, 0.5),      # extra margin at full tenure
    "floor_tenure_full_secs": (600.0, float, 1.0, 86400.0),
    # How much a floor's confidence is scaled by how near its nearest receiver
    # is, relative to the nearest receiver on any competing floor (see
    # _proximity_weighted_scores). 0 = pure fit-quality election.
    "floor_proximity_weight": (0.5, float, 0.0, 1.0),
    # How that weight combines fit and proximity (see _proximity_weighted_scores).
    # "gated": conf x ((1 - w) + w x prox) - a poor fit caps the floor however
    # near its proxies. "geometric": conf^(1 - w) x prox^w - the weighted
    # geometric mean, where a poor fit costs a floor less than a far nearest
    # proxy does. A phone on a bedside table next to an open foyer solves a
    # metre over the void every few cycles; the no-go penalty then cut its
    # own floor's fit to 0.15 and the floor below, hearing it through the
    # slab at much the same range, won for half the night. Geometric at 0.7
    # with floor_switch_margin 0.10 replayed that night at 0 % wrong and
    # halved every floor change in the house; gated at any weight did not.
    "floor_proximity_blend": ("gated", str, ("gated", "geometric")),
    # How many of a floor's nearest receivers that proximity term averages.
    # One receiver straight through a wood floor can read nearer than the
    # receivers in the room (a dog on the sun-room floor: basement 1.7 m,
    # ground 1.9 m); the three nearest cannot (3.1 m vs 4.9 m).
    "floor_proximity_k": (3, int, 1, 8),
    # Where receiver calibration writes its corrections. "sextant": a per-receiver
    # distance multiplier in this layout (the original behaviour). "bermuda":
    # the equivalent per-scanner rssi offset written into Bermuda itself
    # (needs a Bermuda build with the rssi_offsets API), so Bermuda's own
    # area/distance sensors are corrected too and Sextant applies nothing twice.
    "calibration_target": ("sextant", str, ("sextant", "bermuda")),
    # Close-range fade (see _close_range_correction). Calibration fits one
    # stretch per receiver from proxy pairs metres apart; a receiver that hears
    # its siblings short gets stretched, and that stretch pushes a thing lying
    # right beside it away from it. So a stretching correction (> 1) fades out
    # below correction_fade_far_m and is gone by correction_fade_near_m.
    # Shrinking corrections (< 1) are always applied in full. Off = the
    # correction everywhere, as before 3.17.6.
    "correction_close_fade": (True, bool),
    "correction_fade_near_m": (1.0, float, 0.0, 10.0),
    "correction_fade_far_m": (2.5, float, 0.1, 20.0),
    # Fingerprint fusion (fingerprint.py). "geometric" is the trilateration
    # alone. "fingerprint" places the thing at the best-matching reference
    # receivers and only falls back to the fit where no reference exists.
    # "fused" blends both: the fix is (1 - fingerprint_weight) x geometric +
    # fingerprint_weight x fingerprint, and each floor's election confidence
    # is blended the same way with fingerprint_floor_weight. Either non-
    # geometric mode also lets a floor with fewer than three receivers
    # compete, on its fingerprint alone.
    "position_estimator": ("geometric", str, ("geometric", "fingerprint", "fused")),
    "fingerprint_weight": (0.5, float, 0.0, 1.0),
    "fingerprint_floor_weight": (0.5, float, 0.0, 1.0),
    "fingerprint_k": (3, int, 1, 8),                    # references averaged per fix
    "fingerprint_missing_m": (12.0, float, 2.0, 50.0),  # "not heard" counts as this far
    "fingerprint_ref_gain": (1.0, float, 0.25, 4.0),    # probe beacons hotter (<1) / cooler (>1) than things
    # Learn the rest of that gain from the things themselves: every match
    # yields the median ratio between the thing's ranges and its best
    # reference's, and the learned factor (fingerprint.ReferenceDB.learn)
    # multiplies fingerprint_ref_gain. Reported per fix as fp.gain.
    "fingerprint_auto_gain": (True, bool),
    # Truth marks ("it is actually here", Live page) double as fingerprint references at
    # the marked point, in the marking thing's own scale: off to use only the proxies.
    "fingerprint_marks": (True, bool),
    # Whose marks guide a thing. A mark records how ONE device looks from one
    # place; a phone held in a hand and a watch on a wrist do not look alike.
    # With every mark used for everything, a watch at the kitchen counter
    # matched two marks of someone else's phone on the couch and was averaged
    # half way there. "class": a thing's own marks and those of things of its
    # class (the cats share Meg's); "own": its own only; "all": as before 3.17.35.
    "fingerprint_marks_scope": ("class", str, ("own", "class", "all")),
    # Near-field anchor (see _elect_anchor): a thing one proxy reads at
    # under anchor_max_m, with every other proxy at least anchor_ratio times
    # farther, for anchor_secs, is placed AT that proxy - a watch on the
    # bedside table next to it, not 1.7 m away where the far proxies' errors
    # pull the fit. Released once the reading opens past anchor_release_m.
    # anchor_max_m 0 disables it.
    "anchor_max_m": (0.8, float, 0.0, 5.0),
    "anchor_ratio": (2.0, float, 1.0, 10.0),
    "anchor_secs": (20.0, float, 0.0, 600.0),
    "anchor_release_m": (1.5, float, 0.1, 10.0),
    # How long a thing may go unheard before the Live page draws it as a
    # ghost - translucent, with how long ago it was last heard - because the
    # position shown is then a memory rather than a reading. Display only:
    # nothing about positioning changes, and position_timeout still decides
    # when the thing leaves the map altogether.
    "stale_after_secs": (120.0, float, 15.0, 3600.0),
    # How long a restart may take and still be resumed rather than started
    # cold (see runtime.py). Past it only each thing's last sighting is kept.
    "restore_state_secs": (runtime_mod.DEFAULT_MAX_AGE_SECS, float, 0.0, 86400.0),
    # When the Live list stops waiting for a thing and calls it away: a phone
    # that left the house, a tag in a drawer. Between stale_after_secs and
    # this it is still expected back, and shown where it was last seen.
    "away_after_secs": (900.0, float, 60.0, 86400.0),
    # A person's GPS tracker that has not reported for this long is disregarded
    # (see persons.judge_source): a Companion app that lost its location
    # permission sat on "home" for six days.
    "gps_stale_secs": (7200.0, float, 60.0, 604800.0),
    # Keep every floor election (each cycle's per-floor candidates, the odds
    # and the winner, per thing) for this many hours in
    # config/sextant_election_log, for tools/replay_floors.py. 0 = off. About
    # 150 MB a day for twenty things; see election_log.py.
    "election_log_hours": (0.0, float, 0.0, 168.0),
    # How often a robot vacuum that is out cleaning is asked where it is
    # (robots.py). Each ask makes the Roborock integration fetch and parse the
    # vacuum's map, which holds Home Assistant's event loop for a few hundred
    # milliseconds, so this is a trade between a smooth trail and a quiet
    # loop. A docked robot is never asked: it is at its dock.
    "robot_poll_secs": (30.0, float, 10.0, 600.0),
    # mmWave radars placed on the plan (radars.py). Fusion: a thing whose
    # Bluetooth fix is within mmwave_pair_m of a radar target is placed on
    # the target. Auto pins: one target and one thing, both still for a
    # minute, is a location pin (at most one per thing per half hour).
    "mmwave_fusion": (True, bool),
    "mmwave_auto_pins": (True, bool),
    "mmwave_pair_m": (1.5, float, 0.3, 5.0),
    # Hours of position history kept per thing: the history scrubber, the
    # timeline and Activity reach back this far. Applied on the next
    # cycle; an explicit top-level history_max_age (seconds) still wins.
    "history_hours": (6.0, float, 1.0, 168.0),
    # Who may read where things have been (the scrubber, timeline and Activity):
    # everyone signed in, or admins only. Live positions and the sensors stay
    # visible to every user either way, as all Home Assistant entities are.
    "history_admin_only": (False, bool),
}

# Reference fingerprints: receiver-to-receiver ranges, refreshed on a slow
# cadence at the top of the loop (the receivers do not move).
FINGERPRINT_REFRESH_SECS = 20.0
# The learned gains are written out this often (when they moved), so a restart starts warm
# instead of walking the shared gain back from 1.0 and every thing's from scratch.
FP_GAIN_PERSIST_SECS = 300.0
_fingerprint_db = fingerprint.ReferenceDB()
# The last cycles' solver inputs per thing, for truth marks (truth.py).
_truth_buffer = truth_mod.Buffer()


def _thing_fp_weight(layout, entity):
    """This thing's own blend weight (0 = geometric alone, 1 = fingerprint alone; the Live
    slider, thing_fp_weights), or None to follow the tuning."""
    weights = layout.get("thing_fp_weights") if isinstance(layout, dict) else None
    w = weights.get(entity) if isinstance(weights, dict) else None
    if isinstance(w, (int, float)) and not isinstance(w, bool) and 0.0 <= w <= 1.0:
        return float(w)
    return None


def _thing_estimator(layout, entity):
    """The position estimator for one thing: its own blend weight first (0 is geometric,
    1 fingerprint, between is fused), then its estimator override (thing_estimators, from
    the thing dialog: a Tile the fingerprint makes worse can be geometric only), then the
    tuning."""
    w = _thing_fp_weight(layout, entity)
    if w is not None:
        return truth_mod.estimator_for(w)
    overrides = layout.get("thing_estimators") if isinstance(layout, dict) else None
    own = overrides.get(entity) if isinstance(overrides, dict) else None
    if own in TUNING_SPEC["position_estimator"][2]:
        return own
    return _tuning(layout, "position_estimator")


def _seed_thing_gain(layout, entity):
    """A gain multiplier applied from a truth mark (thing_fp_gains) seeds the learned one."""
    seeds = layout.get("thing_fp_gains") if isinstance(layout, dict) else None
    g = seeds.get(entity) if isinstance(seeds, dict) else None
    if entity not in _fingerprint_db.thing_gain and isinstance(g, (int, float)) and not isinstance(g, bool) and g > 0:
        _fingerprint_db.thing_gain[entity] = float(g)


def _fingerprint_wanted(layout):
    """Whether anything needs the reference DB: the tuning, or any thing's own override."""
    if _tuning(layout, "position_estimator") != "geometric":
        return True
    overrides = layout.get("thing_estimators") if isinstance(layout, dict) else None
    if isinstance(overrides, dict) and any(v in ("fused", "fingerprint") for v in overrides.values()):
        return True
    weights = layout.get("thing_fp_weights") if isinstance(layout, dict) else None
    return isinstance(weights, dict) and any(isinstance(w, (int, float)) and w > 0 for w in weights.values())


# Truth marks as stored (with their samples), and the references built from
# them under the corrections in force when they were last built.
_truth_marks = []
_mark_ref_cache = {"key": None, "refs": []}


def _set_truth_marks(marks):
    """Replace the marks the fingerprint matcher draws references from."""
    _truth_marks[:] = list(marks or [])
    _mark_ref_cache["key"] = None


def _raw_vector(layout):
    """{rx_address: metres} this cycle, before calibration and per-thing trim."""
    out = {}
    for floor in (layout or {}).get("floor") or []:
        for receiver in floor.get("receivers") or []:
            address, raw = receiver.get("address"), receiver.get("raw_distance")
            if isinstance(address, str) and address and "distance" in receiver \
                    and isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
                out[address.lower()] = float(raw)
    return out


def _mark_rebase_fns(layout, entity):
    """(mult, rx_at) for truth.rebase: this thing's multiplier per receiver
    now, and the receiver placed at a point with its vertical leg."""
    thing_h = _thing_height(layout, entity)
    corr, placed = {}, {}
    for floor in (layout or {}).get("floor") or []:
        for receiver in floor.get("receivers") or []:
            address, cords = receiver.get("address"), receiver.get("cords") or {}
            if not isinstance(address, str) or not address:
                continue
            c = receiver.get("correction")
            corr[address.lower()] = float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0 else 1.0
            h = receiver.get("height")
            dz = float(h) - thing_h if isinstance(h, (int, float)) and not isinstance(h, bool) and 0 <= h <= 10 else None
            try:
                placed.setdefault(floor.get("name"), []).append((float(cords["x"]), float(cords["y"]), address.lower(), dz))
            except (KeyError, TypeError, ValueError):
                continue
    factor = _thing_distance_factor(layout, entity)

    def mult(address, raw_m):
        c = corr.get(address)
        if c is None:
            return None
        # raw_m None: the multiplier a mark from before raw readings were kept
        # was recorded with - the correction in full.
        return (c if raw_m is None else _close_range_correction(c, raw_m, layout)) * factor

    def rx_at(floor_name, x, y):
        for rx, ry, address, dz in placed.get(floor_name, ()):
            if abs(rx - x) <= 1.0 and abs(ry - y) <= 1.0:
                return address, dz
        return None

    return mult, rx_at


def _rebased_samples(layout, mark):
    """A mark's samples with the corrections in force now (see truth.rebase)."""
    mult, rx_at = _mark_rebase_fns(layout, mark.get("entity"))
    return truth_mod.rebase(mark.get("samples") or [], mult, rx_at, MIN_WEIGHT_RADIUS_M)


def _mark_basis(layout):
    """What a mark reference depends on: corrections, heights, trims, the fade."""
    rx = tuple(sorted(
        (str(r.get("address")), r.get("correction"), r.get("height"))
        for f in (layout or {}).get("floor") or [] for r in f.get("receivers") or []
    ))
    things = json.dumps([(layout or {}).get(k) for k in ("thing_ref_offsets", "thing_heights", "thing_height")], sort_keys=True, default=str)
    fade = tuple(_tuning(layout, k) for k in ("correction_close_fade", "correction_fade_near_m", "correction_fade_far_m"))
    return rx, things, fade


def _mark_refs(layout, entity=None):
    """The truth-mark references that may guide ``entity`` (every one when it
    is None), or None when there are none or they are switched off."""
    refs = _all_mark_refs(layout)
    scope = _tuning(layout, "fingerprint_marks_scope")
    if not refs or entity is None or scope == "all":
        return refs
    classes = layout.get("thing_classes") if isinstance(layout, dict) else None
    mine = (classes or {}).get(entity)

    def guides(ref):
        if ref.get("entity") == entity:
            return True
        return scope == "class" and mine is not None and (classes or {}).get(ref.get("entity")) == mine

    return [r for r in refs if guides(r)] or None


def _all_mark_refs(layout):
    if not _truth_marks or not _tuning(layout, "fingerprint_marks"):
        return None
    key = _mark_basis(layout)
    if key != _mark_ref_cache["key"]:
        refs = []
        for mark in _truth_marks:
            ref = truth_mod.mark_reference(mark, samples=_rebased_samples(layout, mark))
            if ref:
                ref["entity"] = mark.get("entity")   # whose mark it is: see fingerprint_marks_scope
                refs.append(ref)
        _mark_ref_cache.update(key=key, refs=refs)
    return _mark_ref_cache["refs"] or None


def _persist_fp_gains(hass, now_ts):
    """Every FP_GAIN_PERSIST_SECS, save the learned gains if they moved (event loop only)."""
    if now_ts - getattr(_persist_fp_gains, "last", 0.0) < FP_GAIN_PERSIST_SECS:
        return
    _persist_fp_gains.last = now_ts
    snap = {"learned_gain": round(_fingerprint_db.learned_gain, 4),
            "thing_gain": {e: round(g, 4) for e, g in _fingerprint_db.thing_gain.items()}}
    if snap == getattr(_persist_fp_gains, "saved", None):
        return
    _persist_fp_gains.saved = snap
    try:
        hass.async_create_task(save_fp_gains(hass, snap))
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Fingerprint gains not saved: %s", e)


def _restore_fp_gains(saved):
    """Seed the reference DB from a saved snapshot; never over a gain already learned this run."""
    if not isinstance(saved, dict):
        return
    g = saved.get("learned_gain")
    if isinstance(g, (int, float)) and not isinstance(g, bool) and 0 < g and _fingerprint_db.learned_gain == 1.0:
        _fingerprint_db.learned_gain = min(fingerprint.LEARNED_GAIN_MAX, max(fingerprint.LEARNED_GAIN_MIN, float(g)))
    for entity, tg in (saved.get("thing_gain") or {}).items():
        if isinstance(tg, (int, float)) and not isinstance(tg, bool) and tg > 0 and entity not in _fingerprint_db.thing_gain:
            _fingerprint_db.thing_gain[entity] = min(fingerprint.LEARNED_GAIN_MAX, max(fingerprint.LEARNED_GAIN_MIN, float(tg)))
    _persist_fp_gains.saved = {"learned_gain": round(_fingerprint_db.learned_gain, 4),
                               "thing_gain": {e: round(v, 4) for e, v in _fingerprint_db.thing_gain.items()}}


def _refresh_fingerprint_references(hass, layout, now_ts):
    """Sample Bermuda's scanner ranging into the reference DB when due."""
    if not _fingerprint_wanted(layout):
        return
    if now_ts - getattr(_refresh_fingerprint_references, "last", 0.0) < FINGERPRINT_REFRESH_SECS:
        return
    _refresh_fingerprint_references.last = now_ts
    _persist_fp_gains(hass, now_ts)
    ranging = bermuda_source.async_get_scanner_ranging(hass, max_age=fingerprint.REF_MAX_AGE_SECS)
    if ranging is None:
        if not getattr(_refresh_fingerprint_references, "warned", False):
            _refresh_fingerprint_references.warned = True
            _LOGGER.warning(
                "position_estimator is %s but this Bermuda build has no scanner_ranging API; "
                "fingerprinting stays off until Bermuda is updated",
                _tuning(layout, "position_estimator"),
            )
        return
    _fingerprint_db.ingest(ranging)


def _coerce_tuning(key, value, fallback=None):
    """Validate one tuning value against TUNING_SPEC; ``fallback`` when invalid."""
    spec = TUNING_SPEC[key]
    kind = spec[1]
    if kind is bool:
        return value if isinstance(value, bool) else fallback
    if kind is str:
        return value if isinstance(value, str) and value in spec[2] else fallback
    # not-bool: isinstance(True, int) holds in Python.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if not spec[2] <= value <= spec[3]:
        return fallback
    return kind(value)


def _tuning(data, key):
    """A validated tuning value from the layout's top-level "tuning" map.

    Out-of-range, wrong-type or unknown values fall through to the default,
    so a hand-edited store can never poison the loop. ``data`` may be the
    layout dict or anything else (-> defaults).
    """
    default = TUNING_SPEC[key][0]
    tuning = data.get("tuning") if isinstance(data, dict) else None
    if not isinstance(tuning, dict) or key not in tuning:
        return default
    return _coerce_tuning(key, tuning[key], default)


def _solves(receiver):
    """Whether a placed proxy takes part in positioning.

    ``"solve": false`` on a receiver (the "Use in positioning" switch in its
    panel on the Edit page) keeps it out of the solve, the floor election,
    the near-field anchor and spot evidence, while it stays placed, drawn,
    calibrated and self-tested. For a proxy that hears fine but reads wrong
    where it sits - a Shelly in a metal-lined kitchen corner that the
    self-test put 9 m from its plug and that was the nearest reading in one
    kitchen cycle in five - so its readings stop steering the fix without
    losing what the proxy still tells you about itself. The fingerprint
    match keeps using it on purpose: it compares patterns, not geometry, and
    a receiver dropped from the thing's vector would read as "far away"
    (fingerprint.similarity's missing_m), which is the one thing it is not.
    """
    return receiver.get("solve") is not False


def _close_range_correction(correction, raw_m, data):
    """The share of a receiver's calibration correction to apply at this range.

    A stretching correction (> 1) is geometrically faded from nothing at
    correction_fade_near_m to all of it at correction_fade_far_m; see the
    tuning comment. Replayed on the truth marks this halved the error of a
    watch on a bedside proxy (1.5 m -> 0.7 m) and left the others within a
    few centimetres.
    """
    if correction <= 1.0 or not _tuning(data, "correction_close_fade"):
        return correction
    near = _tuning(data, "correction_fade_near_m")
    far = max(_tuning(data, "correction_fade_far_m"), near + 0.01)
    if raw_m >= far:
        return correction
    if raw_m <= near:
        return 1.0
    return correction ** ((raw_m - near) / (far - near))


def _layout_for(new_global_data, entity):
    """The layout dict a thing's per-cycle data was built from (or None)."""
    for ent in new_global_data:
        if ent.get("entity") == entity:
            return ent.get("data")
    return None


# --- Per-pair distance from raw RSSI (distance_estimator = "median") ---------
MEDIAN_TARGET_SAMPLES = 5  # sample count at which a median's weight saturates


def _median_distance(reading, window_secs, min_samples):
    """(distance_m, quality) from a reading's raw RSSI history, or None.

    ``reading`` is one bermuda_source reading carrying ``history`` (newest
    first ``[rssi, stamp]`` pairs, monotonic stamps), ``age`` (seconds since
    the newest sample) and the path-loss parameters Bermuda applied. Samples
    older than ``window_secs`` are ignored; with fewer than ``min_samples``
    left the caller keeps Bermuda's own distance. The median is taken in the
    RSSI (log) domain, where the noise is closer to symmetric, and converted
    once. quality in (0, 1] rises with the sample count, so a distance backed
    by one packet pulls the fit less than one backed by five.
    """
    history = reading.get("history")
    ref_power = reading.get("ref_power")
    attenuation = reading.get("attenuation")
    if (
        not history
        or not isinstance(ref_power, (int, float)) or isinstance(ref_power, bool)
        or not isinstance(attenuation, (int, float)) or isinstance(attenuation, bool)
        or attenuation <= 0
    ):
        return None
    offset = reading.get("rssi_offset") or 0.0
    age = reading.get("age") or 0.0
    newest = None
    samples = []
    for item in history:
        try:
            rssi, stamp = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if newest is None:
            newest = stamp
        if age + (newest - stamp) <= window_secs:
            samples.append(rssi)
    if len(samples) < max(1, int(min_samples)):
        return None
    rssi_med = float(np.median(samples))
    distance = 10.0 ** ((ref_power - (rssi_med + offset)) / (10.0 * attenuation))
    quality = min(1.0, len(samples) / MEDIAN_TARGET_SAMPLES)
    return distance, quality


def _select_receivers(entries, max_receivers, max_range, near_always):
    """Cap the receivers that feed one floor's solve.

    ``entries`` are ``(distance_m, point)`` pairs, or ``(distance_m, point,
    name)`` triples. Keeps every receiver within ``near_always`` metres plus
    the ``max_receivers`` nearest (0 = all), and drops anything beyond
    ``max_range`` (0 = no cap) once three points are already kept, so a floor
    can never be starved below the solver's minimum by the cap alone. Returns
    ``(points, heard)`` nearest-first, ``heard`` being the (name, distance)
    pairs behind those points - which proxy said what, for the floor-election
    telemetry, since the points themselves are anonymous coordinates.
    """
    entries = sorted(entries, key=lambda e: e[0])
    limit = max(int(max_receivers), 3) if max_receivers else 0
    kept, heard = [], []
    for i, entry in enumerate(entries):
        distance, point = entry[0], entry[1]
        if limit and i >= limit and distance > near_always:
            continue
        if max_range and distance > max_range and len(kept) >= 3:
            continue
        kept.append(point)
        heard.append((entry[2] if len(entry) > 2 else None, round(float(distance), 2)))
    return kept, heard


def _thing_height(data, entity=None):
    """Assumed thing height above the floor (m) for slant correction.

    Precedence: the device's own entry in "thing_heights" (set per thing
    in the panel — an ankle beacon at 0.1 m and a phone at 1.0 m need
    different vertical legs), then the top-level "thing_height" override,
    then the 1.0 m default. Out-of-range/garbage values fall through.
    """
    if isinstance(data, dict):
        per_thing = data.get("thing_heights")
        if entity is not None and isinstance(per_thing, dict):
            configured = per_thing.get(entity)
            # not-bool: isinstance(True, int) holds in Python, so a hand-edited
            # true/false would otherwise read as a valid 1.0/0.0 m height.
            if isinstance(configured, (int, float)) and not isinstance(configured, bool) \
                    and 0 <= configured <= 5:
                return float(configured)
        configured = data.get("thing_height")
        if isinstance(configured, (int, float)) and not isinstance(configured, bool) \
                and 0 <= configured <= 5:
            return float(configured)
    return THING_HEIGHT_M


HISTORY_FLUSH_INTERVAL = 60  # s between appends of buffered history to disk


def get_position_history(hass):
    """The per-install position history, created on first use."""
    bucket = hass.data.setdefault(DOMAIN, {})
    hist = bucket.get("_history")
    if hist is None:
        hist = bucket["_history"] = history_mod.PositionHistory(
            history_mod.history_config(get_layout(hass)))
    return hist


def history_dir(hass):
    """Where the NDJSON day segments live (under .storage, never web-served)."""
    return hass.config.path(".storage", history_mod.HISTORY_DIRNAME)


def _history_lock(hass):
    """Serialises every history disk operation (flush, prune, restore, clear).

    Without it a Clear can be undone: the periodic flush drains the queue and
    hands it to the executor, the Clear deletes the segments, and then the
    append lands - putting the forgotten positions back on disk, where the next
    restart reads them in again. The per-thing rewrite has the mirror problem
    (read-filter-replace losing rows an append wrote in the meantime).
    """
    bucket = hass.data.setdefault(DOMAIN, {})
    lock = bucket.get("_history_lock")
    if lock is None:
        lock = bucket["_history_lock"] = asyncio.Lock()
    return lock


def _history_prunable_max_age(hass, hist):
    """The retention to prune against, or None when it can't be trusted.

    Pruning DELETES days of record irreversibly, so it must never run on a
    guessed window: if the layout cache isn't a dict (a fresh install, or a
    store that failed to load this boot) history_config falls back to the 6 h
    default, which would take a configured 7-day record down to six hours.
    """
    layout = get_layout(hass)
    if not isinstance(layout, dict):
        return None
    return hist.cfg["max_age"]


async def flush_position_history(hass, prune=False):
    """Append buffered points to today's segment; optionally prune old days.

    All file work happens in the executor. Failing here loses at most the
    buffered points (the in-memory ring is untouched), so it must never
    propagate into the tracking loop.
    """
    hist = get_position_history(hass)
    hist.configure(history_mod.history_config(get_layout(hass)))
    # Age every track, not just the ones that recorded this cycle: a thing
    # that went silent (or a history since switched off) would otherwise keep
    # serving points past the configured window.
    hist.evict_all()
    if not hist.pending_count() and not prune:
        return
    dirpath = history_dir(hass)
    max_age = _history_prunable_max_age(hass, hist) if prune else None

    async with _history_lock(hass):
        # Drain INSIDE the lock so a Clear cannot slip between the drain and
        # the write and be overwritten by it.
        grouped = hist.drain_pending()
        if not grouped and max_age is None:
            return

        def _work():
            if grouped:
                history_mod.append_segments(dirpath, grouped)
            if max_age is not None:
                history_mod.prune_segments(dirpath, max_age)

        try:
            await hass.async_add_executor_job(_work)
        except Exception:
            # The write failed; put the rows back rather than dropping them on
            # the floor. requeue() honours the pending cap, so a disk that
            # stays broken cannot grow this without bound.
            hist.requeue(grouped)
            raise


async def restore_position_history(hass):
    """Reload the retained window from disk at startup.

    Every restored track is gap-marked afterwards: the integration was down
    for an unknown span, so the first new fix must start a fresh polyline
    rather than draw a straight line across the outage.
    """
    hist = get_position_history(hass)
    # A config-entry reload re-enters setup with the SAME PositionHistory:
    # async_unload_entry cancels the tracking task but leaves hass.data[DOMAIN]
    # alone, so re-reading the segments would append a second copy of every
    # point already held and leave the arrays unsorted, breaking every bisect
    # in query() and evict(). There is nothing to restore into a live ring.
    if hist.entities() or hist.pending_count():
        hist.mark_all_gaps()
        return
    dirpath = history_dir(hass)
    cfg = dict(hist.cfg)
    # Same rule as the periodic flush: pruning DELETES days irreversibly, so it
    # must never run on a guessed window. At startup the layout may not have
    # loaded (or may be unreadable this boot), in which case history_config
    # hands back the 6 h default - and pruning with that would take a
    # configured 7-day record down to six hours on every restart.
    prune_max_age = _history_prunable_max_age(hass, hist)

    # Parse AND build the tracks off the loop: the retained window can be
    # hundreds of thousands of rows and this runs during setup.
    def _work():
        if prune_max_age is not None:
            history_mod.prune_segments(dirpath, prune_max_age)
        loaded = history_mod.PositionHistory(cfg)
        loaded.load_rows(history_mod.restore_recent(dirpath, cfg))
        # The rows came FROM disk; nothing here needs writing back out.
        loaded.drain_pending()
        return loaded

    try:
        async with _history_lock(hass):
            loaded = await hass.async_add_executor_job(_work)
    except Exception as e:
        _LOGGER.warning("Sextant position history could not be restored: %s", e)
        return
    hist.adopt(loaded)
    hist.mark_all_gaps()
    _seed_last_seen_from_history(hass)
    ents = hist.entities()
    _LOGGER.info("Sextant position history restored: %d points across %d things",
                 sum((hist.retained(e) or {}).get("points", 0) for e in ents), len(ents))


def _reading_max_age(data):
    """Seconds after which a distance reading is ignored (0 = never)."""
    if isinstance(data, dict):
        configured = data.get("reading_max_age")
        if isinstance(configured, (int, float)) and not isinstance(configured, bool) \
                and configured >= 0:
            return float(configured)
    return READING_MAX_AGE_SECS


def _reading_age_secs(state):
    """Age of a state in seconds, or None when it can't be determined.

    Uses ``last_updated`` (falling back to ``last_changed``) rather than
    ``last_reported``: Home Assistant only bumps ``last_updated`` when the
    value actually changes, so a sensor that keeps re-reporting the SAME stale
    distance still ages out — which is exactly the stuck-reading case. Returns
    None (i.e. "don't gate") if the timestamps are missing or unusable, so an
    unexpected state object can never blank out the whole map.
    """
    ts = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    if ts is None:
        return None
    try:
        return max(0.0, time.time() - ts.timestamp())
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def _thing_ref_offset(data, entity):
    """This thing's ref-power trim in dB (0.0 when unset). See issue #92."""
    if not isinstance(data, dict) or entity is None:
        return 0.0
    offsets = data.get("thing_ref_offsets")
    if not isinstance(offsets, dict):
        return 0.0
    value = offsets.get(entity)
    # not-bool: isinstance(True, int) holds in Python, so a hand-edited
    # true/false would otherwise read as a valid +1 dB trim.
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and abs(value) <= THING_REF_OFFSET_MAX_DB:
        return float(value)
    return 0.0


def _thing_distance_factor(data, entity):
    """Multiplicative distance scale from this thing's ref-power trim.

    A ref_power offset of `delta` dB scales every distance by
    10 ** (delta / (10 * attenuation)) in Bermuda's path-loss model, so a
    positive trim reads the thing as FARTHER and a negative one as nearer.
    Returns 1.0 (no-op) when no trim is configured.
    """
    offset = _thing_ref_offset(data, entity)
    if offset == 0.0:
        return 1.0
    return 10.0 ** (offset / (10.0 * PATH_LOSS_EXPONENT))


def _floor_scale(data, entity, floor_name):
    """Pixels-per-metre for a thing's elected floor (None if unknown)."""
    for ent in data:
        if ent.get("entity") == entity:
            for floor in ent["data"]["floor"]:
                if floor["name"] == floor_name:
                    return floor.get("scale")
    return None


def _jump_weight(r, prev_r, min_wr):
    """Soft down-weight for a radius that jumped since the previous update.

    Radii are clamped to the physical minimum for the comparison: a thing
    genuinely next to a receiver bounces between sub-clamp readings from pure
    RSSI noise (0.1 <-> 0.3 m is noise, not motion), and an unclamped relative
    gate would penalize that — its most informative receiver — every tick.
    The slant-collapse pathology needs no gate help: the projection floor
    keeps a collapsed radius constant at the clamp (rel = 1 either way) and
    the measured-slant weight radius bounds its influence in the solve.
    """
    if prev_r is None:
        return 1.0  # first sighting on this floor: no basis to distrust it
    r_eff, prev_eff = max(r, min_wr), max(prev_r, min_wr)
    rel = max(r_eff / prev_eff, prev_eff / r_eff)  # symmetric relative change, >= 1
    return 1.0 / (1.0 + ((rel - 1.0) / RADIUS_JUMP_TOL) ** 2)


def _kalman_position_update(entity, floor_name, meas, scale, bounds):
    """Constant-velocity Kalman filter on the trilaterated pixel position.

    ``meas`` is the newest ``(x, y)`` fix in this floor's pixel space. Noise is
    specified in metres via the module KF_* constants and scaled to pixels with
    ``scale`` (pixels per metre) so the smoothing is resolution-independent. The
    state is (re)initialised at the measurement with zero velocity whenever there
    is no prior state, the elected floor changed (coordinates live in a different
    pixel space), or the gap since the last fix exceeds KF_MAX_GAP_S (the thing
    was out of range, so its velocity is meaningless). Returns the filtered
    ``(x, y)`` clipped to ``bounds``; the raw fix is what feeds the filter, so the
    estimate is never biased by zone snapping applied downstream.
    """
    s = scale if isinstance(scale, (int, float)) and scale > 0 else 1.0
    r_var = (KF_MEAS_NOISE_M * s) ** 2          # measurement variance (px^2)
    a_var = (KF_ACCEL_NOISE_MS2 * s) ** 2       # accel variance (px^2/s^4)
    zx, zy = float(meas[0]), float(meas[1])
    now = time.time()
    st = _kf_position_state.get(entity)

    def _clip(px, py):
        if bounds is None:
            return px, py
        minx, miny, maxx, maxy = bounds
        return min(max(px, minx), maxx), min(max(py, miny), maxy)

    if st is None or st["floor"] != floor_name or now - st["ts"] > KF_MAX_GAP_S:
        v_var = (KF_INIT_VEL_UNC_MS * s) ** 2
        _kf_position_state[entity] = {
            "x": np.array([zx, zy, 0.0, 0.0], dtype=float),
            "P": np.diag([r_var, r_var, v_var, v_var]).astype(float),
            "ts": now,
            "floor": floor_name,
        }
        return _clip(zx, zy)

    elapsed = max(now - st["ts"], 1e-3)
    dt = min(elapsed, KF_MAX_DT_S)
    a_still = (KF_ACCEL_NOISE_STILL_MS2 * s) ** 2
    x, P, moving, nis = _kf_step(st["x"], st["P"], (zx, zy), dt, r_var, a_var, a_still,
                                 st.get("moving", 0), KF_MOVE_NIS, KF_MOVE_HOLD_S, elapsed=elapsed)
    st["x"], st["P"], st["ts"], st["floor"], st["moving"], st["nis"] = x, P, now, floor_name, moving, nis
    return _clip(float(x[0]), float(x[1]))


def _kf_step(x, P, meas, dt, r_var, a_var_move, a_var_still, moving, move_nis, hold_s, elapsed=None):
    """One constant-velocity Kalman step with two process-noise levels.

    Pure, so it can be replayed over a logged track and unit-tested: state in,
    state out. ``moving`` is how many more seconds the responsive noise is
    held for (0 = quiet); each quiet step spends the wall clock since the last
    (``elapsed``; ``dt`` itself is capped at KF_MAX_DT_S). The measurement is judged against the QUIET prediction:
    if its normalised innovation exceeds ``move_nis`` the thing has moved, the
    step is redone with the responsive noise so the estimate follows at once,
    and the hold is (re)armed. Returns ``(x, P, moving, nis)``.
    """
    F = np.array([[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
    H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
    R = np.diag([r_var, r_var]).astype(float)
    z = np.array([float(meas[0]), float(meas[1])], dtype=float)
    dt2 = dt * dt
    dt3 = dt2 * dt
    dt4 = dt3 * dt

    def predict(a_var):
        # Piecewise white-noise-acceleration process covariance, per axis.
        q_axis = np.array([[dt4 / 4.0, dt3 / 2.0], [dt3 / 2.0, dt2]]) * a_var
        Q = np.zeros((4, 4))
        Q[np.ix_([0, 2], [0, 2])] = q_axis  # x, vx
        Q[np.ix_([1, 3], [1, 3])] = q_axis  # y, vy
        return F @ x, F @ P @ F.T + Q

    def update(xp, Pp):
        S = H @ Pp @ H.T + R
        K = Pp @ H.T @ np.linalg.inv(S)
        return xp + K @ (z - H @ xp), (np.eye(4) - K @ H) @ Pp

    xq, Pq = predict(a_var_still)
    nu = z - H @ xq
    nis = float(nu @ np.linalg.inv(H @ Pq @ H.T + R) @ nu)
    if nis > move_nis:
        moving = hold_s                      # moved: follow now, and keep following for a while
    elif moving > 0:
        moving = max(0.0, moving - (dt if elapsed is None else elapsed))
    xn, Pn = update(*predict(a_var_move)) if moving > 0 else update(xq, Pq)
    return xn, Pn, moving, nis


def cleanup_legacy_sextant_registry_and_states(hass: HomeAssistant):
    """Remove legacy duplicated Sextant ids from entity registry and state machine."""
    entity_registry = er.async_get(hass)
    legacy_registry_ids = [
        entry.entity_id
        for entry in entity_registry.entities.values()
        if LEGACY_BPS_ENTITY_PATTERN.match(entry.entity_id)
    ]
    for entity_id in legacy_registry_ids:
        _LOGGER.info("Removing legacy Sextant registry entity: %s", entity_id)
        entity_registry.async_remove(entity_id)

    legacy_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if LEGACY_BPS_ENTITY_PATTERN.match(state.entity_id)
    ]
    for entity_id in legacy_state_ids:
        _LOGGER.info("Removing legacy Sextant state: %s", entity_id)
        hass.states.async_remove(entity_id)

# The last cycle failure and how many times it has repeated, so a persistent
# one is logged every so often instead of every fifteen seconds.
_cycle_error_last = None
_cycle_error_count = 0
CYCLE_ERROR_REPEAT_EVERY = 40   # roughly every ten minutes at a 15 s cycle
# thing -> cycles in a row its positioning has raised; rate-limits its log line.
_thing_error_counts = {}


async def update_tracked_entities(hass):
    """Update tracked_entities with the result of the Jinja code once per second."""
    global tracked_entities, new_global_data
    while True:
        # Receiver liveness and the self-localization accuracy sensor are
        # receiver-side diagnostics, independent of how many beacons are being
        # tracked — so they run on their own slow cadence at the TOP of the loop,
        # BEFORE the "too few things" early-continues below (otherwise a fresh
        # deploy, or any time nobody is home, would never publish them).
        now_ts = time.time()
        if now_ts - getattr(update_tracked_entities, "last_liveness", 0.0) >= RECEIVER_DUMP_INTERVAL:
            update_tracked_entities.last_liveness = now_ts
            try:
                await update_receiver_liveness(hass)
            except Exception as e:  # outside the cycle's try: raising here ended the loop for good
                _LOGGER.warning("Sextant receiver liveness update failed: %s", e)

        # Runs on the first tick too, so a fresh deploy shows a value quickly.
        # The scipy solves run in the executor so the loop is never blocked; the
        # calibration sample deques are snapshotted here on the loop thread first
        # to avoid a "mutated during iteration" race with calibration's ingest.
        # Skipped entirely while the accuracy sensor is not live in HA (the
        # user disabled it, or it has not been added yet): a leave-one-out
        # solve over every placed receiver is real work, and its only consumer
        # here is that sensor. The /api/sextant/selftest endpoint computes on
        # demand and is unaffected.
        if (
            now_ts - getattr(update_tracked_entities, "last_selftest", 0.0) >= SELFTEST_SENSOR_INTERVAL
            and _sensor_is_live(hass, ACCURACY_ENTITY_ID)
        ):
            update_tracked_entities.last_selftest = now_ts
            try:
                samples = {k: list(v) for k, v in get_calibration_state(hass).get("samples", {}).items()}
                result = await hass.async_add_executor_job(run_selftest, hass, samples)
                state, attrs = _selftest_summary(result)
                update_sextant_sensor_state(hass, ACCURACY_ENTITY_ID, state, attrs)
            except Exception as e:  # never let the diagnostic sensor stall tracking
                _LOGGER.warning("Sextant self-test sensor update failed: %s", e)

        # Persist the position history at a slow cadence (and prune expired day
        # segments hourly), so a restart does not lose the scrubback window.
        if now_ts - getattr(update_tracked_entities, "last_history_flush", 0.0) >= HISTORY_FLUSH_INTERVAL:
            update_tracked_entities.last_history_flush = now_ts
            prune_due = now_ts - getattr(update_tracked_entities, "last_history_prune", 0.0) >= 3600
            if prune_due:
                update_tracked_entities.last_history_prune = now_ts
            try:
                await flush_position_history(hass, prune=prune_due)
            except Exception as e:  # disk trouble must not stop tracking
                _LOGGER.warning("Sextant position history flush failed: %s", e)

        try:
            # This used to render a Jinja template that selected every
            # "sensor.*_distance_to_*" and then intersect it with the Bermuda
            # ones. The intersection was provably the second list: both walk
            # hass.states.async_all("sensor") and both require "_distance_to_"
            # in the id, so the template only ever added look-alikes from other
            # integrations for the next line to discard again. Rendering a
            # template over every sensor state, once a second, to compute a
            # superset of a list we already have was the single cost in this
            # loop that grew with the size of the user's Home Assistant rather
            # than with the number of beacons.
            tracked_entities = _bermuda_distance_sensor_ids(hass)

            await prune_stale_positions(hass)

            # Which devices to track, and how many usable readings exist.
            #
            # Both used to be derived from the distance entities, which meant
            # "trackable" really meant "has entities enabled". Ask Bermuda what
            # it is actually tracking instead: it creates those entities only
            # for devices with create_sensor set, so the two agree - except the
            # API answer still works when the entities are disabled.
            tracked_prefixes = bermuda_source.async_get_tracked_device_prefixes(hass)
            if tracked_prefixes is not None:
                # Bermuda's tracked set is authoritative: anything Sextant still
                # carries for a device outside it (untracked while HA was down)
                # is an orphan and goes. One set comparison per cycle.
                from .sensor import prune_sensors_for_untracked  # noqa: PLC0415 - sensor imports this package
                # Robots are Sextant's own things, not Bermuda's: keep theirs.
                prune_sensors_for_untracked(hass, set(tracked_prefixes) | set(robot_things(get_layout(hass))))
                unique_values = sorted(tracked_prefixes)
                readings = bermuda_source.async_get_readings(hass) or {}
                # Count device<->scanner pairs with a live distance, which is
                # what the entity count approximated before.
                num_points = sum(
                    1
                    for (prefix, _slug), reading in readings.items()
                    if prefix in tracked_prefixes and reading.get("distance") is not None
                )
            else:
                num_points = len(tracked_entities)
                unique_values = list(
                    {item.split("_distance_to_")[0].replace("sensor.", "") for item in tracked_entities}
                )

            if not unique_values:
                _LOGGER.info("There are no devices present to track, sleep 10 seconds")
                await asyncio.sleep(10)
                continue  # Skip and start over
            if num_points < 3:
                _LOGGER.info("There are not enough things with available data to track, sleep 10 seconds")
                await asyncio.sleep(10)
                continue  # Skip and start over
            # A thing added to Bermuda since setup has no sensors yet.
            from .sensor import ensure_sensors_for_things  # sensor.py imports this package
            ensure_sensors_for_things(hass, unique_values)
            # Use a separate copy per entity to avoid cross-entity mutation side effects.
            layout = get_layout(hass)
            _refresh_fingerprint_references(hass, layout, now_ts)
            new_global_data = [{"entity": ent, "data": _thing_layout(layout)} for ent in unique_values]

            started = time.perf_counter()
            await process_entities(hass, new_global_data)
            _cycle_last.update(at=time.time(), ms=round((time.perf_counter() - started) * 1000.0))
            async_dispatcher_send(hass, SIGNAL_BPS_UPDATE, _push_payload(hass))

        except Exception as e:  # noqa: BLE001 - one bad cycle must not end the loop
            # Loudly, and with the traceback. This used to log "Error executing
            # Jinja code" at INFO, where the recorder's WARNING+ log file never
            # showed it: a cycle that raised every fifteen seconds looked
            # exactly like a cycle that ran, and a restore that crashed the
            # elections was invisible for a day. Repeats of the same failure
            # are counted rather than repeated, so a persistent one does not
            # bury the log.
            global _cycle_error_last, _cycle_error_count
            message = f"{type(e).__name__}: {e}"
            if message == _cycle_error_last:
                _cycle_error_count += 1
                if _cycle_error_count % CYCLE_ERROR_REPEAT_EVERY == 0:
                    _LOGGER.error("Positioning cycle still failing (%d times): %s", _cycle_error_count, message)
            else:
                _cycle_error_last, _cycle_error_count = message, 1
                _LOGGER.exception("Positioning cycle failed: %s", message)

        await _wait_for_next_cycle()


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


def _bermuda_distance_sensor_ids(hass):
    """Entity ids of the ``sensor.*_distance_to_*`` sensors that actually belong
    to the ``bermuda`` integration.

    Other integrations expose look-alike distance sensors — e.g. an ESPHome
    mmWave presence sensor's ``..._distance_to_detection_object`` — which are not
    thing-to-scanner distances and must never feed Sextant. Every place that
    enumerates distance sensors (device tracking, the receiver/beacon debug
    views, the receiver picker) goes through this, mirroring the same
    ``platform == "bermuda"`` guard ``sensor.get_filtered_entities`` already
    applies to the sensor-creation path.
    """
    # Prefer Bermuda's direct API when available. This used to prefer the
    # entity REGISTRY instead (which survives entities being disabled), but
    # that stops working entirely once a user sets `create_scanner_entities
    # = False` on Bermuda's side: with no entities being created, there is
    # nothing in the registry to find, disabled or not. The synthetic ids
    # below aren't real entity_ids - every caller here only ever parses them
    # for their (device, scanner) halves, never resolves them back to an
    # actual entity - so a live-snapshot source works exactly as well and
    # needs no registry at all.
    if bermuda_source.async_api_available(hass):
        pairs = bermuda_source.async_get_snapshot_distance_pairs(hass)
        if pairs is not None:
            return pairs

    ent_reg = er.async_get(hass)
    ids = []
    for st in hass.states.async_all("sensor"):
        eid = st.entity_id
        if "_distance_to_" not in eid:
            continue
        entry = ent_reg.async_get(eid)
        if entry is None or entry.platform != "bermuda":
            continue
        ids.append(eid)
    return ids


def _scanner_slugs_and_readings(hass):
    """Single pass over Bermuda distance sensors: every scanner slug Bermuda
    exposes, and the subset that currently has a live reading (for the heuristic
    tier)."""
    allowed = set(_bermuda_distance_sensor_ids(hass))
    slugs = {eid.split("_distance_to_", 1)[1] for eid in allowed}

    # Which scanners currently have a live reading. With the distance entities
    # disabled there are no states to inspect, so ask Bermuda directly: a
    # non-None distance is exactly what a non-unknown entity state meant.
    readings = bermuda_source.async_get_readings(hass)
    if readings is not None:
        with_reading = {
            scanner_slug
            for (_device_prefix, scanner_slug), reading in readings.items()
            if reading.get("distance") is not None
        }
        return slugs, with_reading

    with_reading = set()
    for st in hass.states.async_all("sensor"):
        eid = st.entity_id
        if eid not in allowed:
            continue
        if st.state not in (None, "", "unknown", "unavailable"):
            with_reading.add(eid.split("_distance_to_", 1)[1])
    return slugs, with_reading


_SCANNER_TOKEN_RE = re.compile(r"^[0-9a-f]{5,12}$")


def _scanner_token(slug):
    """The trailing hardware id Bermuda embeds in a scanner slug — the hex group
    derived from the device's MAC, e.g. 'master_bedroom_esp32c5_f17464' ->
    'f17464'. This survives renames of the human-readable prefix, so it is a
    stable identity for a physical scanner. None when the slug has no hex tail.
    """
    if not slug:
        return None
    last = str(slug).rsplit("_", 1)[-1].lower()
    return last if _SCANNER_TOKEN_RE.match(last) else None


def _suggest_scanner(placement_slug, stored_uid, candidates):
    """Best live scanner slug to re-link a stale placement to. Prefer a shared
    hardware token (an explicitly-stored uid, else the placement's own trailing
    token — the same physical scanner after a rename); otherwise fall back to
    plain string similarity. Returns a candidate slug or None.
    """
    token = stored_uid or _scanner_token(placement_slug)
    if token:
        tok_matches = [c for c in candidates if _scanner_token(c) == token]
        if len(tok_matches) == 1:
            return tok_matches[0]
    best, best_score = None, 0.0
    for c in candidates:
        score = difflib.SequenceMatcher(None, str(placement_slug), c).ratio()
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 0.55 else None


_MAC_SUFFIX_RE = re.compile(r"_([0-9a-f]{2}(?:_[0-9a-f]{2}){5})$")


def _mac_tail_matches(token, mac):
    """True when a placed slug's hex token is the tail of a MAC/uid."""
    if not token or not mac:
        return False
    return str(mac).lower().replace(":", "").endswith(str(token).lower())


def _resolve_receiver_addresses(layout, directory):
    """Give every placed receiver its scanner ADDRESS as identity.

    Receivers used to be identified by their Bermuda slug alone, which is a
    slugified DEVICE NAME: rename the device (or let Bermuda append a MAC to
    disambiguate it) and the placement silently unlinked, while the
    calibration and linking views grew heuristics to guess the match back.
    A scanner's address never changes, so it is the identity and the slug is
    just the label.

    For each receiver in ``layout`` (mutated in place):
      - a stored ``address`` present in ``directory`` is trusted, and the
        receiver's ``entity_id`` label is refreshed to that scanner's CURRENT
        slug, so a rename follows through everywhere the label is shown or
        used as a key;
      - otherwise the address is resolved by exact slug, then by the
        hardware token in the slug / ``scanner_uid`` against the scanners'
        BLE address, wifi mac and unique_id; only a unique candidate that no
        other placement already claims is taken.

    Returns ``(changed, unresolved)``: whether anything was written, and the
    slugs that could not be resolved (left as they were; slug lookups still
    work for them).
    """
    changed = False
    unresolved = []
    if not isinstance(layout, dict) or not isinstance(directory, dict):
        return changed, unresolved
    claimed = set()
    receivers = [
        r for fl in layout.get("floor") or [] if isinstance(fl, dict)
        for r in fl.get("receivers") or [] if isinstance(r, dict)
    ]
    for receiver in receivers:
        address = receiver.get("address")
        if isinstance(address, str) and address.lower() in directory:
            claimed.add(address.lower())
    for receiver in receivers:
        slug = str(receiver.get("entity_id") or "")
        address = receiver.get("address")
        address = address.lower() if isinstance(address, str) else None
        if address in directory:
            live_slug = directory[address].get("slug")
            if live_slug and live_slug != slug:
                receiver["entity_id"] = live_slug
                changed = True
            if receiver.get("address") != address:
                receiver["address"] = address
                changed = True
            continue
        if not slug:
            continue
        candidates = [a for a, s in directory.items() if s.get("slug") == slug and a not in claimed]
        if not candidates:
            # Bermuda disambiguates duplicate scanner names by appending the
            # MAC, e.g. "sewing_room_rrn00_4e893c_dc_06_75_4e_89_3e". That
            # suffix IS the address; and once the duplicate is gone Bermuda
            # drops it again, so the plain token must be tried on the slug
            # with the suffix stripped.
            mac_suffix = _MAC_SUFFIX_RE.search(slug)
            if mac_suffix:
                mac = mac_suffix.group(1).replace("_", ":")
                if mac in directory and mac not in claimed:
                    candidates = [mac]
            base_slug = slug[: mac_suffix.start()] if mac_suffix else slug
            token = receiver.get("scanner_uid") or _scanner_token(base_slug)
            # Only when the suffix gave no exact answer: the BLE and wifi MAC
            # tails differ, so the token can miss (or double-match) an address
            # the suffix names outright.
            if token and not candidates:
                candidates = [
                    a for a, s in directory.items()
                    if a not in claimed and (
                        _mac_tail_matches(token, a)
                        or _mac_tail_matches(token, s.get("address_wifi_mac"))
                        or _mac_tail_matches(token, s.get("unique_id"))
                        or _scanner_token(s.get("slug") or "") == token
                    )
                ]
        if len(candidates) == 1:
            found = candidates[0]
            receiver["address"] = found
            claimed.add(found)
            live_slug = directory[found].get("slug")
            if live_slug and live_slug != slug:
                receiver["entity_id"] = live_slug
            changed = True
        else:
            unresolved.append(slug)
    return changed, unresolved


async def async_resolve_receiver_addresses(hass) -> bool:
    """Resolve/refresh receiver addresses against Bermuda and persist changes.

    Cheap when nothing changed (a dict walk over the placements), so it runs
    at startup and again every liveness tick: Bermuda may not have known a
    scanner yet at boot, and a rename should be picked up within seconds.
    """
    directory = bermuda_source.async_get_scanner_directory(hass)
    if not directory:
        return False
    layout = get_layout(hass)
    if not isinstance(layout, dict):
        return False
    # Only receivers' own keys are written, so a receivers-only copy is a
    # safe probe; a deepcopy of the whole layout (rooms, bias grids, pins)
    # every liveness tick was the real cost of this "cheap" check.
    probe = _thing_layout(layout)
    changed, _unresolved = _resolve_receiver_addresses(probe, directory)
    if not changed:
        return False
    async with LAYOUT_LOCK:
        data = get_layout_for_edit(hass)
        if not isinstance(data, dict):
            return False
        changed, unresolved = _resolve_receiver_addresses(data, directory)
        if not changed:
            return False
        await save_layout(hass, data)
    _LOGGER.info(
        "Receiver identities refreshed from Bermuda (%s unresolved: %s)",
        len(unresolved), ", ".join(unresolved) if unresolved else "none",
    )
    return True


def _placed_receivers(coordinates_json):
    """Placed receivers as (floor_name, entity_id slug, stored scanner_uid, address).

    Parsed defensively: a malformed or hand-edited bpsdata.txt yields [] rather
    than raising into a caller (a bad layout must never break the diagnostics). Each
    entity_id must be a non-empty string — a non-string slug isn't a real
    scanner name and would be unhashable when callers build a set of slugs.
    """
    placed = []
    try:
        parsed = json.loads(coordinates_json)
        floors = parsed.get("floor") if isinstance(parsed, dict) else None
        for fl in floors if isinstance(floors, list) else []:
            if not isinstance(fl, dict):
                continue
            receivers_ = fl.get("receivers")
            for rec in receivers_ if isinstance(receivers_, list) else []:
                if isinstance(rec, dict) and isinstance(rec.get("entity_id"), str) and rec["entity_id"]:
                    address = rec.get("address")
                    placed.append((fl.get("name"), rec["entity_id"], rec.get("scanner_uid"),
                                   address.lower() if isinstance(address, str) else None))
    except Exception:
        return []
    return placed


# Distance-sensor states that mean "no distance right now" (as opposed to a
# real numeric reading). Shared by the diagnostics and the linking debug view.
_NO_DISTANCE_STATES = (None, "", "unknown", "unavailable")


def _scanner_diagnostics(hass, coordinates_json):
    """Compare placed receivers against the scanner slugs Bermuda actually
    exposes, to flag naming mismatches (issue #64):
      - unmatched_receivers: a placed slug that has NO matching distance sensor
        (a genuinely wrong/stale name), each with a suggested live scanner.
      - unplaced_scanners: scanner slugs currently reporting a distance that
        aren't placed on any floor.
    """
    slugs, with_reading = _scanner_slugs_and_readings(hass)
    placed = _placed_receivers(coordinates_json)
    placed_slugs = {p[1] for p in placed}
    directory = bermuda_source.async_get_scanner_directory(hass) or {}
    # Candidates for a re-link: live scanner slugs not already correctly placed.
    free = [s for s in slugs if s not in placed_slugs]
    unmatched = []
    for fname, slug, uid, address in placed:
        if address and address in directory:
            continue  # identified by address: linked whatever its label says
        if slug in slugs:
            continue  # a real sensor exists (offline is a separate concern)
        unmatched.append({
            "entity_id": slug,
            "floor": fname,
            "suggested": _suggest_scanner(slug, uid, free),
        })
    unplaced = sorted(with_reading - placed_slugs)
    return {"unmatched_receivers": unmatched, "unplaced_scanners": unplaced}


def _scanner_linking(hass, coordinates_json):
    """Debug view for issue #64: for every placed receiver, the Bermuda distance
    sensors that feed it and each one's current state.

    This distinguishes the two failure modes David couldn't tell apart from the
    map alone: a receiver correctly linked but simply not reporting a distance
    right now ("silent" — usually just no recent BLE contact), versus one whose
    name matches no distance sensor at all ("unmatched" — a real naming
    mismatch). Reads live HA state directly, so it does not depend on a device
    being actively tracked. Returns:
      - placed:   one row per placed receiver with its status and per-device
                  readings (the exact `<device>_distance_to_<slug>` sensors
                  update_receiver_radii looks up).
      - unplaced: scanner slugs that have distance sensors but no placement,
                  for context (a superset of the diagnostics' "reporting" list).
    """
    by_slug = {}
    readings = bermuda_source.async_get_readings(hass)
    if readings is not None:
        # Prefer live readings over hass.states: with the distance entities
        # disabled (or, since create_scanner_entities=False, not created at
        # all) there is no entity state to read regardless of what
        # _bermuda_distance_sensor_ids returns. A reading exists here for
        # every (device, scanner) pair Bermuda has EVER built an advert for,
        # with distance=None once that reading times out - which is exactly
        # the "silent" (linked but not reporting) signal below, the same as
        # a disabled entity holding its last state indefinitely used to be.
        for (device_slug, scanner_slug), reading in readings.items():
            eid = f"sensor.{device_slug}_distance_to_{scanner_slug}"
            state = None if reading.get("distance") is None else str(reading["distance"])
            by_slug.setdefault(scanner_slug, []).append({"device": device_slug, "entity_id": eid, "state": state})
    else:
        allowed = set(_bermuda_distance_sensor_ids(hass))
        for st in hass.states.async_all("sensor"):
            eid = st.entity_id
            if eid not in allowed:
                continue
            device_part, slug = eid.split("_distance_to_", 1)
            device = device_part[len("sensor."):] if device_part.startswith("sensor.") else device_part
            state = None if st.state is None else str(st.state)
            by_slug.setdefault(slug, []).append({"device": device, "entity_id": eid, "state": state})

    def _reporting(sensors):
        return [s for s in sensors if s["state"] not in _NO_DISTANCE_STATES]

    placed = _placed_receivers(coordinates_json)
    placed_slugs = {p[1] for p in placed}
    rows = []
    directory = bermuda_source.async_get_scanner_directory(hass) or {}
    for fname, slug, uid, address in placed:
        # Identified by address: read the sensors under the scanner's CURRENT
        # slug, so a placement whose label lags a rename still shows as linked.
        live_slug = directory.get(address, {}).get("slug") if address else None
        sensors = sorted(by_slug.get(live_slug or slug) or by_slug.get(slug, []), key=lambda s: s["device"])
        reporting = _reporting(sensors)
        if not sensors and not (address and address in directory):
            status = "unmatched"
        elif reporting:
            status = "live"
        else:
            status = "silent"
        rows.append({
            "entity_id": slug,
            "floor": fname,
            "scanner_uid": uid,
            "address": address,
            "token": _scanner_token(slug),
            "status": status,
            "sensor_count": len(sensors),
            "reporting_count": len(reporting),
            "sensors": sensors,
        })
    unplaced = []
    for slug, sensors in by_slug.items():
        if slug in placed_slugs:
            continue
        unplaced.append({
            "entity_id": slug,
            "token": _scanner_token(slug),
            "sensor_count": len(sensors),
            "reporting_count": len(_reporting(sensors)),
        })
    unplaced.sort(key=lambda u: u["entity_id"])
    return {"placed": rows, "unplaced": unplaced}


def _beacon_links(hass):
    """Debug view: for every tracked device (beacon), the receivers currently
    detecting it, sorted closest -> farthest. The inverse of _scanner_linking
    (grouped by the tracked device instead of by the scanner). Distances are
    normalized to metres only for sorting — Bermuda reports per-entity feet or
    metres — while the value is shown in its own unit. A beacon with no live
    reading still appears (empty list) so a device that's gone dark is visible.
    """
    beacons = {}  # device -> [{scanner, distance, unit}]
    readings = bermuda_source.async_get_readings(hass)
    if readings is not None:
        # Prefer live readings over hass.states - see _scanner_linking for
        # why hass.states cannot work here at all once entities are disabled
        # or, with create_scanner_entities=False, never created. The API
        # reports distance in metres always, so no unit conversion is needed.
        for (device_slug, scanner_slug), reading in readings.items():
            beacons.setdefault(device_slug, [])
            distance = reading.get("distance")
            if distance is None:
                continue
            beacons[device_slug].append({
                "scanner": scanner_slug,
                "distance": round(distance, 2),
                "unit": "m",
                "_m": distance,
            })
    else:
        allowed = set(_bermuda_distance_sensor_ids(hass))
        for st in hass.states.async_all("sensor"):
            eid = st.entity_id
            if eid not in allowed:
                continue
            device_part, slug = eid.split("_distance_to_", 1)
            device = device_part[len("sensor."):] if device_part.startswith("sensor.") else device_part
            beacons.setdefault(device, [])
            if st.state in _NO_DISTANCE_STATES:
                continue
            try:
                val = float(st.state)
            except (ValueError, TypeError):
                continue
            unit = st.attributes.get("unit_of_measurement")
            meters = val * 0.3048 if unit == "ft" else val
            beacons[device].append({
                "scanner": slug,
                "distance": round(val, 2),
                "unit": unit if isinstance(unit, str) and unit else "m",
                "_m": meters,
            })
    out = []
    for device in sorted(beacons):
        recs = sorted(beacons[device], key=lambda r: r["_m"])
        for r in recs:
            r.pop("_m", None)  # internal sort key only
        out.append({"device": device, "receivers": recs})
    return out


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


def _iter_registry_devices(dev_reg):
    """Every DeviceEntry in the device registry, on any supported HA core.

    ``dev_reg.devices.values()`` is deprecated (removed in HA 2027.9): the
    registry's ``devices`` is now a view whose supported use is plain
    iteration, which yields entries. On cores older than that view, iterating
    yields device ids (it was a mapping), so those are resolved through the
    mapping - which on such cores is not deprecated.
    """
    devices = dev_reg.devices
    for item in devices:
        if isinstance(item, str):
            yield devices[item]
        else:
            yield item


def _build_device_availability(hass):
    """Maps for the device-availability tier: slug -> device, device -> entities.

    A device slug that isn't unique is dropped (ambiguous) so a stale/duplicate
    device can't decide a receiver's status.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    slug_to_device = {}
    ambiguous = set()
    for d in _iter_registry_devices(dev_reg):
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
      4. Distance heuristic (last resort): working if some thing got a reading
         through it recently. Only reached for a scanner with no liveness and no
         mapped device (6 of this install's receivers, whose device name doesn't
         slugify to the receiver id). An *up* scanner is caught by tier 1, so
         this can't misfire when every tracked device leaves home — it only
         decides a scanner that is both absent from the dump and deviceless.
    A receiver no tier can resolve at all is left ONLINE (never flagged).
    """
    dom = hass.data.setdefault(DOMAIN, {})
    # Receivers are identified by scanner address; keep the placements'
    # addresses and labels in step with Bermuda (a rename, or a scanner
    # Bermuda only learned about after Sextant started).
    try:
        await async_resolve_receiver_addresses(hass)
    except Exception as e:  # identity upkeep must never stop liveness
        _LOGGER.debug("Receiver identity refresh failed: %s", e)
    # Tier 1's ages come straight from Bermuda's scanner set when its API
    # exposes them: one small dict, already relative to "now", no service
    # call. The dump_devices path below is the fallback for a Bermuda build
    # without that feature - it serialises every scanner's whole advert
    # table on every poll just to reach one last_seen per scanner.
    api_ages = bermuda_source.async_get_scanner_ages(hass)
    if api_ages is not None:
        dom["rl_ages"] = api_ages
        dom["rl_anchor_wall"] = time.time()
        dom["rl_newest"] = None  # not a dump; nothing to re-anchor against
    else:
        try:
            devices = await wait_for(
                hass.services.async_call(
                    "bermuda", "dump_devices", {"configured_devices": True},
                    blocking=True, return_response=True,
                ),
                timeout=DUMP_DEVICES_TIMEOUT_S,
            ) or {}
        except TimeoutError:
            _LOGGER.warning(
                "Receiver liveness: bermuda.dump_devices timed out after %ss; "
                "skipping this refresh", DUMP_DEVICES_TIMEOUT_S,
            )
            devices = None
        except Exception as e:
            _LOGGER.info(f"Receiver liveness: dump_devices unavailable: {e}")
            devices = None

        if isinstance(devices, dict):
            _refresh_dump_ages(hass, dom, devices)
    ages = dom.get("rl_ages", {})
    elapsed = max(0.0, time.time() - dom["rl_anchor_wall"]) if dom.get("rl_anchor_wall") else 0.0

    receivers, with_reading = _scanner_slugs_and_readings(hass)
    slug_to_device, device_entities = _build_device_availability(hass)
    # Placed receivers by address: the placement's own label is what the
    # panel and card match on, so the offline list stays keyed by it, but
    # the liveness lookup goes through the address when there is one.
    directory = bermuda_source.async_get_scanner_directory(hass) or {}
    placed_address = {}
    layout = get_layout(hass)
    if isinstance(layout, dict):
        for fl in layout.get("floor") or []:
            for rec in (fl.get("receivers") or []) if isinstance(fl, dict) else []:
                if isinstance(rec, dict) and isinstance(rec.get("entity_id"), str) and isinstance(rec.get("address"), str):
                    placed_address[rec["entity_id"]] = rec["address"].lower()
        receivers = set(receivers) | set(placed_address)

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
        address = placed_address.get(slug)
        if address and address in directory:
            age = directory[address].get("last_seen_age")
            if isinstance(age, (int, float)):
                return age <= RECEIVER_OFFLINE_SECS
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
        # here is online only while some thing reads a distance through it.
        return slug in with_reading

    dom["rl_offline"] = sorted(s for s in receivers if not is_online(s))


async def update_receiver_radii(hass, eids):
    """Update receiver 'r' values (pixels) and raw 'distance' (meters) for an entity"""
    thing_h = _thing_height(eids["data"], eids["entity"])
    thing_factor = _thing_distance_factor(eids["data"], eids["entity"])
    max_age = _reading_max_age(eids["data"])
    use_median = _tuning(eids["data"], "distance_estimator") == "median"
    median_window = _tuning(eids["data"], "median_window_secs")
    median_min = _tuning(eids["data"], "median_min_samples")
    # Prefer Bermuda's direct API: it serves the same per-scanner readings from
    # memory without any of the distance_to entities existing, which avoids
    # thousands of recorder writes and websocket state_changed fan-outs. None
    # when Bermuda is absent or too old, in which case we scrape entities as
    # before. Fetched once per call, not per receiver.
    # Address-keyed readings need no slug map and cannot drift on a rename;
    # the slug-keyed dict remains for placements not yet resolved, and is
    # only built (a device x scanner join) once one of those is met.
    by_address = bermuda_source.async_get_readings_by_address(hass, include_history=use_median)
    readings = None
    readings_built = by_address is None
    if readings_built:
        readings = bermuda_source.async_get_readings(hass, include_history=use_median)
    for floor in (f for f in eids["data"]["floor"] if f["scale"] is not None):
        for receiver in floor["receivers"]:
            if not isinstance(receiver.get("cords"), dict):
                continue  # not placed: no circle to size, and cords["r"] below would raise
            entity_id = "sensor." + eids["entity"] + "_distance_to_" + receiver["entity_id"]
            reading = None
            address = receiver.get("address")
            if by_address is not None and isinstance(address, str) and address:
                reading = by_address.get((eids["entity"], address.lower()))
            if reading is None and not readings_built:
                readings_built = True
                readings = bermuda_source.async_get_readings(hass, include_history=use_median)
            if reading is None and readings is not None:
                reading = readings.get((eids["entity"], receiver["entity_id"]))
            # Per-point reliability of this reading in (0, 1]; only the
            # median estimator has a basis to rate one below 1.
            quality = 1.0
            if reading is not None:
                # Direct path. Distance is already metres (the API never uses
                # the user's display units), and age is seconds since the
                # scanner last actually HEARD the device rather than since the
                # value last changed - a stronger stuck-reading signal than the
                # entity path can give. A None distance is Bermuda's own
                # "this scanner can no longer hear it" timeout.
                distance_m = reading["distance"]
                age = reading["age"]
                if use_median and distance_m is not None:
                    estimate = _median_distance(reading, median_window, median_min)
                    if estimate is not None:
                        distance_m, quality = estimate
                if distance_m is None:
                    receiver.pop("distance", None)
                    continue
            else:
                rec_value = hass.states.get(entity_id)
                if rec_value is None:
                    continue
                age = _reading_age_secs(rec_value)
                try:
                    distance_m = float(rec_value.state)
                except (TypeError, ValueError):
                    continue
                # Bermuda's distance_to sensors can report in feet or
                # meters, chosen per entity. The floor scale and the
                # calibration corrections are both in meters, so normalize
                # to meters first — otherwise a feet sensor reads ~3.28x too
                # far (treated as metres), inflating its circle and pulling
                # the trilateration toward it.
                unit = rec_value.attributes.get("unit_of_measurement")
                if unit in DistanceConverter.VALID_UNITS and unit != UnitOfLength.METERS:
                    distance_m = DistanceConverter.convert(distance_m, unit, UnitOfLength.METERS)

            # Drop a STUCK reading: when a scanner stops hearing the
            # thing its distance sensor keeps the last value instead of
            # going unavailable, and that frozen radius would anchor the
            # fix to a receiver that can no longer see the device. Removing
            # "distance" takes this receiver out of the cycle's candidate
            # solve (see extract_candidate_floors).
            if max_age and age is not None and age > max_age:
                receiver.pop("distance", None)
                _LOGGER.debug(
                    "Ignoring stale distance for %s (%.0fs old, max %.0fs)",
                    entity_id, age, max_age,
                )
                continue
            try:
                distance = distance_m
                # Per-receiver correction factor learned by the
                # calibration (calibration.py); equivalent to a
                # per-scanner RSSI offset in Bermuda's exponential model.
                correction = receiver.get("correction")
                if isinstance(correction, (int, float)) and correction > 0:
                    distance = distance * _close_range_correction(float(correction), distance, eids["data"])
                # Per-THING ref-power trim (issue #92): a tag whose
                # transmit power differs from Bermuda's configured
                # ref_power reads consistently long or short from EVERY
                # receiver, which no per-receiver correction can fix.
                # Applied before the slant leg so the height geometry sees
                # the trimmed range, and included in the election distance
                # below (a per-thing constant, so cross-floor ordering
                # for this thing is unchanged).
                distance = distance * thing_factor
                # Known mount height: the estimate is a slant range, so
                # remove the vertical leg (mount height vs the assumed
                # thing height) to get the horizontal distance the 2D
                # solve actually needs. A slant shorter than the vertical
                # leg means "practically underneath" — horizontal ~ 0; the
                # solver's MIN_WEIGHT_RADIUS_M clamp keeps such a near-zero
                # radius from monopolizing the fit. The range guard also
                # rejects NaN/Infinity from a hand-edited data file (NaN
                # fails both comparisons), which would otherwise poison
                # every solve on the floor.
                horizontal = distance
                height = receiver.get("height")
                if isinstance(height, (int, float)) and 0 <= height <= 10:
                    dz = float(height) - thing_h
                    # Floored: sqrt(d^2 - dz^2) has a singularity at
                    # d -> dz where its sensitivity blows up, and any
                    # d <= dz collapsed to EXACTLY 0. Bermuda's filtered
                    # distances are sustainedly biased low, so a filtered
                    # slant could sit below dz for many cycles and the
                    # collapsed radius (clamped to min weight radius at
                    # ~100x the weight of a 5 m receiver) dragged the fix
                    # onto that receiver — the 1.7.0 accuracy regression.
                    # The floor never exceeds the raw slant itself, so a
                    # receiver at ~thing height (dz ~ 0, no singularity)
                    # keeps honest sub-floor readings like the no-height
                    # path does.
                    floor_sq = min(distance * distance,
                                   MIN_WEIGHT_RADIUS_M * MIN_WEIGHT_RADIUS_M)
                    horizontal = math.sqrt(max(distance * distance - dz * dz, floor_sq))
                receiver["cords"]["r"] = floor["scale"] * horizontal
                # Raw SLANT distance for the floor election: radii are in
                # per-floor pixel scales and must not be compared across
                # floors — and the dz correction must not leak in here
                # either. sqrt(d^2 - dz^2) is only valid when the thing
                # is on the receiver's own floor, which is exactly what
                # the election hasn't decided yet: electing on corrected
                # values lets a high-mounted probe hearing the thing
                # through the slab shrink its through-floor slant and
                # steal the election from the correct floor.
                receiver["distance"] = distance
                receiver["raw_distance"] = distance_m  # before correction and trim, for truth marks
                receiver["quality"] = quality
            except ValueError:
                #_LOGGER.info(f"Invalid numerical value: {rec_value.state}")
                pass

async def update_trilateration_and_zone(hass, new_global_data, entity):
    """Trilateration with floor hypothesis competition, soft radius-jump
    weighting and Kalman position smoothing.

    The floor used to be elected by the single nearest receiver before any
    position existed — one noisy reading through a ceiling could steal the
    thing for a cycle (issue #94). Now the top FLOOR_CANDIDATES floors (by
    nearest receiver) are each SOLVED, scored by how well the fix explains
    that floor's whole receiver ensemble, folded into smoothed per-floor
    probabilities, and elected with incumbent hysteresis. The winning floor's
    fix continues into the unchanged Kalman/zone/publish pipeline.
    """
    global apitricords

    # Store last r-values per sensor and entity (for soft radius-jump weighting).
    if not hasattr(update_trilateration_and_zone, "last_r_values"):
        update_trilateration_and_zone.last_r_values = {}
    if not hasattr(update_trilateration_and_zone, "last_floor"):
        update_trilateration_and_zone.last_floor = {}

    candidates = extract_candidate_floors(new_global_data, entity)

    if not candidates:
        # No receiver reports any distance for this device: it is out of
        # range. The zone/floor sensors keep their last value (historical
        # behavior), but nearest-zone explicitly reports unknown.
        update_sextant_sensor_state(hass, f"sensor.{entity}_sextant_nearest_room", "unknown")
        return

    # Get previous r-values for this entity
    last_r = update_trilateration_and_zone.last_r_values.get(entity, {})

    # Remember radii for EVERY floor with data — not only the ones solved
    # below — so a floor stays jump-gated even while unelected or briefly
    # pushed out of the candidate cut (keys include the floor: radii are in
    # per-floor pixel scales and must not be compared across floors).
    new_last_r = {}
    for cand in candidates:
        new_last_r.update({(cand["name"], pt[0], pt[1]): pt[2] for pt in cand["cords"]})

    incumbent = update_trilateration_and_zone.last_floor.get(entity)

    # Only floors with enough receivers to trilaterate compete for the solve
    # slots: an unsolvable floor whose single through-slab receiver reads
    # short must not consume a slot and push the only solvable floor out.
    # The incumbent, when solvable, ALWAYS defends its title — even ranked
    # below the cut — so nearest-slant noise alone can never evict it.
    layout = _layout_for(new_global_data, entity)
    estimator = _thing_estimator(layout, entity)
    refs_by_floor = {}
    thing_vec = {}
    fp_gain = 1.0
    own_weight = _thing_fp_weight(layout, entity)
    if estimator != "geometric":
        _seed_thing_gain(layout, entity)
        fp_gain = _tuning(layout, "fingerprint_ref_gain")
        if _tuning(layout, "fingerprint_auto_gain"):
            fp_gain *= _fingerprint_db.gain_for(entity)
        refs_by_floor = fingerprint.build_references(layout, _fingerprint_db.vectors(), fp_gain, extra=_mark_refs(layout, entity))
        thing_vec = fingerprint.thing_vector(layout)
    # A floor with references can compete on its fingerprint with a single
    # receiver hearing the thing; trilateration alone needs three.
    solvable = [
        c for c in candidates
        if len(c["cords"]) >= 3 or (thing_vec and c["name"] in refs_by_floor)
    ]
    to_solve = solvable[:FLOOR_CANDIDATES]
    if incumbent is not None and not any(c["name"] == incumbent for c in to_solve):
        inc_cand = next((c for c in solvable if c["name"] == incumbent), None)
        if inc_cand is not None:
            to_solve.append(inc_cand)

    # Phase 1 (event loop): prepare every candidate floor's solve inputs.
    # Phase 2 (executor): the solves themselves - the only real CPU work in
    # the cycle - run off the loop, all of this thing's floors in one job.
    # Phase 3 (event loop): election, filter, zones, publish.
    jobs = []
    for cand in to_solve:
        floor_name, cords = cand["name"], cand["cords"]

        # Physical floor for the geometric weight and the jump comparison, in
        # this floor's pixels. Slant correction (known mount heights) makes
        # near-zero radii a normal reading, and 1/r^2 must not hand one such
        # receiver the whole fit.
        scale = _floor_scale(new_global_data, entity, floor_name)
        min_wr = MIN_WEIGHT_RADIUS_M * scale if scale else 1e-3

        # Soft radius-jump weighting (replaces the old hard 50% discard). A
        # receiver whose radius jumped versus its previous update is
        # DOWN-WEIGHTED rather than dropped, so the solver keeps enough points
        # to fix a position even while every distance is legitimately changing
        # during movement. The measured slant (px) rides along as each point's
        # weight radius, so the solver's 1/r^2 weight reflects what was
        # MEASURED — a projection collapsed to the minimum can't buy influence.
        weighted = []
        min_jump_w = 1.0
        for pt in cords:
            x, y, r, slant_px = pt[0], pt[1], pt[2], pt[3]
            quality = pt[4] if len(pt) > 4 else 1.0
            w = _jump_weight(r, last_r.get((floor_name, x, y)), min_wr)
            # The reading's own reliability (sample count behind a median)
            # multiplies the temporal gate: both are "how much to trust this
            # radius", from independent evidence.
            weighted.append((x, y, r, w * quality, slant_px))
            min_jump_w = min(min_jump_w, w)

        # The device cannot be outside the floor: bound the solver to the
        # extent of the floor's receivers and zones (with some margin) so the
        # fitted position is the best point WITHIN the map, not a runaway fix
        # that would need clamping afterwards.
        zone_polys = _floor_zone_polygons(hass, new_global_data, entity, floor_name)
        xs = [p[0] for p in weighted]
        ys = [p[1] for p in weighted]
        for _zone_id, polygon, _buffer_size, _no_go in zone_polys:
            minx, miny, maxx, maxy = polygon.bounds
            xs.extend((minx, maxx))
            ys.extend((miny, maxy))
        margin = 0.1 * max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        floor_bounds = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)

        # Nothing here has moved much since last cycle (every receiver's jump
        # weight says so) and last cycle settled on this same floor: try
        # continuing from there first instead of a fresh multi-start battery.
        # See STABLE_HINT_MIN_JUMP_WEIGHT and trilaterate()'s stable_hint.
        kf_state = _kf_position_state.get(entity)
        stable_hint = None
        if (
            min_jump_w >= STABLE_HINT_MIN_JUMP_WEIGHT
            and kf_state is not None
            and kf_state.get("floor") == floor_name
        ):
            stable_hint = (float(kf_state["x"][0]), float(kf_state["x"][1]))

        jobs.append({
            "floor": floor_name, "weighted": weighted, "bounds": floor_bounds,
            "heard": cand.get("heard") or [],
            "min_wr": min_wr, "stable_hint": stable_hint, "scale": scale,
            "zone_polys": zone_polys,
            "fingerprint": None if not thing_vec or floor_name not in refs_by_floor else {
                "mode": estimator,
                "thing": thing_vec,
                "refs": refs_by_floor[floor_name],
                "k": _tuning(layout, "fingerprint_k"),
                "missing_m": _tuning(layout, "fingerprint_missing_m"),
                "weight": own_weight if own_weight is not None else _tuning(layout, "fingerprint_weight"),
                "floor_weight": own_weight if own_weight is not None else _tuning(layout, "fingerprint_floor_weight"),
                "gain": fp_gain,
            },
        })
    # Keep this cycle's inputs so a truth mark can re-solve it under other settings.
    if jobs:
        _truth_buffer.remember(entity, jobs, thing_vec if estimator != "geometric" else fingerprint.thing_vector(layout), fp_gain, estimator,
                               raw_vec=_raw_vector(layout))

    solved = {}  # floor name -> everything the publish pipeline needs
    if jobs:
        # asyncio.gather of synchronous work is sequential: with the solves
        # inline, every thing's fits ran back to back on the event loop in
        # one burst per cycle. On a Pi that burst visibly stalled HA; this is
        # why the upstream interval had to go from 1 s to 15 s.
        results = await hass.async_add_executor_job(_solve_floor_jobs, jobs)
    else:
        results = []
    for job, outcome in zip(jobs, results, strict=False):
        if outcome is None:
            continue  # this floor's readings don't converge; not a contender
        fix, conf, rms_m, fp = outcome
        floor_name, weighted, floor_bounds = job["floor"], job["weighted"], job["bounds"]
        zone_polys, scale = job["zone_polys"], job["scale"]
        solved[floor_name] = {
            "fix": fix,
            "weighted": weighted,
            "heard": job.get("heard") or [],   # (proxy, metres) behind this floor's solve
            "bounds": floor_bounds,
            "zone_polys": zone_polys,
            "scale": scale,
            "conf": conf,
            "rms_m": rms_m,     # kept for the elected floor's telemetry payload
            "fp": fp,           # fingerprint match telemetry (None when unused)
        }

    # Store current r-values for next time
    update_trilateration_and_zone.last_r_values[entity] = new_last_r

    if not solved:
        # No candidate floor has three converging receivers (historical
        # behavior: sensors keep their last value until pruned).
        return

    valid_floors = {f["name"] for f in (layout or {}).get("floor", [])}

    # The elected floor was renamed or deleted in the data file: that is a
    # change of world, not a dark blip — routing it into the grace below
    # would freeze the ghost name for the grace period and then elect
    # whichever OTHER floor inherited the stale probability mass. Reset the
    # whole election state instead (exactly like a prune), so this cycle's
    # fresh scores elect the renamed floor immediately.
    if incumbent is not None and incumbent not in valid_floors:
        _floor_probability.pop(entity, None)
        _floor_probability_at.pop(entity, None)
        _floor_challenge.pop(entity, None)
        _floor_dark_cycles.pop(entity, None)
        _floor_since.pop(entity, None)
        update_trilateration_and_zone.last_floor.pop(entity, None)
        incumbent = None

    # Dark-incumbent grace: the elected floor blipping below three receivers
    # (or its solve failing) for a few cycles is a sensor hiccup, not
    # evidence the thing moved — hold everything frozen: last published
    # values stand, probabilities are NOT updated (a competitor must not
    # accumulate election lead from the incumbent's blind cycles), and no
    # other floor inherits incumbency by forfeit. Only a disappearance
    # longer than the grace lapses the incumbency, and then the best solved
    # floor is adopted on its merits.
    if incumbent is not None and incumbent not in solved \
            and incumbent in _floor_probability.get(entity, {}):
        dark_since = _floor_dark_cycles.setdefault(entity, time.time())
        if time.time() - dark_since < FLOOR_DARK_GRACE_S:
            return
        incumbent = None  # dark beyond grace: incumbency lapses
    _floor_dark_cycles.pop(entity, None)
    # Fit quality alone cannot separate floors joined by an open space: a
    # phone in the office below a catwalk is explained about as well by the
    # upstairs receivers around the void as by the office ones (measured
    # live at 0.54 / 0.46, incumbent never challenged). The floor whose
    # receivers are physically nearest gets the benefit of the doubt.
    prox_scores = _proximity_weighted_scores(
        {f: s["conf"] for f, s in solved.items()},
        {c["name"]: c.get("near_k_m", c.get("nearest_m")) for c in candidates},
        _tuning(layout, "floor_proximity_weight"),
        _tuning(layout, "floor_proximity_blend"),
    )
    # The floor's own prior (layout floor["bias"], default 1): in a house the
    # ground floor is where things usually are, and a phone on the kitchen
    # counter must not tie with the bedroom directly above it. Shaped by the
    # floor's bias field at where THIS floor's solve put the thing, so the
    # prior can differ beside a void from what it is over a slab.
    biases = {f: _floor_bias(layout, f, solved[f]["fix"]) for f in prox_scores}
    scores = {f: s * biases[f] for f, s in prox_scores.items()}
    # Every contender's own fix and how its score was built, not just the
    # winner's. A bias field is tuned against exactly this: where did each
    # floor's solve land, and what did the election make of it. The published
    # odds are smoothed and the losing floors' fixes were never published, so
    # without this a wrong election cannot be replayed under another field.
    # Where the floors are registered against each other (registration.py)
    # each fix is also given in the shared house frame, in metres. Two floors
    # that both hear a thing line-of-sight should put it in the same place;
    # how far apart they put it is evidence no single floor's fit contains.
    frames = _floor_frames(hass, layout)
    floor_cands = {
        f: {
            "fix": [round(float(solved[f]["fix"][0]), 1), round(float(solved[f]["fix"][1]), 1)],
            **_house_position(frames.get(f), solved[f]["fix"]),
            "conf": round(solved[f]["conf"], 4),
            "prox": round(prox_scores[f], 4),
            "bias": round(biases[f], 4),
            "score": round(scores[f], 4),
            # Which proxies said what, per floor. The scores alone cannot tell
            # a floor that lost from a floor that was not listening: Meg's
            # catwalk went from eight receivers to three and the floor below
            # won on what was left, and no telemetry said so.
            "heard": [[name, metres] for name, metres in (solved[f].get("heard") or [])],
        }
        for f in scores
    }
    probs = _update_floor_probabilities(entity, scores, valid_floors)
    now = time.time()
    # The incumbent's required lead grows with how long it has held the floor
    # (up to floor_tenure_bonus at floor_tenure_full_secs), so a floor that
    # has been right for ten minutes is not unseated by one geometry fluke.
    tenure = max(0.0, now - _floor_since.get(entity, now))
    margin = _tuning(layout, "floor_switch_margin") + _tuning(layout, "floor_tenure_bonus") * min(
        1.0, tenure / _tuning(layout, "floor_tenure_full_secs")
    )
    lowest_floor_name, challenge = _elect_floor(
        probs, incumbent, solved, _floor_challenge.get(entity),
        now=now, switch_secs=_tuning(layout, "floor_switch_secs"), margin=margin,
    )
    if challenge is None:
        _floor_challenge.pop(entity, None)
    else:
        _floor_challenge[entity] = challenge
    if lowest_floor_name is None:
        return  # nothing electable this cycle: keep last values

    if update_trilateration_and_zone.last_floor.get(entity) != lowest_floor_name:
        # The Kalman state holds pixel coordinates in the previously elected
        # floor's map space; it must not carry over to the newly elected floor.
        # Neither may the zone election: its polygons are that floor's.
        _kf_position_state.pop(entity, None)
        _zone_state.pop(entity, None)
        _subzone_state.pop(entity, None)
        _anchor_state.pop(entity, None)
        _floor_since[entity] = now
        update_trilateration_and_zone.last_floor[entity] = lowest_floor_name

    elected = solved[lowest_floor_name]
    # The elected floor's match is the one whose thing-vs-reference range
    # ratio says something about the probe gain; fold it in (slowly).
    fp_tel = elected.get("fp")
    if fp_tel and fp_tel.get("ratio") and _tuning(layout, "fingerprint_auto_gain"):
        _fingerprint_db.learn(fp_tel["ratio"], fp_tel.get("conf") or 0.0, entity=entity)
    weighted = elected["weighted"]
    zone_polys = elected["zone_polys"]
    floor_bounds = elected["bounds"]
    scale = elected["scale"]
    tricords = elected["fix"]
    # A thing sitting on a proxy is placed on the proxy (see _elect_anchor).
    anchor = _elect_anchor(entity, lowest_floor_name, _floor_receivers(layout, lowest_floor_name), layout, now=now)
    if anchor is not None and tricords is not None:
        tricords = (anchor["x"], anchor["y"])
        elected["conf"] = max(elected["conf"], ANCHOR_CONF)
    # The Bluetooth answer, kept as "raw" even when a radar target replaces
    # it: the radar pairing is made against it, so it must not become the
    # target it was paired with, or a thing would stick to a target for good.
    ble_fix = tricords
    radar_claim = _radar_claim(entity, lowest_floor_name, layout)
    if radar_claim is not None and tricords is not None:
        tricords = tuple(radar_claim["cords"])
        elected["conf"] = max(elected["conf"], ANCHOR_CONF)

    if tricords is not None:
        # Constant-velocity Kalman smoothing of the published position. The RAW
        # trilaterated fix feeds the filter (so the estimate is never biased by
        # the zone snapping applied below); the filtered output is clipped to the
        # floor bounds inside the helper.
        avg_x, avg_y = _kalman_position_update(
            entity, lowest_floor_name, tricords, scale, floor_bounds
        )

        # A fix outside every zone is physically implausible (BLE noise pushed
        # it into a wall or off the apartment): publish the nearest point on
        # the zone union instead. Snapping is applied to the filter OUTPUT only;
        # the filter state keeps the raw fix, so smoothing is not biased toward
        # the boundary.
        test_point = Point(float(avg_x), float(avg_y))
        # Out of a void, stay on the side the thing is already on.
        zst = _zone_state.get(entity)
        incumbent_room = zst.get("zone") if isinstance(zst, dict) and zst.get("floor") == lowest_floor_name else None
        snapped = snap_point_into_zones(
            zone_polys, test_point, prefer=incumbent_room,
            prefer_margin_px=NO_GO_SNAP_STICK_M * (scale if isinstance(scale, (int, float)) and scale > 0 else 0.0),
        )
        if snapped is not None:
            test_point = snapped
            avg_x, avg_y = float(snapped.x), float(snapped.y)
        # The instantaneous zone of the published point, and the nearest zone
        # no matter how far - both raw, cycle-by-cycle answers, still exposed
        # (nearest_zone as its own sensor, zone_raw in the API) for anything
        # that wants them.
        instant_zone = find_zone_for_point(hass, new_global_data, entity, lowest_floor_name, test_point)
        nearest_zone = find_nearest_zone(hass, new_global_data, entity, lowest_floor_name, test_point)
        # The PUBLISHED zone gets the same treatment floors already had:
        # membership probabilities smoothed over cycles, a margin and a
        # wall-clock dwell before a change, and a lock while the thing is
        # demonstrably still. Half of all zone changes in a 24 h sample were
        # A->B->A flips at a median dwell of 21 s; this is where they went.
        # An anchored thing's position is certain (it is on that proxy):
        # elect its room and spot from the point, not the filter's old
        # uncertainty ellipse, which would keep a small spot from ever winning.
        kf_for_election = None if anchor is not None else _kf_position_state.get(entity)
        zone, zone_locked, zone_speed = _elect_zone(
            entity, lowest_floor_name, instant_zone, test_point,
            kf_for_election, zone_polys, scale, layout, now=now,
        )
        # Sub-zone: only the elected zone's own sub-zones are eligible, with
        # membership smoothing, exit hysteresis, dwell and the zone lock (see
        # _elect_subzone). parent_zone always names the enclosing main zone.
        sub_zone, parent_zone = _elect_subzone(
            entity, lowest_floor_name, zone, zone_locked, test_point, kf_for_election,
            _floor_sub_zone_polygons(hass, new_global_data, entity, lowest_floor_name), scale, layout, now=now,
            fp=elected.get("fp"),
        )
        row = {
                "ent": entity,
                "cords": [avg_x, avg_y],
                "zone": zone,
                "zone_raw": instant_zone,
                "zone_locked": zone_locked,
                # When this room and this spot were entered: kept across a
                # restart, where the sensors' own timestamps are not.
                "since": (_zone_state.get(entity) or {}).get("since"),
                "spot_since": (_subzone_state.get(entity) or {}).get("since"),
                "sub_zone": sub_zone,
                # Smoothed sub-zone membership shares (name -> share, plus
                # "unknown"), the sub-zone counterpart of "floors" below.
                "sub_zones": _subzone_probs(entity),
                # The proxy the thing is anchored to (near-field), or None.
                "anchor": None if anchor is None else anchor["slug"],
                "speed": None if zone_speed is None else round(zone_speed, 2),
                "floor": lowest_floor_name,
                # The exact solver input (post-correction, post-filter), for
                # the panel's trilateration circles.
                "radii": [[float(pt[0]), float(pt[1]), float(pt[2])] for pt in weighted],
                # Smoothed floor-election probabilities, for debugging "why
                # did it pick this floor" (issue #94).
                "floors": {f: round(p, 3) for f, p in probs.items()},
                # This cycle's raw contenders behind those smoothed odds: each
                # floor's own fix, fit, proximity-weighted score, bias (scalar
                # x field) and final score. What a bias field is tuned from.
                "floor_cands": floor_cands,
                # Positioning telemetry for the eval harness (tools/sextant_eval.py)
                # and the debug tab: the pre-Kalman, pre-snap trilaterated fix
                # next to the published (filtered + snapped) `cords`, so solver
                # bias can be told apart from filter lag; plus this fix's
                # weighted RMS residual (m) and the elected floor's confidence.
                # Both frontends read named fields, so these keys are inert.
                "raw": [round(float(ble_fix[0]), 2), round(float(ble_fix[1]), 2)],
                # The mmWave radar this thing was placed by this cycle, or None.
                "radar": None if radar_claim is None else radar_claim.get("radar_name"),
                "rms_m": None if elected["rms_m"] is None else round(elected["rms_m"], 3),
                "conf": round(elected["conf"], 3),
                # Fingerprint fusion telemetry: which estimator ran, the
                # fingerprint's own fix and confidence, and the reference
                # receivers it was averaged from (see fingerprint.py).
                "estimator": estimator,
                "fp": elected.get("fp"),
                "updated": time.time(),
        }
        apitricords = update_or_add_entry(apitricords, row)
        await update_apitricords(hass, apitricords)
        # Opt-in: keep this cycle's election for replay (election_log.py).
        if _tuning(layout, "election_log_hours") > 0:
            election_log.get(hass).add(row, row["updated"])
        # Feed the position history. Stored in METRES in this floor's frame, so
        # a later map re-export (which changes every pixel) cannot move the
        # past; the pixel projection happens at render time. A floor with no
        # scale has no metric frame, so its fixes are not recordable.
        if scale:
            try:
                get_position_history(hass).record(
                    entity, time.time(), avg_x / scale, avg_y / scale,
                    lowest_floor_name, scale, zone, sub_zone)
            except Exception as e:  # history must never break tracking
                _LOGGER.debug("Position history record failed for %s: %s", entity, e)
        # Just heard: presence "here", and the moment it was heard.
        here = _presence_attrs(time.time(), time.time(), layout)
        _presence_published[entity] = "here"
        loc_state, loc_attrs = _location_state(zone, sub_zone, parent_zone, lowest_floor_name, layout)
        update_sextant_sensor_state(hass, f"sensor.{entity}_sextant_room", zone,
                                    {"area_id": room_area(layout, lowest_floor_name, zone)[0], **here})
        update_sextant_sensor_state(hass, f"sensor.{entity}_sextant_nearest_room", nearest_zone, dict(here))
        update_sextant_sensor_state(hass, f"sensor.{entity}_sextant_floor", lowest_floor_name, dict(here))
        update_sextant_sensor_state(hass, f"sensor.{entity}_sextant_spot", sub_zone, {"room": parent_zone, **here})
        update_sextant_sensor_state(hass, f"sensor.{entity}_sextant_location", loc_state, {**loc_attrs, **here})

def room_area(layout, floor_name, room_name):
    """(area_id, floor_id): the Home Assistant area a room is linked to, and
    the Home Assistant floor its Sextant floor is linked to. None where unlinked.

    A room is a shape on a plan and an area is a grouping of devices; linked,
    an automation can act on the area a thing is in ("the lights where David
    is") without a lookup table of its own.
    """
    floors = layout.get("floor") if isinstance(layout, dict) else None
    for floor in floors or []:
        if isinstance(floor, dict) and floor.get("name") == floor_name:
            floor_id = floor.get("floor_id") if isinstance(floor.get("floor_id"), str) else None
            for zone in floor.get("zones") or []:
                if zone.get("entity_id") == room_name and not zone.get("no_go"):
                    area = zone.get("area_id")
                    return (area if isinstance(area, str) and area else None), floor_id
            return None, floor_id
    return None, None


def _location_state(zone, sub_zone, parent_zone, floor, layout=None):
    """(state, attributes) for the fused location sensor: the finest place known.

    The state is the spot when the thing is in one and the room when it is
    not, so one entity answers "where is it" at whatever resolution is
    available. Which of the two it is matters to some automations and is not
    recoverable from the string - "Couch" and "Office" look alike - so `kind`
    says, and `room` is always filled in. A spot's room comes from the spot's
    parent rather than from the elected room: they can disagree for a cycle
    while a thing crosses a boundary, and the room containing the spot is the
    one that matches the state being published.
    """
    known = sub_zone and sub_zone != "unknown"
    # The spot's own room first; failing that (a spot drawn outside every
    # room has no parent) the elected room, which is still a better answer
    # than "unknown" for a thing whose room IS known.
    parent = parent_zone if known and parent_zone and parent_zone != "unknown" else None
    room = parent or zone or "unknown"
    area_id, floor_id = room_area(layout, floor, room)
    return (
        (sub_zone if known else (zone or "unknown")),
        {
            "kind": "spot" if known else "room",
            "room": room,
            "spot": sub_zone if known else None,
            "floor": floor or "unknown",
            # The linked Home Assistant area and floor (None where unlinked).
            "area_id": area_id,
            "floor_id": floor_id,
        },
    )


def _solve_floor_jobs(jobs):
    """Run one thing's candidate-floor solves. Pure CPU; executor-safe.

    Each job carries everything the fit needs (see the phase comments in
    update_trilateration_and_zone); nothing here touches hass or the
    election state. Returns, per job, ``(fix, confidence, rms_m)`` or None
    when that floor's readings do not converge.
    """
    out = []
    for job in jobs:
        fix = conf = rms_m = None
        if len(job["weighted"]) >= 3:
            fix = trilaterate(
                job["weighted"], bounds=job["bounds"],
                min_weight_radius=job["min_wr"], stable_hint=job["stable_hint"],
            )
        if fix is not None:
            conf, rms_m, _coverage = _score_floor_fit(fix, job["weighted"], job["scale"])
            # A fit landing in this floor's no-go zone is physically impossible
            # here (issue #60): down-weight it so the competition prefers the
            # floor where that spot is a real room. Down-weight, not eliminate -
            # a sole candidate still wins and is snapped out by the caller.
            if _point_in_no_go(fix, job["zone_polys"]):
                conf *= NO_GO_CONF_PENALTY
        fix, conf, fp = _fuse_fingerprint(job.get("fingerprint"), fix, conf)
        if fix is None:
            out.append(None)
            continue
        out.append((fix, conf, rms_m, fp))
    return out


def _fuse_fingerprint(spec, geo_fix, geo_conf):
    """Blend the geometric fit with the fingerprint match for one floor.

    Returns (fix, conf, telemetry). With no fingerprint inputs the geometric
    answer passes through. In "fingerprint" mode the match replaces the
    fit (the fit is the fallback where no reference matched); in "fused"
    mode the fix is the weighted blend of the two and the confidence
    likewise, so a floor that only one estimator can place still competes
    on that estimator alone.
    """
    if spec is None:
        return geo_fix, geo_conf, None
    m = fingerprint.match(spec["thing"], spec["refs"], k=spec["k"], missing_m=spec["missing_m"])
    telemetry = None if m is None else {
        "fix": [round(m["x"], 1), round(m["y"], 1)],
        "conf": round(m["conf"], 3),
        "score": round(m["score"], 3),
        "refs": m["refs"],
        # Thing/reference range ratio over the receivers both were heard
        # by (>1: references read short), and the gain the references were
        # built with - the auto-gain loop's input and output.
        "ratio": None if m.get("ratio") is None else round(m["ratio"], 3),
        "gain": round(float(spec.get("gain", 1.0)), 3),
        # The geometric end of the blend, so a client can show both ends of the slider.
        "geo": None if geo_fix is None else [round(float(geo_fix[0]), 1), round(float(geo_fix[1]), 1)],
    }
    if m is None:
        return geo_fix, geo_conf, None
    if spec["mode"] == "fingerprint" or geo_fix is None:
        return (m["x"], m["y"]), m["conf"], telemetry
    # A match whose scale disagrees with the thing (its own gain still being learned, or a radio
    # unlike the probes) is trusted less: the Office Tile read 1.5x its best reference and the
    # fingerprint half of every fix dragged it two rooms over.
    trust = fingerprint.trust(m.get("ratio"))
    telemetry["trust"] = round(trust, 2)
    w, wf = spec["weight"] * trust, spec["floor_weight"] * trust
    fix = ((1.0 - w) * geo_fix[0] + w * m["x"], (1.0 - w) * geo_fix[1] + w * m["y"])
    conf = (1.0 - wf) * geo_conf + wf * m["conf"]
    return fix, conf, telemetry


def update_or_add_entry(data, new_entry):
    """Replace the entry for new_entry["ent"] in place, or append it.

    Every field is refreshed (the caller always builds a complete entry), so
    a new telemetry key needs no change here.
    """
    for item in data:
        if item["ent"] == new_entry["ent"]:  # Check if "ent" already exists
            item.update(new_entry)
            return data

    # If "ent" was not found, add as new post
    data.append(new_entry)
    return data


def _forget_thing_state(ent):
    """Drop everything the tracking loop remembers about one thing, including
    its last sighting - the one thing the stale-position prune keeps, so that
    the Live page can say "away since". For a thing being forgotten on purpose
    (see ws_thing_forget) that memory is the point."""
    for table in (_kf_position_state, _zone_state, _subzone_state, _anchor_state, _floor_probability,
                  _floor_probability_at, _floor_challenge, _floor_dark_cycles, _floor_since, _arrivals, _last_seen, _presence_published):
        table.pop(ent, None)
    getattr(update_trilateration_and_zone, "last_floor", {}).pop(ent, None)
    getattr(update_trilateration_and_zone, "last_r_values", {}).pop(ent, None)


async def prune_stale_positions(hass):
    """Drop things not detected by any receiver for the timeout period.

    Without this, a person who left home stayed on the map at their last
    position forever, and the zone/floor sensors kept the stale values.
    """
    global apitricords
    timeout = STALE_POSITION_SECS
    layout = get_layout(hass)
    if isinstance(layout, dict):
        configured = layout.get("position_timeout")
        if isinstance(configured, (int, float)) and configured > 0:
            timeout = configured

    now = time.time()
    stale_ents = {e["ent"] for e in apitricords if now - e.get("updated", now) > timeout}
    # State restored over a restart for a thing not heard since has no row
    # here, and would otherwise keep its incumbency (and be saved again)
    # forever. It is as old as the last time the thing was seen.
    live = {e["ent"] for e in apitricords}
    restored = (set(_kf_position_state) | set(_zone_state) | set(_subzone_state)
                | set(getattr(update_trilateration_and_zone, "last_floor", {}))) - live
    stale_ents |= {
        ent for ent in restored
        if now - ((_last_seen.get(ent) or {}).get("updated") or 0) > timeout
    }
    if not stale_ents:
        return
    if live & stale_ents:
        apitricords = [e for e in apitricords if e["ent"] not in stale_ents]
        await update_apitricords(hass, apitricords)
    # History is deliberately NOT pruned with the live entry: the point of it is
    # to survive the absence. Just break the line so the scrubber does not draw
    # a straight segment across the gap.
    try:
        hist = get_position_history(hass)
        for ent in stale_ents:
            hist.mark_gap(ent)
    except Exception as e:
        _LOGGER.debug("Position history gap mark failed: %s", e)
    for ent in sorted(stale_ents):
        # Drop the Kalman state too: a returning thing should re-seed fresh
        # rather than predict velocity across the whole absence. Same for the
        # whole election state — probabilities, pending challenge, and the
        # INCUMBENCY itself: it may well come back on another floor, and an
        # hours-stale incumbent must not enjoy hysteresis against it (with
        # freshly-reset probabilities the margin could pin the wrong floor
        # indefinitely). Jump-gate radii from before the absence are equally
        # meaningless.
        _kf_position_state.pop(ent, None)
        _zone_state.pop(ent, None)
        _subzone_state.pop(ent, None)
        _anchor_state.pop(ent, None)
        _floor_probability.pop(ent, None)
        _floor_probability_at.pop(ent, None)
        _floor_challenge.pop(ent, None)
        _floor_dark_cycles.pop(ent, None)
        _floor_since.pop(ent, None)
        getattr(update_trilateration_and_zone, "last_floor", {}).pop(ent, None)
        getattr(update_trilateration_and_zone, "last_r_values", {}).pop(ent, None)
        _arrivals.pop(ent, None)
        _LOGGER.info("Thing %s not seen for %ss; clearing its position", ent, timeout)
        gone = _presence_attrs((_last_seen.get(ent) or {}).get("updated"), now, layout if isinstance(layout, dict) else {})
        _presence_published[ent] = gone["presence"]
        loc_state, loc_attrs = _location_state("unknown", "unknown", "unknown", "unknown")
        update_sextant_sensor_state(hass, f"sensor.{ent}_sextant_room", "unknown", {"area_id": None, **gone})
        update_sextant_sensor_state(hass, f"sensor.{ent}_sextant_floor", "unknown", dict(gone))
        update_sextant_sensor_state(hass, f"sensor.{ent}_sextant_nearest_room", "unknown", dict(gone))
        update_sextant_sensor_state(hass, f"sensor.{ent}_sextant_spot", "unknown", {"room": "unknown", **gone})
        update_sextant_sensor_state(hass, f"sensor.{ent}_sextant_location", loc_state, {**loc_attrs, **gone})

# How often the state a restart would lose is written out. On a clean stop
# it is written again anyway; this is for the power cut that is not clean.
RUNTIME_SAVE_EVERY_S = 60.0
_runtime_saved_at = 0.0


async def update_apitricords(hass, new_data):
    """Update apitricords in hass.data"""
    global _runtime_saved_at
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["apitricords"] = new_data
    # Remember where each thing was last heard, so a thing that goes quiet
    # can still say where and when (see runtime.py).
    for row in new_data or []:
        if isinstance(row, dict) and row.get("ent") and isinstance(row.get("updated"), (int, float)):
            _last_seen[row["ent"]] = {
                "zone": row.get("zone"), "spot": row.get("sub_zone"), "floor": row.get("floor"),
                "updated": row["updated"], "cords": row.get("cords"),
            }
    now = time.time()
    if not _runtime_saved_at:
        # The first cycle after a start has one thing in it; there is nothing
        # worth writing yet, and a clean stop writes whatever is current.
        _runtime_saved_at = now
    elif now - _runtime_saved_at >= RUNTIME_SAVE_EVERY_S:
        _runtime_saved_at = now
        await _save_runtime(hass)


def _sensor_is_live(hass, entity_id):
    """Whether a cached Sextant sensor object is actually attached to HA.

    sensor.py creates the entity objects and caches them before
    async_add_entities runs. An entity the user has DISABLED in the registry
    is never added, so it stays in the cache with ``hass`` unset forever -
    writing state to it raises ("Attribute hass is None"), which the
    accuracy sensor did every self-test cycle.
    """
    sensors_cache = hass.data.get("sextant_sensors")
    if not sensors_cache:
        return False
    sensor = sensors_cache.get(entity_id)
    return sensor is not None and getattr(sensor, "hass", None) is not None


# last_heard moves every cycle a thing is heard, and it is on all five of a
# thing's sensors. Written every time, that was 97 % of Sextant's state changes
# and a fifth of the whole house's (7.3 a second of 36): a new State, Context
# and Event each, for a timestamp. When last_heard is ALL that changed, the
# write waits until the last one is this old. Anything else - the state, the
# presence, any other attribute - is written at once, with the exact
# last_heard of that moment.
LAST_HEARD_WRITE_S = 60.0
_NOT_WRITTEN = object()


def _without_last_heard(attributes):
    if not isinstance(attributes, dict) or "last_heard" not in attributes:
        return attributes
    return {k: v for k, v in attributes.items() if k != "last_heard"}


def update_sextant_sensor_state(hass, entity_id, state, attributes=None, now=None):
    """Update state (and optional extra attributes) on a registered Sextant SensorEntity."""
    sensors_cache = hass.data.get("sextant_sensors")
    if not sensors_cache:
        return
    sensor = sensors_cache.get(entity_id)
    if sensor is None:
        return
    if getattr(sensor, "hass", None) is None:
        # Not added to HA (disabled in the registry, or not yet added). The
        # value is kept on the object so it is current if the entity is
        # enabled later; there is just no state machine to write to yet.
        sensor._state = state
        if attributes is not None:
            sensor._attrs = attributes
        return
    now = time.time() if now is None else now
    steady = _without_last_heard(attributes if attributes is not None else getattr(sensor, "_attrs", None))
    written = getattr(sensor, "_written", _NOT_WRITTEN)
    if (written is not _NOT_WRITTEN and written[0] == state and written[1] == steady
            and 0 <= now - written[2] < LAST_HEARD_WRITE_S):
        # Only last_heard moved, and it was written within the minute. The
        # entity keeps what was written: Home Assistant polls a sensor every
        # 30 s and writes whatever it holds, so holding the newer timestamp
        # here put the write straight back (measured: one every ~30 s).
        return
    sensor._state = state
    if attributes is not None:
        sensor._attrs = attributes
    sensor._written = (state, steady, now)
    sensor.async_write_ha_state()

async def process_single_entity(hass, new_global_data, eids):
    """Process a single entity: first receivers, then trilateration"""
    await update_receiver_radii(hass, eids)  # Wait for the receivers to update
    await update_trilateration_and_zone(hass, new_global_data, eids["entity"])  # When it is complete → perform trilateration

def _thing_layout(layout):
    """One thing's working copy of the layout: only what a cycle writes to.

    Each thing gets its own copy because the cycle writes that thing's readings
    onto the receivers (``distance``, ``quality``, ``cords.r``). That is ALL it
    writes, so that is all that needs copying. This used to be a deepcopy of
    the whole layout per thing per cycle - rooms, spots, pins, bias-field grids,
    every thing's name and class - eighteen times over, in one synchronous
    block on the event loop, and it got heavier with every feature that stored
    something on a floor. Everything but the receivers is shared and must be
    treated as read-only here, as it already was.
    """
    out = dict(layout)
    floors = []
    for floor in layout.get("floor", []):
        own = dict(floor)
        own["receivers"] = [
            {**r, "cords": dict(r["cords"])} if isinstance(r.get("cords"), dict) else dict(r)
            for r in floor.get("receivers") or []
        ]
        floors.append(own)
    out["floor"] = floors
    return out


async def process_entities(hass, new_global_data):
    """Process every thing, one at a time, letting the event loop run in between.

    This used to gather all of them. Each thing's work up to its first real
    await - reading the receivers, building the solver jobs - is synchronous,
    and asyncio runs every ready task before it polls for I/O again, so a
    gather of eighteen things ran eighteen of those back to back: a 200-330 ms
    stall of the whole of Home Assistant, measured, on every cycle. Nothing
    was gained for it either - the solves go to the executor but the rest
    holds the GIL, so the things never really ran in parallel.

    One at a time, the longest the loop waits on Sextant is a single thing's
    chunk. The cycle takes a little longer end to end and has fifteen seconds
    to do it in.
    """
    try:
        await _radar_cycle(hass, get_layout(hass))
    except Exception as e:  # noqa: BLE001 - a radar must never stop the things'
        _LOGGER.debug("Radars not read: %s", e)
    for eids in new_global_data:
        ent = eids.get("entity")
        try:
            await process_single_entity(hass, new_global_data, eids)
            _thing_error_counts.pop(ent, None)
        except Exception:  # noqa: BLE001 - one thing's bad data must not stop the rest
            n = _thing_error_counts[ent] = _thing_error_counts.get(ent, 0) + 1
            # Named, not printed whole: eids["data"] is this thing's copy of
            # the layout, tens of KB, and bad data fails every 15 s cycle. The
            # first failure and every CYCLE_ERROR_REPEAT_EVERY-th after it.
            if n == 1 or n % CYCLE_ERROR_REPEAT_EVERY == 0:
                _LOGGER.exception("Positioning failed for %s (%d cycle%s in a row)", ent, n, "" if n == 1 else "s")
        await asyncio.sleep(0)  # a thing with nothing to solve never awaits: yield for it
    try:
        _update_person_sensors(hass)
    except Exception as e:  # noqa: BLE001 - a person's sensor must never stop the things'
        _LOGGER.warning("Person locations not updated: %s", e)
    try:
        layout = get_layout(hass)
        _publish_presence(hass, layout if isinstance(layout, dict) else {})
    except Exception as e:  # noqa: BLE001 - an attribute must never stop the things'
        _LOGGER.debug("Presence not published: %s", e)
    try:
        layout = get_layout(hass)
        await election_log.get(hass).flush(_tuning(layout, "election_log_hours"))
    except Exception as e:  # noqa: BLE001 - a log must never stop the things'
        _LOGGER.debug("Election log not flushed: %s", e)
    try:
        await _robot_cycle(hass, get_layout(hass))
    except Exception as e:  # noqa: BLE001 - a vacuum must never stop the things'
        _LOGGER.debug("Robots not placed: %s", e)


# --- Robot vacuums (robots.py) ------------------------------------------------
#
# A robot is configured per vacuum entity in the layout's "robots" map:
# {"floor", "fit" (robots.fit, with the transform), "dock_map" (the dock in
# the robot's map, millimetres), "dock_plan" (where the user marked it)}. It
# is published as a thing of its own, vacuum.rocky -> "vacuum_rocky", with
# the same sensors and Live row as any other; its position is not solved but
# asked of the robot, and put through the fitted transform.

# The vacuum states in which a robot is somewhere other than its dock.
ROBOT_OUT_STATES = frozenset({"cleaning", "returning", "paused", "idle", "error"})
# vacuum entity -> {"inflight", "last_poll", "map" (x, y) mm, "at", "fails" (in a row),
# "reads_ok", "reads_failed" (since start), "last_error"}
_robot_state = {}


def _new_robot_state():
    return {"inflight": False, "last_poll": 0.0, "map": None, "at": None, "fails": 0,
            "reads_ok": 0, "reads_failed": 0, "last_error": None}


# --- mmWave radars (radars.py) ---------------------------------------------------
#
# A floor's "radars" are placed on the Edit page: {"radar_id", "device_id",
# "cords", "heading", "flip"}. Each cycle reads every placed radar's targets,
# puts them on the plan and pairs them with the things whose Bluetooth fix
# (last cycle's "raw") is nearest. A paired thing is placed on its target;
# an unpaired target is someone Sextant has no device for.

RADAR_SPECS_TTL_S = 60.0
RADAR_FRESH_S = 30.0          # a thing's last fix older than this cannot claim a target
AUTO_PIN_STILL_S = 60.0       # both still this long before a pin is taken
AUTO_PIN_EVERY_S = 1800.0     # at most one automatic pin per thing per half hour
AUTO_PIN_MIN_GAP_M = 1.0      # nor one within a metre of that thing's last
AUTO_PIN_KEEP = 30            # automatic pins kept per thing, newest first
AUTO_PIN_SOURCE = "mmwave"
_radar_specs_cache = {"at": 0.0, "specs": {}}
# The last cycle's targets and claims: {"t", "targets": [...], "claims": {thing: target}}
_radar_frame = {"t": 0.0, "targets": [], "claims": {}}
# thing -> {"radar", "cords", "since"} while one target and that thing stay still
_radar_still = {}
_radar_last_pin = {}


def _radar_specs(hass, now=None):
    """Every device that reports mmWave target coordinates: {device_id: spec}.

    Found from the registries (no configuration): any device with
    target_N_x / target_N_y entities, cached for a minute.
    """
    now = time.time() if now is None else now
    if now - _radar_specs_cache["at"] < RADAR_SPECS_TTL_S and _radar_specs_cache["specs"]:
        return _radar_specs_cache["specs"]
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    by_device = {}
    for entry in ent_reg.entities.values():
        if entry.device_id and not entry.disabled_by:
            by_device.setdefault(entry.device_id, []).append(entry.entity_id)
    specs = {}
    for device_id, ids in by_device.items():
        spec = radars_mod.device_spec(ids)
        if spec is None:
            continue
        dev = dev_reg.async_get(device_id)
        spec["name"] = (dev.name_by_user or dev.name) if dev else device_id
        spec["model"] = dev.model if dev else None
        spec["area_id"] = dev.area_id if dev else None
        specs[device_id] = spec
    _radar_specs_cache.update(at=now, specs=specs)
    return specs


def _radar_claim(entity, floor_name, layout):
    """The target this thing claimed this cycle, if fusion is on and it is fresh."""
    if not _tuning(layout, "mmwave_fusion"):
        return None
    t = (_radar_frame.get("claims") or {}).get(entity)
    if not t or t.get("floor") != floor_name or time.time() - _radar_frame.get("t", 0) > RADAR_FRESH_S:
        return None
    return t


# What an mmWave target can be: a person or an animal - the radar sees bodies,
# not objects. So only a thing that goes with one can claim a target: a phone
# or a watch, a person, a pet (and an unclassified thing, which is usually a
# phone nobody classed). An AirPods case or a Tile on the office desk sits a
# metre from whoever is at the desk, and used to be the nearest fix to them:
# it would claim the person, be moved onto them, and could become a pin.
RADAR_CLAIM_CLASSES = frozenset({"", "phone", "watch", "person", "man", "woman", "child", "paw", "dog", "cat"})

# A sensor this close to a room (it is usually on the wall) is in that room;
# a target this far outside its room still counts, since the walls are drawn
# by hand and the radar's fixes jitter.
RADAR_ROOM_AUTO_M = 0.5
RADAR_ROOM_MARGIN_M = 0.3


def _radar_room(hass, layout, floor, radar):
    """The room a radar's targets are kept to: (name, polygon), or (None, None)
    for no limit.

    The radar's ``room`` is "*" for no limit, a room's name, or unset for the
    room the sensor is in. This radar model sees through drywall: without the
    limit, someone on the couch behind the office wall is a target too.
    """
    want = radar.get("room")
    if want == "*":
        return None, None
    if want == "auto":
        want = None
    scale = floor.get("scale")
    polys = [(name, poly) for name, poly, _b, no_go in _floor_zone_polygons(
        hass, [{"entity": "_radar", "data": layout}], "_radar", floor.get("name")) if not no_go]
    if want:
        for name, poly in polys:
            if name == want:
                return name, poly
    if not polys or not isinstance(scale, (int, float)) or scale <= 0:
        return None, None
    at = Point(float(radar["cords"]["x"]), float(radar["cords"]["y"]))
    dist, name, poly = min(((poly.distance(at), name, poly) for name, poly in polys), key=lambda t: t[0])
    return (name, poly) if dist <= RADAR_ROOM_AUTO_M * scale else (None, None)


def _radar_targets(hass, layout, floor, radar, spec):
    """One radar's targets on the plan, each marked ``outside`` when it is
    beyond the radar's room (see _radar_room)."""
    scale = floor["scale"]
    room, poly = _radar_room(hass, layout, floor, radar)
    margin = RADAR_ROOM_MARGIN_M * scale
    out = []
    for index, x, y, speed in radars_mod.read_targets(spec, hass.states.get):
        px, py = radars_mod.to_plan(radar, x, y, scale)
        outside = poly is not None and poly.distance(Point(px, py)) > margin
        out.append({"index": index, "cords": [round(px, 1), round(py, 1)], "x_m": round(x, 2), "y_m": round(y, 2),
                    "speed": speed, "outside": outside, "limit": room})
    return out


async def _radar_cycle(hass, layout, now=None):
    """Read every placed radar, pair its targets with things, publish the
    untracked count, and take automatic pins."""
    global _radar_frame
    now = time.time() if now is None else now
    if not isinstance(layout, dict):
        return
    placed = [(f, r) for f in layout.get("floor", []) for r in (f.get("radars") or [])
              if isinstance(r, dict) and r.get("device_id") and isinstance(r.get("cords"), dict)]
    if not placed:
        if _radar_frame["targets"]:
            _radar_frame = {"t": now, "targets": [], "claims": {}}
            update_sextant_sensor_state(hass, UNTRACKED_ENTITY_ID, 0, {"rooms": [], "targets": []})
        return
    specs = _radar_specs(hass, now)
    targets = []
    for floor, radar in placed:
        scale = floor.get("scale")
        spec = specs.get(radar["device_id"])
        if spec is None or not isinstance(scale, (int, float)) or scale <= 0:
            continue
        for t in _radar_targets(hass, layout, floor, radar, spec):
            if t["outside"]:
                continue                     # behind a wall: someone else's room
            targets.append({"floor": floor["name"], "radar": radar.get("radar_id"), "radar_name": spec.get("name"),
                            "index": t["index"], "cords": t["cords"], "speed": t["speed"], "thing": None})
    # Pair per floor, against each thing's Bluetooth fix from the last cycle.
    rows = [r for r in apitricords if isinstance(r, dict) and now - (r.get("updated") or 0) <= RADAR_FRESH_S]
    radius_m = _tuning(layout, "mmwave_pair_m")
    for floor in {t["floor"] for t in targets}:
        scale = next((f.get("scale") for f in layout["floor"] if f.get("name") == floor), None)
        mine = [(i, tuple(t["cords"])) for i, t in enumerate(targets) if t["floor"] == floor]
        things = [(r["ent"], tuple(r.get("raw") or r.get("cords"))) for r in rows
                  if r.get("floor") == floor and (r.get("raw") or r.get("cords"))
                  and (r.get("robot") or thing_class(layout, r["ent"]) in RADAR_CLAIM_CLASSES)]
        pairs, _free = radars_mod.pair(mine, things, radius_m * scale)
        for i, ent in pairs.items():
            targets[i]["thing"] = ent
    robots = {r["ent"] for r in rows if r.get("robot")}
    claims = {t["thing"]: t for t in targets if t["thing"] and t["thing"] not in robots}
    _radar_frame = {"t": now, "targets": targets, "claims": claims}
    untracked = [t for t in targets if not t["thing"]]
    rooms = []
    for t in untracked:
        room = find_zone_for_point(hass, [{"entity": "_radar", "data": layout}], "_radar", t["floor"], Point(*t["cords"]))
        t["room"] = room
        rooms.append(room)
    update_sextant_sensor_state(hass, UNTRACKED_ENTITY_ID, len(untracked), {
        "rooms": sorted(set(r for r in rooms if r and r != "unknown")),
        "targets": [{"room": t.get("room"), "floor": t["floor"], "radar": t["radar_name"]} for t in untracked],
    })
    if _tuning(layout, "mmwave_auto_pins"):
        await _radar_auto_pins(hass, layout, targets, rows, now)


async def _radar_auto_pins(hass, layout, targets, rows, now):
    """A location pin wherever one radar sees exactly one target, one thing
    has claimed it, and both have been still for AUTO_PIN_STILL_S."""
    by_radar = {}
    for t in targets:
        by_radar.setdefault(t["radar"], []).append(t)
    still_speed = _tuning(layout, "stationary_speed")
    row_of = {r["ent"]: r for r in rows}
    seen = set()
    for radar, ts in by_radar.items():
        if len(ts) != 1 or not ts[0]["thing"] or row_of.get(ts[0]["thing"], {}).get("robot"):
            continue
        t = ts[0]
        ent = t["thing"]
        row = row_of.get(ent) or {}
        scale = next((f.get("scale") for f in layout["floor"] if f.get("name") == t["floor"]), None) or 1.0
        still = (row.get("speed") is not None and row["speed"] < still_speed) and (t["speed"] is None or t["speed"] < 0.15)
        prev = _radar_still.get(ent)
        if not still:
            _radar_still.pop(ent, None)
            continue
        seen.add(ent)
        if not prev or prev["radar"] != radar or math.hypot(prev["cords"][0] - t["cords"][0], prev["cords"][1] - t["cords"][1]) > 0.5 * scale:
            _radar_still[ent] = {"radar": radar, "cords": t["cords"], "since": now}
            continue
        if now - prev["since"] < AUTO_PIN_STILL_S or now - _radar_last_pin.get(ent, 0.0) < AUTO_PIN_EVERY_S:
            continue
        if await _add_auto_pin(hass, ent, t, scale, now):
            _radar_last_pin[ent] = now
    for ent in [e for e in _radar_still if e not in seen]:
        _radar_still.pop(ent, None)


async def _add_auto_pin(hass, ent, target, scale, now):
    """Record an automatic location pin at a radar target. False when there
    are too few recent cycles to pin, or a recent pin is already that close."""
    samples = _truth_buffer.samples(ent, since=now - truth_mod.DEFAULT_WINDOW_SECS, floor=target["floor"])
    if len(samples) < truth_mod.MIN_SAMPLES:
        return False
    x, y = target["cords"]
    store = await load_truth(hass)
    marks = store.setdefault("marks", [])
    mine = [m for m in marks if m.get("entity") == ent and str(m.get("source") or "").startswith(AUTO_PIN_SOURCE)]
    if any(m.get("floor") == target["floor"] and math.hypot(m["x"] - x, m["y"] - y) < AUTO_PIN_MIN_GAP_M * scale for m in mine[-5:]):
        return False
    mark = {"id": int(store.get("next_id") or 1), "entity": ent, "floor": target["floor"], "x": round(float(x), 2),
            "y": round(float(y), 2), "t": now, "samples": samples,
            "source": f"{AUTO_PIN_SOURCE}:{target.get('radar_name') or target.get('radar')}"}
    store["next_id"] = mark["id"] + 1
    marks.append(mark)
    extra = len(mine) + 1 - AUTO_PIN_KEEP
    if extra > 0:
        drop = {id(m) for m in mine[:extra]}
        store["marks"] = [m for m in marks if id(m) not in drop]
    await save_truth(hass, store)
    _set_truth_marks(store["marks"])
    _LOGGER.info("mmWave pin for %s on %s at (%.0f, %.0f) from %s", ent, target["floor"], x, y, target.get("radar_name"))
    return True


def robot_thing(vacuum_entity_id):
    """The thing a robot is published as: vacuum.rocky -> vacuum_rocky."""
    return str(vacuum_entity_id).replace(".", "_")


def _robots(layout):
    robots = layout.get("robots") if isinstance(layout, dict) else None
    return robots if isinstance(robots, dict) else {}


def robot_things(layout):
    """The things of every robot that has been lined up with a floor."""
    return [robot_thing(v) for v, cfg in _robots(layout).items()
            if isinstance(cfg, dict) and (cfg.get("fit") or {}).get("transform") and cfg.get("floor")]


async def _robot_cycle(hass, layout):
    """Place every lined-up robot: at its dock while docked, else where it last
    said it was, asking again every robot_poll_secs (in the background, so a
    slow map fetch never holds up the cycle)."""
    if not isinstance(layout, dict):
        return
    now = time.time()
    for vac, cfg in _robots(layout).items():
        if not isinstance(cfg, dict) or not (cfg.get("fit") or {}).get("transform") or not cfg.get("floor"):
            continue
        st = hass.states.get(vac)
        state = st.state if st is not None else None
        rs = _robot_state.setdefault(vac, _new_robot_state())
        if state == "docked" and cfg.get("dock_map"):
            await _publish_robot(hass, layout, vac, cfg, cfg["dock_map"], now, state)
            continue
        if state not in ROBOT_OUT_STATES:
            continue  # unavailable or unknown: publish nothing, and it goes quiet
        if not rs["inflight"] and now - rs["last_poll"] >= _tuning(layout, "robot_poll_secs"):
            rs["inflight"], rs["last_poll"] = True, now
            hass.async_create_background_task(_robot_poll(hass, vac), f"sextant robot poll {vac}")
        if rs["map"] is not None and rs["at"] is not None:
            await _publish_robot(hass, layout, vac, cfg, rs["map"], rs["at"], state)


async def _robot_poll(hass, vac):
    """Ask one robot where it is (the Roborock integration's
    get_vacuum_current_position) and remember the answer."""
    rs = _robot_state.setdefault(vac, _new_robot_state())
    try:
        resp = await hass.services.async_call(
            "roborock", "get_vacuum_current_position", {"entity_id": vac},
            blocking=True, return_response=True,
        )
        pos = (resp or {}).get(vac) if isinstance(resp, dict) else None
        if isinstance(pos, dict) and isinstance(pos.get("x"), (int, float)) and isinstance(pos.get("y"), (int, float)):
            rs["map"], rs["at"], rs["fails"] = (float(pos["x"]), float(pos["y"])), time.time(), 0
            rs["reads_ok"] = rs.get("reads_ok", 0) + 1
        else:
            rs["reads_failed"] = rs.get("reads_failed", 0) + 1
            rs["last_error"] = "no position in the answer"
    except Exception as e:  # noqa: BLE001 - a failed map fetch keeps the last position
        rs["fails"] = rs.get("fails", 0) + 1
        rs["reads_failed"] = rs.get("reads_failed", 0) + 1
        rs["last_error"] = str(e)[:200]
        if rs["fails"] in (1, 10) or rs["fails"] % 100 == 0:
            _LOGGER.info("Robot %s: position not read (%d in a row): %s", vac, rs["fails"], e)
    finally:
        rs["inflight"] = False


async def _publish_robot(hass, layout, vac, cfg, map_xy, at, state):
    """A robot's map position on the plan, published like any placed thing."""
    global apitricords
    thing = robot_thing(vac)
    floor = cfg["floor"]
    scale = next((f.get("scale") for f in layout.get("floor", []) if f.get("name") == floor), None)
    x, y = robots_mod.apply(cfg["fit"]["transform"], map_xy)
    data = [{"entity": thing, "data": layout}]
    point = Point(float(x), float(y))
    zone_polys = _floor_zone_polygons(hass, data, thing, floor)
    previous = next((r.get("zone") for r in apitricords if r.get("ent") == thing), None)
    snapped = snap_point_into_zones(
        zone_polys, point, prefer=previous,
        prefer_margin_px=NO_GO_SNAP_STICK_M * (scale if isinstance(scale, (int, float)) and scale > 0 else 0.0),
    )
    if snapped is not None:
        point = snapped
    zone = find_zone_for_point(hass, data, thing, floor, point)
    nearest = find_nearest_zone(hass, data, thing, floor, point)
    sub_zone, parent_zone = find_sub_zone_for_point(hass, data, thing, floor, point)
    cords = [float(point.x), float(point.y)]
    row = {
        "ent": thing, "cords": cords, "zone": zone, "zone_raw": zone, "zone_locked": False,
        "since": None, "spot_since": None, "sub_zone": sub_zone, "sub_zones": None, "anchor": None,
        "speed": None, "floor": floor, "radii": [], "floors": {floor: 1.0}, "floor_cands": {},
        "raw": [round(float(x), 2), round(float(y), 2)], "rms_m": (cfg.get("fit") or {}).get("rms_m"),
        "conf": 1.0, "estimator": "robot", "fp": None, "updated": float(at),
        # Which vacuum this is, and what it is doing: the Live page offers
        # "mark the dock" on a robot instead of "it's actually here".
        "robot": vac, "robot_state": state,
    }
    apitricords = update_or_add_entry(apitricords, row)
    await update_apitricords(hass, apitricords)
    from .sensor import ensure_sensors_for_things  # noqa: PLC0415 - sensor.py imports this package
    ensure_sensors_for_things(hass, [thing])
    if isinstance(scale, (int, float)) and scale > 0:
        try:
            get_position_history(hass).record(thing, float(at), cords[0] / scale, cords[1] / scale, floor, scale, zone, sub_zone)
        except Exception as e:  # noqa: BLE001 - history must never break placing
            _LOGGER.debug("Position history record failed for %s: %s", thing, e)
    here = _presence_attrs(float(at), time.time(), layout)
    _presence_published[thing] = here["presence"]
    loc_state, loc_attrs = _location_state(zone, sub_zone, parent_zone, floor, layout)
    update_sextant_sensor_state(hass, f"sensor.{thing}_sextant_room", zone, {"area_id": room_area(layout, floor, zone)[0], **here})
    update_sextant_sensor_state(hass, f"sensor.{thing}_sextant_nearest_room", nearest, dict(here))
    update_sextant_sensor_state(hass, f"sensor.{thing}_sextant_floor", floor, dict(here))
    update_sextant_sensor_state(hass, f"sensor.{thing}_sextant_spot", sub_zone, {"room": parent_zone, **here})
    update_sextant_sensor_state(hass, f"sensor.{thing}_sextant_location", loc_state, {**loc_attrs, **here, "vacuum_state": state})


# thing -> {"floor", "x", "y", "since", "away"}: where an owned thing has
# stayed (within persons.STAY_RADIUS_M) and since when, for persons.pick.
_arrivals = {}
# Where each thing was last heard, kept across restarts: {ent: last dict}.
# The Live page reads it to say "away since 5:32 PM" for a thing nothing is
# hearing, which the sensors cannot answer after a restart.
_last_seen = {}
# ent -> the presence last written to its sensors ("here", "quiet", "away"),
# so a transition is written once, not every cycle (see _publish_presence).
_presence_published = {}
# person -> (thing, considered) that last placed them, held through a quiet spell.
_person_last = {}


def _presence_of(updated, now, layout):
    """here / quiet / away from how long since the thing was heard, on the same
    two thresholds the Live page uses: quiet past stale_after_secs, away past
    away_after_secs (or never heard). An automation can read home/away off it
    and fall back to GPS when it says away."""
    if not isinstance(updated, (int, float)):
        return "away"
    age = max(0.0, now - updated)
    if age <= _tuning(layout, "stale_after_secs"):
        return "here"
    if age <= _tuning(layout, "away_after_secs"):
        return "quiet"
    return "away"


def _presence_attrs(updated, now, layout):
    """The two presence attributes every per-thing sensor carries."""
    iso = None
    if isinstance(updated, (int, float)) and updated > 0:
        iso = datetime.fromtimestamp(updated, timezone.utc).isoformat(timespec="seconds")
    return {"presence": _presence_of(updated, now, layout), "last_heard": iso}


def _publish_presence(hass, layout):
    """Write a presence transition (here -> quiet -> away) to the sensors of a
    thing the cycle did not hear. A thing that is heard gets its presence with
    its position; one that has gone quiet is not processed at all, so nothing
    else would ever tell its sensors."""
    now = time.time()
    # Every thing that has sensors, not only the remembered sightings: a
    # thing not heard since the sightings were first kept has none, and would
    # otherwise never be told it is away.
    suffix = "_sextant_location"
    with_sensors = {e[len("sensor."):-len(suffix)] for e in (hass.data.get("sextant_sensors") or {}) if e.endswith(suffix)}
    for ent in with_sensors | set(_last_seen):
        seen = _last_seen.get(ent)
        seen = seen if isinstance(seen, dict) else {}
        presence = _presence_of(seen.get("updated"), now, layout)
        # "here" is normally written with the position; a thing heard but not
        # located (too few proxies for a fix) never gets that write, so it is
        # covered here too. The published gate keeps it to one write.
        if _presence_published.get(ent) == presence:
            continue
        attrs = _presence_attrs(seen.get("updated"), now, layout)
        for suffix in THING_SENSOR_SUFFIXES:
            _patch_sensor_attrs(hass, f"sensor.{ent}{suffix}", attrs)
        _presence_published[ent] = presence


THING_SENSOR_SUFFIXES = ("_sextant_room", "_sextant_floor", "_sextant_nearest_room", "_sextant_spot", "_sextant_location")


def _patch_sensor_attrs(hass, entity_id, attrs):
    """Change some attributes of a registered Sextant sensor, keeping the rest."""
    sensor = (hass.data.get("sextant_sensors") or {}).get(entity_id)
    if sensor is None:
        return
    update_sextant_sensor_state(hass, entity_id, sensor._state, {**(sensor._attrs or {}), **attrs})


async def _restore_runtime(hass):
    """Carry the last cycle's state over a restart (see runtime.py)."""
    # The restore has to happen before the first cycle, which puts it ahead of
    # the setup step that fills the layout cache - so it loads the layout
    # itself rather than reading an empty one. Its own window
    # (restore_state_secs) is a tuning key IN that layout: read too early, a
    # window David had set to twenty minutes was silently the five-minute
    # default, and a reboot that took five minutes ten seconds resumed nothing.
    layout = get_layout(hass)
    if not isinstance(layout, dict):
        layout = await load_layout(hass)
    if not isinstance(layout, dict):
        layout = {}
    try:
        data = await load_runtime(hass)
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Saved runtime state not loaded: %s", e)
        return
    back = runtime_mod.restore(data, time.time(), _tuning(layout, "restore_state_secs"))
    _last_seen.update(back["last"])
    _seed_last_seen_from_history(hass)
    now = time.time()
    for entity, kf in back["kf"].items():
        # Stamped now, velocity dropped: the saved ts is the downtime old, and
        # _kalman_update resets anything past KF_MAX_GAP_S - any restart
        # longer than half a minute threw the restored filter straight away.
        x = np.array(kf["x"], dtype=float)
        x[2:] = 0.0
        _kf_position_state[entity] = {
            "x": x, "P": np.array(kf["P"], dtype=float),
            "ts": now, "floor": kf["floor"],
        }
    for entity, state in back["zone"].items():
        # Filled out against the live shape: a key the snapshot predates (or
        # never carried) has to read as "no value", not raise mid-election.
        _zone_state[entity] = {**_new_zone_state(state.get("floor"), now), **state}
    for entity, state in back["spot"].items():
        # The election stores its answer as a tuple; JSON made it a list.
        value = state.get("value")
        if isinstance(value, list) and len(value) == 2:
            state = {**state, "value": tuple(value)}
        # And so is the pending challenger's, inside (candidate, ts): left a
        # list it never equals the tuple candidate, so the dwell restarted.
        pending = state.get("pending")
        if (isinstance(pending, list) and len(pending) == 2
                and isinstance(pending[0], list) and len(pending[0]) == 2):
            state = {**state, "pending": (tuple(pending[0]), pending[1])}
        _subzone_state[entity] = {**_new_subzone_state(state.get("floor"), state.get("zone"), now), **state}
    _arrivals.update(back["arrivals"])
    # The floor election, and with it the fact that this thing's floor is not
    # NEW. A cycle that finds no incumbent floor for a thing treats the one it
    # elects as a change, and a floor change clears the Kalman filter and the
    # room and spot elections - which is exactly what used to happen to every
    # restored thing on the first cycle after a restart, a second after the
    # restore had put it all back.
    _restore_floor_elections(back["floors"])
    if back["age"] is None:
        _LOGGER.info("No saved state to resume from; starting cold")
    elif back["kf"] or back["zone"]:
        _LOGGER.info("Resumed %d things after %.0f s down", len(back["zone"] or back["kf"]), back["age"])
    else:
        # A warning, not an info: state was thrown away, and the line says by
        # how much the window was missed - which is the one thing you want to
        # know when every thing comes back reading "here for 0 minutes".
        _LOGGER.warning(
            "Down for %.0f s, past the %.0f s restore window (restore_state_secs): "
            "elections start cold, keeping %d last sightings",
            back["age"], _tuning(layout, "restore_state_secs"), len(back["last"]),
        )


def _thing_floors():
    """``update_trilateration_and_zone.last_floor``, which the cycle creates lazily.

    The restore runs before the first cycle, so it has to be ready to make it.
    """
    if not hasattr(update_trilateration_and_zone, "last_floor"):
        update_trilateration_and_zone.last_floor = {}
    return update_trilateration_and_zone.last_floor


def _restore_floor_elections(saved):
    """Put back each thing's elected floor, its tenure and its probabilities."""
    last_floor = _thing_floors()
    for entity, state in (saved or {}).items():
        if not isinstance(state, dict):
            continue
        name, since, probs = state.get("name"), state.get("since"), state.get("probs")
        if isinstance(name, str):
            last_floor[entity] = name
        if isinstance(since, (int, float)):
            _floor_since[entity] = float(since)
        if isinstance(probs, dict):
            kept = {f: float(p) for f, p in probs.items() if isinstance(p, (int, float))}
            if kept:
                _floor_probability[entity] = kept


def _floor_elections():
    """Each thing's elected floor, when it was elected, and its smoothed
    probabilities: {ent: {"name", "since", "probs"}}, for the snapshot."""
    last_floor = _thing_floors()
    return {
        entity: {"name": last_floor.get(entity), "since": _floor_since.get(entity),
                 "probs": _floor_probability.get(entity)}
        for entity in set(last_floor) | set(_floor_since) | set(_floor_probability)
    }


def _rows_with_last_seen(rows, last_seen):
    """The published rows, plus a row for every thing that has gone quiet but
    whose last sighting is still remembered - the snapshot builds its record
    of last sightings from these."""
    live = {r.get("ent") for r in rows}
    return rows + [
        {"ent": ent, "zone": v.get("zone"), "sub_zone": v.get("spot"), "floor": v.get("floor"),
         "updated": v.get("updated"), "cords": v.get("cords")}
        for ent, v in last_seen.items() if ent not in live and isinstance(v, dict)
    ]


def _seed_last_seen_from_history(hass):
    """Give a last sighting to anything the position history has seen and
    _last_seen has not: the history outlives any restart, so its last point is
    a real sighting, with the room and floor it was in.

    Called from the runtime restore AND again once the history has loaded. The
    restore runs first, ahead of the setup step that reads the history off
    disk, so on a real restart the history it asked was still empty and this
    fallback never once did anything - the same ordering trap as the restore
    window reading an unloaded layout.
    """
    try:
        hist = get_position_history(hass)
        for entity in hist.entities():
            if entity in _last_seen:
                continue
            span = hist.retained(entity)
            if not (span and isinstance(span.get("to"), (int, float))):
                continue
            seen = {"zone": None, "spot": None, "floor": None, "updated": span["to"], "cords": None}
            try:
                q = hist.query(entity, span["to"] - 1, span["to"] + 1, 4)
                if q.get("t"):
                    i = len(q["t"]) - 1
                    fi, zi, si = q["f"][i], q["z"][i], q["sp"][i]
                    seen["floor"] = q["floors"][fi] if isinstance(fi, int) and fi < len(q["floors"]) else None
                    seen["zone"] = (q["zones"][zi] or None) if isinstance(zi, int) and zi < len(q["zones"]) else None
                    seen["spot"] = (q["spots"][si] or None) if isinstance(si, int) and si < len(q["spots"]) else None
            except Exception:  # noqa: BLE001 - the time alone is still worth having
                pass
            _last_seen[entity] = seen
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("No history to date the last sighting from: %s", e)


async def _save_runtime(hass):
    """Write the state a restart would otherwise lose."""
    try:
        rows = [r for r in (hass.data.get(DOMAIN, {}).get("apitricords") or []) if isinstance(r, dict)]
        # A thing that has gone quiet is pruned out of the rows, but where it
        # was last heard is precisely what has to survive a restart - that is
        # the whole of "away since". Built from the rows alone, a stale thing's
        # sighting lasted one restart at most: Socks, silent since 5:42 AM on
        # a dying tag, was gone entirely after the 15:34 restart.
        rows = _rows_with_last_seen(rows, _last_seen)
        data = runtime_mod.snapshot(time.time(), kf=_kf_position_state, zones=_zone_state,
                                    spots=_subzone_state, arrivals=_arrivals, rows=rows,
                                    floors=_floor_elections())
        await save_runtime(hass, data)
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Could not save the runtime state: %s", e)


def _history_arrival(hass, ent, floor, x, y, now):
    """When the position history says this thing came to stay within a couple
    of metres of (x, y), or None when it has nothing to say (yet)."""
    try:
        q = get_position_history(hass).query(ent, now - 86400, now, 5000)
        floors = q.get("floors") or []
        points = [(t, floors[fi] if isinstance(fi, int) and fi < len(floors) else fi, xm, ym)
                  for t, fi, xm, ym in zip(q.get("t", []), q.get("f", []), q.get("x_m", []), q.get("y_m", []))]
        return persons_mod.settled_since(points, (floor, x, y))
    except Exception:  # noqa: BLE001 - no history is not an error
        return None


# How long after first seeing a thing the history is asked again when it had
# no answer: it is loaded from disk a little after the first cycle of a start.
ARRIVAL_RETRY_SECS = 600.0


def _arrived_at(hass, layout, ent, row, now):
    """When this thing arrived within a couple of metres of where it is now.

    The position history decides, whenever a thing is first seen or has
    moved: it outlives restarts and passes over short absences, so neither a
    restart nor the wrong-floor fix that often follows one makes a thing that
    has sat on a nightstand all night look freshly arrived. Between moves the
    answer is kept. A history with nothing to say yet (it loads a little
    after the first cycle) is asked again rather than taken as "just now".
    """
    floor, cords = row.get("floor"), row.get("cords")
    scale = next((f.get("scale") for f in layout.get("floor") or [] if f.get("name") == floor), None)
    if not floor or not cords or not scale:
        return now
    x, y = float(cords[0]) / float(scale), float(cords[1]) / float(scale)
    st = _arrivals.get(ent)
    if st is not None and st["floor"] == floor and math.hypot(x - st["x"], y - st["y"]) <= persons_mod.STAY_RADIUS_M:
        st["away_since"] = None
        if st["provisional"] and now - st["first_seen"] <= ARRIVAL_RETRY_SECS:
            found = _history_arrival(hass, ent, floor, x, y, now)
            if found is not None:
                # Answered: take it if earlier, and stop asking either way (a
                # 24 h history query per owned thing per cycle otherwise).
                st["since"], st["provisional"] = min(found, st["since"]), False
        return st["since"]
    fallback = now
    if st is not None:
        # Elsewhere: a move only once it has lasted (a stray fix is not one).
        st["away_since"] = st.get("away_since") or now
        if now - st["away_since"] < persons_mod.MOVE_CONFIRM_SECS:
            return st["since"]
        fallback = st["away_since"]
    found = _history_arrival(hass, ent, floor, x, y, now)
    _arrivals[ent] = {"floor": floor, "x": x, "y": y, "since": found if found is not None else fallback,
                      "provisional": found is None, "first_seen": now, "away_since": None}
    return _arrivals[ent]["since"]


def _update_person_sensors(hass):
    """Each owner's location from the thing that speaks for them (persons.py)."""
    layout = get_layout(hass)
    by_person = persons_mod.owners(layout)
    from . import sensor as sensor_mod  # noqa: PLC0415 - the platform imports this module

    # Someone who no longer owns anything loses their sensors; only looked at
    # when the set of owners changes, so steady state costs a comparison.
    owning = frozenset(by_person)
    if hass.data.get("sextant_person_owners") != owning:
        sensor_mod.prune_person_sensors(hass, owning)
        try:
            from . import device_tracker as tracker_mod  # noqa: PLC0415
            tracker_mod.prune_person_trackers(hass, owning)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Person trackers not pruned: %s", e)
        hass.data["sextant_person_owners"] = owning
    for ent in [e for e in _arrivals if not any(e in things for things in by_person.values())]:
        del _arrivals[ent]
    if not by_person:
        return
    sensor_mod.ensure_person_sensors(hass, list(by_person))
    rows = {r.get("ent"): r for r in (hass.data.get(DOMAIN, {}).get("apitricords") or []) if isinstance(r, dict)}
    classes = layout.get("thing_classes") or {}
    chargers = layout.get("thing_charging_entity") or {}
    now, stale = time.time(), _tuning(layout, "stale_after_secs")
    home = (getattr(hass.config, "latitude", None), getattr(hass.config, "longitude", None))
    gps_stale = _tuning(layout, "gps_stale_secs")
    trackers = hass.data.get("sextant_trackers") or {}
    try:
        from . import device_tracker as tracker_mod  # noqa: PLC0415 - the platform imports this module
        tracker_mod.ensure_person_trackers(hass, list(by_person))
    except Exception as e:  # noqa: BLE001 - the sensors do not depend on the trackers
        _LOGGER.debug("Person trackers not ensured: %s", e)
    for person, things in by_person.items():
        candidates, presences = [], []
        for ent in things:
            if not persons_mod.locates_owner(layout, ent, classes.get(ent)):
                continue
            seen = _last_seen.get(ent) or {}
            presences.append(_presence_of(seen.get("updated") if isinstance(seen, dict) else None, now, layout))
            row = rows.get(ent)
            if not row:
                continue
            candidates.append({
                "ent": ent, "cls": classes.get(ent), "updated": row.get("updated"),
                "arrived": _arrived_at(hass, layout, ent, row, now),
                "zone": row.get("zone"), "sub_zone": row.get("sub_zone"), "floor": row.get("floor"),
                "area": room_area(layout, row.get("floor"), row.get("zone")),
                "on_charger": bool(chargers.get(ent)) and persons_mod.on_charger(
                    getattr(hass.states.get(chargers[ent]), "state", None)),
            })
        slug = person.split(".", 1)[1]
        why = persons_mod.considered(candidates, now)
        presence = persons_mod.presence_of_things(presences)
        best = persons_mod.pick(candidates, now, stale)
        if best is not None:
            _person_last[person] = (best, why)
        # Quiet: the last place they were put stands until BLE either hears
        # them again or gives up (away), when GPS takes over.
        held, held_why = _person_last.get(person, (None, None)) if best is None and presence == "quiet" else (None, None)
        gps, ignored = persons_mod.choose_gps(hass.states.get, layout, person, now, gps_stale)
        for suffix, (state, attrs) in persons_mod.fuse(best, why if best else held_why, presence, held, gps, ignored, home).items():
            update_sextant_sensor_state(hass, f"sensor.{slug}_{suffix}", state, attrs)
        tracker = trackers.get(slug)
        if tracker is not None:
            tracker.set_fix(persons_mod.tracker_fix(presence, gps))

def extract_candidate_floors(new_global_data, tmpentity):
    """Every floor hearing the thing, ranked by its nearest receiver.

    Returns a list of {"name", "cords": [(x, y, r, slant_px, quality), ...],
    "nearest_m"} sorted by nearest_m — slant_px is the measured (corrected)
    slant distance in this floor's pixels, carried alongside the projected
    radius r so the solver can weight by what was MEASURED rather than by the
    projection (whose height-corrected value can legitimately collapse to the
    minimum); quality is the reading's own reliability in (0, 1] (see
    _median_distance), folded into the solver weight by the caller. The
    per-floor list is capped to the nearest receivers (_select_receivers,
    tuning solver_max_receivers / solver_max_range / solver_near_always).
    The ranking compares raw slant distances (meters),
    not radii: radii are scaled into each floor's own pixel space, so
    comparing them across floors would let the floor with the smallest scale
    win regardless of where the thing actually is. Ties (the same receiver
    placed on several floors) keep the data file's floor order. Each floor's
    cords feed that floor's own candidate solve — cords from different floors
    live in different pixel coordinate systems and never mix.
    """
    candidates = []
    for entity in new_global_data:
        if entity["entity"] != tmpentity:
            continue
        layout = entity["data"]
        max_receivers = _tuning(layout, "solver_max_receivers")
        max_range = _tuning(layout, "solver_max_range")
        near_always = _tuning(layout, "solver_near_always")
        prox_k = max(1, int(_tuning(layout, "floor_proximity_k")))
        for floor in layout["floor"]:
            entries = []
            for receiver in floor["receivers"]:
                distance = receiver.get("distance")
                if distance is None or "r" not in receiver.get("cords", {}):
                    continue
                if receiver.get("solve") is False:
                    continue  # placed and heard, but kept out of positioning (see _solves)
                quality = receiver.get("quality")
                if not isinstance(quality, (int, float)) or isinstance(quality, bool) \
                        or not 0 < quality <= 1:
                    quality = 1.0
                entries.append((distance, (
                    receiver["cords"]["x"], receiver["cords"]["y"],
                    receiver["cords"]["r"], distance * floor["scale"], float(quality),
                ), receiver.get("entity_id")))
            # Nearest-K cap: with thirty receivers on a floor, the far ones
            # contribute mostly noise, and 1/r^2 does not zero them out.
            cords, heard = _select_receivers(entries, max_receivers, max_range, near_always)
            nearest = min((e[0] for e in entries), default=float("inf"))
            ranked = sorted(e[0] for e in entries)[:prox_k]
            if cords:
                candidates.append({
                    "name": floor["name"],
                    "cords": cords,
                    "heard": heard,
                    "nearest_m": nearest,
                    # Mean of the floor_proximity_k nearest slants: what the
                    # proximity term compares across floors.
                    "near_k_m": sum(ranked) / len(ranked) if ranked else float("inf"),
                })
    candidates.sort(key=lambda c: c["nearest_m"])  # stable: file order breaks ties
    return candidates


def _score_floor_fit(fix, weighted, scale):
    """Score one candidate floor's solve. Returns (confidence, rms_m, coverage).

    Confidence blends how well the fix explains ALL of the floor's reporting
    receivers (weighted RMS residual, converted to metres so floors with
    different pixel scales compare fairly) with how many receivers corroborate
    it. Modelled on ESPresense's scenario confidence (fit quality + node
    coverage): the thing's true floor tends to explain its whole receiver
    ensemble, while a wrong floor fits one loud through-slab reading and
    contradicts the rest.

    Not always, measured: a wood floor shortens every through-slab reading by
    about the same factor (0.65x the true 3D range), so the floor below can
    fit a still phone as well as its own floor does, and better once the
    phone's own fix strays over a void and takes the no-go penalty. That is
    what the proximity term and floor_proximity_blend are for.
    """
    x, y = fix
    n = len(weighted)
    num = den = 0.0
    for pt in weighted:  # (x, y, r, w[, slant_px]) — indexed so both shapes work
        xi, yi, ri, wi = pt[0], pt[1], pt[2], pt[3]
        res = math.hypot(xi - x, yi - y) - ri
        num += wi * res * res
        den += wi
    rms_px = math.sqrt(num / den) if den > 0 else float("inf")
    # Reduced-chi-square-style correction: a 3-receiver floor fits the 2
    # position unknowns almost perfectly no matter what (a single residual
    # degree of freedom), which flatters exactly the floors with the least
    # evidence. Inflate by sqrt(n / (n - 2)) so fits compete on evidence.
    rms_px *= math.sqrt(n / max(n - 2.0, 1.0))
    rms_m = rms_px / scale if scale else rms_px
    quality = 1.0 / (1.0 + (rms_m / FLOOR_RESIDUAL_SCALE_M) ** 2)
    # Corroboration: how many receivers hear the thing on this floor,
    # saturating at COVERAGE_TARGET_N. An absolute count, NOT a share of the
    # floor's placed receivers: a dead or unmatched placement must not
    # handicap its floor forever, and a tiny fully-reporting 3-receiver floor
    # must not out-cover a floor with 6 of 8 receivers reporting.
    coverage = min(1.0, n / COVERAGE_TARGET_N)
    return 0.5 * coverage + 0.5 * quality, rms_m, coverage


def _floor_frames(hass, layout):
    """Each floor's frame into house metres, cached per layout version."""
    cache = _zone_poly_cache(hass)
    version = get_layout_version(hass)
    cached = cache.get(("registration",))
    if cached is not None and cached[0] == version:
        return cached[1]
    frames = registration.solve(layout).get("floors", {})
    cache[("registration",)] = (version, frames)
    return frames


def _house_position(frame, fix):
    """``{"house": [x, y, z]}`` in metres for a registered floor, else ``{}``."""
    at = registration.to_house(frame, float(fix[0]), float(fix[1]))
    if at is None:
        return {}
    return {"house": [round(at[0], 2), round(at[1], 2), round(frame["elevation"], 2)]}


def _floor_bias(layout, floor_name, fix=None):
    """The floor's election prior: its scalar bias, shaped by its bias field.

    Multiplies the floor's score before the probabilities are updated, so a
    bias of 1.2 is a 20 % head start in every cycle, not a one-off nudge.
    A scalar that is not a number in (0, 10] is ignored.

    ``fix`` is this floor's OWN candidate fix this cycle, in its own pixels.
    Given one, the scalar is multiplied by the floor's bias field sampled
    there (floor_field.py) - the scalar is one prior for the whole plan, the
    field is where on the plan that prior is wrong. No fix, no field, or a
    flat field all leave the scalar exactly as it was.
    """
    floors = layout.get("floor") if isinstance(layout, dict) else None
    for floor in floors or []:
        if isinstance(floor, dict) and floor.get("name") == floor_name:
            bias = floor.get("bias")
            if not (isinstance(bias, (int, float)) and not isinstance(bias, bool) and 0 < bias <= 10):
                bias = 1.0
            return float(bias) * (floor_field.sample(floor, fix) if fix is not None else 1.0)
    return 1.0


def floor_bias_map(layout, frames, floor_name, other_name, cell_m=0.5):
    """How much this floor's election prior favours it over another floor, place by place.

    For a grid of points inside this floor's rooms: this floor's bias (scalar
    x field) there, divided by the other floor's bias at the same place in
    the house - mapped through the floors' registration when both are
    registered, by metres from the plan origin otherwise. Above 1, a thing
    here leans to this floor; below 1, to the other. Returns
    ``{cell_px, registered, cells: [[x, y, ratio]], min, max}`` in this floor's pixels.
    """
    floors = {f.get("name"): f for f in (layout or {}).get("floor") or [] if isinstance(f, dict)}
    mine, other = floors.get(floor_name), floors.get(other_name)
    if mine is None or other is None or not mine.get("scale") or not other.get("scale"):
        return None
    scale_a, scale_b = float(mine["scale"]), float(other["scale"])
    rooms = [Polygon([(c["x"], c["y"]) for c in z.get("cords") or []])
             for z in mine.get("zones") or [] if len(z.get("cords") or []) >= 3 and not z.get("no_go")]
    rooms = [r if r.is_valid else r.buffer(0) for r in rooms]
    if not rooms:
        return None
    x0 = min(r.bounds[0] for r in rooms); y0 = min(r.bounds[1] for r in rooms)
    x1 = max(r.bounds[2] for r in rooms); y1 = max(r.bounds[3] for r in rooms)
    step = cell_m * scale_a
    fa, fb = frames.get(floor_name), frames.get(other_name)
    registered = bool(fa and fa.get("ok") and fb and fb.get("ok"))
    cells = []
    y = y0 + step / 2
    while y < y1:
        x = x0 + step / 2
        while x < x1:
            if any(r.covers(Point(x, y)) for r in rooms):
                if registered:
                    hx, hy = registration.to_house(fa, x, y)
                    there = registration.from_house(fb, hx, hy)
                else:
                    there = (x / scale_a * scale_b, y / scale_a * scale_b)
                a = _floor_bias(layout, floor_name, (x, y))
                b = _floor_bias(layout, other_name, there)
                cells.append([round(x, 1), round(y, 1), round(a / b, 4) if b > 0 else 1.0])
            x += step
        y += step
    ratios = [c[2] for c in cells] or [1.0]
    return {"cell_px": step, "registered": registered, "cells": cells, "min": min(ratios), "max": max(ratios)}


def _proximity_weighted_scores(scores, nearest_by_floor, weight, blend="gated"):
    """Scale each solved floor's confidence by receiver proximity.

    ``prox`` for a floor is the nearest measured distance on ANY competing
    floor divided by this floor's own nearest, so the floor with the nearest
    receiver scores 1 and a floor whose closest receiver is twice as far
    scores 0.5. "gated" multiplies the confidence by
    ``(1 - weight) + weight * prox``; "geometric" takes the weighted
    geometric mean ``conf ** (1 - weight) * prox ** weight``, so a floor
    whose fit is poor (a fix a metre over a void, the no-go penalty applied)
    is not capped by that fit when its proxies are plainly the nearest. With
    one solved floor, an unusable distance, or weight 0, the scores pass
    through unchanged. Distances are the raw slants (metres) the candidate
    ranking already uses, so a through-slab reading directly below a thing
    counts against the floor below it just as it did in the old
    nearest-receiver election - but now as one weighted term inside the fit
    competition, behind the same margin and dwell, rather than as the whole
    answer.
    """
    if not scores or not weight or weight <= 0:
        return dict(scores)
    usable = {
        f: float(d) for f, d in nearest_by_floor.items()
        if f in scores and isinstance(d, (int, float)) and not isinstance(d, bool)
        and d > 0 and math.isfinite(d)
    }
    if len(usable) < 2:
        return dict(scores)
    best = min(usable.values())
    out = {}
    for floor, conf in scores.items():
        prox = best / usable[floor] if floor in usable else 1.0
        if blend == "geometric":
            out[floor] = (max(conf, 0.0) ** (1.0 - weight)) * (prox ** weight)
        else:
            out[floor] = conf * ((1.0 - weight) + weight * prox)
    return out


def _update_floor_probabilities(entity, scores, valid_floors=None, now=None):
    """Fold this cycle's per-floor confidences into smoothed probabilities.

    Each floor's probability moves 1-FLOOR_PROB_SMOOTHING of the way toward
    its share of this cycle's total confidence; floors with no data this
    cycle decay toward zero and are dropped once negligible, so the dict
    cannot grow unbounded. Floors no longer present in the data file
    (renamed/deleted) are dropped immediately — a ghost name must not hold
    probability mass nor appear in the published election. Returns a
    normalized copy.
    """
    probs = _floor_probability.setdefault(entity, {})
    now = time.time() if now is None else now
    # No history (new, or just reset): the step is a whole one, as tuned.
    keep = _step_weight(FLOOR_PROB_SMOOTHING, _floor_probability_at.get(entity) if probs else None, now)
    _floor_probability_at[entity] = now
    if valid_floors is not None:
        for floor in [f for f in probs if f not in valid_floors]:
            del probs[floor]
    total = sum(scores.values())
    for floor in set(probs) | set(scores):
        target = (scores.get(floor, 0.0) / total) if total > 0 else 0.0
        probs[floor] = keep * probs.get(floor, 0.0) + (1.0 - keep) * target
    for floor in [f for f, p in probs.items() if p < 0.01]:
        del probs[floor]
    norm = sum(probs.values())
    if norm > 0:
        for floor in probs:
            probs[floor] /= norm
    return dict(probs)


def _elect_floor(probs, incumbent, solved, challenge, now=None,
                 switch_secs=FLOOR_SWITCH_SECS, margin=FLOOR_SWITCH_MARGIN):
    """Pick the floor to publish. Returns (floor, challenge_state).

    The caller guarantees the incumbent is either solved this cycle or None
    (a dark incumbent is handled by the grace hold upstream, and one dark
    beyond the grace loses its standing entirely — incumbency is never
    transferred by forfeit, it lapses).

    A solved incumbent only loses to a challenger that leads its probability
    by ``margin`` continuously for ``switch_secs`` of wall-clock time
    (challenge_state carries the challenge's start between cycles; a cycle
    where the lead lapses ends the challenge). The margin filters share
    noise, the dwell filters geometry flukes, and together they cannot
    permanently dead-band a genuinely better floor the way a large margin
    alone would — floor flapping was the disease (issue #94), a stuck wrong
    floor must not be the cure. Wall clock rather than cycles because the
    update interval is configurable: "three cycles" meant 30 s at one
    setting and 3 s at another.
    """
    now = time.time() if now is None else now
    contenders = {f: p for f, p in probs.items() if f in solved}
    if not contenders:
        return None, None
    best = max(contenders, key=contenders.get)
    if incumbent is None or incumbent not in contenders:
        return best, None  # no standing incumbent: adopt the best immediately
    if best == incumbent or contenders[best] - contenders[incumbent] < margin:
        return incumbent, None
    since = challenge["since"] if challenge and challenge.get("floor") == best else now
    if now - since >= switch_secs:
        return best, None
    return incumbent, {"floor": best, "since": since}


# --- Zone election ------------------------------------------------------------
ZONE_SAMPLE_ANGLES = np.linspace(0.0, 2.0 * np.pi, 8, endpoint=False)
ZONE_SAMPLE_CENTER_WEIGHT = 0.4  # the rest is spread over the 1-sigma ring


def _covariance_samples(center, cov, scale=None):
    """Weighted (x, y, w) samples: the centre plus eight points on the
    1-sigma error ellipse of a 2x2 position covariance (pixels^2).

    Not a Gaussian quadrature - a cheap, deterministic stand-in for "how much
    of the position's uncertainty lies in each zone", which is all the zone
    election needs. Falls back to the centre alone when the covariance is
    unusable.
    """
    x0, y0 = float(center[0]), float(center[1])
    samples = [(x0, y0, ZONE_SAMPLE_CENTER_WEIGHT)]
    try:
        vals, vecs = np.linalg.eigh(np.asarray(cov, dtype=float).reshape(2, 2))
    except (np.linalg.LinAlgError, ValueError, TypeError):
        return [(x0, y0, 1.0)]
    if not np.all(np.isfinite(vals)) or not np.all(np.isfinite(vecs)):
        return [(x0, y0, 1.0)]
    a, b = np.sqrt(np.clip(vals, 0.0, None))
    u, v = vecs[:, 0], vecs[:, 1]
    w = (1.0 - ZONE_SAMPLE_CENTER_WEIGHT) / len(ZONE_SAMPLE_ANGLES)
    for th in ZONE_SAMPLE_ANGLES:
        c, s = math.cos(th), math.sin(th)
        samples.append((x0 + c * a * u[0] + s * b * v[0], y0 + c * a * u[1] + s * b * v[1], w))
    return samples


def _zone_membership(zone_polys, samples):
    """zone_id -> share of sample weight in (or, failing that, nearest to)
    each allowed zone. No-go zones never receive membership."""
    shares = {}
    if isinstance(zone_polys, _FloorZones):
        # One vectorised covers (and, for points outside every zone, one
        # distance) over zones x samples, instead of a Python shapely call per
        # pair: up to nine samples times every room, per thing per cycle.
        ids, geoms = zone_polys.allowed_ids, zone_polys.allowed_geoms
        if not ids or not samples:
            return {}
        pts = shapely.points([(x, y) for x, y, _w in samples])
        covered = shapely.covers(geoms[:, None], pts[None, :])
        inside = covered.any(axis=0)
        pick = np.argmax(covered, axis=0)  # the first covering zone, as below
        if not inside.all():
            outside = np.flatnonzero(~inside)
            pick[outside] = np.argmin(shapely.distance(geoms[:, None], pts[outside][None, :]), axis=0)
        for i, (_x, _y, w) in enumerate(samples):
            zid = ids[int(pick[i])]
            shares[zid] = shares.get(zid, 0.0) + w
        total = sum(shares.values())
        return {z: v / total for z, v in shares.items()} if total > 0 else {}
    allowed = [(zid, poly) for zid, poly, _b, no_go in zone_polys if not no_go]
    if not allowed:
        return {}
    for x, y, w in samples:
        pt = Point(x, y)
        hit = None
        for zid, poly in allowed:
            if poly.covers(pt):
                hit = zid
                break
        if hit is None:
            hit = min(allowed, key=lambda zp: zp[1].distance(pt))[0]
        shares[hit] = shares.get(hit, 0.0) + w
    total = sum(shares.values())
    return {z: s / total for z, s in shares.items()} if total > 0 else {}


def _elect_zone(entity, floor_name, instant_zone, point, kf_state, zone_polys, scale, layout, now=None):
    """The zone to PUBLISH for a thing this cycle. Returns
    (zone, locked, speed_m_s).

    Floors already had hysteresis; zones were assigned point-wise every
    cycle, so a thing resting near a boundary toggled with every fit.
    Three mechanisms, all tunable (TUNING_SPEC, sextant.set_tuning):

    1. Membership probability. The published point's zone is not a yes/no:
       samples on the Kalman filter's 1-sigma error ellipse are attributed
       to zones, giving a share per zone, smoothed over cycles (EMA).
    2. Margin and dwell. The incumbent zone holds until a challenger leads
       its smoothed share by zone_switch_margin continuously for
       zone_switch_secs of wall clock.
    3. Stationary lock. When the filter's speed stays under stationary_speed
       for stationary_secs, the thing is on a table and the zone locks -
       but only a zone it has EARNED: held for stationary_secs already, and
       still the best-supported zone at that moment. Locking whatever was
       elected first froze a guess: after a restart the first cycle's room
       is one noisy fit, and a watch on a couch a metre from three room
       edges was locked into the foyer that way.
       The lock releases when the point sits more than zone_unlock_margin
       metres outside the locked zone for zone_unlock_secs (the time
       already spent away then counts toward the dwell, so the switch
       follows at once), when the thing is clearly moving again for
       stationary_secs, or when the locked zone has all but lost the
       evidence (under ZONE_OUTVOTED_SHARE of it) for twice
       zone_unlock_secs - a lock is there to hold through jitter, not to
       outlast the thing being somewhere else.

    nearest_zone stays instantaneous for automations that want the raw
    answer, and zone_raw in the API carries the point's own zone. Turning
    zone_hysteresis off publishes zone_raw as before.
    """
    now = time.time() if now is None else now
    valid = {zid for zid, _p, _b, no_go in zone_polys if not no_go}
    if not _tuning(layout, "zone_hysteresis") or not valid:
        _zone_state.pop(entity, None)
        return instant_zone, False, None

    st = _zone_state.get(entity)
    if st is None or st["floor"] != floor_name or (st["zone"] is not None and st["zone"] not in valid):
        st = _zone_state[entity] = _new_zone_state(floor_name, now)

    # 1. Membership, smoothed.
    center = (point.x, point.y) if isinstance(point, Point) else (point[0], point[1])
    speed = None
    if kf_state is not None and kf_state.get("floor") == floor_name:
        samples = _covariance_samples(center, kf_state["P"][:2, :2])
        if isinstance(scale, (int, float)) and scale > 0:
            speed = math.hypot(float(kf_state["x"][2]), float(kf_state["x"][3])) / scale
    else:
        samples = [(center[0], center[1], 1.0)]
    shares = _zone_membership(zone_polys, samples)
    if not shares and instant_zone in valid:
        shares = {instant_zone: 1.0}
    alpha = _step_weight(_tuning(layout, "zone_prob_smoothing"), st.get("probs_at") if st["probs"] else None, now)
    st["probs_at"] = now
    probs = st["probs"]
    for z in set(probs) | set(shares):
        probs[z] = alpha * probs.get(z, 0.0) + (1.0 - alpha) * shares.get(z, 0.0)
    for z in [z for z, p in probs.items() if p < 0.01 or z not in valid]:
        del probs[z]
    norm = sum(probs.values())
    if norm > 0:
        for z in probs:
            probs[z] /= norm

    # 3a. Stillness / motion clocks.
    if speed is not None:
        if speed < _tuning(layout, "stationary_speed"):
            st["still_since"] = st["still_since"] or now
            st["moving_since"] = None
        else:
            st["still_since"] = None
            st["moving_since"] = st["moving_since"] or now
    else:
        st["still_since"] = st["moving_since"] = None

    incumbent = st["zone"]
    if not probs:
        return (incumbent if incumbent is not None else instant_zone), False, speed
    best = max(probs, key=probs.get)
    if incumbent is None:
        st.update(zone=best, since=now, challenge=None, locked=False, away_since=None)
        return best, False, speed

    # 3b. Lock and unlock.
    stationary_secs = _tuning(layout, "stationary_secs")
    if (
        not st["locked"]
        and st["still_since"] is not None
        and now - st["still_since"] >= stationary_secs
        and now - st["since"] >= stationary_secs   # held long enough to mean something
        and best == incumbent                      # and the evidence still says so
        and now - st.get("born", now) >= _tuning(layout, "zone_lock_warmup_secs")  # not in the settling first minutes
    ):
        st["locked"] = True
        st["away_since"] = None
        st["outvoted_since"] = None
    challenge_since = None
    if st["locked"]:
        incumbent_poly = next((p for zid, p, _b, _n in zone_polys if zid == incumbent), None)
        margin_px = _tuning(layout, "zone_unlock_margin") * (scale if isinstance(scale, (int, float)) and scale > 0 else 1.0)
        pt = point if isinstance(point, Point) else Point(center)
        gap = None if incumbent_poly is None else incumbent_poly.distance(pt)
        away = gap is None or gap > margin_px

        # Starting and clearing the away clock on the same threshold makes a fix
        # that rests *at* the margin permanently unreleasable. A bag in the
        # laundry room solved 1.02 m from the foyer against a 1.00 m margin: the
        # clock started, then any cycle that wobbled a centimetre closer wiped
        # it, so the 30 s dwell never completed. The room sensor read foyer for
        # as long as the bag sat there, while the map drew it where it really
        # was. So coming back has to mean coming properly back, not merely
        # dipping under the line the clock started on.
        if away:
            st["away_since"] = st["away_since"] or now
        elif gap is not None and gap <= margin_px * ZONE_AWAY_RESET_FRACTION:
            st["away_since"] = None
        unlock_secs = _tuning(layout, "zone_unlock_secs")
        left_for_long = st["away_since"] is not None and now - st["away_since"] >= unlock_secs
        moving_for_long = st["moving_since"] is not None and now - st["moving_since"] >= stationary_secs
        # The evidence has all but abandoned the locked zone. Deliberately a
        # near-zero share rather than "another zone leads": a thing resting on
        # a boundary splits its evidence about evenly, and holding through
        # exactly that is what the lock is for.
        if probs.get(incumbent, 0.0) < ZONE_OUTVOTED_SHARE:
            st["outvoted_since"] = st["outvoted_since"] or now
        else:
            st["outvoted_since"] = None
        outvoted_for_long = st["outvoted_since"] is not None and now - st["outvoted_since"] >= 2 * unlock_secs
        if not left_for_long and not moving_for_long and not outvoted_for_long:
            st["challenge"] = None
            return incumbent, True, speed
        # Unlocked. Time already spent outside counts toward the dwell below.
        challenge_since = st["away_since"] or st["outvoted_since"]
        st.update(locked=False, still_since=None, away_since=None, outvoted_since=None)

    # 2. Margin and dwell.
    margin = _tuning(layout, "zone_switch_margin")
    if best == incumbent or probs[best] - probs.get(incumbent, 0.0) < margin:
        st["challenge"] = None
        return incumbent, False, speed
    ch = st["challenge"]
    if ch and ch.get("zone") == best:
        since = ch["since"]
    else:
        since = challenge_since if challenge_since is not None else now
    if now - since >= _tuning(layout, "zone_switch_secs"):
        st.update(zone=best, since=now, challenge=None)
        return best, False, speed
    st["challenge"] = {"zone": best, "since": since}
    return incumbent, False, speed


# A proxy is only "clearly nearest" above this ratio to the runner-up.
SPOT_PROXY_MIN_RATIO = 1.25


def _spot_proxy_evidence(layout, proxies):
    """How strongly the proxies on a spot say the thing is on it, 0..1.

    ``proxies`` is the spot's own proxy or proxies (a couch with an outlet at
    each end). The distance term fades from 1 at spot_proxy_near_m to 0 at
    spot_proxy_far_m, so the nearest proxy three metres away says nothing.
    The ratio term needs every proxy NOT on the spot (any floor) to read
    farther: 0 when one is within SPOT_PROXY_MIN_RATIO, 1 from
    spot_proxy_ratio. The spot's proxies are not each other's rivals - two
    outlets on one couch both hearing a thing close is the point - so the
    evidence is the strongest of them.
    """
    if isinstance(proxies, str):
        proxies = (proxies,)
    if not proxies or not isinstance(layout, dict):
        return 0.0
    mine, others = [], []
    for floor in layout.get("floor") or []:
        for receiver in floor.get("receivers") or []:
            d = receiver.get("distance")
            if not isinstance(d, (int, float)) or isinstance(d, bool) or not d > 0 or not _solves(receiver):
                continue
            (mine if receiver.get("entity_id") in proxies else others).append(float(d))
    if not mine:
        return 0.0
    near = _tuning(layout, "spot_proxy_near_m")
    far = max(_tuning(layout, "spot_proxy_far_m"), near + 0.01)
    full = max(_tuning(layout, "spot_proxy_ratio"), SPOT_PROXY_MIN_RATIO + 0.01)
    best = 0.0
    for d in mine:
        by_distance = min(1.0, max(0.0, (far - d) / (far - near)))
        by_ratio = 1.0 if not others else min(1.0, max(0.0, (min(others) / d - SPOT_PROXY_MIN_RATIO) / (full - SPOT_PROXY_MIN_RATIO)))
        best = max(best, by_distance * by_ratio)
    return best


def _pin_positions():
    """slug -> (floor, x, y) for every location pin, in that floor's pixels."""
    return {f"mark:{m.get('id')}": (m.get("floor"), float(m.get("x", 0.0)), float(m.get("y", 0.0)))
            for m in _truth_marks if m.get("id") is not None}


def spot_pin_evidence(fp, floor_name, poly, pins=None, at=None, margin_px=0.0):
    """How much of a fingerprint fix came from pins inside this spot, 0..1.

    A fix is the weighted mean of its best matching references (weight
    1/(score+0.05)**2, see fingerprint.match), and a pin is a place the user
    pointed at. So the share of that weight sitting inside the spot says "the
    readings look like the times you said it was here" - evidence a proxy on
    the spot cannot give when the thing lying on it blocks that proxy (a cat
    on a couch reads the couch's own outlets twice too far).

    It only speaks for a thing whose fix is on or beside the spot (``at``,
    fading to nothing ``margin_px`` outside): pins of a class are shared, so
    a cat across the room still matches the pins on the couch, and without
    this it would be held on a couch it had left.
    """
    refs = (fp or {}).get("refs") or ()
    if not refs or poly is None:
        return 0.0
    near = 1.0
    if at is not None:
        away = poly.distance(Point(at[0], at[1]))
        near = 1.0 if away <= 0 else (max(0.0, 1.0 - away / margin_px) if margin_px > 0 else 0.0)
        if near <= 0:
            return 0.0
    if pins is None:
        pins = _pin_positions()
    inside = total = 0.0
    for slug, score in refs:
        try:
            w = 1.0 / (float(score) + 0.05) ** 2
        except (TypeError, ValueError):
            continue
        total += w
        at = pins.get(slug)
        if at and at[0] == floor_name and poly.contains(Point(at[1], at[2])):
            inside += w
    return (inside / total) * near if total > 0 else 0.0


def _spot_proxies(sub):
    """The proxies a spot names as sitting on it: ``proxy`` is one name or a list."""
    raw = sub.get("proxy")
    names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return tuple(n for n in names if isinstance(n, str) and n)


def _spot_settings(layout, floor_name):
    """Spot name -> {"proxies", "enter_prob"} for the spots on a floor that set either."""
    out = {}
    floors = layout.get("floor") if isinstance(layout, dict) else None
    for floor in floors or []:
        if floor.get("name") != floor_name:
            continue
        for sub in floor.get("subzones") or []:
            enter = sub.get("enter_prob")
            enter = float(enter) if isinstance(enter, (int, float)) and not isinstance(enter, bool) and 0.05 <= enter <= 0.95 else None
            proxies = _spot_proxies(sub)
            if enter is not None or proxies:
                out[sub.get("entity_id")] = {"proxies": proxies, "enter_prob": enter}
    return out


def _subzone_membership(sub_polys, samples):
    """sub-zone id -> share of sample weight inside it; samples in no
    sub-zone count toward "unknown". Unlike zones, sub-zones do not tile the
    room, so a sample outside every one of them is evidence of NO sub-zone,
    never of the nearest."""
    shares = {}
    for x, y, w in samples:
        pt = Point(x, y)
        hit = next((sid for sid, _parent, poly in sub_polys if poly.covers(pt)), "unknown")
        shares[hit] = shares.get(hit, 0.0) + w
    total = sum(shares.values())
    return {s: v / total for s, v in shares.items()} if total > 0 else {}


ANCHOR_CONF = 0.9   # an anchored fix is as sure as a fix gets


def _floor_receivers(layout, floor_name):
    floors = layout.get("floor") if isinstance(layout, dict) else None
    for floor in floors or []:
        if isinstance(floor, dict) and floor.get("name") == floor_name:
            return floor.get("receivers") or []
    return []


def _elect_anchor(entity, floor_name, receivers, layout, now=None):
    """The proxy this thing sits on, if any: {"slug", "x", "y"} or None.

    Trilateration is a compromise between every proxy's range, so a watch
    20 cm from one proxy still lands a metre or two away when the farther
    proxies read a little short. When ONE proxy reads the thing inside
    anchor_max_m and every other proxy reads it at least anchor_ratio times
    farther, the thing is on that proxy: after anchor_secs of that the fix
    becomes the proxy's own position. The anchor holds until that proxy's
    reading opens past anchor_release_m (or vanishes) for anchor_secs, or
    another proxy earns the anchor instead. A stationary phone on the
    counter, the keys on the hook by their proxy, the watch on the bedside
    table: exactly the things sub-zones exist for.
    """
    max_m = _tuning(layout, "anchor_max_m")
    if not max_m or max_m <= 0:
        _anchor_state.pop(entity, None)
        return None
    now = time.time() if now is None else now
    ratio = _tuning(layout, "anchor_ratio")
    dwell = _tuning(layout, "anchor_secs")
    release_m = _tuning(layout, "anchor_release_m")
    ranked = []
    by_slug = {}
    for rx in receivers:
        d = rx.get("distance")
        cords = rx.get("cords") or {}
        if not isinstance(d, (int, float)) or isinstance(d, bool) or not d > 0 or cords.get("x") is None or not _solves(rx):
            continue
        ranked.append((float(d), rx))
        by_slug[rx.get("entity_id")] = (float(d), rx)
    ranked.sort(key=lambda t: t[0])
    candidate = None
    if ranked and ranked[0][0] <= max_m and (len(ranked) < 2 or ranked[0][0] * ratio <= ranked[1][0]):
        candidate = ranked[0][1]
    st = _anchor_state.get(entity)
    if st is not None and st.get("floor") != floor_name:
        st = None
        _anchor_state.pop(entity, None)

    def _place(rx):
        return {"slug": rx.get("entity_id"), "x": float(rx["cords"]["x"]), "y": float(rx["cords"]["y"])}

    if st is None:
        # Not anchored: a candidate must persist for the dwell.
        if candidate is None:
            _anchor_state.pop(entity, None)
            return None
        slug = candidate.get("entity_id")
        pending = _anchor_state.get(entity, {}).get("pending")
        if pending is None or pending[0] != slug:
            _anchor_state[entity] = {"floor": floor_name, "slug": None, "since": None, "pending": (slug, now)}
            return None if dwell > 0 else _adopt_anchor(entity, floor_name, candidate, now)
        if now - pending[1] >= dwell:
            return _adopt_anchor(entity, floor_name, candidate, now)
        return None

    if st.get("slug") is None:
        # pending state stored under st (floor matched)
        if candidate is None:
            _anchor_state.pop(entity, None)
            return None
        slug = candidate.get("entity_id")
        pending = st.get("pending")
        if pending is None or pending[0] != slug:
            st["pending"] = (slug, now)
            return None
        if now - pending[1] >= dwell:
            return _adopt_anchor(entity, floor_name, candidate, now)
        return None

    # Anchored. Another proxy earning the anchor takes it over at once.
    if candidate is not None and candidate.get("entity_id") != st["slug"]:
        return _adopt_anchor(entity, floor_name, candidate, now)
    current = by_slug.get(st["slug"])
    if current is not None and current[0] <= release_m:
        st["away_since"] = None
        return _place(current[1])
    # Reading gone or opened past the release distance: let go after the dwell.
    st["away_since"] = st.get("away_since") or now
    if now - st["away_since"] >= dwell:
        _anchor_state.pop(entity, None)
        return None
    if current is not None:
        return _place(current[1])
    # No reading at all: hold the last known position of the anchor.
    return {"slug": st["slug"], "x": st["x"], "y": st["y"]}


def _adopt_anchor(entity, floor_name, rx, now):
    _anchor_state[entity] = {
        "floor": floor_name, "slug": rx.get("entity_id"), "since": now, "pending": None, "away_since": None,
        "x": float(rx["cords"]["x"]), "y": float(rx["cords"]["y"]),
    }
    return {"slug": rx.get("entity_id"), "x": float(rx["cords"]["x"]), "y": float(rx["cords"]["y"])}


def _subzone_probs(entity):
    """The thing's smoothed sub-zone shares for the telemetry payload, or None."""
    st = _subzone_state.get(entity)
    probs = st.get("probs") if isinstance(st, dict) else None
    if not probs:
        return None
    return {name: round(p, 3) for name, p in probs.items()}


def _elect_subzone(entity, floor_name, zone, zone_locked, point, kf_state, sub_polys, scale, layout, now=None, fp=None):
    """The (sub_zone, parent_zone) pair to publish. parent_zone is always the
    elected main zone; sub_zone is one of its sub-zones or "unknown".

    Sub-zones are a couch, a desk, a key hook: a metre or two across, which
    is the size of the positioning error itself, so a strict point-in-polygon
    test flickered. This is the zone election scaled down:

    1. Membership: the fix's 1-sigma error ellipse is sampled and the share
       inside each sub-zone smoothed over cycles; entering needs a share of
       subzone_enter_prob, and a sample in no sub-zone is evidence of none.
    2. Hysteresis on the way out: an occupied sub-zone is only left once the
       fix sits more than subzone_unlock_margin metres outside its polygon
       (or another sub-zone clearly wins).
    3. Dwell: any change must persist for subzone_switch_secs, wall clock.
    4. The zone lock carries over: a thing the zone election holds still
       (the phone on the table) keeps its sub-zone too.

    A spot restricted to certain classes (spot_classes) is not a candidate for
    a thing of any other class at all, so a cat's bed never competes for a
    phone and the phone's own spots are judged without it.
    """
    now = time.time() if now is None else now
    cls = thing_class(layout, entity)
    polys = [(sid, parent, poly) for sid, parent, poly, allowed in sub_polys
             if parent == zone and spot_accepts(allowed, cls)]
    st = _subzone_state.get(entity)
    if st is None or st.get("floor") != floor_name or st.get("zone") != zone:
        st = _subzone_state[entity] = _new_subzone_state(floor_name, zone, now)
    if not polys:
        if st["value"] != ("unknown", zone):
            st["since"] = now
        st["value"], st["pending"] = ("unknown", zone), None
        return st["value"]
    center = (point.x, point.y) if isinstance(point, Point) else (float(point[0]), float(point[1]))
    if kf_state is not None and kf_state.get("floor") == floor_name:
        samples = _covariance_samples(center, kf_state["P"][:2, :2])
    else:
        samples = [(center[0], center[1], 1.0)]
    shares = _subzone_membership(polys, samples)
    # A proxy on the spot is evidence of its own, as strong as it is: it lifts
    # the spot's share to at least that, taking the rest proportionally.
    settings = _spot_settings(layout, floor_name)
    pins = _pin_positions() if fp else None
    margin_px = _tuning(layout, "subzone_unlock_margin") * (scale if isinstance(scale, (int, float)) and scale > 0 else 0.0)
    # Pins reach as far as the lock does: a watch on a bedside table sits one
    # to two metres off its own spot, and its pins are what get it back on.
    pin_reach_px = _tuning(layout, "subzone_lock_release_m") * (scale if isinstance(scale, (int, float)) and scale > 0 else 0.0)
    for sid, _parent, _poly in polys:
        p = _spot_proxy_evidence(layout, (settings.get(sid) or {}).get("proxies"))
        # Pins on the spot are evidence of their own, for a thing that is
        # there or just beside it (see spot_pin_evidence).
        if pins:
            p = max(p, spot_pin_evidence(fp, floor_name, _poly, pins, at=center, margin_px=pin_reach_px))
        old = shares.get(sid, 0.0)
        if p > old:
            keep = (1.0 - p) / (1.0 - old) if old < 1.0 else 0.0
            shares = {s: v * keep for s, v in shares.items()}
            shares[sid] = p
    alpha = _step_weight(_tuning(layout, "zone_prob_smoothing"), st.get("probs_at") if st["probs"] else None, now)
    st["probs_at"] = now
    probs = st["probs"]
    for s in set(probs) | set(shares):
        probs[s] = alpha * probs.get(s, 0.0) + (1.0 - alpha) * shares.get(s, 0.0)
    for s in [s for s, p in probs.items() if p < 0.01]:
        del probs[s]
    default_enter = _tuning(layout, "subzone_enter_prob")

    def enter_for(sid):
        own = (settings.get(sid) or {}).get("enter_prob")
        return default_enter if own is None else own

    current = st["value"][0]
    contenders = {s: p for s, p in probs.items() if s != "unknown"}
    best = max(contenders, key=contenders.get) if contenders else None

    cur_poly = next((poly for sid, _p, poly in polys if sid == current), None) if current != "unknown" else None
    away_px = cur_poly.distance(Point(*center)) if cur_poly is not None else None
    still_near = away_px is not None and away_px <= margin_px
    release_px = _tuning(layout, "subzone_lock_release_m") * (scale if isinstance(scale, (int, float)) and scale > 0 else 0.0)
    if current != "unknown" and zone_locked and away_px is not None and away_px <= release_px:
        st["pending"] = None
        # A still thing stays on its couch / table / hook: its fix wanders
        # about while it lies there and the spot is often smaller than that
        # wander. Only a fix well away (subzone_lock_release_m) means it has
        # really left - the room lock holds the ROOM, and a cat that crossed
        # the room is not on the couch any more.
        return st["value"]

    if current != "unknown":
        if cur_poly is None:
            candidate = ("unknown", zone)  # the sub-zone was deleted or renamed
        else:
            if best is not None and best != current and contenders[best] >= enter_for(best) and contenders[best] > probs.get(current, 0.0):
                candidate = (best, zone)
            elif still_near or probs.get(current, 0.0) >= enter_for(current):
                candidate = st["value"]
            else:
                candidate = ("unknown", zone)
    else:
        # Each spot against its own threshold: the best one that clears it.
        cleared = {s: p for s, p in contenders.items() if p >= enter_for(s)}
        best = max(cleared, key=cleared.get) if cleared else None
        candidate = (best, zone) if best is not None else ("unknown", zone)

    if candidate == st["value"]:
        st["pending"] = None
        return candidate
    pending = st["pending"]
    if pending is None or pending[0] != candidate:
        st["pending"] = (candidate, now)
        return st["value"]
    if now - pending[1] >= _tuning(layout, "subzone_switch_secs"):
        st["value"], st["pending"], st["since"] = candidate, None, now
        return candidate
    return st["value"]


# Compiled zone/sub-zone polygons for a floor, keyed by (cache kind, floor_name)
# -> (layout_version, [result tuples]). A zone's geometry only changes when the
# user edits and saves the floorplan (get_layout_version bumps then), yet
# every position update for every tracked device used to rebuild every zone's
# shapely Polygon from scratch — repeatedly, since the solve step,
# find_zone_for_point, find_nearest_zone and find_sub_zone_for_point each
# called this independently in the same cycle. Caching against the layout
# version means a floor's polygons are compiled once per edit, not once per
# (device x lookup) per cycle.
_ZONE_POLY_CACHE_KEY = "sextant_zone_polygon_cache"


def _zone_poly_cache(hass) -> dict:
    return hass.data.setdefault(_ZONE_POLY_CACHE_KEY, {})


def _geometry_array(geoms):
    """A 1-D object array of shapely geometries (built element-wise: numpy
    would otherwise try to unpack the geometries as sequences)."""
    arr = np.empty(len(geoms), dtype=object)
    for i, geom in enumerate(geoms):
        arr[i] = geom
    return arr


# How far back inside the unlock margin a fix has to come before the "it has
# left the locked zone" clock is wiped. Half, so that starting and stopping the
# clock are different thresholds and a fix resting on the margin resolves one
# way or the other instead of stalling forever. See _elect_zone.
ZONE_AWAY_RESET_FRACTION = 0.5
# A locked zone holding less than this share of the smoothed membership
# evidence has been left, whatever the distance margin says (see _elect_zone).
ZONE_OUTVOTED_SHARE = 0.1


def _snap_geometry(zone_polys):
    """(valid, nogo_union) for snap_point_into_zones: the allowed-space union
    with the (grown) no-go union carved out of it, or None for either when the
    floor has none of that kind."""
    allowed = [polygon for _zone_id, polygon, _buffer_size, no_go in zone_polys if not no_go]
    nogo = [polygon for _zone_id, polygon, _buffer_size, no_go in zone_polys if no_go]
    nogo_union = unary_union(nogo) if nogo else None
    if nogo_union is not None and nogo_union.is_empty:
        nogo_union = None
    valid = None
    if allowed:
        valid = unary_union(allowed)
        if nogo_union is not None:
            # Grow-and-subtract: the snap target's boundary ends up
            # NO_GO_SNAP_MARGIN_PX outside the dead space, so the snapped point
            # is not still read as inside it by the boundary-inclusive tests.
            valid = valid.difference(nogo_union.buffer(NO_GO_SNAP_MARGIN_PX))
        if valid.is_empty:
            valid = None
    return valid, nogo_union


class _FloorZones(list):
    """A floor's (zone_id, polygon, buffer_size, no_go) tuples, plus the
    geometry the per-cycle lookups derive from them, built once per layout
    version rather than once per lookup.

    Profiling a 21-room house put the three lookups at 26 ms
    (find_zone_for_point: a buffer() ring around every zone the point was not
    in), 15 ms (snap_point_into_zones: the allowed union minus the no-go
    union) and 11 ms (find_nearest_zone: one distance call per zone) per
    call, several times per thing per cycle, all on the event loop. The
    rings, the unions and the arrays below are the same for every lookup
    until the floorplan is edited, so they live with the tuples in the
    version-keyed cache, and the predicates run as one vectorised shapely
    call over the floor instead of one Python call per zone.
    """

    __slots__ = ("allowed_ids", "allowed_geoms", "allowed_rings", "allowed_boundaries", "snap")

    def __init__(self, tuples):
        super().__init__(tuples)
        self.allowed_ids = [zone_id for zone_id, _polygon, _buffer_size, no_go in self if not no_go]
        allowed = [polygon for _zone_id, polygon, _buffer_size, no_go in self if not no_go]
        self.allowed_geoms = _geometry_array(allowed)
        self.allowed_rings = _geometry_array([
            polygon.buffer(buffer_size)
            for _zone_id, polygon, buffer_size, no_go in self if not no_go
        ])
        # boundary works for Polygon and MultiPolygon alike.
        self.allowed_boundaries = _geometry_array([polygon.boundary for polygon in allowed])
        self.snap = _snap_geometry(self)


def _floor_zone_polygons(hass, data, entity, floor_name):
    """(zone entity_id, polygon, buffer_size, no_go) tuples for the floor.

    no_go marks a zone a thing can't be in (issue #60). Callers keep no-go
    zones for the solver bounds (they still bound the floor) but exclude them
    from zone assignment and snapping — a fix must never be reported as, or
    snapped into, dead space.
    """
    cache = _zone_poly_cache(hass)
    cache_key = ("zones", floor_name)
    version = get_layout_version(hass)
    cached = cache.get(cache_key)
    if cached is not None and cached[0] == version:
        return cached[1]

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

    results = []
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
                        results.append((zone["entity_id"], polygon, buffer_size, bool(zone.get("no_go"))))
            break

    results = _FloorZones(results)
    cache[cache_key] = (version, results)
    return results


def _point_in_no_go(point, zone_polys):
    """True if the point falls strictly inside any no-go zone in zone_polys.

    zone_polys is the (zone_id, polygon, buffer_size, no_go) list from
    _floor_zone_polygons. Accepts a shapely Point or an (x, y) pair. Uses
    strict containment (not boundary-inclusive covers): a no-go zone's edge is
    typically a physical railing, and a thing genuinely ON the walkway there
    sits on that boundary — it must not be penalised as if over the void.
    """
    if not isinstance(point, Point):
        point = Point(float(point[0]), float(point[1]))
    return any(no_go and polygon.contains(point)
               for _zone_id, polygon, _buffer_size, no_go in zone_polys)


def find_zone_for_point(hass, data, entity, floor_name, point):
    """Find zone for point, prioritize correct polygon, select nearest buffer if no correct zone matches.

    No-go zones (issue #60) are skipped: a thing can't be in one, so a fix
    there is reported as belonging to the nearest real zone (or "unknown").
    """
    zones = _floor_zone_polygons(hass, data, entity, floor_name)
    if not zones.allowed_ids:
        return "unknown"
    if not isinstance(point, Point):
        point = Point(float(point[0]), float(point[1]))
    # covers() also matches points on the polygon boundary. The first zone
    # in layout order that covers the point wins, as before.
    covered = np.flatnonzero(shapely.covers(zones.allowed_geoms, point))
    if covered.size:
        return zones.allowed_ids[int(covered[0])]  # Prioritize correct polygon
    # Otherwise the zone whose soft buffer holds the point and whose edge is
    # closest to it (ties broken by zone id, like the old sorted candidates).
    in_ring = np.flatnonzero(shapely.contains(zones.allowed_rings, point))
    if in_ring.size:
        distances = shapely.distance(zones.allowed_boundaries[in_ring], point)
        candidates = sorted(
            (float(distance), zones.allowed_ids[int(i)]) for i, distance in zip(in_ring, distances)
        )
        return candidates[0][1]
    return "unknown"


def snap_point_into_zones(zone_polys, point, prefer=None, prefer_margin_px=0.0):
    """Project a point onto valid (allowed, non-no-go) space.

    Returns the snapped Point, or None when the point is already in valid
    space (or there is nowhere valid to put it). ``prefer`` names a zone (the
    thing's current room): when the point has to be snapped and that zone's
    valid part is no more than ``prefer_margin_px`` farther than the nearest
    valid space, the point goes to that zone instead (NO_GO_SNAP_STICK_M). No-go zones (issue #60) are
    subtracted from the allowed region — grown by NO_GO_SNAP_MARGIN_PX first —
    so a fix in dead space is pushed to the nearest genuinely-allowed point,
    clear of the boundary-inclusive no-go edge. This holds even when a no-go
    zone is drawn overlapping or nested inside an allowed room (subtraction
    carves the void out of the room), and when the floor's only zones are
    no-go (the point is at least pushed off the dead-space footprint).
    """
    # The unions come with the floor's cached zone list; a plain list (tests,
    # ad-hoc callers) gets them computed here.
    valid, nogo_union = (
        zone_polys.snap if isinstance(zone_polys, _FloorZones) else _snap_geometry(zone_polys)
    )

    if valid is not None:
        if valid.covers(point):
            return None  # already in valid space
        snapped, _ = nearest_points(valid, point)
        if prefer is not None and prefer_margin_px > 0:
            own = _preferred_snap_target(zone_polys, prefer, nogo_union)
            if own is not None and own.distance(point) <= valid.distance(point) + prefer_margin_px:
                snapped, _ = nearest_points(own, point)
        return snapped

    # No allowed space to land in. If the point sits in declared dead space,
    # at least push it just off the no-go footprint; otherwise leave it as-is.
    if nogo_union is not None and nogo_union.covers(point):
        snapped, _ = nearest_points(nogo_union.buffer(NO_GO_SNAP_MARGIN_PX).boundary, point)
        return snapped
    return None


def _preferred_snap_target(zone_polys, zone_id, nogo_union):
    """The valid part of one allowed zone (its polygon less the grown no-go
    union), or None when the floor has no such allowed zone."""
    polys = [polygon for zid, polygon, _buffer_size, no_go in zone_polys if zid == zone_id and not no_go]
    if not polys:
        return None
    own = unary_union(polys)
    if nogo_union is not None:
        own = own.difference(nogo_union.buffer(NO_GO_SNAP_MARGIN_PX))
    return None if own.is_empty else own


def find_nearest_zone(hass, data, entity, floor_name, point):
    """The zone closest to the point, no matter how far away.

    Trilateration jitter can land a fix between two zones or outside the map
    entirely; this always names the closest zone on the elected floor (a point
    inside a zone has distance 0, so it matches find_zone_for_point there).
    Returns "unknown" only when the floor has no usable zones.
    """
    zones = _floor_zone_polygons(hass, data, entity, floor_name)
    if not zones.allowed_ids:  # dead space is never "the nearest zone" (issue #60)
        return "unknown"
    if not isinstance(point, Point):
        point = Point(float(point[0]), float(point[1]))
    # One vectorised call over the floor; argmin keeps the first of equals,
    # as the old strict-less-than scan did.
    distances = shapely.distance(zones.allowed_geoms, point)
    return zones.allowed_ids[int(np.argmin(distances))]


def spot_classes(sub) -> frozenset:
    """The thing classes a spot accepts, or an empty set for any of them.

    A bedside table is for a phone, a watch, keys; the cat bed on the landing
    is for the cat. Without this every spot competes for every thing, and the
    one that happens to be nearest wins - which is how a phone ends up "on"
    the cat bed. Stored on the sub-zone as ``classes``; absent or empty keeps
    the original behaviour, so nothing drawn before this changes.
    """
    raw = sub.get("classes") if isinstance(sub, dict) else None
    return frozenset(c for c in raw if isinstance(c, str) and c) if isinstance(raw, list) else frozenset()


def thing_class(layout, entity) -> str:
    """The class given to this thing on the Things page ("" when it has none)."""
    classes = layout.get("thing_classes") if isinstance(layout, dict) else None
    value = classes.get(entity) if isinstance(classes, dict) else None
    return value if isinstance(value, str) else ""


# Two classes are families rather than one specific kind of thing: ticking
# Person on a spot means a person, however they are classed, and Pet means
# the dog or the cat. Only the family admits its members - a spot asking for
# Man is not satisfied by something classed merely Person.
CLASS_FAMILIES = {
    "person": frozenset({"person", "man", "woman", "child"}),
    "paw": frozenset({"paw", "dog", "cat"}),
    # A backpack, a purse and a suitcase are all bags, and a spot meant for
    # one - the hook by the door, the shelf in the closet - takes any of them.
    # They are separate classes because they are not interchangeable to a
    # person reading the Live page: luggage sitting in the hall means
    # something a purse on the counter does not.
    "bag": frozenset({"bag", "backpack", "purse", "luggage"}),
}


def spot_accepts(allowed, cls) -> bool:
    """Whether a spot restricted to `allowed` takes a thing of class `cls`."""
    if not allowed:
        return True
    return cls in allowed or any(cls in CLASS_FAMILIES.get(a, ()) for a in allowed)


def _floor_sub_zone_polygons(hass, data, entity, floor_name):
    """(sub-zone name, parent zone name, polygon) tuples for the entity's floor.

    Sub-zones are small precise areas drawn inside a zone (a couch, a desk), so
    they are matched strictly (no soft buffer). They live in a separate
    "subzones" list, so the main-zone election/snap/nearest logic is untouched.
    The fourth item is the set of thing classes the spot accepts (empty: any) -
    see spot_classes.

    Cached the same way and for the same reason as _floor_zone_polygons.
    """
    cache = _zone_poly_cache(hass)
    cache_key = ("subzones", floor_name)
    version = get_layout_version(hass)
    cached = cache.get(cache_key)
    if cached is not None and cached[0] == version:
        return cached[1]

    results = []
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
                results.append((sub.get("entity_id"), zone_name_by_id.get(parent_ref, parent_ref),
                                polygon, spot_classes(sub)))
        break

    cache[cache_key] = (version, results)
    return results


def find_sub_zone_for_point(hass, data, entity, floor_name, point):
    """The sub-zone containing the point and its parent zone name.

    Returns (sub_zone_name, parent_zone_name); ("unknown", None) when the point
    is in no sub-zone.
    """
    cls = thing_class((data[0] or {}).get("data") if data else None, entity)
    for sub_id, parent_id, polygon, allowed in _floor_sub_zone_polygons(hass, data, entity, floor_name):
        if not spot_accepts(allowed, cls):
            continue
        if polygon.covers(point):
            return sub_id, parent_id
    return "unknown", None


# --- Live push (websocket) ------------------------------------------------------
# Fired on the event loop at the end of every positioning cycle with the same
# payload the /api/sextant/cords poll would return, plus receiver health. The
# panel and the Lovelace card can subscribe once instead of polling, and a
# subscriber sees every cycle rather than whichever ones its timer lands on.
SIGNAL_BPS_UPDATE = "sextant_positions_updated"


def _push_payload(hass):
    """What a websocket subscriber gets on subscribe and after each cycle."""
    dom = hass.data.get(DOMAIN, {}) if isinstance(getattr(hass, "data", None), dict) else {}
    return {
        "stamp": time.time(),
        "positions": list(dom.get("apitricords") or []),
        "offline_receivers": list(dom.get("rl_offline") or []),
        # mmWave radar targets on the plan: {floor, cords, radar_name, thing,
        # room}; a target with no thing is someone Sextant has no device for.
        "radar_targets": [t for t in (_radar_frame.get("targets") or [])] if time.time() - _radar_frame.get("t", 0) <= RADAR_FRESH_S else [],
        # The refresh interval and when the last cycle ran, for the countdown
        # (and its menu, which sets a temporary interval).
        "interval": interval_info(),
    }


@websocket_api.websocket_command({vol.Required("type"): "sextant/subscribe"})
@websocket_api.async_response
async def _ws_subscribe(hass, connection, msg):
    """``sextant/subscribe``: stream positions and receiver health, one event per cycle.

    The first event is sent immediately with the current state, so a client
    has something to draw before the next cycle. Unsubscribing is the
    standard ``unsubscribe_events`` on the subscription id.
    """

    @callback
    def _forward(payload):
        connection.send_message(websocket_api.event_message(msg["id"], payload))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_BPS_UPDATE, _forward)
    connection.send_result(msg["id"])
    _forward(_push_payload(hass))


def _register_websocket(hass) -> None:
    if hass.data.get("sextant_ws_registered"):
        return
    websocket_api.async_register_command(hass, _ws_subscribe)
    from . import ws as ws_module  # noqa: PLC0415 - ws imports this package lazily

    ws_module.async_register(hass)
    hass.data["sextant_ws_registered"] = True


async def async_apply_tuning(hass, settings, reset=False):
    """Validate and store tuning overrides (TUNING_SPEC); returns the stored map.

    Validation is strict here where a human is typing, and lenient in
    _tuning where the store is read: an unknown key or an out-of-range
    value is refused with the allowed range, rather than silently ignored
    later. ``reset`` drops every override first. Shared by the
    sextant.set_tuning service and the panel's websocket command.
    """
    updates = {}
    for key, value in (settings or {}).items():
        if key not in TUNING_SPEC:
            raise HomeAssistantError(f"unknown tuning key {key!r}; known: {', '.join(sorted(TUNING_SPEC))}")
        coerced = _coerce_tuning(key, value, None)
        if coerced is None:
            spec = TUNING_SPEC[key]
            allowed = (
                f"one of {', '.join(spec[2])}" if spec[1] is str
                else "true or false" if spec[1] is bool
                else f"a number between {spec[2]} and {spec[3]}"
            )
            raise HomeAssistantError(f"{key} must be {allowed}, got {value!r}")
        updates[key] = coerced
    async with LAYOUT_LOCK:
        data = get_layout_for_edit(hass)
        if not isinstance(data, dict):
            raise HomeAssistantError("No Sextant layout saved yet; place receivers first.")
        tuning = {} if reset else dict(data.get("tuning") or {})
        tuning.update(updates)
        if tuning:
            data["tuning"] = tuning
        else:
            data.pop("tuning", None)
        await save_layout(hass, data)
    _LOGGER.info("sextant.set_tuning: %s", tuning or "defaults restored")
    return tuning


def _register_calibration_services(hass) -> None:
    """Expose the calibration actions as HA services.

    These mirror async_calibration_action exactly and call the same functions, so the
    panel and an automation cannot diverge. The point of having both is that
    the REST view needs a bearer token and a JSON body, which makes it awkward
    from an automation or a script - and unusable from the Developer Tools
    action UI, which is where anyone would look first.

    ValueError is what the calibration layer raises for the expected refusals
    (unknown floor, a run already going, a floor with no scale). Translating it
    to HomeAssistantError surfaces the message in the UI as a normal action
    failure instead of a traceback in the log.
    """

    async def _start(call: ServiceCall) -> None:
        try:
            await start_calibration(
                hass, call.data["floor"], int(call.data.get("duration", 600))
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def _cancel(call: ServiceCall) -> None:
        await async_cancel_calibration(hass)

    async def _apply(call: ServiceCall) -> None:
        cal = get_calibration_state(hass)
        floor = call.data.get("floor") or cal.get("floor")
        try:
            await apply_corrections(hass, cal, floor)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await save_calibration_state(hass)

    async def _reset(call: ServiceCall) -> None:
        cal = get_calibration_state(hass)
        floor = call.data.get("floor") or cal.get("floor")
        try:
            await reset_corrections(hass, cal, floor)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await save_calibration_state(hass)

    async def _auto(call: ServiceCall) -> None:
        await set_auto_calibration(hass, bool(call.data["enabled"]))

    async def _heights(call: ServiceCall) -> None:
        """Write receiver mount heights through the layout store.

        Deliberately a service rather than a file edit: the layout lives in
        HA's Store, which keeps an in-memory cache and writes it back. Editing
        config/.storage/sextant underneath a running HA is silently lost the next
        time anything saves. Going through save_layout persists atomically
        and refreshes the cache, so calibration sees the change immediately -
        _read_coords rebuilds the receiver map from it on every run.
        """
        heights = {str(k): float(v) for k, v in (call.data.get("heights") or {}).items()}
        default = call.data.get("default")
        for name, value in heights.items():
            if not 0 <= value <= 10:
                raise HomeAssistantError(f"height for {name!r} must be between 0 and 10 m, got {value}")

        async with LAYOUT_LOCK:
            data = get_layout_for_edit(hass)
            if not isinstance(data, dict) or not data.get("floor"):
                raise HomeAssistantError("No Sextant layout saved yet; place receivers first.")

            seen: set[str] = set()
            changed = 0
            for floor in data["floor"]:
                for receiver in floor.get("receivers", []):
                    slug = str(receiver.get("entity_id") or "")
                    if not slug:
                        continue
                    seen.add(slug)
                    if slug in heights:
                        receiver["height"] = heights[slug]
                        changed += 1
                    elif default is not None:
                        receiver["height"] = float(default)
                        changed += 1
            await save_layout(hass, data)

        unmatched = sorted(set(heights) - seen)
        if unmatched:
            # Not fatal: a typo should not silently do nothing, but it should
            # also not discard the heights that did match.
            _LOGGER.warning(
                "sextant.set_receiver_heights: %d receiver(s) updated; no receiver matched %s",
                changed, ", ".join(unmatched),
            )
        else:
            _LOGGER.info("sextant.set_receiver_heights: %d receiver(s) updated", changed)

    # Admin services: Home Assistant lets any signed-in user call an ordinary
    # service. A call made by a person must come from an administrator;
    # automations and scripts carry no user and keep working.
    async_register_admin_service(hass, 
        DOMAIN, "start_calibration", _start,
        schema=vol.Schema({
            vol.Required("floor"): cv.string,
            vol.Optional("duration", default=600): vol.All(
                vol.Coerce(int), vol.Range(min=60, max=3600)
            ),
        }),
    )
    async_register_admin_service(hass, DOMAIN, "cancel_calibration", _cancel, schema=vol.Schema({}))
    async_register_admin_service(hass, 
        DOMAIN, "apply_corrections", _apply,
        schema=vol.Schema({vol.Optional("floor"): cv.string}),
    )
    async_register_admin_service(hass, 
        DOMAIN, "reset_corrections", _reset,
        schema=vol.Schema({vol.Optional("floor"): cv.string}),
    )
    async def _thing_heights(call: ServiceCall) -> None:
        """Write per-thing carry heights into the layout store.

        Same reasoning as _heights: the layout lives in HA's Store, so a direct
        file edit is lost the next time anything saves. Keys are deliberately
        NOT validated against seen devices - a thing that has not been heard
        yet should be configurable ahead of time, and the read path already
        ignores anything out of range.
        """
        heights = {str(k): float(v) for k, v in call.data["heights"].items()}

        async with LAYOUT_LOCK:
            data = get_layout_for_edit(hass)
            if not isinstance(data, dict):
                raise HomeAssistantError("No Sextant layout saved yet.")
            current = data.get("thing_heights")
            if not isinstance(current, dict):
                current = {}
            current.update(heights)
            data["thing_heights"] = current
            await save_layout(hass, data)
        _LOGGER.info("sextant.set_thing_heights: %d thing(s) set", len(heights))

    async def _bias_field(call: ServiceCall) -> ServiceResponse:
        """Lay, shape or remove a floor's bias field (floor_field.py).

        A service for the same reason the heights are: the layout lives in
        HA's Store and a file edit under a running HA is lost on the next
        save. ``flat`` lays the no-op field, ``paint`` writes a value under a
        room, a spot or a drawn polygon, ``clear`` removes the field again.
        """
        floor_name = call.data["floor"]
        action = call.data["action"]
        async with LAYOUT_LOCK:
            data = get_layout_for_edit(hass)
            floors = data.get("floor") if isinstance(data, dict) else None
            floor = next((f for f in floors or [] if isinstance(f, dict) and f.get("name") == floor_name), None)
            if floor is None:
                known = ", ".join(str(f.get("name")) for f in floors or [] if isinstance(f, dict))
                raise HomeAssistantError(f"No floor named {floor_name!r}. Floors: {known or 'none'}")
            touched = None
            try:
                if action == "clear":
                    floor.pop(floor_field.FIELD_KEY, None)
                else:
                    if action == "flat" or floor_field.parse(floor) is None:
                        # Painting an unfielded floor lays the flat field
                        # first, so "paint the catwalk" is one call, not two.
                        xs, ys = [], []
                        for receiver in floor.get("receivers") or []:
                            cords = receiver.get("cords") or {}
                            if "x" in cords and "y" in cords:
                                xs.append(float(cords["x"]))
                                ys.append(float(cords["y"]))
                        for shape in (floor.get("zones") or []) + (floor.get("subzones") or []):
                            for pt in shape.get("cords") or []:
                                xs.append(float(pt["x"]))
                                ys.append(float(pt["y"]))
                        if not xs:
                            raise ValueError("the floor has no extent; place proxies or draw rooms first")
                        floor[floor_field.FIELD_KEY] = floor_field.flat(
                            (min(xs), min(ys), max(xs), max(ys)), floor.get("scale"),
                            call.data.get("cell_m", 1.0), call.data.get("value", 1.0) if action == "flat" else 1.0,
                        )
                    if action == "paint":
                        points = call.data.get("points")
                        area = call.data.get("area")
                        if (points is None) == (area is None):
                            raise ValueError('paint needs exactly one of "area" (a room or spot name) or "points"')
                        if area is not None:
                            shapes = (floor.get("zones") or []) + (floor.get("subzones") or [])
                            shape = next((s for s in shapes if s.get("entity_id") == area), None)
                            if shape is None:
                                names = ", ".join(sorted(str(s.get("entity_id")) for s in shapes))
                                raise ValueError(f"no room or spot named {area!r} on {floor_name}. Known: {names}")
                            points = [(pt["x"], pt["y"]) for pt in shape.get("cords") or []]
                        touched = floor_field.paint(floor, points, call.data.get("value", 1.0), call.data.get("mode", "set"))
            except (KeyError, TypeError) as err:
                raise HomeAssistantError(
                    f"sextant.set_floor_bias_field: {floor_name} has a room, spot or proxy without usable coordinates ({err!r})"
                ) from err
            except ValueError as err:
                raise HomeAssistantError(f"sextant.set_floor_bias_field: {err}") from err
            await save_layout(hass, data)
        summary = floor_field.describe(floor)
        _LOGGER.info("sextant.set_floor_bias_field: %s on %s -> %s (cells painted: %s)",
                     action, floor_name, summary, touched)
        return {"floor": floor_name, "action": action, "cells_painted": touched, "field": summary}

    async_register_admin_service(hass, 
        DOMAIN, "set_floor_bias_field", _bias_field,
        schema=vol.Schema({
            vol.Required("floor"): cv.string,
            vol.Required("action"): vol.In(["flat", "paint", "clear"]),
            vol.Optional("cell_m", default=1.0): vol.All(
                vol.Coerce(float), vol.Range(min=floor_field.CELL_MIN_M, max=floor_field.CELL_MAX_M)),
            vol.Optional("value", default=1.0): vol.All(
                vol.Coerce(float), vol.Range(min=floor_field.FIELD_MIN, max=floor_field.FIELD_MAX)),
            vol.Optional("mode", default="set"): vol.In(["set", "multiply"]),
            vol.Optional("area"): cv.string,
            vol.Optional("points"): [vol.All([vol.Coerce(float)], vol.Length(min=2, max=2))],
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(hass, 
        DOMAIN, "set_auto_calibration", _auto,
        schema=vol.Schema({vol.Required("enabled"): cv.boolean}),
    )
    heights_schema = vol.Schema({
        vol.Required("heights"): vol.Schema({cv.string: vol.All(vol.Coerce(float), vol.Range(min=0, max=5))}),
    })
    async_register_admin_service(hass, DOMAIN, "set_thing_heights", _thing_heights, schema=heights_schema)
    # What a thing is called was "tracker" until 3.12.0. The old service name
    # stays registered so an automation written against it keeps working.
    async_register_admin_service(hass, DOMAIN, "set_tracker_heights", _thing_heights, schema=heights_schema)
    async_register_admin_service(hass, 
        DOMAIN, "set_receiver_heights", _heights,
        schema=vol.Schema({
            vol.Optional("heights", default=dict): vol.Schema({cv.string: vol.Coerce(float)}),
            vol.Optional("default"): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
        }),
    )

    async def _set_tuning(call: ServiceCall) -> None:
        """Change positioning tuning live (TUNING_SPEC), through the store."""
        await async_apply_tuning(hass, call.data.get("settings") or {}, bool(call.data.get("reset")))

    async_register_admin_service(hass, 
        DOMAIN, "set_tuning", _set_tuning,
        schema=vol.Schema({
            vol.Optional("settings", default=dict): dict,
            vol.Optional("reset", default=False): cv.boolean,
        }),
    )


async def async_setup(hass, config):
    """Set up the Sextant integration."""
    _LOGGER.info("Sextant integration initierad.")

    if hass.data.get("sextant_initialized", False):
        _LOGGER.debug("Sextant already initialized in current runtime; skipping duplicate init.")
        return True  # Abort if already running

    hass.data["sextant_initialized"] = True  # Set flag

    # A clean stop writes the state one last time, so a restart resumes from
    # the moment it went down rather than from the last periodic save. STOP
    # comes first and FINAL_WRITE last, which is where Home Assistant expects
    # its stores to be written; taking both costs one extra write.
    async def _on_stop(_event):
        await _save_runtime(hass)

    # Once per Home Assistant run: sextant_initialized is cleared on unload,
    # so every reload (each options change) used to add another set, and a
    # stop then ran every save and shutdown once per reload, concurrently.
    first_setup = not hass.data.get("sextant_stop_listeners")
    hass.data["sextant_stop_listeners"] = True
    if first_setup:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_FINAL_WRITE, _on_stop)

    async def initialize_sextant():
        """Initialize the Sextant component"""
        _LOGGER.info("Initializing Sextant...")

        if "sextant_views_registered" not in hass.data:
            hass.http.register_view(SextantFrontendView())
            hass.http.register_view(SextantMapImageView())
            hass.http.register_view(SextantSaveAPIText())
            hass.http.register_view(SextantUploadThingIconAPI())
            hass.http.register_view(SextantCordsAPI(hass))
            hass.http.register_view(SextantSelfTestAPI(hass))
            hass.data["sextant_views_registered"] = True

        if "sextant_services_registered" not in hass.data:
            _register_calibration_services(hass)
            hass.data["sextant_services_registered"] = True
        _register_websocket(hass)

        config_path = hass.config.path()
        target_dir = maps_dir(hass)
        thing_icons_dir = os.path.join(config_path, "www", "sextant_icons")

        try:
            await aiofiles.os.makedirs(target_dir, exist_ok=True)
            _LOGGER.info(f"Folder {target_dir} has been created or already existed")
        except Exception as e:
            _LOGGER.error(f"Could not create the folder {target_dir}: {e}")
            return
        # Up to 3.11.10 the images sat under www/ and were public at /local/.
        await migrate_maps_out_of_www(hass)

        try:
            await aiofiles.os.makedirs(thing_icons_dir, exist_ok=True)
            _LOGGER.info(f"Folder {thing_icons_dir} has been created or already existed")
        except Exception as e:
            _LOGGER.error(f"Could not create the folder {thing_icons_dir}: {e}")
            return

        show_sidebar_panel = True
        update_interval = DEFAULT_UPDATE_INTERVAL
        if hasattr(config, "options"):
            show_sidebar_panel = config.options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
            update_interval = config.options.get(OPTION_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        else:
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                show_sidebar_panel = entries[0].options.get(OPTION_SHOW_SIDEBAR_PANEL, True)
                update_interval = entries[0].options.get(OPTION_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        global secToUpdate
        secToUpdate = update_interval
        panels = hass.data.get("frontend_panels", {})
        if "sextant" in panels:
            async_remove_panel(hass, "sextant")

        if show_sidebar_panel:
            try:
                _LOGGER.debug("Registering the custom panel for Sextant...")
                # A native custom panel: the element receives `hass` and talks
                # to the backend over the websocket. The module URL carries the
                # installed version, so a page loaded before an update sees a
                # different version in layout/get and offers a reload; no
                # constant in the frontend has to be bumped per release. The
                # version is a path segment rather than a query string so the
                # panel's relative imports (./sextant-map.js ...) resolve to new
                # URLs too: a still-open tab that re-imports the new panel after
                # a restart must not mix it with the old modules already in its
                # module map (that raised "does not provide an export named ...").
                from homeassistant.loader import async_get_integration  # noqa: PLC0415  (the test stubs have no loader)

                integration = await async_get_integration(hass, DOMAIN)
                from . import ws as ws_mod  # noqa: PLC0415

                ws_mod.RUNNING_VERSION = str(integration.version) if integration.version else None
                ws_mod.RUNNING_CODE = await hass.async_add_executor_job(ws_mod.code_signature)
                await panel_custom.async_register_panel(
                    hass,
                    frontend_url_path="sextant",
                    webcomponent_name="sextant-panel",
                    module_url=f"/sextant/v/{integration.version}/sextant-panel.js",
                    sidebar_title="Sextant",
                    sidebar_icon="mdi:compass-rose",
                    require_admin=False,
                    embed_iframe=False,
                )
                _LOGGER.info("Panel registered successfully.")
            except Exception as e:
                _LOGGER.error(f"Failed to register panel: {e}")
        else:
            _LOGGER.info("Sextant sidebar panel is disabled by integration options.")

        # Move any legacy www/sextant_maps flat files into the Store (once), then
        # load the layout into the in-memory cache. Store writes are atomic, so
        # a crash can no longer leave a 0-byte layout (issue #104).
        await migrate_from_bps(hass)
        await migrate_legacy(hass)
        await load_layout(hass)
        # One-time (and thereafter incremental) migration: identify placed
        # receivers by scanner address rather than by slug alone.
        try:
            await async_resolve_receiver_addresses(hass)
        except Exception as e:
            _LOGGER.debug("Receiver identity migration deferred: %s", e)
        # Layout is loaded, so the history settings are readable: bring the
        # retained window back before the tracking loop starts appending.
        await restore_position_history(hass)

        old_task = hass.data.get("sextant_update_task")
        if old_task:
            old_task.cancel()
        hass.data["sextant_update_task"] = hass.async_create_task(update_tracked_entities(hass))

        async def handle_homeassistant_stop(event):
            """Stop background work promptly so shutdown cannot drag or leave
            the unload half-done (which strands stale registry entries)."""
            update_task = hass.data.pop("sextant_update_task", None)
            if update_task:
                update_task.cancel()
            try:
                await flush_position_history(hass)
            except Exception as e:
                _LOGGER.debug("Sextant position history final flush failed: %s", e)
            await async_shutdown_calibration(hass)

        if first_setup:
            hass.bus.async_listen_once("homeassistant_stop", handle_homeassistant_stop)

        await async_restore_calibration_state(hass)
        await async_start_auto_if_enabled(hass)

        _LOGGER.info("The Sextant integration is fully initialized")

    async def handle_homeassistant_started(event):
        """Handles the 'homeassistant_started' event"""
        await initialize_sextant()

    if hass.is_running:
        await initialize_sextant()
    else:
        hass.bus.async_listen_once("homeassistant_started", handle_homeassistant_started)

    return True

async def async_unload_entry(hass: HomeAssistant, entry):
    """Remove a configuration entry"""
    _LOGGER.info("Attempting to offload platforms for entry: %s", entry.entry_id)

    state_listener_unsub = hass.data.pop("sextant_state_listener_unsub", None)
    if state_listener_unsub:
        state_listener_unsub()

    # Stop the loop before anything below can fail and return early (which
    # left it running against half-torn-down state), and write the runtime
    # out: setup restores it, and a reload would otherwise rewind every
    # election to the last periodic save, up to a minute back.
    update_task = hass.data.pop("sextant_update_task", None)
    if update_task:
        update_task.cancel()
    await _save_runtime(hass)

    # Get whatever the tracking loop buffered since the last 60 s flush onto
    # disk before the task goes away. The in-memory ring survives an unload
    # (hass.data[DOMAIN] is not cleared), so this is belt-and-braces, but an
    # unload followed by a hard stop would otherwise lose that minute.
    try:
        await flush_position_history(hass)
    except Exception as e:
        _LOGGER.debug("Sextant position history flush on unload failed: %s", e)

    cleanup_legacy_sextant_registry_and_states(hass)

    # The registry entries stay. Unloading the platform below takes the
    # entities out of the state machine, and Home Assistant itself clears a
    # config entry's registry entries when the entry is REMOVED. Deleting them
    # here ran on every reload and options change: it threw away per-entity
    # settings (area, name, disabled) and rewrote the whole registry twice.

    try: # Attempt to unload platforms
        unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "device_tracker"])
    except Exception as e:
        _LOGGER.error(f"Error during offloading of platforms for entry {entry.entry_id}: {e}")
        return False

    if not unload_ok:
        _LOGGER.error("Failed to offload platforms for entry: %s", entry.entry_id)
        return False

    try: #Remove the frontend panel
        async_remove_panel(hass, frontend_url_path="sextant")
        _LOGGER.info("Frontend-panel removed for entry: %s", entry.entry_id)
    except Exception as e:
        _LOGGER.error(f"Error when removing frontend-panel for entry {entry.entry_id}: {e}")
        return False

    await async_shutdown_calibration(hass)

    # Remove Sextant states from the state machine when integration is unloaded.
    sextant_state_ids = [
        state.entity_id
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.")
        and state.entity_id.endswith(
            ("_sextant_room", "_sextant_floor", "_sextant_nearest_room", "_sextant_spot", "_sextant_location")
        )
    ]
    for entity_id in sextant_state_ids:
        hass.states.async_remove(entity_id)

    # Allow clean setup after integration reload/removal.
    hass.data.pop("sextant_initialized", None)
    hass.data.pop("sextant_sensors", None)
    hass.data.pop("sextant_add_entities", None)

    return True


async def async_setup_entry(hass, entry):
    """Set the integration from a configuration entry"""
    _LOGGER.info("async_setup_entry called")
    # Truth marks double as fingerprint references; load them before the first cycle.
    try:
        store = await load_truth(hass)
        _set_truth_marks(store.get("marks", []))
        _restore_fp_gains(await load_fp_gains(hass))
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Truth marks or learned gains not loaded: %s", e)
    await _restore_runtime(hass)
    cleanup_legacy_sextant_registry_and_states(hass)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "device_tracker"])
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    """Set up Sextant from a config entry."""
    return await async_setup(hass, entry)


async def async_update_options(hass, entry):
    """Reload integration when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)

class SextantFrontendView(HomeAssistantView):
    """Serve the frontend files."""

    url = "/sextant/{file_name}"
    # The panel is registered under a per-release path (see the panel
    # registration) so that every module URL changes on update; the version
    # segment is ignored here. The unversioned path stays for the dashboard
    # card resource users register by hand (/sextant/sextant-map-card.js).
    extra_urls = ["/sextant/v/{version}/{file_name}"]
    name = "sextant:frontend"
    requires_auth = False

    async def get(self, request, file_name, version=None):
        """Serve static files from the frontend folder."""
        frontend_path = FRONTEND_PATH / file_name

        _LOGGER.debug("Serving file: %s", frontend_path)

        # This view needs no login, so it must never leave its folder. Home
        # Assistant's request filter already rejects an encoded "../", but
        # that is its defence, not ours: only a plain file name, directly
        # inside the frontend folder, is served.
        if Path(file_name).name != file_name or frontend_path.parent != FRONTEND_PATH:
            return web.Response(status=404, text="File not found")

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
        # sextant-panel.js is loaded as an ES module (panel_custom), and browsers
        # refuse a module served with a non-JS MIME type. Force it rather than
        # trust the host's mimetypes registry (a Windows HA host can map .js to
        # text/plain, which would silently blank the panel).
        if file_name.endswith(".js"):
            response.headers["Content-Type"] = "text/javascript"
        return response

class SextantMapImageView(HomeAssistantView):
    """Serve a floor-plan image to a signed-in user.

    The images used to live under www/ and were fetchable by anyone who
    could reach the port. They now live in config/sextant_maps and come
    through here: a login is required, the name is pinned inside the folder
    and only image extensions are served.
    """

    url = "/api/sextant/map/{file_name}"
    name = "api:sextant:map"
    requires_auth = True

    async def get(self, request, file_name):
        hass = request.app["hass"]
        target = _safe_maps_child(maps_dir(hass), file_name, _ALLOWED_MAP_EXTS)
        if target is None or not target.is_file():
            return web.Response(status=404, text="No such map")
        response = web.FileResponse(path=str(target))
        response.headers["Cache-Control"] = "private, no-cache"
        # An .svg can carry script, and this is Home Assistant's own origin:
        # sandboxed, it renders as an image and runs nothing even when opened
        # in a tab of its own.
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


class SextantSaveAPIText(HomeAssistantView):
    """Handle saving of Sextant coordinates to a text file."""

    url = "/api/sextant/save_text"
    name = "api:sextant:save_text"
    requires_auth = True

    async def post(self, request):
        """Handle saving coordinates to a text file."""
        if (denied := _admin_only(request)) is not None:
            return denied
        hass = request.app["hass"]
        data = await request.post()

        coordinates = data.get("coordinates")

        if not coordinates:
            return web.Response(status=400, text="Missing coordinates")
        # Reject a non-JSON layout blob before it can reach the store.
        try:
            coords_obj = json.loads(coordinates)
        except (ValueError, TypeError):
            return web.Response(status=400, text="Coordinates must be valid JSON")

        maps_path = maps_dir(hass)

        try: # Persist the layout to the store + handle map upload/removal
            # Serialized with the calibration writers so read-modify-write
            # cycles on the layout cannot interleave.
            async with LAYOUT_LOCK:
                error = await self._write_save(hass, maps_path, data, coords_obj)
            if error is not None:
                return error
            # A calibration window may be sampling right now; hand it the
            # edited placements so a re-linked/added receiver starts matching
            # on the next dump instead of after the next window/solve cycle.
            refresh_receivers_from_coords(hass, coordinates)
            floors = coords_obj.get("floor", []) if isinstance(coords_obj, dict) else []
            _LOGGER.info(
                "Saved layout: %d floor(s), %d proxies, %d rooms",
                len(floors),
                sum(len(f.get("receivers") or []) for f in floors if isinstance(f, dict)),
                sum(len(f.get("zones") or []) for f in floors if isinstance(f, dict)),
            )
            return web.Response(status=200, text="Coordinates saved successfully")

        except Exception as e:
            _LOGGER.error(f"Failed to save coordinates: {e}")
            return web.Response(status=500, text="Failed to save coordinates")

    async def _write_save(self, hass, maps_path, data, coords_obj):
        """Validate all inputs, THEN persist; returns an error Response or None.

        Ordering matters: the new-floor map upload and the removal target are
        validated (and the upload read + size-checked) BEFORE the layout is
        saved, so a rejected upload can no longer strand the saved layout.
        Every filesystem target derived from client input is constrained to
        maps_path via _safe_maps_child (audit #2: path traversal / arbitrary
        write+delete). The layout goes to the Store (atomic); only the map
        images live under maps_path.
        """
        # --- Validate the optional new-floor map upload ---
        map_target = None
        map_bytes = None
        if data.get("new_floor") == "true":
            map_file = data.get("file")
            if not map_file:
                return web.Response(status=400, text="Missing file")
            map_target = _safe_maps_child(maps_path, map_file.filename, _ALLOWED_MAP_EXTS)
            if map_target is None:
                return web.Response(status=400, text="Invalid map filename")
            # Up to 25 MB off aiohttp's temp file: not on the event loop.
            map_bytes = await hass.async_add_executor_job(map_file.file.read)
            if len(map_bytes) > MAX_MAP_UPLOAD_BYTES:
                return web.Response(status=413, text="Map file too large")

        # --- Validate the optional removal target ---
        # No extension allowlist here: a map stored under any earlier-accepted
        # extension must stay deletable (the allowlist is an UPLOAD guard, not a
        # reason to strand an existing floor). Containment still applies, and
        # the layout/calibration files are explicitly protected.
        remove_target = None
        remove_file = data.get("remove")
        if remove_file:
            remove_target = _safe_maps_child(maps_path, remove_file, None)
            if remove_target is None or remove_target.name in _PROTECTED_MAPS_FILES:
                return web.Response(status=400, text="Invalid remove target")

        # --- Inputs valid: persist. Map first (so the saved layout never names
        # a map that failed to write), then the layout, then delete the old map. ---
        if map_target is not None:
            try:
                async with aiofiles.open(map_target, "wb") as f:
                    await f.write(map_bytes)
            except Exception as e:
                _LOGGER.error(f"Failed to save maps: {e}")
                return web.Response(status=500, text="Failed to save maps")

        await save_layout(hass, coords_obj)

        # Never delete the map we just wrote (a replace with the same filename).
        if remove_target is not None and remove_target != map_target:
            try:
                await hass.async_add_executor_job(_unlink_if_exists, remove_target)
                _LOGGER.info(f"Removed file: {remove_target.name}")
            except Exception as e:
                _LOGGER.error(f"Failed to remove file {remove_target.name}: {e}")
                return web.Response(status=500, text="Failed to remove file")
        return None


def _unlink_if_exists(path):
    """Delete a file that may be gone already (runs in the executor)."""
    if path.exists():
        path.unlink()


def list_map_files(maps_path):
    """Map image file names on disk (runs in the executor)."""
    with os.scandir(maps_path) as entries:
        return [
            entry.name
            for entry in entries
            if entry.is_file() and entry.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ]


def list_thing_icons(icons_path):
    """Custom thing icons as picker options (runs in the executor)."""
    if not os.path.isdir(icons_path):
        return []
    with os.scandir(icons_path) as entries:
        return [
            {"value": f"/local/sextant_icons/{entry.name}", "label": entry.name}
            for entry in entries
            if entry.is_file() and entry.name.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif"))
        ]


class SextantUploadThingIconAPI(HomeAssistantView):
    """API to upload custom thing icons."""

    url = "/api/sextant/upload_thing_icon"
    name = "api:sextant:upload_thing_icon"
    requires_auth = True

    async def post(self, request):
        if (denied := _admin_only(request)) is not None:
            return denied
        hass = request.app["hass"]
        data = await request.post()
        icon_file = data.get("icon")
        if not icon_file:
            return web.Response(status=400, text="Missing icon")

        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(icon_file.filename).name)
        if not safe_name or Path(safe_name).suffix.lower() not in _ALLOWED_ICON_EXTS:
            return web.Response(status=400, text="Icons must be png, jpg, webp or gif")
        icon_bytes = await hass.async_add_executor_job(icon_file.file.read)
        if len(icon_bytes) > MAX_ICON_UPLOAD_BYTES:
            return web.Response(status=413, text="Icon file too large (2 MB max)")

        icons_path = hass.config.path("www/sextant_icons")
        try:
            await aiofiles.os.makedirs(icons_path, exist_ok=True)
            target_path = Path(icons_path) / safe_name
            async with aiofiles.open(target_path, "wb") as f:
                await f.write(icon_bytes)
        except Exception as e:
            _LOGGER.error(f"Failed to upload thing icon: {e}")
            return web.Response(status=500, text="Failed to upload icon")

        return web.json_response({
            "icon_url": f"/local/sextant_icons/{safe_name}",
            "icon_name": safe_name,
        })


class SextantCordsAPI(HomeAssistantView):
    """API endpoint that returns apitricords."""

    url = "/api/sextant/cords"
    name = "api:sextant:cords"
    requires_auth = True

    def __init__(self, hass):
        """Spara referens till hass"""
        self.hass = hass

    async def get(self, request):
        """Return apitricords from hass.data."""
        apitricords = self.hass.data.get(DOMAIN, {}).get("apitricords", {})

        if not apitricords:
            return web.json_response({"error": "No data available"}, status=404)

        return web.json_response(apitricords)

# Trilateration function
# Floor on the distance used in the Jacobian's direction vector. Only guards
# the 0/0 at a fit sitting exactly on a receiver; far below any real geometry.
_JAC_MIN_DIST = 1e-9


def trilaterate(known_points, bounds=None, min_weight_radius=1e-3, stable_hint=None):
    """Weighted least-squares position fit.

    known_points are (x, y, r[, w[, wr]]) tuples. The optional w is a per-point
    reliability in [0, 1] (1 = fully trusted); it multiplies the geometric
    1/r^2 weight, so a spiky reading pulls the fit less without being dropped.
    Missing w defaults to 1. The optional wr overrides r as the RADIUS USED IN
    THE GEOMETRIC WEIGHT (the residual always uses r): live callers pass the
    measured slant (px) here, so a height-corrected projection that collapsed
    to the floor cannot buy itself dominant 1/r^2 influence — following the
    4-tuple form with collapsed projected radii reintroduces exactly the 1.7.0
    hijack. min_weight_radius clamps whichever weight radius is in use.

    bounds, when given as (minx, miny, maxx, maxy), constrains the solution to
    the floor's extent: the fit then finds the best position WITHIN the map,
    which lands on the boundary when an unconstrained fit would escape it.

    min_weight_radius clamps the radius used in the 1/r^2 WEIGHT (never the
    residual). Callers pass a physical floor in pixels (MIN_WEIGHT_RADIUS_M x
    floor scale): a near-zero radius — device at the receiver, or a slant range
    fully consumed by a known mount height — is an honest reading, but its
    weight must stay finite or that single receiver decides the whole fit. The
    default keeps the bare no-scale fallback safe against division by zero.

    stable_hint, when given, is an (x, y) the caller already believes is close
    (typically last cycle's own fix) because nothing has moved much since. It
    tries ONE solve from there first instead of the usual multi-start battery;
    only when that fails to converge does it fall back to the full battery
    below, so a hint can only save work, never cost accuracy.
    """
    num_points = len(known_points)

    if num_points < 3: # Make sure there are enough points (min 3) to do a trilataration
        _LOGGER.error("At least three known points are required for trilateration.")
        return None

    # The per-point arrays are built ONCE here, not rebuilt inside the
    # objective: least_squares calls the objective (and the Jacobian) many
    # times per solve, and at this problem size the Python loop that used to
    # live in there cost far more than the arithmetic it performed.
    px = np.fromiter((p[0] for p in known_points), dtype=float, count=num_points)
    py = np.fromiter((p[1] for p in known_points), dtype=float, count=num_points)
    pr = np.fromiter((p[2] for p in known_points), dtype=float, count=num_points)
    rel = np.fromiter((p[3] if len(p) > 3 else 1.0 for p in known_points),
                      dtype=float, count=num_points)
    # Weight radius: the MEASURED slant when supplied (5th element), else the
    # fit radius. Height correction can legitimately collapse the projected
    # radius to the minimum while the measured slant is ~dz (metres) —
    # weighting by the projection handed such a receiver ~100x influence and
    # the fit snapped onto it (1.7.0 regression).
    wrad = np.fromiter((p[4] if len(p) > 4 else p[2] for p in known_points),
                       dtype=float, count=num_points)
    # reliability x geometric (1/r^2) weight, pre-rooted: the residual is
    # sqrt(w) * (distance - r), so sqrt() belongs here rather than per call.
    sqrt_w = np.sqrt(rel / np.maximum(wrad, min_weight_radius) ** 2)

    def objective_function(X):
        return sqrt_w * (np.hypot(px - X[0], py - X[1]) - pr)

    def jacobian(X):
        """Exact derivative of the residual: d/dX sqrt(w)(|X - p| - r).

        Supplying it saves least_squares the finite-difference pass, which
        costs one extra objective evaluation per unknown per iteration.
        The distance is floored before dividing: a fit sitting exactly on a
        receiver has an undefined direction, and a NaN there would poison the
        whole step rather than just that row.
        """
        dx = X[0] - px
        dy = X[1] - py
        d = np.maximum(np.hypot(dx, dy), _JAC_MIN_DIST)
        return np.column_stack((sqrt_w * dx / d, sqrt_w * dy / d))

    # MULTI-START. The soft_l1 objective is multi-modal once gross outliers are
    # present, and a single descent from the centroid can settle in a basin
    # that is not the best one — a trust region is no more immune to this than
    # any other local method. Solving from a few cheap, physically-motivated
    # starts and keeping the lowest-cost result measured (tools/solver_bench.py,
    # 2400 fits over the real floorplan geometry) as a median error improvement
    # of ~17 cm and p95 from 9.9 m to 5.6 m. At two parameters the extra solves
    # are nearly free.
    #
    #   1. receiver centroid — always plausible, and inside any bounds.
    #   2. the SMALLEST-radius receiver — the strongest single prior on where
    #      the thing is.
    #   3. the 1/r^2-weighted centroid — leans the same way as (2) without
    #      committing to one receiver.
    if bounds is not None:
        minx, miny, maxx, maxy = bounds
        lo, hi = [minx, miny], [maxx, maxy]
    else:
        lo, hi = [-np.inf, -np.inf], [np.inf, np.inf]

    # The fits run on the pure-numpy Levenberg-Marquardt solver in
    # solver_numpy.py rather than scipy's least_squares. Benchmarked on this
    # install's real floorplans (tools/solver_bench.py, 3000 fits, 15%
    # gross outliers): identical convergence, median accuracy 1.08 m vs
    # 1.08 m for scipy with the same multi-start, p95 6.1 m vs 6.0 m. The
    # positioning hot path therefore no longer needs scipy at all; the
    # calibration solver still does, until it is reworked.
    result = None
    if stable_hint is not None:
        hint = np.clip(np.asarray(stable_hint, dtype=float), lo, hi)
        r = least_squares_bounded_soft_l1(
            objective_function, hint, jacobian, (lo, hi), f_scale=SOLVER_ROBUST_F_SCALE,
        )
        if r.success:
            result = r

    if result is not None:
        x, y = result.x
        return x, y

    i_near = int(np.argmin(pr))
    wcen = 1.0 / np.maximum(pr, 1e-9) ** 2
    starts = [
        np.array([float(px.mean()), float(py.mean())]),
        np.array([float(px[i_near]), float(py[i_near])]),
        np.array([float((px * wcen).sum() / wcen.sum()),
                  float((py * wcen).sum() / wcen.sum())]),
    ]
    starts = [np.clip(s, lo, hi) for s in starts]

    # Skip a start that is effectively one already tried: with few receivers
    # the three candidates often coincide, and a duplicate solve buys nothing.
    unique_starts = []
    for s in starts:
        if not any(np.allclose(s, u, rtol=0, atol=1e-6) for u in unique_starts):
            unique_starts.append(s)

    # One call solves every start and keeps the lowest robust cost (the cost
    # is the same objective for every start, so it is directly comparable).
    result = least_squares_bounded_soft_l1(
        objective_function, unique_starts[0], jacobian, (lo, hi),
        f_scale=SOLVER_ROBUST_F_SCALE, extra_starts=unique_starts[1:],
    )

    if result is None or not result.success:
        # Non-convergence is an expected, handled outcome on ill-conditioned
        # input — a floor whose readings don't agree, or the self-test's
        # leave-one-out on a corner/degenerate receiver. Every caller treats
        # None as "not a contender" / "unsolved", so this is DEBUG, not ERROR
        # (it was spamming the log once the periodic self-test began running).
        _LOGGER.debug("Trilateration did not converge for %d points; skipping.", len(known_points))
        return None
    x, y = result.x # Extract the calculated coordinates
    return x, y # return the result


# --- Receiver self-localization: a zero-setup, ground-truthed accuracy bench --
# Every receiver's true position is known (it is placed on the map) and Bermuda
# measures the distance between scanners, so each receiver's position can be
# solved from the OTHERS' measured distances -- through the SAME trilaterate() +
# per-receiver correction + slant the live thing uses -- and compared to where
# it actually is (leave-one-out). This measures the SOLVER, per-receiver
# calibration, and geometry with no parked beacon and no hand-measured truth.
# It does NOT exercise the temporal filtering (a static, always-fresh anchor has
# no motion to smooth or stale readings to reject), and it is an OPTIMISTIC
# bound on real thing accuracy: calibration is fit on these very inter-receiver
# links, and receiver-to-receiver paths (ceiling height, clean line of sight)
# are easier than a body-worn beacon's. Consumed by tools/sextant_eval.py `selftest`.
SELFTEST_MIN_SAMPLES = 3  # need a median over at least this many raw distances
SELFTEST_SENSOR_INTERVAL = 1800  # refresh the accuracy sensor every 30 min


def _selftest_receivers(coords):
    """slug -> {entity, x, y, scale, floor, height, correction} for every placement."""
    out = {}
    if not isinstance(coords, dict):
        return out
    for floor in coords.get("floor", []):
        scale = floor.get("scale")
        if not scale:
            continue
        for rec in floor.get("receivers", []):
            cords = rec.get("cords") or {}
            eid = rec.get("entity_id")
            if not eid or cords.get("x") is None or cords.get("y") is None:
                continue
            height = rec.get("height")
            corr = rec.get("correction")
            out[str(eid)] = {
                "entity": str(eid),
                "x": float(cords["x"]),
                "y": float(cords["y"]),
                "scale": float(scale),
                "floor": floor.get("name"),
                "height": float(height) if isinstance(height, (int, float)) and 0 <= height <= 10 else None,
                "correction": float(corr) if isinstance(corr, (int, float)) and corr > 0 else 1.0,
            }
    return out


def _selftest_floor_bounds(coords, receivers):
    """Per-floor solver bounds = extent of ALL placed receivers + zone polygons
    (+10% margin), mirroring the live solve (update_trilateration_and_zone).

    Keyed on the floor so a left-out perimeter receiver's own known position
    still lies inside the feasible box — bounding only to the OTHER receivers
    would clamp it and inflate its error.
    """
    xs_by, ys_by = {}, {}
    for r in receivers.values():
        xs_by.setdefault(r["floor"], []).append(r["x"])
        ys_by.setdefault(r["floor"], []).append(r["y"])
    if isinstance(coords, dict):
        for floor in coords.get("floor", []):
            name = floor.get("name")
            for zone in floor.get("zones", []) or []:  # no-go zones still bound the floor
                for c in (zone.get("cords") or []):
                    if isinstance(c, dict) and c.get("x") is not None and c.get("y") is not None:
                        xs_by.setdefault(name, []).append(float(c["x"]))
                        ys_by.setdefault(name, []).append(float(c["y"]))
    out = {}
    for name, xs in xs_by.items():
        ys = ys_by[name]
        margin = 0.1 * max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        out[name] = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    return out


def _point_in_ring(x, y, ring):
    """Ray-casting point-in-polygon; a point on the boundary counts as inside."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_int = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x <= x_int:
                inside = not inside
    return inside


def _selftest_rooms(coords):
    """floor name -> [(room name, ring)] from the layout's rooms, in drawn order.

    No-go areas are not rooms. Legacy rectangle zones stored their corners in
    scan order, so those four are ordered around their centroid first (the
    same rule _floor_zone_polygons applies).
    """
    rooms = {}
    if not isinstance(coords, dict):
        return rooms
    for floor in coords.get("floor", []):
        entries = []
        for zone in floor.get("zones") or []:
            if not isinstance(zone, dict) or zone.get("no_go"):
                continue
            name = zone.get("entity_id") or zone.get("zone_id")
            ring = [(float(c["x"]), float(c["y"])) for c in (zone.get("cords") or [])
                    if isinstance(c, dict) and c.get("x") is not None and c.get("y") is not None]
            if not name or len(ring) < 3:
                continue
            if not zone.get("poly") and len(ring) == 4:
                cx = sum(x for x, _y in ring) / 4
                cy = sum(y for _x, y in ring) / 4
                ring.sort(key=lambda pt: math.atan2(pt[1] - cy, pt[0] - cx))
            entries.append((str(name), ring))
        rooms[floor.get("name")] = entries
    return rooms


def _room_at(rooms, x, y):
    """The first room on the floor containing (x, y), else None."""
    for name, ring in rooms or []:
        if _point_in_ring(x, y, ring):
            return name
    return None


def run_selftest(hass, samples=None, corrections=None, floors=None):
    """Leave-one-out receiver self-localization accuracy against known positions.

    ``corrections`` overrides the layout's per-receiver correction factors
    ({slug: factor}); ``floors`` restricts the receivers solved to those floor
    names. Together they let calibration judge a candidate set of corrections
    on one floor with the metric that matters, before writing anything.

    Solves on the RAW inter-receiver distances Sextant collects for calibration
    (median per link), not the Bermuda-filtered distance sensor the live thing
    reads — so the numbers also exclude Bermuda's own smoothing.

    ``samples`` may be a pre-snapshotted ``{"tx|rx": [floats]}`` taken on the
    event loop; callers running this in the executor MUST pass one, since the
    live calibration deques are appended to on the loop and iterating them off
    thread would race ("deque mutated during iteration"). Defaults to the live
    samples for direct/synchronous callers.
    """
    coords = get_layout(hass)
    receivers = _selftest_receivers(coords)
    for slug, factor in (corrections or {}).items():
        if slug in receivers and isinstance(factor, (int, float)) and factor > 0:
            receivers[slug]["correction"] = float(factor)
    floor_bounds = _selftest_floor_bounds(coords, receivers)
    rooms = _selftest_rooms(coords)
    if floors is not None:
        wanted = {str(f) for f in floors}
        receivers = {slug: r for slug, r in receivers.items() if str(r["floor"]) in wanted}
    if samples is None:
        samples = get_calibration_state(hass).get("samples", {})

    def _measured_m(target, rx):
        # The live thing sees the beacon (target) transmit and a receiver hear
        # it, so use exactly that direction ("target|rx") and the receiver's gain.
        vals = [float(v) for v in samples.get(f"{target}|{rx}", [])
                if isinstance(v, (int, float)) and v > 0]
        if len(vals) < SELFTEST_MIN_SAMPLES:
            return None
        return float(np.median(vals))

    solved, unsolved = [], []
    for slug, tgt in receivers.items():
        pts = []
        for oslug, o in receivers.items():
            if oslug == slug or o["floor"] != tgt["floor"]:
                continue
            raw = _measured_m(slug, oslug)
            if raw is None:
                continue
            d = raw * o["correction"]  # receiving scanner's correction (live path)
            horizontal = d
            if tgt["height"] is not None and o["height"] is not None:
                dz = o["height"] - tgt["height"]
                # Same floored projection as the live path (singularity guard;
                # the floor never exceeds the raw slant).
                floor_sq = min(d * d, MIN_WEIGHT_RADIUS_M * MIN_WEIGHT_RADIUS_M)
                horizontal = math.sqrt(max(d * d - dz * dz, floor_sq))
            # (x, y, projected radius, measured slant) in pixels — the slant is
            # the weight radius, mirroring the live solve.
            pts.append((o["x"], o["y"], horizontal * tgt["scale"], d * tgt["scale"]))
        room = _room_at(rooms.get(tgt["floor"]), tgt["x"], tgt["y"])
        if len(pts) < 3:
            unsolved.append({"entity": slug, "floor": tgt["floor"], "room": room, "heard_by": len(pts)})
            continue
        fix = trilaterate(
            [(px, py, pr, 1.0, psl) for (px, py, pr, psl) in pts],
            bounds=floor_bounds.get(tgt["floor"]),
            min_weight_radius=MIN_WEIGHT_RADIUS_M * tgt["scale"],
        )
        if fix is None:
            unsolved.append({"entity": slug, "floor": tgt["floor"], "room": room, "heard_by": len(pts), "reason": "no convergence"})
            continue
        err_px = math.hypot(fix[0] - tgt["x"], fix[1] - tgt["y"])
        solved.append({
            "entity": slug, "floor": tgt["floor"], "room": room,
            "known": [round(tgt["x"], 1), round(tgt["y"], 1)],
            "est": [round(float(fix[0]), 1), round(float(fix[1]), 1)],
            "error_m": round(err_px / tgt["scale"], 3),
            "heard_by": len(pts),
        })
    return {
        "receivers": solved,
        "unsolved": unsolved,
        "counts": {"placed": len(receivers), "solved": len(solved), "unsolved": len(unsolved)},
        # Floors in layout order and their rooms, so the breakdown can list a
        # room that has no proxy in it at all (a thing there is placed from
        # its neighbours' proxies, which is worth knowing).
        "floors": [f.get("name") for f in (coords.get("floor", []) if isinstance(coords, dict) else [])],
        "rooms": {floor: [name for name, _ring in entries] for floor, entries in rooms.items()},
    }


def _error_stats(rows):
    """solved / cep50 / cep95 / max / worst for a group of solved receivers."""
    errs = np.array([r["error_m"] for r in rows], dtype=float)
    worst = max(rows, key=lambda r: r["error_m"])
    return {
        "solved": len(rows),
        "cep50_m": round(float(np.percentile(errs, 50)), 3),
        "cep95_m": round(float(np.percentile(errs, 95)), 3),
        "max_m": round(float(errs.max()), 3),
        "worst": worst.get("entity"),
    }


def selftest_breakdown(result):
    """The self-test per floor and per room: which parts of the house the
    proxies place well and which they do not.

    ``floors`` has one row per floor in layout order; ``rooms`` one row per
    room per floor (rooms with no proxy inside them included, with
    ``solved`` 0 and no error figures), plus a ``room: None`` row for proxies
    placed outside every room. Within a floor the rooms come worst first,
    rooms without a proxy last. A whole-house CEP95 hides exactly this: a
    house can read 7 m overall because one basement room reads 7 m while
    the rest sit under 2 m.
    """
    solved = [r for r in result.get("receivers", []) if isinstance(r.get("error_m"), (int, float))]
    unsolved = result.get("unsolved", []) or []
    known_rooms = result.get("rooms", {}) or {}
    order = list(result.get("floors") or [])
    for r in solved + unsolved:
        if r.get("floor") not in order:
            order.append(r.get("floor"))
    for f in known_rooms:
        if f not in order:
            order.append(f)

    floors, rooms = [], []
    for f in order:
        on_floor = [r for r in solved if r.get("floor") == f]
        row = {"floor": f, "solved": 0, "unsolved": sum(1 for u in unsolved if u.get("floor") == f)}
        if on_floor:
            row.update(_error_stats(on_floor))
        floors.append(row)

        names = list(known_rooms.get(f, []))
        for extra in sorted({r.get("room") for r in solved + unsolved if r.get("floor") == f} - set(names) - {None}):
            names.append(extra)
        floor_rooms = []
        for room in names + [None]:
            here = [r for r in on_floor if r.get("room") == room]
            missing = sum(1 for u in unsolved if u.get("floor") == f and u.get("room") == room)
            if room is None and not here and not missing:
                continue  # nothing placed outside the rooms: no row for it
            row = {"floor": f, "room": room, "solved": 0, "unsolved": missing}
            if here:
                row.update(_error_stats(here))
            floor_rooms.append(row)
        floor_rooms.sort(key=lambda r: (0 if r["solved"] or r["unsolved"] else 1, -(r.get("cep95_m") or 0.0), str(r["room"])))
        rooms.extend(floor_rooms)
    return {"floors": floors, "rooms": rooms}


def _selftest_summary(result):
    """(state, attributes) for the accuracy sensor from a run_selftest result.

    State is CEP95 in metres (lower is better), or None (-> "unknown") when no
    receiver was solvable. Attributes stay compact (no per-receiver list) so
    they don't bloat recorder history; the full detail is on /api/sextant/selftest.
    """
    recv = [r for r in result.get("receivers", []) if isinstance(r.get("error_m"), (int, float))]
    counts = result.get("counts", {})
    attrs = {
        "solved": counts.get("solved"),
        "placed": counts.get("placed"),
        "unsolved": counts.get("unsolved"),
    }
    if not recv:
        return None, attrs
    errs = np.array([r["error_m"] for r in recv], dtype=float)
    attrs["cep50_m"] = round(float(np.percentile(errs, 50)), 3)
    attrs["cep95_m"] = round(float(np.percentile(errs, 95)), 3)
    attrs["mean_m"] = round(float(errs.mean()), 3)
    attrs["max_m"] = round(float(errs.max()), 3)
    worst = max(recv, key=lambda r: r["error_m"])
    attrs["worst"] = f"{worst.get('entity')} ({worst.get('error_m')} m)"
    by_floor = {}
    for r in recv:
        by_floor.setdefault(r.get("floor"), []).append(r["error_m"])
    attrs["per_floor_cep95_m"] = {
        f: round(float(np.percentile(v, 95)), 3) for f, v in by_floor.items()
    }
    by_room = {}
    for r in recv:
        if r.get("room"):
            by_room.setdefault(f"{r.get('floor')} / {r['room']}", []).append(r["error_m"])
    attrs["per_room_cep95_m"] = {
        k: round(float(np.percentile(v, 95)), 3) for k, v in by_room.items()
    }
    return attrs["cep95_m"], attrs


HISTORY_DEFAULT_POINTS = 3000
HISTORY_MAX_QUERY_POINTS = 20000


class SextantSelfTestAPI(HomeAssistantView):
    """Read-only receiver leave-one-out self-localization accuracy (see run_selftest)."""

    url = "/api/sextant/selftest"
    name = "api:sextant:selftest"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request):
        # One scipy solve per receiver; run off the event loop. Snapshot the
        # calibration sample deques HERE (on the loop) before handing to the
        # executor, so iterating them off-thread can't race calibration's ingest.
        samples = {k: list(v) for k, v in get_calibration_state(self.hass).get("samples", {}).items()}
        data = await self.hass.async_add_executor_job(run_selftest, self.hass, samples)
        data["breakdown"] = selftest_breakdown(data)
        return web.json_response(data)
