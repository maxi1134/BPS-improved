/**
 * Edit mode: the floor-plan editor on the shared map.
 *
 * Works on a draft copy of the layout; nothing reaches the store until Save.
 * Receivers are placed by picking a scanner Bermuda knows, zones, sub-zones
 * and no-go areas are drawn as polygons, and the map's scale is set from a
 * measured distance. A new floor is a map image upload (through the HTTP
 * view, the one thing the websocket does not carry).
 */
import { LitElement, html, css, nothing } from "./lit.js";
import { SextantMap, polygonCentroid, snapToVertex, squareUp } from "./sextant-map.js";
import { sharedStyles, widgetStyles, toast, callWS, confirmDialog, fmtNum, fmtLen, uiField, uiSelect, uiSwitch, uiButton, uiIconButton, uiMenu, proxyName, lenUnit, toDisplayLen, fromDisplayLen, fmtScale, isImperial, THING_CLASSES, CLASS_FAMILIES } from "./sextant-ui.js";
import { mapUrlFor } from "./sextant-panel.js";
import { lostShapes } from "./sextant-shapes.js";

// [id, label under the icon, icon, tooltip]
/** The Anchor tool's "New anchor" choice (not a name any anchor can have). */
const NEW_PIN = "\u0000new";

const TOOLS = [
  ["select", "Select", "mdi:cursor-default-outline", "Select and drag proxies, rooms and vertices. A dragged proxy snaps onto a nearby wall on the side you are dragging from; hold Alt to place it freely"],
  ["receiver", "Proxy", "mdi:access-point-plus", "Place a proxy: pick one Bermuda knows, then click the map"],
  ["zone", "Room", "mdi:vector-polygon", "Draw a room: click corners, close on the first one"],
  ["subzone", "Spot", "mdi:vector-rectangle", "Draw a spot (a couch, a desk, a bedside table) inside a room"],
  ["nogo", "No-go", "mdi:cancel", "Draw an area things can never be in (a void, a wall)"],
  ["measure", "Scale", "mdi:ruler", "Set the map scale from a known distance"],
  ["pin", "Anchor", "mdi:crosshairs-gps", "Anchor a point that lines up through the house - an outside corner, a stair post. The same name on another floor says how the floors stack. It lands on a room corner when one is near; hold Alt to place it freely"],
  ["radar", "mmWave", "mdi:radar", "Place an mmWave presence sensor (Everything Presence, Apollo R PRO-1) and set the way it faces. Its targets land on the plan: a thing near one is placed on it, one still target and one still thing make a location pin, and a target no thing accounts for is someone Sextant has no device for"],
  ["remark", "Note", "mdi:note-text-outline", "Leave a note on the plan - where a proxy is going, what to check, what a room is really called. Notes are for people; nothing about positioning reads them"],
];
// Layers that can be locked against selection and dragging, so a finished
// room layout is not nudged while proxies are being moved (and vice versa).
const LOCKS = [
  ["zone", "Rooms", "mdi:floor-plan"],
  ["subzone", "Spots", "mdi:vector-rectangle"],
  ["receiver", "Proxies", "mdi:access-point"],
  ["pin", "Anchors", "mdi:crosshairs-gps"],
];
const UNDO_DEPTH = 50;

function uid(prefix) { return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`; }

/** A layout as a string to compare, without the display-only fields the
 * editor hangs on proxies and pins (the ones _cleanDraft drops before a save). */
function layoutKey(layout) {
  const copy = JSON.parse(JSON.stringify(layout || {}));
  for (const f of copy.floor || []) {
    for (const r of f.receivers || []) { delete r.unmatched; delete r.label; }
    for (const q of f.pins || []) { delete q.linked; delete q.miss; delete q.missLabel; }
  }
  return JSON.stringify(copy);
}

class SextantEdit extends LitElement {
  static properties = {
    hass: { attribute: false },
    data: { attribute: false },
    floor: { type: String },
    narrow: { type: Boolean },
    spots: { attribute: false },   // advised proxy spots to show: [{floor, room, x, y}]
    _draft: { state: true },
    _dirty: { state: true },
    _tool: { state: true },
    _nextPin: { state: true },   // the Pin tool's next name: one waiting on another floor, or NEW_PIN
    _selection: { state: true },
    _placing: { state: true },
    _measure: { state: true },
    _proposal: { state: true },
    _biasView: { state: true },
    _busy: { state: true },
    _locks: { state: true },
    _undo: { state: true },
    _history: { state: true },     // the kept copies of the plan, once asked for
    _alignment: { state: true },
    _radarDevices: { state: true },   // devices reporting mmWave target coordinates
    _radarLive: { state: true },      // the selected radar's targets right now
  };

  constructor() {
    super();
    this._draft = null;
    this._dirty = false;
    this._tool = "select";
    this._selection = null;
    this._placing = null; // scanner address chosen for the next receiver click
    this._measure = null; // {a, b} map points
    this._proposal = null;
    this._busy = false;
    this._tick = 0;
    // Rooms start locked: once a floor plan is drawn it rarely changes, and a
    // slip while moving a proxy must not move a wall.
    this._locks = { zone: true, subzone: false, receiver: false, pin: false };
    this._undo = [];
    this._alignment = null; // how the floors stack, graded by the backend against this draft
  }

  // --- undo ---------------------------------------------------------------------

  _snapshot() {
    if (!this._draft) return;
    this._undo = [...this._undo.slice(-(UNDO_DEPTH - 1)), JSON.stringify(this._draft, (k, v) => (k === "linked" || k === "miss" || k === "missLabel" ? undefined : v))];
  }

  _undoLast() {
    if (!this._undo.length) return;
    const prev = this._undo[this._undo.length - 1];
    this._undo = this._undo.slice(0, -1);
    this._draft = JSON.parse(prev);
    this._dirty = layoutKey(this._draft) !== layoutKey(this.data?.layout || { floor: [] });
    this._proposal = null;
    this._pushFloor();
  }

  _setLock(kind, locked) {
    this._locks = { ...this._locks, [kind]: locked };
    this._map?.setLocks(this._locks);
    if (this._selection?.kind === kind && locked) this._selection = null;
  }

  firstUpdated() {
    this._map = new SextantMap(this.renderRoot.querySelector("canvas"), {
      fetch: (url) => this.hass.fetchWithAuth(url),
      onSelect: (hit) => { this._selection = hit; },
      onDragStart: () => this._snapshot(),
      onChange: (kind) => { this._dirty = true; this._tick++; if (kind === "pin") this._refreshAlignment(); this.requestUpdate(); },
      onDrawPoint: () => this.requestUpdate(),
      onDrawClose: () => this._closeDraft(),
      onContextMenu: (hit) => this._context(hit),
    });
    this._map.setMode("edit");
    this._map.setOptions({ labels: true, subzones: true, receivers: true, trails: false });
    this._map.setLocks(this._locks);
    this._syncDraft(true);
    this._loadRadarDevices();
    // A reload or a closed tab would take the draft with it.
    this._warnUnload = (ev) => { if (this._dirty) { ev.preventDefault(); ev.returnValue = ""; } };
    window.addEventListener("beforeunload", this._warnUnload);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearTimeout(this._alignTimer);
    clearInterval(this._radarTimer);
    window.removeEventListener("beforeunload", this._warnUnload);
    this._map?.destroy();
  }

  /** Whether the draft holds changes that are not saved. */
  get unsaved() { return !!this._dirty; }

  /** Ask before something would throw the draft away (another floor, another
   * page). True to go ahead. */
  confirmLeave(what = "Leave") {
    return !this._dirty || confirmDialog(`${what} without saving? The changes to this floor plan will be lost.`);
  }

  updated(changed) {
    if (!this._map) return;
    if (changed.has("data")) this._syncDraft(!this._dirty);
    if (changed.has("floor")) this._pushFloor();
    if (changed.has("hass")) this._map.setAreas(this.hass?.areas);
    if (changed.has("floor") || changed.has("data")) this._loadBiasView();
    if (changed.has("spots") || changed.has("floor")) this._map.setSuggestions((this.spots || []).filter((s) => s.floor === this.floor).map((s) => ({ x: s.x, y: s.y, label: `add a proxy here · ${s.room}` })));
    if (changed.has("_tool")) { this._map.setTool(["measure", "receiver", "pin", "remark", "radar"].includes(this._tool) ? "select" : this._tool); }
    if (changed.has("_selection")) this._watchRadar();
  }

  _syncDraft(replace) {
    if (replace || !this._draft) {
      this._draft = JSON.parse(JSON.stringify(this.data?.layout || { floor: [] }));
      this._dirty = false;
      this._selection = null;
      this._undo = [];
    }
    this._pushFloor();
  }

  _floorObj() { return (this._draft?.floor || []).find((f) => f.name === this.floor) || null; }

  _pushFloor() {
    const f = this._floorObj();
    if (f) {
      // Mark placements Bermuda does not know, for the orange marker.
      const known = new Set(Object.keys(this.data?.scanners || {}));
      const knownSlugs = new Set(Object.values(this.data?.scanners || {}).map((s) => s.slug));
      for (const r of f.receivers || []) {
        r.unmatched = !((r.address && known.has(r.address)) || knownSlugs.has(r.entity_id));
        r.label = proxyName(this.data, r.address || r.entity_id);   // the map shows the friendly name, not the slug
      }
    }
    this._map?.setFloor(f, mapUrlFor(this.floor, this.data?.maps));
    this._map?.setOffline(this.data?.offline_receivers || []);
    this._map?.setSelection(null);
    this._selection = null;
    // The last report already covers every floor: show THIS floor's rings now
    // rather than leaving the previous floor's drawn until the next reply.
    this._markPins();
    this._refreshAlignment();
  }

  // --- pins: how the floors stack ----------------------------------------------

  /** Transient marks the map draws pins with; stripped again before Save. */
  _markPins() {
    const rep = this._alignment;
    for (const fl of this._draft?.floor || []) {
      const misses = rep?.floors?.[fl.name]?.misses || {};
      for (const pin of fl.pins || []) {
        pin.linked = (rep?.pins?.[pin.name] || []).length > 1;
        pin.miss = misses[pin.name] ?? null;
        pin.missLabel = pin.miss == null ? null : fmtLen(pin.miss, this.hass, 2);
      }
    }
    this._map?.setPinGhosts(rep?.floors?.[this.floor]?.ghosts || []);
  }

  /** Ask the backend to grade the DRAFT, so a pin's effect on the fit shows
   * while it is still being placed. Debounced: a drag fires per pixel. */
  _refreshAlignment() {
    clearTimeout(this._alignTimer);
    // Replies can overtake each other while a pin is dragged; only the answer
    // to the LATEST question may be shown, or the card flickers back to a
    // grading of where the pin was half a second ago.
    const asked = (this._alignAsked = (this._alignAsked || 0) + 1);
    this._alignTimer = setTimeout(async () => {
      const draft = this._draft;
      if (!draft || !(draft.floor || []).some((fl) => (fl.pins || []).length)) { this._alignment = null; this._markPins(); return; }
      const clean = { floor: draft.floor.map((fl) => ({ name: fl.name, scale: fl.scale, level: fl.level, elevation: fl.elevation, pins: (fl.pins || []).map((q) => ({ name: q.name, cords: q.cords })) })) };
      let report = null;
      try {
        report = await this.hass.callWS({ type: "sextant/registration", layout: clean });
      } catch (_e) {
        report = null;   // an older backend: the pins still save, they just are not graded
      }
      if (asked !== this._alignAsked || !this.isConnected) return;   // superseded, or the page has gone
      this._alignment = report;
      this._markPins();
    }, 250);
  }

  /** A fresh name no floor uses yet. */
  _freshPinName() {
    const all = new Set((this._draft.floor || []).flatMap((fl) => (fl.pins || []).map((q) => q.name)));
    let n = 1;
    while (all.has(`Anchor ${n}`)) n++;
    return `Anchor ${n}`;
  }

  /** What the next click with the Pin tool will be called: the name picked
   * under the tool while it is still waiting here, a fresh one for "New anchor",
   * else the first name another floor is waiting on (so linking a floor is a
   * row of clicks in the same order), else a fresh one. */
  _nextPinName(f) {
    const waiting = this._pinNamesElsewhere(f);
    if (this._nextPin === NEW_PIN) return this._freshPinName();
    if (this._nextPin && waiting.includes(this._nextPin)) return this._nextPin;
    return waiting[0] || this._freshPinName();
  }

  /** Names pinned on other floors and not yet on this one: what to offer next.
   * Names marked "not on this floor" (f.pins_absent) are skipped. */
  _pinNamesElsewhere(f) {
    const here = new Set([...(f.pins || []).map((q) => q.name), ...(f.pins_absent || [])]);
    const names = [];
    for (const fl of this._draft?.floor || []) if (fl !== f) for (const q of fl.pins || []) if (q.name && !here.has(q.name) && !names.includes(q.name)) names.push(q.name);
    return names;
  }

  _placePin(e) {
    const f = this._floorObj();
    if (!f || this._map.hover?.kind === "pin") return;   // a click on a pin selects it
    let p = this._mapPoint(e);
    if (!e.altKey) p = snapToVertex(p, f.zones, 12 / this._map.view.k) || p;
    this._snapshot();
    f.pins = f.pins || [];
    const name = this._nextPinName(f);
    f.pins.push({ pin_id: uid("pin"), name, cords: { x: Math.round(p.x * 1000) / 1000, y: Math.round(p.y * 1000) / 1000 } });
    this._dirty = true;
    this._selection = { kind: "pin", index: f.pins.length - 1 };
    this._map.setSelection(this._selection);
    this._refreshAlignment();
    this.requestUpdate();
  }

  /** A note on the plan, where the click was. Notes carry no geometry and no
   * name of their own - nothing but a person reads them - so placing one is
   * just a point and an empty line of text waiting in the inspector. */
  _placeRemark(e) {
    const f = this._floorObj();
    if (!f || this._map.hover?.kind === "remark") return;   // a click on a note selects it
    const p = this._mapPoint(e);
    this._snapshot();
    f.remarks = f.remarks || [];
    f.remarks.push({ remark_id: uid("remark"), text: "", cords: { x: Math.round(p.x * 1000) / 1000, y: Math.round(p.y * 1000) / 1000 } });
    this._dirty = true;
    this._selection = { kind: "remark", index: f.remarks.length - 1 };
    this._map.setSelection(this._selection);
    this.requestUpdate();
    // Straight into typing: a note placed and then lost because the text box
    // was never found is worse than no note.
    this.updateComplete.then(() => this.renderRoot?.querySelector(".remark-text input, .remark-text textarea")?.focus());
  }

  /** An mmWave sensor where the click was, facing up the plan until told
   * otherwise; the device is picked in the inspector. */
  _placeRadar(e) {
    const f = this._floorObj();
    if (!f || this._map.hover?.kind === "radar") return;   // a click on one selects it
    const p = this._mapPoint(e);
    this._snapshot();
    f.radars = f.radars || [];
    f.radars.push({ radar_id: uid("radar"), device_id: null, heading: 0, flip: false, cords: { x: Math.round(p.x * 1000) / 1000, y: Math.round(p.y * 1000) / 1000 } });
    this._dirty = true;
    this._selection = { kind: "radar", index: f.radars.length - 1 };
    this._map.setSelection(this._selection);
    this.requestUpdate();
  }

  async _loadRadarDevices() {
    const r = await this.hass?.callWS({ type: "sextant/radar/devices" }).catch(() => null);
    this._radarDevices = r?.devices || [];
    this._map?.setRadarInfo(Object.fromEntries(this._radarDevices.map((d) => [d.device_id, { name: d.name, range_m: d.range_m, fov_deg: d.fov_deg }])));
  }

  /** While an mmWave sensor is selected, show its targets as they are now,
   * placed as the draft has it: walk in front of it and watch yourself move. */
  _watchRadar() {
    clearInterval(this._radarTimer);
    this._radarTimer = null;
    if (this._selection?.kind !== "radar") { this._radarLive = null; this._map?.setRadarLive(null); return; }
    this._pollRadar();
    this._radarTimer = setInterval(() => this._pollRadar(), 2000);
  }

  async _pollRadar() {
    const f = this._floorObj(), sel = this._selection;
    const item = sel?.kind === "radar" ? f?.radars?.[sel.index] : null;
    if (!item?.device_id || !item.cords) { this._radarLive = null; this._map?.setRadarLive(null); return; }
    const r = await this.hass.callWS({ type: "sextant/radar/targets", device_id: item.device_id, floor: this.floor,
      x: item.cords.x, y: item.cords.y, heading: item.heading || 0, flip: !!item.flip,
      height_m: item.height_m ?? null, room: item.room ?? null }).catch(() => null);
    if (this._selection !== sel) return;
    this._radarLive = r?.targets || [];
    this._radarRoom = r?.room ?? null;
    this._map?.setRadarLive({ radar_id: item.radar_id, targets: this._radarLive });
  }

  _renderRadar(item) {
    const devices = this._radarDevices || [];
    const dev = devices.find((d) => d.device_id === item.device_id);
    const placed = new Set((this._draft?.floor || []).flatMap((fl) => (fl.radars || []).filter((r) => r !== item).map((r) => r.device_id)));
    const turn = (deg) => this._edit("heading", ((((item.heading || 0) + deg) % 360) + 360) % 360);
    const live = this._radarLive || [];
    const counted = live.filter((t) => !t.outside);
    const rooms = (this._floorObj()?.zones || []).filter((z) => !z.no_go && z.entity_id).map((z) => z.entity_id);
    const auto = item.room == null || item.room === "";
    return html`<div class="card">
      <h4>mmWave sensor</h4>
      <div class="row">${uiSelect({ label: "Device", value: item.device_id || "", options: [{ value: "", label: devices.length ? "pick one" : "no mmWave sensors found" }, ...devices.map((d) => ({ value: d.device_id, label: `${d.name}${d.model ? ` · ${d.model}` : ""}${placed.has(d.device_id) ? " (placed elsewhere)" : ""}` }))], onChange: (v) => { this._edit("device_id", v || null); this._pollRadar(); }, style: "flex: 1" })}</div>
      <div class="row">
        ${uiField({ label: "Facing (degrees clockwise from up)", type: "number", step: 1, min: 0, max: 359, value: item.heading ?? 0, onChange: (v) => { this._edit("heading", (((Number(v) || 0) % 360) + 360) % 360); this._pollRadar(); }, style: "width: 230px" })}
      </div>
      <div class="iconrow">
        ${[["mdi:arrow-up", 0, "up"], ["mdi:arrow-right", 90, "right"], ["mdi:arrow-down", 180, "down"], ["mdi:arrow-left", 270, "left"]].map(([icon, a, way]) =>
          uiIconButton({ icon, box: true, active: (item.heading ?? 0) === a, title: `Face ${way} the plan (${a}°)`, onClick: () => { this._edit("heading", a); this._pollRadar(); } }))}
        <span class="gap"></span>
        ${uiIconButton({ icon: "mdi:rotate-left", box: true, title: "Turn 15° anticlockwise", onClick: () => { turn(-15); this._pollRadar(); } })}
        ${uiIconButton({ icon: "mdi:rotate-right", box: true, title: "Turn 15° clockwise", onClick: () => { turn(15); this._pollRadar(); } })}
      </div>
      ${uiSwitch({ label: "Flip left/right", checked: !!item.flip, onChange: (v) => { this._edit("flip", !!v); this._pollRadar(); } })}
      <div class="row">
        ${uiField({ label: `Mount height (${lenUnit(this.hass)})`, type: "number", step: 0.05, min: 0, max: isImperial(this.hass) ? 16 : 5,
          value: toDisplayLen(item.height_m, this.hass), onChange: (v) => { this._edit("height_m", v === "" ? undefined : fromDisplayLen(v, this.hass)); this._pollRadar(); }, style: "width: 170px" })}
        ${uiSelect({ label: "Counts people in", value: auto ? "auto" : item.room, style: "flex: 1",
          // "auto", not "": HA's select shows an option with an empty value as blank.
          options: [{ value: "auto", label: `The room it's in${auto && this._radarRoom ? ` (${this._radarRoom})` : ""}` }, ...rooms.map((n) => ({ value: n, label: n })), { value: "*", label: "Everywhere it sees" }],
          onChange: (v) => { this._edit("room", v && v !== "auto" ? v : undefined); this._pollRadar(); } })}
      </div>
      <div class="muted small">The height turns what it measures - a straight line down to your chest - into distance across the floor; from 2 m up, someone a metre out otherwise reads 40 % too far. It sees through drywall, so targets outside the room are ignored (hollow dots here, and the wedge is filled only inside the room).</div>
      <div class="muted small">${dev ? html`Sees ${fmtLen(dev.range_m, this.hass)} out, ±${Math.round((dev.fov_deg || 120) / 2)}° either side (the range set on the device).${dev.installation_angle ? html` Its installation angle is set to ${dev.installation_angle}° on the device, which already turns the coordinates it reports: face this marker the way that turned frame points.` : nothing}` : "Pick the device, then turn the marker the way the sensor faces."}</div>
      ${item.device_id ? html`<div class="small"><b>${counted.length}</b> target${counted.length === 1 ? "" : "s"} right now${live.length > counted.length ? html` (and ${live.length - counted.length} outside the room)` : nothing}${live.length ? html` - the pink dots. Walk straight away from it: the dot should move along the wedge's centre line; if it goes off at an angle, turn the marker; if it moves the wrong way sideways, flip.` : html` - walk in front of it to see yourself as a pink dot.`}</div>` : nothing}
      <div class="muted small">Save the plan to put it to work: a thing whose Bluetooth fix is near a target is placed on the target, one still target and one still thing make a location pin, and a target nobody's device accounts for counts in sensor.sextant_untracked_people.</div>
      <div class="row"><span class="grow"></span>${uiButton({ label: "Delete", kind: "danger", onClick: () => this._deleteSelection() })}</div>
    </div>`;
  }

  /** The draft as it should be stored: without the marks the map draws with.
   * Both save paths go through here - adding a floor posts the draft too, and
   * used to send `unmatched`, `label` and the pin marks along with it. */
  _cleanDraft() {
    const draft = this._draft;
    for (const f of draft.floor) {
      for (const r of f.receivers || []) { delete r.unmatched; delete r.label; }
      for (const q of f.pins || []) { delete q.linked; delete q.miss; delete q.missLabel; }
    }
    return draft;
  }

  _listFor(kind, f) { return kind === "receiver" ? f.receivers : kind === "zone" ? f.zones : kind === "pin" ? f.pins : kind === "remark" ? f.remarks : kind === "radar" ? (f.radars = f.radars || []) : f.subzones; }

  // --- tools -------------------------------------------------------------------

  _setTool(tool) {
    this._tool = tool;
    this._measure = null;
    if (tool !== "receiver") this._placing = null;
    if (["receiver", "measure", "pin", "remark", "radar"].includes(tool)) this._map.setTool("select");
    if (tool === "pin" && this._locks.pin) this._setLock("pin", false);   // you are placing pins: they must be reachable
  }

  _onCanvasClick(e) {
    if (this._map?.lastDragMoved) return; // a pan, not a click
    if (this._tool === "receiver" && this._placing) this._placeReceiver(e);
    else if (this._tool === "measure") this._measureClick(e);
    else if (this._tool === "pin") this._placePin(e);
    else if (this._tool === "remark") this._placeRemark(e);
    else if (this._tool === "radar") this._placeRadar(e);
  }

  _mapPoint(e) {
    const r = this._map.canvas.getBoundingClientRect();
    return this._map.toMap({ x: e.clientX - r.left, y: e.clientY - r.top });
  }

  _placeReceiver(e) {
    const f = this._floorObj();
    if (!f) return;
    const p = this._mapPoint(e);
    const info = this.data?.scanners?.[this._placing];
    const slug = info?.slug || this._placing.replace(/:/g, "_");
    this._snapshot();
    f.receivers = f.receivers || [];
    f.receivers.push({ entity_id: slug, cords: { x: Math.round(p.x * 1000) / 1000, y: Math.round(p.y * 1000) / 1000 }, type: "receiver", address: this._placing });
    this._placing = null;
    this._tool = "select";
    this._dirty = true;
    this._pushFloor();
    this._selection = { kind: "receiver", index: f.receivers.length - 1 };
    this._map.setSelection(this._selection);
  }

  _measureClick(e) {
    const p = this._mapPoint(e);
    if (!this._measure || this._measure.b) this._measure = { a: p, b: null };
    else this._measure = { ...this._measure, b: p };
  }

  _applyMeasure(shown) {
    const metres = fromDisplayLen(shown, this.hass);
    const m = this._measure, f = this._floorObj();
    if (!m?.b || !f || !(metres > 0)) return;
    this._snapshot();
    const px = Math.hypot(m.b.x - m.a.x, m.b.y - m.a.y);
    f.scale = Math.round((px / metres) * 10000) / 10000;
    this._dirty = true;
    this._measure = null;
    this._tool = "select";
    toast(this, `Scale set to ${fmtScale(f.scale, this.hass)}`);
  }

  _closeDraft() {
    const pts = this._map.finishDraft();
    const f = this._floorObj();
    if (!pts || !f) return;
    this._snapshot();
    const tool = this._tool;
    if (tool === "zone" || tool === "nogo") {
      f.zones = f.zones || [];
      const name = tool === "nogo" ? `No-go ${f.zones.filter((z) => z.no_go).length + 1}` : `Room ${f.zones.length + 1}`;
      f.zones.push({ zone_id: uid("zone"), entity_id: name, poly: true, cords: pts, type: "zone", ...(tool === "nogo" ? { no_go: true } : {}) });
      if (this._locks.zone) this._setLock("zone", false);   // you just drew one: you are editing rooms
      this._selection = { kind: "zone", index: f.zones.length - 1 };
    } else if (tool === "subzone") {
      f.subzones = f.subzones || [];
      // A spot belongs to one room: the one under its first corner (the
      // centroid's if that corner sits outside every room). Save clips the
      // spot to that room.
      const rooms = (f.zones || []).filter((z) => !z.no_go);
      const parent = rooms.find((z) => this._inside(pts[0], z.cords)) || rooms.find((z) => this._inside(polygonCentroid(pts), z.cords));
      f.subzones.push({ sub_zone_id: uid("subzone"), entity_id: `Spot ${f.subzones.length + 1}`, parent: parent?.zone_id || null, poly: true,
        color: `hsl(${Math.floor(Math.random() * 360)}, 70%, 45%)`, cords: pts, type: "subzone" });
      this._selection = { kind: "subzone", index: f.subzones.length - 1 };
    }
    this._dirty = true;
    this._tool = "select";
    this._map.setTool("select");
    this._map.setSelection(this._selection);
    this._map.invalidate();
  }

  _inside(pt, cords) {
    let inside = false;
    for (let i = 0, j = cords.length - 1; i < cords.length; j = i++) {
      const a = cords[i], b = cords[j];
      if ((a.y > pt.y) !== (b.y > pt.y) && pt.x < ((b.x - a.x) * (pt.y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
    }
    return inside;
  }

  _context(hit) {
    if (!hit) return;
    if (hit.vertex != null) {
      const f = this._floorObj();
      const list = hit.kind === "zone" ? f.zones : f.subzones;
      if (list[hit.index].cords.length > 3) { this._snapshot(); list[hit.index].cords.splice(hit.vertex, 1); this._dirty = true; this._map.invalidate(); }
    }
  }

  /** Straighten the selected room or spot's near-straight edges (see squareUp). */
  _squareUp() {
    const sel = this._selection, f = this._floorObj();
    if (!sel || !f || (sel.kind !== "zone" && sel.kind !== "subzone")) return;
    const item = this._listFor(sel.kind, f)[sel.index];
    const squared = squareUp(item.cords || []);
    const moved = squared.reduce((m, q, i) => Math.max(m, Math.hypot(q.x - item.cords[i].x, q.y - item.cords[i].y)), 0);
    if (moved < 0.01) return toast(this, "Already square");
    this._snapshot();
    item.cords = squared;
    this._dirty = true;
    this._map.invalidate();
    this.requestUpdate();
    const m = f.scale ? moved / f.scale : null;
    toast(this, m != null ? `Squared up; no corner moved more than ${Math.round(m * 100)} cm. Save to keep it` : "Squared up. Save to keep it");
  }

  /** Home Assistant's areas, by name. */
  _areas() { return Object.values(this.hass?.areas || {}).sort((a, b) => String(a.name).localeCompare(String(b.name))); }

  /** Link a room to a Home Assistant area: its sensors then carry the area's id, and its label the area's icon. */
  _renderAreaLink(item) {
    const areas = this._areas();
    if (!areas.length) return nothing;
    const floors = this.hass?.floors || {};
    const area = this.hass?.areas?.[item.area_id];
    return html`<div class="row" style="align-items: center">
      ${uiSelect({ label: "Home Assistant area", value: item.area_id || "", options: [{ value: "", label: "not linked" },
        ...areas.map((a) => ({ value: a.area_id, label: a.floor_id && floors[a.floor_id] ? `${a.name} (${floors[a.floor_id].name})` : a.name }))],
        onChange: (v) => { this._edit("area_id", v || undefined); this._map.invalidate(); }, style: "flex: 1" })}
      ${area?.icon ? html`<ha-icon icon=${area.icon} title="The area's icon, shown in this room's label"></ha-icon>` : nothing}
    </div>
    <div class="muted small">A thing in this room reports the area's id (<code>area_id</code> on its room and location sensors), so an automation can act on the area itself.</div>`;
  }

  /** Rooms named exactly like an area (whatever the case) are linked to it; linked rooms are left alone. */
  _linkAreasByName() {
    const f = this._floorObj();
    if (!f) return;
    const byName = new Map(this._areas().map((a) => [String(a.name).trim().toLowerCase(), a.area_id]));
    const todo = (f.zones || []).filter((z) => !z.no_go && !z.area_id && byName.has(String(z.entity_id).trim().toLowerCase()));
    if (!todo.length) return toast(this, "No unlinked room on this floor is named like an area");
    this._snapshot();
    for (const z of todo) z.area_id = byName.get(String(z.entity_id).trim().toLowerCase());
    this._dirty = true;
    this._map.invalidate();
    this.requestUpdate();
    toast(this, `Linked ${todo.length} room${todo.length === 1 ? "" : "s"} to the area of the same name. Save to keep it`);
  }

  /** The floors this floor can be compared with, nearest level first (Basement and Second Floor both want Ground). */
  _biasPartners(f) {
    const lvl = (x) => (typeof x.level === "number" ? x.level : 0);
    return (this._draft?.floor || []).filter((o) => o.name !== f.name)
      .sort((a, b) => Math.abs(lvl(a) - lvl(f)) - Math.abs(lvl(b) - lvl(f)) || lvl(b) - lvl(a));
  }

  async _loadBiasView() {
    const f = this._floorObj(), v = this._biasView;
    if (!v?.on || !f) { this._map?.setBiasMap(null); return; }
    const partners = this._biasPartners(f);
    const other = partners.some((o) => o.name === v.other) ? v.other : partners[0]?.name;
    if (!other) { this._map?.setBiasMap(null); return; }
    const r = await this.hass.callWS({ type: "sextant/floor_bias_map", floor: f.name, other }).catch((e) => { toast(this, `bias map: ${e?.message || e}`); return null; });
    if (!this._biasView?.on || this._floorObj()?.name !== f.name) return;
    this._biasView = { ...this._biasView, other, result: r };
    this._map?.setBiasMap(r ? { size: r.cell_px, cells: r.cells } : null);
  }

  /** Where this floor's election prior leans against a neighbour's: saved values, so Save before looking. */
  _renderBiasView(f) {
    const v = this._biasView || {};
    const partners = this._biasPartners(f);
    if (!partners.length) return nothing;
    const r = v.on ? v.result : null;
    const pct = (x) => `${x >= 1 ? "+" : "−"}${Math.round(Math.abs(x - 1) * 100)}%`;
    return html`<div class="card">
      <div class="row" style="align-items: center; gap: 8px; flex-wrap: wrap">
        ${uiSwitch({ label: "Show floor bias", checked: !!v.on, onChange: (on) => { this._biasView = { ...v, on }; this._loadBiasView(); } })}
        ${v.on && partners.length > 1 ? uiSelect({ label: "Against", value: v.other || partners[0].name, options: partners.map((o) => ({ value: o.name, label: o.name })), onChange: (other) => { this._biasView = { ...this._biasView, other }; this._loadBiasView(); }, style: "width: 170px" }) : nothing}
        ${v.on && partners.length === 1 ? html`<span class="muted small">against ${partners[0].name}</span>` : nothing}
      </div>
      ${r ? html`<div class="muted small">Grey: ${f.name} and ${v.other} even. <span style="color:#1eaa46">Green</span>: a thing here leans to ${v.other}; <span style="color:#d72828">red</span>: to ${f.name}. Here it runs from ${pct(r.min)} to ${pct(r.max)}.${r.registered ? "" : " The floors are not lined up with anchors, so places are matched by distance from each plan's corner."} Shows the saved layout.</div>` : nothing}
    </div>`;
  }

  /** Spot size in the unit a tape measure reads: inches when imperial, cm otherwise. */
  _sizeUnit() { return isImperial(this.hass) ? { name: "in", perM: 39.3701 } : { name: "cm", perM: 100 }; }

  _spotBox(item) {
    const xs = (item.cords || []).map((q) => q.x), ys = (item.cords || []).map((q) => q.y);
    return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) };
  }

  /** Type the real table's size: the spot becomes that exact rectangle, one corner held where it is. */
  _renderSpotSize(item, f) {
    if (!f.scale || (item.cords || []).length < 3) return nothing;
    const u = this._sizeUnit(), b = this._spotBox(item);
    const cur = { w: Math.round(((b.x1 - b.x0) / f.scale) * u.perM), d: Math.round(((b.y1 - b.y0) / f.scale) * u.perM) };
    const s = this._size?.id === item.sub_zone_id ? this._size : { id: item.sub_zone_id, corner: "tl" };
    const set = (k, v) => { this._size = { ...s, [k]: v }; };
    return html`<div class="row">
      ${uiField({ label: `Width (${u.name}, across the plan)`, type: "number", step: 1, min: 1, value: s.w ?? cur.w, onChange: (v) => set("w", Number(v)), style: "width: 150px" })}
      ${uiField({ label: `Depth (${u.name}, up the plan)`, type: "number", step: 1, min: 1, value: s.d ?? cur.d, onChange: (v) => set("d", Number(v)), style: "width: 150px" })}
      ${uiSelect({ label: "Keep this corner", value: s.corner, options: [{ value: "tl", label: "Top left" }, { value: "tr", label: "Top right" }, { value: "bl", label: "Bottom left" }, { value: "br", label: "Bottom right" }], onChange: (v) => set("corner", v), style: "width: 140px" })}
      ${uiButton({ label: "Set size", icon: "mdi:ruler-square", onClick: () => this._resizeSpot(), title: "Make the spot an exact rectangle of this size, measured on the real furniture" })}
    </div>`;
  }

  /** The proxy on the spot (its readings count as being on it) and the spot's own entry share. */
  _renderSpotEvidence(item, f) {
    const c = polygonCentroid(item.cords || []);
    const linked = Array.isArray(item.proxy) ? item.proxy : item.proxy ? [item.proxy] : [];
    const near = (f.receivers || []).filter((r) => r.cords)
      .map((r) => ({ id: r.entity_id, name: proxyName(this.data, r.address || r.entity_id), d: Math.hypot(r.cords.x - c.x, r.cords.y - c.y) }))
      .sort((a, b) => a.d - b.d).slice(0, 6);
    for (const id of linked) if (!near.some((n) => n.id === id)) near.push({ id, name: proxyName(this.data, id), d: null });
    const toggle = (id) => {
      const next = linked.includes(id) ? linked.filter((x) => x !== id) : [...linked, id];
      this._edit("proxy", next.length ? next : undefined);
    };
    const global = this.data?.layout?.tuning?.subzone_enter_prob ?? this.data?.tuning_spec?.subzone_enter_prob?.default ?? 0.5;
    return html`<div class="field"><span class="small">Proxies on this spot</span>
      <div class="proxypick">${near.map((n) => html`<button class="chip ${linked.includes(n.id) ? "on" : ""}" aria-pressed=${linked.includes(n.id)} @click=${() => toggle(n.id)}
        title=${linked.includes(n.id) ? "On this spot: click to unlink" : "Click if this proxy sits on the furniture"}>${n.name}${n.d != null && f.scale ? html` <span class="muted">${fmtLen(n.d / f.scale, this.hass)}</span>` : nothing}</button>`)}</div></div>
    <div class="row">
      ${uiField({ label: "Entry share", type: "number", step: 0.05, min: 0.05, max: 0.95, value: item.enter_prob ?? "", placeholder: `${global} (default)`, onChange: (v) => this._edit("enter_prob", v === "" || v == null ? undefined : Number(v)), style: "width: 150px" })}
    </div>
    <div class="muted small">Proxies sitting on or against the furniture (a nightstand, both ends of a couch): a thing one of them hears close by, and clearly closer than any proxy not on the spot, counts as here. Entry share: how much of a thing's likely position must fall in the spot to enter it; blank uses the Tuning page's value.</div>`;
  }

  _resizeSpot() {
    const sel = this._selection, f = this._floorObj();
    if (!sel || sel.kind !== "subzone" || !f?.scale) return;
    const item = f.subzones[sel.index], u = this._sizeUnit(), b = this._spotBox(item);
    const s = this._size?.id === item.sub_zone_id ? this._size : {};
    const w = ((s.w ?? ((b.x1 - b.x0) / f.scale) * u.perM) / u.perM) * f.scale;
    const d = ((s.d ?? ((b.y1 - b.y0) / f.scale) * u.perM) / u.perM) * f.scale;
    if (!(w > 0 && d > 0)) return toast(this, "Width and depth must be above zero");
    const corner = s.corner || "tl";
    const x0 = corner[1] === "l" ? b.x0 : b.x1 - w, y0 = corner[0] === "t" ? b.y0 : b.y1 - d;
    const r = (v) => Math.round(v * 1000) / 1000;
    this._snapshot();
    item.cords = [{ x: r(x0), y: r(y0) }, { x: r(x0 + w), y: r(y0) }, { x: r(x0 + w), y: r(y0 + d) }, { x: r(x0), y: r(y0 + d) }];
    item.poly = true;
    this._size = null;
    this._dirty = true;
    this._map.invalidate();
    this.requestUpdate();
    toast(this, "Spot resized. Drag it into place if needed, then Save");
  }

  _deleteSelection() {
    const sel = this._selection, f = this._floorObj();
    if (!sel || !f) return;
    const list = this._listFor(sel.kind, f);
    const item = list[sel.index];
    const what = { receiver: "proxy", zone: "room", pin: "anchor", remark: "note", radar: "mmWave sensor" }[sel.kind] || "spot";
    // An empty note has nothing to name and nothing to lose: no dialog for it.
    const named = item.entity_id ?? item.name ?? item.text;
    if (named && !confirmDialog(`Delete ${what} "${named}"?`)) return;
    this._snapshot();
    list.splice(sel.index, 1);
    if (sel.kind === "pin") this._refreshAlignment();
    if (sel.kind === "zone") for (const s of f.subzones || []) if (s.parent === item.zone_id) s.parent = null;
    this._selection = null;
    this._dirty = true;
    this._map.setSelection(null);
    this._map.invalidate();
  }

  _edit(field, value) {
    const sel = this._selection, f = this._floorObj();
    if (!sel || !f) return;
    const list = this._listFor(sel.kind, f);
    const item = list[sel.index];
    this._snapshot();
    if (sel.kind === "pin") this._refreshAlignment();
    if (field === "calibrate" || field === "solve") { if (value === undefined) delete item[field]; else item[field] = value; }
    else if (field === "height" || field === "correction") item[field] = value === "" || value == null ? undefined : Number(value);
    else if (field === "no_go") item.no_go = !!value;
    else item[field] = value;
    if (item.height === undefined) delete item.height;
    if (item.correction === undefined) delete item.correction;
    if (item[field] === undefined) delete item[field];
    this._dirty = true;
    this._map.invalidate();
    this.requestUpdate();
  }

  // --- floors ------------------------------------------------------------------

  async _addFloor(name, file) {
    if (!name || !file) return toast(this, "A floor needs a name and a map image");
    const draft = this._cleanDraft();
    const ext = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")) : ".png";
    const filename = `${name}${ext}`;
    draft.floor.push({ name, scale: null, receivers: [], zones: [], subzones: [] });
    const form = new FormData();
    form.append("coordinates", JSON.stringify(draft));
    form.append("new_floor", "true");
    form.append("file", file, filename);
    this._busy = true;
    try {
      const resp = await this.hass.fetchWithAuth("/api/sextant/save_text", { method: "POST", body: form });
      if (!resp.ok) throw new Error(await resp.text());
      toast(this, `Floor ${name} added`);
      this._dirty = false;
      this.dispatchEvent(new CustomEvent("layout-changed"));
      this.dispatchEvent(new CustomEvent("floor-changed", { detail: name }));
    } catch (e) {
      draft.floor.pop();
      toast(this, `Add floor failed: ${e.message || e}`, 6000);
    } finally {
      this._busy = false;
    }
  }

  async _removeFloor() {
    const f = this._floorObj();
    if (!f || !confirmDialog(`Delete floor "${f.name}" with its ${(f.receivers || []).length} proxies and ${(f.zones || []).length} rooms?`)) return;
    const maps = this.data?.maps || [];
    const url = mapUrlFor(f.name, maps);
    const mapFile = url ? decodeURIComponent(url.split("/").pop()) : null;
    const before = this._draft.floor;
    this._draft.floor = before.filter((x) => x !== f);
    // A failed save leaves the floor where it was, in the draft as in the store.
    if (!(await this._save(mapFile))) { this._draft.floor = before; this._pushFloor(); this.requestUpdate(); return; }
    this.dispatchEvent(new CustomEvent("floor-changed", { detail: this._draft.floor[0]?.name || null }));
  }

  async _save(removeMap = null) {
    const draft = this._cleanDraft();
    // Asked before anything is sent. The server keeps a copy of the plan it
    // replaces, but a spot that draws as nothing is still a spot you have to
    // notice is missing: this is the question that would have caught the two
    // bedside tables that went out as one point each.
    const lost = lostShapes(this.data?.layout, draft);
    if (lost.length && !confirmDialog(`This save would break ${lost.length === 1 ? "a shape" : `${lost.length} shapes`}:\n\n${lost.join("\n")}\n\nSave anyway? The plan as it is now is kept under History.`)) return null;
    this._busy = true;
    const r = await callWS(this, this.hass, { type: "sextant/layout/save", layout: draft, ...(removeMap ? { remove_map: removeMap } : {}) });
    this._busy = false;
    if (r) {
      const n = r.confined?.length || 0;
      toast(this, n ? `Floor plan saved; ${n === 1 ? `${r.confined[0]} was` : `${n} spots were`} fitted inside ${n === 1 ? "its room" : "their rooms"}` : "Floor plan saved");
      // The server's own check, in case a shape went wrong on the way in.
      if (r.lost?.length) toast(this, `Saved, but ${r.lost.length === 1 ? "a shape was" : `${r.lost.length} shapes were`} lost: ${r.lost[0]}${r.lost.length > 1 ? " …" : ""}. Restore the earlier copy from History if that was not meant.`, 10000);
      this._history = null;   // stale now: a copy was just added
      this._dirty = false;
      this.dispatchEvent(new CustomEvent("layout-changed"));
    }
    return r;
  }

  /** The kept copies of the plan (see snapshots.py): every save keeps the plan
   * it replaced - all of today's, hourly for a week, daily for three months. */
  async _toggleHistory() {
    if (this._history) { this._history = null; return; }
    const r = await callWS(this, this.hass, { type: "sextant/snapshots/list" });
    if (r) this._history = r.snapshots || [];
  }

  async _restoreSnapshot(snap) {
    const when = new Date(snap.at * 1000).toLocaleString();
    if (this._dirty && !confirmDialog(`You have unsaved changes; restoring throws them away. Restore the plan from ${when}?`)) return;
    if (!this._dirty && !confirmDialog(`Restore the plan from ${when}? The plan as it is now is kept, so this can be undone the same way.`)) return;
    this._busy = true;
    const r = await callWS(this, this.hass, { type: "sextant/snapshots/restore", id: snap.id });
    this._busy = false;
    if (!r) return;
    toast(this, `Restored the plan from ${when}`);
    // Not dirty: the fresh layout the panel fetches next replaces the draft
    // (see updated()), the same way a discard does.
    this._dirty = false;
    this._history = null;
    this.dispatchEvent(new CustomEvent("layout-changed"));
  }

  _renderHistory() {
    const h = this._history;
    const fmt = (s) => `${new Date(s.at * 1000).toLocaleString()} · v${s.version}${s.bytes ? ` · ${Math.round(s.bytes / 1024)} KB` : ""}`;
    return html`<div class="card small">
      <h4>History<span class="grow"></span>${uiButton({ label: h ? "Hide" : "Show", kind: "text", disabled: this._busy, onClick: () => this._toggleHistory() })}</h4>
      ${h === null || h === undefined
        ? html`<div class="muted">Every save keeps the plan it replaced: all of today's, hourly for a week, daily for three months. Any of them can be put back.</div>`
        : h.length
          ? html`<ul class="plain">${h.slice(0, 40).map((s) => html`<li class="row">
              <span class="grow">${fmt(s)}</span>
              ${uiButton({ label: "Restore", kind: "outline", disabled: this._busy, onClick: () => this._restoreSnapshot(s), title: "Put this copy of the plan back; the current one is kept" })}
            </li>`)}</ul>${h.length > 40 ? html`<div class="muted">…and ${h.length - 40} older</div>` : nothing}`
          : html`<div class="muted">No kept copies yet: the first one is made the next time the plan is saved.</div>`}
    </div>`;
  }

  _discard() {
    if (this._dirty && !confirmDialog("Discard unsaved changes?")) return;
    this._syncDraft(true);
  }

  async _adjust(target) {
    const f = this._floorObj();
    if (!f) return;
    this._busy = true;
    const r = await callWS(this, this.hass, { type: "sextant/adjust_zones", target, zones: f.zones || [], subzones: f.subzones || [], options: {} });
    this._busy = false;
    if (r) this._proposal = { target, ...r };
  }

  _acceptProposal() {
    const f = this._floorObj(), p = this._proposal;
    if (!f || !p) return;
    this._snapshot();
    if (p.zones) f.zones = p.zones;
    if (p.subzones) f.subzones = p.subzones;
    this._proposal = null;
    this._dirty = true;
    this._pushFloor();
  }

  // --- render -----------------------------------------------------------------

  render() {
    const f = this._floorObj();
    const sel = this._selection;
    const scanners = Object.entries(this.data?.scanners || {}).sort((a, b) => (a[1].name || "").localeCompare(b[1].name || ""));
    const placedAddr = new Set((this._draft?.floor || []).flatMap((fl) => (fl.receivers || []).map((r) => r.address)));
    return html`
      <div class="stage">
        <canvas @click=${(e) => this._onCanvasClick(e)}></canvas>
        <div class="zoom" role="group" aria-label="Zoom">
          <button title="Fit the whole floor into view" aria-label="Fit the whole floor" @click=${() => this._map.fit()}><ha-icon icon="mdi:fit-to-screen-outline"></ha-icon></button>
          <button title="Zoom in" aria-label="Zoom in" @click=${() => this._map.zoomBy(1.3)}><ha-icon icon="mdi:plus"></ha-icon></button>
          <button title="Zoom out" aria-label="Zoom out" @click=${() => this._map.zoomBy(1 / 1.3)}><ha-icon icon="mdi:minus"></ha-icon></button>
        </div>
        <div class="toolbar">
          ${TOOLS.map(([id, label, icon, tip]) => html`<button class="tool ${this._tool === id ? "active" : ""}" title=${`${label}: ${tip}`} aria-label=${label} aria-pressed=${this._tool === id} @click=${() => this._setTool(id)}><ha-icon icon=${icon}></ha-icon><span>${label}</span></button>`)}
          <span class="sep"></span>
          ${LOCKS.map(([kind, label, icon]) => html`<button class="tool lock ${this._locks[kind] ? "locked" : ""}" aria-label=${`${label} ${this._locks[kind] ? "locked" : "unlocked"}`} aria-pressed=${!!this._locks[kind]} title=${this._locks[kind] ? `${label} are locked: click to allow selecting and moving them` : `${label} can be moved: click to lock them`} @click=${() => this._setLock(kind, !this._locks[kind])}>
            <span class="lockicons"><ha-icon icon=${icon}></ha-icon><ha-icon class="badge" icon=${this._locks[kind] ? "mdi:lock" : "mdi:lock-open-variant-outline"}></ha-icon></span><span>${label}</span></button>`)}
          <span class="sep"></span>
          <button class="tool" aria-label="Undo" title="Undo the last change (${this._undo.length} step${this._undo.length === 1 ? "" : "s"})" ?disabled=${!this._undo.length} @click=${() => this._undoLast()}><ha-icon icon="mdi:undo"></ha-icon><span>Undo</span></button>
          ${uiButton({ label: "Save", kind: "primary", disabled: !this._dirty || this._busy, onClick: () => this._save(), title: "Write the floor plan to the store" })}
          ${uiButton({ label: "Discard", kind: "text", disabled: !this._dirty, onClick: () => this._discard() })}
        </div>
        ${this._tool === "receiver" ? html`<div class="hint">
          <select @change=${(e) => { this._placing = e.target.value || null; }}>
            <option value="">Pick a proxy, then click the map…</option>
            ${scanners.map(([addr, s]) => html`<option value=${addr} ?disabled=${placedAddr.has(addr)}>${s.name || s.slug}${placedAddr.has(addr) ? " (placed)" : ""}${s.area ? ` · ${s.area}` : ""}</option>`)}
          </select></div>` : nothing}
        ${this._tool === "measure" ? html`<div class="hint">
          ${this._measure?.b ? html`${uiField({ label: `Distance between the two points (${lenUnit(this.hass)})`, type: "number", step: 0.01, min: 0.1, onChange: (v) => { this._metres = v; }, style: "width: 240px" })} ${uiButton({ label: "Set scale", kind: "primary", onClick: () => this._applyMeasure(this._metres) })}`
            : this._measure ? "Click the second point." : `Click two points a known distance apart. Current scale: ${fmtScale(f?.scale, this.hass)}`}
        </div>` : nothing}
        ${this._tool === "pin" ? html`<div class="hint">Click a point you can find on every floor: an outside corner, a stair post, a chimney. It lands on a room corner when one is near (Alt places it freely). Then switch floor and anchor the same points - the names carry over in order.${this._renderNextPin()}</div>` : nothing}
        ${["zone", "subzone", "nogo"].includes(this._tool) ? html`<div class="hint">Click to add corners; click the first corner or double-click to close. An edge close to horizontal, vertical or 45° snaps exact (orange); hold Alt to place a corner freely. ${uiButton({ label: "Cancel", kind: "text", onClick: () => { this._map.cancelDraft(); } })}</div>` : nothing}
      </div>
      <aside class="side">
        ${f ? html`
          <div class="card">
            <h4>${f.name} <span class="muted small">${fmtScale(f.scale, this.hass)}</span><span class="grow"></span>
              ${uiMenu({ items: [
                { label: "Link rooms to areas", icon: "mdi:link-variant", disabled: this._busy, onClick: () => this._linkAreasByName(), title: "Link every unlinked room on this floor to the Home Assistant area of the same name" },
                { label: "Adjust rooms", icon: "mdi:vector-square", disabled: this._busy, onClick: () => this._adjust("zones"), title: "Square up rooms and snap shared walls" },
                { label: "Adjust spots", icon: "mdi:vector-rectangle", disabled: this._busy, onClick: () => this._adjust("subzones") },
                { divider: true },
                { label: "Delete floor", icon: "mdi:delete-outline", danger: true, disabled: this._busy, onClick: () => this._removeFloor() },
              ] })}</h4>
            <div class="row small muted">${(f.receivers || []).length} proxies · ${(f.zones || []).filter((z) => !z.no_go).length} rooms · ${(f.zones || []).filter((z) => z.no_go).length} no-go · ${(f.subzones || []).length} spots</div>
            <div class="row small muted">Level: storey number, 0 = ground, -1 = basement; orders the floor picker top-down. Elevation: this floor's finished floor above the ground floor's, so ceiling height plus the floor structure; blank assumes 3 m a storey. Bias: election prior, 1.2 = a 20 % head start every cycle.</div>
            <div class="row">
              ${/* Measuring two points gives a float with a dozen decimals; a
                    hundredth of a pixel per metre is already far finer than any
                    measurement behind it, so show and store it rounded. */ ""}
              ${uiField({ label: "Scale (px per m)", type: "number", step: 0.01, value: f.scale == null ? "" : Math.round(f.scale * 100) / 100, onChange: (v) => { this._snapshot(); f.scale = Number(v) || null; this._dirty = true; this.requestUpdate(); }, style: "width: 150px" })}
              ${uiField({ label: "Level", type: "number", step: 1, value: f.level ?? "", placeholder: "0", onChange: (v) => { if (v === "" || v == null) delete f.level; else f.level = Math.round(Number(v)); this._dirty = true; this.requestUpdate(); }, style: "width: 90px" })}
              ${uiField({ label: `Elevation (${lenUnit(this.hass)})`, type: "number", step: 0.05, value: toDisplayLen(f.elevation, this.hass), placeholder: String(toDisplayLen((f.level || 0) * 3, this.hass)), onChange: (v) => { this._snapshot(); const m = fromDisplayLen(v, this.hass); if (m == null || isNaN(m)) delete f.elevation; else f.elevation = m; this._dirty = true; this._refreshAlignment(); this.requestUpdate(); }, style: "width: 130px" })}
              ${uiField({ label: "Election bias", type: "number", step: 0.05, min: 0.25, max: 4, value: f.bias ?? "", placeholder: "1", onChange: (v) => { if (v === "" || v == null) delete f.bias; else f.bias = Number(v); this._dirty = true; this.requestUpdate(); }, style: "width: 120px" })}
              ${Object.keys(this.hass?.floors || {}).length ? uiSelect({ label: "Home Assistant floor", value: f.floor_id || "", options: [{ value: "", label: "not linked" }, ...Object.values(this.hass.floors).map((x) => ({ value: x.floor_id, label: x.name }))], onChange: (v) => { this._snapshot(); if (v) f.floor_id = v; else delete f.floor_id; this._dirty = true; this.requestUpdate(); }, style: "width: 170px" }) : nothing}
            </div>
          </div>` : html`<div class="card muted">No floor yet. Add one below.</div>`}
        ${f ? this._renderBiasView(f) : nothing}
        ${f ? this._renderAlignment(f) : nothing}
        ${this._proposal ? html`<div class="card">
          <h4>Proposed ${this._proposal.target}</h4>
          <ul class="plain small">${(this._proposal.report || this._proposal.changes || []).slice(0, 12).map((c) => html`<li>${typeof c === "string" ? c : `${c.name || c.zone || ""}: ${c.change || c.note || JSON.stringify(c)}`}</li>`)}</ul>
          <div class="row">${uiButton({ label: "Accept", kind: "primary", onClick: () => this._acceptProposal() })}${uiButton({ label: "Reject", kind: "text", onClick: () => { this._proposal = null; } })}</div>
        </div>` : nothing}
        ${sel ? this._renderSelection(sel, f) : html`<div class="card muted small">Select a proxy, room or spot on the map to edit it. Drag to move; drag a vertex or an edge midpoint; right-click a vertex to remove it. Locked layers (the padlocks in the toolbar) cannot be selected.</div>`}
        <div class="card">
          <h4>Add a floor</h4>
          <form @submit=${(e) => { e.preventDefault(); const fd = new FormData(e.target); this._addFloor(fd.get("name"), fd.get("file")); }}>
            <div class="row"><input class="grow" type="text" name="name" placeholder="Floor name" required><input type="file" name="file" accept="image/*" required></div>
            <div class="row">${uiButton({ label: "Add floor", kind: "primary", disabled: this._busy, onClick: (e) => e.target.closest("form").requestSubmit() })}<span class="muted small">The image is stored as the floor's map.</span></div>
          </form>
        </div>
        ${this._renderHistory()}
        ${(this.data?.scanner_diagnostics?.unplaced_scanners || []).length ? html`<div class="card small"><h4>Proxies reporting, not placed</h4>${this.data.scanner_diagnostics.unplaced_scanners.map((s) => proxyName(this.data, s)).join(", ")}</div>` : nothing}
      </aside>
    `;
  }

  /** Say a pin another floor has is not on this one (a basement-only post, a
   * corner that moved upstairs), so the Pin tool stops offering it; or undo that. */
  _setPinAbsent(f, name, absent) {
    this._snapshot();
    const list = new Set(f.pins_absent || []);
    if (absent) list.add(name); else list.delete(name);
    if (list.size) f.pins_absent = [...list].sort(); else delete f.pins_absent;
    if (this._nextPin === name) this._nextPin = undefined;
    this._dirty = true;
    this.requestUpdate();
  }

  /** Under the Pin tool: which name the next click takes - one another floor
   * is waiting on, or a new pin (sticks while you place several) - and the
   * names marked as not on this floor. */
  _renderNextPin() {
    const f = this._floorObj();
    if (!f) return nothing;
    const waiting = this._pinNamesElsewhere(f);
    const placed = new Set((f.pins || []).map((q) => q.name));
    const absent = (f.pins_absent || []).filter((n) => !placed.has(n));
    if (!waiting.length && !absent.length) return nothing;   // every click is a new pin anyway
    const next = this._nextPinName(f);
    const isNew = this._nextPin === NEW_PIN;
    const where = (name) => (this._draft?.floor || []).filter((fl) => fl !== f && (fl.pins || []).some((q) => q.name === name)).map((fl) => fl.name).join(", ");
    return html`<div class="row small next-pin"><span class="muted">Next anchor:</span>
      ${waiting.map((n) => html`<span class="pair"><button class="chip ${!isNew && n === next ? "on" : ""}" title="Anchored on ${where(n)}: the same point here" @click=${() => { this._nextPin = n; }}>${n}</button><button class="chip skip" title="${n} is not on this floor: stop offering it here" aria-label="${n} is not on this floor" @click=${() => this._setPinAbsent(f, n, true)}>not here</button></span>`)}
      <button class="chip ${isNew || !waiting.length ? "on" : ""}" title="A point not anchored on any other floor yet, named ${this._freshPinName()}" @click=${() => { this._nextPin = NEW_PIN; }}>New anchor</button>
    </div>
    ${absent.length ? html`<div class="row small next-pin"><span class="muted">Not on this floor:</span>${absent.map((n) => html`<button class="chip" title="Offer ${n} on this floor again" @click=${() => this._setPinAbsent(f, n, false)}>${n} ↺</button>`)}</div>` : nothing}`;
  }

  /** How this floor stacks against the others, and what the pins say about it. */
  _renderAlignment(f) {
    const rep = this._alignment, pins = f.pins || [];
    const row = rep?.floors?.[f.name];
    const waiting = this._pinNamesElsewhere(f);
    if (!pins.length && !waiting.length) {
      return html`<div class="card small muted"><h4>Alignment</h4>Floors are drawn separately and nothing says how they stack. Anchor two or more points that line up through the house (the Anchor tool), with the same names on each floor.</div>`;
    }
    const useScale = (px) => { this._snapshot(); f.scale = px; this._dirty = true; this._refreshAlignment(); this.requestUpdate(); toast(this, `Scale set to ${fmtScale(px, this.hass)}. Save, then re-run calibration for this floor: its corrections were learned at the old scale`, 8000); };
    const off = row?.implied_scale && row.scale ? Math.abs(row.implied_scale / row.scale - 1) : 0;
    return html`<div class="card small">
      <h4>Alignment <span class="muted small">${pins.length} anchor${pins.length === 1 ? "" : "s"}</span></h4>
      ${!row ? html`<div class="muted">Checking…</div>`
        : row.reference ? html`<div>This is the reference floor: the others are lined up against it.</div>`
        : row.ok ? html`${(row.suspects || []).length ? html`<div class="warn"><b>${row.suspects.join(" and ")}</b> ${row.suspects.length === 1 ? "does" : "do"} not line up with the rest (${row.suspects.map((n) => fmtLen(row.misses?.[n], this.hass, 1)).join(", ")} off) and ${row.suspects.length === 1 ? "was" : "were"} left out of the fit. Most often the corner clicked here is not above the one on the other floor: a room that is longer upstairs, a wall set in from the one below. The grey rings on the plan show where the other floors put each anchor.</div>` : nothing}<div>Lined up on ${row.shared - (row.suspects || []).length} agreeing anchors, typically within <b>${fmtLen(row.rms_m, this.hass, 2)}</b>${row.worst && row.max_m >= 0.05 ? html`; worst is <b>${row.worst}</b> at ${fmtLen(row.max_m, this.hass, 2)}` : nothing}${Math.abs(row.rotation_deg) >= 0.5 ? html`. This plan is turned ${fmtNum(row.rotation_deg, 1)}° against the reference` : nothing}.</div>`
        : row.rms_m != null && row.implied_scale && row.agree_rms_m != null && row.agree_rms_m <= 0.3 ? html`<div class="warn">The anchors agree with each other (within ${fmtLen(row.agree_rms_m, this.hass, 2)}) but not at this floor's scale, so the floor cannot be lined up yet. That points at the scale, not at any anchor.</div>`
        : row.rms_m != null ? html`<div class="warn">The anchors disagree by ${fmtLen(row.rms_m, this.hass, 2)} - too much to use. Check <b>${row.worst}</b> first (${fmtLen(row.max_m, this.hass, 2)} off), or anchors that sit very close together.</div>`
        : html`<div class="muted">Not lined up yet: ${row.why}.</div>`}
      ${off >= 0.01 ? html`<div class="row">The anchors fit best at <b>${fmtScale(row.implied_scale, this.hass)}</b>; this floor is set to ${fmtScale(row.scale, this.hass)} (${fmtNum(off * 100, 1)} % apart). ${uiButton({ label: "Use the anchors' scale", kind: "text", onClick: () => useScale(row.implied_scale), title: "Set this floor's scale from its anchors. Four or more well-spread anchors usually beat one tape measurement" })}</div>` : nothing}
      ${waiting.length ? html`<div class="muted">Anchored on other floors, not here yet: ${waiting.join(", ")}.</div>` : nothing}
      ${(rep?.unlinked || []).filter((n) => pins.some((q) => q.name === n)).length ? html`<div class="muted">Only on this floor so far: ${rep.unlinked.filter((n) => pins.some((q) => q.name === n)).join(", ")}.</div>` : nothing}
    </div>`;
  }

  _renderSelection(sel, f) {
    const list = this._listFor(sel.kind, f);
    const item = list?.[sel.index];
    if (!item) return nothing;
    if (sel.kind === "radar") return this._renderRadar(item);
    if (sel.kind === "remark") {
      return html`<div class="card">
        <h4>Note</h4>
        <div class="row remark-text">${uiField({ label: "Note", value: item.text || "", onChange: (v) => this._edit("text", String(v || "")), style: "flex: 1" })}</div>
        <div class="muted small">For people only - the solver, the rooms and the spots all ignore it. x ${fmtNum(item.cords?.x, 0)}, y ${fmtNum(item.cords?.y, 0)}</div>
        <div class="row"><span class="grow"></span>${uiButton({ label: "Delete", kind: "danger", onClick: () => this._deleteSelection() })}</div>
      </div>`;
    }
    if (sel.kind === "pin") {
      const others = [...new Set((this._draft?.floor || []).filter((fl) => fl !== f).flatMap((fl) => (fl.pins || []).map((q) => q.name)))].filter(Boolean);
      const taken = new Set((f.pins || []).filter((q) => q !== item).map((q) => q.name));
      return html`<div class="card">
        <h4>Anchor</h4>
        <div class="row">${uiField({ label: "Name (the same on every floor)", value: item.name || "", onChange: (v) => { const name = String(v || "").trim(); if (!name) return; if (taken.has(name)) return toast(this, `This floor already has an anchor called ${name}`); this._edit("name", name); }, style: "flex: 1" })}</div>
        <div class="row small"><span class="muted">${others.filter((n) => !taken.has(n) && n !== item.name).length ? "On other floors:" : "Name:"}</span>${others.filter((n) => !taken.has(n) && n !== item.name).map((n) => html`<button class="chip" @click=${() => this._edit("name", n)}>${n}</button>`)}<button class="chip" title="Not the same point as any anchor on another floor: give it a name of its own" @click=${() => this._edit("name", this._freshPinName())}>New anchor</button></div>
        <div class="muted small">${item.linked ? "Linked: this name is anchored on another floor too." : "Not linked yet: anchor the same point on another floor and give it this name."}${item.miss != null && item.miss >= 0.05 ? ` Misses the fit by ${fmtLen(item.miss, this.hass, 2)}.` : ""} x ${fmtNum(item.cords?.x, 0)}, y ${fmtNum(item.cords?.y, 0)}</div>
        <div class="row"><span class="grow"></span>${uiButton({ label: "Delete", kind: "danger", onClick: () => this._deleteSelection() })}</div>
      </div>`;
    }
    const zones = (f.zones || []).filter((z) => !z.no_go);
    return html`<div class="card">
      <h4>${sel.kind === "receiver" ? "Proxy" : sel.kind === "zone" ? (item.no_go ? "No-go area" : "Room") : "Spot"}</h4>
      <div class="row">
        ${uiField({ label: "Name", value: item.entity_id || "", onChange: (v) => this._edit("entity_id", v), style: "flex: 1" })}
      </div>
      ${sel.kind === "receiver" ? html`
        <div class="row">
          ${uiSelect({ label: "Bermuda proxy", value: item.address || "", options: [{ value: "", label: "none" }, ...Object.entries(this.data?.scanners || {}).map(([addr, s]) => ({ value: addr, label: `${s.name || s.slug} · ${addr}` }))], onChange: (v) => this._edit("address", v || undefined), style: "flex: 1" })}
        </div>
        <div class="row">
          ${uiField({ label: `Mount height (${lenUnit(this.hass)})`, type: "number", step: 0.05, min: 0, max: isImperial(this.hass) ? 33 : 10, value: toDisplayLen(item.height, this.hass), onChange: (v) => this._edit("height", v === "" ? "" : fromDisplayLen(v, this.hass)), style: "width: 150px" })}
          ${uiField({ label: "Correction ×", type: "number", step: 0.001, min: 0.5, max: 2, value: item.correction ?? "", onChange: (v) => this._edit("correction", v), style: "width: 150px" })}
        </div>
        ${uiSwitch({ label: "Use in positioning", checked: item.solve !== false,
          onChange: (v) => this._edit("solve", v ? undefined : false) })}
        <div class="muted small">Off keeps this proxy out of the solve, the floor election, anchoring and spot evidence, while it stays placed, drawn, calibrated and self-tested. For a proxy that hears fine but reads wrong where it sits - metal under the counter, a range hood - and keeps dragging the fix its way.</div>
        ${uiSwitch({ label: "Leave this proxy's correction alone", checked: item.calibrate === false,
          onChange: (v) => this._edit("calibrate", v ? false : undefined) })}
        <div class="muted small">Calibration skips it: it is left out of the fit and its correction is never overwritten. For a radio whose distances are the wrong shape rather than the wrong scale - a tablet or a phone, which read long up close and far too short across a room - where no single multiplier fits.</div>
        <div class="muted small">${item.unmatched ? "Bermuda does not report this proxy right now." : "Linked."} x ${fmtNum(item.cords?.x, 0)}, y ${fmtNum(item.cords?.y, 0)}</div>` : nothing}
      ${sel.kind === "zone" ? uiSwitch({ label: "No-go area (things can never be here)", checked: !!item.no_go, onChange: (v) => this._edit("no_go", v) }) : nothing}
      ${sel.kind === "zone" && !item.no_go ? this._renderAreaLink(item) : nothing}
      ${sel.kind === "subzone" ? html`
        <div class="row">
          ${uiSelect({ label: "Parent room", value: item.parent || "", options: [{ value: "", label: "none" }, ...zones.map((z) => ({ value: z.zone_id, label: z.entity_id }))], onChange: (v) => this._edit("parent", v || null), style: "flex: 1" })}
          <label class="field">Colour<input type="color" .value=${this._hex(item.color)} @change=${(e) => this._edit("color", e.target.value)}></label>
        </div>
        <div class="classes">
          <div class="muted small">Takes which things? None picked means any of them. A bedside table is for a phone, a watch and keys; a cat bed is for the cat.</div>
          <div class="classpick">${this._classPicker(item)}</div>
          <div class="muted small">A ringed group goes together: pick Person or Pet and its kinds count too, shown without the grey background.</div>
        </div>
        ${this._renderSpotSize(item, f)}
        ${this._renderSpotEvidence(item, f)}` : nothing}
      <div class="row"><span class="muted small">${(item.cords?.length ?? 1)} point(s)</span><span class="grow"></span>${sel.kind === "zone" || sel.kind === "subzone" ? uiButton({ label: "Square up", icon: "mdi:vector-square", onClick: () => this._squareUp(), title: "Make every edge that is nearly horizontal, vertical or 45° exactly that. Other angles stay as drawn" }) : nothing}${uiButton({ label: "Delete", kind: "danger", onClick: () => this._deleteSelection() })}</div>
    </div>`;
  }

  /** The class icons for a spot: each family (Person with man, woman, child;
   * Pet with the dog and the cat) inside its own dotted ring so it is obvious
   * which icons travel together, then the rest on their own. Picking the
   * family lights its members too, so the spot's reach is on the screen. */
  _classPicker(item) {
    const chosen = item.classes || [];
    const members = new Set(Object.values(CLASS_FAMILIES).flat());
    const byKey = Object.fromEntries(THING_CLASSES.filter(([k]) => k).map((c) => [c[0], c]));
    const icon = (key) => {
      const [k, label, mdi] = byKey[key];
      const picked = chosen.includes(k);
      const implied = !picked && chosen.some((c) => (CLASS_FAMILIES[c] || []).includes(k));
      const title = implied ? `${label} — comes with the family` : label;
      return html`<button class="cls ${picked ? "on" : implied ? "implied" : ""}" title=${title}
                          aria-label=${title} aria-pressed=${picked || implied}
                          @click=${() => this._edit("classes", this._toggleClass(chosen, k, !picked))}>
        <ha-icon icon=${mdi}></ha-icon>
      </button>`;
    };
    // The families on their own line and the rest on the next: a ring cannot
    // break across lines, so mixing them left a ragged hole in the row.
    return html`
      <div class="picked-row">
        ${Object.entries(CLASS_FAMILIES).map(([parent, kin]) => html`
          <span class="family">${[parent, ...kin].map(icon)}</span>`)}
      </div>
      <div class="picked-row">
        ${THING_CLASSES.filter(([k]) => k && !CLASS_FAMILIES[k] && !members.has(k)).map(([k]) => icon(k))}
      </div>
    `;
  }

  /** The spot's class list with `cls` added or removed; undefined when empty,
   * so a spot that takes anything carries no key at all. */
  _toggleClass(current, cls, on) {
    const next = (current || []).filter((c) => c !== cls);
    if (on) next.push(cls);
    return next.length ? next : undefined;
  }

  _hex(color) {
    if (!color) return "#2a9d8f";
    if (color.startsWith("#")) return color;
    const m = /hsl\((\d+),\s*(\d+)%,\s*(\d+)%\)/.exec(color);
    if (!m) return "#2a9d8f";
    const h = Number(m[1]) / 360, s = Number(m[2]) / 100, l = Number(m[3]) / 100;
    const f = (n) => { const k = (n + h * 12) % 12; const a = s * Math.min(l, 1 - l); const c = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)); return Math.round(c * 255).toString(16).padStart(2, "0"); };
    return `#${f(0)}${f(8)}${f(4)}`;
  }

  static styles = [sharedStyles, widgetStyles, css`
    .classes { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
    /* One icon per class rather than seventeen labelled switches: picked is
       the page's own ink, the rest sit back in grey. The name is on hover. */
    .classpick { display: flex; flex-direction: column; gap: 6px; }
    .picked-row { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
    .family { display: inline-flex; gap: 1px; padding: 2px; border: 1.5px solid var(--divider-color); border-radius: 11px; }
    /* Sized so both family rings sit on one line of the 320px side panel. */
    .proxypick { display: flex; flex-wrap: wrap; gap: 4px; margin: 2px 0 4px; }
    /* Inside the hint, which floats over the canvas: a row in the stage's own
       flow would add height, and the canvas is sized to the stage. */
    .hint .next-pin { flex-basis: 100%; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 2px; }
    .next-pin .pair { display: inline-flex; }
    .next-pin .pair .chip:first-child { border-top-right-radius: 0; border-bottom-right-radius: 0; }
    .next-pin .pair .chip.skip { border-left: 0; border-top-left-radius: 0; border-bottom-left-radius: 0; color: var(--secondary-text-color); }
    .next-pin .chip.on { background: var(--primary-color, #03a9f4); border-color: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
    .proxypick .chip { border: 1px solid var(--divider-color, #ccc); border-radius: 14px; padding: 3px 9px; background: transparent; color: var(--primary-text-color); cursor: pointer; font: inherit; font-size: 12px; }
    .proxypick .chip.on { background: var(--primary-color, #03a9f4); border-color: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
    .proxypick .chip.on .muted { color: inherit; opacity: .8; }
    .classpick .cls { padding: 3px; border: 1px solid transparent; border-radius: 7px; background: transparent; line-height: 0; cursor: pointer; color: var(--disabled-text-color, #c4c4c4); }
    .classpick .cls ha-icon { --mdc-icon-size: 20px; }
    .classpick .cls.on { color: var(--primary-text-color); background: var(--secondary-background-color); border-color: var(--divider-color); }
    /* Pulled in by a family: the same ink as a picked one, but no fill, so it
       reads as "counts, though you did not pick it yourself". */
    .classpick .cls.implied { color: var(--primary-text-color); background: transparent; }
    .classpick .cls:hover { border-color: var(--primary-color); }
    :host { display: grid; grid-template-columns: 1fr 320px; min-height: 0; }
    .stage { position: relative; min-width: 0; }
    canvas { width: 100%; height: 100%; display: block; --sextant-map-bg: var(--card-background-color, #fff); }
    /* The tools as one compact floating panel of icon-only buttons, the
       active one filled - names in the tooltips and aria labels. Save and
       Discard keep their words: they are actions, not modes. */
    .toolbar { position: absolute; left: 10px; top: 10px; display: flex; gap: 2px; padding: 4px; border-radius: 12px; background: var(--card-background-color); box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.25)); align-items: center; z-index: 2; }
    .toolbar button.tool { display: flex; align-items: center; justify-content: center; width: 36px; height: 36px; min-width: 0; padding: 0; border: 0; border-radius: 8px; background: transparent; color: var(--primary-text-color); cursor: pointer; }
    .toolbar button.tool > span:not(.lockicons) { display: none; }
    .toolbar button.tool:hover:not([disabled]) { background: var(--secondary-background-color, rgba(0,0,0,0.06)); }
    .toolbar button.tool[disabled] { opacity: 0.4; cursor: default; }
    .toolbar button.tool:focus-visible, .zoom button:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 1px; }
    .toolbar button.tool ha-icon { --mdc-icon-size: 21px; }
    .zoom { position: absolute; right: 10px; bottom: 10px; display: flex; gap: 2px; padding: 4px; border-radius: 12px; background: var(--card-background-color); box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.25)); z-index: 2; }
    .zoom button { display: flex; align-items: center; justify-content: center; width: 32px; height: 32px; padding: 0; border: 0; border-radius: 8px; background: transparent; color: var(--primary-text-color); cursor: pointer; }
    .zoom button:hover { background: var(--secondary-background-color, rgba(0,0,0,0.06)); }
    .zoom ha-icon { --mdc-icon-size: 20px; }
    .toolbar button.active { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
    .toolbar button.lock.locked { background: var(--secondary-background-color); color: var(--secondary-text-color); }
    .card h4 { display: flex; align-items: center; gap: 6px; }
    .lockicons { position: relative; display: inline-block; }
    .lockicons .badge { position: absolute; right: -8px; bottom: -4px; --mdc-icon-size: 13px; background: var(--card-background-color); border-radius: 50%; }
    .toolbar button.lock.locked .lockicons .badge { color: var(--error-color, #b00020); }
    .sep { width: 1px; height: 24px; background: var(--divider-color); margin: 0 4px; }
    .hint { position: absolute; left: 10px; bottom: 10px; right: 128px; padding: 8px 10px; border-radius: 8px; background: var(--card-background-color); box-shadow: var(--ha-card-box-shadow, 0 1px 4px rgba(0,0,0,0.2)); font-size: 13px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .hint select { max-width: 100%; }
    .chip { padding: 2px 9px; border-radius: 999px; border: 1px solid var(--divider-color); background: var(--secondary-background-color); color: var(--primary-text-color); font: inherit; font-size: 12px; cursor: pointer; }
    .chip:hover { border-color: var(--primary-color); }
    .warn { color: var(--warning-color, #9a5b00); }
    .side { border-left: 1px solid var(--divider-color); overflow: auto; padding: 12px; }
    ul.plain { list-style: none; padding: 0; margin: 4px 0; }
    @media (max-width: 720px) { :host { grid-template-columns: 1fr; grid-template-rows: 1fr auto; } .side { border-left: 0; border-top: 1px solid var(--divider-color); max-height: 45vh; } .toolbar { flex-wrap: wrap; max-width: calc(100% - 20px); gap: 3px; padding: 4px; } .toolbar button.tool { width: 34px; height: 34px; } }
  `];
}

if (!customElements.get("sextant-edit")) customElements.define("sextant-edit", SextantEdit);
