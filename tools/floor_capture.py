"""Capture every thing's per-cycle floor election, for tools/replay_floors.py.

Subscribes to sextant/subscribe the way the panel does and appends one JSON
line per thing per cycle: the elected floor, the smoothed floor odds, each
floor's candidate (fix, fit confidence, proximity, bias, score, and the
proxies on that floor that heard the thing with their distances), speed, the
room and its lock state, and the published position - the same records the
integration keeps itself when election_log_hours is set on the Tuning page
(election_log.py). This is the external alternative: nothing to turn on in
Sextant, a night's worth at a time, from any machine that can reach it.

    python tools/floor_capture.py out.jsonl --hours 30 --host 10.2.3.6:8123 --token-file ~/.ha_token

Needs the websockets package.
"""
import argparse, asyncio, json, os, sys, time, websockets

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("--hours", type=float, default=30)
ap.add_argument("--host", default="127.0.0.1:8123")
ap.add_argument("--token-file", default="~/.ha_token")
args = ap.parse_args()
TOKEN = open(os.path.expanduser(args.token_file)).read().strip()
OUT, HOURS, HOST = args.out, args.hours, args.host
KEEP = ("ent", "floor", "floors", "floor_cands", "speed", "zone", "zone_raw", "zone_locked", "locked", "nearest_zone", "sub_zone", "anchor", "conf", "cords", "fix", "residual_m")
def rows_of(payload):
    for key in ("positions", "rows", "things", "data"):
        v = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(v, list): return v
    return payload if isinstance(payload, list) else []
async def main():
    deadline, n, first = time.time() + HOURS * 3600, 0, True
    while time.time() < deadline:
        try:
            async with websockets.connect(f"ws://{HOST}/api/websocket", max_size=64*1024*1024) as ws:
                await ws.recv(); await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
                assert json.loads(await ws.recv())["type"] == "auth_ok"
                await ws.send(json.dumps({"id": 1, "type": "sextant/subscribe"}))
                with open(OUT, "a") as fh:
                    while time.time() < deadline:
                        m = json.loads(await asyncio.wait_for(ws.recv(), 120))
                        if m.get("type") != "event": continue
                        ev = m.get("event") or {}
                        rows = rows_of(ev)
                        if first:
                            print(f"{time.strftime('%H:%M:%S')} first event keys={list(ev)[:8] if isinstance(ev, dict) else type(ev)} rows={len(rows)} "
                                  f"with floor_cands={sum(1 for r in rows if isinstance(r, dict) and r.get('floor_cands'))}", flush=True)
                            first = False
                        t = time.time()
                        for r in rows:
                            if not isinstance(r, dict) or not r.get("floor_cands"): continue
                            rec = {"t": t, **{k: r.get(k) for k in KEEP}, "radii_n": len(r.get("radii") or [])}
                            fh.write(json.dumps(rec, separators=(",", ":")) + "\n"); n += 1
                        fh.flush()
                        if n and n % 5000 == 0: print(f"{time.strftime('%H:%M:%S')} {n} rows", flush=True)
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} reconnect: {str(e)[:80]}", flush=True); await asyncio.sleep(15)
    print(f"{time.strftime('%H:%M:%S')} done, {n} rows", flush=True)
asyncio.run(main())
