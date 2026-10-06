/**
 * Floor ordering, with no DOM: imported by the panel through sextant-ui.js,
 * and directly by the tests.
 */

/** Floors by level: top-down (a dropdown or a list, where the top floor goes
 * at the top), or with `up` bottom-up (a row of tabs, read left to right like
 * the building seen from the side: basement first). Ties keep file order. */
export function sortFloors(floors, up = false) {
  const dir = up ? 1 : -1;
  return [...(floors || [])].map((f, i) => [f, i])
    .sort((a, b) => (dir * ((a[0].level ?? 0) - (b[0].level ?? 0))) || (a[1] - b[1]))
    .map(([f]) => f);
}
