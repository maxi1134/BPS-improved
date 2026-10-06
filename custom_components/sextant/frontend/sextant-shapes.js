/**
 * What a save would destroy, with no DOM: imported by the editor, and
 * directly by the tests. The server runs the same check (snapshots.degraded)
 * and reports back, but by then the save is done; this one runs before the
 * draft is sent, so the editor can ask first.
 */

const KINDS = [["room", "zones", "zone_id"], ["spot", "subzones", "sub_zone_id"]];

function shapes(layout) {
  const out = new Map();
  for (const f of layout?.floor || []) {
    for (const [kind, key, idKey] of KINDS) {
      for (const item of f?.[key] || []) {
        if (!item) continue;
        out.set(`${f.name}\u0000${kind}\u0000${item.entity_id ?? item[idKey]}`, (item.cords || []).length);
      }
    }
  }
  return out;
}

/** Every room or spot that `after` leaves with fewer than three corners
 * where `before` had three or more, in words. Three is the least that
 * encloses any area; one or two draw as nothing, and the shape silently
 * disappears from the plan. Deleting a shape outright is not reported: people
 * do that on purpose. */
export function lostShapes(before, after) {
  const was = shapes(before), now = shapes(after), lost = [];
  for (const [key, corners] of was) {
    const left = now.get(key);
    if (corners >= 3 && left !== undefined && left < 3) {
      const [floor, kind, name] = key.split("\u0000");
      lost.push(`${kind} "${name}" on ${floor} drops from ${corners} corners to ${left} and would no longer be drawn`);
    }
  }
  return lost;
}
