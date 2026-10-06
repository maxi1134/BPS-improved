#!/usr/bin/env python3
"""
Room-stability KPI for Sextant: how often the published zone/floor sensors change.

Positional error in metres is hard to measure on a live install (there is no
ground truth for a phone in a pocket), but the thing automations actually
suffer from is easy to measure: a thing that is not moving must not change
room. This reads the Home Assistant recorder history for every ``_sextant_zone``
and ``_sextant_floor`` sensor and reports, per sensor:

  changes/h      state changes per thing-hour (lower is better)
  flip%          share of changes that are an A -> B -> A round trip within
                 the next change - the signature of boundary flapping
  median dwell   median seconds a state was held before changing
  <60s%          share of dwells shorter than a minute
  unknown        how many times the sensor went unknown/unavailable

Run it BEFORE and AFTER a positioning change on the same window length, and
compare with ``--baseline``. That is the number every accuracy change should
move; positional CEP from ``sextant_eval.py`` is the second opinion.

Usage::

    export HASS_URL=https://home.example   HASS_TOKEN=...   # long-lived token
    python tools/flap_kpi.py --hours 12                     # live query
    python tools/flap_kpi.py --hours 12 --json before.json  # keep for later
    python tools/flap_kpi.py --hours 12 --baseline before.json

    python tools/flap_kpi.py --from-json before.json        # re-print a saved run

Only the standard library is used, so it runs anywhere Python 3.9+ does.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

SUFFIXES = ("_sextant_room", "_sextant_floor")
# History and baselines recorded under the older names are the same sensors.
LEGACY_SUFFIXES = {"_bps_zone": "_sextant_room", "_sextant_zone": "_sextant_room", "_bps_floor": "_sextant_floor"}


def canonical(entity_id):
    for old, new in LEGACY_SUFFIXES.items():
        if entity_id.endswith(old):
            return entity_id[: -len(old)] + new
    return entity_id
DEAD_STATES = {"unknown", "unavailable", "", None}


# --- metrics (pure; unit-tested) ------------------------------------------- #


def _parse_ts(value):
    """ISO-8601 (HA's format, with offset) -> aware datetime."""
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def compute_metrics(rows, window_hours=None):
    """
    Stability metrics for one entity's history.

    ``rows`` is a list of ``{"state": str, "last_changed": iso}`` in
    chronological order, as HA's history API returns for one entity (the
    first row is the state at the window start). Consecutive duplicate
    states are collapsed first, so a row that only re-reported the same
    value does not count as a change.

    ``window_hours`` normalises the change rate; when omitted the span of the
    rows themselves is used, which under-counts for a thing that was quiet
    at either end of the window.
    """
    seq = []
    for row in rows:
        state = row.get("state")
        ts = _parse_ts(row.get("last_changed") or row.get("last_updated"))
        if seq and seq[-1][1] == state:
            continue
        seq.append((ts, state))

    if window_hours:
        hours = float(window_hours)
    elif len(seq) >= 2:
        hours = max((seq[-1][0] - seq[0][0]).total_seconds() / 3600.0, 1e-9)
    else:
        hours = 0.0

    # A pass through unavailable or unknown is the sensor going away and coming
    # back - a restart, a reload - and says nothing about where the thing is.
    # Those rows go before anything is counted: Kitchen, unavailable, Kitchen is
    # no change at all, and Kitchen, unavailable, Office is the one change it
    # always was. Counted as they were, one restart added two changes to every
    # sensor, and a day with three restarts read as a fleet-wide flap.
    dead = sum(1 for _ts, s in seq if s in DEAD_STATES)
    live = []
    for ts, s in seq:
        if s in DEAD_STATES or (live and live[-1][1] == s):
            continue
        live.append((ts, s))
    seq = live
    changes = max(len(seq) - 1, 0)

    # A -> B -> A within the next change: the boundary-flap signature.
    flips = sum(
        1
        for i in range(len(seq) - 2)
        if seq[i][1] == seq[i + 2][1] and seq[i][1] != seq[i + 1][1]
    )
    dwells = [(seq[i + 1][0] - seq[i][0]).total_seconds() for i in range(len(seq) - 1)]
    short = sum(1 for d in dwells if d < 60.0)

    transitions = Counter((seq[i][1], seq[i + 1][1]) for i in range(len(seq) - 1))
    # Fold A->B and B->A together: the pair is what flaps, not the direction.
    pairs = Counter()
    for (a, b), n in transitions.items():
        pairs[tuple(sorted((a, b)))] += n

    return {
        "changes": changes,
        "hours": round(hours, 3),
        "changes_per_hour": round(changes / hours, 2) if hours else None,
        "flips": flips,
        "flip_ratio": round(flips / changes, 3) if changes else None,
        "median_dwell_s": round(statistics.median(dwells), 1) if dwells else None,
        "short_dwell_ratio": round(short / len(dwells), 3) if dwells else None,
        "dead": dead,
        "states": len({s for _ts, s in seq if s not in DEAD_STATES}),
        "top_pairs": [
            {"pair": list(pair), "count": n} for pair, n in pairs.most_common(3)
        ],
        # Far moves need the plan's room outlines, which the panel has and this
        # tool does not; kept as None so the two outputs stay the same shape.
        "far_moves": None,
        "far_moves_per_day": None,
        "far_move_ratio": None,
    }


def summarise(per_entity):
    """Fleet-level roll-up: totals over every entity of one kind."""
    out = {}
    for suffix in SUFFIXES:
        members = {canonical(k): v for k, v in per_entity.items() if canonical(k).endswith(suffix)}
        if not members:
            continue
        changes = sum(m["changes"] for m in members.values())
        flips = sum(m["flips"] for m in members.values())
        hours = sum(m["hours"] for m in members.values())
        dwells = [m["median_dwell_s"] for m in members.values() if m["median_dwell_s"] is not None]
        out[suffix.lstrip("_")] = {
            "entities": len(members),
            "changes": changes,
            "changes_per_thing_hour": round(changes / hours, 2) if hours else None,
            "flip_ratio": round(flips / changes, 3) if changes else None,
            "median_of_median_dwell_s": round(statistics.median(dwells), 1) if dwells else None,
            "far_moves": None,
            "far_moves_per_thing_day": None,
        }
    return out


# --- Home Assistant REST access ------------------------------------------- #


def _get(url, token, path, params=None):
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(
        url.rstrip("/") + path + query,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - user-supplied HA URL
        return json.load(resp)


def discover_entities(url, token, include_nearest=False):
    suffixes = SUFFIXES + tuple(LEGACY_SUFFIXES) + (("_sextant_nearest_room", "_sextant_nearest_zone") if include_nearest else ())
    states = _get(url, token, "/api/states")
    return sorted(s["entity_id"] for s in states if s["entity_id"].endswith(suffixes))


def fetch_history(url, token, entity_ids, hours, end=None):
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    data = _get(
        url,
        token,
        f"/api/history/period/{start.isoformat()}",
        {
            "filter_entity_id": ",".join(entity_ids),
            "minimal_response": "",
            "no_attributes": "",
            "significant_changes_only": "",
            **({"end_time": end.isoformat()} if end is not None else {}),
        },
    )
    # One list per entity; minimal_response omits entity_id on all but the
    # first row of each list.
    out = {}
    for rows in data:
        if not rows:
            continue
        eid = rows[0].get("entity_id")
        if eid:
            out[eid] = rows
    return out


# --- output ---------------------------------------------------------------- #


def _fmt(value, width, suffix=""):
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        return f"{value:.2f}{suffix}".rjust(width)
    return f"{value}{suffix}".rjust(width)


def _baseline_entry(base_entities, eid):
    """The baseline's row for this sensor, across the bps -> sextant rename."""
    seen = {eid}
    for a, b in (("_sextant_room", "_sextant_zone"), ("_sextant_", "_bps_")):
        for c in list(seen):
            seen.add(c.replace(a, b)); seen.add(c.replace(b, a))
    for candidate in seen:
        if candidate in base_entities:
            return base_entities[candidate]
    return None


def print_report(per_entity, summary, baseline=None):
    base_entities = (baseline or {}).get("entities", {})
    head = f"{'entity':<52}{'chg/h':>8}{'flip%':>8}{'dwell':>10}{'<60s%':>8}{'dead':>6}"
    if baseline:
        head += f"{'Δchg/h':>9}{'Δflip%':>9}"
    print(head)
    print("-" * len(head))
    for eid in sorted(per_entity):
        m = per_entity[eid]
        line = (
            f"{eid:<52}"
            + _fmt(m["changes_per_hour"], 8)
            + _fmt(None if m["flip_ratio"] is None else m["flip_ratio"] * 100, 8)
            + _fmt(m["median_dwell_s"], 10)
            + _fmt(None if m["short_dwell_ratio"] is None else m["short_dwell_ratio"] * 100, 8)
            + _fmt(m["dead"], 6)
        )
        if baseline:
            b = _baseline_entry(base_entities, eid)
            if b and b.get("changes_per_hour") is not None and m["changes_per_hour"] is not None:
                line += _fmt(m["changes_per_hour"] - b["changes_per_hour"], 9)
            else:
                line += _fmt(None, 9)
            if b and b.get("flip_ratio") is not None and m["flip_ratio"] is not None:
                line += _fmt((m["flip_ratio"] - b["flip_ratio"]) * 100, 9)
            else:
                line += _fmt(None, 9)
        print(line)
        for p in m["top_pairs"][:2]:
            print(f"{'':<8}{p['pair'][0]} <-> {p['pair'][1]}: {p['count']}")
    print()
    for kind, s in summary.items():
        line = (
            f"{kind:<12} {s['entities']} sensors, {s['changes']} changes, "
            f"{s['changes_per_thing_hour']} per thing-hour, "
            f"flip ratio {s['flip_ratio']}, median dwell {s['median_of_median_dwell_s']} s"
        )
        base_summary = {canonical("_" + k).lstrip("_"): v for k, v in ((baseline or {}).get("summary") or {}).items()}
        if kind in base_summary:
            b = base_summary[kind]
            if b.get("changes_per_thing_hour") is not None and s["changes_per_thing_hour"] is not None:
                line += f"   (was {b['changes_per_thing_hour']}/h, flip {b.get('flip_ratio')})"
        print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("HASS_URL"), help="HA base URL (or $HASS_URL)")
    ap.add_argument("--token", default=os.environ.get("HASS_TOKEN"), help="long-lived token (or $HASS_TOKEN)")
    ap.add_argument("--hours", type=float, default=12.0, help="window to score (default 12)")
    ap.add_argument("--entities", nargs="*", help="explicit entity ids (default: every *_sextant_zone / *_sextant_floor)")
    ap.add_argument("--nearest", action="store_true", help="also score *_sextant_nearest_zone")
    ap.add_argument("--json", metavar="FILE", help="write the metrics to FILE")
    ap.add_argument("--baseline", metavar="FILE", help="compare against a run saved with --json")
    ap.add_argument("--from-json", metavar="FILE", help="re-print a saved run instead of querying HA")
    ap.add_argument("--end", metavar="ISO", help="score the window ending at this time instead of now "
                    "(e.g. 2026-09-17T04:55:00-04:00); needs --entities when those sensors no longer exist")
    ap.add_argument("--history-json", metavar="FILE",
                    help="score a saved /api/history/period response (a list of per-entity state lists) "
                         "instead of querying HA; pairs with --hours for the window length")
    args = ap.parse_args(argv)

    baseline = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as fh:
            baseline = json.load(fh)

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            saved = json.load(fh)
        print_report(saved["entities"], saved["summary"], baseline)
        return 0

    end = None
    if args.end:
        end = datetime.fromisoformat(args.end)
        if end.tzinfo is None:
            end = end.astimezone()

    if args.history_json:
        with open(args.history_json, encoding="utf-8") as fh:
            raw = json.load(fh)
        history = {}
        for states in raw:
            if states:
                history[states[0]["entity_id"]] = states
        entity_ids = args.entities or sorted(history)
    else:
        if not args.url or not args.token:
            ap.error("need --url/--token or $HASS_URL/$HASS_TOKEN (or --from-json / --history-json)")
    try:
        if not args.history_json:
            entity_ids = args.entities or discover_entities(args.url, args.token, args.nearest)
            if not entity_ids:
                print("no Sextant zone/floor sensors found", file=sys.stderr)
                return 1
            history = fetch_history(args.url, args.token, entity_ids, args.hours, end=end)
    except urllib.error.HTTPError as e:
        print(f"HA request failed: {e.code} {e.reason}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"HA unreachable: {e.reason}", file=sys.stderr)
        return 2

    per_entity = {eid: compute_metrics(history.get(eid, []), args.hours) for eid in entity_ids}
    summary = summarise(per_entity)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": args.hours,
        "entities": per_entity,
        "summary": summary,
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    print_report(per_entity, summary, baseline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
