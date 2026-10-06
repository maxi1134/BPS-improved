// Touch input on the map: two fingers pinch, and a placing tap only counts on lift.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
globalThis.window = globalThis.window || { devicePixelRatio: 1 };
const { SextantMap } = await import("../../custom_components/sextant/frontend/sextant-map.js");

function fakeCanvas() {
  const ctx = new Proxy({}, { get: () => () => ({ addColorStop() {} }), set: () => true });
  return {
    style: {}, width: 400, height: 800,
    getContext: () => ctx,
    addEventListener() {}, setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 800 }),
  };
}

const ev = (type, id, x, y, extra = {}) => ({ type, pointerId: id, clientX: x, clientY: y, button: 0, pointerType: "touch", altKey: false, ...extra });

function mapWith(host = {}) {
  const map = new SextantMap(fakeCanvas(), host);
  map.floor = { name: "F", scale: 100, receivers: [], zones: [], subzones: [] };
  map.view = { k: 1, tx: 0, ty: 0 };
  map.invalidate = () => {};
  return map;
}

test("two fingers spreading zoom in about the point between them", () => {
  const map = mapWith();
  map._down(ev("pointerdown", 1, 100, 100));
  map._down(ev("pointerdown", 2, 200, 100));
  const anchor = map.toMap({ x: 150, y: 100 });
  map._move(ev("pointermove", 1, 50, 100));
  map._move(ev("pointermove", 2, 250, 100));
  assert.ok(Math.abs(map.view.k - 2) < 1e-9);
  const after = map.toScreen(anchor);
  assert.ok(Math.abs(after.x - 150) < 1e-6 && Math.abs(after.y - 100) < 1e-6);
  map._up(ev("pointerup", 1, 50, 100));
  // The finger left behind does not start a pan.
  const view = { ...map.view };
  map._move(ev("pointermove", 2, 300, 300));
  assert.deepEqual(map.view, view);
  map._up(ev("pointerup", 2, 300, 300));
});

test("a placing tap lands on lift, and not at all when it turned into a pan or pinch", () => {
  const placed = [];
  const map = mapWith({ isPlacing: () => true, onMapClick: (m) => { placed.push(m); return true; }, onSelect: () => { throw new Error("placing must not change the selection"); } });
  map._down(ev("pointerdown", 1, 120, 80));
  assert.equal(placed.length, 0);
  map._move(ev("pointermove", 1, 124, 83));   // a finger's wobble
  map._up(ev("pointerup", 1, 124, 83));
  assert.deepEqual(placed, [{ x: 120, y: 80 }]);

  map._down(ev("pointerdown", 1, 120, 80));
  map._move(ev("pointermove", 1, 180, 80));   // a pan
  map._up(ev("pointerup", 1, 180, 80));
  map._down(ev("pointerdown", 1, 120, 80));
  map._down(ev("pointerdown", 2, 220, 80));   // a pinch
  map._up(ev("pointerup", 2, 220, 80));
  map._up(ev("pointerup", 1, 120, 80));
  assert.equal(placed.length, 1);
});

test("the first finger of a pinch does not leave a corner behind while drawing", () => {
  const map = mapWith();
  map.setMode("edit");
  map.setTool("subzone");
  map.draft = [{ x: 0, y: 0 }, { x: 100, y: 0 }];
  map._down(ev("pointerdown", 1, 300, 300));
  assert.equal(map.draft.length, 3);
  map._down(ev("pointerdown", 2, 350, 300));
  assert.equal(map.draft.length, 2);
});

test("zoomTo fills the view with a spot", () => {
  const map = mapWith();
  map.zoomTo([{ x: 500, y: 500 }, { x: 640, y: 500 }, { x: 640, y: 640 }, { x: 500, y: 640 }]);
  const a = map.toScreen({ x: 500, y: 500 }), b = map.toScreen({ x: 640, y: 640 });
  assert.ok(b.x - a.x > 300 && b.x - a.x <= 400);
  assert.ok(Math.abs((a.x + b.x) / 2 - 200) < 1e-6 && Math.abs((a.y + b.y) / 2 - 400) < 1e-6);
});

test("heatCells adds up time per cell and stops at dropouts and long silences", async () => {
  const { heatCells } = await import("../../custom_components/sextant/frontend/sextant-map.js");
  const pts = [
    { t: 0, x: 1.1, y: 1.1, f: "Up" },          // 100 s on the bed
    { t: 100, x: 1.2, y: 1.3, f: "Up" },        // same cell, 50 s
    { t: 150, x: 4.0, y: 1.0, f: "Up" },        // unheard 2 h (a dropout): a minute
    { t: 7350, x: 4.0, y: 1.0, f: "Up", gap: 2 },  // heard again after a dropout
    { t: 7400, x: 0.2, y: 0.2, f: "Down" },     // down: until `end`
  ];
  const h = heatCells(pts, 7460, 0.5, 300);
  const bed = h.Up.cells.find((c) => c.x === 1.25 && c.y === 1.25);
  assert.equal(bed.secs, 150);
  const desk = h.Up.cells.find((c) => c.x === 4.25 && c.y === 1.25);
  assert.equal(desk.secs, 60 + 50);
  assert.equal(h.Up.max, 150);
  assert.equal(h.Down.total, 60);
  // No dropout flagged but a long wait for the next point: at most maxHoldSecs.
  const quiet = heatCells([{ t: 0, x: 0, y: 0, f: "F" }, { t: 5000, x: 9, y: 9, f: "F" }], 5000);
  assert.equal(quiet.F.cells.find((c) => c.x === 0.25).secs, 300);
});

test("bias colours: grey when even, green when this floor is favoured less, red when more", async () => {
  const { biasRgba, heatRgb } = await import("../../custom_components/sextant/frontend/sextant-map.js");
  assert.deepEqual(biasRgba(1).slice(0, 3), [140, 140, 140]);
  const less = biasRgba(0.5), more = biasRgba(2);
  assert.ok(less[1] > less[0] && less[1] > less[2]);           // green
  assert.ok(more[0] > more[1] && more[0] > more[2]);           // red
  assert.ok(biasRgba(1.1)[3] < more[3]);                       // stronger lean, stronger colour
  assert.deepEqual(heatRgb(0), [40, 110, 255]);
  assert.deepEqual(heatRgb(1), [225, 30, 30]);
});

test("a room linked to an area gets that area's icon; others do not", () => {
  const map = new SextantMap(fakeCanvas(), {});
  map.invalidate = () => {};
  map.setAreas({ kitchen: { icon: "mdi:silverware-fork-knife" }, hall: { icon: null }, den: {} });
  assert.deepEqual(map.areaIcons, { kitchen: "mdi:silverware-fork-knife" });
  map.setAreas(null);
  assert.deepEqual(map.areaIcons, {});
});

test("the map reads light or dark from the page's own background", () => {
  const map = new SextantMap(fakeCanvas(), {});
  const bg = { value: "#ffffff" };
  map._css = (name, fallback) => (name === "--sextant-map-bg" ? bg.value : fallback);
  map._readTheme();
  assert.equal(map.dark, false, "white is light");
  for (const dark of ["#101418", "#000", "rgb(24, 26, 30)", "rgba(16,18,22,1)"]) {
    bg.value = dark;
    map._readTheme();
    assert.equal(map.dark, true, `${dark} is dark`);
  }
  for (const light of ["#fafafa", "rgb(240, 240, 235)"]) {
    bg.value = light;
    map._readTheme();
    assert.equal(map.dark, false, `${light} is light`);
  }
});
