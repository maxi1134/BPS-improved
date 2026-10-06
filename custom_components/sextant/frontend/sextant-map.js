/**
 * Sextant map: one canvas renderer shared by the panel and the Lovelace card.
 *
 * Draws a floor (image, zones, sub-zones, no-go areas, receivers) and the
 * things on it, with pan/zoom, and - in edit mode - lets the host move
 * receivers, drag polygon vertices, add vertices on edges and draw new
 * polygons. It owns no data model: the host hands it a floor object in the
 * layout's own shape (pixel coordinates of the floor image), thing rows
 * from the positions payload, and receives edits back through callbacks.
 *
 * Coordinate frames: "map" = floor image pixels (what the layout stores);
 * "screen" = CSS pixels on the canvas. view = {k, tx, ty}: screen = map*k + t.
 */

export const MAP_FRAME_WIDTH = 2000;
const RECEIVER_SIZE = 10;
const RECEIVER_SIZE_EDIT = 13;   // proxies are the things people drag: give them a target
const VERTEX_SIZE = 6;
const PIN_SIZE = 11;
const REMARK_SIZE = 9;           // the note's dot; its text hangs off to the right
const RADAR_SIZE = 11;           // an mmWave sensor's marker
const HIT_SLOP = 8;
// Closest zoom: 20 screen px per map px, enough for a bedside table to fill a phone.
const MAX_ZOOM = 20;
const THING_RADIUS = 12;
const HUES = [205, 25, 140, 95, 320, 45, 260, 180, 0, 60];
// Who wins a crowded patch of plan when labels are laid out (see _flushLabels).
// The thing you picked out first, then the things, then the plan they sit on.
const LABEL_PRIO = { focus: 4, thing: 3, place: 2, other: 2, proxy: 1 };
const LABEL_TRIES = 7;           // how far a label may step from its marker before it is dropped

/** Where each label of a frame goes so that no two overlap.
 *
 * Markers never move - a marker is a measurement, and shifting one would be a
 * lie about where something is - so it is the labels that give way. Each takes
 * its anchor if it is free, then steps below and above it in turn, and is
 * dropped if it can find nowhere clear: one readable label and a marker you
 * can click beats two labels printed through each other.
 *
 * Each label carries `x`, `y` (the anchor), `w`, `h` (its plate), `gap` (the
 * breathing room between stacked labels), `prio` (who gets first refusal) and
 * `order` (the tiebreak, so a frame draws the same way twice running).
 * Returns `{label, y}` for the ones that found room, in paint order. */
export function placeLabels(labels, tries = LABEL_TRIES) {
  const clash = (a, b) => a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
  const taken = [], out = [];
  for (const L of [...labels].sort((a, b) => b.prio - a.prio || a.order - b.order)) {
    const halfW = L.w / 2, halfH = L.h / 2, step = L.h + (L.gap || 0);
    let at = null;
    for (let ring = 0; ring < tries && at === null; ring++) {
      for (const dy of ring === 0 ? [0] : [ring * step, -ring * step]) {
        const box = { x0: L.x - halfW, x1: L.x + halfW, y0: L.y + dy - halfH, y1: L.y + dy + halfH };
        if (!taken.some((t) => clash(box, t))) { at = { y: L.y + dy, box }; break; }
      }
    }
    if (at === null) continue;
    taken.push(at.box);
    out.push({ label: L, y: at.y });
  }
  return out;
}

/** Things whose markers would sit on top of each other, gathered into one.
 *
 * A nightstand with a phone, a watch and an AirPods case on it drew three
 * dots in one place and three labels fighting for the space under them. At
 * that zoom they are one thing to look at: a marker with a count. Zoom in
 * and they come apart on their own, because `radius` is a screen size
 * converted to map units. Greedy: each thing joins the first cluster whose
 * centre is within `radius`, else starts one; `skip` names the things to
 * leave out (the one you clicked stays its own marker). Returns clusters of
 * two or more, with the centroid and the members in input order. */
export function clusterThings(things, radius, skip = new Set()) {
  const clusters = [];
  for (const t of things) {
    if (!t.cords || skip.has(t.ent)) continue;
    const [x, y] = t.cords;
    const home = clusters.find((c) => Math.hypot(c.x - x, c.y - y) < radius);
    if (home) {
      home.members.push(t);
      const n = home.members.length;
      home.x += (x - home.x) / n;
      home.y += (y - home.y) / n;
    } else {
      clusters.push({ x, y, members: [t] });
    }
  }
  return clusters.filter((c) => c.members.length > 1);
}

/** The proxies a thing marker is sitting on top of, each with `cover`: the
 * radius of the widest thing covering it. A proxy buried under a thing reads
 * as a proxy that is GONE, so the map draws these ones again over the top. */
export function coveredProxies(proxies, things) {
  const out = [];
  for (const p of proxies) {
    let cover = 0;
    for (const t of things) {
      if (Math.hypot(t.x - p.x, t.y - p.y) < t.r + p.s * 0.25) cover = Math.max(cover, t.r);
    }
    if (cover) out.push({ ...p, cover });
  }
  return out;
}

// --- Material Design Icons on the canvas ------------------------------------
// The panel classes things (person, dog, phone...) and draws that class's
// MDI icon in the dot. Canvas cannot render <ha-icon>, but Home Assistant
// resolves an icon name to SVG path data for us: render one off-screen,
// read the path out of its shadow DOM, and keep it. Callers get null until
// it arrives (the map is redrawn then) and fall back to initials.
const _iconPaths = new Map();
export function mdiPath(name, onReady) {
  if (!name) return null;
  if (_iconPaths.has(name)) return _iconPaths.get(name);
  _iconPaths.set(name, null);
  (async () => {
    try {
      const el = document.createElement("ha-icon");
      el.setAttribute("icon", name);
      el.style.cssText = "position:absolute;left:-9999px;top:-9999px;";
      document.body.appendChild(el);
      await el.updateComplete;
      for (let i = 0; i < 20; i++) {
        const svg = el.shadowRoot?.querySelector("ha-svg-icon");
        await svg?.updateComplete;
        const d = svg?.shadowRoot?.querySelector("path")?.getAttribute("d");
        if (d) { _iconPaths.set(name, new Path2D(d)); break; }
        await new Promise((r) => setTimeout(r, 100));
      }
      el.remove();
    } catch { /* stays null: initials are drawn instead */ }
    onReady?.();
  })();
  return null;
}

/** A thing's colour: the one it was given (#rrggbb), else its automatic hue. */
export function thingColor(ent, custom) {
  return custom && /^#[0-9a-f]{6}$/i.test(custom) ? custom : `hsl(${thingHue(ent)}, 70%, 45%)`;
}

/** The same colour with an alpha (and optionally darkened, for the halo). */
export function thingRgba(ent, custom, alpha, darker = false) {
  if (custom && /^#[0-9a-f]{6}$/i.test(custom)) {
    const n = parseInt(custom.slice(1), 16);
    const f = darker ? 0.8 : 1;
    return `rgba(${Math.round((n >> 16) * f)}, ${Math.round(((n >> 8) & 255) * f)}, ${Math.round((n & 255) * f)}, ${alpha})`;
  }
  const hue = thingHue(ent);
  return darker ? `hsla(${hue}, 80%, 40%, ${alpha})` : `hsla(${hue}, 70%, 45%, ${alpha})`;
}

export function thingHue(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return HUES[h % HUES.length];
}

export function polygonCentroid(points) {
  if (!points.length) return { x: 0, y: 0 };
  let area = 0, cx = 0, cy = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i], b = points[(i + 1) % points.length];
    const f = a.x * b.y - b.x * a.y;
    area += f; cx += (a.x + b.x) * f; cy += (a.y + b.y) * f;
  }
  if (Math.abs(area) < 1e-9) {
    const n = points.length;
    return { x: points.reduce((s, p) => s + p.x, 0) / n, y: points.reduce((s, p) => s + p.y, 0) / n };
  }
  area *= 0.5;
  return { x: cx / (6 * area), y: cy / (6 * area) };
}

export function pointInPolygon(pt, points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const a = points[i], b = points[j];
    if ((a.y > pt.y) !== (b.y > pt.y) && pt.x < ((b.x - a.x) * (pt.y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

function distToSegment(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

// --- Wall snapping for proxies -----------------------------------------------
// A proxy in a wall outlet or a light switch IS part of the wall, and a hand
// placing it lands a few centimetres to one side or the other; which side
// decides which room it counts for. While a proxy is dragged, a wall within
// WALL_SNAP_M pulls it onto itself, WALL_INSET_M inside the room the cursor
// is in (so the point-in-room test is never ambiguous), and keeps it there
// until the cursor is WALL_RELEASE_M past the wall: crossing takes a
// deliberate move, sliding along the wall and turning a corner does not.
// The host skips this while Alt is held.
export const WALL_SNAP_M = 0.25;
export const WALL_RELEASE_M = 0.6;
export const WALL_INSET_M = 0.05;
const PX_PER_M_FALLBACK = 40; // an unscaled floor: 2000 px frame at ~50 m
// The Edit page floats its toolbar over the top of the canvas, and Live its
// option chips; a label drawn this close to the top would sit behind them.
const TOP_OVERLAY_PX = 120;

function projectOnSegment(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y;
  const l2 = dx * dx + dy * dy;
  let t = l2 ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2 : 0;
  t = Math.max(0, Math.min(1, t));
  const q = { x: a.x + t * dx, y: a.y + t * dy };
  return { q, d: Math.hypot(p.x - q.x, p.y - q.y) };
}

/** q moved `inset` off the edge a-b into the interior of `ring`. */
function insetInto(q, a, b, ring, inset) {
  const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const nx = -(b.y - a.y) / len, ny = (b.x - a.x) / len;
  const plus = { x: q.x + nx * inset, y: q.y + ny * inset };
  const minus = { x: q.x - nx * inset, y: q.y - ny * inset };
  if (pointInPolygon(plus, ring)) return plus;
  if (pointInPolygon(minus, ring)) return minus;
  const c = polygonCentroid(ring);
  return Math.hypot(plus.x - c.x, plus.y - c.y) <= Math.hypot(minus.x - c.x, minus.y - c.y) ? plus : minus;
}

/**
 * Where a dragged proxy should sit for cursor `p` (map px).
 * @param {{x:number,y:number}} p cursor
 * @param {Array<{entity_id:string, cords:Array<{x:number,y:number}>}>} rooms the floor's rooms (no-go areas excluded)
 * @param {number|null} scale px per metre (null: an unscaled floor)
 * @param {object|null} prev the snap returned by the previous call in this drag, or null
 * @returns {{point:{x:number,y:number}, snap:null|{room:number, edge:number, a, b, name:string}}}
 */
export function snapToWall(p, rooms, scale, prev) {
  const m = scale || PX_PER_M_FALLBACK;
  const snapD = WALL_SNAP_M * m, releaseD = WALL_RELEASE_M * m, inset = WALL_INSET_M * m;
  const edgeOf = (room, edge) => {
    const ring = rooms[room].cords;
    return { a: ring[edge], b: ring[(edge + 1) % ring.length], ring };
  };
  const place = (room, edge) => {
    const { a, b, ring } = edgeOf(room, edge);
    const { q } = projectOnSegment(p, a, b);
    return { point: insetInto(q, a, b, ring, inset), snap: { room, edge, a, b, name: rooms[room].entity_id } };
  };
  // Candidate walls: the room the cursor is in, else every room.
  const inside = rooms.findIndex((r) => (r.cords || []).length >= 3 && pointInPolygon(p, r.cords));
  let nearest = null;
  rooms.forEach((r, ri) => {
    const ring = r.cords || [];
    if (ring.length < 3 || (inside >= 0 && ri !== inside)) return;
    for (let e = 0; e < ring.length; e++) {
      const { d } = projectOnSegment(p, ring[e], ring[(e + 1) % ring.length]);
      if (!nearest || d < nearest.d) nearest = { room: ri, edge: e, d };
    }
  });
  // Hysteresis: a wall already holding the proxy keeps it until the cursor is
  // well past it, unless another wall of the same room is now closer and
  // within snapping range (turning a corner).
  if (prev && rooms[prev.room] && (rooms[prev.room].cords || []).length > prev.edge) {
    const { a, b } = edgeOf(prev.room, prev.edge);
    const dPrev = projectOnSegment(p, a, b).d;
    const corner = nearest && nearest.room === prev.room && nearest.edge !== prev.edge && nearest.d <= snapD && nearest.d < dPrev;
    if (dPrev <= releaseD && !corner) return place(prev.room, prev.edge);
  }
  if (nearest && nearest.d <= snapD) return place(nearest.room, nearest.edge);
  return { point: p, snap: null };
}

/**
 * A pin marks a vertical line through the house, and the lines people can
 * actually find on every floor's plan are corners - which are already drawn,
 * as room vertices. Landing a pin exactly on one is both easier than aiming
 * and more accurate than a hand can be at plan resolution.
 * Returns the nearest room vertex within `radius` map px, or null.
 */
export function snapToVertex(p, rooms, radius) {
  let best = null, bestD = radius;
  for (const room of rooms || []) {
    for (const q of room.cords || []) {
      const d = Math.hypot(q.x - p.x, q.y - p.y);
      if (d <= bestD) { bestD = d; best = { x: q.x, y: q.y }; }
    }
  }
  return best;
}

const OBJECT_URLS = new Map(); // authenticated image path -> object URL, per page load

async function resolveImageUrl(url, authFetch) {
  if (!authFetch || !url.startsWith("/api/")) return url;
  const cached = OBJECT_URLS.get(url);
  if (cached) return cached;
  const resp = await authFetch(url);
  if (!resp.ok) throw new Error(`map image ${resp.status}`);
  const objectUrl = URL.createObjectURL(await resp.blob());
  OBJECT_URLS.set(url, objectUrl);
  return objectUrl;
}

/** Within this many degrees of horizontal, vertical or 45°, an edge is snapped exact. */
export const ORTHO_SNAP_DEG = 7;

// Unit directions of the edges a plan is drawn with. Screen y points down,
// so d1 (1, 1) runs down-right and d2 (1, -1) up-right.
const R2 = Math.SQRT1_2;
const DIRS = { h: [1, 0], v: [0, 1], d1: [R2, R2], d2: [R2, -R2] };

function edgeClass(dx, dy, tolDeg) {
  // "h", "v", "d1" or "d2" when the edge is within tolDeg of that direction, else null.
  if (Math.hypot(dx, dy) < 1e-9) return null;
  let a = (Math.atan2(dy, dx) * 180) / Math.PI;       // (-180, 180]
  a = ((a % 180) + 180) % 180;                        // an edge has no direction: [0, 180)
  for (const [target, cls] of [[0, "h"], [45, "d1"], [90, "v"], [135, "d2"], [180, "h"]]) {
    if (Math.abs(a - target) <= tolDeg) return cls;
  }
  return null;
}

function nearAxis(dx, dy, tolDeg) {
  const c = edgeClass(dx, dy, tolDeg);
  return c === "h" || c === "v" ? c : null;
}

/** The point on the line through `o` along unit `d` nearest to `p`. */
function project(p, o, d) {
  const s = (p.x - o.x) * d[0] + (p.y - o.y) * d[1];
  return { x: o.x + s * d[0], y: o.y + s * d[1] };
}

/** Where two lines (point + unit direction) cross, or null when parallel. */
function intersect(o1, d1, o2, d2) {
  const den = d1[0] * d2[1] - d1[1] * d2[0];
  if (Math.abs(den) < 1e-9) return null;
  const s = ((o2.x - o1.x) * d2[1] - (o2.y - o1.y) * d2[0]) / den;
  return { x: o1.x + s * d1[0], y: o1.y + s * d1[1] };
}

/**
 * Where a corner should land so its edges to `prev` and `next` come out
 * exact. An edge within ORTHO_SNAP_DEG of horizontal, vertical or 45° is
 * made exactly that; with both edges constrained the corner lands where the
 * two lines cross. Anything further off - a wall at 30° - is left exactly as
 * drawn. Either neighbour may be null (the first corner, or a draft with no
 * closing corner yet).
 */
export function snapCorner(p, prev, next, tolDeg = ORTHO_SNAP_DEG) {
  const lines = [];
  for (const n of [prev, next]) {
    if (!n) continue;
    const cls = edgeClass(p.x - n.x, p.y - n.y, tolDeg);
    if (cls) lines.push({ o: n, d: DIRS[cls] });
  }
  if (!lines.length) return { x: p.x, y: p.y, snapped: false };
  const at = (lines.length === 2 && intersect(lines[0].o, lines[0].d, lines[1].o, lines[1].d)) || project(p, lines[0].o, lines[0].d);
  return { x: at.x, y: at.y, snapped: true };
}

/**
 * The same polygon with every nearly-straight edge made exact.
 *
 * First the horizontal and vertical runs: corners joined by a near-vertical
 * edge share one x (their mean), by a near-horizontal edge one y - across
 * whole runs, so two collinear edges in a row line up rather than stepping.
 * Then the 45° edges: each becomes an exact diagonal through its own
 * midpoint, and its corners move to where it crosses the edge on their
 * other side, which keeps an edge squared in the first pass exactly square.
 * Edges that fit none of the four directions are left alone.
 */
export function squareUp(points, tolDeg = ORTHO_SNAP_DEG + 3) {
  const n = points.length;
  if (n < 3) return points.map((q) => ({ ...q }));
  const groups = (axis) => {
    const parent = [...Array(n).keys()];
    const find = (i) => (parent[i] === i ? i : (parent[i] = find(parent[i])));
    for (let i = 0; i < n; i++) {
      const a = points[i], b = points[(i + 1) % n];
      if (nearAxis(b.x - a.x, b.y - a.y, tolDeg) === axis) parent[find(i)] = find((i + 1) % n);
    }
    const sums = new Map();
    for (let i = 0; i < n; i++) {
      const r = find(i), s = sums.get(r) || { v: 0, c: 0 };
      s.v += axis === "v" ? points[i].x : points[i].y; s.c += 1; sums.set(r, s);
    }
    return (i) => { const s = sums.get(find(i)); return s.c > 1 ? s.v / s.c : null; };
  };
  const vx = groups("v"), hy = groups("h");
  const pts = points.map((q, i) => ({ ...q, x: vx(i) ?? q.x, y: hy(i) ?? q.y }));

  // The line each edge lies on, exact where it has a class.
  const line = (i) => {
    const a = pts[i], b = pts[(i + 1) % n];
    const cls = edgeClass(b.x - a.x, b.y - a.y, tolDeg);
    if (cls === "d1" || cls === "d2") return { o: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, d: DIRS[cls], diag: true };
    const len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    return { o: a, d: [(b.x - a.x) / len, (b.y - a.y) / len], diag: false };
  };
  const lines = pts.map((_q, i) => line(i));
  const out = pts.map((q, i) => {
    const before = lines[(i - 1 + n) % n], after = lines[i];   // the two edges meeting at corner i
    if (!before.diag && !after.diag) return q;
    const hit = intersect(before.o, before.d, after.o, after.d);
    return hit || project(q, (before.diag ? before : after).o, (before.diag ? before : after).d);
  });
  return out.map((q) => ({ ...q, x: Math.round(q.x * 1000) / 1000, y: Math.round(q.y * 1000) / 1000 }));
}

/** Seconds as the shortest honest phrase: "45s", "3m", "2h", "1d". */
export function shortAge(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : s < 86400 ? `${Math.floor(s / 3600)}h` : `${Math.floor(s / 86400)}d`;
}

/** How long since a thing's last fix, and whether that makes it a ghost. */
export function staleness(thing, staleAfter, now = Date.now() / 1000) {
  const age = typeof thing?.updated === "number" ? Math.max(0, now - thing.updated) : 0;
  return { age, ghost: staleAfter > 0 && age > staleAfter };
}

/**
 * Where a thing spent its time: history points binned into square cells of
 * cellM metres per floor, each cell holding the seconds spent in it. A point
 * holds until the next one (history keeps a point only when something
 * changed), but never across a dropout, and no longer than maxHoldSecs, so a
 * thing that went unheard does not pile hours onto its last spot.
 * points: [{t, x, y, f, gap}] in metres, oldest first; `end` closes the last.
 * Returns {floor: {cellM, total, max, cells: [{x, y, secs}]}} (x, y = cell centre, m).
 */
export function heatCells(points, end, cellM = 0.5, maxHoldSecs = 300) {
  const acc = {};
  for (let i = 0; i < points.length; i++) {
    const p = points[i], next = points[i + 1];
    if (p.f == null || !isFinite(p.x) || !isFinite(p.y)) continue;
    const until = next ? (next.gap === 2 ? p.t + Math.min(60, maxHoldSecs) : next.t) : end;
    const secs = Math.max(0, Math.min(until - p.t, maxHoldSecs));
    if (!secs) continue;
    const floor = (acc[p.f] = acc[p.f] || { cellM, total: 0, max: 0, byKey: new Map() });
    const cx = Math.floor(p.x / cellM), cy = Math.floor(p.y / cellM), key = `${cx},${cy}`;
    const cell = floor.byKey.get(key) || { x: (cx + 0.5) * cellM, y: (cy + 0.5) * cellM, secs: 0 };
    cell.secs += secs;
    floor.byKey.set(key, cell);
    floor.total += secs;
    floor.max = Math.max(floor.max, cell.secs);
  }
  const out = {};
  for (const [f, v] of Object.entries(acc)) out[f] = { cellM: v.cellM, total: v.total, max: v.max, cells: [...v.byKey.values()] };
  return out;
}

/** Heat ramp 0..1: blue, through yellow, to red. */
export function heatRgb(w) {
  const stops = [[40, 110, 255], [255, 215, 0], [225, 30, 30]];
  const u = Math.min(1, Math.max(0, w)) * 2, i = Math.min(1, Math.floor(u)), f = u - i;
  return stops[i].map((a, k) => Math.round(a + (stops[i + 1][k] - a) * f));
}

/**
 * A bias ratio as a colour: neutral grey at 1, green below (this floor is
 * favoured less than the other), red above, full strength at 2x either way.
 */
export function biasRgba(ratio) {
  const r = Math.max(1e-3, Number(ratio) || 1);
  const s = Math.min(1, Math.abs(Math.log(r)) / Math.log(2));
  const grey = [140, 140, 140], hue = r < 1 ? [30, 170, 70] : [215, 40, 40];
  return [...grey.map((g, k) => Math.round(g + (hue[k] - g) * s)), Math.round(255 * (0.3 + 0.35 * s))];
}

export class SextantMap {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} host callbacks: onSelect(sel), onChange(kind, item), onDrawPoint(pt),
   *                 onHover(sel), onContextMenu(sel, event); colors: getColor(name)
   */
  constructor(canvas, host = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.host = host;
    this.floor = null;
    this.image = null;
    this.imageUrl = null;
    this.things = [];
    this.trails = new Map();
    this.offline = new Set();
    this.marks = [];   // location pins of the focused thing on this floor: [{x, y, label}]
    this.heat = null;  // where the focused thing has been: {size, max, cells: [{x, y, secs}]} in map px
    this.dark = false;      // set from the page's own background before each draw
    this.areaIcons = {};  // Home Assistant area id -> its mdi icon, for rooms linked to an area
    this.biasMap = null;  // this floor's election prior against another's: {size, cells: [[x, y, ratio]]} in map px
    this.suggestions = [];  // advised proxy spots on this floor: [{x, y, label}]
    // staleAfter: seconds without a fix after which a thing is drawn as a ghost (0 = never).
    this.options = { circles: false, trails: true, fingerprint: false, grid: "off", labels: true, subzones: true, image: true, focus: null, staleAfter: 120 };
    this.authFetch = host.fetch || null; // (url) => Promise<Response>, e.g. hass.fetchWithAuth
    this.locks = { zone: false, subzone: false, receiver: false, pin: false }; // edit mode: locked kinds cannot be selected or dragged
    this.mode = "view";
    this.tool = "select";
    this.selection = null; // {kind:'receiver'|'zone'|'subzone'|'pin'|'thing', index, vertex?}
    this.pinGhosts = [];
    this.radarInfo = {};     // device_id -> {name, range_m, fov_deg}: how far each radar sees
    this.radarTargets = [];  // mmWave targets on this floor: [{cords, thing, room, radar_name}]
    this.radarLive = null;   // the Edit page's selected radar, live: {radar_id, targets: [{index, cords}]}
    this.hover = null;
    this.draft = null; // points of a polygon being drawn
    this.view = { k: 1, tx: 0, ty: 0 };
    this._fitted = false;
    this._drag = null;
    this._pointers = new Map();   // pointers down now, by id: two make a pinch
    this._raf = 0;
    this._bind();
    this._resize = new ResizeObserver(() => this._onResize());
    this._resize.observe(canvas);
    this._onResize();
  }

  destroy() {
    this._resize.disconnect();
    cancelAnimationFrame(this._raf);
    this._raf = 0;
    for (const [type, fn, opts] of this._listeners || []) this.canvas.removeEventListener(type, fn, opts);
    this._listeners = [];
    this._planCache = null;
  }

  // --- data ------------------------------------------------------------------

  setFloor(floor, imageUrl) {
    const changed = !this.floor || this.floor !== floor || imageUrl !== this.imageUrl;
    this.floor = floor;
    if (imageUrl !== this.imageUrl) {
      this.imageUrl = imageUrl;
      this.image = null;
      this._fitted = false;
      if (imageUrl) {
        const img = new Image();
        img.onload = () => { if (this.imageUrl === imageUrl) { this.image = img; this._fitted = false; this.invalidate(); } };
        img.onerror = () => this.invalidate();
        // Floor plans come from an authenticated API path, which an <img>
        // cannot send a token to: fetch them with the host's authenticated
        // fetch and hand the image a local object URL instead.
        resolveImageUrl(imageUrl, this.authFetch).then((src) => { if (this.imageUrl === imageUrl) img.src = src; }).catch(() => this.invalidate());
      }
    }
    if (changed) { this.selection = null; this.draft = null; }
    this.invalidate();
  }

  setThings(rows) { this.things = rows || []; this.invalidate(); }
  setTrail(ent, points) { if (points) this.trails.set(ent, points); else this.trails.delete(ent); this.invalidate(); }
  clearTrails() { this.trails.clear(); this.invalidate(); }
  setOffline(slugs) { this.offline = new Set(slugs || []); this.invalidate(); }
  setMarks(list) { this.marks = list || []; this.invalidate(); }
  setHeat(heat) { this.heat = heat?.cells?.length ? heat : null; this._heatImg = null; this.invalidate(); }
  /** hass.areas ({area_id: {icon}}): a room linked to an area shows that area's icon in its label. */
  setAreas(areas) {
    const next = {};
    for (const [id, a] of Object.entries(areas || {})) if (a?.icon) next[id] = a.icon;
    if (JSON.stringify(next) !== JSON.stringify(this.areaIcons)) { this.areaIcons = next; this.invalidate(); }
  }
  setBiasMap(m) { this.biasMap = m?.cells?.length ? m : null; this._biasImg = null; this.invalidate(); }
  setSuggestions(list) { this.suggestions = list || []; this.invalidate(); }
  setOptions(opts) { Object.assign(this.options, opts); this.invalidate(); }
  setMode(mode) { this.mode = mode; if (mode !== "edit") { this.draft = null; this.tool = "select"; } this.invalidate(); }
  setTool(tool) { this.tool = tool; this.draft = tool === "select" ? null : this.draft; this.invalidate(); }
  setSelection(sel) { this.selection = sel; this.invalidate(); }
  setLocks(locks) { Object.assign(this.locks, locks || {}); if (this.selection && this.locks[this.selection.kind]) this.selection = null; this.invalidate(); }
  finishDraft() {
    const pts = this.draft;
    this.draft = null; this._drawHover = null;
    this.invalidate();
    return pts && pts.length >= 3 ? pts : null;
  }
  cancelDraft() { this.draft = null; this._drawHover = null; this.invalidate(); }

  // --- view --------------------------------------------------------------------

  _onResize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width)), h = Math.max(1, Math.round(rect.height));
    if (this.canvas.width !== w * dpr || this.canvas.height !== h * dpr) {
      this.canvas.width = w * dpr; this.canvas.height = h * dpr;
      this._fitted = false;
    }
    this.invalidate();
  }

  fit() {
    const rect = this.canvas.getBoundingClientRect();
    const size = this._mapSize();
    if (!size.w || !size.h || !rect.width) return;
    const k = Math.min(rect.width / size.w, rect.height / size.h) * 0.96;
    this.view = { k, tx: (rect.width - size.w * k) / 2, ty: (rect.height - size.h * k) / 2 };
    this._fitted = true;
    this.invalidate();
  }

  /** Zoom so these map points fill the view, with a margin: a spot fills a phone screen. */
  zoomTo(points, margin = 0.2) {
    const rect = this.canvas.getBoundingClientRect();
    if (!points?.length || !rect.width) return;
    const xs = points.map((q) => q.x), ys = points.map((q) => q.y);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const w = Math.max(x1 - x0, 10), h = Math.max(y1 - y0, 10);
    const k = Math.max(0.05, Math.min(MAX_ZOOM, Math.min(rect.width / w, rect.height / h) * (1 - margin)));
    this.view = { k, tx: rect.width / 2 - ((x0 + x1) / 2) * k, ty: rect.height / 2 - ((y0 + y1) / 2) * k };
    this._fitted = true;
    this.invalidate();
  }

  // The layout's coordinate frame is NOT the image's natural pixels: the
  // original editor drew every floor image onto a canvas normalised to
  // MAP_FRAME_WIDTH pixels wide (height by aspect ratio) and stored
  // coordinates in that frame, and every saved layout depends on it.
  _mapSize() {
    if (this.image) return { w: MAP_FRAME_WIDTH, h: MAP_FRAME_WIDTH * (this.image.naturalHeight / this.image.naturalWidth) };
    // No image yet: size to the content so an image-less floor still renders.
    let maxX = 0, maxY = 0;
    const f = this.floor || {};
    for (const r of [...(f.receivers || []), ...(f.pins || []), ...(f.remarks || []), ...(f.radars || [])]) { maxX = Math.max(maxX, r.cords?.x || 0); maxY = Math.max(maxY, r.cords?.y || 0); }
    for (const list of [f.zones || [], f.subzones || []]) for (const z of list) for (const p of z.cords || []) { maxX = Math.max(maxX, p.x); maxY = Math.max(maxY, p.y); }
    return { w: maxX ? maxX * 1.05 : 1000, h: maxY ? maxY * 1.05 : 700 };
  }

  toScreen(p) { return { x: p.x * this.view.k + this.view.tx, y: p.y * this.view.k + this.view.ty }; }
  toMap(p) { return { x: (p.x - this.view.tx) / this.view.k, y: (p.y - this.view.ty) / this.view.k }; }

  // --- input --------------------------------------------------------------------

  _bind() {
    const c = this.canvas;
    c.style.touchAction = "none";
    // Kept so destroy() can take them off again: a card that reconnects
    // builds a new map on the same canvas.
    this._listeners = [
      ["pointerdown", (e) => this._down(e)],
      ["pointermove", (e) => this._move(e)],
      ["pointerup", (e) => this._up(e)],
      ["pointercancel", (e) => this._up(e)],
      ["wheel", (e) => this._wheel(e), { passive: false }],
      ["dblclick", (e) => this._dblclick(e)],
      ["contextmenu", (e) => { e.preventDefault(); const hit = this.hitTest(this._local(e)); if (this.host.onContextMenu) this.host.onContextMenu(hit, e); }],
    ];
    for (const [type, fn, opts] of this._listeners) c.addEventListener(type, fn, opts);
  }

  _local(e) { const r = this.canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }

  _down(e) {
    const p = this._local(e);
    this.canvas.setPointerCapture(e.pointerId);
    // Two fingers pinch: zoom about the point between them, and pan with it.
    this._pointers.set(e.pointerId, p);
    if (this._pointers.size === 2) { this._startPinch(); return; }
    if (this._pointers.size > 2) return;
    // Placing something (a location pin): the tap counts when the finger lifts
    // without moving, so a pan or a pinch never places it by accident.
    if (this.mode !== "edit" && e.button === 0 && this.host.isPlacing?.()) {
      this._drag = { kind: "pan", start: p, view: { ...this.view }, moved: false, slop: e.pointerType === "touch" ? 10 : 3, place: this.toMap(p) };
      return;
    }
    const hit = this.hitTest(p);
    if (this.mode === "edit" && this.tool !== "select" && e.button === 0) {
      // Drawing: each click adds a vertex; clicking the first vertex closes.
      const m = this.toMap(p);
      if (this.draft && this.draft.length >= 3) {
        const first = this.toScreen(this.draft[0]);
        if (Math.hypot(first.x - p.x, first.y - p.y) < HIT_SLOP * 1.5) { if (this.host.onDrawClose) this.host.onDrawClose(); return; }
      }
      this.draft = this.draft || [];
      const at = e.altKey ? m : snapCorner(m, this.draft[this.draft.length - 1] || null, this.draft.length >= 2 ? this.draft[0] : null);
      this.draft.push({ x: Math.round(at.x * 1000) / 1000, y: Math.round(at.y * 1000) / 1000 });
      this._draftPush = performance.now();
      if (this.host.onDrawPoint) this.host.onDrawPoint(this.draft);
      this.invalidate();
      return;
    }
    if (this.mode === "edit" && hit && e.button === 0 && hit.kind !== "thing") {
      this.selection = hit;
      if (this.host.onSelect) this.host.onSelect(hit);
      const m = this.toMap(p);
      this._drag = { kind: "item", hit, start: m, moved: false, origin: this._itemPoints(hit) };
      if (this.host.onDragStart) this.host.onDragStart(hit);
      this.invalidate();
      return;
    }
    if (hit && e.button === 0 && this.mode !== "edit" && (hit.kind === "thing" || hit.kind === "receiver")) {
      // A proxy is worth a click outside the editor too: the host shows what
      // it is and what it is doing.
      this.selection = hit.kind === "thing" ? hit : null;
      if (this.host.onSelect) this.host.onSelect(hit);
    } else if (this.mode !== "edit" && this.host.onSelect && (!hit || e.button === 0)) {
      // Outside the editor a room or a spot is not something to select, so a
      // click on one means the same as a click on the floor: nothing here.
      this.selection = null;
      this.host.onSelect(null);
    } else if (this.mode === "edit" && !hit) {
      this.selection = null;
      if (this.host.onSelect) this.host.onSelect(null);
    }
    this._drag = { kind: "pan", start: p, view: { ...this.view }, moved: false, slop: e.pointerType === "touch" ? 10 : 3 };
  }

  /** A second finger landed: undo what the first one started, then pinch. */
  _startPinch() {
    const d = this._drag;
    if (d?.kind === "item" && d.moved) {
      const f = this.floor, hit = d.hit;
      if (hit.kind === "receiver") f.receivers[hit.index].cords = { ...d.origin[0] };
      else if (hit.kind === "pin") f.pins[hit.index].cords = { ...d.origin[0] };
      else if (hit.kind === "remark") f.remarks[hit.index].cords = { ...d.origin[0] };
      else if (hit.kind === "radar") f.radars[hit.index].cords = { ...d.origin[0] };
      else (hit.kind === "zone" ? f.zones : f.subzones)[hit.index].cords = d.origin.map((q) => ({ ...q }));
    }
    // The first finger of a pinch is not a corner.
    if (this.draft?.length && this._draftPush && performance.now() - this._draftPush < 600) {
      this.draft.pop();
      if (!this.draft.length) this.draft = null;
      this._draftPush = 0;
      if (this.host.onDrawPoint) this.host.onDrawPoint(this.draft || []);
    }
    const [a, b] = [...this._pointers.values()];
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    this._drag = { kind: "pinch", dist: Math.max(Math.hypot(a.x - b.x, a.y - b.y), 1), view: { ...this.view }, anchor: this.toMap(mid), moved: true };
    this.invalidate();
  }

  _itemPoints(hit) {
    const f = this.floor;
    if (hit.kind === "receiver") { const r = f.receivers[hit.index]; return [{ x: r.cords.x, y: r.cords.y }]; }
    if (hit.kind === "pin") { const q = f.pins[hit.index]; return [{ x: q.cords.x, y: q.cords.y }]; }
    if (hit.kind === "remark") { const q = f.remarks[hit.index]; return [{ x: q.cords.x, y: q.cords.y }]; }
    if (hit.kind === "radar") { const q = f.radars[hit.index]; return [{ x: q.cords.x, y: q.cords.y }]; }
    const list = hit.kind === "zone" ? f.zones : f.subzones;
    return (list[hit.index].cords || []).map((q) => ({ x: q.x, y: q.y }));
  }

  _move(e) {
    const p = this._local(e);
    if (this._pointers.has(e.pointerId)) this._pointers.set(e.pointerId, p);
    if (this._drag?.kind === "pinch") {
      if (this._pointers.size < 2) return;
      const d = this._drag, [a, b] = [...this._pointers.values()];
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const k = Math.max(0.05, Math.min(MAX_ZOOM, d.view.k * Math.hypot(a.x - b.x, a.y - b.y) / d.dist));
      this.view = { k, tx: mid.x - d.anchor.x * k, ty: mid.y - d.anchor.y * k };
      this._fitted = true;
      this.invalidate();
      return;
    }
    if (!this._drag && this.mode === "edit" && this.tool !== "select" && this.draft && this.draft.length) {
      // The next corner, where a click would put it: snapped, so a right
      // angle is visible before it is committed.
      const m = this.toMap(p);
      this._drawHover = e.altKey ? m : snapCorner(m, this.draft[this.draft.length - 1], this.draft.length >= 2 ? this.draft[0] : null);
      this.invalidate();
    } else if (this._drawHover) {
      this._drawHover = null;
    }
    if (!this._drag) {
      const hit = this.hitTest(p);
      const key = hit ? `${hit.kind}:${hit.index}:${hit.vertex ?? ""}` : "";
      if (key !== this._hoverKey) {
        this._hoverKey = key; this.hover = hit;
        this.canvas.style.cursor = hit ? (this.mode === "edit" ? "move" : "pointer") : (this.mode === "edit" && this.tool !== "select" ? "crosshair" : "grab");
        if (this.host.onHover) this.host.onHover(hit);
        this.invalidate();
      }
      return;
    }
    const d = this._drag;
    if (d.kind === "pan") {
      const dx = p.x - d.start.x, dy = p.y - d.start.y;
      if (Math.abs(dx) + Math.abs(dy) > (d.slop ?? 2)) d.moved = true;
      this.view = { k: d.view.k, tx: d.view.tx + dx, ty: d.view.ty + dy };
      this.invalidate();
      return;
    }
    const m = this.toMap(p);
    const dx = m.x - d.start.x, dy = m.y - d.start.y;
    if (Math.abs(dx) + Math.abs(dy) > 0.5) d.moved = true;
    const f = this.floor, hit = d.hit;
    if (hit.kind === "receiver") {
      const raw = { x: d.origin[0].x + dx, y: d.origin[0].y + dy };
      if (this.options.wallSnap !== false && !e.altKey) {
        const rooms = (f.zones || []).filter((z) => !z.no_go && (z.cords || []).length >= 3);
        const res = snapToWall(raw, rooms, f.scale, d.snap);
        d.snap = res.snap;
        this._snap = res.snap;
        f.receivers[hit.index].cords = res.point;
      } else {
        d.snap = null;
        this._snap = null;
        f.receivers[hit.index].cords = raw;
      }
    } else if (hit.kind === "pin") {
      const raw = { x: d.origin[0].x + dx, y: d.origin[0].y + dy };
      // Onto a room corner when one is near; Alt places it freely.
      const corner = e.altKey ? null : snapToVertex(raw, f.zones, (HIT_SLOP * 1.5) / this.view.k);
      f.pins[hit.index].cords = corner || raw;
      this._pinSnap = corner;
    } else if (hit.kind === "remark") {
      // A note has to be caught here: the branch below is rooms-or-spots, and
      // a note used to fall into it and have its one point written over the
      // spot at the same index. That is how two bedside tables lost every
      // corner but one and vanished from the plan (2026-09-22).
      f.remarks[hit.index].cords = { x: d.origin[0].x + dx, y: d.origin[0].y + dy };
    } else if (hit.kind === "radar") {
      // Caught for the same reason as a note: the branch below is rooms-or-spots.
      f.radars[hit.index].cords = { x: d.origin[0].x + dx, y: d.origin[0].y + dy };
    } else {
      const list = hit.kind === "zone" ? f.zones : f.subzones;
      const item = list[hit.index];
      if (hit.vertex != null) {
        const raw = { x: d.origin[hit.vertex].x + dx, y: d.origin[hit.vertex].y + dy };
        const cnt = item.cords.length;
        const at = e.altKey ? raw : snapCorner(raw, item.cords[(hit.vertex - 1 + cnt) % cnt], item.cords[(hit.vertex + 1) % cnt]);
        item.cords[hit.vertex] = { x: at.x, y: at.y };
      } else if (hit.edge != null) {
        // Dragging an edge midpoint inserts a vertex there, then drags it.
        const at = hit.edge + 1;
        item.cords.splice(at, 0, { x: m.x, y: m.y });
        d.hit = { ...hit, edge: undefined, vertex: at };
        d.origin = item.cords.map((q) => ({ x: q.x, y: q.y }));
        d.origin[at] = { x: m.x - dx, y: m.y - dy };
        this.selection = d.hit;
      } else {
        item.cords = d.origin.map((q) => ({ x: q.x + dx, y: q.y + dy }));
      }
    }
    this.invalidate();
  }

  _up(e) {
    this._pointers.delete(e.pointerId);
    // Lifting one finger of a pinch ends it; the other does not start a pan.
    if (this._drag?.kind === "pinch") { if (this._pointers.size < 2) { this._drag = null; this.lastDragMoved = true; } return; }
    const d = this._drag;
    this._drag = null;
    if (d?.kind === "pan" && d.place && !d.moved && e.type === "pointerup" && this.host.onMapClick) this.host.onMapClick(d.place);
    this._snap = null;
    this._pinSnap = null;
    this.lastDragMoved = !!(d && d.moved);
    if (!d) return;
    if (d.kind === "item" && d.moved) {
      const f = this.floor, hit = d.hit;
      const round = (q) => ({ x: Math.round(q.x * 1000) / 1000, y: Math.round(q.y * 1000) / 1000 });
      if (hit.kind === "receiver") f.receivers[hit.index].cords = round(f.receivers[hit.index].cords);
      else if (hit.kind === "pin") f.pins[hit.index].cords = round(f.pins[hit.index].cords);
      else if (hit.kind === "remark") f.remarks[hit.index].cords = round(f.remarks[hit.index].cords);
      else if (hit.kind === "radar") f.radars[hit.index].cords = round(f.radars[hit.index].cords);
      else { const list = hit.kind === "zone" ? f.zones : f.subzones; list[hit.index].cords = list[hit.index].cords.map(round); }
      if (this.host.onChange) this.host.onChange(hit.kind, hit.index);
    }
    this.invalidate();
  }

  /** Zoom by a factor about the centre of the view, for the + and − buttons -
   * the same maths as the wheel, anchored where the eye already is. */
  zoomBy(factor) {
    const rect = this.canvas.getBoundingClientRect();
    const p = { x: rect.width / 2, y: rect.height / 2 };
    const k = Math.max(0.05, Math.min(MAX_ZOOM, this.view.k * factor));
    const m = this.toMap(p);
    this.view = { k, tx: p.x - m.x * k, ty: p.y - m.y * k };
    this._fitted = true;
    this.invalidate();
  }

  _wheel(e) {
    e.preventDefault();
    const p = this._local(e);
    const factor = Math.exp(-e.deltaY * 0.0015);
    const k = Math.max(0.05, Math.min(MAX_ZOOM, this.view.k * factor));
    const m = this.toMap(p);
    this.view = { k, tx: p.x - m.x * k, ty: p.y - m.y * k };
    this._fitted = true;
    this.invalidate();
  }

  _dblclick(e) {
    if (this.mode === "edit" && this.draft && this.draft.length >= 3) { if (this.host.onDrawClose) this.host.onDrawClose(); return; }
    this.fit();
  }

  // --- hit testing -----------------------------------------------------------

  hitTest(p) {
    const f = this.floor;
    if (!f) return null;
    const m = this.toMap(p);
    const slop = HIT_SLOP / this.view.k;
    if (this.mode !== "edit") {
      for (let i = this.things.length - 1; i >= 0; i--) {
        const t = this.things[i];
        if (!t.cords) continue;
        if (Math.hypot(t.cords[0] - m.x, t.cords[1] - m.y) <= (THING_RADIUS + 4) / this.view.k) return { kind: "thing", index: i, ent: t.ent };
      }
    }
    const edit = this.mode === "edit";
    if (edit) {
      // Notes before everything: a note is deliberately placed ON the thing it
      // is about - the proxy that is going here, the spot to check - so it has
      // to be reachable there. The radius is tight, and a note is the one
      // thing on the plan that can be dragged aside without consequence.
      for (let i = (f.remarks || []).length - 1; i >= 0; i--) {
        const q = f.remarks[i].cords;
        if (q && Math.hypot(q.x - m.x, q.y - m.y) <= REMARK_SIZE / this.view.k) return { kind: "remark", index: i, id: f.remarks[i].remark_id };
      }
      for (let i = (f.radars || []).length - 1; i >= 0; i--) {
        const q = f.radars[i].cords;
        if (q && Math.hypot(q.x - m.x, q.y - m.y) <= (RADAR_SIZE * 1.3) / this.view.k) return { kind: "radar", index: i, id: f.radars[i].radar_id };
      }
    }
    if (edit && !this.locks.pin) {
      // Pins first: they sit on corners, where walls and proxies also are,
      // and a pin under a proxy would otherwise be unreachable. The reverse
      // is what the Pins padlock is for: lock them and the proxy is reachable.
      for (let i = (f.pins || []).length - 1; i >= 0; i--) {
        const q = f.pins[i].cords;
        if (q && Math.hypot(q.x - m.x, q.y - m.y) <= slop + PIN_SIZE / this.view.k) return { kind: "pin", index: i, id: f.pins[i].name };
      }
    }
    const rs = ((edit ? RECEIVER_SIZE_EDIT : RECEIVER_SIZE) + (edit ? 6 : 0)) / this.view.k;
    if (!(edit && this.locks.receiver)) {
      for (let i = (f.receivers || []).length - 1; i >= 0; i--) {
        const r = f.receivers[i];
        if (r.cords && Math.abs(r.cords.x - m.x) <= slop + rs && Math.abs(r.cords.y - m.y) <= slop + rs) return { kind: "receiver", index: i, id: r.entity_id };
      }
    }
    if (this.mode === "edit") {
      // Vertices and edge midpoints of the selected polygon first.
      const sel = this.selection;
      if (sel && (sel.kind === "zone" || sel.kind === "subzone")) {
        const list = sel.kind === "zone" ? f.zones : f.subzones;
        const item = list[sel.index];
        if (item) {
          const pts = item.cords || [];
          for (let v = 0; v < pts.length; v++) if (Math.hypot(pts[v].x - m.x, pts[v].y - m.y) <= slop + VERTEX_SIZE / this.view.k) return { kind: sel.kind, index: sel.index, id: item.entity_id, vertex: v };
          for (let v = 0; v < pts.length; v++) {
            const a = pts[v], b = pts[(v + 1) % pts.length];
            const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
            if (Math.hypot(mid.x - m.x, mid.y - m.y) <= slop + VERTEX_SIZE / this.view.k) return { kind: sel.kind, index: sel.index, id: item.entity_id, edge: v };
          }
        }
      }
    }
    const subs = this.options.subzones && !(edit && this.locks.subzone) ? f.subzones || [] : [];
    for (let i = subs.length - 1; i >= 0; i--) if ((subs[i].cords || []).length >= 3 && pointInPolygon(m, subs[i].cords)) return { kind: "subzone", index: i, id: subs[i].entity_id };
    if (edit && this.locks.zone) return null;
    for (let i = (f.zones || []).length - 1; i >= 0; i--) if ((f.zones[i].cords || []).length >= 3 && pointInPolygon(m, f.zones[i].cords)) return { kind: "zone", index: i, id: f.zones[i].entity_id };
    return null;
  }

  // --- drawing ----------------------------------------------------------------

  invalidate() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
  }

  draw() {
    const ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    if (!this._fitted) this.fit();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    const f = this.floor;
    if (!f) return;
    // Per-frame collections: labels are placed together at the end so they can
    // dodge each other, and the marks let a covered proxy be redrawn on top.
    this._labels = [];
    this._proxyMarks = [];
    this._thingMarks = [];
    const v = this.view;
    ctx.save();
    ctx.translate(v.tx, v.ty);
    ctx.scale(v.k, v.k);
    const size = this._mapSize();
    this._readTheme();
    ctx.fillStyle = this._css("--sextant-map-bg", "#ffffff");
    ctx.fillRect(0, 0, size.w, size.h);
    if (this.image && this.options.image !== false) {
      // A floor plan is black on white. On a dark theme that is a lamp in a
      // dark room, so it is inverted to white on black; the hue rotation puts
      // any colour in the drawing back where it was, and the walls are taken
      // down to a grey that reads as a drawing rather than a light source.
      if (this.dark) {
        ctx.save();
        ctx.globalAlpha = 0.85;
        ctx.drawImage(this._darkPlan(), 0, 0, size.w, size.h);
        ctx.restore();
      } else {
        ctx.drawImage(this.image, 0, 0, size.w, size.h);
      }
    }
    this._drawGrid(ctx, size);
    this._drawPolygons(ctx, f.zones || [], "zone");
    if (this.options.subzones) this._drawPolygons(ctx, f.subzones || [], "subzone");
    if (this.biasMap) this._drawBiasMap(ctx);
    if (this.heat && this.mode !== "edit") this._drawHeat(ctx);
    this._drawDraft(ctx);
    if (this._snap) this._drawSnap(ctx);
    // "Proxies" off hides them entirely: a plan with dozens of them is busy,
    // and most of the time you are looking at the things, not the proxies.
    if (this.options.receivers !== false || this.mode === "edit") this._drawReceivers(ctx, f.receivers || []);
    if (this.mode === "edit") this._drawPins(ctx, f.pins || []);
    // Notes draw in both modes: one is written while planning and read while
    // standing in the room with the Live page open, which is the whole point.
    this._drawRemarks(ctx, f.remarks || []);
    if (this.mode === "edit") this._drawRadars(ctx, f.radars || []);
    this._drawRadarTargets(ctx);
    if (this.suggestions.length) {
      // Everything labelled so far belongs to the plan, so it goes down before
      // the scrim and fades with it. Labels queued after this line are the
      // advice, and land on top.
      this._flushLabels(ctx);
      // A house plan is busy: walls, room fills, dozens of proxies. Fade all
      // of it back behind a scrim of the page's own background so the advised
      // spots drawn next are the only thing at full strength - the plan stays
      // legible underneath as a ghost, for working out where the spot is.
      ctx.save();
      ctx.globalAlpha = 0.78;
      ctx.fillStyle = this._css("--sextant-map-bg", "#ffffff");
      ctx.fillRect(0, 0, size.w, size.h);
      ctx.restore();
      this._drawSuggestions(ctx);
    }
    if (this.mode !== "edit") { this._drawThings(ctx); this._drawMarks(ctx); }
    this._drawProxyPeeks(ctx);
    this._flushLabels(ctx);
    ctx.restore();
  }

  /** The floor plan with the dark-theme filter already applied, made once per
   * image: filtering the whole plan on every frame is most of a frame's cost. */
  _darkPlan() {
    const img = this.image;
    if (this._planCache?.image === img) return this._planCache.canvas;
    const w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
    if (!w || !h) return img;
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const cx = c.getContext("2d");
    if (!cx) return img;
    cx.filter = "invert(1) hue-rotate(180deg) brightness(0.62)";
    cx.drawImage(img, 0, 0);
    this._planCache = { image: img, canvas: c };
    return c;
  }

  /** Whether the page this map is drawn on is dark, from its own background:
   * the theme is the user's, and the map follows it rather than a switch.
   * Hex and rgb() are what a theme resolves to; anything else reads light. */
  _readTheme() {
    const bg = String(this._css("--sextant-map-bg", "#ffffff")).trim();
    let rgb = null;
    const hex = /^#([0-9a-f]{3,8})$/i.exec(bg);
    if (hex) {
      const h = hex[1].length <= 4 ? [...hex[1]].map((c) => c + c).join("") : hex[1];
      rgb = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    } else {
      const fn = /^rgba?\(([^)]+)\)$/i.exec(bg);
      if (fn) rgb = fn[1].split(/[ ,/]+/).slice(0, 3).map((v) => parseFloat(v));
    }
    if (!rgb || rgb.some((v) => !Number.isFinite(v))) { this.dark = bg.toLowerCase() === "black"; return; }
    const [r, g, b] = rgb;
    this.dark = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5;
  }

  _css(name, fallback) {
    const value = getComputedStyle(this.canvas).getPropertyValue(name).trim();
    return value || fallback;
  }

  _drawGrid(ctx, size) {
    const unit = this.options.grid;
    const scale = this.floor.scale;
    if (!unit || unit === "off" || !scale) return;
    const step = unit === "ft" ? scale * 0.3048 : scale;
    ctx.save();
    ctx.strokeStyle = "rgba(120,120,120,0.25)";
    ctx.lineWidth = 1 / this.view.k;
    ctx.beginPath();
    for (let x = 0; x <= size.w; x += step) { ctx.moveTo(x, 0); ctx.lineTo(x, size.h); }
    for (let y = 0; y <= size.h; y += step) { ctx.moveTo(0, y); ctx.lineTo(size.w, y); }
    ctx.stroke();
    ctx.restore();
  }

  _drawPolygons(ctx, list, kind) {
    const k = this.view.k;
    list.forEach((item, index) => {
      const pts = item.cords || [];
      if (pts.length < 2) return;
      const selected = this.selection && this.selection.kind === kind && this.selection.index === index;
      const hovered = this.hover && this.hover.kind === kind && this.hover.index === index;
      const noGo = !!item.no_go;
      const hue = kind === "subzone" ? null : thingHue(item.entity_id || "");
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      if (pts.length >= 3) ctx.closePath();
      if (kind === "subzone") {
        ctx.fillStyle = (item.color || "hsl(175,70%,45%)").replace(/\)$/, ", 0.22)").replace("hsl(", "hsla(");
        ctx.strokeStyle = item.color || "hsl(175,70%,45%)";
      } else if (noGo) {
        // A void is read at a glance, not studied: the dashed outline and a
        // wash say "nothing here" without the heavy fill and close hatch that
        // used to bury a stairwell's worth of plan under it.
        ctx.fillStyle = "rgba(110,110,110,0.14)";
        ctx.strokeStyle = "rgba(70,70,70,0.75)";
      } else {
        ctx.fillStyle = `hsla(${hue}, 60%, 55%, ${selected || hovered ? 0.28 : 0.16})`;
        ctx.strokeStyle = `hsla(${hue}, 60%, 40%, 0.9)`;
      }
      ctx.lineWidth = (selected ? 3 : hovered ? 2 : 1.25) / k;
      if (noGo) ctx.setLineDash([6 / k, 4 / k]);
      ctx.fill();
      ctx.stroke();
      ctx.setLineDash([]);
      if (noGo) this._hatch(ctx, pts);
      if (this.options.labels && item.entity_id) {
        const c = polygonCentroid(pts);
        const icon = kind === "zone" ? this.areaIcons[item.area_id] : null;
        this._label(ctx, item.entity_id, c.x, c.y, kind === "subzone" ? 11 : 13, kind === "subzone" ? 0.75 : 0.9,
          icon ? mdiPath(icon, () => this.invalidate()) : null, LABEL_PRIO.place);
      }
      if (this.mode === "edit" && selected) {
        for (let v = 0; v < pts.length; v++) this._handle(ctx, pts[v], VERTEX_SIZE / k, "#ffffff", ctx.strokeStyle);
        for (let v = 0; v < pts.length; v++) {
          const a = pts[v], b = pts[(v + 1) % pts.length];
          this._handle(ctx, { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, VERTEX_SIZE * 0.6 / k, "rgba(255,255,255,0.6)", ctx.strokeStyle);
        }
      }
    });
  }

  _hatch(ctx, pts) {
    ctx.save();
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.closePath();
    ctx.clip();
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    ctx.strokeStyle = "rgba(60,60,60,0.16)";
    ctx.lineWidth = 1 / this.view.k;
    ctx.beginPath();
    const step = 30 / this.view.k;
    for (let x = minX - (maxY - minY); x < maxX; x += step) { ctx.moveTo(x, maxY); ctx.lineTo(x + (maxY - minY), minY); }
    ctx.stroke();
    ctx.restore();
  }

  _handle(ctx, p, r, fill, stroke) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fillStyle = fill; ctx.fill();
    ctx.lineWidth = 1.5 / this.view.k; ctx.strokeStyle = stroke; ctx.stroke();
  }

  /** A label on its white plate; `glyph` (a 24-unit MDI Path2D) sits before the text. */
  /** Queue a label rather than paint it. Every label of a frame is placed
   * together in _flushLabels, so they can dodge each other; painting here
   * would make that impossible. The ambient alpha of whatever queued the
   * label (a faded thing, a locked proxy) is folded in now, because by flush
   * time that ctx.save() block is long gone.
   *
   * `prio` decides who gets first refusal on a crowded spot: the things you
   * are looking at outrank the furniture they are sitting on. */
  _label(ctx, text, x, y, px, alpha = 0.9, glyph = null, prio = LABEL_PRIO.other) {
    (this._labels ||= []).push({
      text, x, y, px, glyph, prio,
      alpha: alpha * ctx.globalAlpha,
      order: this._labels.length,
    });
  }

  /** Place and paint the frame's labels so that no two sit on top of each
   * other. Markers never move - a marker is a measurement - so it is the
   * labels that give way: each tries its anchor, then steps below and above
   * it, and is dropped if it can find nowhere clear. Two labels printed over
   * one another are worse than one label and a marker you can click. */
  _flushLabels(ctx) {
    const queue = this._labels || [];
    this._labels = [];
    if (!queue.length) return;
    const k = this.view.k;
    // Measuring needs the canvas; deciding where things go does not, so the
    // decision lives in placeLabels where it can be tested without one.
    const measured = queue.map((L) => {
      const size = L.px / k, pad = 4 / k;
      ctx.font = `600 ${size}px system-ui, sans-serif`;
      const gs = L.glyph ? size * 1.15 : 0, gap = L.glyph ? size * 0.3 : 0;
      const total = ctx.measureText(L.text).width + gs + gap;
      return { ...L, m: { size, pad, total, gs, gap },
               w: total + pad * 2, h: size + pad, gap: 2 / k };
    });
    for (const { label, y } of placeLabels(measured)) this._paintLabel(ctx, label, y, label.m);
  }

  _paintLabel(ctx, L, y, m) {
    const plate = this.dark ? "18,22,28" : "255,255,255";
    const ink = this.dark ? "235,240,246" : "20,24,32";
    ctx.font = `600 ${m.size}px system-ui, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = `rgba(${plate},${L.alpha * 0.85})`;
    ctx.fillRect(L.x - m.total / 2 - m.pad, y - m.size / 2 - m.pad / 2, m.total + m.pad * 2, m.size + m.pad);
    ctx.fillStyle = `rgba(${ink},${L.alpha})`;
    if (L.glyph) {
      ctx.save();
      ctx.translate(L.x - m.total / 2, y - m.gs / 2);
      ctx.scale(m.gs / 24, m.gs / 24);
      ctx.fill(L.glyph);
      ctx.restore();
    }
    ctx.fillText(L.text, L.x + (m.gs + m.gap) / 2, y);
  }

  _drawReceivers(ctx, receivers) {
    const k = this.view.k, edit = this.mode === "edit";
    const base = edit ? RECEIVER_SIZE_EDIT : RECEIVER_SIZE;
    receivers.forEach((r, index) => {
      if (!r.cords) return;
      const selected = this.selection && this.selection.kind === "receiver" && this.selection.index === index;
      const hovered = this.hover && this.hover.kind === "receiver" && this.hover.index === index;
      const offline = this.offline.has(r.entity_id);
      const unmatched = r.unmatched;
      const locked = edit && this.locks.receiver;
      const s = (hovered || selected ? base * 1.4 : base) / k;
      const face = offline ? "#d9534f" : unmatched ? "#e0a54a" : "#1f7a8c";
      // A proxy kept out of positioning ("Use in positioning" off) is drawn
      // hollow: still there, still heard, just not steering anything.
      const idle = r.solve === false;
      ctx.save();
      ctx.translate(r.cords.x, r.cords.y);
      ctx.rotate(Math.PI / 4);
      ctx.globalAlpha = locked ? 0.55 : 1;
      ctx.fillStyle = idle ? "rgba(255,255,255,0.35)" : face;
      ctx.strokeStyle = selected ? "#ffd166" : idle ? face : "#ffffff";
      ctx.lineWidth = (selected ? 3 : idle ? 2 : 1.5) / k;
      ctx.fillRect(-s / 2, -s / 2, s, s);
      ctx.strokeRect(-s / 2, -s / 2, s, s);
      ctx.restore();
      this._proxyMarks.push({ x: r.cords.x, y: r.cords.y, s, color: face });
      if (this.options.labels && (edit || hovered || selected)) {
        // The proxy under the pointer, or the one being edited, always gets
        // its name: in Edit that label is how you tell which one you grabbed.
        this._label(ctx, r.label || r.entity_id, r.cords.x, r.cords.y + (base + 9) / k, 10, 0.8, null,
                    hovered || selected ? LABEL_PRIO.focus : LABEL_PRIO.proxy);
      }
    });
  }

  /** Proxies that a thing is sitting on top of, drawn again over it.
   *
   * Things are the subject of the Live map, so they keep the foreground. But
   * a proxy that disappears completely under one reads as a proxy that is
   * GONE - a nightstand with a proxy, a watch and an AirPods case on it
   * showed no proxy at all, and it took a DOM dump to prove it was still
   * there. So a covered proxy comes back as a hollow diamond drawn wide
   * enough to ring whatever is covering it: same centre, same measurement,
   * just no longer invisible. */
  _drawProxyPeeks(ctx) {
    const k = this.view.k;
    for (const p of coveredProxies(this._proxyMarks, this._thingMarks)) {
      const s = Math.max(p.s, (p.cover + 5 / k) * 2);
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(Math.PI / 4);
      ctx.lineWidth = 4 / k; ctx.strokeStyle = "rgba(255,255,255,0.85)";
      ctx.strokeRect(-s / 2, -s / 2, s, s);
      ctx.lineWidth = 2 / k; ctx.strokeStyle = p.color;
      ctx.strokeRect(-s / 2, -s / 2, s, s);
      ctx.restore();
    }
  }

  /** Alignment pins: a surveyor's crosshair, so it reads as a reference mark
   * and not as one more proxy. Green when the same name exists on another
   * floor, amber while it is still on its own, red when the fit says this
   * pin disagrees with the others (`miss`, metres, set by the editor). */
  /** Where the other floors say this floor's pins are, in this floor's px. */
  setPinGhosts(ghosts) { this.pinGhosts = ghosts || []; this.invalidate(); }
  setRadarInfo(info) { this.radarInfo = info || {}; this.invalidate(); }
  setRadarTargets(list) { this.radarTargets = list || []; this.invalidate(); }
  setRadarLive(live) { this.radarLive = live || null; this.invalidate(); }

  /** Placed mmWave sensors (Edit): a marker, the way it faces, and the wedge
   * it sees - its range as set on the device, ±60 degrees. */
  /** The room an mmWave sensor's targets are kept to, as the backend picks it
   * (_radar_room): its `room` by name, "*" for none, else the room it is in or
   * within half a metre of - it is usually on the wall. */
  _radarRoomZone(r) {
    if (r.room === "*") return null;
    const zones = (this.floor?.zones || []).filter((z) => !z.no_go && (z.cords || []).length >= 3);
    if (r.room && r.room !== "auto") { const named = zones.find((z) => z.entity_id === r.room); if (named) return named; }
    const scale = this.floor?.scale || PX_PER_M_FALLBACK;
    const p = r.cords;
    const inside = (pts) => {
      let hit = false;
      for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
        const a = pts[i], b = pts[j];
        if ((a.y > p.y) !== (b.y > p.y) && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) hit = !hit;
      }
      return hit;
    };
    const edgeDist = (pts) => Math.min(...pts.map((a, i) => {
      const b = pts[(i + 1) % pts.length];
      const dx = b.x - a.x, dy = b.y - a.y, len2 = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
      return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
    }));
    let best = null, bestD = Infinity;
    for (const z of zones) {
      const d = inside(z.cords) ? 0 : edgeDist(z.cords);
      if (d < bestD) { best = z; bestD = d; }
    }
    return bestD <= 0.5 * scale ? best : null;
  }

  _drawRadars(ctx, radars) {
    const k = this.view.k, scale = this.floor?.scale || PX_PER_M_FALLBACK;
    radars.forEach((r, index) => {
      if (!r.cords) return;
      const info = this.radarInfo[r.device_id] || {};
      // The range is a slant from the mounting height to a chest (1 m up):
      // across the floor it reaches that much less (radars.floor_factor).
      const slant = info.range_m || 6, dh = (Number(r.height_m) || 0) - 1.0;
      const range = (dh > 0 ? Math.sqrt(Math.max(slant * slant - dh * dh, 0)) : slant) * scale;
      const room = this._radarRoomZone(r);
      const half = ((info.fov_deg || 120) / 2) * Math.PI / 180;
      const a0 = ((r.heading || 0) * Math.PI) / 180 - Math.PI / 2;   // 0 = up the plan, clockwise
      const selected = this.selection?.kind === "radar" && this.selection.index === index;
      const hovered = this.hover?.kind === "radar" && this.hover.index === index;
      const colour = r.device_id ? "#1f9e89" : "#e0a54a";
      ctx.save();
      ctx.beginPath(); ctx.moveTo(r.cords.x, r.cords.y); ctx.arc(r.cords.x, r.cords.y, range, a0 - half, a0 + half); ctx.closePath();
      const wedge = new Path2D(); wedge.moveTo(r.cords.x, r.cords.y); wedge.arc(r.cords.x, r.cords.y, range, a0 - half, a0 + half); wedge.closePath();
      ctx.fillStyle = colour;
      if (room) {
        // Kept to a room: the part it counts is filled, the rest of what it
        // sees (through the walls) is only outlined.
        ctx.save();
        const clip = new Path2D();
        room.cords.forEach((c, i) => (i ? clip.lineTo(c.x, c.y) : clip.moveTo(c.x, c.y))); clip.closePath();
        ctx.clip(clip);
        ctx.globalAlpha = selected ? 0.22 : 0.1;
        ctx.fill(wedge);
        ctx.restore();
      } else {
        ctx.globalAlpha = selected ? 0.22 : 0.1;
        ctx.fill(wedge);
      }
      ctx.globalAlpha = selected ? 0.9 : 0.45;
      ctx.strokeStyle = colour; ctx.lineWidth = 1.5 / k; ctx.setLineDash([6 / k, 4 / k]); ctx.stroke(); ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      const s = ((selected || hovered) ? RADAR_SIZE * 1.3 : RADAR_SIZE) / k;
      ctx.fillStyle = colour; ctx.strokeStyle = selected ? "#ffd166" : "#ffffff"; ctx.lineWidth = (selected ? 3 : 1.5) / k;
      ctx.beginPath(); ctx.arc(r.cords.x, r.cords.y, s, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      // Which way it faces: a stroke from the centre out past the rim.
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2.5 / k;
      ctx.beginPath(); ctx.moveTo(r.cords.x, r.cords.y); ctx.lineTo(r.cords.x + Math.cos(a0) * s * 1.9, r.cords.y + Math.sin(a0) * s * 1.9); ctx.stroke();
      ctx.restore();
      this._label(ctx, info.name || (r.device_id ? "mmWave" : "mmWave: pick the device"), r.cords.x, r.cords.y + (RADAR_SIZE + 10) / k, 10, 0.85, null,
                  selected || hovered ? LABEL_PRIO.focus : LABEL_PRIO.proxy);
    });
  }

  /** mmWave targets. Edit, with a radar selected: its targets right now,
   * numbered, to check the direction against. Live: a target no tracked
   * thing accounts for, as a hollow marker - someone Sextant has no device for. */
  _drawRadarTargets(ctx) {
    const k = this.view.k;
    if (this.mode === "edit") {
      for (const t of this.radarLive?.targets || []) {
        const [x, y] = t.cords;
        ctx.save();
        // A target outside the sensor's room is hollow: seen, not counted.
        ctx.fillStyle = t.outside ? "rgba(214,51,132,0.15)" : "#d63384"; ctx.strokeStyle = t.outside ? "#d63384" : "#ffffff"; ctx.lineWidth = 2 / k;
        if (t.outside) ctx.setLineDash([3 / k, 2 / k]);
        ctx.beginPath(); ctx.arc(x, y, 7 / k, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = t.outside ? "#d63384" : "#ffffff"; ctx.font = `700 ${9 / k}px system-ui, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(String(t.index), x, y);
        ctx.restore();
      }
      return;
    }
    for (const t of this.radarTargets) {
      if (t.thing) continue;          // a tracked thing is drawn as itself
      const [x, y] = t.cords;
      const r = THING_RADIUS / k;
      ctx.save();
      ctx.fillStyle = this.dark ? "rgba(40,46,56,0.85)" : "rgba(255,255,255,0.85)";
      ctx.strokeStyle = this.dark ? "#9fb3c8" : "#5b6b7c"; ctx.lineWidth = 2 / k; ctx.setLineDash([4 / k, 3 / k]);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = ctx.strokeStyle; ctx.font = `700 ${13 / k}px system-ui, sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("?", x, y + 0.5 / k);
      ctx.restore();
      this._label(ctx, "untracked", x, y + r + 9 / k, 10, 0.8, null, LABEL_PRIO.thing);
    }
  }

  /** Notes: a small dot where the click was, the text on a plate beside it.
   *
   * Deliberately the quietest thing on the plan - a dashed ring, no fill, and
   * the text at the size of a caption - because a note is about something
   * else on the map and must not outshine it. An empty one still draws: it is
   * a note being written, and it has to be findable to be finished. */
  _drawRemarks(ctx, remarks) {
    const k = this.view.k;
    const edit = this.mode === "edit";
    remarks.forEach((note, index) => {
      if (!note.cords) return;
      const selected = edit && this.selection?.kind === "remark" && this.selection.index === index;
      const hovered = edit && this.hover?.kind === "remark" && this.hover.index === index;
      const r = (selected || hovered ? REMARK_SIZE * 1.25 : REMARK_SIZE) / k;
      const colour = selected ? "#ffd166" : this.dark ? "#9fb3c8" : "#5b6b7c";
      ctx.save();
      ctx.globalAlpha = edit ? 1 : 0.75;
      ctx.lineWidth = 3 / k;
      ctx.strokeStyle = this.dark ? "rgba(18,22,28,0.9)" : "rgba(255,255,255,0.9)";
      ctx.beginPath(); ctx.arc(note.cords.x, note.cords.y, r, 0, Math.PI * 2); ctx.stroke();
      ctx.setLineDash([4 / k, 3 / k]);
      ctx.strokeStyle = colour; ctx.lineWidth = 1.8 / k;
      ctx.beginPath(); ctx.arc(note.cords.x, note.cords.y, r, 0, Math.PI * 2); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = colour;
      ctx.beginPath(); ctx.arc(note.cords.x, note.cords.y, 2.2 / k, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
      const text = (note.text || "").trim();
      if (text) {
        ctx.save();
        ctx.font = `600 ${10 / k}px system-ui, sans-serif`;
        const w = ctx.measureText(text).width;
        ctx.restore();
        this._label(ctx, text, note.cords.x + r + 4 / k + w / 2, note.cords.y, 10, edit ? 0.95 : 0.7);
      } else if (edit) {
        this._label(ctx, "note", note.cords.x + r + 4 / k + 12 / k, note.cords.y, 9, 0.55);
      }
    });
  }

  _drawPins(ctx, pins) {
    const k = this.view.k;
    // Ghosts first, under the pins: a hollow ring where another floor puts the
    // same pin, tied to this floor's pin by a red line when they disagree. A
    // pin on the wrong corner is then a long red line, visible from across
    // the plan, instead of a number in a side panel.
    const byName = new Map(pins.filter((q) => q.cords).map((q) => [q.name, q]));
    const loud = 0.3 * (this.floor?.scale || 100);   // 30 cm in this floor's px
    for (const g of this.pinGhosts || []) {
      const mine = byName.get(g.name);
      const gap = mine ? Math.hypot(mine.cords.x - g.x, mine.cords.y - g.y) : 0;
      ctx.save();
      if (mine && gap > loud) {
        ctx.strokeStyle = "#d9534f"; ctx.lineWidth = 2.5 / k; ctx.setLineDash([6 / k, 4 / k]);
        ctx.beginPath(); ctx.moveTo(mine.cords.x, mine.cords.y); ctx.lineTo(g.x, g.y); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 4 / k;
      ctx.beginPath(); ctx.arc(g.x, g.y, (PIN_SIZE * 0.8) / k, 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = gap > loud ? "#d9534f" : "#7a8a99"; ctx.lineWidth = 1.8 / k;
      ctx.beginPath(); ctx.arc(g.x, g.y, (PIN_SIZE * 0.8) / k, 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
      if (this.options.labels && (!mine || gap > loud)) this._label(ctx, `${g.name} on ${g.floor}`, g.x, g.y + (PIN_SIZE + 10) / k, 10, 0.75);
    }
    pins.forEach((pin, index) => {
      if (!pin.cords) return;
      const selected = this.selection && this.selection.kind === "pin" && this.selection.index === index;
      const hovered = this.hover && this.hover.kind === "pin" && this.hover.index === index;
      const r = (selected || hovered ? PIN_SIZE * 1.3 : PIN_SIZE) / k;
      const colour = pin.miss != null && pin.miss > 0.3 ? "#d9534f" : pin.linked ? "#2e9d5b" : "#e0a54a";
      ctx.save();
      ctx.globalAlpha = this.locks.pin ? 0.5 : 1;
      ctx.translate(pin.cords.x, pin.cords.y);
      ctx.lineCap = "round";
      for (const [stroke, width] of [["#ffffff", 5], [colour, 2.5]]) {
        ctx.strokeStyle = selected && width < 5 ? "#ffd166" : stroke;
        ctx.lineWidth = width / k;
        ctx.beginPath();
        ctx.arc(0, 0, r * 0.62, 0, Math.PI * 2);
        ctx.moveTo(-r * 1.25, 0); ctx.lineTo(-r * 0.2, 0);
        ctx.moveTo(r * 0.2, 0); ctx.lineTo(r * 1.25, 0);
        ctx.moveTo(0, -r * 1.25); ctx.lineTo(0, -r * 0.2);
        ctx.moveTo(0, r * 0.2); ctx.lineTo(0, r * 1.25);
        ctx.stroke();
      }
      ctx.restore();
      if (this.options.labels) {
        const miss = pin.miss != null && pin.miss >= 0.05 ? ` · ${pin.missLabel || pin.miss.toFixed(2) + " m"}` : "";
        this._label(ctx, `${pin.name || "pin"}${miss}`, pin.cords.x, pin.cords.y - (PIN_SIZE + 12) / k, 10, 0.9);
      }
    });
    if (this._pinSnap) {
      ctx.save();
      ctx.strokeStyle = "#ff9800";
      ctx.lineWidth = 2 / k;
      const s = 9 / k;
      ctx.strokeRect(this._pinSnap.x - s, this._pinSnap.y - s, s * 2, s * 2);
      ctx.restore();
    }
  }

  _drawSnap(ctx) {
    // The wall holding the dragged proxy, and which room's side it is on.
    const { a, b, name } = this._snap;
    const k = this.view.k;
    ctx.save();
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(255, 152, 0, 0.35)";
    ctx.lineWidth = 10 / k;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.strokeStyle = "#ff9800";
    ctx.lineWidth = 3 / k;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.restore();
    if (name) this._label(ctx, `on the wall, ${name} side`, (a.x + b.x) / 2, (a.y + b.y) / 2, 11, 0.95);
  }

  _drawDraft(ctx) {
    const pts = this.draft;
    if (!pts || !pts.length) return;
    const k = this.view.k;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.strokeStyle = "#ffd166"; ctx.lineWidth = 2 / k; ctx.setLineDash([6 / k, 4 / k]);
    ctx.stroke(); ctx.setLineDash([]);
    const h = this._drawHover;
    if (h) {
      const last = pts[pts.length - 1];
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(h.x, h.y);
      ctx.strokeStyle = h.snapped ? "#ff9800" : "rgba(255,209,102,0.6)"; ctx.lineWidth = 1.5 / k;
      ctx.setLineDash([4 / k, 4 / k]); ctx.stroke(); ctx.setLineDash([]);
      this._handle(ctx, h, VERTEX_SIZE / k, h.snapped ? "#ff9800" : "#ffd166", "#5a4400");
    }
    pts.forEach((p, i) => this._handle(ctx, p, (i === 0 ? VERTEX_SIZE * 1.3 : VERTEX_SIZE) / k, "#ffd166", "#5a4400"));
  }

  /** Location pins: a pin where the user said the focused thing really was. */
  _drawSuggestions(ctx) {
    // Where the Advice page says a proxy would help: a magenta target, in
    // both modes. Drawn over a scrim (see draw()), numbered so a whole
    // floor's worth can be counted off against the Advice list, and haloed
    // so a ring never disappears into a dark wall it sits on.
    const k = this.view.k;
    this.suggestions.forEach((s, i) => {
      const r = 17 / k;
      ctx.beginPath(); ctx.arc(s.x, s.y, r + 5 / k, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(200,0,180,0.14)"; ctx.fill();
      ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255,255,255,0.9)"; ctx.lineWidth = 6 / k; ctx.stroke();
      ctx.strokeStyle = "#c800b4"; ctx.lineWidth = 3 / k; ctx.setLineDash([5 / k, 4 / k]); ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(s.x, s.y, 6 / k, 0, Math.PI * 2); ctx.fillStyle = "#c800b4"; ctx.fill();
      if (this.suggestions.length > 1) {
        ctx.font = `700 ${9 / k}px system-ui, sans-serif`;
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillStyle = "#fff"; ctx.fillText(String(i + 1), s.x, s.y + 0.4 / k);
      }
      // Above the ring, unless the label would land under the toolbar the
      // Edit page floats over the top of the canvas, or off the plan.
      const screenY = this.view.ty + s.y * k;
      const above = screenY > TOP_OVERLAY_PX && s.y > 40 / k;
      this._label(ctx, s.label || "add a proxy here", s.x, s.y + (above ? -26 : 28) / k, 11, 1);
    });
  }

  /**
   * A regular grid of cells drawn smooth: one pixel per cell in a small
   * image, scaled up with bilinear smoothing, so cell edges become gradients
   * instead of squares. A transparent cell of padding all round lets the
   * edges fade out. cells: [{x, y, rgba: [r, g, b, a 0..255]}], centres in map px.
   */
  _smoothGrid(cells, size) {
    if (typeof document === "undefined" || !cells.length) return null;
    const xs = cells.map((c) => c.x), ys = cells.map((c) => c.y);
    const x0 = Math.min(...xs), y0 = Math.min(...ys);
    const cols = Math.round((Math.max(...xs) - x0) / size) + 3, rows = Math.round((Math.max(...ys) - y0) / size) + 3;
    const img = document.createElement("canvas");
    img.width = cols; img.height = rows;
    const g = img.getContext("2d"), data = g.createImageData(cols, rows);
    for (const c of cells) {
      const i = ((Math.round((c.y - y0) / size) + 1) * cols + Math.round((c.x - x0) / size) + 1) * 4;
      data.data.set(c.rgba, i);
    }
    g.putImageData(data, 0, 0);
    return { img, x: x0 - 1.5 * size, y: y0 - 1.5 * size, w: cols * size, h: rows * size };
  }

  _blit(ctx, grid) {
    if (!grid) return;
    ctx.save();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(grid.img, grid.x, grid.y, grid.w, grid.h);
    ctx.restore();
  }

  /** Time spent per cell: blue for a moment, through yellow, to red for the longest stay. */
  _drawHeat(ctx) {
    const { size, max, cells } = this.heat;
    if (!max) return;
    if (!this._heatImg) {
      // Square root: an hour on the bed would otherwise wash every walk-through out to nothing.
      this._heatImg = this._smoothGrid(cells.map((c) => {
        const w = Math.sqrt(c.secs / max);
        return { x: c.x, y: c.y, rgba: [...heatRgb(w), Math.round(255 * (0.25 + 0.5 * w))] };
      }), size);
    }
    this._blit(ctx, this._heatImg);
  }

  /** Grey where the two floors' priors are even, green where this floor is favoured less, red more. */
  _drawBiasMap(ctx) {
    const { size, cells } = this.biasMap;
    if (!this._biasImg) {
      this._biasImg = this._smoothGrid(cells.map(([x, y, ratio]) => ({ x, y, rgba: biasRgba(ratio) })), size);
    }
    this._blit(ctx, this._biasImg);
  }

  _drawMarks(ctx) {
    const k = this.view.k;
    for (const m of this.marks) {
      const r = 5 / k;
      ctx.beginPath(); ctx.moveTo(m.x, m.y); ctx.lineTo(m.x, m.y - 16 / k);
      ctx.strokeStyle = "#ffd166"; ctx.lineWidth = 2 / k; ctx.stroke();
      ctx.beginPath(); ctx.arc(m.x, m.y - 16 / k, r, 0, Math.PI * 2);
      ctx.fillStyle = "#ffd166"; ctx.fill(); ctx.strokeStyle = "#5a4400"; ctx.lineWidth = 1 / k; ctx.stroke();
      ctx.beginPath(); ctx.arc(m.x, m.y, 2.5 / k, 0, Math.PI * 2); ctx.fillStyle = "#5a4400"; ctx.fill();
      if (m.label) this._label(ctx, m.label, m.x, m.y - 24 / k, 10, 0.8);
    }
  }

  _drawThings(ctx) {
    const k = this.view.k;
    const focus = this.options.focus || null;
    // Markers that would overlap at this zoom become one marker with a count
    // (see clusterThings). The thing you picked out always stays its own.
    const picked = new Set([focus, this.selection?.kind === "thing" ? this.selection.ent : null].filter(Boolean));
    const clusters = this.options.cluster === false ? [] : clusterThings(this.things, (THING_RADIUS * 1.8) / k, picked);
    const clustered = new Map();
    for (const c of clusters) for (const m of c.members) clustered.set(m.ent, c);
    for (const t of this.things) {
      if (!t.cords) continue;
      const custom = t.color || null;
      const color = thingColor(t.ent, custom);
      const paint = (a, dark = false) => thingRgba(t.ent, custom, a, dark);
      const focused = focus && t.ent === focus;
      // Not heard for a while: what is drawn is where it WAS. A ghost -
      // faint, outlined in dashes, labelled with how long ago - rather than a
      // solid dot claiming a position nobody has confirmed.
      const { age, ghost } = staleness(t, this.options.staleAfter);
      ctx.save();
      if (focus && !focused) ctx.globalAlpha = 0.28;   // everything but the one you clicked fades back
      if (ghost) ctx.globalAlpha *= 0.4;
      const trail = this.options.trails ? this.trails.get(t.ent) : null;
      if (trail && trail.length > 1) {
        ctx.beginPath();
        trail.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
        ctx.strokeStyle = paint(0.5); ctx.lineWidth = 2 / k; ctx.stroke();
      }
      if (this.options.circles && Array.isArray(t.radii)) {
        for (const [x, y, r] of t.radii) {
          ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
          ctx.strokeStyle = paint(0.35); ctx.lineWidth = 1 / k; ctx.stroke();
        }
      }
      if (this.options.fingerprint && t.fp && t.fp.fix) {
        // The two ends of the blend: the fingerprint match (dashed circle) and the geometric fit (square).
        ctx.beginPath(); ctx.arc(t.fp.fix[0], t.fp.fix[1], 7 / k, 0, Math.PI * 2);
        ctx.strokeStyle = color; ctx.lineWidth = 2 / k; ctx.setLineDash([3 / k, 3 / k]); ctx.stroke(); ctx.setLineDash([]);
        ctx.beginPath(); ctx.moveTo(t.fp.fix[0], t.fp.fix[1]); ctx.lineTo(t.cords[0], t.cords[1]);
        ctx.strokeStyle = paint(0.5); ctx.lineWidth = 1 / k; ctx.stroke();
        if (t.fp.geo) {
          const s = 6 / k;
          ctx.beginPath(); ctx.rect(t.fp.geo[0] - s, t.fp.geo[1] - s, 2 * s, 2 * s);
          ctx.strokeStyle = color; ctx.lineWidth = 2 / k; ctx.stroke();
          ctx.beginPath(); ctx.moveTo(t.fp.geo[0], t.fp.geo[1]); ctx.lineTo(t.cords[0], t.cords[1]);
          ctx.strokeStyle = paint(0.5); ctx.lineWidth = 1 / k; ctx.stroke();
        }
      }
      if (t.raw && this.options.circles) {
        ctx.beginPath(); ctx.arc(t.raw[0], t.raw[1], 4 / k, 0, Math.PI * 2);
        ctx.fillStyle = paint(0.6); ctx.fill();
      }
      // Trails, rings and fixes are drawn for every thing; the dot and label
      // of a clustered one are drawn once for the cluster, below.
      if (clustered.has(t.ent)) { ctx.restore(); continue; }
      const selected = focused || (this.selection && this.selection.kind === "thing" && this.selection.ent === t.ent);
      const r = (focused ? THING_RADIUS * 1.6 : THING_RADIUS) / k;
      if (focused) {
        // A halo that does not scale with zoom, so the focused thing is findable at any zoom level.
        ctx.beginPath(); ctx.arc(t.cords[0], t.cords[1], r * 2.6, 0, Math.PI * 2);
        ctx.strokeStyle = paint(0.9, true); ctx.lineWidth = 3 / k; ctx.setLineDash([8 / k, 5 / k]); ctx.stroke(); ctx.setLineDash([]);
      }
      // Confidence ring: the published conf in (0,1] as the ring's alpha.
      // A ghost has no current confidence to show.
      if (!ghost) {
        ctx.beginPath(); ctx.arc(t.cords[0], t.cords[1], r * 1.9, 0, Math.PI * 2);
        ctx.fillStyle = paint(0.08 + 0.22 * (t.conf ?? 0.5)); ctx.fill();
      }
      ctx.beginPath(); ctx.arc(t.cords[0], t.cords[1], r, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      if (ghost) ctx.setLineDash([4 / k, 3 / k]);
      ctx.lineWidth = (selected ? 3 : 2) / k; ctx.strokeStyle = selected ? "#ffd166" : "#ffffff"; ctx.stroke();
      ctx.setLineDash([]);
      const glyph = t.mdi ? mdiPath(t.mdi, () => this.invalidate()) : null;
      if (t.icon && t.icon.complete && t.icon.naturalWidth) {
        ctx.save(); ctx.beginPath(); ctx.arc(t.cords[0], t.cords[1], r * 0.85, 0, Math.PI * 2); ctx.clip();
        ctx.drawImage(t.icon, t.cords[0] - r * 0.85, t.cords[1] - r * 0.85, r * 1.7, r * 1.7); ctx.restore();
      } else if (glyph) {
        // MDI paths live in a 24x24 box; fit it inside the dot.
        const s = (r * 1.4) / 24;
        ctx.save(); ctx.translate(t.cords[0] - r * 0.7, t.cords[1] - r * 0.7); ctx.scale(s, s);
        ctx.fillStyle = "#ffffff"; ctx.fill(glyph); ctx.restore();
      } else {
        ctx.fillStyle = "#ffffff"; ctx.font = `700 ${11 / k}px system-ui, sans-serif`;
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText((t.label || t.ent).slice(0, 2).toUpperCase(), t.cords[0], t.cords[1]);
      }
      const label = ghost ? `${t.label || t.ent} · ${shortAge(age)} ago` : (t.label || t.ent);
      if (this.options.labels || focused || ghost) {
        this._label(ctx, label, t.cords[0], t.cords[1] + r + 9 / k, focused ? 13 : 11, 0.9, null,
                    focused ? LABEL_PRIO.focus : LABEL_PRIO.thing);
      }
      // Where the dot ended up, so a proxy hidden underneath it can come back
      // over the top: see _drawProxyPeeks.
      this._thingMarks.push({ x: t.cords[0], y: t.cords[1], r });
      ctx.restore();
    }
    for (const c of clusters) this._drawCluster(ctx, c, focus);
  }

  /** One marker for several things in one place: a dot in the first member's
   * colour carrying the count, and a label that names the first two. */
  _drawCluster(ctx, c, focus) {
    const k = this.view.k, r = (THING_RADIUS * 1.15) / k;
    const lead = c.members[0];
    const custom = lead.color || null;
    const stale = c.members.every((t) => staleness(t, this.options.staleAfter).ghost);
    ctx.save();
    if (focus) ctx.globalAlpha = 0.28;   // a cluster is never the focused thing
    if (stale) ctx.globalAlpha *= 0.4;
    ctx.beginPath(); ctx.arc(c.x, c.y, r * 1.9, 0, Math.PI * 2);
    ctx.fillStyle = thingRgba(lead.ent, custom, 0.18); ctx.fill();
    ctx.beginPath(); ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
    ctx.fillStyle = thingColor(lead.ent, custom); ctx.fill();
    if (stale) ctx.setLineDash([4 / k, 3 / k]);
    ctx.lineWidth = 2 / k; ctx.strokeStyle = "#ffffff"; ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#ffffff"; ctx.font = `700 ${13 / k}px system-ui, sans-serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(String(c.members.length), c.x, c.y);
    const names = c.members.map((t) => t.label || t.ent);
    const label = names.length > 2 ? `${names[0]}, ${names[1]} +${names.length - 2}` : names.join(", ");
    if (this.options.labels) this._label(ctx, label, c.x, c.y + r + 9 / k, 11, 0.9, null, LABEL_PRIO.thing);
    this._thingMarks.push({ x: c.x, y: c.y, r });
    ctx.restore();
  }
}
