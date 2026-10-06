// Wall snapping for dragged proxies (sextant-map.js snapToWall). Run: node --test tests/frontend
import { test } from "node:test";
import assert from "node:assert/strict";
import { snapToWall, WALL_SNAP_M, WALL_RELEASE_M, WALL_INSET_M } from "../../custom_components/sextant/frontend/sextant-map.js";

const scale = 100; // px per metre
const rect = (name, x0, y0, x1, y1) => ({ entity_id: name, cords: [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }] });
const A = rect("A", 0, 0, 400, 300);      // shares the wall x=400 with B
const B = rect("B", 400, 0, 800, 300);
const rooms = [A, B];
const near = (p, q) => assert.ok(Math.hypot(p.x - q.x, p.y - q.y) < 1e-6, `${JSON.stringify(p)} != ${JSON.stringify(q)}`);

test("away from every wall the proxy follows the cursor", () => {
  const r = snapToWall({ x: 200, y: 150 }, rooms, scale, null);
  assert.equal(r.snap, null);
  near(r.point, { x: 200, y: 150 });
});

test("near the shared wall from inside A it lands on the wall, 5 cm on A's side", () => {
  const r = snapToWall({ x: 392, y: 150 }, rooms, scale, null);
  assert.equal(r.snap.name, "A");
  near(r.point, { x: 400 - WALL_INSET_M * scale, y: 150 });
});

test("crossing the wall by 10 cm keeps it on A's side; 70 cm releases it", () => {
  let r = snapToWall({ x: 392, y: 150 }, rooms, scale, null);
  r = snapToWall({ x: 410, y: 150 }, rooms, scale, r.snap);
  assert.equal(r.snap.name, "A");
  near(r.point, { x: 395, y: 150 });
  r = snapToWall({ x: 470, y: 150 }, rooms, scale, r.snap);
  assert.equal(r.snap, null);            // past WALL_RELEASE_M: free again
  near(r.point, { x: 470, y: 150 });
  r = snapToWall({ x: 408, y: 150 }, rooms, scale, r.snap);
  assert.equal(r.snap.name, "B");        // approached from B: B's side
  near(r.point, { x: 405, y: 150 });
});

test("sliding along a wall keeps the wall and turning a corner switches walls", () => {
  let r = snapToWall({ x: 393, y: 100 }, rooms, scale, null);
  const wall = r.snap.edge;
  r = snapToWall({ x: 397, y: 250 }, rooms, scale, r.snap);
  assert.equal(r.snap.edge, wall);
  near(r.point, { x: 395, y: 250 });
  r = snapToWall({ x: 380, y: 296 }, rooms, scale, r.snap);   // now nearer the bottom wall
  assert.notEqual(r.snap.edge, wall);
  assert.equal(r.snap.name, "A");
  near(r.point, { x: 380, y: 295 });
});

test("from outside every room it snaps into the nearest room", () => {
  const r = snapToWall({ x: 200, y: 310 }, rooms, scale, null);
  assert.equal(r.snap.name, "A");
  near(r.point, { x: 200, y: 295 });
  assert.equal(snapToWall({ x: 200, y: 400 }, rooms, scale, null).snap, null);
});

test("an unscaled floor uses pixel thresholds instead of failing", () => {
  const r = snapToWall({ x: 395, y: 150 }, rooms, null, null);
  assert.equal(r.snap.name, "A");
  assert.ok(r.point.x < 400 && r.point.x > 390);
});

test("a stale snap onto a wall that no longer exists is ignored", () => {
  const r = snapToWall({ x: 200, y: 150 }, rooms, scale, { room: 5, edge: 0 });
  assert.equal(r.snap, null);
});

test("constants are the documented ones", () => {
  assert.deepEqual([WALL_SNAP_M, WALL_RELEASE_M, WALL_INSET_M], [0.25, 0.6, 0.05]);
});
