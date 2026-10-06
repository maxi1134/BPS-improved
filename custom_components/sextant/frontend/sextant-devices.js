/**
 * Things and Bermuda modes: Bermuda without its options flow.
 *
 * section="things": what Bermuda tracks, each with a dialog for its name,
 * class (the icon on the map), height, ref trim and custom icon, and
 * everything Bermuda hears but does not track (one click to track, which
 * opens that same dialog).
 * section="bermuda": Bermuda's own things - global options, FindMy
 * accessories (with a step-by-step add) and the Tiles' binding state.
 */
import { LitElement, html, css, nothing } from "./lit.js";
import { pointInPolygon, thingColor } from "./sextant-map.js";

// Mirrors persons.LOCATES_BY_DEFAULT: classes whose place is their owner's place.
const LOCATES_BY_DEFAULT = new Set(["watch", "phone", "person", "man", "woman", "child", "cat", "dog", "paw"]);
import { sharedStyles, widgetStyles, fmtAge, fmtNum, toast, callWS, confirmDialog, uiField, uiSelect, uiSwitch, uiButton, thingName, fmtLen, lenUnit, toDisplayLen, fromDisplayLen, THING_CLASSES, classIcon, pronounKey } from "./sextant-ui.js";

const KIND_FILTERS = [["all", "Everything"], ["tile", "Tiles"], ["ibeacon", "iBeacons"], ["device", "Other devices"]];
const RECENT_SECS = 60;
// Quick colours for a thing; the picker next to them takes any colour.
const PALETTE = ["#e53935", "#fb8c00", "#fdd835", "#43a047", "#00897b", "#1e88e5", "#3949ab", "#8e24aa", "#d81b60", "#6d4c41", "#8d6e63", "#546e7a"];
const CROP_SIZE = 260;   // the cropper on screen (px)
const ICON_SIZE = 192;   // the icon that is uploaded (px)
// The iBeacon every calibration probe advertises (README, "make each probe advertise"), hex without dashes.
const PROBE_BEACON_UUID = "fde3b1502f6443baaee9867f75ee4a6f";
const FINDMY_KEYS = ["master_key", "skn", "sks", "paired_at"];
const FINDMY_GUIDE = "https://github.com/davidcoulson/bermuda/blob/fork-testing/docs/findmy.md";

class SextantDevices extends LitElement {
  static properties = {
    hass: { attribute: false },
    data: { attribute: false },
    positions: { attribute: false },
    section: { type: String },
    openThing: { attribute: false },
    _tracked: { state: true },
    _candidates: { state: true },
    _tiles: { state: true },
    _identities: { state: true },
    _bindPick: { state: true },
    _adoptPick: { state: true },
    _findmy: { state: true },
    _options: { state: true },
    _filter: { state: true },
    _kind: { state: true },
    _showAll: { state: true },
    _busy: { state: true },
    _wizard: { state: true },
    _findmyWizard: { state: true },
    _addWizard: { state: true },
    _loaded: { state: true },
    _robots: { state: true },
    _robotFloor: { state: true },
  };

  constructor() {
    super();
    this.section = "things";
    this.openThing = null;   // a thing another page asked us to open the dialog for
    this._handledThing = null;  // the request already answered (not reactive, on purpose)
    this._loaded = false;    // whether the first Bermuda fetch has come back
    this._tracked = null;
    this._candidates = null;
    this._tiles = null;
    this._identities = null;
    this._bindPick = {};
    this._adoptPick = {};
    this._findmy = null;
    this._options = null;
    this._filter = "";
    this._kind = "all";
    this._showAll = false;
    this._busy = false;
    this._wizard = null;        // thing dialog state
    this._findmyWizard = null;  // FindMy add-accessory dialog state
    this._addWizard = null;     // "Add a thing" chooser: {kind, irk, error}
    this._pendingTrack = null;  // config_value just sent to Bermuda: open its dialog once it appears
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refreshLight(), 15000);
  }

  disconnectedCallback() { super.disconnectedCallback(); clearInterval(this._timer); }

  /** Open the dialog for a thing another page sent us to.
   *
   * Waited for rather than done on the way in: the Live page knows the slug
   * before this page has its data, and the dialog reads names, classes and
   * heights out of the layout. Announcing it back lets the panel forget the
   * request, so returning here later does not reopen it. */
  updated() {
    // One updated() only: this class had two, and the later definition
    // silently replaced this one, so everything below had never run.
    if (this._wizard?.crop) this._drawCropper();
    // Wait for the Bermuda fetch as well as the layout. The six calls behind
    // _refresh are still in flight when this page mounts, so a request that
    // arrived with the page used to be answered with no address at all - and
    // answered once, because the request is cleared either way. The click on
    // Live's Edit then dropped you in the full list to hunt for the thing you
    // had already picked.
    if (!this.openThing || !this.data?.layout || !this._loaded) return;
    const slug = this.openThing;
    // Once per request, and never again: opening the dialog sets _wizard,
    // which schedules another update, which re-enters this method with
    // openThing still set - the panel clears it a tick later. Without this
    // guard that is an endless render loop, and the page locks up.
    if (this._handledThing === slug) return;
    this._handledThing = slug;
    // _tracked is a map keyed by address, as every other reader of it treats
    // it - this one asked it for .find and threw a TypeError instead, every
    // time, which is why the dialog never opened and the request was never
    // cleared either.
    const address = Object.entries(this._tracked || {}).find(([, d]) => d.slug === slug)?.[0]
      || (this.data?.entities || {})[slug]
      || null;
    this._openWizard(slug, address);
    this.dispatchEvent(new CustomEvent("thing-opened", { bubbles: true, composed: true }));
  }

  get _hasApi() { return (this.data?.features || []).includes("device_management"); }

  async _refresh() {
    // No hass yet (connectedCallback can beat the property being set): try
    // again rather than leaving _loaded false forever, which would strand a
    // pending openThing request.
    if (!this.hass) { setTimeout(() => this._refresh(), 100); return; }
    const [tracked, candidates, tiles, findmy, options, identities] = await Promise.all([
      this.hass.callWS({ type: "sextant/bermuda/tracked" }).catch(() => null),
      this.hass.callWS({ type: "sextant/bermuda/candidates" }).catch(() => null),
      this.hass.callWS({ type: "sextant/bermuda/tiles" }).catch(() => null),
      this.hass.callWS({ type: "sextant/bermuda/findmy" }).catch(() => null),
      this.hass.callWS({ type: "sextant/bermuda/options" }).catch(() => null),
      this.hass.callWS({ type: "sextant/bermuda/tile_identities" }).catch(() => null),
    ]);
    this._loadRobots();
    this._tracked = tracked?.tracked ?? null;
    this._candidates = candidates?.candidates ?? null;
    this._tiles = tiles?.tiles ?? null;
    this._identities = identities?.identities ?? null;
    this._findmy = findmy?.accessories ?? null;
    this._options = options?.options ?? null;
    this._loaded = true;
    this._openPendingTrack();
  }

  async _refreshLight() {
    if (!this.hass) return;
    const [candidates, tiles, identities] = await Promise.all([
      this.hass.callWS({ type: "sextant/bermuda/candidates" }).catch(() => null),
      this.hass.callWS({ type: "sextant/bermuda/tiles" }).catch(() => null),
      this.hass.callWS({ type: "sextant/bermuda/tile_identities" }).catch(() => null),
    ]);
    if (candidates) this._candidates = candidates.candidates;
    if (tiles) this._tiles = tiles.tiles;
    if (identities) this._identities = identities.identities;
  }

  // --- Bermuda actions -------------------------------------------------------

  async _bindTile(uid) {
    const tileId = this._bindPick[uid];
    if (!tileId) return;
    const r = await callWS(this, this.hass, { type: "sextant/bermuda/tile/bind", tile_id: tileId, uid });
    if (r) {
      toast(this, r.address ? `${thingName(this.data, tileId)} is now ${r.address}` : `${thingName(this.data, tileId)} remembered as ${uid}; it binds when that Tile is next heard`);
      this._bindPick = { ...this._bindPick, [uid]: "" };
      await this._refreshLight();
    }
  }

  async _adoptTile(tileId) {
    const address = this._adoptPick[tileId];
    if (!address) return;
    const r = await callWS(this, this.hass, { type: "sextant/bermuda/tile/adopt", tile_id: tileId, address });
    if (r) {
      toast(this, `${thingName(this.data, tileId)} is now ${r.address}`);
      this._adoptPick = { ...this._adoptPick, [tileId]: "" };
      await this._refreshLight();
      this.dispatchEvent(new CustomEvent("layout-changed"));
    }
  }

  async _track(add, remove) {
    this._busy = true;
    const r = await callWS(this, this.hass, { type: "sextant/bermuda/track", add, remove });
    this._busy = false;
    if (r) {
      toast(this, add.length ? `Tracking ${add.join(", ")}. Bermuda is reloading…` : `Stopped tracking ${remove.join(", ")}.`);
      for (const ms of [4000, 9000, 15000]) setTimeout(() => { this._refresh(); this.dispatchEvent(new CustomEvent("layout-changed")); }, ms);
    }
    return r;
  }

  /** A device just sent to Bermuda shows up in the tracked list a few seconds
   *  later, with its slug: apply the settings chosen in the dialog to it then. */
  async _openPendingTrack() {
    const p = this._pendingTrack;
    if (!p || Date.now() - p.since > 90000) { this._pendingTrack = null; return; }
    const want = String(p.value).toLowerCase();
    const hit = Object.entries(this._tracked || {}).find(([address, d]) => address.toLowerCase() === want || String(d.slug).toLowerCase() === want || String(d.config_value || "").toLowerCase() === want);
    if (!hit) return;
    this._pendingTrack = null;
    const slug = hit[1].slug;
    if (!p.settings) { this._openWizard(slug, hit[0]); return; }
    let icon = p.settings.icon;
    if (p.file) { const uploaded = await this._uploadIcon(slug, p.file); if (uploaded) icon = uploaded; }
    const r = await this._tune(slug, { ...p.settings, icon });
    if (r) toast(this, `${p.settings.name || thingName(this.data, slug)} is tracked and set up`);
  }

  /** Track: the dialog first, Bermuda only after Save (so nothing changes if you change your mind). */
  _startTrack(c) {
    this._wizard = {
      new: true, config_value: c.config_value, address: c.address, slug: null,
      name: "", placeholder: c.name || c.address, thing_class: c.kind === "tile" ? "tag" : "", height: "", ref: "", icon: "", file: null,
    };
  }

  async _tune(entity, patch) {
    const r = await callWS(this, this.hass, { type: "sextant/thing/tune", entity, ...patch });
    if (r) this.dispatchEvent(new CustomEvent("layout-changed"));
    return r;
  }

  async _uploadIcon(entity, file) {
    if (!file) return null;
    const form = new FormData();
    form.append("icon", file, file.name || `${entity}-icon.png`);
    try {
      const resp = await this.hass.fetchWithAuth("/api/sextant/upload_thing_icon", { method: "POST", body: form });
      if (!resp.ok) throw new Error(await resp.text());
      const body = await resp.json().catch(() => ({}));
      return body.icon_url || body.value || body.path || `/local/sextant_icons/${file.name}`;
    } catch (e) {
      toast(this, `icon upload failed: ${e.message || e}`, 6000);
      return null;
    }
  }

  async _removeFindMy(address, name) {
    if (!confirmDialog(`Remove FindMy accessory ${name}?`)) return;
    const r = await callWS(this, this.hass, { type: "sextant/bermuda/findmy/remove", address });
    if (r) { toast(this, "Removed"); this._refresh(); }
  }

  async _saveOptions() {
    const options = {};
    for (const [key, value] of Object.entries(this._options || {})) if (value !== null && value !== undefined) options[key] = value;
    const r = await callWS(this, this.hass, { type: "sextant/bermuda/options/set", options });
    if (r) { toast(this, "Bermuda options saved; Bermuda is reloading…"); this._options = r.options; }
  }

  // --- Thing dialog --------------------------------------------------------

  /** Battery-state and charging sensors to offer for "On-charger sensor"; the current choice always included. */
  _batterySensors(current) {
    const ids = Object.keys(this.hass?.states || {}).filter((e) =>
      (e.startsWith("sensor.") && /batter(y|ies)_(state|status)$/.test(e)) || (e.startsWith("binary_sensor.") && /charg/.test(e)));
    if (current && !ids.includes(current)) ids.push(current);
    return ids.sort();
  }

  /** Percentage battery sensors to offer for "Battery sensor"; the current choice always included.
   * Ranked by name so a thing's own sensor comes first: a pet's Holy-IoT tag is
   * sensor.great_room_holy_iot_sensors_<pet>_battery. */
  _levelSensors(current, slug) {
    const states = this.hass?.states || {};
    const ids = Object.keys(states).filter((e) => e.startsWith("sensor.")
      && (states[e]?.attributes?.device_class === "battery" || /_battery(_level)?$/.test(e))
      && states[e]?.attributes?.unit_of_measurement === "%");
    if (current && !ids.includes(current)) ids.push(current);
    const mine = (e) => (slug && e.includes(slug) ? 0 : 1);
    return ids.sort((a, b) => mine(a) - mine(b) || a.localeCompare(b));
  }

  _openWizard(slug, address) {
    const layout = this.data?.layout || {};
    this._wizard = {
      slug, address,
      name: layout.thing_names?.[slug] || "",
      placeholder: thingName({ ...this.data, names: { ...(this.data?.names || {}), [slug]: undefined } }, slug),
      thing_class: layout.thing_classes?.[slug] || "",
      pronouns: layout.thing_pronouns?.[slug] || "",
      owner: layout.thing_owners?.[slug] || "",
      locates: typeof layout.thing_locates_owner?.[slug] === "boolean" ? (layout.thing_locates_owner[slug] ? "yes" : "no") : "",
      charging: layout.thing_charging_entity?.[slug] || "",
      battery: layout.thing_battery_entity?.[slug] || "",
      height: toDisplayLen(layout.thing_heights?.[slug], this.hass),
      ref: layout.thing_ref_offsets?.[slug] ?? "",
      icon: layout.thing_icons?.[slug] || "",
      estimator: layout.thing_estimators?.[slug] || "",
      color: layout.thing_colors?.[slug] || "",
      file: null,     // the cropped photo (a Blob) waiting to be uploaded on Save
      preview: null,  // its object URL
      crop: null,     // the cropper state while a photo is being framed
    };
  }

  // --- Photo cropper: drag and zoom a picture inside a circle; the circle becomes the icon. ---

  _startCrop(file) {
    const w = this._wizard;
    if (!w || !file) return;
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      if (this._wizard !== w) return;
      w.crop = { img, url, zoom: 1, x: 0, y: 0 };
      this.requestUpdate();
      this.updateComplete.then(() => this.renderRoot.querySelector("canvas.cropper")?.scrollIntoView({ block: "nearest" }));
    };
    img.onerror = () => { toast(this, "That file is not an image the browser can read", 5000); URL.revokeObjectURL(url); };
    img.src = url;
  }

  /** Draw the photo into a square of `size` px with the crop's zoom and pan; `clip` cuts the circle. */
  _paintCrop(ctx, size, clip) {
    const c = this._wizard?.crop;
    if (!c) return;
    const base = size / Math.min(c.img.naturalWidth, c.img.naturalHeight);
    const k = base * c.zoom, f = size / CROP_SIZE;
    const dw = c.img.naturalWidth * k, dh = c.img.naturalHeight * k;
    ctx.save();
    if (clip) { ctx.beginPath(); ctx.arc(size / 2, size / 2, size / 2, 0, Math.PI * 2); ctx.clip(); }
    ctx.drawImage(c.img, (size - dw) / 2 + c.x * f, (size - dh) / 2 + c.y * f, dw, dh);
    ctx.restore();
  }

  _drawCropper() {
    const canvas = this.renderRoot.querySelector("canvas.cropper");
    const c = this._wizard?.crop;
    if (!canvas || !c) return;
    const ctx = canvas.getContext("2d"), S = CROP_SIZE;
    ctx.clearRect(0, 0, S, S);
    ctx.fillStyle = "#222"; ctx.fillRect(0, 0, S, S);
    this._paintCrop(ctx, S, false);
    // Dim everything outside the circle.
    ctx.beginPath(); ctx.rect(0, 0, S, S); ctx.arc(S / 2, S / 2, S / 2 - 1, 0, Math.PI * 2, true);
    ctx.fillStyle = "rgba(0,0,0,0.55)"; ctx.fill("evenodd");
    ctx.beginPath(); ctx.arc(S / 2, S / 2, S / 2 - 1, 0, Math.PI * 2); ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.stroke();
  }

  _cropPointer(e) {
    const c = this._wizard?.crop;
    if (!c) return;
    if (e.type === "pointerdown") { e.currentTarget.setPointerCapture(e.pointerId); c.drag = { x: e.clientX - c.x, y: e.clientY - c.y }; }
    else if (e.type === "pointermove" && c.drag) { c.x = e.clientX - c.drag.x; c.y = e.clientY - c.drag.y; this._drawCropper(); }
    else if (e.type === "pointerup" || e.type === "pointercancel") { c.drag = null; }
  }

  _cropWheel(e) {
    const c = this._wizard?.crop;
    if (!c) return;
    e.preventDefault();
    c.zoom = Math.min(4, Math.max(1, c.zoom * (e.deltaY < 0 ? 1.08 : 1 / 1.08)));
    this._drawCropper(); this.requestUpdate();
  }

  _acceptCrop() {
    const w = this._wizard;
    if (!w?.crop) return;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = ICON_SIZE;
    this._paintCrop(canvas.getContext("2d"), ICON_SIZE, true);
    canvas.toBlob((blob) => {
      if (!blob || this._wizard !== w) return;
      if (w.preview) URL.revokeObjectURL(w.preview);
      w.file = new File([blob], `${w.slug}-icon.png`, { type: "image/png" });
      w.preview = URL.createObjectURL(blob);
      w.icon = "";
      URL.revokeObjectURL(w.crop.url);
      w.crop = null;
      this.requestUpdate();
    }, "image/png");
  }

  async _saveWizard() {
    const w = this._wizard;
    if (!w) return;
    const settings = {
      name: w.name.trim() || null,
      thing_class: w.thing_class || null,
      pronouns: w.pronouns || null,
      owner: w.owner || null,
      locates_owner: w.locates === "yes" ? true : w.locates === "no" ? false : null,
      charging_entity: w.charging || null,
      battery_entity: w.battery || null,
      height: w.height === "" || w.height == null ? null : fromDisplayLen(w.height, this.hass),
      ref_offset_db: w.ref === "" || w.ref == null ? null : Number(w.ref),
      icon: w.icon || null,
      estimator: w.estimator || null,
      color: w.color || null,
    };
    if (w.new) {
      // Nothing has touched Bermuda yet. Now it does; the settings follow once the device has a slug.
      this._pendingTrack = { value: w.config_value, since: Date.now(), settings, file: w.file };
      const r = await this._track([w.config_value], []);
      if (r) this._wizard = null; else this._pendingTrack = null;
      return;
    }
    this._busy = true;
    let icon = settings.icon;
    if (w.file) { const uploaded = await this._uploadIcon(w.slug, w.file); if (uploaded) icon = uploaded; }
    const r = await this._tune(w.slug, { ...settings, icon });
    this._busy = false;
    if (r) { toast(this, `${w.name.trim() || w.placeholder} saved`); this._wizard = null; }
  }

  _renderWizard() {
    const w = this._wizard;
    if (!w) return nothing;
    const unit = lenUnit(this.hass);
    return html`<div class="modal" @click=${(e) => { if (e.target === e.currentTarget) this._wizard = null; }}>
      <div class="dialog card" role="dialog" aria-label="Thing settings">
        <h3>${w.new ? "Track " : ""}${w.placeholder} <span class="muted small">${w.address}</span></h3>
        ${w.new ? html`<p class="small muted">Nothing is sent to Bermuda until you press Track below; Cancel leaves it untracked.</p>` : nothing}
        <div class="row">
          ${uiField({ label: "Name", value: w.name, placeholder: w.placeholder, onChange: (v) => { w.name = v; this.requestUpdate(); }, style: "flex: 1; min-width: 220px" })}
        </div>
        <p class="small muted">Blank keeps the device name from Bermuda / Home Assistant.</p>
        <h4>What is this?</h4>
        <div class="classes">
          ${THING_CLASSES.map(([key, label, icon]) => html`<button class="cls ${w.thing_class === key ? "on" : ""}" title=${label} @click=${() => { w.thing_class = key; this.requestUpdate(); }}>
            ${icon ? html`<ha-icon icon=${icon}></ha-icon>` : html`<span class="initials">Ab</span>`}<span>${label}</span></button>`)}
        </div>
        <div class="row">
          ${uiSelect({ label: "Pronouns", value: w.pronouns || "", options: [
            { value: "", label: `From its class (${pronounKey({ thing_classes: { [w.slug]: w.thing_class || undefined } }, w.slug)})` },
            { value: "he", label: "He / him" }, { value: "she", label: "She / her" },
            { value: "they", label: "They / them" }, { value: "it", label: "It" },
          ], onChange: (v) => { w.pronouns = v; this.requestUpdate(); }, style: "width: 220px" })}
          <span class="small muted">How Sextant refers to this thing. A person or pet with no pronouns set is "they".</span>
        </div>
        <div class="row">
          ${uiSelect({ label: "Belongs to", value: w.owner || "", options: [{ value: "", label: "Nobody" },
            ...Object.values(this.hass?.states || {}).filter((s) => s.entity_id.startsWith("person."))
              .map((s) => ({ value: s.entity_id, label: s.attributes.friendly_name || s.entity_id }))
              .sort((a, b) => a.label.localeCompare(b.label))],
            onChange: (v) => { w.owner = v; this.requestUpdate(); }, style: "width: 220px" })}
          <span class="small muted">A Home Assistant person. Their things are grouped together on Live, and the one they are carrying gives them a location sensor of their own.</span>
        </div>
        ${w.owner ? html`<div class="row">
          ${uiSelect({ label: "Gives the owner's location", value: w.locates || "", options: [
            { value: "", label: `From its class (${LOCATES_BY_DEFAULT.has(w.thing_class) ? "yes" : "no"})` },
            { value: "yes", label: "Yes" }, { value: "no", label: "No" },
          ], onChange: (v) => { w.locates = v; this.requestUpdate(); }, style: "width: 220px" })}
          <span class="small muted">Watches, phones and a pet's own tag say where their owner is. Headphones, keys or a bag go along only some of the time, so by default they don't.</span>
        </div>
        <div class="row">
          ${uiSelect({ label: "On-charger sensor", value: w.charging || "", options: [
            { value: "", label: "None" },
            ...this._batterySensors(w.charging).map((e) => ({ value: e, label: `${this.hass?.states?.[e]?.attributes?.friendly_name || e} (${this.hass?.states?.[e]?.state ?? "?"})` })),
          ], onChange: (v) => { w.charging = v; this.requestUpdate(); }, style: "width: 320px" })}
          <span class="small muted">While this says Charging (or Charged / Full), ${w.name || w.placeholder || "this thing"} is on a charger and doesn't give its owner's location. Unavailable counts as not charging.</span>
        </div>` : nothing}
        <div class="row">
          ${uiSelect({ label: "Battery sensor", value: w.battery || "", options: [
            { value: "", label: "None" },
            ...this._levelSensors(w.battery, w.slug).map((e) => ({ value: e, label: `${this.hass?.states?.[e]?.attributes?.friendly_name || e} (${this.hass?.states?.[e]?.state ?? "?"}%)` })),
          ], onChange: (v) => { w.battery = v; this.requestUpdate(); }, style: "width: 320px" })}
          <span class="small muted">Shown on the Live page, with a badge once it drops to 20%. A tag that goes quiet looks exactly like a thing that has left, until the battery says why.</span>
        </div>
        <div class="row colour">
          <span class="avatar-preview" style="background: ${thingColor(w.slug, w.color || null)}" title="how this thing will look">${w.preview || w.icon ? html`<img src=${w.preview || w.icon} alt="">` : classIcon(w.thing_class) ? html`<ha-icon icon=${classIcon(w.thing_class)}></ha-icon>` : html`<span class="initials">${(w.name || w.placeholder || "?").slice(0, 2).toUpperCase()}</span>`}</span>
          <span class="small">Colour</span>
          ${PALETTE.map((c) => html`<button class="swatch ${w.color === c ? "on" : ""}" style="background: ${c}" title=${c} @click=${() => { w.color = c; this.requestUpdate(); }}></button>`)}
          <input type="color" title="Any colour" .value=${w.color || "#1e88e5"} @input=${(e) => { w.color = e.target.value; this.requestUpdate(); }}>
          ${w.color ? uiButton({ label: "Auto", kind: "text", onClick: () => { w.color = ""; this.requestUpdate(); }, title: "Back to the automatic colour" }) : nothing}
        </div>
        <p class="small muted">The colour is used everywhere this thing is drawn: the map, the Live list, the map card.</p>
        <div class="row">
          ${uiField({ label: `Height (${unit})`, type: "number", step: 0.05, min: 0, max: unit === "ft" ? 16 : 5, value: w.height, placeholder: String(toDisplayLen(this._defaultHeight(), this.hass)), onChange: (v) => { w.height = v; this.requestUpdate(); }, style: "width: 150px" })}
          ${uiField({ label: "Ref trim (dB)", type: "number", step: 0.5, min: -20, max: 20, value: w.ref, placeholder: "0", onChange: (v) => { w.ref = v; this.requestUpdate(); }, style: "width: 150px" })}
        </div>
        <p class="small muted">Height: how high this is usually carried or placed (a phone in a pocket about 1 m, a dog's collar 0.3 m). Ref trim: a few dB either way if this thing always reads too near or too far.</p>
        <div class="row">
          ${uiSelect({ label: "Positioning", value: w.estimator || "default", options: [{ value: "default", label: `Default (${this.data?.layout?.tuning?.position_estimator || "geometric"})` }, { value: "geometric", label: "Geometric only, no fingerprint" }, { value: "fused", label: "Fused" }, { value: "fingerprint", label: "Fingerprint only" }], onChange: (v) => { w.estimator = v === "default" ? "" : v; this.requestUpdate(); }, style: "min-width: 280px" })}
        </div>
        <p class="small muted">Positioning: the estimator for this thing alone. If the fingerprint makes placement worse (a Tile or a tag whose radio reads unlike the phones), pick geometric only.</p>
        <h4>Photo or custom icon <span class="muted small">optional, drawn instead of the class icon</span></h4>
        ${w.crop ? html`<div class="cropper">
          <canvas class="cropper" width=${CROP_SIZE} height=${CROP_SIZE}
                  @pointerdown=${(e) => this._cropPointer(e)} @pointermove=${(e) => this._cropPointer(e)} @pointerup=${(e) => this._cropPointer(e)} @pointercancel=${(e) => this._cropPointer(e)}
                  @wheel=${(e) => this._cropWheel(e)}></canvas>
          <div class="row">
            <span class="small muted">Drag to move</span>
            <input type="range" min="1" max="4" step="0.02" .value=${String(w.crop.zoom)} title="Zoom" @input=${(e) => { w.crop.zoom = Number(e.target.value); this._drawCropper(); }}>
            ${uiButton({ label: "Use this crop", kind: "primary", onClick: () => this._acceptCrop() })}
            ${uiButton({ label: "Cancel", kind: "text", onClick: () => { URL.revokeObjectURL(w.crop.url); w.crop = null; this.requestUpdate(); } })}
          </div>
        </div>` : html`<div class="row">
          ${w.preview ? html`<img class="icon round" src=${w.preview} alt=""><span class="small muted">cropped photo, uploaded on ${w.new ? "Track" : "Save"}</span>` : w.icon ? html`<img class="icon round" src=${w.icon} alt="">` : nothing}
          <label class="btn small" title="Pick a photo, then frame it in the circle">Photo…<input type="file" accept="image/*" hidden @change=${(e) => { this._startCrop(e.target.files[0] || null); e.target.value = ""; }}></label>
          ${uiSelect({ label: "Icon", value: w.preview ? "none" : (w.icon || "none"), options: [{ value: "none", label: w.preview ? "the cropped photo" : "class icon" }, ...(this.data?.icons || []).map((i) => ({ value: i.value, label: i.label }))], onChange: (v) => { w.icon = v === "none" ? "" : v; if (w.icon) { w.file = null; if (w.preview) URL.revokeObjectURL(w.preview); w.preview = null; } this.requestUpdate(); }, style: "min-width: 180px" })}
          ${(w.preview || w.icon) ? uiButton({ label: "Remove", kind: "text", onClick: () => { w.icon = ""; w.file = null; if (w.preview) URL.revokeObjectURL(w.preview); w.preview = null; this.requestUpdate(); } }) : nothing}
        </div>`}
        <div class="row end">
          ${uiButton({ label: "Cancel", kind: "text", onClick: () => { this._wizard = null; } })}
          ${uiButton({ label: this._busy ? (w.new ? "Tracking…" : "Saving…") : (w.new ? "Track" : "Save"), kind: "primary", disabled: this._busy, onClick: () => this._saveWizard() })}
        </div>
      </div>
    </div>`;
  }

  // --- FindMy dialog ---------------------------------------------------------

  _openFindMyWizard() { this._findmyWizard = { step: 1, text: "", parsed: [], errors: [], names: {} }; }

  _parseFindMy(text) {
    const w = this._findmyWizard;
    w.text = text;
    w.parsed = []; w.errors = [];
    const trimmed = text.trim();
    if (!trimmed) { this.requestUpdate(); return; }
    let items;
    try {
      const value = JSON.parse(trimmed);
      items = Array.isArray(value) ? value : [value];
    } catch {
      // Several objects pasted one after another, or one per line.
      items = [];
      for (const chunk of trimmed.split(/\}\s*(?:,|\n)\s*\{/)) {
        const s = chunk.trim().replace(/^\{?/, "{").replace(/\}?$/, "}");
        try { items.push(JSON.parse(s)); } catch { w.errors.push(`Not valid JSON: ${s.slice(0, 40)}…`); }
      }
    }
    items.forEach((it, i) => {
      if (!it || typeof it !== "object") { w.errors.push(`Item ${i + 1} is not an object`); return; }
      const missing = FINDMY_KEYS.filter((k) => !it[k]);
      if (missing.length) { w.errors.push(`Item ${i + 1}${it.name ? ` (${it.name})` : ""} is missing ${missing.join(", ")}`); return; }
      w.parsed.push({ json: JSON.stringify(it), name: it.name || "", model: it.model || "", identifier: it.identifier || "" });
    });
    this.requestUpdate();
  }

  async _addFindMyAll() {
    const w = this._findmyWizard;
    if (!w?.parsed.length) return;
    this._busy = true;
    let added = 0;
    for (const [i, item] of w.parsed.entries()) {
      const name = (w.names[i] ?? item.name ?? "").trim() || null;
      const r = await callWS(this, this.hass, { type: "sextant/bermuda/findmy/add", accessory_json: item.json, name });
      if (r) added++;
    }
    this._busy = false;
    toast(this, `Added ${added} of ${w.parsed.length} accessor${w.parsed.length === 1 ? "y" : "ies"}`);
    if (added) { this._findmyWizard = null; this._refresh(); }
  }

  /** What kind of thing is being added, and what each kind needs. An address
   * that never changes needs nothing: Bermuda already hears it and it is in
   * the list below. The rest need a key or a binding, each of which already
   * has a flow - this is the signpost to the right one. */
  _addKinds() {
    return [
      ["irk", "mdi:cellphone-key", "A phone or watch",
       "A modern phone changes its Bluetooth address every few minutes. Only its Identity Resolving Key can follow it, and only the phone can give you that key."],
      ["findmy", "mdi:apple", "An AirTag or FindMy tag",
       "It rotates its address on a schedule fixed when it was paired, so Bermuda needs the pairing keys from the Mac it was paired on."],
      ["tile", "mdi:tag", "A Tile",
       "Tiles rotate too. Bermuda follows one across rotations once you tell it which Tile is which, on the Bermuda page."],
      ["plain", "mdi:bluetooth", "Anything else",
       "An iBeacon, a fitness band, a tag with a fixed address: nothing to paste. Once a couple of proxies hear it, it is in the list below - press Track there."],
    ];
  }

  async _addIrk() {
    const w = this._addWizard;
    this._busy = "irk";
    const r = await callWS(this, this.hass, { type: "sextant/irk/add", irk: (w.irk || "").trim() });
    this._busy = null;
    if (r) {
      toast(this, `${r.title || "Phone"} added; it appears below once a proxy hears it`);
      this._addWizard = null;
      this._refresh();
    }
  }

  _renderAddWizard() {
    const w = this._addWizard;
    if (!w) return nothing;
    const kinds = this._addKinds();
    const close = () => { this._addWizard = null; };
    return html`<div class="modal" @click=${(e) => { if (e.target === e.currentTarget) close(); }}>
      <div class="dialog card wide" role="dialog" aria-label="Add a thing">
        <h3>Add a thing</h3>
        ${!w.kind ? html`
          <p class="small muted">What is it? How a thing is followed depends on whether its Bluetooth address stays put.</p>
          <div class="kinds">
            ${kinds.map(([kind, icon, title, blurb]) => html`
              <button class="kind" @click=${() => { this._addWizard = { ...w, kind }; }}>
                <ha-icon icon=${icon}></ha-icon>
                <span><b>${title}</b><br><span class="muted small">${blurb}</span></span>
              </button>`)}
          </div>` : nothing}
        ${w.kind === "irk" ? html`
          <ol class="steps">
            <li><b>Get the key.</b> On an iPhone it is in a HomeKit or a Home Assistant companion-app export; on Android, from a Bluetooth pairing dump. It is 32 hex characters, or base64 ending in <code>=</code>. An <code>irk:</code> in front is fine.</li>
            <li><b>Keep the phone awake and near a proxy</b> while you add it: Home Assistant only accepts a key it can watch resolve an address right now.</li>
            <li><b>Track it</b> from the list below once it appears.</li>
          </ol>
          ${uiField({ label: "Identity Resolving Key", value: w.irk, placeholder: "a1b2c3…  or  irk:a1b2c3…", onChange: (v) => { this._addWizard = { ...w, irk: v }; }, style: "width: 100%" })}
          <p class="small muted">Home Assistant's own Private BLE Device integration holds the key and resolves the address; Bermuda tracks what it resolves. Sextant just fills in the form.</p>` : nothing}
        ${w.kind === "findmy" ? html`
          <p class="small">An AirTag needs its pairing keys, which only the Mac it was paired from holds. That has its own walkthrough.</p>` : nothing}
        ${w.kind === "tile" ? html`
          <p class="small">A Tile is bound on the Bermuda page, where the Tiles it hears are listed with the addresses they are using.</p>` : nothing}
        ${w.kind === "plain" ? html`
          <p class="small">Nothing to paste. Bring it near a couple of proxies, then find it in <b>Heard, not tracked</b> below and press <b>Track…</b>. If it is hiding, switch on <b>Show all</b>.</p>` : nothing}
        <div class="row end">
          ${w.kind ? uiButton({ label: "Back", kind: "text", onClick: () => { this._addWizard = { ...w, kind: null }; } }) : nothing}
          ${uiButton({ label: "Close", kind: "text", onClick: close })}
          ${w.kind === "irk" ? uiButton({ label: this._busy === "irk" ? "Adding…" : "Add the phone", kind: "primary", disabled: this._busy === "irk" || !(w.irk || "").trim(), onClick: () => this._addIrk() }) : nothing}
          ${w.kind === "findmy" ? uiButton({ label: "Open the FindMy walkthrough", kind: "primary", onClick: () => { close(); this._openFindMyWizard(); } }) : nothing}
          ${w.kind === "tile" ? uiButton({ label: "Go to the Bermuda page", kind: "primary", onClick: () => { close(); this.dispatchEvent(new CustomEvent("quick-nav", { detail: "bermuda", bubbles: true, composed: true })); } }) : nothing}
        </div>
      </div>
    </div>`;
  }

  _renderFindMyWizard() {
    const w = this._findmyWizard;
    if (!w) return nothing;
    return html`<div class="modal" @click=${(e) => { if (e.target === e.currentTarget) this._findmyWizard = null; }}>
      <div class="dialog card wide" role="dialog" aria-label="Add FindMy accessories">
        <h3>Add FindMy accessories</h3>
        <ol class="steps">
          <li><b>Export the keys.</b> An AirTag or FindMy tag changes its address every 15 minutes on a schedule seeded when it was paired, so Bermuda needs the pairing keys rather than an address. Where they come from depends on your Mac: macOS 14 and earlier still keeps them on disk, and <code>tools/findmy_export.py</code> reads them; macOS 15 and later does not, so they have to come out of iCloud first. <a href=${FINDMY_GUIDE} target="_blank" rel="noopener">The walkthrough</a> has the commands for both, and what to expect from AirPods and a Siri Remote.</li>
          <li><b>Paste them here</b>, one or several, in any order. Each needs ${FINDMY_KEYS.map((k, i) => html`${i ? ", " : ""}<code>${k}</code>`)}; a name and model come along when the export had them.</li>
          <li><b>Name and add.</b> Each accessory becomes a <code>findmy_…</code> device in Bermuda; track it from the Things page once it has been seen.</li>
        </ol>
        <textarea placeholder='{"master_key": "...", "skn": "...", "sks": "...", "paired_at": "...", "name": "Keys"}' .value=${w.text} @input=${(e) => this._parseFindMy(e.target.value)}></textarea>
        ${w.errors.length ? html`<ul class="errors">${w.errors.map((e) => html`<li>${e}</li>`)}</ul>` : nothing}
        ${w.parsed.length ? html`<div class="wrap"><table>
          <tr><th>Accessory</th><th>Model</th><th>Name in Bermuda</th></tr>
          ${w.parsed.map((p, i) => html`<tr>
            <td>${p.name || html`<span class="muted">unnamed</span>`}<br><span class="muted small">${p.identifier || "id from the key"}</span></td>
            <td>${p.model || "—"}</td>
            <td>${uiField({ value: w.names[i] ?? p.name ?? "", placeholder: p.name || "name", onChange: (v) => { w.names[i] = v; }, style: "width: 220px" })}</td>
          </tr>`)}
        </table></div>` : nothing}
        <div class="row end">
          ${uiButton({ label: "Cancel", kind: "text", onClick: () => { this._findmyWizard = null; } })}
          ${uiButton({ label: this._busy ? "Adding…" : `Add ${w.parsed.length || ""} accessor${w.parsed.length === 1 ? "y" : "ies"}`, kind: "primary", disabled: this._busy || !w.parsed.length, onClick: () => this._addFindMyAll() })}
        </div>
      </div>
    </div>`;
  }

  // --- render ----------------------------------------------------------------

  render() {
    if (!this._hasApi) {
      return html`<div class="page"><div class="card">This Bermuda build has no device-management API. Update Bermuda to fork-testing.15 or later to manage things from here.</div></div>`;
    }
    return html`<div class="page">${this.section === "bermuda" ? this._renderBermuda() : this._renderThings()}</div>${this._renderWizard()}${this._renderFindMyWizard()}${this._renderAddWizard()}`;
  }

  /** The carry height used for a thing without one of its own: the layout's thing_height, else 1 m. */
  _defaultHeight() {
    const h = this.data?.layout?.thing_height;
    return typeof h === "number" && h >= 0 && h <= 5 ? h : 1.0;
  }

  /** The proxies' own iBeacon (every ESPHome probe advertises the same one) is not a device to track.
   *  Its name is the ESPHome device name, which ends in the WiFi MAC's last six hex digits; the
   *  scanner's slug carries the same six (great_room_eth_d83d6c for ble-esp32-eth-d83d6c). */
  _isProxyBeacon(c) {
    if (c.kind !== "ibeacon") return false;
    if (String(c.address || c.config_value || "").toLowerCase().replace(/-/g, "").startsWith(PROBE_BEACON_UUID)) return true;
    const name = String(c.name || "").toLowerCase();
    const scanners = Object.values(this.data?.scanners || {});
    if (scanners.some((s) => [s.name, s.slug].filter(Boolean).some((n) => n.toLowerCase() === name))) return true;
    const hex = /([0-9a-f]{6})$/.exec(name.replace(/[^0-9a-z]/g, ""))?.[1];
    return !!hex && scanners.some((s) => `${s.slug || ""} ${s.name || ""}`.toLowerCase().includes(hex));
  }

  /** Addresses of every proxy placed on a floor plan: the only ones whose hearing counts. */
  _placedAddresses() {
    const out = new Set();
    for (const f of this.data?.layout?.floor || []) for (const r of f.receivers || []) if (r.address) out.add(String(r.address).toLowerCase());
    return out;
  }

  /** Heard only by proxies that are not on any floor plan (a kiosk, a test board): not worth listing. */
  _onlyUnplaced(c, placed) {
    if (!Array.isArray(c.scanner_addresses) || !placed.size) return false;
    return c.scanner_addresses.length > 0 && !c.scanner_addresses.some((a) => placed.has(String(a).toLowerCase()));
  }

  /** Where a heard device is: the room (and floor) of the loudest placed proxy that hears it. */
  _heardWhere(c, index) {
    const heard = Array.isArray(c.heard_by) ? c.heard_by : [];
    const hit = heard.find((h) => index.has(String(h.address || "").toLowerCase()));
    if (!hit) return null;
    const p = index.get(String(hit.address).toLowerCase());
    return { room: p.room, floor: p.floor, proxy: p.name, rssi: hit.rssi };
  }

  _renderThings() {
    const layout = this.data?.layout || {};
    const placed = this._placedAddresses();
    const index = this._placedIndex();
    const heights = layout.thing_heights || {}, offsets = layout.thing_ref_offsets || {}, icons = layout.thing_icons || {}, classes = layout.thing_classes || {}, colors = layout.thing_colors || {};
    const live = new Map((this.positions?.positions || []).map((p) => [p.ent, p]));
    const tracked = Object.entries(this._tracked || {}).sort((a, b) => thingName(this.data, a[1].slug).localeCompare(thingName(this.data, b[1].slug)));
    const filter = this._filter.toLowerCase();
    const placedHearing = (c) => (Array.isArray(c.heard_by) ? c.heard_by : []).filter((h) => placed.has(String(h.address || "").toLowerCase())).length;
    const all = (this._candidates || []).filter((c) => !this._isProxyBeacon(c) && !this._onlyUnplaced(c, placed));
    const recent = all.filter((c) => (c.last_seen_age ?? 1e9) <= RECENT_SECS);
    // One proxy cannot place a device (the solver wants three), so something only one placed proxy hears is
    // noise here: a neighbour's gadget at the edge of range, a proxy's own radar module. "Show all" lifts this.
    const lonely = new Set(placed.size ? recent.filter((c) => placedHearing(c) < 2) : []);
    const haystack = (c) => { const w = this._heardWhere(c, index); return `${c.name} ${c.address} ${c.manufacturer || ""} ${c.apple_summary || ""} ${c.area_name || ""} ${c.kind} ${w ? `${w.room || ""} ${w.floor || ""} ${w.proxy || ""}` : ""}`.toLowerCase(); };
    const candidates = (this._showAll ? all : recent.filter((c) => !lonely.has(c)))
      .filter((c) => (this._kind === "all" || c.kind === this._kind) && (!filter || haystack(c).includes(filter)))
      .sort((a, b) => (a.last_seen_age ?? 1e9) - (b.last_seen_age ?? 1e9));
    return html`
      <section class="card">
        <h3>Tracked <span class="muted">${tracked.length}</span>
          <span class="grow"></span>
          ${uiButton({ label: "Add a thing…", kind: "primary", icon: "mdi:plus", onClick: () => { this._addWizard = { kind: null, irk: "", error: null }; } })}
        </h3>
        <div class="wrap"><table class="compact">
          <tr><th>Thing</th><th>Where</th><th class="num">Height</th><th class="num">Ref trim</th><th></th></tr>
          ${tracked.map(([address, d]) => {
            const slug = d.slug, p = live.get(slug), name = thingName(this.data, slug), mdi = classIcon(classes[slug]);
            return html`<tr>
              <td class="who">
                <button class="iconpick" title="Change the icon or class of ${name}" @click=${() => this._openWizard(slug, address)}>
                  ${icons[slug] ? html`<img class="icon round" src=${icons[slug]} alt="" style="box-shadow: 0 0 0 2px ${thingColor(slug, colors[slug])}">` : html`<ha-icon class="icon" icon=${mdi || "mdi:tag-outline"} style="color: ${thingColor(slug, colors[slug])}"></ha-icon>`}
                </button>
                <div><a href="#" class="name" title="Edit name, class, height, ref trim and icon" @click=${(e) => { e.preventDefault(); this._openWizard(slug, address); }}>${name}</a><br><span class="muted small">${address}${classes[slug] ? ` · ${(THING_CLASSES.find(([k]) => k === classes[slug]) || [])[1] || classes[slug]}` : ""}${layout.thing_estimators?.[slug] ? ` · ${layout.thing_estimators[slug]} only` : ""}</span></div>
              </td>
              <td>${p ? html`${p.zone}${p.sub_zone && p.sub_zone !== "unknown" ? ` · ${p.sub_zone}` : ""}<br><span class="muted small">${p.floor}</span>`
                : d.last_seen_age != null ? html`<span class="muted">no position</span><br><span class="muted small">seen ${fmtAge(d.last_seen_age)} ago</span>`
                : html`<span class="muted">not heard yet</span>`}</td>
              <td class="num">${heights[slug] != null ? fmtLen(heights[slug], this.hass) : html`<span class="muted" title="the default carry height; click the name to give this thing its own">${fmtLen(this._defaultHeight(), this.hass)}</span>`}</td>
              <td class="num">${offsets[slug] != null && offsets[slug] !== 0 ? `${offsets[slug] > 0 ? "+" : ""}${fmtNum(offsets[slug], 1)} dB` : html`<span class="muted">0</span>`}</td>
              <td class="actions">
                <button class="iconbtn" title="Edit ${name}" @click=${() => this._openWizard(slug, address)}><ha-icon icon="mdi:pencil-outline"></ha-icon></button>
                <button class="iconbtn danger" title="Stop tracking ${name}" ?disabled=${this._busy} @click=${() => confirmDialog(`Stop tracking ${name}?`) && this._track([], [address])}><ha-icon icon="mdi:trash-can-outline"></ha-icon></button>
              </td>
            </tr>`;
          })}
          ${tracked.length ? nothing : html`<tr><td colspan="5" class="muted">Nothing tracked yet.</td></tr>`}
        </table></div>
        <p class="small muted">Click a name to set its name, class, height, ref trim or icon.</p>
      </section>

      <section class="card">
        <h3>Heard, not tracked <span class="muted">${candidates.length}${this._showAll ? "" : ` in the last ${RECENT_SECS} s`}</span>${!this._showAll && lonely.size ? html` <span class="muted small" title="One proxy cannot position a device; switch on Show all to see them">· ${lonely.size} heard by a single proxy, hidden</span>` : nothing}</h3>
        <div class="row">
          <input class="grow" type="search" placeholder="Filter by name, address, maker, room, floor, proxy or kind" .value=${this._filter} @input=${(e) => { this._filter = e.target.value; }}>
          ${uiSelect({ label: "Kind", value: this._kind, options: KIND_FILTERS.map(([v, l]) => ({ value: v, label: l })), onChange: (v) => { this._kind = v; }, style: "min-width: 150px" })}
          <span class="chips">${uiSwitch({ label: `Show all (${all.length})`, checked: this._showAll, onChange: (v) => { this._showAll = v; } })}</span>
          ${uiButton({ label: "Refresh", kind: "text", icon: "mdi:refresh", onClick: () => this._refreshLight() })}
        </div>
        <div class="wrap"><table class="compact">
          <tr><th>Device</th><th>Maker</th><th>Where</th><th class="num">Proxies</th><th class="num">Signal</th><th>Seen</th><th></th></tr>
          ${candidates.slice(0, 200).map((c) => { const w = this._heardWhere(c, index); return html`<tr>
            <td><b>${c.name}</b> ${c.kind === "tile" ? html`<span class="pill">Tile</span>` : c.kind === "ibeacon" ? html`<span class="pill">iBeacon</span>` : nothing}<br><span class="muted small">${c.address}</span></td>
            <td>${c.family || c.manufacturer || html`<span class="muted">unknown</span>`}
              ${c.family && c.manufacturer && c.manufacturer !== c.family ? html`<br><span class="small muted">${c.manufacturer}</span>` : nothing}
              ${c.apple_summary ? html`<br><span class="small muted">${c.apple_summary}</span>`
                : c.family_rotates ? html`<br><span class="small muted" title="Its address changes, so it cannot be followed by address alone">rotating address</span>`
                : c.address_type === "bd_addr_random_resolvable" ? html`<br><span class="small muted">rotating address (IRK)</span>` : nothing}</td>
            <td>${w ? html`${w.room || w.floor}${w.room ? html`<br><span class="muted small">${w.floor}</span>` : nothing}<br><span class="muted small">${w.proxy}</span>` : c.area_name ? html`${c.area_name}` : html`<span class="muted">—</span>`}</td>
            <td class="num" title="placed proxies hearing it">${placed.size ? placedHearing(c) : c.scanners}</td>
            <td class="num" title="strongest reading (RSSI)">${w ? w.rssi : c.best_rssi ?? "—"} dBm</td>
            <td class="small">${fmtAge(c.last_seen_age)} ago<br><span class="muted">first ${fmtAge(c.first_seen_age)}</span></td>
            <td>${uiButton({ label: "Track…", kind: "primary", disabled: this._busy, onClick: () => this._startTrack(c), title: "Choose its name, class and height first; Bermuda tracks it when you confirm" })}</td>
          </tr>`; })}
          ${candidates.length ? nothing : html`<tr><td colspan="7" class="muted">${this._candidates ? (this._showAll ? "No matching devices." : `Nothing heard in the last ${RECENT_SECS} s matches; switch on "Show all" for everything Bermuda remembers.`) : "Loading…"}</td></tr>`}
        </table></div>
        <p class="small muted">Only what two or more placed proxies hear is listed: one proxy cannot position a device, and anything heard solely by an unplaced proxy (a kiosk, a test board) is left out; "Show all" lifts both. Apple FindMy tags are not in this list: they need their pairing keys, added on the Bermuda page.</p>
      </section>
      ${this._renderPeople()}
      ${this._renderRobots()}`;
  }

  _renderBermuda() {
    return html`<div class="cols">
      <section class="card">
        <h3>Bermuda global options</h3>
        ${this._options ? html`
          <form @submit=${(e) => { e.preventDefault(); this._saveOptions(); }}>
            <div class="row">
              ${[["ref_power", "Ref power (dBm at 1 m)", 1], ["attenuation", "Attenuation", 0.1], ["max_area_radius", "Max area radius (m)", 0.1],
                 ["max_velocity", "Max velocity (m/s)", 0.1], ["devtracker_nothome_timeout", "Not-home timeout (s)", 1],
                 ["update_interval", "Update interval (s)", 0.1], ["smoothing_samples", "Smoothing samples", 1]].map(([k, l, step]) =>
                uiField({ label: l, type: "number", step, value: this._options[k] ?? "", style: "width: 170px", onChange: (v) => { this._options = { ...this._options, [k]: v === "" ? null : Number(v) }; } }))}
              <span class="chips">${uiSwitch({ label: "Create scanner entities", checked: !!this._options.create_scanner_entities, onChange: (v) => { this._options = { ...this._options, create_scanner_entities: v }; } })}</span>
              <span class="chips" title="Read Tile IDs over Bluetooth. Off by default: Private ID Tiles rotate the readable ID with the address, and a connection makes them rotate on the spot.">${uiSwitch({ label: "Tile identity probes", checked: !!this._options.tile_identity_probes, onChange: (v) => { this._options = { ...this._options, tile_identity_probes: v }; } })}</span>
            </div>
            <div class="row">${uiButton({ label: "Save options", kind: "primary", onClick: () => this._saveOptions() })}<span class="muted small">Bermuda reloads to apply. These are Bermuda's units, metres and dBm.</span></div>
          </form>` : html`<div class="muted">Loading…</div>`}
      </section>

      <section class="card">
        <h3>FindMy accessories <span class="muted">${(this._findmy || []).length}</span></h3>
        <div class="wrap"><table class="compact">
          <tr><th>Accessory</th><th>Model</th><th>Status</th><th></th></tr>
          ${(this._findmy || []).map((a) => html`<tr>
            <td><b>${a.name}</b><br><span class="muted small">${a.address}</span></td>
            <td>${a.model || "—"}</td>
            <td>${a.current_source ? html`<span class="pill ok">seen as ${a.current_source}</span> <span class="muted small">${fmtAge(a.last_seen_age)} ago</span>` : a.alignment_index ? html`<span class="pill warn" title="Bermuda knows where this accessory is in its key schedule, so it knows which addresses to look for. No proxy has heard one of them yet - the tag is out of range, or its battery is flat.">not heard yet</span>` : html`<span class="pill" title="No key alignment for this accessory yet, so Bermuda is working through a wide range of addresses it could be using. This narrows the moment a proxy hears it once.">looking for it</span>`}</td>
            <td><button class="iconbtn danger" title="Remove ${a.name}" @click=${() => this._removeFindMy(a.address, a.name)}><ha-icon icon="mdi:trash-can-outline"></ha-icon></button></td>
          </tr>`)}
          ${(this._findmy || []).length ? nothing : html`<tr><td colspan="4" class="muted">None configured.</td></tr>`}
        </table></div>
        <div class="row">${uiButton({ label: "Add accessories…", kind: "primary", icon: "mdi:plus", onClick: () => this._openFindMyWizard() })}<span class="muted small">Walks through exporting the keys and pasting them.</span></div>
      </section>

      <section class="card wide">
        <h3>Tiles</h3>
        ${this._renderTiles()}
      </section>
    </div>`;
  }

  async _loadRobots() {
    const r = await this.hass.callWS({ type: "sextant/robot/list" }).catch(() => null);
    this._robots = r;
  }

  async _alignRobot(vacuum, floor) {
    this._busy = `robot:${vacuum}`;
    const r = await callWS(this, this.hass, { type: "sextant/robot/align", vacuum, floor });
    this._busy = null;
    if (!r) return;
    const f = r.fit;
    toast(this, f ? `${r.name}: ${f.pairs.length} point${f.pairs.length === 1 ? "" : "s"} agree to ${fmtNum(f.rms_m, 2)} m` : `${r.name}: fewer than two rooms match by name`);
    this._loadRobots();
    this.dispatchEvent(new CustomEvent("layout-changed", { bubbles: true, composed: true }));
  }

  async _removeRobot(vacuum, name) {
    if (!confirmDialog(`Stop placing ${name} on the plan?`)) return;
    const r = await callWS(this, this.hass, { type: "sextant/robot/remove", vacuum });
    if (r) { this._loadRobots(); this.dispatchEvent(new CustomEvent("layout-changed", { bubbles: true, composed: true })); }
  }

  /** Robot vacuums: each Roborock's own map lined up with a floor, from the
   * rooms both maps name and the dock marked on Live (robots.py). */
  _renderRobots() {
    const list = this._robots?.robots || [];
    if (!list.length) return nothing;
    const floors = (this.data?.layout?.floor || []).map((f) => f.name);
    return html`<section class="card wide">
      <h3>Robot vacuums <span class="muted small">placed from their own maps, asked every ${fmtNum(this.data?.layout?.tuning?.robot_poll_secs ?? 30, 0)} s while out and put at the dock otherwise</span></h3>
      ${this._robots?.action_available ? nothing : html`<p class="small warn">The Roborock integration here has no <code>get_vacuum_map_rooms</code> action: install the roborock override 2026.9.3.2 or later.</p>`}
      <p class="small muted">Line up pairs the rooms both maps name - by room name, or by the Home Assistant area a room is linked to - and fits the robot's map onto the floor. Then, on Live, select the robot and use <b>Mark dock</b> (<ha-icon icon="mdi:home-import-outline" style="--mdc-icon-size: 16px"></ha-icon>): the dock is the most trusted point, and the only way to settle a map with just two matching rooms.</p>
      ${list.map((r) => {
        const f = r.fit, busy = this._busy === `robot:${r.vacuum}`;
        const floor = this._robotFloor?.[r.vacuum] || r.floor || floors[0];
        return html`<div class="robot" style="padding: 8px 0; border-top: 1px solid var(--divider-color)">
          <div class="row"><b>${r.name}</b><span class="muted small">${r.state || "unknown"}${r.map_name ? ` · map "${r.map_name}"` : ""}</span><span class="grow"></span>
            ${uiSelect({ label: "Floor", value: floor, options: floors.map((n) => ({ value: n, label: n })), onChange: (v) => { this._robotFloor = { ...(this._robotFloor || {}), [r.vacuum]: v }; }, style: "width: 180px" })}
            ${uiButton({ label: busy ? "Reading its map…" : f ? "Line up again" : "Line up", kind: "primary", icon: "mdi:vector-link", disabled: busy || !this._robots?.action_available, onClick: () => this._alignRobot(r.vacuum, floor) })}
            ${r.floor ? html`<button class="iconbtn danger" title="Stop placing ${r.name}" @click=${() => this._removeRobot(r.vacuum, r.name)}><ha-icon icon="mdi:trash-can-outline"></ha-icon></button>` : nothing}
          </div>
          ${r.floor && !f ? html`<div class="small warn">Fewer than two of its rooms match a room on ${r.floor} by name. Name them alike (in the Roborock app or here), or link the rooms to Home Assistant areas of the same name, and line up again.</div>` : nothing}
          ${f ? html`<div class="small">
              <b>${f.pairs.length}</b> point${f.pairs.length === 1 ? "" : "s"} agree to <b>${fmtNum(f.rms_m, 2)} m</b> (RMS)${f.transform.reflect ? ", mirrored" : ""}, rotated ${fmtNum(f.transform.theta * 180 / Math.PI, 1)}°${f.scale_ratio ? html` · the rooms imply ${fmtNum(f.scale_ratio, 2)}× the floor's scale` : nothing}
              ${r.dock_marked ? html` · <span class="pill ok">dock marked</span>` : html` · <span class="pill warn" title="On Live, select ${r.name} and use Mark dock">dock not marked</span>`}
              <div class="muted">${f.pairs.map((p) => html`<span class="pill" title="${fmtNum(p.residual_m, 2)} m apart after the fit">${p.pair} ${fmtNum(p.residual_m, 1)} m</span> `)}</div>
              ${f.dropped?.length ? html`<div class="muted">Left out, the two maps disagree about them: ${f.dropped.map((d) => `${d.pair} (${fmtNum(d.residual_m, 1)} m)`).join(", ")}</div>` : nothing}
              ${f.pairs.length <= 2 && !r.dock_marked ? html`<div class="warn">Two points cannot tell a mirrored map from a turned one: mark the dock to settle it.</div>` : nothing}
            </div>` : nothing}
          ${r.floor && r.reads && (r.reads.ok || r.reads.failed) ? html`<div class="small muted" title=${r.reads.last_error ? `Last failure: ${r.reads.last_error}` : ""}>Position reads since Home Assistant started: <b>${r.reads.ok}</b> ok, <b>${r.reads.failed}</b> failed${r.reads.ok + r.reads.failed ? ` (${Math.round(100 * r.reads.failed / (r.reads.ok + r.reads.failed))}%)` : ""}${r.reads.failed ? " · a failed read keeps the last position" : ""}</div>` : nothing}
          ${r.unmatched && (r.unmatched.robot?.length || r.unmatched.plan?.length) ? html`<div class="small muted">No match: ${r.unmatched.robot?.length ? html`robot's <i>${r.unmatched.robot.join(", ")}</i>` : nothing}${r.unmatched.robot?.length && r.unmatched.plan?.length ? "; " : ""}${r.unmatched.plan?.length ? html`plan's <i>${r.unmatched.plan.join(", ")}</i>` : nothing}</div>` : nothing}
        </div>`;
      })}
    </section>`;
  }

  /** Each owner, where they read as being, and their GPS sources in order:
   * their things place them while home; away, the first source that is
   * neither broken nor stale (persons.judge_source) gives the zone. */
  _people() {
    return [...new Set(Object.values(this.data?.layout?.thing_owners || {}).filter((p) => typeof p === "string" && p.startsWith("person.")))].sort();
  }

  _gpsTrackerOptions(chosen) {
    return Object.values(this.hass?.states || {})
      .filter((s) => s.entity_id.startsWith("device_tracker.") && !s.entity_id.endsWith("_sextant") && !chosen.includes(s.entity_id)
        && (s.attributes?.source_type === "gps" || s.attributes?.latitude != null))
      .map((s) => ({ value: s.entity_id, label: `${s.attributes?.friendly_name || s.entity_id} · ${s.state}` }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  async _setPersonTrackers(person, trackers) {
    const r = await callWS(this, this.hass, { type: "sextant/person/trackers/set", person, trackers });
    if (r) { toast(this, "Saved; the person's location follows on the next cycle"); this.dispatchEvent(new CustomEvent("layout-changed")); }
  }

  _renderPeople() {
    const people = this._people();
    if (!people.length) return nothing;
    const table = this.data?.layout?.person_trackers || {};
    return html`<section class="card wide">
      <h3>People <span class="muted small">their things place them at home; a GPS tracker takes over when Sextant loses them</span></h3>
      ${people.map((p) => {
        const slug = p.split(".", 1)[1] || p.slice(7);
        const name = this.hass?.states?.[p]?.attributes?.friendly_name || slug;
        const loc = this.hass?.states?.[`sensor.${slug}_sextant_person_location`];
        const a = loc?.attributes || {};
        const chosen = Array.isArray(table[p]) ? table[p] : [];
        const ignored = Array.isArray(a.gps_ignored) ? a.gps_ignored : [];
        return html`<div class="person" style="padding: 6px 0; border-top: 1px solid var(--divider-color)">
          <div class="row"><b>${name}</b>
            <span class="muted small">${loc ? html`${loc.state}${a.source ? ` · via ${a.source === "ble" ? "Sextant" : a.source === "held" ? "Sextant (last place, not heard for a bit)" : a.source === "gps" ? (this.hass?.states?.[a.tracker]?.attributes?.friendly_name || a.tracker) : "nothing usable"}` : ""}${a.distance_m != null && a.source === "gps" ? ` · ${a.distance_m >= 1000 ? `${(a.distance_m / 1000).toFixed(1)} km` : `${a.distance_m} m`} from home` : ""}` : "no location yet"}</span>
            <span class="grow"></span>
            <span class="muted small">device_tracker.${slug}_sextant</span></div>
          <div class="row"><span class="muted small">GPS sources, first usable wins</span>
            ${chosen.map((t, i) => html`<span class="pill">${i + 1}. ${this.hass?.states?.[t]?.attributes?.friendly_name || t}
              <button class="iconbtn" title="Remove this source" aria-label="Remove this source" @click=${() => this._setPersonTrackers(p, chosen.filter((x) => x !== t))}><ha-icon icon="mdi:close" style="--mdc-icon-size: 14px"></ha-icon></button></span>`)}
            ${uiSelect({ label: chosen.length ? "Add another" : "Add a source", value: "", options: [{ value: "", label: "…" }, ...this._gpsTrackerOptions(chosen)], onChange: (v) => { if (v) this._setPersonTrackers(p, [...chosen, v]); }, style: "width: 280px" })}
          </div>
          ${ignored.length ? html`<div class="muted small">Disregarded right now: ${ignored.map((g) => `${this.hass?.states?.[g.entity]?.attributes?.friendly_name || g.entity} (${g.reason})`).join(", ")}</div>` : nothing}
        </div>`;
      })}
    </section>`;
  }

  _renderTiles() {
    const t = this._tiles;
    if (!t) return html`<div class="muted">Loading…</div>`;
    const bindings = Object.entries(t.bindings || {});
    if (!bindings.length) return html`<div class="muted">No Tiles configured. Track one from the Things page; Tiles show as <span class="pill">Tile</span>.</div>`;
    // Live, unbound Tile addresses with where they are, for the adopt pickers.
    const index = this._placedIndex();
    const liveTiles = (this._candidates || []).filter((c) => c.kind === "tile" && (c.last_seen_age ?? 1e9) <= 60)
      .map((c) => { const w = this._heardWhere(c, index); return { address: c.address, where: w ? `${w.room || w.floor} · ${w.proxy} (${w.rssi} dBm)` : c.area_name || "unplaced proxies only" }; })
      .sort((a, b) => a.where.localeCompare(b.where));
    return html`
      <p class="small muted">Click a Tile's name to call it "Kitchen keys"; the map and the Things page use that name. Bermuda follows each Tile across its address rotations by RSSI pattern and remembers where it last was, so it finds it again after a restart; that works while the Tile is not sitting among other Tiles. A Tile marked <span class="pill warn">not heard</span> has rotated away unseen: pick the live address that is where the Tile is and Adopt. On Private ID Tiles the ID readable over Bluetooth rotates too, so it cannot name a tag; probing stays off unless "Tile identity probes" is on above.</p>
      <div class="wrap"><table class="compact">
        <tr><th>Tile</th><th>Bound address</th><th>This Tile is…</th><th>History</th></tr>
        ${bindings.map(([id, sources]) => { const lost = t.bound_age?.[id] == null || t.bound_age[id] > 300; return html`<tr>
          <td><a href="#" class="name" title="Name this Tile (and set its class, height and icon)" @click=${(e) => { e.preventDefault(); this._openWizard(id, sources[0]); }}>${thingName(this.data, id)}</a>${this.data?.layout?.thing_names?.[id] ? nothing : html`<br><span class="muted small">click to name it</span>`}${t.uids?.[id] ? html`<br><code class="small">${t.uids[id]}</code>` : nothing}</td>
          <td><code>${sources[0] || "—"}</code>${t.bound_age?.[id] != null ? html` <span class="muted small">heard ${fmtAge(t.bound_age[id])} ago</span>` : html` <span class="pill warn">not heard</span>`}</td>
          <td>${liveTiles.length ? html`<div class="row">
            ${uiSelect({ label: lost ? "pick the tag it is now" : "re-point it", value: this._adoptPick[id] || "", options: [{ value: "", label: lost ? "choose a live Tile address…" : "leave as is" }, ...liveTiles.map((c) => ({ value: c.address, label: `${c.address} · ${c.where}` }))], onChange: (v) => { this._adoptPick = { ...this._adoptPick, [id]: v }; }, style: "min-width: 300px" })}
            ${uiButton({ label: "Adopt", kind: lost ? "primary" : "outline", disabled: !this._adoptPick[id], onClick: () => this._adoptTile(id) })}</div>` : html`<span class="muted small">no unbound Tile address heard right now</span>`}</td>
          <td class="small muted">${sources.slice(1, 4).join(" → ") || "—"}</td>
        </tr>`; })}
      </table></div>
      ${this._options?.tile_identity_probes ? this._renderIdentities(bindings.map(([id]) => id)) : nothing}
      <p class="small muted">Handovers ${t.handovers ?? 0} (${t.ambiguous_handovers ?? 0} ambiguous) · probes ${t.probes ?? 0}, failed ${t.probe_failures ?? 0}${t.probes_inherited ? `, ${t.probes_inherited} inherited` : ""}${t.probes_pending?.length ? `, pending ${t.probes_pending.length}` : ""}${t.probe_budget_left != null ? ` · ${t.probe_budget_left} connections left this hour` : ""}.
        ${t.last_handover ? html`Last: ${thingName(this.data, t.last_handover.tile)} → <code>${t.last_handover.to}</code> by ${t.last_handover.reason || "rssi pattern"}${t.last_handover.score != null ? ` (${fmtNum(t.last_handover.score, 1)} dB over ${t.last_handover.scanners} proxies)` : ""}.` : nothing}
        ${t.last_probe?.detail ? html`<br>Last probe of <code>${t.last_probe.address}</code>: ${t.last_probe.error || "ok"} ${t.last_probe.detail}` : nothing}
      </p>`;
  }

  /** The placed proxy (address -> {name, floor, room}) index, for "where is this Tile". */
  _placedIndex() {
    const out = new Map();
    for (const f of this.data?.layout?.floor || []) {
      for (const r of f.receivers || []) {
        if (!r.address || !r.cords) continue;
        const room = (f.zones || []).find((z) => !z.no_go && (z.cords || []).length >= 3 && pointInPolygon({ x: r.cords.x, y: r.cords.y }, z.cords));
        out.set(String(r.address).toLowerCase(), { name: this.data?.scanners?.[r.address]?.name || r.entity_id, floor: f.name, room: room?.entity_id || null });
      }
    }
    return out;
  }

  _renderIdentities(tileIds) {
    const ids = Object.values(this._identities || {}).sort((a, b) => (a.last_seen_age ?? 1e9) - (b.last_seen_age ?? 1e9));
    if (!ids.length) return nothing;
    const options = [{ value: "", label: "choose a Tile…" }, ...tileIds.map((id) => ({ value: id, label: thingName(this.data, id) }))];
    const placed = this._placedIndex();
    const where = (row) => {
      const heard = row.heard_by || (row.strongest ? [row.strongest] : []);
      const hit = heard.find((h) => placed.has(String(h.address || "").toLowerCase()));
      if (!hit) return { room: row.area_name || null, proxy: null, unplaced: heard.length };
      const p = placed.get(String(hit.address).toLowerCase());
      return { room: p.room ? `${p.room} · ${p.floor}` : p.floor, proxy: `${p.name} (${hit.rssi} dBm)`, unplaced: heard.length - heard.filter((h) => placed.has(String(h.address || "").toLowerCase())).length };
    };
    return html`<h4>Tile IDs heard <span class="muted small">read from the tags; pick which configured Tile each one is. To tell two apart, ring one from the Tile app, carry it to another room for two minutes and watch which ID moves.</span></h4>
      <div class="wrap"><table class="compact">
        <tr><th>Tile ID</th><th>Last heard</th><th>Where</th><th>Loudest placed proxy</th><th>Is</th><th></th></tr>
        ${ids.map((row) => { const w = where(row); return html`<tr>
          <td><code>${row.uid}</code></td>
          <td>${row.last_seen_age != null ? `${fmtAge(row.last_seen_age)} ago` : "—"}</td>
          <td>${w.room || "—"}</td>
          <td class="small">${w.proxy || html`<span class="muted">only unplaced proxies hear it</span>`}${w.unplaced && w.proxy ? html` <span class="muted">(+${w.unplaced} unplaced and ignored)</span>` : nothing}</td>
          <td>${row.tile_id ? html`<b>${thingName(this.data, row.tile_id)}</b>` : html`<div class="row">
            ${uiSelect({ label: "", value: this._bindPick[row.uid] || "", options, onChange: (v) => { this._bindPick = { ...this._bindPick, [row.uid]: v }; }, style: "min-width: 200px" })}
            ${uiButton({ label: "Bind", kind: "primary", disabled: !this._bindPick[row.uid], onClick: () => this._bindTile(row.uid) })}</div>`}</td>
          <td class="small muted">${row.addresses?.length || 0} address${row.addresses?.length === 1 ? "" : "es"}</td>
        </tr>`; })}
      </table></div>`;
  }

  static styles = [sharedStyles, widgetStyles, css`
    .kinds { display: grid; gap: 8px; margin: 10px 0; }
    .kind { display: flex; align-items: flex-start; gap: 10px; text-align: left; padding: 10px 12px; border: 1px solid var(--divider-color); border-radius: 10px; background: var(--card-background-color); color: inherit; font: inherit; cursor: pointer; }
    .kind:hover { border-color: var(--primary-color); background: var(--secondary-background-color); }
    .kind ha-icon { --mdc-icon-size: 24px; color: var(--primary-color); flex: none; margin-top: 2px; }
    h3 .grow { flex: 1; }
    h3 { display: flex; align-items: center; gap: 8px; }
    :host { display: block; overflow: auto; position: relative; }
    .cols { grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); }
    @media (max-width: 720px) { .cols { grid-template-columns: 1fr; } .row > ha-textfield, .row > ha-input, .row > ha-select, .row > .chips, .row > label.field { width: 100% !important; min-width: 0 !important; box-sizing: border-box; } .card { min-width: 0; overflow-x: hidden; } }
    .card.wide { grid-column: 1 / -1; }
    table.compact th, table.compact td { padding: 5px 8px; }
    td.who { display: flex; align-items: center; gap: 10px; }
    .icon { width: 28px; height: 28px; object-fit: contain; flex: none; color: var(--secondary-text-color); --mdc-icon-size: 26px; }
    button.iconpick { padding: 2px; border-radius: 50%; border-color: transparent; line-height: 0; }
    button.iconpick:hover { border-color: var(--primary-color); }
    a.name { font-weight: 600; color: var(--primary-text-color); text-decoration: none; border-bottom: 1px dotted var(--secondary-text-color); }
    a.name:hover { color: var(--primary-color); border-bottom-color: var(--primary-color); }
    td.actions { white-space: nowrap; }
    button.iconbtn.danger { color: var(--error-color, #b00020); border-color: transparent; }
    button.iconbtn.danger:hover { border-color: var(--error-color, #b00020); }
    .icon.round { border-radius: 50%; object-fit: cover; }
    .row.colour { align-items: center; gap: 6px; }
    .swatch { width: 22px; height: 22px; border-radius: 50%; border: 2px solid transparent; padding: 0; cursor: pointer; }
    .swatch.on { border-color: var(--primary-text-color); box-shadow: 0 0 0 1px #fff inset; }
    .row.colour input[type=color] { width: 34px; height: 26px; border: 0; padding: 0; background: none; cursor: pointer; }
    .avatar-preview { width: 32px; height: 32px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; overflow: hidden; color: #fff; border: 2px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,0.15); --mdc-icon-size: 20px; }
    .avatar-preview img { width: 100%; height: 100%; object-fit: cover; }
    .avatar-preview .initials { font-size: 11px; font-weight: 700; }
    .cropper canvas { display: block; border-radius: 8px; touch-action: none; cursor: grab; max-width: 100%; }
    .cropper input[type=range] { flex: 1; min-width: 100px; }
    .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: flex; align-items: center; justify-content: center; z-index: 20; padding: 16px; }
    .dialog { width: min(640px, 100%); max-height: 90vh; overflow: auto; }
    .dialog.wide { width: min(820px, 100%); }
    .row.end { justify-content: flex-end; margin-top: 12px; }
    .classes { display: grid; grid-template-columns: repeat(auto-fill, minmax(88px, 1fr)); gap: 6px; margin: 4px 0 10px; }
    button.cls { display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 8px 4px; font-size: 11px; line-height: 1.1; }
    button.cls ha-icon { --mdc-icon-size: 26px; }
    button.cls.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
    .initials { font-weight: 700; font-size: 16px; line-height: 26px; }
    ol.steps { padding-left: 20px; margin: 4px 0 10px; }
    ol.steps li { margin: 6px 0; font-size: 13px; }
    ul.errors { color: var(--error-color, #b00020); font-size: 13px; margin: 6px 0; padding-left: 20px; }
    details { margin-top: 8px; }
    summary { cursor: pointer; }
  `];
}

if (!customElements.get("sextant-devices")) customElements.define("sextant-devices", SextantDevices);
