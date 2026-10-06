// Things in one place become one marker (sextant-map.js clusterThings). Run: node --test tests/frontend
import { test } from "node:test";
import assert from "node:assert/strict";
import { clusterThings } from "../../custom_components/sextant/frontend/sextant-map.js";

const thing = (ent, x, y) => ({ ent, cords: [x, y] });
const names = (c) => c.members.map((m) => m.ent);

test("things far apart are not clustered", () => {
  assert.deepEqual(clusterThings([thing("a", 0, 0), thing("b", 500, 0)], 20), []);
});

test("Michelle's nightstand: phone, watch and case become one marker of three", () => {
  const out = clusterThings([thing("phone", 267, 499), thing("watch", 270, 502), thing("case", 264, 497), thing("lamp", 900, 900)], 20);
  assert.equal(out.length, 1);
  assert.deepEqual(names(out[0]), ["phone", "watch", "case"]);
  assert.ok(Math.abs(out[0].x - 267) < 1 && Math.abs(out[0].y - 499.3) < 1, "centroid of the three");
});

test("the thing you clicked stays its own marker and the rest still cluster", () => {
  const out = clusterThings([thing("phone", 267, 499), thing("watch", 270, 502), thing("case", 264, 497)], 20, new Set(["phone"]));
  assert.equal(out.length, 1);
  assert.deepEqual(names(out[0]), ["watch", "case"]);
});

test("a smaller radius - zooming in - takes them apart again", () => {
  const things = [thing("phone", 267, 499), thing("watch", 290, 499)];
  assert.equal(clusterThings(things, 30).length, 1);
  assert.equal(clusterThings(things, 10).length, 0);
});

test("a thing with no position is ignored, not crashed on", () => {
  assert.deepEqual(clusterThings([{ ent: "ghost" }, thing("a", 0, 0)], 20), []);
});
