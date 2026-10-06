// Dragging a note moves the note, and nothing else. Run: node --test tests/frontend
//
// A note used to fall into the drag handler's rooms-or-spots branch and have
// its single point written over the spot at the same list index. On
// 2026-09-22 that turned "David Bedside Table" (subzones[0], dragged note
// remarks[0]) and "Michelle Bedside Table" into one-point shapes that drew as
// nothing at all.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = () => {};
globalThis.window = globalThis.window || { devicePixelRatio: 1 };
const { SextantMap } = await import("../../custom_components/sextant/frontend/sextant-map.js");

function fakeCanvas() {
  const ctx = new Proxy({}, { get: () => () => ({ addColorStop() {} }), set: () => true });
  const listeners = new Map();
  return {
    style: {}, width: 400, height: 800, listeners,
    getContext: () => ctx,
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type, fn) { if (listeners.get(type) === fn) listeners.delete(type); },
    setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 800 }),
  };
}

const ev = (type, x, y) => ({ type, pointerId: 1, clientX: x, clientY: y, button: 0, pointerType: "mouse", altKey: false });
const rect = (x0, y0, x1, y1) => [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }];

function editMap(remarks) {
  const changes = [];
  const map = new SextantMap(fakeCanvas(), { onChange: (kind, index) => changes.push([kind, index]) });
  map.floor = {
    name: "Second Floor", scale: 100, receivers: [], zones: [],
    subzones: [
      { entity_id: "David Bedside Table", cords: rect(497, 495, 572, 549) },
      { entity_id: "Michelle Bedside Table", cords: rect(232, 494, 308, 547) },
    ],
    remarks,
  };
  map.view = { k: 1, tx: 0, ty: 0 };
  map.invalidate = () => {};
  map.setMode("edit");
  return { map, changes };
}

function drag(map, index, from, to) {
  map._drag = { kind: "item", hit: { kind: "remark", index }, start: { ...from }, moved: false,
                origin: map._itemPoints({ kind: "remark", index }) };
  map._move(ev("pointermove", to.x, to.y));
  map._up(ev("pointerup", to.x, to.y));
}

test("a dragged note moves, lands rounded, and every spot keeps its corners", () => {
  const { map, changes } = editMap([{ remark_id: "r0", text: "Proxy from light below", cords: { x: 1206.512, y: 807.677 } }]);
  const before = JSON.parse(JSON.stringify(map.floor.subzones));
  drag(map, 0, { x: 1206.512, y: 807.677 }, { x: 1223.91649, y: 785.294 });
  assert.deepEqual(map.floor.remarks[0].cords, { x: 1223.916, y: 785.294 });
  assert.deepEqual(map.floor.subzones, before, "no spot may change when a note is dragged");
  assert.deepEqual(changes, [["remark", 0]]);
});

test("the second note on a floor leaves the second spot alone too", () => {
  const { map } = editMap([
    { remark_id: "r0", text: "one", cords: { x: 1206, y: 807 } },
    { remark_id: "r1", text: "two", cords: { x: 530, y: 700 } },
  ]);
  const before = JSON.parse(JSON.stringify(map.floor.subzones));
  drag(map, 1, { x: 530, y: 700 }, { x: 546.522, y: 682.442 });
  assert.deepEqual(map.floor.remarks[1].cords, { x: 546.522, y: 682.442 });
  assert.deepEqual(map.floor.remarks[0].cords, { x: 1206, y: 807 });
  assert.deepEqual(map.floor.subzones, before);
});

test("destroy takes the canvas listeners off again", () => {
  const canvas = fakeCanvas();
  const map = new SextantMap(canvas, {});
  assert.ok(canvas.listeners.size > 0);
  map.destroy();
  assert.equal(canvas.listeners.size, 0);
  assert.equal(map._raf, 0);
});
