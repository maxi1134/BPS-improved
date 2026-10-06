// What a save would destroy (sextant-shapes.js). Run: node --test tests/frontend
import { test } from "node:test";
import assert from "node:assert/strict";
import { lostShapes } from "../../custom_components/sextant/frontend/sextant-shapes.js";

const pts = (n) => Array.from({ length: n }, (_, i) => ({ x: i, y: i * 2 }));
const layout = (spots, rooms = []) => ({
  floor: [{
    name: "Second Floor",
    zones: rooms.map(([name, n], i) => ({ zone_id: `z${i}`, entity_id: name, cords: pts(n) })),
    subzones: spots.map(([name, n], i) => ({ sub_zone_id: `s${i}`, entity_id: name, cords: pts(n) })),
  }],
});

test("an untouched plan loses nothing", () => {
  const a = layout([["David Bedside Table", 4]]);
  assert.deepEqual(lostShapes(a, JSON.parse(JSON.stringify(a))), []);
});

test("a spot collapsing to one point is reported - the bedside-table bug", () => {
  const before = layout([["David Bedside Table", 4], ["Michelle Bedside Table", 4]]);
  const after = layout([["David Bedside Table", 1], ["Michelle Bedside Table", 1]]);
  const lost = lostShapes(before, after);
  assert.equal(lost.length, 2);
  assert.match(lost[0], /David Bedside Table.*4 corners to 1/);
});

test("a room collapsing is reported too", () => {
  assert.equal(lostShapes(layout([], [["Master Bedroom", 8]]), layout([], [["Master Bedroom", 2]])).length, 1);
});

test("reshaping, deleting and adding are not losses", () => {
  assert.deepEqual(lostShapes(layout([["Couch", 8]]), layout([["Couch", 4]])), []);
  assert.deepEqual(lostShapes(layout([["Couch", 8], ["Pantry", 4]]), layout([["Couch", 8]])), []);
  assert.deepEqual(lostShapes(layout([["Couch", 8]]), layout([["Couch", 8], ["New", 4]])), []);
});

test("junk in does not throw", () => {
  assert.deepEqual(lostShapes(null, undefined), []);
  assert.deepEqual(lostShapes({ floor: [null] }, { floor: [{ name: "F", zones: [null] }] }), []);
});
