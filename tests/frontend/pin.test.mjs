// Corner snapping for alignment pins (sextant-map.js snapToVertex). Run: node --test tests/frontend
import { test } from "node:test";
import assert from "node:assert/strict";
import { snapToVertex } from "../../custom_components/sextant/frontend/sextant-map.js";

const rect = (x0, y0, x1, y1) => ({ cords: [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }] });
const rooms = [rect(0, 0, 400, 300), rect(400, 0, 800, 300)];

test("a pin dropped near a room corner lands exactly on it", () => {
  assert.deepEqual(snapToVertex({ x: 797, y: 296 }, rooms, 12), { x: 800, y: 300 });
});

test("out of reach of every corner the pin stays where it was put", () => {
  assert.equal(snapToVertex({ x: 200, y: 150 }, rooms, 12), null);
  assert.equal(snapToVertex({ x: 413, y: 0 }, rooms, 12), null);   // on a wall, but 13 px from its corner
});

test("the nearest corner wins, and a corner two rooms share is one corner", () => {
  assert.deepEqual(snapToVertex({ x: 405, y: 4 }, rooms, 12), { x: 400, y: 0 });
  assert.deepEqual(snapToVertex({ x: 9, y: 2 }, [rect(0, 0, 10, 10), rect(16, 0, 30, 10)], 12), { x: 10, y: 0 });
});

test("a floor with no rooms, or rooms with no points, snaps to nothing", () => {
  assert.equal(snapToVertex({ x: 1, y: 1 }, [], 12), null);
  assert.equal(snapToVertex({ x: 1, y: 1 }, undefined, 12), null);
  assert.equal(snapToVertex({ x: 1, y: 1 }, [{}], 12), null);
});

// --- ghosts (staleness) and the short age label -------------------------------------
import { staleness, shortAge } from "../../custom_components/sextant/frontend/sextant-map.js";

test("a thing is a ghost only once it has gone unheard longer than the threshold", () => {
  const now = 10_000;
  assert.equal(staleness({ updated: now - 119 }, 120, now).ghost, false);
  assert.equal(staleness({ updated: now - 121 }, 120, now).ghost, true);
  assert.equal(staleness({ updated: now - 121 }, 120, now).age, 121);
  assert.equal(staleness({ updated: now - 9999 }, 0, now).ghost, false);   // 0 switches ghosts off
  assert.equal(staleness({}, 120, now).ghost, false);                      // a replayed history point has no age
});

test("ages read as the shortest honest phrase", () => {
  assert.deepEqual([45, 180, 7300, 200000].map(shortAge), ["45s", "3m", "2h", "2d"]);
});

// --- right-angle snapping and squaring -----------------------------------------------
import { snapCorner, squareUp, ORTHO_SNAP_DEG } from "../../custom_components/sextant/frontend/sextant-map.js";

test("a corner near a straight edge snaps onto it; a real diagonal does not", () => {
  const prev = { x: 0, y: 0 };
  assert.deepEqual(snapCorner({ x: 5, y: 100 }, prev, null), { x: 0, y: 100, snapped: true });     // ~3 deg off vertical
  assert.deepEqual(snapCorner({ x: 100, y: -6 }, prev, null), { x: 100, y: 0, snapped: true });    // ~3 deg off horizontal
  const diag = snapCorner({ x: 100, y: 60 }, prev, null);                                            // 31 deg: between the snaps
  assert.equal(diag.snapped, false);
  assert.deepEqual([diag.x, diag.y], [100, 60]);
  assert.ok(ORTHO_SNAP_DEG >= 5 && ORTHO_SNAP_DEG <= 10);
});

test("a dragged corner takes x from one neighbour and y from the other: a clean right angle", () => {
  const at = snapCorner({ x: 103, y: 197 }, { x: 100, y: 0 }, { x: 0, y: 200 });
  assert.deepEqual(at, { x: 100, y: 200, snapped: true });
});

test("squareUp straightens a wonky L and leaves every corner at 90 degrees", () => {
  const wonky = [
    { x: 1313.6, y: 342.9 }, { x: 1312.0, y: 434.8 }, { x: 1403.5, y: 435.0 }, { x: 1398.8, y: 585.2 },
    { x: 1226.8, y: 594.8 }, { x: 1226.8, y: 703.4 }, { x: 1512.2, y: 703.4 }, { x: 1511.0, y: 341.6 },
  ];   // the real couch, as drawn
  const sq = squareUp(wonky);
  for (let i = 0; i < sq.length; i++) {
    const a = sq[i], b = sq[(i + 1) % sq.length];
    assert.ok(a.x === b.x || a.y === b.y, `edge ${i} is not straight: ${JSON.stringify([a, b])}`);
  }
  const worst = Math.max(...sq.map((q, i) => Math.hypot(q.x - wonky[i].x, q.y - wonky[i].y)));
  assert.ok(worst < 8, `a corner moved ${worst} px`);
});

test("squareUp keeps a diagonal wall and lines up collinear runs", () => {
  const shape = [{ x: 0, y: 0 }, { x: 100, y: 2 }, { x: 200, y: -1 }, { x: 300, y: 200 }, { x: 1, y: 200 }];
  const sq = squareUp(shape);
  assert.equal(sq[0].y, sq[1].y); assert.equal(sq[1].y, sq[2].y);   // one straight top edge, not a step
  assert.equal(sq[0].x, sq[4].x);                                     // left side straight
  assert.deepEqual([sq[3].x, sq[3].y], [300, 200]);                   // a 63 deg wall's far end untouched
  assert.deepEqual(squareUp([{ x: 0, y: 0 }, { x: 1, y: 1 }]), [{ x: 0, y: 0 }, { x: 1, y: 1 }]);
});


test("45 degree edges snap too, and a corner between two constrained edges lands on the crossing", () => {
  const near45 = snapCorner({ x: 100, y: 104 }, { x: 0, y: 0 }, null);               // ~46 deg
  assert.equal(near45.snapped, true);
  assert.ok(Math.abs(near45.x - near45.y) < 1e-9, `not on the diagonal: ${JSON.stringify(near45)}`);
  const up = snapCorner({ x: 100, y: -97 }, { x: 0, y: 0 }, null);                   // ~44 deg the other way
  assert.ok(Math.abs(up.x + up.y) < 1e-9, `not on the other diagonal: ${JSON.stringify(up)}`);
  // A cut corner: vertical edge up from (200, 0), 45 degree edge from (150, 250).
  const cut = snapCorner({ x: 203, y: 198 }, { x: 200, y: 0 }, { x: 150, y: 250 });
  assert.ok(Math.abs(cut.x - 200) < 1e-9 && Math.abs(cut.y - 200) < 1e-9, JSON.stringify(cut));
  const free = snapCorner({ x: 100, y: 58 }, { x: 0, y: 0 }, null);                 // 30 deg: nowhere near
  assert.equal(free.snapped, false);
});

test("squareUp makes a chamfered corner an exact 45 and keeps the square edges square", () => {
  const bay = [{ x: 0, y: 0 }, { x: 300, y: 2 }, { x: 301, y: 200 }, { x: 250, y: 248 }, { x: 1, y: 251 }];
  const sq = squareUp(bay);
  const dir = (a, b) => Math.round((Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI);
  const headings = sq.map((q, i) => dir(q, sq[(i + 1) % sq.length]));
  assert.deepEqual(headings, [0, 90, 135, 180, -90]);
  const worst = Math.max(...sq.map((q, i) => Math.hypot(q.x - bay[i].x, q.y - bay[i].y)));
  assert.ok(worst < 6, `a corner moved ${worst} px`);
});
