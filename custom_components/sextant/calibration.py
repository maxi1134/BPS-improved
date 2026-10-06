"""Receiver auto-calibration from probe-to-probe BLE measurements.

Each ESPHome probe advertises an iBeacon from the same Bluetooth MAC its
scanner uses, so every sibling probe ranges it and Bermuda accumulates the
measurements on the scanner's own device — for all devices, tracked or not.
The bermuda.dump_devices service exposes those adverts with raw distances.

Combined with the receivers' known positions on the floor plan (and the
floor's pixels-per-meter scale) this yields a measured-vs-true matrix over
receiver pairs. A robust least-squares fit decomposes the log-space error
into a receive-side bias per receiver (rx) and a transmit-side bias per
beacon (tx):

    log10(measured_ij / true_ij) ~= rx_j + tx_i

The per-receiver correction factor 10^(-rx_j) multiplies every distance that
receiver reports (see update_receiver_radii). Because Bermuda's distance
model is exponential in RSSI, a multiplicative distance factor is exactly
equivalent to an additive per-scanner RSSI offset, so nothing is lost by
correcting in distance space. Corrections are normalized to a geometric mean
of 1: they encode only how receivers differ from each other. A bias shared by
the whole fleet is dominated by the beacons' TX power, which says nothing
about the phones and watches actually being tracked — applying it would
uniformly rescale every thing distance and shift all positions (learned the
hard way: zones all went "unknown" the moment the first corrections landed).
"""

import asyncio
import json
import logging
import math
import re
import time
from array import array
from datetime import datetime, timezone

import numpy as np
from homeassistant.util import slugify
from .solver_numpy import least_squares_bounded

_LOGGER = logging.getLogger(__name__)

# Layout + calibration state now live in HA's Store (see storage.py). The
# shared write lock lives there too so both modules serialize on one lock
# without an import cycle.
from . import bermuda_source
from .storage import (
    LAYOUT_LOCK,
    get_layout_for_edit,
    save_layout,
    load_calib_state,
    save_calib_state,
)

DOMAIN = "sextant"
SAMPLE_INTERVAL = 10  # seconds between sample rounds (manual run)
DEFAULT_DURATION = 600  # seconds of sampling (manual run)
MIN_DURATION = 60
MAX_DURATION = 3600
MAX_CONSECUTIVE_FAILURES = 6
AUTO_SAMPLE_INTERVAL = 30  # seconds between sample rounds in continuous mode
AUTO_SOLVE_INTERVAL = 900  # seconds between re-solves in continuous mode
AUTO_MIN_WINDOW = 300  # seconds of data before the first auto solve
APPLY_EPSILON = 0.01  # relative correction change worth persisting
DUMP_DEVICES_TIMEOUT_S = 10  # cap on a single bermuda.dump_devices call
SAMPLES_MAXLEN = 720  # rolling window per pair (6 h at the auto interval)
STATE_SAMPLES_PER_PAIR = 100  # samples persisted per pair (a solve needs 5; 100 is 50 min)
STATE_MAX_AGE = SAMPLES_MAXLEN * AUTO_SAMPLE_INTERVAL  # drop older windows
STALE_ADVERT_SECS = 30  # ignore readings older than this within a sample round
MIN_SAMPLES_PER_PAIR = 5
MIN_TRUE_DISTANCE_M = 0.3  # closer pairs carry no path-loss information
MIN_PAIRS = 4
CORRECTION_MIN = 0.2
CORRECTION_MAX = 5.0
GAUGE_WEIGHT = 10.0
DIFF_WEIGHT = 2.0  # direction-difference equations are wall-free; trust them
# Asymmetric loss on absolute equations: walls only lengthen measurements, so
# a measurement above prediction is cheap to leave unexplained (positive
# side), while below prediction is physically impossible and expensive.
POS_SCALE = 0.12  # log10 units before wall-side residuals stop growing much
NEG_SLOPE = 3.0
NEG_SCALE = 0.4


class SampleWindow:
    """A bounded window of raw distances as a packed array of doubles.

    A deque of Python floats costs about 32 bytes a sample (the float object
    plus the deque's pointer to it); a packed double costs 8. A house-sized
    install holds a few thousand pairs, so at the 720-sample cap that is a
    16 MB window instead of 64 MB. It behaves like the deque it replaced for
    everything the module does with one: append, len, iterate, slice,
    list(). Trimming moves at most a few KB per append.
    """

    __slots__ = ("_buf", "maxlen")

    def __init__(self, values=(), maxlen=SAMPLES_MAXLEN):
        self.maxlen = int(maxlen)
        self._buf = array("d", values)
        self._trim()

    def _trim(self):
        excess = len(self._buf) - self.maxlen
        if excess > 0:
            del self._buf[:excess]

    def append(self, value):
        self._buf.append(float(value))
        self._trim()

    def __len__(self):
        return len(self._buf)

    def __iter__(self):
        return iter(self._buf)

    def __getitem__(self, index):
        return self._buf[index]

    def __repr__(self):
        return f"SampleWindow({list(self._buf)!r}, maxlen={self.maxlen})"



def _normalize(value):
    return str(value or "").strip().lower()


# Mirrors __init__._scanner_token (calibration cannot import __init__ — it
# would be circular): the trailing MAC-derived hex group in a scanner slug,
# a stable hardware identity that survives renames.
_SCANNER_TOKEN_RE = re.compile(r"^[0-9a-f]{5,12}$")


def _scanner_token(slug):
    if not slug:
        return None
    last = str(slug).rsplit("_", 1)[-1].lower()
    return last if _SCANNER_TOKEN_RE.match(last) else None


def _address_tail(address):
    """Last 3 bytes of a MAC as 6 hex chars, e.g. 'aa:bb:cc:4e:88:d8' -> '4e88d8'."""
    hexonly = re.sub(r"[^0-9a-f]", "", str(address or "").lower())
    return hexonly[-6:] if len(hexonly) >= 6 else None


def _token_matches_address(token, address):
    """Whether a slug's hardware token plausibly names this Bluetooth MAC.

    ESPHome derives device-name suffixes from the base/WiFi MAC while the
    scanner advertises from its Bluetooth MAC; ESP-IDF derives both from the
    base MAC as +0..+3 in the last octet, wrapping uint8 (0xfe -> 0x00, no
    carry). That offset is strictly one-way — a derived MAC is never BELOW
    the base the token came from — so use a directional modular window.
    Espressif allocates base MACs in blocks of 4, so a symmetric window would
    admit the previous same-batch chip (its BT MAC sits 2 BELOW a neighbor's
    base): different hardware, and the reason this must stay directional.
    """
    tail = _address_tail(address)
    if not token or not tail:
        return False
    tok = str(token).lower()[-6:]
    if len(tok) != 6:
        return False
    try:
        return tok[:4] == tail[:4] and (int(tail[4:6], 16) - int(tok[4:6], 16)) % 256 <= 3
    except ValueError:
        return False


async def save_calibration_state(hass) -> None:
    """Persist the latest solves and the sample window across restarts.

    The applied corrections live in bpsdata.txt and always survive; this file
    only keeps the panel's matrix and the rolling window warm so a reboot does
    not blank the display and restart the window from zero.
    """
    cal = get_calibration_state(hass)
    payload = {
        "saved_at": time.time(),
        "results": cal["results"],
        "applied": cal["applied"],
        # To the millimetre: the readings are RSSI-derived and good to decimetres
        # at best, and 17-digit floats made this file 5 MB for a 58-proxy house.
        "samples": {
            key: [round(v, 3) for v in list(values)[-STATE_SAMPLES_PER_PAIR:]]
            for key, values in cal["samples"].items()
        },
    }
    try:
        await save_calib_state(hass, payload)
    except Exception as e:
        _LOGGER.warning("Could not persist calibration state: %s", e)


async def async_restore_calibration_state(hass) -> None:
    """Reload the persisted solves and sample window at startup."""
    cal = get_calibration_state(hass)
    if cal["mode"] != "off":
        return
    payload = await load_calib_state(hass)
    if not isinstance(payload, dict):
        return  # first run, or nothing persisted: start cold

    if isinstance(payload.get("results"), dict):
        cal["results"] = payload["results"]
    if isinstance(payload.get("applied"), dict):
        cal["applied"] = payload["applied"]

    saved_at = payload.get("saved_at")
    fresh = isinstance(saved_at, (int, float)) and time.time() - saved_at <= STATE_MAX_AGE
    if fresh and isinstance(payload.get("samples"), dict):
        for key, values in payload["samples"].items():
            if isinstance(values, list):
                cal["samples"][key] = SampleWindow(
                    float(v) for v in values if isinstance(v, (int, float))
                )
    _LOGGER.info(
        "Restored calibration state (%d floor results, %d sample pairs%s)",
        len(cal["results"]),
        len(cal["samples"]),
        "" if fresh else ", window too old — dropped",
    )


async def _read_coords(hass):
    """The current layout as a fresh dict to mutate, or None if there is none.

    A deep copy from the store cache (never the live object) so the
    read-modify-write below can't leak a half-mutated layout to the tracking
    loop before the save lands.
    """
    return get_layout_for_edit(hass)


def _find_floor(coords, floor_name):
    for floor in coords.get("floor", []):
        if _normalize(floor.get("name")) == _normalize(floor_name):
            return floor
    return None


def get_calibration_state(hass) -> dict:
    hass.data.setdefault(DOMAIN, {})
    return hass.data[DOMAIN].setdefault(
        "calibration",
        {
            "state": "idle",  # idle | sampling | done | error
            "mode": "off",  # off | manual | auto
            "floor": None,  # manual runs are floor-scoped
            "started_at": None,
            "ends_at": None,
            "duration": None,
            "samples": {},  # "tx|rx" -> SampleWindow of raw distances (meters)
            "receivers": {},  # slug -> {"x", "y", "scale", "floor"}
            "results": {},  # floor name -> latest solve result
            "applied": {},  # floor name -> corrections last written to disk
            "last_solved_at": None,
            "auto_decisions": {},  # floor name -> what the last auto solve did and why
            "error": None,
            "task": None,
        },
    )


def _build_receiver_map(coords, floor_name=None) -> dict:
    """Map receiver slug -> position/scale/floor, for one floor or all floors."""
    receivers = {}
    for floor in coords.get("floor", []):
        if floor_name is not None and _normalize(floor.get("name")) != _normalize(floor_name):
            continue
        scale = floor.get("scale")
        if not scale:
            continue
        for receiver in floor.get("receivers", []):
            cords = receiver.get("cords") or {}
            # "calibrate": false keeps a proxy out of the pair fit entirely.
            # A radio whose distances are the wrong SHAPE - the laundry
            # tablet reads long up close and far too short across the room -
            # cannot be described by one multiplier, and letting the solver
            # try drags every other proxy's correction with it.
            if receiver.get("calibrate") is False:
                continue
            if receiver.get("entity_id") and cords.get("x") is not None and cords.get("y") is not None:
                uid = receiver.get("scanner_uid")
                height = receiver.get("height")
                address = receiver.get("address")
                receivers[str(receiver["entity_id"])] = {
                    # Scanner address: the placement's identity (see
                    # __init__._resolve_receiver_addresses); an exact match
                    # in _match_scanners, ahead of any name heuristics.
                    "address": address.lower() if isinstance(address, str) and address else None,
                    "x": float(cords["x"]),
                    "y": float(cords["y"]),
                    "scale": float(scale),
                    "floor": floor.get("name"),
                    # Hardware identity stored by a panel re-link (issue #64);
                    # lets calibration match the scanner after a rename.
                    "uid": uid if isinstance(uid, str) and uid else None,
                    # Mount height (m) set in the panel; makes the pair's
                    # ground-truth distance 3D (see _true_distance_m). The
                    # range guard also drops NaN/Infinity from a hand-edited
                    # file (NaN fails both comparisons), which would poison
                    # true_m and error out every solve on the floor.
                    "height": float(height)
                    if isinstance(height, (int, float)) and 0 <= height <= 10 else None,
                }
    return receivers


def _match_scanners(cal: dict, devices: dict) -> dict:
    """Map scanner MAC -> placed receiver slug (issue #63).

    Tier 1: slugify(current device name) == placed slug — names still in sync.
    Tier 2: hardware identity for placements no name matched: the stored
    scanner_uid (panel re-link, issue #64) or the MAC-derived hex tail in the
    placed slug, matched against a remaining device's name tail or Bluetooth
    MAC. Entity ids freeze at creation while device names follow renames, so
    tier 1 alone silently dropped renamed/moved probes from calibration —
    always the same ones, with tracking still fine. Only a UNIQUE candidate
    wins, so a rename can never quietly pair two different scanners.

    Placed slugs that matched anything are timestamped in cal["matched_placed"]
    so the report can tell "no matching scanner" apart from "matched but no
    adverts sampled".
    """
    scanners = []
    for dev in devices.values():
        if not isinstance(dev, dict) or dev.get("_is_scanner") is not True:
            continue
        address = str(dev.get("address") or "").lower()
        scanners.append((address, slugify(str(dev.get("name") or ""))))

    scanner_slug_by_mac = {}
    # Tier 0: the placement carries the scanner's address. Exact, and immune
    # to renames; a placement matched here is never re-guessed by name.
    by_address = {
        info["address"]: slug for slug, info in cal["receivers"].items()
        if info.get("address")
    }
    for address, _name_slug in scanners:
        if address in by_address:
            scanner_slug_by_mac[address] = by_address[address]
    for address, name_slug in scanners:
        if address in scanner_slug_by_mac:
            continue
        if name_slug in cal["receivers"] and name_slug not in scanner_slug_by_mac.values():
            scanner_slug_by_mac[address] = name_slug

    matched_slugs = set(scanner_slug_by_mac.values())
    # Tier-2 candidates: devices tier 1 didn't claim, excluding any whose
    # current name belongs to a placement on ANY floor — a manual run maps
    # only its own floor, so without the all-floors guard a name-synced
    # scanner placed elsewhere would look free here and could be captured
    # while a placement's true scanner happens to be offline.
    all_placed = cal.get("all_placed_slugs") or set(cal["receivers"])
    free_devs = [
        (a, n) for a, n in scanners
        if a not in scanner_slug_by_mac and n not in all_placed
    ]
    # Iterate to a fixpoint: consuming a device can turn a previously
    # ambiguous placement unique, and bpsdata receiver order must not decide
    # who gets matched.
    pending = [s for s in cal["receivers"] if s not in matched_slugs]
    progress = True
    while progress and pending:
        progress = False
        for slug in list(pending):
            token = cal["receivers"][slug].get("uid") or _scanner_token(slug)
            if not token:
                pending.remove(slug)
                continue
            candidates = {
                a for a, n in free_devs
                if _scanner_token(n) == token or _token_matches_address(token, a)
            }
            if len(candidates) == 1:
                address = candidates.pop()
                scanner_slug_by_mac[address] = slug
                matched_slugs.add(slug)
                free_devs = [(a, n) for a, n in free_devs if a != address]
                pending.remove(slug)
                progress = True

    # slug -> when it last matched. Timestamps (not a grow-only set) so an
    # unbounded auto window doesn't keep reporting "matched" for a placement
    # whose match broke long ago.
    matched = cal.get("matched_placed")
    if not isinstance(matched, dict):
        matched = {}
        cal["matched_placed"] = matched
    now = time.time()
    for slug in matched_slugs:
        matched[slug] = now
    return scanner_slug_by_mac


def _all_placed_slugs(coords) -> set:
    """Every placed receiver slug across ALL floors — tier-2 matching must not
    treat another floor's (or any placed) scanner as a free candidate."""
    slugs = set()
    for floor in coords.get("floor", []):
        for receiver in floor.get("receivers", []):
            eid = receiver.get("entity_id")
            if isinstance(eid, str) and eid:
                slugs.add(eid)
    return slugs


def _ingest_dump(cal: dict, devices: dict) -> None:
    """Extract fresh probe-to-probe raw distances from a dump_devices payload."""
    if not isinstance(devices, dict):
        return

    # Scanner MAC -> receiver slug, restricted to receivers on this floor.
    scanner_slug_by_mac = _match_scanners(cal, devices)

    # Monotonic "now": the freshest advert stamp in the payload.
    newest = 0.0
    for dev in devices.values():
        adverts = dev.get("adverts") if isinstance(dev, dict) else None
        if not isinstance(adverts, dict):
            continue
        for advert in adverts.values():
            stamp = advert.get("stamp") if isinstance(advert, dict) else None
            if isinstance(stamp, (int, float)) and stamp > newest:
                newest = stamp

    # The beacon advertises from the scanner's own MAC, so the transmitter's
    # measurements live on the scanner devices themselves.
    for dev in devices.values():
        if not isinstance(dev, dict) or dev.get("_is_scanner") is not True:
            continue
        tx_slug = scanner_slug_by_mac.get(str(dev.get("address") or "").lower())
        if tx_slug is None:
            continue
        adverts = dev.get("adverts")
        if not isinstance(adverts, dict):
            continue
        for advert in adverts.values():
            if not isinstance(advert, dict):
                continue
            rx_slug = scanner_slug_by_mac.get(str(advert.get("scanner_address") or "").lower())
            if rx_slug is None or rx_slug == tx_slug:
                continue
            stamp = advert.get("stamp")
            if not isinstance(stamp, (int, float)) or newest - stamp > STALE_ADVERT_SECS:
                continue
            distance = advert.get("rssi_distance_raw")
            if not isinstance(distance, (int, float)) or distance <= 0:
                continue
            cal["samples"].setdefault(f"{tx_slug}|{rx_slug}", SampleWindow()).append(distance)


def _scanner_devices_from_directory(directory) -> dict:
    """The scanner half of a dump_devices payload, built from Bermuda's scanner
    directory, so _match_scanners serves both sample sources unchanged."""
    devices = {}
    for address, info in (directory or {}).items():
        if not address or not isinstance(info, dict):
            continue
        address = str(address).lower()
        devices[address] = {
            "_is_scanner": True,
            "address": address,
            "name": info.get("name") or info.get("slug") or "",
        }
    return devices


def _ingest_ranging(cal: dict, ranging, directory) -> None:
    """Extract probe-to-probe raw distances from the fork's scanner-ranging table.

    The same samples _ingest_dump takes from a dump_devices payload, without
    Bermuda serialising every device it knows first: the table holds only
    how each scanner hears the other scanners, which is all calibration ever
    read from a dump. Ages are relative to now (the dump path measures them
    against the newest stamp in the payload); with fresh data the two agree.
    """
    if not isinstance(ranging, dict) or not isinstance(ranging.get("scanners"), dict):
        return
    scanner_slug_by_mac = _match_scanners(cal, _scanner_devices_from_directory(directory))
    for tx_address, heard_by in ranging["scanners"].items():
        tx_slug = scanner_slug_by_mac.get(str(tx_address or "").lower())
        if tx_slug is None or not isinstance(heard_by, dict):
            continue
        for rx_address, reading in heard_by.items():
            rx_slug = scanner_slug_by_mac.get(str(rx_address or "").lower())
            if rx_slug is None or rx_slug == tx_slug or not isinstance(reading, dict):
                continue
            age = reading.get("age")
            if isinstance(age, (int, float)) and age > STALE_ADVERT_SECS:
                continue
            distance = reading.get("distance_raw")
            if not isinstance(distance, (int, float)) or distance <= 0:
                continue
            cal["samples"].setdefault(f"{tx_slug}|{rx_slug}", SampleWindow()).append(distance)


def _true_distance_m(cal: dict, slug_a: str, slug_b: str):
    a = cal["receivers"].get(slug_a)
    b = cal["receivers"].get(slug_b)
    if not a or not b or _normalize(a["floor"]) != _normalize(b["floor"]) or not a["scale"]:
        return None
    horizontal = math.hypot(a["x"] - b["x"], a["y"] - b["y"]) / a["scale"]
    # Known mount heights make the truth 3D: the probes' adverts travel the
    # slant path, so judging them against the flat map distance reads pure
    # geometry as RSSI bias — a 0.3 m vs 2.2 m pair 3 m apart on the map is
    # really 3.55 m apart, an 18% phantom error the fit would otherwise bake
    # into that receiver's correction. Applied only when BOTH heights are set;
    # a lone height says nothing about the pair's geometry.
    if a.get("height") is not None and b.get("height") is not None:
        return math.hypot(horizontal, a["height"] - b["height"])
    return horizontal


def solve_snapshot(cal: dict) -> dict:
    """A private copy of everything solve() reads, taken ON the event loop.

    solve() is handed to an executor (it is the single longest blocking call in
    the integration - tens of milliseconds on a desktop, hundreds on a Pi-class
    box, per floor). But cal["samples"] is a dict of windows the ingest path
    appends to on the loop, so iterating it off-thread races that ingest: a new
    pair key raises "dictionary changed size during iteration", and a window
    growing under np.median silently returns a median of a moving set. Copying
    the two structures it reads is cheap next to the solve and removes the race
    entirely - the same discipline run_selftest already uses.
    """
    return {
        "samples": {k: list(v) for k, v in (cal.get("samples") or {}).items()},
        "receivers": dict(cal.get("receivers") or {}),
    }


async def async_solve(hass, cal: dict, floor_name: str):
    """solve() off the event loop, against a snapshot taken on it."""
    snap = solve_snapshot(cal)
    return await hass.async_add_executor_job(solve, snap, floor_name)


def solve(cal: dict, floor_name: str):
    """Fit per-receiver corrections for one floor from the collected samples.

    Returns a result dict, or raises ValueError when there is not enough data.
    """
    pairs = []  # (tx_slug, rx_slug, true_m, measured_m, n_samples)
    for key, values in cal["samples"].items():
        if len(values) < MIN_SAMPLES_PER_PAIR:
            continue
        tx_slug, rx_slug = key.split("|", 1)
        tx_info = cal["receivers"].get(tx_slug)
        if not tx_info or _normalize(tx_info["floor"]) != _normalize(floor_name):
            continue
        true_m = _true_distance_m(cal, tx_slug, rx_slug)
        if true_m is None or true_m < MIN_TRUE_DISTANCE_M:
            continue
        measured = float(np.median(values))
        if measured <= 0:
            continue
        pairs.append((tx_slug, rx_slug, true_m, measured, len(values)))

    # Placed receivers absent from the matrix, split by cause so the report
    # can say WHY (issue #63): never matched to any Bermuda scanner (identity
    # drift — a rename left the device name out of sync with the frozen
    # entity-id slug) vs matched but no usable adverts (not beaconing, or too
    # few/too-close samples). Computed BEFORE the too-few-pairs gate so the
    # strongest mismatch case (so few matches the solve can't even run) still
    # names its culprits. "Matched" counts only recent matches, so a match
    # that broke mid-window ages out instead of masking the drift.
    participating = {p[0] for p in pairs} | {p[1] for p in pairs}
    matched = cal.get("matched_placed")
    if not isinstance(matched, dict):
        matched = {}
    cutoff = time.time() - STATE_MAX_AGE
    matched_fresh = {
        s for s, ts in matched.items() if isinstance(ts, (int, float)) and ts >= cutoff
    }
    placed_on_floor = sorted(
        s for s, r in cal["receivers"].items()
        if _normalize(r["floor"]) == _normalize(floor_name)
    )
    missing_unmatched = [s for s in placed_on_floor if s not in participating and s not in matched_fresh]
    missing_no_data = [s for s in placed_on_floor if s not in participating and s in matched_fresh]

    if len(pairs) < MIN_PAIRS:
        hint = ""
        if missing_unmatched:
            hint = (
                " Placed receivers with no matching Bermuda scanner (renamed device?): "
                + ", ".join(missing_unmatched) + "."
            )
        raise ValueError(
            f"Only {len(pairs)} usable receiver pairs (need at least {MIN_PAIRS}). "
            "Sample longer, or check that the probes are advertising their iBeacon."
            + hint
        )

    participants = sorted({p[0] for p in pairs} | {p[1] for p in pairs})
    index = {slug: i for i, slug in enumerate(participants)}
    n = len(participants)

    y = np.array([math.log10(p[3] / p[2]) for p in pairs])
    # Distant pairs cross more walls; the exponential model amplifies their
    # error, so weight them down.
    weights = np.array([1.0 / (1.0 + p[2]) for p in pairs])
    tx_idx = np.array([index[p[0]] for p in pairs])
    rx_idx = np.array([index[p[1]] for p in pairs])

    # Wall attenuation is a property of the PATH, so it cancels exactly in
    # the difference between the two directions of a pair:
    #   y_ab - y_ba = (rx_b + tx_a) - (rx_a + tx_b)
    # These wall-free equations pin the receivers' relative biases.
    by_key = {(p[0], p[1]): k for k, p in enumerate(pairs)}
    diff_rows = []
    for (a, b), k_ab in by_key.items():
        k_ba = by_key.get((b, a))
        if k_ba is not None and a < b:
            diff_rows.append((k_ab, k_ba))
    diff_ab = np.array([d[0] for d in diff_rows], dtype=int)
    diff_ba = np.array([d[1] for d in diff_rows], dtype=int)

    def residuals(x):
        rx = x[:n]
        tx = x[n:]
        # Absolute equations, asymmetric: e > 0 means the pair measures long,
        # which a wall explains — compress it so walls stay cheap. e < 0 is
        # physically impossible (walls cannot shorten a path), so it stays
        # expensive; the node biases end up tracking each node's cleanest
        # (line-of-sight) paths instead of averaging its walls in.
        e = y - (rx[rx_idx] + tx[tx_idx])
        e_pos = np.clip(e, 0.0, None)
        e_neg = np.clip(-e, 0.0, None)
        res = (
            POS_SCALE * np.log1p(e_pos / POS_SCALE)
            - NEG_SLOPE * NEG_SCALE * np.log1p(e_neg / NEG_SCALE)
        ) * weights
        if len(diff_rows):
            pred_diff = (rx[rx_idx[diff_ab]] + tx[tx_idx[diff_ab]]) - (rx[rx_idx[diff_ba]] + tx[tx_idx[diff_ba]])
            meas_diff = y[diff_ab] - y[diff_ba]
            res = np.append(res, DIFF_WEIGHT * (pred_diff - meas_diff))
        return np.append(res, GAUGE_WEIGHT * np.mean(tx))

    # Pure numpy (solver_numpy): the same bounded Levenberg-Marquardt descent
    # the trilateration uses, with a linear loss and a forward-difference
    # Jacobian - exactly what the scipy call here used to do, without the
    # dependency.
    fit = least_squares_bounded(
        residuals,
        np.zeros(2 * n),
        bounds=(-1.0, 1.0),  # each side capped at a 10x factor
    )
    rx = fit.x[:n]
    tx = fit.x[n:]

    # Corrections must encode RELATIVE receiver differences only. Any bias
    # shared by the whole fleet — typically the beacons' TX power differing
    # from the ref_power Bermuda's thing calibration assumes — would rescale
    # every thing distance at once and shift all trilaterated positions
    # (points drift out of their zones). Normalize to a geometric mean of 1
    # so the absolute scale stays with Bermuda's own calibration.
    # (Cast to plain floats: numpy scalars are not JSON serializable.)
    raw_factors = {slug: float(10 ** (-rx[i])) for slug, i in index.items()}
    log_mean = float(np.mean([math.log10(f) for f in raw_factors.values()]))
    corrections = {
        slug: round(min(CORRECTION_MAX, max(CORRECTION_MIN, f / (10 ** log_mean))), 4)
        for slug, f in raw_factors.items()
    }

    # A node with no line-of-sight path absorbs its walls into the fitted
    # bias, and that is locally invisible — the residuals look clean. The
    # honest observable warning sign is an aggressive correction (genuine
    # hardware gain differences are small) or hardly any pairs to fit from.
    pair_count = {}
    for p in pairs:
        pair_count[p[0]] = pair_count.get(p[0], 0) + 1
        pair_count[p[1]] = pair_count.get(p[1], 0) + 1
    low_confidence = sorted(
        slug
        for slug in corrections
        if not (0.5 <= corrections[slug] <= 2.0) or pair_count.get(slug, 0) < 3
    )

    # Median, not RMS: the asymmetric loss deliberately leaves through-wall
    # pairs unexplained, and their large residuals would dominate an RMS and
    # can even make "after" read worse than "before" while the fit is fine.
    def typical(values):
        return float(np.median(np.abs(values))) if len(values) else 0.0

    before = typical(y)
    after = typical(y - (rx[rx_idx] + tx[tx_idx]))

    matrix = [
        {
            "tx": p[0],
            "rx": p[1],
            "true_m": round(p[2], 2),
            "measured_m": round(p[3], 2),
            "corrected_m": round(p[3] * corrections[p[1]], 2),
            "error_pct": round((p[3] / p[2] - 1.0) * 100.0, 1),
            "samples": p[4],
        }
        for p in pairs
    ]

    return {
        "floor": floor_name,
        "solved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pairs_used": len(pairs),
        "bidirectional_pairs": len(diff_rows),
        "low_confidence": low_confidence,
        "receivers": corrections,
        "rx_bias_db_equident": {
            # The equivalent Bermuda "Calibration 2" rssi_offset (attenuation 3).
            slug: round(-30.0 * math.log10(corrections[slug]), 1)
            for slug in corrections
        },
        # Typical multiplicative error before/after, e.g. 1.35 = 35% off.
        "error_factor_before": round(10 ** before, 3),
        "error_factor_after": round(10 ** after, 3),
        "matrix": matrix,
        "missing_unmatched": missing_unmatched,
        "missing_no_data": missing_no_data,
    }


async def _dump_devices(hass) -> dict:
    # Bounded so a hung/slow Bermuda service can't stall the sampling loop
    # indefinitely; the caller counts a timeout as a normal sampling failure.
    response = await asyncio.wait_for(
        hass.services.async_call(
            "bermuda",
            "dump_devices",
            {"configured_devices": True},
            blocking=True,
            return_response=True,
        ),
        timeout=DUMP_DEVICES_TIMEOUT_S,
    )
    return response or {}


async def _collect_samples(hass, cal: dict) -> str:
    """One round of proxy-to-proxy distances into the window; the source used.

    The Bermuda fork's scanner-ranging table comes first: it is read from the
    scanner objects in place, a few hundred KB for a house-sized install.
    Stock Bermuda falls back to the dump_devices service, which serialises
    every configured device with all of its adverts on the event loop
    (several MB, a good part of a second, every round).
    """
    ranging = bermuda_source.async_get_scanner_ranging(hass, max_age=STALE_ADVERT_SECS)
    if ranging is not None:
        directory = bermuda_source.async_get_scanner_directory(hass)
        if directory is not None:
            _ingest_ranging(cal, ranging, directory)
            cal["sample_source"] = "ranging"
            return "ranging"
    _ingest_dump(cal, await _dump_devices(hass))
    cal["sample_source"] = "dump"
    return "dump"


async def _sample_loop(hass, cal: dict) -> None:
    """One-shot, floor-scoped sampling window (manual run)."""
    failures = 0
    try:
        while time.time() < cal["ends_at"]:
            try:
                await _collect_samples(hass, cal)
                failures = 0
            except Exception as e:  # service missing, timeout, bad payload
                failures += 1
                _LOGGER.warning("Calibration sampling failed (%d): %s", failures, e)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    cal["state"] = "error"
                    cal["mode"] = "off"
                    cal["error"] = (
                        "Bermuda sampling kept failing — is the Bermuda "
                        f"integration installed and current? Last error: {e}"
                    )
                    return
            await asyncio.sleep(SAMPLE_INTERVAL)

        # One final round before solving so placements edited late in the
        # window (re-link + save inside the last sample gap) are matched and
        # classified against reality — mirrors the auto loop's pre-solve
        # ingest. A failed round just falls back to what the window gathered.
        try:
            await _collect_samples(hass, cal)
        except Exception as e:
            _LOGGER.debug("Final calibration sample round failed: %s", e)

        result = await async_solve(hass, cal, cal["floor"])
        cal["results"][result["floor"]] = result
        cal["last_solved_at"] = result["solved_at"]
        cal["state"] = "done"
        cal["mode"] = "off"
        await save_calibration_state(hass)
    except ValueError as e:
        cal["state"] = "error"
        cal["mode"] = "off"
        cal["error"] = str(e)
    except asyncio.CancelledError:
        cal["state"] = "idle"
        cal["mode"] = "off"
        raise
    except Exception as e:
        _LOGGER.exception("Calibration failed")
        cal["state"] = "error"
        cal["mode"] = "off"
        cal["error"] = str(e)


async def _auto_loop(hass, cal: dict) -> None:
    """Continuous mode: sample forever, re-solve and re-apply periodically."""
    started = time.time()
    last_solve = 0.0
    try:
        while True:
            try:
                await _collect_samples(hass, cal)
                if cal["error"] and cal["error"].startswith("Bermuda sampling"):
                    cal["error"] = None
            except Exception as e:
                _LOGGER.warning("Auto-calibration sampling failed: %s", e)
                cal["error"] = f"Bermuda sampling failing: {e}"

            now = time.time()
            if now - started >= AUTO_MIN_WINDOW and now - last_solve >= AUTO_SOLVE_INTERVAL:
                last_solve = now
                try:
                    await _auto_solve_and_apply(hass, cal)
                    await save_calibration_state(hass)
                except Exception as e:
                    _LOGGER.exception("Auto-calibration solve failed")
                    cal["error"] = str(e)
            await asyncio.sleep(AUTO_SAMPLE_INTERVAL)
    except asyncio.CancelledError:
        raise


async def _auto_solve_and_apply(hass, cal: dict) -> None:
    """Re-solve every floor and persist corrections that changed enough."""
    # Refresh the receiver map and re-match BEFORE taking the file lock: every
    # ingest so far this cycle ran against the OLD map, so a receiver placed
    # or re-linked since would be misreported as "no matching scanner" (a
    # rename hint) for a whole solve interval despite matching perfectly. And
    # the dump_devices fallback is an external RPC — awaiting it under
    # LAYOUT_LOCK would let a wedged Bermuda hang every layout writer
    # (panel saves included), where pre-lock it only delays this solve.
    coords = await _read_coords(hass)
    if not coords:
        return
    cal["receivers"] = _build_receiver_map(coords)
    cal["all_placed_slugs"] = _all_placed_slugs(coords)
    try:
        await _collect_samples(hass, cal)
    except Exception as e:
        _LOGGER.debug("Pre-solve sample round failed; missing-receiver buckets may lag one cycle: %s", e)
    async with LAYOUT_LOCK:
        await _auto_solve_and_apply_locked(hass, cal)


JUDGE_MARGIN = 0.02  # a candidate must beat the incumbent's median by this much (relative)


async def _judge_by_selftest(hass, cal: dict, coords: dict, floor: dict, result: dict):
    """Median self-test error on this floor with no corrections, with the ones
    in place, and with this solve's. None when too few proxies are solvable.

    The fit's own before/after error factor is a median over pairs that the
    asymmetric loss deliberately leaves unexplained (walls), so it can read
    worse while the fit is fine. The leave-one-out self-test is the metric
    that matters - how far each proxy lands from where it is placed - and
    it takes a candidate correction set without writing it anywhere.
    """
    from . import run_selftest  # noqa: PLC0415 - the package imports this module

    floor_name = floor.get("name")
    slugs = [str(r.get("entity_id")) for r in floor.get("receivers", []) if r.get("entity_id")]
    current = {str(r.get("entity_id")): r.get("correction") for r in floor.get("receivers", [])
               if isinstance(r.get("correction"), (int, float)) and r.get("correction") > 0}
    samples = solve_snapshot(cal)["samples"]
    candidates = {
        "none": {slug: 1.0 for slug in slugs},
        "current": {slug: current.get(slug, 1.0) for slug in slugs},
        "new": {slug: result["receivers"].get(slug, current.get(slug, 1.0)) for slug in slugs},
    }
    medians, solved = {}, {}
    for name, corrections in candidates.items():
        res = await hass.async_add_executor_job(run_selftest, hass, samples, corrections, {floor_name})
        errs = sorted(r["error_m"] for r in res.get("receivers", []) if isinstance(r.get("error_m"), (int, float)))
        solved[name] = len(errs)
        medians[name] = float(np.median(errs)) if errs else None
    if min(solved.values()) < 3:
        return None
    stamp = floor.get("calibration") or {}
    return {
        "none_m": round(medians["none"], 3),
        "current_m": round(medians["current"], 3) if current else None,
        "new_m": round(medians["new"], 3),
        "solved": solved["new"],
        "current_auto": bool(current) and bool(stamp.get("auto")),
    }


def _auto_decision(judge, result):
    """("apply" | "hold" | "revert", reason) for an auto solve.

    With a self-test verdict: apply a solve that beats both no corrections and
    the ones in place by JUDGE_MARGIN; take out corrections auto itself wrote
    when none beats them by that margin; otherwise leave the floor alone.
    Without one (too few solvable proxies), fall back to the fit's own
    error factor: apply only if it did not get worse.
    """
    if not judge:
        worse = result["error_factor_after"] > result["error_factor_before"]
        return ("hold", "fit reads worse than no correction; too few proxies for the self-test") if worse else ("apply", "fit improves; too few proxies for the self-test")
    none_m, current_m, new_m = judge["none_m"], judge["current_m"], judge["new_m"]
    incumbent = current_m if current_m is not None else none_m
    if new_m <= incumbent * (1 - JUDGE_MARGIN) and new_m <= none_m:
        return "apply", f"self-test median {incumbent:.2f} m -> {new_m:.2f} m"
    if current_m is not None and judge.get("current_auto") and none_m <= current_m * (1 - JUDGE_MARGIN):
        return "revert", f"no corrections beat the ones auto applied: {current_m:.2f} m -> {none_m:.2f} m"
    return "hold", f"this solve would be {new_m:.2f} m against {incumbent:.2f} m in place"


async def _auto_solve_and_apply_locked(hass, cal: dict) -> None:
    # Re-read inside the lock so a concurrent writer is not clobbered; the
    # wrapper's pre-lock read was only to refresh the matching state.
    coords = await _read_coords(hass)
    if not coords:
        return
    # Floors and receivers may have been edited since the last cycle.
    cal["receivers"] = _build_receiver_map(coords)
    cal["all_placed_slugs"] = _all_placed_slugs(coords)

    changed = False
    for floor in coords.get("floor", []):
        floor_name = floor.get("name")
        on_floor = [s for s, r in cal["receivers"].items() if _normalize(r["floor"]) == _normalize(floor_name)]
        if len(on_floor) < 3:
            continue
        try:
            result = await async_solve(hass, cal, floor_name)
        except ValueError:
            continue  # not enough pairs on this floor yet
        cal["results"][floor_name] = result
        cal["last_solved_at"] = result["solved_at"]

        previous = cal["applied"].get(floor_name, {})
        decisions = cal.setdefault("auto_decisions", {})
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if _calibration_target(coords) == "bermuda":
            # Offsets already written are inside the samples, so each solve
            # fits the RESIDUAL: nothing to do while it stays near 1.0. (The
            # self-test cannot judge offsets that live inside Bermuda.)
            deltas = [abs(c - 1.0) for c in result["receivers"].values()]
            if previous and deltas and max(deltas) < APPLY_EPSILON:
                decisions[floor_name] = {"action": "hold", "reason": "residual within 1 %", "at": now_iso}
                continue
            action, reason = "apply", "residual moved"
        else:
            # "Applied" is what this loop last wrote; if the layout no longer
            # carries those corrections (an older copy was saved over it),
            # judge afresh rather than deciding nothing has changed.
            stored = {str(r.get("entity_id")): r.get("correction") for r in floor.get("receivers", [])}
            if any(stored.get(slug) is None for slug in previous):
                previous = {}
            deltas = [abs(result["receivers"][slug] / previous.get(slug, 1.0) - 1.0) for slug in result["receivers"]]
            if previous and deltas and max(deltas) < APPLY_EPSILON:
                decisions[floor_name] = {"action": "hold", "reason": "no factor moved more than 1 %", "at": now_iso}
                continue
            judge = await _judge_by_selftest(hass, cal, coords, floor, result)
            result["selftest"] = judge
            action, reason = _auto_decision(judge, result)

        decisions[floor_name] = {"action": action, "reason": reason, "at": now_iso, **({"selftest": result.get("selftest")} if result.get("selftest") else {})}
        if action == "apply":
            try:
                await _write_floor_corrections(hass, coords, floor, result, auto=True)
            except ValueError as err:
                _LOGGER.warning("Auto-calibration could not apply for %s: %s", floor_name, err)
                continue
            cal["applied"][floor_name] = dict(result["receivers"])
            changed = True
            _LOGGER.info("Auto-calibration applied on %s: %s", floor_name, reason)
        elif action == "revert":
            for receiver in floor.get("receivers", []):
                receiver.pop("correction", None)
            floor["calibration"] = {**(floor.get("calibration") or {}), "applied_at": now_iso, "auto": True, "reverted": True}
            cal["applied"].pop(floor_name, None)
            changed = True
            _LOGGER.info("Auto-calibration removed its corrections on %s: %s", floor_name, reason)
        else:
            _LOGGER.info("Auto-calibration held back on %s: %s", floor_name, reason)

    if changed:
        await save_layout(hass, coords)
        _LOGGER.info("Auto-calibration updated receiver corrections")


async def start_calibration(hass, floor_name: str, duration: int) -> dict:
    cal = get_calibration_state(hass)
    if cal["mode"] == "auto":
        raise ValueError("Auto calibration is running; turn it off for a manual run.")
    if cal["state"] == "sampling":
        raise ValueError("A calibration is already running.")
    # A finished task object may still be mid-teardown; make sure it is gone
    # before its state fields are reused.
    await _stop_task(cal)

    coords = await _read_coords(hass)
    if not coords:
        raise ValueError("No Sextant data saved yet.")
    floor = _find_floor(coords, floor_name)
    if floor is None:
        raise ValueError(f'No floor named "{floor_name}".')
    if not floor.get("scale"):
        raise ValueError("The floor has no scale; set it before calibrating.")

    receivers = _build_receiver_map(coords, floor_name=floor_name)
    if len(receivers) < 3:
        raise ValueError("At least three placed receivers are needed to calibrate.")

    duration = max(MIN_DURATION, min(MAX_DURATION, int(duration or DEFAULT_DURATION)))
    now = time.time()
    cal.update(
        {
            "state": "sampling",
            "mode": "manual",
            "floor": floor.get("name"),
            "started_at": now,
            "ends_at": now + duration,
            "duration": duration,
            "samples": {},
            "receivers": receivers,
            "matched_placed": {},  # fresh window: re-learn which placements match
            "all_placed_slugs": _all_placed_slugs(coords),
            "error": None,
        }
    )
    cal["task"] = hass.async_create_task(_sample_loop(hass, cal))
    return cal


async def _stop_task(cal: dict) -> None:
    task = cal.get("task")
    cal["task"] = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def async_cancel_calibration(hass) -> None:
    """Stop a run, whichever mode it is in.

    Mirrors the "cancel" action of async_calibration_action so the service layer does
    not have to reach for _stop_task, and so the two entry points can never
    drift on what cancelling means: turning auto off is a different operation
    from aborting a manual run, and only auto persists its state.
    """
    cal = get_calibration_state(hass)
    if cal["mode"] == "auto":
        await set_auto_calibration(hass, False)
        return
    await _stop_task(cal)
    cal["state"] = "idle"
    cal["mode"] = "off"
    cal["error"] = None


async def set_auto_calibration(hass, enabled: bool) -> None:
    """Enable/disable continuous calibration and persist the flag."""
    cal = get_calibration_state(hass)
    coords = await _read_coords(hass)
    if not coords:
        raise ValueError("No Sextant data saved yet.")

    if bool(coords.get("auto_calibration")) != bool(enabled):
        async with LAYOUT_LOCK:
            # Re-read inside the lock so a concurrent writer is not clobbered.
            coords = await _read_coords(hass) or coords
            coords["auto_calibration"] = bool(enabled)
            await save_layout(hass, coords)

    await _stop_task(cal)
    if enabled:
        # Deliberately keeps cal["samples"]: a restored or still-warm window
        # means the first solve after enabling has history to work with.
        cal.update(
            {
                "state": "sampling",
                "mode": "auto",
                "floor": None,
                "started_at": time.time(),
                "ends_at": None,
                "duration": None,
                "receivers": _build_receiver_map(coords),
                "matched_placed": {},  # fresh window: re-learn which placements match
                "all_placed_slugs": _all_placed_slugs(coords),
                "error": None,
            }
        )
        cal["task"] = hass.async_create_task(_auto_loop(hass, cal))
    else:
        cal["state"] = "idle"
        cal["mode"] = "off"


def refresh_receivers_from_coords(hass, coordinates_json) -> None:
    """Keep an ACTIVE calibration window tracking placements edited mid-run.

    Called by the panel-save endpoint right after bpsdata.txt is written: a
    re-linked, added, or removed receiver takes effect on the next dump
    instead of waiting for the next manual window or auto solve cycle (up to
    15 minutes of ingesting — and reporting missing — against stale slugs).
    """
    cal = hass.data.get(DOMAIN, {}).get("calibration")
    if not cal or cal.get("state") != "sampling":
        return
    try:
        coords = json.loads(coordinates_json) if coordinates_json else None
        if not isinstance(coords, dict):
            return
        floor = cal.get("floor") if cal.get("mode") == "manual" else None
        new_map = _build_receiver_map(coords, floor_name=floor)
        if floor is not None and not new_map:
            # The floor this manual window is calibrating vanished from the
            # save (deleted or de-scaled mid-run). Keep the start-of-run
            # snapshot rather than blinding the window — an empty map would
            # end the run with a misleading "0 usable pairs / check iBeacon"
            # error instead of a coherent (if now moot) report.
            return
        cal["receivers"] = new_map
        cal["all_placed_slugs"] = _all_placed_slugs(coords)
    except Exception as e:
        # A save must never fail because of calibration bookkeeping.
        _LOGGER.debug("Could not refresh calibration receivers from save: %s", e)


async def async_start_auto_if_enabled(hass) -> None:
    """Resume continuous calibration after a restart when the flag is set."""
    coords = await _read_coords(hass)
    if coords and coords.get("auto_calibration"):
        try:
            await set_auto_calibration(hass, True)
            _LOGGER.info("Auto-calibration resumed")
        except Exception as e:
            _LOGGER.warning("Could not resume auto-calibration: %s", e)


async def async_shutdown_calibration(hass) -> None:
    """Stop any running calibration task (integration unload/shutdown)."""
    state = hass.data.get(DOMAIN, {}).get("calibration")
    if state:
        await _stop_task(state)
        state["state"] = "idle"
        state["mode"] = "off"
        if state["samples"] or state["results"]:
            await save_calibration_state(hass)


PATH_LOSS_EXPONENT_DEFAULT = 3.0  # Bermuda's default attenuation, if it does not report one
APPLY_MIN_DB = 0.5  # smallest offset change worth writing into Bermuda


def _calibration_target(coords) -> str:
    """"sextant" (multiplier in the layout) or "bermuda" (rssi offset in Bermuda)."""
    tuning = coords.get("tuning") if isinstance(coords, dict) else None
    target = tuning.get("calibration_target") if isinstance(tuning, dict) else None
    if target == "bps":  # layouts saved before the rename
        target = "sextant"
    return target if target in ("sextant", "bermuda") else "sextant"


def _push_corrections_to_bermuda(hass, coords, floor, result) -> tuple[dict, set]:
    """Write one floor's solved corrections into Bermuda as rssi offsets.

    A per-receiver distance multiplier ``c`` is exactly an rssi offset of
    ``-10 * attenuation * log10(c)`` dB on the receiving scanner in Bermuda's
    path-loss model, so the correction moves into Bermuda without changing
    what the solver fitted. Offsets ACCUMULATE: the samples the next solve
    sees already carry the offsets written now, so that solve fits the
    residual, and the residual is added to what is there. Bermuda's own value
    before Sextant first touched a scanner is remembered per address
    (``bermuda_offset_base`` in the layout) so reset can restore it.

    Returns the number of scanners written. Raises ValueError when the
    Bermuda build cannot do this, so the caller can say why nothing changed.
    """
    info = bermuda_source.async_get_rssi_offsets(hass)
    slug_to_addr = bermuda_source.async_get_scanner_addresses_by_slug(hass)
    if info is None or slug_to_addr is None:
        raise ValueError(
            "This Bermuda build has no rssi_offsets API; set calibration_target to sextant "
            "(sextant.set_tuning) or update Bermuda."
        )
    attenuation = info.get("attenuation")
    if not isinstance(attenuation, (int, float)) or attenuation <= 0:
        attenuation = PATH_LOSS_EXPONENT_DEFAULT
    current = {str(k).lower(): float(v) for k, v in (info.get("offsets") or {}).items()}
    base = coords.setdefault("bermuda_offset_base", {})
    updates = {}
    pushed = set()
    for receiver in floor.get("receivers", []):
        if receiver.get("calibrate") is False:
            continue
        slug = str(receiver.get("entity_id") or "")
        correction = result["receivers"].get(slug)
        address = slug_to_addr.get(slug)
        if not address or not isinstance(correction, (int, float)) or correction <= 0:
            continue
        address = str(address).lower()
        delta_db = -10.0 * float(attenuation) * math.log10(float(correction))
        if abs(delta_db) < APPLY_MIN_DB:
            continue
        old = current.get(address, 0.0)
        base.setdefault(address, old)
        updates[address] = round(old + delta_db, 1)
        pushed.add(slug)
    return updates, pushed


def _commit_bermuda_offsets(hass, updates: dict, pushed: set) -> None:
    """Write planned offsets to Bermuda. Called AFTER the layout carrying
    their baseline is saved, so reset can always find the values to restore."""
    if not updates:
        return
    bermuda_source.async_set_rssi_offsets(hass, updates)
    # The windows these receivers heard into predate the new offsets, and
    # hold hours of samples: left in, the next solve fitted the SAME
    # residual again and added it a second time, every cycle, until the
    # window turned over. Start them over so it fits only what is left.
    samples = get_calibration_state(hass).get("samples") or {}
    for key in [k for k in samples if k.rpartition("|")[2] in pushed]:
        del samples[key]


async def _write_floor_corrections(hass, coords, floor, result, *, auto: bool) -> int:
    """Apply a solve result to one floor, per calibration_target, and save the
    layout. Returns the number of receivers (or Bermuda scanners) updated."""
    target = _calibration_target(coords)
    if target == "bermuda":
        updates, pushed = _push_corrections_to_bermuda(hass, coords, floor, result)
        updated = len(updates)
        # The correction now lives in Bermuda's distances; a multiplier here
        # would apply it twice. Except on a proxy taken out of calibration:
        # nothing was pushed for it, and its multiplier is the user's own.
        for receiver in floor.get("receivers", []):
            if receiver.get("calibrate") is not False:
                receiver.pop("correction", None)
    else:
        updated = 0
        for receiver in floor.get("receivers", []):
            if receiver.get("calibrate") is False:
                continue           # its correction is the user's, not ours
            correction = result["receivers"].get(str(receiver.get("entity_id")))
            if correction is not None:
                receiver["correction"] = correction
                updated += 1
    floor["calibration"] = {
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "auto": auto,
        "target": target,
        "pairs_used": result["pairs_used"],
        "error_factor_before": result["error_factor_before"],
        "error_factor_after": result["error_factor_after"],
    }
    # Saved before Bermuda is touched: a push whose baseline never reached
    # disk (auto turned off mid-loop, a later step raising) left offsets in
    # Bermuda that reset could no longer undo.
    await save_layout(hass, coords)
    if target == "bermuda":
        _commit_bermuda_offsets(hass, updates, pushed)
    return updated


async def apply_corrections(hass, cal: dict, floor_name: str) -> int:
    """Write the solved corrections into bpsdata.txt. Returns receivers updated."""
    result = None
    for name, res in cal["results"].items():
        if _normalize(name) == _normalize(floor_name):
            result = res
            break
    if not result:
        raise ValueError("No calibration result to apply for this floor.")
    async with LAYOUT_LOCK:
        return await _apply_result_locked(hass, cal, result)


async def _apply_result_locked(hass, cal: dict, result: dict) -> int:
    coords = await _read_coords(hass)
    if not coords:
        raise ValueError("No Sextant data saved.")
    floor = _find_floor(coords, result["floor"])
    if floor is None:
        raise ValueError(f'Floor "{result["floor"]}" no longer exists.')

    updated = await _write_floor_corrections(hass, coords, floor, result, auto=False)
    cal["applied"][floor.get("name")] = dict(result["receivers"])
    return updated


async def reset_corrections(hass, cal: dict, floor_name: str) -> int:
    async with LAYOUT_LOCK:
        coords = await _read_coords(hass)
        if not coords:
            raise ValueError("No Sextant data saved.")
        floor = _find_floor(coords, floor_name)
        if floor is None:
            raise ValueError(f'No floor named "{floor_name}".')

        removed = 0
        for receiver in floor.get("receivers", []):
            if receiver.pop("correction", None) is not None:
                removed += 1
        # Offsets Sextant wrote into Bermuda for this floor's scanners go back to
        # what Bermuda had before Sextant first touched them (whatever the target
        # is set to now).
        base = coords.get("bermuda_offset_base")
        slug_to_addr = bermuda_source.async_get_scanner_addresses_by_slug(hass) or {}
        if isinstance(base, dict) and base:
            restore = {}
            for receiver in floor.get("receivers", []):
                address = slug_to_addr.get(str(receiver.get("entity_id") or ""))
                if address and str(address).lower() in base:
                    restore[str(address).lower()] = base.pop(str(address).lower())
            if restore:
                bermuda_source.async_set_rssi_offsets(hass, restore)
                removed += len(restore)
            if not base:
                coords.pop("bermuda_offset_base", None)
        floor.pop("calibration", None)
        cal["applied"].pop(floor.get("name"), None)

        await save_layout(hass, coords)
        return removed


def _status_payload(cal: dict) -> dict:
    pair_counts = {key: len(values) for key, values in cal["samples"].items()}
    payload = {
        "state": cal["state"],
        "mode": cal["mode"],
        "floor": cal["floor"],
        "error": cal["error"],
        "results": cal["results"],
        "last_solved_at": cal["last_solved_at"],
        # When the current window started (auto: since it was switched on or
        # resumed at start-up), so the panel can say how long it has sampled
        # and when the first automatic solve is due (AUTO_MIN_WINDOW after).
        "started_at": cal["started_at"],
        "first_solve_after": AUTO_MIN_WINDOW,
        "auto_decisions": cal.get("auto_decisions") or {},
        # "ranging" (the fork's scanner-ranging table) or "dump" (stock
        # Bermuda's dump_devices service); None before the first round.
        "sample_source": cal.get("sample_source"),
        "pair_counts": pair_counts,
        "receiver_count": len(cal["receivers"]),
    }
    if cal["mode"] == "manual" and cal["state"] == "sampling" and cal["ends_at"]:
        payload["seconds_left"] = max(0, int(cal["ends_at"] - time.time()))
        payload["duration"] = cal["duration"]
    return payload


async def async_calibration_action(hass, data: dict) -> dict:
    """Run one calibration action ("start", "auto", "cancel", "solve", "apply",
    "reset") and return the status payload. Raises ValueError with a message
    for the user on a bad request or a failed solve."""
    cal = get_calibration_state(hass)
    action = data.get("action")
    if action == "start":
        await start_calibration(hass, data.get("floor"), data.get("duration"))
    elif action == "auto":
        await set_auto_calibration(hass, bool(data.get("enabled")))
    elif action == "cancel":
        if cal["mode"] == "auto":
            await set_auto_calibration(hass, False)
        else:
            await _stop_task(cal)
            cal["state"] = "idle"
            cal["mode"] = "off"
            cal["error"] = None
    elif action == "solve":
        floor_name = data.get("floor") or cal.get("floor")
        if cal["state"] != "sampling":
            # Outside a run the map is whatever the last one left: empty after
            # a restart, or another floor's after a manual run there.
            coords = await _read_coords(hass)
            if coords:
                cal["receivers"] = _build_receiver_map(coords)
                cal["all_placed_slugs"] = _all_placed_slugs(coords)
        result = await async_solve(hass, cal, floor_name)
        coords = await _read_coords(hass)
        floor = _find_floor(coords, result["floor"]) if coords else None
        if floor is not None and _calibration_target(coords) != "bermuda":
            try:
                result["selftest"] = await _judge_by_selftest(hass, cal, coords, floor, result)
            except Exception as e:  # noqa: BLE001 - a verdict is advice, never a reason to lose the solve
                _LOGGER.debug("Self-test verdict unavailable: %s", e)
        cal["results"][result["floor"]] = result
        cal["last_solved_at"] = result["solved_at"]
        cal["error"] = None
    elif action == "apply":
        updated = await apply_corrections(hass, cal, data.get("floor") or cal.get("floor"))
        await save_calibration_state(hass)
        return {"applied": updated, **_status_payload(cal)}
    elif action == "reset":
        removed = await reset_corrections(hass, cal, data.get("floor") or cal.get("floor"))
        await save_calibration_state(hass)
        return {"reset": removed, **_status_payload(cal)}
    else:
        raise ValueError(f"Unknown action {action!r}")
    return _status_payload(cal)


