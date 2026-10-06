#!/usr/bin/env python3
"""
Replay every thing's floor election over logged cycles, under other settings.

Sextant's floor election is a pipeline: each contending floor's fit
confidence is scaled by proximity, biased, folded into smoothed odds, and the
incumbent only loses to a challenger that leads by a margin for a dwell. The
knobs are on the Tuning page, and the question is always the same - would a
different setting have held Eilee's phone on her floor overnight without
freezing the cats on the wrong one. This answers it from cycles that really
happened rather than from a guess.

Input is the election log Sextant keeps when ``election_log_hours`` is set
(the ``config/sextant_election_log`` directory, or one of its files) or a
capture from tools/floor_capture.py: one JSON object per thing per cycle with
the elected ``floor``, the smoothed ``floors`` and the ``floor_cands`` block.

Validation first. With the live rules the replay must reproduce the floors
that were published; the "as logged" row says how closely, and only then does
a variant mean anything. Each variant then reports how often it agrees with
what was published, how many floor changes it makes and how many of those are
flips (A -> B -> A), per thing.

Usage::

    python tools/replay_floors.py config/sextant_election_log --layout layout.json
    python tools/replay_floors.py capture.jsonl --hours 12 --thing eilee_phone

    # grade a thing against where it really was (a still phone overnight):
    python tools/replay_floors.py log/ --truth private_ble_device_eilee_phone="Second Floor" \\
        --since "2026-09-25 00:00" --until "2026-09-25 07:00"

    # the proximity term is re-scored from each floor's nearest proxy, so
    # floor_proximity_weight and floor_proximity_blend can be replayed as
    # well as the margin and dwell:
    python tools/replay_floors.py log/ --weight 0.7 --formula geometric --margin 0.1

``--layout`` supplies the tuning the log was made under (the layout file, or a
snapshot from config/sextant_snapshots); without it the defaults apply.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time
from datetime import datetime

DEFAULTS = {
    "floor_prob_smoothing": 0.7, "floor_switch_secs": 60.0, "floor_switch_margin": 0.05,
    "floor_tenure_bonus": 0.05, "floor_tenure_full_secs": 600.0, "floor_proximity_weight": 0.5,
    "stationary_speed": 0.3,
}


def load_rows(path, hours=None):
    """Every record from a file or a directory of .jsonl files, oldest first."""
    files = sorted(glob.glob(os.path.join(path, "*.jsonl"))) if os.path.isdir(path) else [path]
    rows = []
    for name in files:
        with open(name, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    rows = [r for r in rows if isinstance(r, dict) and r.get("ent") and r.get("t") is not None]
    rows.sort(key=lambda r: r["t"])
    if hours and rows:
        cut = rows[-1]["t"] - hours * 3600
        rows = [r for r in rows if r["t"] >= cut]
    return rows


def rescore(cands, weight, formula="gated"):
    """Each floor's score with the proximity term rebuilt at ``weight``.

    The log carries each floor's fit confidence, its proximity-weighted score
    and its bias. The live rule is score = conf * ((1 - w) + w * prox) * bias
    with prox the nearest proxy on any floor over this floor's nearest, both
    read off the ``heard`` lists here; that is floor_proximity_blend
    "gated". "geometric" is the other blend, conf^(1-w) * prox^w * bias,
    where a low fit confidence hurts a floor less than a far nearest proxy
    does. With ``weight`` None the logged scores are used as they are.
    """
    if weight is None:
        return {f: (c or {}).get("score") or 0.0 for f, c in cands.items()}
    nearest = {}
    for f, c in cands.items():
        heard = [h[1] for h in ((c or {}).get("heard") or []) if isinstance(h, (list, tuple)) and len(h) > 1 and isinstance(h[1], (int, float)) and h[1] > 0]
        if heard:
            nearest[f] = min(heard)
    best = min(nearest.values()) if len(nearest) >= 2 else None
    out = {}
    for f, c in cands.items():
        c = c or {}
        conf, bias = c.get("conf") or 0.0, c.get("bias") if isinstance(c.get("bias"), (int, float)) else 1.0
        prox = (best / nearest[f]) if best is not None and f in nearest else 1.0
        if formula == "geometric":
            out[f] = (max(conf, 0.0) ** (1.0 - weight)) * (prox ** weight) * bias
        else:
            out[f] = conf * ((1.0 - weight) + weight * prox) * bias
    return out


def replay(seq, tuning, switch=None, margin=None, tenure_bonus=None, weight=None, formula="gated", still_hold=None, still_margin=None):
    """The floor that would have been published each cycle.

    Seeded from the first logged cycle: the live election had an incumbent
    with tenure when the log began, and a cold start would replay its first
    minutes wrongly. ``still_hold`` / ``still_margin`` are an extra dwell and
    margin for a thing slower than stationary_speed.
    """
    smooth = float(tuning["floor_prob_smoothing"])
    switch = float(tuning["floor_switch_secs"]) if switch is None else switch
    margin = float(tuning["floor_switch_margin"]) if margin is None else margin
    tenure_bonus = float(tuning["floor_tenure_bonus"]) if tenure_bonus is None else tenure_bonus
    tenure_full, still_speed = float(tuning["floor_tenure_full_secs"]), float(tuning["stationary_speed"])
    probs = dict(seq[0].get("floors") or {})
    incumbent, challenge, since = seq[0].get("floor"), None, seq[0]["t"]
    out = []
    for r in seq:
        scores = rescore(r.get("floor_cands") or {}, weight, formula)
        if not scores:
            out.append(incumbent)
            continue
        total = sum(scores.values()) or 1.0
        for f in set(probs) | set(scores):
            probs[f] = smooth * probs.get(f, 0.0) + (1 - smooth) * scores.get(f, 0.0) / total
        for f in [f for f, p in probs.items() if p < 0.01]:
            del probs[f]
        norm = sum(probs.values()) or 1.0
        p = {f: v / norm for f, v in probs.items()}
        contenders = {f: v for f, v in p.items() if f in scores}
        if not contenders:
            out.append(incumbent)
            continue
        best = max(contenders, key=contenders.get)
        if incumbent is None or incumbent not in contenders:
            incumbent, challenge, since = best, None, r["t"]
        else:
            still = isinstance(r.get("speed"), (int, float)) and r["speed"] < still_speed
            need_margin = margin + tenure_bonus * min(1.0, max(0.0, r["t"] - since) / tenure_full)
            if still and still_margin is not None:
                need_margin = max(need_margin, still_margin)
            need_secs = max(switch, still_hold) if (still and still_hold is not None) else switch
            if best == incumbent or contenders[best] - contenders.get(incumbent, 0.0) < need_margin:
                challenge = None
            else:
                challenge = challenge if challenge and challenge[0] == best else (best, r["t"])
                if r["t"] - challenge[1] >= need_secs:
                    incumbent, challenge, since = best, None, r["t"]
        out.append(incumbent)
    return out


def changes_and_flips(floors):
    s = [f for f in floors if f]
    ch = [(s[i - 1], s[i]) for i in range(1, len(s)) if s[i] != s[i - 1]]
    flips = sum(1 for i in range(1, len(ch)) if ch[i][1] == ch[i - 1][0])
    return len(ch), flips


def short(ent):
    for prefix in ("private_ble_device_", "tile_", "findmy_"):
        if ent.startswith(prefix):
            return ent[len(prefix):]
    return ent


def parse_when(text):
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return datetime.fromisoformat(text).timestamp()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="an election-log directory, one of its files, or a floor_capture.py file")
    ap.add_argument("--layout", help="layout JSON (or a snapshot) for the tuning the log was made under")
    ap.add_argument("--hours", type=float, help="only the last N hours of the log")
    ap.add_argument("--thing", action="append", default=[], help="only these things (substring match; repeatable)")
    ap.add_argument("--truth", action="append", default=[], metavar="ENT=FLOOR", help="where a thing really was, for grading")
    ap.add_argument("--since", help="grading window start (ISO or unix seconds, local time)")
    ap.add_argument("--until", help="grading window end")
    ap.add_argument("--weight", type=float, help="floor_proximity_weight to replay at (re-scores every floor)")
    ap.add_argument("--formula", choices=("gated", "geometric"), default="gated",
                    help="floor_proximity_blend to replay --weight under (see rescore)")
    ap.add_argument("--margin", type=float, help="floor_switch_margin to replay at")
    ap.add_argument("--switch", type=float, help="floor_switch_secs to replay at")
    ap.add_argument("--no-variants", action="store_true", help="only the settings given, not the built-in sweep")
    args = ap.parse_args(argv)

    tuning = dict(DEFAULTS)
    if args.layout:
        with open(args.layout, encoding="utf-8") as fh:
            layout = json.load(fh)
        for k in DEFAULTS:
            if k in (layout.get("tuning") or {}):
                tuning[k] = layout["tuning"][k]
    rows = load_rows(args.path, args.hours)
    if args.thing:
        rows = [r for r in rows if any(t in r["ent"] for t in args.thing)]
    if not rows:
        print("no cycles to replay", file=sys.stderr)
        return 1
    by_ent = collections.defaultdict(list)
    for r in rows:
        by_ent[r["ent"]].append(r)
    truth = dict(t.split("=", 1) for t in args.truth if "=" in t)
    since, until = parse_when(args.since), parse_when(args.until)

    hours = (rows[-1]["t"] - rows[0]["t"]) / 3600
    print(f"{len(rows)} cycles over {hours:.1f} h, {len(by_ent)} things; logged rules: switch {tuning['floor_switch_secs']:.0f} s, "
          f"margin {tuning['floor_switch_margin']}, tenure +{tuning['floor_tenure_bonus']}/{tuning['floor_tenure_full_secs']:.0f} s, "
          f"smoothing {tuning['floor_prob_smoothing']}, proximity weight {tuning['floor_proximity_weight']}")
    given = {k: v for k, v in (("weight", args.weight), ("margin", args.margin), ("switch", args.switch)) if v is not None}
    if args.weight is not None and args.formula != "gated":
        given["formula"] = args.formula
    variants = [("as logged (validation)", {})]
    if given:
        variants.append((", ".join(f"{k} {v}" for k, v in given.items()), given))
    if not args.no_variants:
        for w in (0.8, 1.0):
            for m in (0.05, 0.10):
                variants.append((f"gated {w}, margin {m}", {"weight": w, "margin": m}))
        for w in (0.5, 0.7):
            for m in (0.05, 0.10):
                variants.append((f"geometric {w}, margin {m}", {"weight": w, "formula": "geometric", "margin": m}))
        variants += [("switch 120 s", {"switch": 120.0}), ("still: hold 300 s", {"still_hold": 300.0}),
                     ("still: margin 0.20", {"still_margin": 0.20})]

    pub_ch = sum(changes_and_flips([r["floor"] for r in s])[0] for s in by_ent.values())
    pub_fl = sum(changes_and_flips([r["floor"] for r in s])[1] for s in by_ent.values())
    head = f"{'variant':30} {'agree':>6} {'changes':>8} {'flips':>6}"
    if truth:
        head += "   " + "  ".join(f"{short(e)[:14]} wrong%" for e in truth)
    print(f"\n{head}   (published: changes {pub_ch}, flips {pub_fl})")
    for label, kw in variants:
        agree = n = ch_tot = fl_tot = 0
        wrong = {}
        per = []
        for ent, seq in by_ent.items():
            out = replay(seq, tuning, **kw)
            pub = [r["floor"] for r in seq]
            agree += sum(1 for a, b in zip(out, pub) if a == b)
            n += len(out)
            c, f = changes_and_flips(out)
            ch_tot += c
            fl_tot += f
            per.append((short(ent), c, f))
            if ent in truth:
                graded = [(o, r) for o, r in zip(out, seq) if (since is None or r["t"] >= since) and (until is None or r["t"] <= until)]
                wrong[ent] = (100.0 * sum(1 for o, _ in graded if o != truth[ent]) / len(graded)) if graded else float("nan")
        line = f"{label:30} {agree / n * 100:5.1f}% {ch_tot:8} {fl_tot:6}"
        if truth:
            line += "   " + "  ".join(f"{wrong.get(e, float('nan')):20.1f}" for e in truth)
        busiest = sorted(per, key=lambda x: -x[1])[:4]
        print(line + "   " + " ".join(f"{s[:6]}{c}/{f}" for s, c, f in busiest if c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
