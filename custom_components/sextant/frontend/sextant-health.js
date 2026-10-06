/**
 * Proxies, Calibration and Tuning modes (one element, three sections).
 *
 * section="proxies":     every placed proxy grouped by floor and room, with
 *                        a health count per group, plus the self-test.
 * section="calibration": proxy calibration runs and their corrections.
 * section="tuning":      the stability KPI with baselines, live tuning, and
 *                        history retention.
 */
import { LitElement, html, css, nothing } from "./lit.js";
import { pointInPolygon } from "./sextant-map.js";
import { sharedStyles, widgetStyles, fmtAge, fmtNum, toast, callWS, confirmDialog, uiField, uiSelect, uiSwitch, uiButton, sortFloors, thingName, proxyName, fmtLen } from "./sextant-ui.js";

// A far move is a room change to a room on the same floor whose outline is
// more than 2.5 m from the previous room's. Walked, it passes through
// somewhere in between; one the plan never saw in between is a position
// that jumped. Each pet in the house this was built on makes about ten a
// day; a rise after a change is what matters, not the level.
const FAR_MOVES_HELP = "Moves per day to a room on the same floor that is not a neighbour (outlines more than 2.5 m apart). Walked moves pass through a room in between; these did not, so they are the ones to watch.";
const QUIET_SECS = 120;   // online, but nothing heard for this long: "quiet"

/** Plain labels for the tuning keys, with a one-line meaning; the key itself is shown under the field. */
const TUNING_LABELS = {
  position_estimator: ["Position estimator", "geometric = trilateration alone; fingerprint = proxy references alone; fused = a blend of both"],
  fingerprint_weight: ["Fingerprint share of the fix", "0 is the trilateration alone, 1 the fingerprint alone"],
  fingerprint_floor_weight: ["Fingerprint share of floor confidence", "how much the fingerprint match counts in the floor election"],
  fingerprint_k: ["References averaged per fix", "the best-matching proxies whose positions are averaged"],
  fingerprint_missing_m: ["Not heard counts as (m)", "a proxy that does not hear the thing is treated as this far away"],
  fingerprint_ref_gain: ["Reference gain", "probe beacons hotter (<1) or cooler (>1) than the things"],
  fingerprint_auto_gain: ["Learn reference gain from things", "walk the gain in from every match, published per fix as fp.gain"],
  fingerprint_marks: ["Location pins as references", "each pin from the Live page is also a fingerprint reference at that point"],
  distance_estimator: ["Distance estimator", "bermuda = Bermuda's smoothed distance; median = the median of the recent raw RSSI samples"],
  median_window_secs: ["Median window (s)", "only samples newer than this feed the median"],
  median_min_samples: ["Median minimum samples", "fewer than this falls back to Bermuda's distance"],
  solver_max_receivers: ["Nearest proxies per solve", "0 uses every proxy that hears the thing"],
  solver_max_range: ["Drop readings beyond (m)", "once three proxies remain; 0 never drops"],
  solver_near_always: ["Always use proxies within (m)", "proxies this close count whatever the cap"],
  zone_hysteresis: ["Room hysteresis", "off publishes the instantaneous room every cycle"],
  zone_prob_smoothing: ["Room share smoothing", "weight on the previous cycle's room shares"],
  zone_switch_margin: ["Room switch margin", "the lead a challenger room needs"],
  zone_switch_secs: ["Room switch dwell (s)", "held that long before the room changes"],
  stationary_speed: ["Stationary below (m/s)", "slower than this counts as still"],
  stationary_secs: ["Stationary after (s)", "still this long locks the room"],
  zone_unlock_margin: ["Unlock outside room by (m)", "the fix must sit this far outside the locked room"],
  zone_unlock_secs: ["Unlock after (s)", "for this long before the lock releases"],
  subzone_switch_secs: ["Spot switch dwell (s)", "a spot change waits this long"],
  subzone_enter_prob: ["Spot entry share", "the smoothed share of the fix inside a spot needed to enter it"],
  subzone_unlock_margin: ["Leave spot outside by (m)", "the fix must sit this far outside a spot before leaving it"],
  anchor_max_m: ["Anchor within (m)", "one proxy reading closer than this can anchor the thing; 0 turns anchoring off"],
  anchor_ratio: ["Others at least × farther", "every other proxy must read at least this many times farther"],
  anchor_secs: ["Anchor after (s)", "the condition must hold this long"],
  anchor_release_m: ["Release beyond (m)", "the anchor lets go once the reading opens past this"],
  floor_switch_secs: ["Floor switch dwell (s)", "a challenger floor must lead this long"],
  floor_switch_margin: ["Floor switch margin", "the lead a challenger floor needs before that dwell starts; 0.10 holds a still thing between two near-tied floors"],
  floor_tenure_bonus: ["Tenure bonus", "extra margin an incumbent floor earns at full tenure"],
  floor_tenure_full_secs: ["Full tenure after (s)", "tenure is counted up to this"],
  floor_proximity_weight: ["Proximity weight", "how much nearest-proxy proximity scales a floor's score; 0 judges the fit alone"],
  floor_proximity_k: ["Proxies averaged for proximity", "the k nearest proxies whose distances are averaged"],
  floor_proximity_blend: ["Proximity blend", "gated = fit × ((1 − w) + w × proximity), a poor fit caps the floor; geometric = fit^(1 − w) × proximity^w, the nearest proxies can carry a floor whose fit is poor (a fix over a void)"],
  subzone_lock_release_m: ["Spot lock release (m)", "how far a still thing's fix must leave its spot before the room lock stops holding it there"],
  away_after_secs: ["Away after (s)", "unheard this long, the Live list stops waiting for a thing and calls it away; between ghost and this it is shown where it was last seen"],
  restore_state_secs: ["Resume after a restart within (s)", "a restart shorter than this resumes the elections; longer starts cold (raise before a host reboot)"],
  gps_stale_secs: ["GPS source stale after (s)", "a person's GPS tracker that has not reported for this long is passed over for the next one"],
  robot_poll_secs: ["Robot position every (s)", "how often a robot vacuum that is out is asked where it is; each ask is a map parse in the Roborock integration"],
  mmwave_fusion: ["Place things on mmWave targets", "a thing whose Bluetooth fix is near a radar target is placed on the target"],
  mmwave_auto_pins: ["Pins from mmWave", "one still target and one still thing make a location pin (at most one per thing per half hour)"],
  mmwave_pair_m: ["Claim a target within (m)", "how near a thing's Bluetooth fix must be to an mmWave target to be placed on it"],
  election_log_hours: ["Election log (hours)", "keep every floor election on disk for this long (config/sextant_election_log, ~6 MB an hour for twenty things) for tools/replay_floors.py; 0 = off"],
  stale_after_secs: ["Ghost after (s)", "a thing unheard this long is drawn faded on Live, with how long ago it was heard"],
  spot_proxy_near_m: ["Spot proxy: full within (m)", "a spot's own proxy hearing a thing this close counts fully as the thing being on the spot"],
  spot_proxy_far_m: ["Spot proxy: nothing from (m)", "from this far the spot's proxy says nothing; between the two it fades"],
  spot_proxy_ratio: ["Spot proxy: clearly nearest ×", "every other proxy must read this many times farther for full weight (none within 1.25×)"],
  zone_lock_warmup_secs: ["Lock warm-up (s)", "no stationary lock until a thing has been tracked this long since a start or a floor change"],
  fingerprint_marks_scope: ["Pins guide", "whose location pins place a thing: own = only its own; class = also those of things of its class (the cats share one another's); all = everyone's"],
  history_admin_only: ["History for admins only", "only administrators may read where things have been (scrubber, timeline, Activity); live positions stay visible to everyone"],
  history_hours: ["History kept (hours)", "how far back the history scrubber, the timeline and Activity reach (1 to 168)"],
  calibration_target: ["Calibration writes to", "sextant = a per-proxy factor in the layout; bermuda = per-scanner RSSI offsets in Bermuda"],
  correction_close_fade: ["Fade stretch up close", "a proxy calibration stretches (factor above 1) pushes a thing lying right beside it away; fade that stretch out at short range"],
  correction_fade_near_m: ["No stretch within (m)", "a reading this close gets none of its proxy's stretch"],
  correction_fade_far_m: ["Full stretch from (m)", "from this far out the proxy's stretch applies in full; between the two it fades in"],
};

// The self-test and the advice take seconds to compute and are worth keeping:
// switching to another page tears this element down, so the last result is
// stored in the browser and restored when the page comes back (for a day),
// and flagged as stale with a Refresh prompt once it is over an hour old.
const REMEMBER_MS = 24 * 60 * 60 * 1000;
const STALE_MS = 60 * 60 * 1000;
function remember(key, value) {
  try { localStorage.setItem(`sextant.${key}`, JSON.stringify({ at: Date.now(), value })); } catch { /* private mode */ }
}
function recall(key) {
  try {
    const raw = localStorage.getItem(`sextant.${key}`);
    if (!raw) return null;
    const { at, value } = JSON.parse(raw);
    return Date.now() - at <= REMEMBER_MS ? { ...value, at } : null;
  } catch { return null; }
}
function stale(result) {
  return !!(result && result.at && Date.now() - result.at > STALE_MS);
}

class SextantHealth extends LitElement {
  static properties = {
    hass: { attribute: false },
    data: { attribute: false },
    positions: { attribute: false },
    floor: { type: String },
    section: { type: String },
    _receivers: { state: true },
    _cal: { state: true },
    _selftest: { state: true },
    _advice: { state: true },
    _kpi: { state: true },
    _accuracy: { state: true },
    _kpiHours: { state: true },
    _baselines: { state: true },
    _baseline: { state: true },
    _baselineName: { state: true },
    _linking: { state: true },
    _busy: { state: true },
    _tuning: { state: true },
    _calDuration: { state: true },
    _open: { state: true },
  };

  constructor() {
    super();
    this.section = "proxies";
    this._receivers = null;
    this._cal = null;
    this._selftest = recall("selftest");
    this._advice = recall("advice");
    this._kpi = null;
    this._kpiHours = 12;
    this._baselines = [];
    this._baseline = "";
    this._baselineName = "";
    this._linking = null;
    this._busy = null;
    this._tuning = {};
    this._calDuration = 600;
    this._open = new Set();   // expanded floor/room groups
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._poll(), 10000);
  }

  disconnectedCallback() { super.disconnectedCallback(); clearInterval(this._timer); }

  updated(changed) {
    if (changed.has("data") && this.data) this._tuning = { ...(this.data.layout?.tuning || {}) };
  }

  async _refresh() {
    if (!this.hass) return;
    const [rx, cal] = await Promise.all([
      this.hass.callWS({ type: "sextant/receivers" }).catch(() => null),
      this.hass.callWS({ type: "sextant/calibration/status" }).catch(() => null),
    ]);
    this._receivers = rx;
    this._cal = cal;
    this._loadBaselines();
  }

  async _poll() {
    if (!this.hass) return;
    const cal = await this.hass.callWS({ type: "sextant/calibration/status" }).catch(() => null);
    if (cal) this._cal = cal;
    if (this._cal?.state === "sampling" || this._cal?.mode === "auto") return;
    const rx = await this.hass.callWS({ type: "sextant/receivers" }).catch(() => null);
    if (rx) this._receivers = rx;
  }

  /** The floor calibration acts on: the one picked in the header. */
  get _calFloor() { return this.floor || this._cal?.floor || this.data?.layout?.floor?.[0]?.name || null; }

  async _calAction(action, extra = {}) {
    this._busy = action;
    const r = await callWS(this, this.hass, { type: "sextant/calibration/action", action, ...extra });
    this._busy = null;
    if (r) {
      this._cal = r;
      if (action === "apply") { toast(this, `Applied corrections to ${r.applied} proxy(ies)`); this.dispatchEvent(new CustomEvent("layout-changed")); }
      else if (action === "reset") { toast(this, `Reset ${r.reset} proxy(ies)`); this.dispatchEvent(new CustomEvent("layout-changed")); }
    }
  }

  async _runSelftest() {
    this._busy = "selftest";
    const r = await callWS(this, this.hass, { type: "sextant/selftest" });
    this._busy = null;
    if (r) { this._selftest = { ...r, at: Date.now() }; remember("selftest", r); }
  }

  async _runKpi() {
    this._busy = "kpi";
    const msg = { type: "sextant/kpi", hours: this._kpiHours };
    if (this._baseline) msg.baseline = this._baseline;
    const r = await callWS(this, this.hass, msg);
    this._busy = null;
    if (r) this._kpi = r;
  }

  async _loadBaselines() {
    const r = await this.hass.callWS({ type: "sextant/kpi/baselines" }).catch(() => null);
    if (r) {
      this._baselines = r.baselines || [];
      if (this._baseline && !this._baselines.some((b) => b.name === this._baseline)) this._baseline = "";
    }
  }

  async _saveBaseline() {
    const name = (this._baselineName || "").trim();
    if (!name) return;
    this._busy = "baseline";
    const r = await callWS(this, this.hass, { type: "sextant/kpi/baseline/save", name, hours: this._kpiHours });
    this._busy = null;
    if (r) {
      toast(this, `Saved baseline "${r.name}" (${r.things} things, ${this._kpiHours} h)`);
      this._baselineName = "";
      await this._loadBaselines();
      this._baseline = r.name;
    }
  }

  async _deleteBaseline(name) {
    if (!name || !confirmDialog(`Delete the KPI baseline "${name}"?`)) return;
    const r = await callWS(this, this.hass, { type: "sextant/kpi/baseline/delete", name });
    if (r) { toast(this, `Deleted baseline "${name}"`); this._baseline = ""; if (this._kpi) this._kpi = { ...this._kpi, baseline: undefined, deltas: undefined }; await this._loadBaselines(); }
  }

  async _loadLinking() {
    const r = await callWS(this, this.hass, { type: "sextant/scanner_linking" });
    if (r) this._linking = r;
  }

  async _saveTuning() {
    const spec = this.data?.tuning_spec || {};
    const settings = {};
    for (const [k, v] of Object.entries(this._tuning)) {
      if (v === "" || v == null) continue;
      const s = spec[k];
      settings[k] = s?.type === "bool" ? !!v : s?.type === "str" ? String(v) : Number(v);
    }
    const r = await callWS(this, this.hass, { type: "sextant/tuning/set", settings, reset: true });
    if (r) { toast(this, "Tuning applied live"); this._tuning = { ...r.tuning }; this.dispatchEvent(new CustomEvent("layout-changed")); }
  }

  async _resetTuning() {
    if (!confirmDialog("Restore every tuning value to its default?")) return;
    const r = await callWS(this, this.hass, { type: "sextant/tuning/set", reset: true });
    if (r) { toast(this, "Defaults restored"); this._tuning = {}; this.dispatchEvent(new CustomEvent("layout-changed")); }
  }

  render() {
    switch (this.section) {
      case "calibration":
        return html`<div class="page"><div class="cols">${this._renderCalibration()}</div></div>`;
      case "tuning":
        return html`<div class="page"><div class="cols">${this._renderKpi()}${this._renderAccuracy()}${this._renderTuning()}${this._renderHistory()}</div></div>`;
      case "advice":
        return html`<div class="page"><div class="cols">${this._renderAdvice()}</div></div>`;
      default:
        return html`<div class="page"><div class="cols">${this._renderReceivers()}${this._renderSelftest()}</div></div>`;
    }
  }

  // --- Proxies -------------------------------------------------------------------

  /** A proxy's status: "offline", "unmatched", "quiet" or "ok". */
  _status(r) {
    if (!r.matched) return "unmatched";
    if (!r.online) return "offline";
    const age = r.last_seen_age ?? r.age;
    if (age != null && age > QUIET_SECS) return "quiet";
    return "ok";
  }

  /** The room (non-no-go zone) a proxy's placement falls in, from the layout. */
  _roomOf(r) {
    const floor = (this.data?.layout?.floor || []).find((f) => f.name === r.floor);
    const placed = (floor?.receivers || []).find((x) => x.entity_id === r.slug || (r.address && x.address === r.address));
    if (!floor || !placed?.cords) return null;
    const hit = (floor.zones || []).find((z) => !z.no_go && (z.cords || []).length >= 3 && pointInPolygon({ x: placed.cords.x, y: placed.cords.y }, z.cords));
    return hit?.entity_id || null;
  }

  _toggle(key) {
    const s = new Set(this._open);
    s.has(key) ? s.delete(key) : s.add(key);
    this._open = s;
  }

  _counts(rows) {
    const c = { ok: 0, quiet: 0, offline: 0, unmatched: 0 };
    for (const r of rows) c[this._status(r)]++;
    return c;
  }

  _countPills(c) {
    return html`<span class="counts">
      ${c.ok ? html`<span class="pill ok" title="online and heard recently">${c.ok}</span>` : nothing}
      ${c.quiet ? html`<span class="pill quiet" title="online but nothing heard for ${QUIET_SECS} s">${c.quiet}</span>` : nothing}
      ${c.offline ? html`<span class="pill bad" title="offline">${c.offline}</span>` : nothing}
      ${c.unmatched ? html`<span class="pill warn" title="placed but Bermuda has no scanner by that name">${c.unmatched}</span>` : nothing}
    </span>`;
  }

  _renderReceivers() {
    const rx = this._receivers;
    const placed = rx?.placed || [];
    const diag = this.data?.scanner_diagnostics || {};
    const floors = sortFloors(this.data?.layout?.floor || []).map((f) => f.name);
    for (const name of new Set(placed.map((r) => r.floor).filter(Boolean))) if (!floors.includes(name)) floors.push(name);
    const total = this._counts(placed);
    const attention = placed.filter((r) => this._status(r) !== "ok");
    const showCorr = placed.some((r) => r.correction != null);  // nothing applied yet: no empty column
    return html`<section class="card receivers">
      <h3>Proxies <span class="muted">${placed.length} placed</span></h3>
      <div class="row">
        ${this._countPills(total)}
        <span class="muted small">green online · yellow quiet (${fmtAge(QUIET_SECS)} without a reading) · red offline · orange unmatched</span>
        <span class="pill">${(rx?.unplaced || []).length} heard but unplaced</span>
        <span class="grow"></span>
        ${uiButton({ label: "Refresh", kind: "text", icon: "mdi:refresh", onClick: () => this._refresh() })}
      </div>
      ${attention.length ? html`<div class="attention">
        <b>Needs a look:</b> ${attention.map((r) => html`<span class="pill ${this._status(r) === "offline" ? "bad" : this._status(r) === "quiet" ? "quiet" : "warn"}" title=${this._status(r)}>${proxyName(this.data, r.address || r.slug)} · ${r.floor || "?"}</span>`)}
      </div>` : html`<div class="muted small">Every placed proxy is online and reporting.</div>`}
      ${floors.map((floor) => {
        const rows = placed.filter((r) => r.floor === floor);
        if (!rows.length) return nothing;
        const key = `floor:${floor}`;
        const rooms = new Map();
        for (const r of rows) { const room = this._roomOf(r) || "outside any room"; if (!rooms.has(room)) rooms.set(room, []); rooms.get(room).push(r); }
        const roomNames = [...rooms.keys()].sort((a, b) => (a === "outside any room") - (b === "outside any room") || a.localeCompare(b));
        return html`<div class="group">
          <button class="grouphead" @click=${() => this._toggle(key)}>
            <ha-icon icon=${this._open.has(key) ? "mdi:chevron-down" : "mdi:chevron-right"}></ha-icon>
            <b>${floor}</b> <span class="muted small">${rows.length} proxies · ${rooms.size} rooms</span>
            <span class="grow"></span>${this._countPills(this._counts(rows))}
          </button>
          ${this._open.has(key) ? roomNames.map((room) => {
            const rr = rooms.get(room), rkey = `room:${floor}:${room}`;
            return html`<div class="room">
              <button class="grouphead sub" @click=${() => this._toggle(rkey)}>
                <ha-icon icon=${this._open.has(rkey) ? "mdi:chevron-down" : "mdi:chevron-right"}></ha-icon>
                ${room} <span class="muted small">${rr.length}</span>
                <span class="grow"></span>${this._countPills(this._counts(rr))}
              </button>
              ${this._open.has(rkey) ? html`<div class="wrap"><table>
                <tr><th>Proxy</th><th>Status</th><th class="num">Last heard</th>${showCorr ? html`<th class="num" title="calibration correction factor">Corr.</th>` : nothing}<th class="num">Height</th></tr>
                ${rr.slice().sort((a, b) => (this._status(a) === "ok") - (this._status(b) === "ok") || a.slug.localeCompare(b.slug)).map((r) => {
                  const st = this._status(r);
                  return html`<tr>
                    <td><b>${proxyName(this.data, r.address || r.slug)}</b><br><span class="muted small">${r.slug}${r.address ? ` · ${r.address}` : ""}</span></td>
                    <td><span class="pill ${st === "ok" ? "ok" : st === "quiet" ? "quiet" : st === "offline" ? "bad" : "warn"}">${st}</span></td>
                    <td class="num">${r.last_seen_age != null ? fmtAge(r.last_seen_age) : r.age != null ? fmtAge(r.age) : "—"}</td>
                    ${showCorr ? html`<td class="num">${r.correction != null ? fmtNum(r.correction, 3) : "—"}</td>` : nothing}
                    <td class="num">${r.height != null ? fmtLen(r.height, this.hass) : "—"}</td>
                  </tr>`;
                })}
              </table></div>` : nothing}
            </div>`;
          }) : nothing}
        </div>`;
      })}
      ${(rx?.unplaced || []).length ? html`<details><summary>Heard but not placed (${rx.unplaced.length})</summary>
        <ul class="plain">${rx.unplaced.map((u) => html`<li><b>${u.name || u.slug}</b> <span class="muted small">${u.address}${u.area ? ` · ${u.area}` : ""} · ${fmtAge(u.last_seen_age)} ago</span></li>`)}</ul></details>` : nothing}
      ${(diag.unmatched_receivers || []).length ? html`<details open><summary>Naming mismatches (${diag.unmatched_receivers.length})</summary>
        <ul class="plain">${diag.unmatched_receivers.map((u) => html`<li><b>${u.entity_id}</b> on ${u.floor}${u.suggested ? html` → suggested <code>${u.suggested}</code>` : nothing}</li>`)}</ul></details>` : nothing}
      <details @toggle=${(e) => { if (e.target.open && !this._linking) this._loadLinking(); }}><summary>What each proxy hears right now</summary>
        <p class="small muted">One row per placed proxy: which things it currently reports a distance for, and how far. <b>live</b> = it heard a tracked device in the last minute; <b>silent</b> = it is fine but nothing tracked is in its range right now (normal for a proxy in an empty room; worth a look only if it stays silent while people walk past it); <b>unmatched</b> = no Bermuda proxy has that name, so fix the placement on the Edit page. Unplaced proxies are not shown because their readings are never used.</p>
        ${this._linking ? html`<div class="wrap"><table>
          <tr><th>Proxy</th><th>Status</th><th class="num">Reporting</th><th>Things heard</th></tr>
          ${(this._linking.placed || []).map((p) => {
            const rows = (p.sensors || p.readings || []).filter((d) => d.state != null && d.state !== "unknown" && d.state !== "unavailable");
            return html`<tr>
              <td>${proxyName(this.data, p.address || p.entity_id || p.receiver || p.slug)}<br><span class="muted small">${p.floor || ""}</span></td>
              <td><span class="pill ${p.status === "live" ? "ok" : "warn"}">${p.status}</span></td>
              <td class="num">${p.reporting_count ?? rows.length} / ${p.sensor_count ?? (p.sensors || []).length}</td>
              <td class="small">${rows.length ? rows.map((d) => `${thingName(this.data, d.device || d.entity)} ${fmtLen(Number(d.state ?? d.distance), this.hass)}`).join(" · ") : html`<span class="muted">nothing tracked in range</span>`}</td>
            </tr>`;
          })}
        </table></div>` : html`<div class="muted small">Loading…</div>`}
      </details>
    </section>`;
  }

  _accPill(m) {
    return html`<span class="pill ${m < 2 ? "ok" : m < 4 ? "warn" : "bad"}">${fmtLen(m, this.hass, 2)}</span>`;
  }

  async _runAdvice() {
    this._busy = "advice";
    const r = await callWS(this, this.hass, { type: "sextant/advice" });
    this._busy = null;
    if (r) { this._advice = { ...r, at: Date.now() }; remember("advice", r); }
  }

  async _ignoreScanner(address, ignored) {
    const r = await callWS(this, this.hass, { type: "sextant/scanner/ignore", address, ignored });
    if (!r || !this._advice) return;
    // Keep the cached report in step without re-running the analysis.
    const a = this._advice;
    const moving = ignored ? (a.unplaced || []).find((u) => u.address === address) : (a.ignored || []).find((u) => u.address === address);
    const unplaced = ignored ? (a.unplaced || []).filter((u) => u.address !== address) : [...(a.unplaced || []), ...(moving ? [moving] : [])];
    this._advice = { ...a, unplaced, ignored: r.ignored };
    const { at, ...report } = this._advice;
    remember("advice", report);
  }

  // The Edit page, where the spots are pinned, is an administrator's; for
  // anyone else the button landed on Live with nothing to show.
  _isAdmin() { return this.hass?.user?.is_admin !== false; }

  _showSpots(floor, spots) {
    // sextant-edit labels each ring "add a proxy here · <room>" itself, so
    // several rooms' spots read fine together on one floor's plan.
    this.dispatchEvent(new CustomEvent("show-spots", { detail: { floor, spots: spots.map((s) => ({ floor, room: s.room, x: s.x, y: s.y })) }, bubbles: true, composed: true }));
  }

  /** advice rows grouped into one change plan per floor, worst floor first
   * (a floor's rank is its single worst room; ties broken by how many
   * proxies it needs). Each floor keeps the existing worst-first order
   * among its own rooms. */
  _adviceByFloor(rooms) {
    const rank = { "no proxy": 0, "weak proxy": 1, "one-sided": 2, "coverage": 2, "noisy": 3, "ok": 4 };
    const byFloor = new Map();
    for (const r of rooms || []) {
      if (!byFloor.has(r.floor)) byFloor.set(r.floor, []);
      byFloor.get(r.floor).push(r);
    }
    return [...byFloor.entries()]
      .map(([floor, rows]) => ({
        floor, rows,
        worst: Math.min(...rows.map((r) => rank[r.issue] ?? 9)),
        toAdd: rows.reduce((n, r) => n + (r.add || 0), 0),
        flagged: rows.filter((r) => r.issue !== "ok").length,
      }))
      .sort((a, b) => a.worst - b.worst || b.toAdd - a.toAdd || a.floor.localeCompare(b.floor));
  }

  _renderAdvice() {
    const a = this._advice;
    const issuePill = (issue) => html`<span class="pill ${issue === "ok" ? "ok" : issue === "noisy" || issue === "coverage" || issue === "one-sided" ? "warn" : "bad"}">${issue}</span>`;
    const floors = a ? this._adviceByFloor(a.rooms) : [];
    return html`<section class="card wide">
      <h3>Where a proxy would help</h3>
      <p class="small muted">Every room is judged two ways: how far its proxies land from where they are placed in the self-test, and whether any point in the room has three proxies near enough and around it. Grouped into one change plan per floor, worst floor first; a suggested spot is on a wall, where an outlet or a switch is. <b>Show on plan</b> pins one room's spots on the Edit page, <b>Show all on this floor</b> pins every spot on the floor at once.</p>
      <div class="row">${uiButton({ label: this._busy === "advice" ? "Analysing…" : a ? "Refresh" : "Analyse the house", kind: stale(a) || !a ? "primary" : "outline", disabled: this._busy === "advice", onClick: () => this._runAdvice() })}
        ${stale(a) ? html`<span class="pill warn" title="Auto calibration has sampled a lot since; refresh for a current picture">over an hour old</span>` : nothing}
        ${a ? html`<span class="muted small">${a.at ? `analysed ${fmtAge((Date.now() - a.at) / 1000)} ago · ` : ""}${a.summary.rooms} rooms · ${a.summary.to_add ? `${a.summary.to_add} proxies to add` : "nothing to add"}${Object.entries(a.summary.issues || {}).filter(([k]) => k !== "ok").map(([k, n]) => ` · ${n} ${k}`).join("")}</span>` : nothing}</div>
      ${a?.unplaced?.length ? html`<div class="row"><b>Heard but not placed:</b>
        ${a.unplaced.map((u) => html`<span class="chips">${u.name}${u.suggest ? html` <span class="muted small">→ ${u.suggest.room} (${u.suggest.floor})</span> ${this._isAdmin() ? uiButton({ label: "Show on plan", kind: "text", onClick: () => this._showSpots(u.suggest.floor, [{ room: u.suggest.room, x: u.suggest.x, y: u.suggest.y }]) }) : nothing}` : nothing} ${uiButton({ label: "Ignore", kind: "text", title: "Leave this scanner out of the unplaced lists (a kiosk, a test board, an outdoor proxy)", onClick: () => this._ignoreScanner(u.address, true) })}</span>`)}</div>` : nothing}
      ${a?.ignored?.length ? html`<div class="row muted small">Ignored: ${a.ignored.map((u) => html`<span class="chips">${u.name} ${uiButton({ label: "Un-ignore", kind: "text", onClick: () => this._ignoreScanner(u.address, false) })}</span>`)}</div>` : nothing}
      ${a ? floors.map(({ floor, rows, toAdd, flagged }) => {
        const allSpots = rows.flatMap((r) => (r.spots || []).map((s) => ({ ...s, room: r.room })));
        return html`<div class="floorplan">
          <h4>${floor} <span class="muted small">${flagged ? `${flagged} room${flagged === 1 ? "" : "s"} flagged` : "every room fine"}${toAdd ? ` · ${toAdd} to add` : ""}</span>
            ${allSpots.length && this._isAdmin() ? uiButton({ label: `Show all ${allSpots.length} on this floor`, kind: "outline", onClick: () => this._showSpots(floor, allSpots) }) : nothing}
          </h4>
          <div class="wrap"><table>
            <tr><th>Room</th><th>Issue</th><th class="num">Proxies</th><th class="num">Median</th><th class="num">Add</th><th>What to do</th><th></th></tr>
            ${rows.map((r) => html`<tr>
              <td>${r.room}</td><td>${issuePill(r.issue)}</td>
              <td class="num">${r.proxies}${r.solved < r.proxies ? html` <span class="muted small">(${r.solved} solved)</span>` : nothing}</td>
              <td class="num">${r.median_m != null ? fmtLen(r.median_m, this.hass, 2) : "—"}</td>
              <td class="num">${r.add || ""}</td>
              <td class="small">${r.note ? r.note.replace(/[a-z0-9_]+_(rrn00|s2224|eth|shelly)[a-z0-9_]*/g, (m) => proxyName(this.data, m)) : html`<span class="muted">fine</span>`}</td>
              <td>${r.spots?.length && this._isAdmin() ? uiButton({ label: "Show on plan", kind: "text", onClick: () => this._showSpots(r.floor, r.spots.map((s) => ({ ...s, room: r.room }))) }) : nothing}</td>
            </tr>`)}
          </table></div>
        </div>`;
      }) : html`<p class="muted small">Runs the self-test (a few seconds) and reads the plan.</p>`}
    </section>`;
  }

  _renderSelftest() {
    const st = this._selftest;
    const bd = st?.breakdown;
    const solved = st?.result?.receivers || [];
    const unsolved = st?.result?.unsolved || [];
    const cell = (m) => (m != null ? fmtLen(m, this.hass, 2) : "—");
    // A floor whose every proxy is unsolved with nobody hearing it has no
    // calibration samples at all (the window holds the last manual run's
    // floor unless Auto calibration is on), which is a different message
    // from "the geometry did not converge".
    const noSamples = new Set((bd?.floors || []).filter((f) => !f.solved && f.unsolved
      && unsolved.filter((u) => u.floor === f.floor).every((u) => !u.heard_by)).map((f) => f.floor));
    const count = (r) => (r.solved || r.unsolved
      ? html`${r.solved}${r.unsolved ? html` <span class="muted small">+${r.unsolved} unsolved</span>` : nothing}`
      : html`<span class="muted small">no proxy</span>`);
    return html`<section class="card">
      <h3>Proxy self-test</h3>
      <p class="small muted">Leave-one-out: each proxy is located from the others' ranges to it and compared with where it is placed. Read it by room: a whole-house figure hides which rooms the proxies place well and which they do not. It works from the calibration sample window, so a floor only has figures once it has been sampled: turn on <b>Auto calibration</b> below to keep every floor sampled.</p>
      <div class="row">${uiButton({ label: this._busy === "selftest" ? "Running…" : st ? "Refresh" : "Run self-test", kind: stale(st) || !st ? "primary" : "outline", disabled: this._busy === "selftest", onClick: () => this._runSelftest() })}
        ${stale(st) ? html`<span class="pill warn">over an hour old</span>` : nothing}
        ${st && st.state != null ? html`<span class="muted small">${st.at ? `run ${fmtAge((Date.now() - st.at) / 1000)} ago · ` : ""}whole house</span> ${this._accPill(Number(st.state))}` : nothing}</div>
      ${bd ? html`<div class="wrap"><table>
        <tr><th>Floor / room</th><th class="num">Proxies</th><th class="num">Median</th><th class="num">CEP95</th><th>Worst proxy</th></tr>
        ${bd.floors.map((f) => html`
          <tr class="grouphead"><td><b>${f.floor}</b></td><td class="num">${count(f)}</td><td class="num">${cell(f.cep50_m)}</td><td class="num">${f.cep95_m != null ? this._accPill(f.cep95_m) : "—"}</td><td>${noSamples.has(f.floor) ? html`<span class="muted small">no calibration samples for this floor yet</span>` : f.worst ? proxyName(this.data, f.worst) : ""}</td></tr>
          ${bd.rooms.filter((r) => r.floor === f.floor).map((r) => html`<tr><td style="padding-left: 22px">${r.room ?? html`<span class="muted">outside any room</span>`}</td><td class="num">${count(r)}</td><td class="num">${cell(r.cep50_m)}</td><td class="num">${r.cep95_m != null ? this._accPill(r.cep95_m) : "—"}</td><td>${r.worst ? proxyName(this.data, r.worst) : ""}</td></tr>`)}`)}
      </table></div>
      <details><summary>Every proxy (${solved.length} solved${unsolved.length ? `, ${unsolved.length} unsolved` : ""})</summary>
        <div class="wrap"><table><tr><th>Proxy</th><th>Room</th><th class="num">Error</th><th class="num">Heard by</th></tr>
          ${[...solved].sort((a, b) => (b.error_m ?? -1) - (a.error_m ?? -1)).map((v) => html`<tr><td>${proxyName(this.data, v.entity)}</td><td class="muted">${v.floor}${v.room ? ` · ${v.room}` : ""}</td><td class="num">${fmtLen(v.error_m, this.hass, 2)}</td><td class="num">${v.heard_by}</td></tr>`)}
          ${unsolved.map((v) => html`<tr><td>${proxyName(this.data, v.entity)}</td><td class="muted">${v.floor}${v.room ? ` · ${v.room}` : ""}</td><td class="num"><span class="muted">unsolved${v.reason ? ` (${v.reason})` : ""}</span></td><td class="num">${v.heard_by}</td></tr>`)}
        </table></div>
      </details>` : nothing}
    </section>`;
  }

  // --- Calibration ---------------------------------------------------------------

  _renderCalibration() {
    const cal = this._cal;
    const results = cal?.results || {};
    const sampling = cal?.state === "sampling";
    const floor = this._calFloor;
    const cur = results[floor];
    // "Worse" = the self-test says this solve would not place the floor's
    // proxies better than what is in place (or than nothing); without a
    // verdict, the fit's own error factor decides.
    const judged = (r) => (r?.selftest ? r.selftest.new_m > Math.min(r.selftest.none_m, r.selftest.current_m ?? Infinity) : !!r && r.error_factor_after > r.error_factor_before);
    const worse = judged(cur);
    const verdict = (r) => (r?.selftest ? html` <span class="muted small">self-test median: none ${fmtLen(r.selftest.none_m, this.hass, 2)}${r.selftest.current_m != null ? ` · in place ${fmtLen(r.selftest.current_m, this.hass, 2)}` : ""} · this solve ${fmtLen(r.selftest.new_m, this.hass, 2)}</span>` : nothing);
    const decision = (name) => { const d = cal?.auto_decisions?.[name]; return d ? html` <span class="pill ${d.action === "apply" ? "ok" : d.action === "revert" ? "warn" : ""}" title=${d.reason}>auto: ${d.action === "apply" ? "applied" : d.action === "revert" ? "removed its corrections" : "held back"}</span>` : nothing; };
    return html`<section class="card wide">
      <h3>Proxy calibration <span class="muted small">${floor ? `for ${floor}, picked in the header` : ""}</span></h3>
      <p class="small muted">Every proxy hears every other proxy's beacon at a known distance; a run collects those readings and solves one range correction per proxy. Apply only when the error factor after is lower than before, otherwise the corrections are absorbing placement error, not radio bias.</p>
      ${cal ? html`
        <div class="row">
          <span class="pill ${sampling ? "warn" : cal.mode === "auto" ? "ok" : ""}">${cal.mode === "auto" ? `auto${cal.started_at ? ` · sampling for ${fmtAge(Date.now() / 1000 - cal.started_at)}` : ""}${cal.started_at && !cal.last_solved_at && cal.first_solve_after ? ` · first solve in ${fmtAge(Math.max(0, cal.started_at + cal.first_solve_after - Date.now() / 1000))}` : ""}` : cal.state}${sampling && cal.seconds_left != null ? ` · ${fmtAge(cal.seconds_left)} left` : ""}</span>
          <span class="muted small">${Object.keys(cal.pair_counts || {}).length} pairs sampled · ${cal.receiver_count} proxies${cal.last_solved_at ? ` · solved ${fmtAge(Date.now() / 1000 - cal.last_solved_at)} ago` : ""}</span>
          ${cal.error ? html`<span class="pill bad">${cal.error}</span>` : nothing}
        </div>
        <div class="row">
          ${uiField({ label: "Duration (s)", type: "number", min: 60, max: 3600, step: 30, value: this._calDuration, onChange: (v) => { this._calDuration = Number(v); }, style: "width: 130px" })}
          ${uiButton({ label: "Start run", kind: "primary", disabled: sampling || !!this._busy, onClick: () => this._calAction("start", { floor, duration: this._calDuration }) })}
          ${uiButton({ label: "Cancel", kind: "text", disabled: !!this._busy, onClick: () => this._calAction("cancel") })}
          <span class="chips">${uiSwitch({ label: "Auto calibration", checked: cal.mode === "auto", onChange: (v) => this._calAction("auto", { enabled: v }) })}</span>
        </div>
        <div class="row">
          ${uiButton({ label: "Solve now", disabled: !!this._busy, onClick: () => this._calAction("solve", { floor }) })}
          ${uiButton({ label: worse ? "Apply anyway" : "Apply corrections", kind: worse ? "danger" : "outline", icon: worse ? "mdi:alert" : undefined, disabled: !!this._busy || !cur,
            title: worse ? `This solve made ${floor} worse (×${fmtNum(cur.error_factor_before, 2)} → ×${fmtNum(cur.error_factor_after, 2)}); the corrections are absorbing placement error` : cur ? `Store the ${floor} factors with the layout` : "Solve a floor first",
            onClick: () => { if (!worse || confirmDialog(`This solve made ${floor} worse: error ×${fmtNum(cur.error_factor_before, 2)} → ×${fmtNum(cur.error_factor_after, 2)}. Corrections that make the fit worse are absorbing placement error, not radio bias. Apply anyway?`)) this._calAction("apply", { floor }); } })}
          ${uiButton({ label: "Reset", kind: "danger", disabled: !!this._busy, onClick: () => confirmDialog(`Reset corrections on ${floor}?`) && this._calAction("reset", { floor }) })}
        </div>
        ${Object.entries(results).map(([name, r]) => html`<details ?open=${name === floor}>
          <summary>${name}: ${r.pairs_used} pairs, error ×${fmtNum(r.error_factor_before, 2)} → ×${fmtNum(r.error_factor_after, 2)}${judged(r) ? html` <span class="pill warn">would not help: do not apply</span>` : nothing}${r.low_confidence?.length ? html` <span class="pill warn">${r.low_confidence.length} low confidence</span>` : nothing}${decision(name)}${verdict(r)}</summary>
          <div class="wrap"><table><tr><th>Proxy</th><th class="num">Factor</th><th class="num">≈ dB</th></tr>
            ${Object.entries(r.receivers || {}).sort((a, b) => Math.abs(b[1] - 1) - Math.abs(a[1] - 1)).map(([slug, f]) => html`<tr><td>${proxyName(this.data, slug)}${(r.low_confidence || []).includes(slug) ? html` <span class="pill warn">low</span>` : nothing}</td><td class="num">${fmtNum(f, 3)}</td><td class="num">${fmtNum(r.rx_bias_db_equident?.[slug], 1)}</td></tr>`)}
          </table></div>
          ${(r.missing_no_data || []).length ? html`<p class="small muted">No samples: ${r.missing_no_data.join(", ")}</p>` : nothing}
          ${(r.missing_unmatched || []).length ? html`<p class="small muted">Unmatched: ${r.missing_unmatched.join(", ")}</p>` : nothing}
        </details>`)}
      ` : html`<div class="muted">Loading…</div>`}
    </section>`;
  }

  // --- Tuning --------------------------------------------------------------------

  _renderKpi() {
    const k = this._kpi;
    const s = k?.summary || {};
    const d = k?.deltas;
    const ents = Object.entries(k?.entities || {}).filter(([e]) => e.endsWith("_sextant_room")).sort((a, b) => (b[1].changes_per_hour ?? 0) - (a[1].changes_per_hour ?? 0));
    // Fewer changes / flips is better (green); a longer dwell is better.
    const lessIsBetter = (v, digits = 1, scale = 1) => v == null ? "—" : html`<span class=${v < 0 ? "good" : v > 0 ? "bad" : ""}>${v > 0 ? "+" : ""}${fmtNum(v * scale, digits)}</span>`;
    const moreIsBetter = (v) => v == null ? "—" : html`<span class=${v > 0 ? "good" : v < 0 ? "bad" : ""}>${v > 0 ? "+" : "−"}${fmtAge(Math.abs(v))}</span>`;
    const sz = d?.summary?.sextant_room;
    const baselineOptions = [{ value: "", label: "no baseline" }, ...this._baselines.map((b) => ({ value: b.name, label: `${b.name} · ${b.hours} h · ${(b.saved_at || "").slice(0, 10)}` }))];
    const name = (e) => thingName(this.data, e.replace(/^sensor\./, "").replace(/_sextant_room$/, ""));
    return html`<section class="card wide">
      <h3>Stability</h3>
      <div class="row">
        ${uiSelect({ label: "Window", value: this._kpiHours, options: [1, 3, 6, 12, 24, 48].map((h) => ({ value: h, label: `${h} h` })), onChange: (v) => { this._kpiHours = Number(v); }, style: "min-width: 110px" })}
        ${uiSelect({ label: "Compare with", value: this._baseline, options: baselineOptions, onChange: (v) => { this._baseline = v; }, style: "min-width: 240px" })}
        ${uiButton({ label: this._busy === "kpi" ? "Computing…" : "Compute", kind: "primary", disabled: this._busy === "kpi", onClick: () => this._runKpi() })}
        ${s.sextant_room ? html`<span class="pill">${s.sextant_room.changes_per_thing_hour} room changes / thing-h</span>
          <span class="pill">flip ratio ${s.sextant_room.flip_ratio}</span><span class="pill">median dwell ${fmtAge(s.sextant_room.median_of_median_dwell_s)}</span>
          ${s.sextant_room.far_moves_per_thing_day != null ? html`<span class="pill" title=${FAR_MOVES_HELP}>${fmtNum(s.sextant_room.far_moves_per_thing_day, 1)} far moves / thing-day</span>` : nothing}` : nothing}
        ${sz ? html`<span class="pill" title="this window minus the baseline">vs ${k.baseline.name}: ${lessIsBetter(sz.changes_per_thing_hour, 2)} chg/thing-h · ${lessIsBetter(sz.flip_ratio, 0, 100)} flip pts · ${moreIsBetter(sz.median_of_median_dwell_s)} dwell${sz.far_moves_per_thing_day != null ? html` · ${lessIsBetter(sz.far_moves_per_thing_day, 1)} far/day` : nothing}</span>` : nothing}
      </div>
      <div class="row">
        ${uiField({ label: "Save this window as a baseline", value: this._baselineName, placeholder: "e.g. fused 2026-09-17", onChange: (v) => { this._baselineName = v; }, style: "width: 260px" })}
        ${uiButton({ label: this._busy === "baseline" ? "Saving…" : "Save baseline", disabled: this._busy === "baseline" || !(this._baselineName || "").trim(), onClick: () => this._saveBaseline(), title: "Computes the selected window now and keeps it for later comparison" })}
        ${this._baseline ? uiButton({ label: "Delete baseline", kind: "danger", onClick: () => this._deleteBaseline(this._baseline) }) : nothing}
      </div>
      ${ents.length ? html`<div class="wrap"><table>
        <tr><th>Thing</th><th class="num">chg/h</th><th class="num">flip %</th><th class="num">dwell</th><th class="num">&lt;60 s %</th><th class="num" title=${FAR_MOVES_HELP}>far/day</th><th class="num">dead</th>${d ? html`<th class="num">Δ chg/h</th><th class="num">Δ flip pts</th><th class="num">Δ dwell</th><th class="num" title=${FAR_MOVES_HELP}>Δ far/day</th>` : nothing}</tr>
        ${ents.map(([e, m]) => html`<tr><td>${name(e)}</td><td class="num">${fmtNum(m.changes_per_hour, 1)}</td><td class="num">${m.flip_ratio != null ? fmtNum(m.flip_ratio * 100, 0) : "—"}</td><td class="num">${fmtAge(m.median_dwell_s)}</td><td class="num">${m.short_dwell_ratio != null ? fmtNum(m.short_dwell_ratio * 100, 0) : "—"}</td><td class="num">${m.far_moves_per_day != null ? fmtNum(m.far_moves_per_day, 1) : "—"}</td><td class="num">${m.dead}</td>${d ? html`<td class="num">${lessIsBetter(d.entities?.[e]?.changes_per_hour, 1)}</td><td class="num">${lessIsBetter(d.entities?.[e]?.flip_ratio, 0, 100)}</td><td class="num">${moreIsBetter(d.entities?.[e]?.median_dwell_s)}</td><td class="num">${lessIsBetter(d.entities?.[e]?.far_moves_per_day, 1)}</td>` : nothing}</tr>`)}
      </table></div>` : k ? html`<div class="muted small">No room sensors in the recorder window.</div>` : nothing}
    </section>`;
  }

  async _runAccuracy() {
    this._busy = "accuracy";
    const r = await callWS(this, this.hass, { type: "sextant/truth/evaluate" });
    this._busy = null;
    if (r) this._accuracy = r;
  }

  _renderAccuracy() {
    const a = this._accuracy;
    const rows = Object.entries(a?.things || {}).sort((x, y) => y[1].mean_m - x[1].mean_m);
    return html`<section class="card wide">
      <h3>Accuracy <span class="muted small">against your location pins</span></h3>
      <p class="small muted">Every pin (Live page, "It's actually here…") re-solved under the settings in force now: how far each thing lands from where you said it was, and how often it gets the room right.</p>
      <div class="row">${uiButton({ label: this._busy === "accuracy" ? "Evaluating…" : "Evaluate pins", kind: "primary", disabled: this._busy === "accuracy", onClick: () => this._runAccuracy() })}
        ${a ? html`<span class="pill">${(a.marks || []).length} pin${(a.marks || []).length === 1 ? "" : "s"}</span>` : nothing}</div>
      ${rows.length ? html`<div class="wrap"><table>
        <tr><th>Thing</th><th class="num">Pins</th><th class="num">Mean error</th><th class="num">Right room</th></tr>
        ${rows.map(([e, m]) => html`<tr><td>${thingName(this.data, e)}</td><td class="num">${m.marks}</td><td class="num">${fmtLen(m.mean_m, this.hass)}</td><td class="num">${Math.round(m.room_ok * 100)}%</td></tr>`)}
      </table></div>` : a ? html`<div class="muted small">No pins yet.</div>` : nothing}
    </section>`;
  }

  _renderTuning() {
    const spec = this.data?.tuning_spec || {};
    const groups = [
      ["Estimator", ["position_estimator", "fingerprint_weight", "fingerprint_floor_weight", "fingerprint_k", "fingerprint_missing_m", "fingerprint_ref_gain", "fingerprint_auto_gain", "fingerprint_marks", "fingerprint_marks_scope", "distance_estimator", "median_window_secs", "median_min_samples"]],
      ["Solver", ["solver_max_receivers", "solver_max_range", "solver_near_always"]],
      ["Rooms", ["zone_hysteresis", "zone_prob_smoothing", "zone_switch_margin", "zone_switch_secs", "stationary_speed", "stationary_secs", "zone_unlock_margin", "zone_unlock_secs", "zone_lock_warmup_secs"]],
      ["Spots", ["subzone_switch_secs", "subzone_enter_prob", "subzone_unlock_margin", "subzone_lock_release_m", "spot_proxy_near_m", "spot_proxy_far_m", "spot_proxy_ratio"]],
      ["Near-field anchor", ["anchor_max_m", "anchor_ratio", "anchor_secs", "anchor_release_m"]],
      ["Floors", ["floor_switch_secs", "floor_switch_margin", "floor_tenure_bonus", "floor_tenure_full_secs", "floor_proximity_weight", "floor_proximity_blend", "floor_proximity_k"]],
      ["People", ["gps_stale_secs"]],
      ["Robots and mmWave", ["robot_poll_secs", "mmwave_fusion", "mmwave_auto_pins", "mmwave_pair_m"]],
      ["Calibration", ["calibration_target", "correction_close_fade", "correction_fade_near_m", "correction_fade_far_m"]],
      ["History and display", ["history_hours", "history_admin_only", "stale_after_secs", "away_after_secs", "restore_state_secs", "election_log_hours"]],
    ];
    const known = new Set(groups.flatMap((g) => g[1]));
    const rest = Object.keys(spec).filter((k) => !known.has(k));
    if (rest.length) groups.push(["Other", rest]);
    const field = (key) => {
      const s = spec[key];
      if (!s) return nothing;
      const v = this._tuning[key];
      const set = (value) => { this._tuning = { ...this._tuning, [key]: value }; };
      const [label, help] = TUNING_LABELS[key] || [key, ""];
      let control;
      if (s.type === "bool") control = html`<span class="chips">${uiSwitch({ label, checked: v == null ? !!s.default : !!v, onChange: set })}</span>`;
      else if (s.type === "str") control = uiSelect({ label, value: v ?? s.default, options: s.choices.map((c) => ({ value: c, label: c })), onChange: set, style: "min-width: 220px" });
      else control = uiField({ label, type: "number", step: s.type === "int" ? 1 : "any", min: s.min, max: s.max, placeholder: String(s.default), value: v == null ? "" : v, onChange: (val) => set(val === "" ? null : Number(val)), style: "width: 220px" });
      return html`<div class="tfield" title=${help ? `${help} (default ${s.default})` : `default ${s.default}`}>${control}<code class="hint">${key}</code></div>`;
    };
    return html`<section class="card wide">
      <h3>Tuning <span class="muted small">applies live, no restart · distances here are metres, the solver's own unit</span></h3>
      ${groups.map(([name, keys]) => html`<h4>${name}</h4><div class="row">${keys.map(field)}</div>`)}
      <div class="row">${uiButton({ label: "Apply", kind: "primary", onClick: () => this._saveTuning() })}${uiButton({ label: "Restore defaults", kind: "text", onClick: () => this._resetTuning() })}</div>
    </section>`;
  }

  async _clearHistory(entity) {
    if (!confirmDialog(entity ? `Forget the recorded positions of ${thingName(this.data, entity)}?` : "Forget every thing's recorded positions?")) return;
    const r = await callWS(this, this.hass, { type: "sextant/history/clear", ...(entity ? { entity } : {}) });
    if (r) toast(this, `History cleared (${r.removed} file${r.removed === 1 ? "" : "s"} rewritten)`);
  }

  _renderHistory() {
    const ents = this.data?.entities || [];
    return html`<section class="card">
      <h3>Position history</h3>
      <p class="small muted">The scrubber on the Live page replays these; this is only where they can be forgotten.</p>
      <div class="row">
        ${uiSelect({ label: "Thing", value: this._histEnt || "", options: [{ value: "", label: "every thing" }, ...ents.map((e) => ({ value: e, label: thingName(this.data, e) }))], onChange: (v) => { this._histEnt = v; }, style: "min-width: 220px" })}
        ${uiButton({ label: "Clear history", kind: "danger", onClick: () => this._clearHistory(this._histEnt || null) })}
      </div>
    </section>`;
  }

  static styles = [sharedStyles, widgetStyles, css`
    .good { color: var(--success-color, #2e7d32); font-weight: 600; }
    .bad { color: var(--error-color, #c62828); font-weight: 600; }
    :host { display: block; overflow: auto; }
    .cols { grid-template-columns: repeat(auto-fit, minmax(460px, 1fr)); }
    .card.wide { grid-column: 1 / -1; }
    section.receivers { grid-column: 1 / -1; }
    .attention { margin: 8px 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    .group { border-top: 1px solid var(--divider-color); margin-top: 6px; }
    .grouphead { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; background: transparent; border: 0; padding: 8px 4px; font: inherit; color: inherit; cursor: pointer; border-radius: 6px; }
    .grouphead:hover { background: var(--secondary-background-color); }
    .grouphead.sub { padding-left: 28px; font-size: 13px; }
    .grouphead ha-icon { --mdc-icon-size: 20px; color: var(--secondary-text-color); }
    .room .wrap { padding-left: 28px; margin-bottom: 8px; }
    .counts { display: inline-flex; gap: 4px; }
    ul.plain { list-style: none; padding: 0; margin: 6px 0; }
    ul.plain li { padding: 3px 0; }
    details { margin-top: 8px; }
    summary { cursor: pointer; }
    h4 { margin-top: 12px; }
    /* One change plan per floor on the Advice page: a heading with the
       floor's own tally and a button that stages every one of its
       suggested spots on the map together, then that floor's own table. */
    .floorplan { border-top: 1px solid var(--divider-color); margin-top: 10px; padding-top: 8px; }
    .floorplan h4 { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .tfield { display: inline-flex; flex-direction: column; gap: 2px; }
    .tfield .hint { font-size: 11px; color: var(--secondary-text-color); padding-left: 2px; }
    @media (max-width: 720px) { .cols { grid-template-columns: 1fr; } .tfield, .tfield > * { width: 100%; } }
  `];
}

if (!customElements.get("sextant-health")) customElements.define("sextant-health", SextantHealth);
