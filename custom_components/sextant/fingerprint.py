"""Fingerprint positioning with the receivers as free reference points.

Every receiver that advertises (an ESPHome iBeacon, a Shelly) is heard by
every other receiver, so Bermuda continuously measures a labelled vector of
ranges at a known position - one per placed receiver, refreshed for free.
A thing's own vector of ranges can be matched against those reference
vectors by similarity, and the thing placed at a weighted average of the
best-matching receivers' positions. Nothing here uses a path-loss model to
turn a range into geometry: a wall that makes receiver X read a thing
long makes X read the reference receivers behind that wall long too, and
the comparison cancels it. That is what ESPresense Companion and
room-assistant lean on, and it is what a dense receiver layout is good at.

The trilateration keeps its job; this module produces a second opinion
(a fix and a per-floor confidence) that __init__ fuses with the geometric
fit according to the ``position_estimator`` tuning. It can also place a
thing that only ONE receiver hears, where trilateration has nothing.

All functions are pure so the match can run in the executor.
"""

from __future__ import annotations

import math
from collections import deque

# A receiver does not hear itself, but a thing standing at a receiver
# hears it very close: the reference vector gets this self-range.
SELF_DISTANCE_M = 0.5
# Reference ranges are medians over this many refreshes: the receivers do
# not move, so smoothing hard costs nothing and takes Bermuda's per-cycle
# noise out of the database.
REF_SAMPLES = 12
# Pairs not heard within this many seconds are not sampled.
REF_MAX_AGE_SECS = 60.0
# Log-ratio at which a single receiver's disagreement costs half the
# confidence: ln(2) = the reference and the thing differ by a factor 2.
SCORE_SCALE = math.log(2.0)
# References scoring worse than this multiple of the best are not averaged
# in, even inside the k cut - a poor third neighbour would drag the fix.
NEIGHBOUR_SCORE_RATIO = 3.0

# Auto-gain: each accepted match moves the learned reference gain by
# ratio ** (LEARN_ALPHA * conf); at one match per thing per cycle, a
# household of things walks a factor-of-two error off in a few minutes
# and then hovers, never runs away (clamped).
LEARN_ALPHA = 0.02
LEARNED_GAIN_MIN = 0.25
LEARNED_GAIN_MAX = 4.0
# Every radio reads differently (a watch weak, a Tile hot): each thing also learns its own
# multiplier on the shared gain, faster, since it only ever affects that thing.
THING_LEARN_ALPHA = 0.1
# A match whose thing/reference range ratio is this far from 1 (a factor of three) says nothing
# about where the thing is; the fusion weight falls linearly to zero there.
TRUST_SCALE = math.log(3.0)
MIN_SHARED_FOR_RATIO = 3   # receivers both vectors need before a ratio is trusted


class ReferenceDB:
    """Rolling per-pair ranges between receivers, from Bermuda's scanner ranging."""

    def __init__(self, samples=REF_SAMPLES):
        self._samples = {}  # (tx_address, rx_address) -> deque of raw metres
        # vectors() is asked for by every thing on every cycle, but the samples
        # only change on ingest(), which runs every FINGERPRINT_REFRESH_SECS.
        # Recomputing a median per scanner pair each time was ~180,000 medians
        # a minute for answers that had not changed.
        self._vectors = None
        self._maxlen = samples
        self.stamp = None
        # Multiplies the configured reference gain (see learn()).
        self.learned_gain = 1.0
        # Per-thing multiplier on top of learned_gain (see learn(entity=...)).
        self.thing_gain = {}

    def gain_for(self, entity=None):
        """The learned gain for one thing: the shared gain times its own multiplier."""
        return self.learned_gain * self.thing_gain.get(entity, 1.0)

    def learn(self, ratio, conf=1.0, alpha=LEARN_ALPHA, entity=None):
        """Fold one match's thing/reference range ratio into the learned gain.

        ``ratio`` > 1 means the thing reads farther than the reference the
        matcher paired it with, i.e. the references are built too short and
        the gain should rise. A thing is rarely exactly at a reference, so
        single ratios scatter either side of the truth; the exponent is
        small and scaled by the match confidence so only the average moves
        the gain. Returns the new gain.
        """
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not ratio > 0 or not math.isfinite(ratio):
            return self.learned_gain
        weight = max(0.0, min(1.0, float(conf)))
        self.learned_gain = min(LEARNED_GAIN_MAX, max(LEARNED_GAIN_MIN, self.learned_gain * ratio ** (alpha * weight)))
        if entity is not None:
            own = self.thing_gain.get(entity, 1.0) * ratio ** (THING_LEARN_ALPHA * weight)
            self.thing_gain[entity] = min(LEARNED_GAIN_MAX, max(LEARNED_GAIN_MIN, own))
        return self.learned_gain

    def ingest(self, ranging, max_age=REF_MAX_AGE_SECS):
        """Fold one ``async_get_scanner_ranging`` payload into the medians."""
        if not isinstance(ranging, dict):
            return
        scanners = ranging.get("scanners")
        if not isinstance(scanners, dict):
            return
        for tx, heard in scanners.items():
            if not isinstance(heard, dict):
                continue
            for rx, reading in heard.items():
                if not isinstance(reading, dict):
                    continue
                age = reading.get("age")
                if age is not None and age > max_age:
                    continue
                # The raw range: Bermuda's filtered one is biased toward the
                # minimum, and the median below does the smoothing anyway.
                d = reading.get("distance_raw")
                if d is None:
                    d = reading.get("distance")
                if not isinstance(d, (int, float)) or isinstance(d, bool) or not d > 0 or not math.isfinite(d):
                    continue
                key = (str(tx).lower(), str(rx).lower())
                dq = self._samples.get(key)
                if dq is None:
                    dq = self._samples[key] = deque(maxlen=self._maxlen)
                dq.append(float(d))
        self.stamp = ranging.get("stamp")
        self._vectors = None

    def vectors(self):
        """{tx_address: {rx_address: median metres}} for every sampled pair.

        Cached until the next ingest. Callers get the shared dict and must
        treat it as read-only, which build_references already does.
        """
        if self._vectors is None:
            out = {}
            for (tx, rx), dq in self._samples.items():
                if not dq:
                    continue
                out.setdefault(tx, {})[rx] = _median(dq)
            self._vectors = out
        return self._vectors

    def pairs(self):
        return sum(1 for dq in self._samples.values() if dq)


def trust(ratio):
    """How much a match with this thing/reference range ratio should weigh: 1 at a ratio of 1,
    falling to 0 at a factor of TRUST_SCALE either way. None (no ratio) is trusted in full."""
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not ratio > 0 or not math.isfinite(ratio):
        return 1.0
    return max(0.0, 1.0 - abs(math.log(ratio)) / TRUST_SCALE)


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])


def build_references(layout, vectors, gain=1.0, extra=None):
    """Per floor, the reference points with their fingerprint vectors.

    A placed receiver becomes a reference when it carries a scanner address
    and at least one other placed receiver has ranged it. The vector is
    keyed by receiving scanner address and holds metres on the same scale
    the thing's readings use: Bermuda's estimate, times that receiver's
    calibration correction (exactly what update_receiver_radii applies to a
    thing's reading from the same receiver), times ``gain`` - the knob for
    a probe beacon that transmits hotter or cooler than the things do.
    The receiver's own entry is SELF_DISTANCE_M. ``extra`` adds references
    that are not receivers (truth marks: a known point with the vector a
    thing read there, stored in probe scale), scaled by the same gain.
    """
    floors = (layout or {}).get("floor") if isinstance(layout, dict) else None
    if not floors or not isinstance(vectors, dict):
        return {}
    # Every placed receiver's correction, by address, across all floors: a
    # reference on the ground floor is ranged by upstairs receivers too.
    correction_by_address = {}
    for floor in floors:
        for receiver in floor.get("receivers", []) or []:
            address = receiver.get("address")
            if not isinstance(address, str) or not address:
                continue
            corr = receiver.get("correction")
            correction_by_address[address.lower()] = (
                float(corr) if isinstance(corr, (int, float)) and not isinstance(corr, bool) and corr > 0 else 1.0
            )
    refs_by_floor = {}
    for floor in floors:
        if not floor.get("scale"):
            continue
        refs = []
        for receiver in floor.get("receivers", []) or []:
            address = receiver.get("address")
            cords = receiver.get("cords") or {}
            if not isinstance(address, str) or not address or cords.get("x") is None or cords.get("y") is None:
                continue
            address = address.lower()
            heard = vectors.get(address) or {}
            vector = {}
            for rx, d in heard.items():
                if rx in correction_by_address and rx != address:
                    vector[rx] = d * correction_by_address[rx] * gain
            if not vector:
                continue
            vector[address] = SELF_DISTANCE_M
            refs.append({
                "slug": receiver.get("entity_id"),
                "address": address,
                "x": float(cords["x"]),
                "y": float(cords["y"]),
                "vector": vector,
            })
        for ref in extra or []:
            if ref.get("floor") != floor["name"] or not ref.get("vector"):
                continue
            refs.append({
                "slug": ref.get("slug"), "address": None, "x": float(ref["x"]), "y": float(ref["y"]),
                "vector": {rx: d * gain for rx, d in ref["vector"].items()},
            })
        if refs:
            refs_by_floor[floor["name"]] = refs
    return refs_by_floor


def thing_vector(layout):
    """{rx_address: metres} from this cycle's per-receiver readings, all floors."""
    vector = {}
    floors = (layout or {}).get("floor") if isinstance(layout, dict) else None
    for floor in floors or []:
        for receiver in floor.get("receivers", []) or []:
            address = receiver.get("address")
            d = receiver.get("distance")
            if not isinstance(address, str) or not address:
                continue
            if not isinstance(d, (int, float)) or isinstance(d, bool) or not d > 0 or not math.isfinite(d):
                continue
            vector[address.lower()] = float(d)
    return vector


def similarity(thing, reference, missing_m):
    """Weighted RMS log-ratio between two range vectors; lower is closer.

    Over the union of receivers: a receiver hearing only one of the two
    counts as hearing the other at ``missing_m`` (out of range is
    information - a receiver hearing the thing at 3 m but not the
    reference says the reference is far from it). Near receivers weigh
    more: they carry the geometry, far ones mostly carry the noise. None
    when there is nothing to compare.
    """
    keys = set(thing) | set(reference)
    if not keys:
        return None
    num = den = 0.0
    for rx in keys:
        dt = thing.get(rx)
        dr = reference.get(rx)
        if dt is None and dr is None:
            continue
        if dt is None:
            dt = max(missing_m, dr)
        elif dr is None:
            dr = max(missing_m, dt)
        e = math.log(dt / dr)
        w = 1.0 / (1.0 + min(dt, dr))
        num += w * e * e
        den += w
    if den <= 0:
        return None
    return math.sqrt(num / den)


def match(thing, refs, k=3, missing_m=12.0):
    """Place a thing vector against one floor's references.

    Returns None when nothing can be compared, else
    {"x", "y", "conf", "score", "refs": [(slug, score), ...]} where the fix
    is the inverse-score-weighted mean of the best ``k`` references (within
    NEIGHBOUR_SCORE_RATIO of the best) and ``conf`` in (0, 1] falls with
    the best score: 0.5 when the closest reference still disagrees with the
    thing by a factor of two on average.
    """
    if not thing or not refs:
        return None
    scored = []
    for ref in refs:
        s = similarity(thing, ref["vector"], missing_m)
        if s is not None:
            scored.append((s, ref))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    best = scored[0][0]
    chosen = [(s, r) for s, r in scored[: max(1, int(k))] if s <= best * NEIGHBOUR_SCORE_RATIO + 1e-9]
    wsum = x = y = 0.0
    for s, r in chosen:
        w = 1.0 / (s + 0.05) ** 2
        wsum += w
        x += w * r["x"]
        y += w * r["y"]
    conf = 1.0 / (1.0 + (best / SCORE_SCALE) ** 2)
    # Gain evidence: over the receivers that heard BOTH the thing and the
    # best reference (never the reference's own self entry), the median of
    # thing / reference. Independent of the score's weighting on purpose.
    best_ref = scored[0][1]
    logs = [
        math.log(thing[rx] / best_ref["vector"][rx])
        for rx in thing
        if rx in best_ref["vector"] and rx != best_ref.get("address") and best_ref["vector"][rx] > 0
    ]
    ratio = math.exp(_median(logs)) if len(logs) >= MIN_SHARED_FOR_RATIO else None
    return {
        "x": x / wsum,
        "y": y / wsum,
        "conf": conf,
        "score": best,
        "refs": [(r.get("slug"), round(s, 3)) for s, r in chosen],
        "ratio": ratio,
        "shared": len(logs),
    }
