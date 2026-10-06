// Dragging an mmWave sensor moves the sensor, and nothing else; it is found by
// a click on its marker. Run: node --test tests/frontend
//
// The drag handler's last branch is rooms-or-spots: any point-like kind that
// is not caught before it has its one point written over the spot at the
// same index (the note bug of 2026-09-22). A radar is caught the same way.
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
    getContext: () => ctx, addEventListener() {}, removeEventListener() {}, setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 800 }),
  };
}
const ev = (type, x, y) => ({ type, pointerId: 1, clientX: x, clientY: y, button: 0, pointerType: "mouse", altKey: false });
const rect = (x0, y0, x1, y1) => [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }];

function editMap() {
  const changes = [];
  const map = new SextantMap(fakeCanvas(), { onChange: (kind, index) => changes.push([kind, index]) });
  map.floor = {
    name: "Office Floor", scale: 100, receivers: [], zones: [], remarks: [],
    subzones: [{ entity_id: "Desk", cords: rect(100, 100, 200, 160) }],
    radars: [{ radar_id: "r0", device_id: "dev", heading: 90, cords: { x: 300.5, y: 400.25 } }],
  };
  map.view = { k: 1, tx: 0, ty: 0 };
  map.invalidate = () => {};
  map.setMode("edit");
  return { map, changes };
}

test("a dragged radar moves, lands rounded, keeps its heading, and the spot keeps its corners", () => {
  const { map, changes } = editMap();
  const before = JSON.parse(JSON.stringify(map.floor.subzones));
  map._drag = { kind: "item", hit: { kind: "radar", index: 0 }, start: { x: 300.5, y: 400.25 }, moved: false,
                origin: map._itemPoints({ kind: "radar", index: 0 }) };
  map._move(ev("pointermove", 350.12345, 420.98765));
  map._up(ev("pointerup", 350.12345, 420.98765));
  const r = map.floor.radars[0];
  assert.deepEqual(r.cords, { x: 350.123, y: 420.988 });
  assert.equal(r.heading, 90);
  assert.deepEqual(map.floor.subzones, before);
  assert.deepEqual(changes, [["radar", 0]]);
});

test("a click on the marker finds the radar", () => {
  const { map } = editMap();
  const hit = map.hitTest({ x: 302, y: 401 });
  assert.equal(hit?.kind, "radar");
  assert.equal(hit.index, 0);
});

// The wedge is filled only inside the room the backend keeps targets to
// (_radar_room): the named room, none for "*", else the room the sensor is in
// or within half a metre of.
test("the wedge's room is the one the backend limits targets to", () => {
  const { map } = editMap();
  map.floor.zones = [
    { entity_id: "Office", cords: rect(0, 0, 1000, 500) },
    { entity_id: "Hall", cords: rect(0, 500, 1000, 1000) },
    { entity_id: "Void", no_go: true, cords: rect(0, 1000, 1000, 1200) },
  ];
  const at = (x, y, room) => map._radarRoomZone({ cords: { x, y }, room })?.entity_id ?? null;
  assert.equal(at(500, 900), "Hall");
  assert.equal(at(500, 1030), "Hall", "on the wall, 0.3 m outside");
  assert.equal(at(500, 1100), null, "a metre out, and a no-go area is never a room");
  assert.equal(at(500, 900, "Office"), "Office");
  assert.equal(at(500, 900, "*"), null);
});
