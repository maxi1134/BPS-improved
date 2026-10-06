// Floors in the order a reader expects for each place they are shown.
import { test } from "node:test";
import assert from "node:assert/strict";
import { sortFloors } from "../../custom_components/sextant/frontend/sextant-floors.js";

const FLOORS = [
  { name: "Ground Floor", level: 0 },
  { name: "Second Floor", level: 1 },
  { name: "Basement", level: -1 },
];

test("a list reads top-down: the top floor first", () => {
  assert.deepEqual(sortFloors(FLOORS).map((f) => f.name), ["Second Floor", "Ground Floor", "Basement"]);
});

test("tabs read bottom-up, left to right, like the building from the side", () => {
  assert.deepEqual(sortFloors(FLOORS, true).map((f) => f.name), ["Basement", "Ground Floor", "Second Floor"]);
});

test("floors on the same level keep their file order either way", () => {
  const twins = [{ name: "A", level: 0 }, { name: "B", level: 0 }];
  assert.deepEqual(sortFloors(twins).map((f) => f.name), ["A", "B"]);
  assert.deepEqual(sortFloors(twins, true).map((f) => f.name), ["A", "B"]);
});
