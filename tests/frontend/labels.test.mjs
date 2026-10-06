// Label placement and proxy peek-through (sextant-map.js). Run: node --test tests/frontend
import { test } from "node:test";
import assert from "node:assert/strict";
import { placeLabels, coveredProxies } from "../../custom_components/sextant/frontend/sextant-map.js";

// A label plate 100 wide and 14 high, with 2 of breathing room under it.
let seq = 0;
const lab = (x, y, prio = 2, w = 100) => ({ x, y, w, h: 14, gap: 2, prio, order: seq++ });
const boxOf = (p) => ({ x0: p.label.x - p.label.w / 2, x1: p.label.x + p.label.w / 2,
                        y0: p.y - p.label.h / 2, y1: p.y + p.label.h / 2 });
const overlaps = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;

test("a label on its own keeps its anchor", () => {
  const out = placeLabels([lab(100, 100)]);
  assert.equal(out.length, 1);
  assert.equal(out[0].y, 100);
});

test("labels far apart all keep their anchors", () => {
  const out = placeLabels([lab(0, 0), lab(500, 0), lab(0, 500)]);
  assert.equal(out.length, 3);
  for (const p of out) assert.equal(p.y, p.label.y);
});

test("two labels at the same point do not overlap - this is the 'Michel Michelle Phone 3 Case' bug", () => {
  const out = placeLabels([lab(100, 100), lab(100, 100)]);
  assert.equal(out.length, 2);
  assert.ok(!overlaps(boxOf(out[0]), boxOf(out[1])));
});

test("a stack of labels on one nightstand all stay readable", () => {
  const out = placeLabels([lab(100, 100), lab(104, 101), lab(98, 99), lab(101, 100)]);
  assert.equal(out.length, 4, "all four should find room");
  const boxes = out.map(boxOf);
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      assert.ok(!overlaps(boxes[i], boxes[j]), `labels ${i} and ${j} overlap`);
    }
  }
});

test("the higher priority label keeps the anchor and the lower one moves", () => {
  const quiet = lab(100, 100, 1);
  const loud = lab(100, 100, 3);
  const out = placeLabels([quiet, loud]);
  const placedLoud = out.find((p) => p.label === loud);
  const placedQuiet = out.find((p) => p.label === quiet);
  assert.equal(placedLoud.y, 100, "the loud one sits where it wanted to");
  assert.notEqual(placedQuiet.y, 100);
});

test("a label with nowhere to go is dropped rather than printed through another", () => {
  const many = Array.from({ length: 40 }, () => lab(100, 100));
  const out = placeLabels(many);
  assert.ok(out.length < many.length, "some must be dropped");
  const boxes = out.map(boxOf);
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      assert.ok(!overlaps(boxes[i], boxes[j]), "nothing that IS placed may overlap");
    }
  }
});

test("placement is stable: the same frame twice gives the same answer", () => {
  const build = () => { seq = 0; return [lab(100, 100, 2), lab(100, 100, 2), lab(101, 100, 2)]; };
  const a = placeLabels(build()).map((p) => [p.label.order, p.y]);
  const b = placeLabels(build()).map((p) => [p.label.order, p.y]);
  assert.deepEqual(a, b);
});

// --- proxies under things ----------------------------------------------------

const proxy = (x, y, s = 10) => ({ x, y, s });
const thing = (x, y, r = 12) => ({ x, y, r });

test("a proxy with nothing on it is left alone", () => {
  assert.deepEqual(coveredProxies([proxy(100, 100)], [thing(400, 400)]), []);
});

test("a proxy under a thing is reported, with the covering radius", () => {
  // Michelle's nightstand: the C5 with her watch sitting on it.
  const out = coveredProxies([proxy(267, 499)], [thing(267, 499)]);
  assert.equal(out.length, 1);
  assert.equal(out[0].cover, 12);
});

test("the widest thing on a proxy is the one it has to clear", () => {
  const out = coveredProxies([proxy(100, 100)], [thing(100, 100, 12), thing(102, 101, 19)]);
  assert.equal(out.length, 1);
  assert.equal(out[0].cover, 19);
});

test("a thing just off the proxy does not count as covering it", () => {
  assert.deepEqual(coveredProxies([proxy(100, 100)], [thing(140, 100)]), []);
});

test("only the covered proxy of a pair is reported - the other stays as drawn", () => {
  // Both master-bedroom C5s; only Michelle's has things resting on it.
  const out = coveredProxies([proxy(267, 499), proxy(564, 500)], [thing(267, 499)]);
  assert.equal(out.length, 1);
  assert.equal(out[0].x, 267);
});
