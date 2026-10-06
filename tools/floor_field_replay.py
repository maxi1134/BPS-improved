#!/usr/bin/env python3
"""
Re-score logged floor elections under a proposed bias field, before shipping it.

A bias field (custom_components/sextant/floor_field.py) is a prior, and a prior
painted by eye is a guess. This takes cycles that really happened - each one
carrying every contending floor's own fix and score, the ``floor_cands`` block
Sextant publishes per cycle - and asks what the election would have made of
them under a different field. The fits and the proximity weighting are taken
as logged; only the bias is recomputed, which is exactly the part a field
changes.

Two honest limits. It scores each cycle on its own: the live election smooths
the odds and makes a challenger hold a lead for a dwell, so "would have won
62 % of cycles" means "would have been elected", not "would have flickered".
And it can only re-place a floor that solved that cycle - a field cannot
conjure a contender.

Usage::

    # 1. log a thing (any subscriber to sextant/subscribe that keeps
    #    "floor_cands" and "updated" will do), one JSON object per line
    # 2. take a copy of the layout and shape its field, or point at the store
    python tools/floor_field_replay.py meg_cycles.jsonl --layout layout.json

    # with ground truth for a window (unix seconds), it grades instead of counts
    python tools/floor_field_replay.py meg_cycles.jsonl --layout layout.json \\
        --truth "Second Floor" --since 1789760000 --until 1789763000

    # try a stroke without editing anything: FLOOR:AREA=VALUE, repeatable
    python tools/floor_field_replay.py meg_cycles.jsonl --layout layout.json \\
        --paint "Second Floor:Catwalk=1.4" --paint "Second Floor:Foyer Upper=0.5"
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

_FF_PATH = Path(__file__).resolve().parent.parent / "custom_components" / "sextant" / "floor_field.py"
_spec = importlib.util.spec_from_file_location("floor_field", _FF_PATH)
floor_field = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(floor_field)


def load_layout(path: Path) -> dict:
    """A bare layout, or HA's store file wrapping one under "data"."""
    raw = json.loads(path.read_text())
    layout = raw.get("data") if isinstance(raw.get("data"), dict) and "floor" in raw["data"] else raw
    if not isinstance(layout.get("floor"), list):
        raise SystemExit(f"{path}: no floors in this file")
    return layout


def load_cycles(path: Path, since: float | None, until: float | None) -> list[dict]:
    cycles = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row.get("floor_cands"), dict) or not row["floor_cands"]:
            continue
        stamp = row.get("updated")
        if since is not None and (stamp is None or stamp < since):
            continue
        if until is not None and (stamp is None or stamp > until):
            continue
        cycles.append(row)
    return cycles


def scalar_bias(floor: dict) -> float:
    bias = floor.get("bias")
    ok = isinstance(bias, (int, float)) and not isinstance(bias, bool) and 0 < bias <= 10
    return float(bias) if ok else 1.0


def apply_strokes(layout: dict, strokes: list[str]) -> None:
    for stroke in strokes:
        try:
            where, value = stroke.rsplit("=", 1)
            floor_name, area = where.split(":", 1)
            value = float(value)
        except ValueError:
            raise SystemExit(f'--paint {stroke!r}: expected "FLOOR:AREA=VALUE"') from None
        floor = next((f for f in layout["floor"] if f.get("name") == floor_name), None)
        if floor is None:
            raise SystemExit(f"--paint: no floor named {floor_name!r}")
        shapes = (floor.get("zones") or []) + (floor.get("subzones") or [])
        shape = next((s for s in shapes if s.get("entity_id") == area), None)
        if shape is None:
            raise SystemExit(f"--paint: no room or spot named {area!r} on {floor_name}")
        if floor_field.parse(floor) is None:
            pts = [(p["x"], p["y"]) for s in shapes for p in s.get("cords") or []]
            pts += [(r["cords"]["x"], r["cords"]["y"]) for r in floor.get("receivers") or [] if "cords" in r]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            floor[floor_field.FIELD_KEY] = floor_field.flat((min(xs), min(ys), max(xs), max(ys)), floor["scale"])
        cells = floor_field.paint(floor, [(p["x"], p["y"]) for p in shape["cords"]], value)
        print(f"  painted {floor_name} / {area} = {value}  ({cells} cells)")


def rescore(cycle: dict, floors: dict[str, dict]) -> dict[str, float]:
    out = {}
    for name, cand in cycle["floor_cands"].items():
        floor = floors.get(name)
        bias = scalar_bias(floor) * floor_field.sample(floor, cand["fix"]) if floor else 1.0
        out[name] = cand["prox"] * bias
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", type=Path, help="JSONL of per-cycle payloads carrying floor_cands")
    ap.add_argument("--layout", type=Path, required=True, help="layout JSON (bare, or HA's .storage/sextant)")
    ap.add_argument("--paint", action="append", default=[], metavar="FLOOR:AREA=VALUE")
    ap.add_argument("--truth", help="the floor the thing was really on, for the whole window")
    ap.add_argument("--since", type=float)
    ap.add_argument("--until", type=float)
    args = ap.parse_args()

    layout = copy.deepcopy(load_layout(args.layout))
    if args.paint:
        print("Strokes:")
        apply_strokes(layout, args.paint)
    floors = {f["name"]: f for f in layout["floor"] if isinstance(f, dict)}
    cycles = load_cycles(args.log, args.since, args.until)
    if not cycles:
        print("No cycles with floor_cands in that window. The payload gained them in the "
              "build that added bias fields; older logs cannot be replayed.", file=sys.stderr)
        return 1

    logged, proposed, contested = Counter(), Counter(), 0
    margins_logged, margins_proposed = [], []
    for cycle in cycles:
        old = {f: c["score"] for f, c in cycle["floor_cands"].items()}
        new = rescore(cycle, floors)
        logged[max(old, key=old.get)] += 1
        proposed[max(new, key=new.get)] += 1
        if len(old) > 1:
            contested += 1
        if args.truth and args.truth in old and len(old) > 1:
            for scores, sink in ((old, margins_logged), (new, margins_proposed)):
                rival = max(v for f, v in scores.items() if f != args.truth)
                sink.append(scores[args.truth] / rival if rival > 0 else float("inf"))

    n = len(cycles)
    print(f"\n{n} cycles, {contested} contested by more than one floor\n")
    print(f"  {'floor':<22}{'as logged':>12}{'proposed':>12}")
    for name in sorted(set(logged) | set(proposed)):
        mark = "  <- truth" if name == args.truth else ""
        print(f"  {name:<22}{logged[name] / n:>11.1%}{proposed[name] / n:>12.1%}{mark}")
    if args.truth:
        if not margins_logged:
            print(f"\n{args.truth!r} never contested a cycle in this window - nothing to grade.")
        else:
            def median(xs):
                xs = sorted(xs)
                return xs[len(xs) // 2]
            print(f"\n  median lead of {args.truth!r} over its best rival (1.0 = tie, the live")
            print("  election wants it held above its switch margin for the dwell):")
            print(f"    as logged {median(margins_logged):.2f}x    proposed {median(margins_proposed):.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
