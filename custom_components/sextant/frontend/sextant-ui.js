/**
 * Shared styles and helpers for the Sextant panel's modes.
 *
 * Plain HTML controls styled with Home Assistant's theme variables rather
 * than HA's own web components: the panel then renders identically across
 * frontend versions and the elements it needs are always defined.
 */
import { css } from "./lit.js";

export const sharedStyles = css`
  * { box-sizing: border-box; }
  h3 { margin: 0 0 8px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--secondary-text-color); }
  h4 { margin: 0 0 6px; font-size: 15px; }
  .muted { color: var(--secondary-text-color); }
  .small { font-size: 12px; }
  .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow, 0 1px 4px rgba(0,0,0,0.15)); padding: 12px 14px; margin: 0 0 12px; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .grow { flex: 1; min-width: 0; }
  button, .btn { font: inherit; padding: 6px 12px; border-radius: 8px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); cursor: pointer; }
  button:hover { border-color: var(--primary-color); }
  button:disabled { opacity: 0.5; cursor: default; }
  button.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  button.danger { border-color: var(--error-color, #b00020); color: var(--error-color, #b00020); }
  button.ghost { background: transparent; }
  button.icon { padding: 4px 6px; line-height: 0; }
  input[type="text"], input[type="number"], input[type="search"], select, textarea { font: inherit; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); min-width: 0; }
  input[type="number"] { width: 90px; }
  textarea { width: 100%; min-height: 80px; font-family: ui-monospace, monospace; font-size: 12px; }
  label.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--secondary-text-color); }
  label.field > * { color: var(--primary-text-color); font-size: 14px; }
  label.inline { display: inline-flex; align-items: center; gap: 6px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--divider-color); vertical-align: middle; }
  th { font-weight: 600; color: var(--secondary-text-color); font-size: 12px; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .wrap { overflow-x: auto; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; background: var(--secondary-background-color); }
  .pill.quiet { background: rgba(224,165,74,0.22); color: var(--warning-color, #9a5b00); }
  /* A switch and its label as one bordered chip, so it is obvious which word each switch belongs to. */
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chips > ha-formfield, .chips > label.inline { border: 1px solid var(--divider-color); border-radius: 999px; padding: 0 12px 0 2px; }
  .chips > label.inline { padding: 4px 12px 4px 8px; }
  /* ⋮ overflow menu (uiMenu): the rare actions live here instead of each
     taking a button of its own down the side of a card. */
  details.menu { position: relative; }
  details.menu > summary { list-style: none; display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; border: 0; border-radius: 8px; background: transparent; color: var(--secondary-text-color); cursor: pointer; }
  details.menu > summary::-webkit-details-marker, details.menu > summary::marker { display: none; content: ""; }
  details.menu > summary:hover { background: var(--secondary-background-color, rgba(0,0,0,0.06)); color: var(--primary-text-color); }
  details.menu .menu-items { position: absolute; right: 0; top: 36px; z-index: 5; min-width: 210px; padding: 4px; border-radius: 10px; background: var(--card-background-color); box-shadow: var(--ha-card-box-shadow, 0 4px 14px rgba(0,0,0,0.3)); display: flex; flex-direction: column; }
  details.menu .menu-item { display: flex; align-items: center; gap: 10px; width: 100%; padding: 8px 10px; border: 0; border-radius: 6px; background: transparent; text-align: left; font: inherit; color: var(--primary-text-color); cursor: pointer; }
  details.menu .menu-item:hover:not([disabled]) { background: var(--secondary-background-color, rgba(0,0,0,0.06)); }
  details.menu .menu-item[disabled] { opacity: 0.45; cursor: default; }
  details.menu .menu-item.danger { color: var(--error-color, #b00020); }
  details.menu .menu-item ha-icon { --mdc-icon-size: 20px; }
  details.menu hr { width: 100%; border: 0; border-top: 1px solid var(--divider-color); margin: 4px 0; }
  button.iconbtn { padding: 6px; line-height: 0; border-radius: 50%; border: 0; background: transparent; color: var(--secondary-text-color); cursor: pointer; }
  button.iconbtn:hover:not([disabled]) { background: var(--secondary-background-color, rgba(0,0,0,0.06)); color: var(--primary-text-color); }
  button.iconbtn.on { background: var(--primary-text-color); color: var(--card-background-color); }
  button.iconbtn[disabled] { opacity: 0.4; cursor: default; }
  button.iconbtn ha-icon { --mdc-icon-size: 22px; }
  /* The boxed kind, for a row of controls in a card (turn this way, nudge that
     way): the Edit toolbar's rounded squares, with an outline so they read as
     buttons away from the toolbar's own panel. */
  button.iconbtn.box { display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px; padding: 0; border-radius: 8px;
    border: 1px solid var(--divider-color, rgba(127,127,127,0.35)); color: var(--primary-text-color); }
  button.iconbtn.box ha-icon { --mdc-icon-size: 20px; }
  button.iconbtn.box.on { background: var(--primary-text-color); color: var(--card-background-color); border-color: var(--primary-text-color); }
  button.iconbtn:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 1px; }
  .iconrow { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin: 4px 0 8px; }
  .iconrow .gap { width: 10px; }
  .iconbar { display: flex; align-items: center; gap: 2px; }
  .segctl-wrap { display: inline-flex; align-items: center; gap: 8px; }
  .segctl { display: inline-flex; border: 1px solid var(--divider-color); border-radius: 999px; overflow: hidden; }
  .segctl button { border: 0; background: transparent; padding: 4px 11px; font: inherit; font-size: 12px; color: var(--secondary-text-color); cursor: pointer; }
  .segctl button + button { border-left: 1px solid var(--divider-color); }
  .segctl button:hover:not([disabled]) { color: var(--primary-text-color); }
  .segctl button.on { background: var(--primary-text-color); color: var(--card-background-color); font-weight: 600; }
  .segctl button[disabled] { opacity: 0.4; cursor: default; }
  .pill.ok { background: rgba(44,110,73,0.18); color: var(--success-color, #2c6e49); }
  .pill.warn { background: rgba(224,165,74,0.22); color: var(--warning-color, #9a5b00); }
  .pill.bad { background: rgba(217,83,79,0.18); color: var(--error-color, #b00020); }
  .empty { padding: 24px; color: var(--secondary-text-color); text-align: center; }
  .page { padding: 12px 16px; overflow: auto; }
  .cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 12px; align-items: start; }
  code { font-family: ui-monospace, monospace; font-size: 12px; }
  .toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); background: var(--primary-text-color); color: var(--primary-background-color); padding: 8px 14px; border-radius: 8px; font-size: 13px; z-index: 10; box-shadow: 0 2px 8px rgba(0,0,0,0.3); }
  /* A wide table on a phone: the leading column (almost always the row's
     identity - a proxy, a thing, a device) stays put while the rest
     scrolls under it, and a fading edge says there is more to see. Pure
     CSS, no markup changes, so every .wrap table in the panel gets this. */
  @media (max-width: 720px) {
    .wrap { position: relative; background: linear-gradient(to right, var(--card-background-color) 30%, rgba(0,0,0,0)), linear-gradient(to right, rgba(0,0,0,0), var(--card-background-color) 70%) 100% 0; background-repeat: no-repeat; background-size: 20px 100%, 20px 100%; background-attachment: local, scroll; }
    .wrap table { border-collapse: separate; border-spacing: 0; }
    .wrap th:first-child, .wrap td:first-child { position: sticky; left: 0; background: var(--card-background-color); box-shadow: 2px 0 4px -2px rgba(0,0,0,0.2); }
  }
`;

export function fmtAge(seconds) {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

export function fmtNum(value, digits = 2) {
  if (value == null || !isFinite(value)) return "—";
  return Number(value).toFixed(digits);
}

/** A short message at the bottom of the host element's shadow root. */
export function toast(host, message, ms = 3500) {
  const root = host.renderRoot || host.shadowRoot || host;
  let el = root.querySelector(".toast");
  if (!el) { el = document.createElement("div"); el.className = "toast"; root.appendChild(el); }
  el.textContent = message;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.remove(), ms);
}

/** hass.callWS with the error surfaced as a toast; resolves to null on failure. */
export async function callWS(host, hass, message) {
  try {
    return await hass.callWS(message);
  } catch (e) {
    toast(host, e?.message || String(e), 6000);
    return null;
  }
}

export function confirmDialog(text) {
  return window.confirm(text);
}

export function slugLabel(slug) {
  return String(slug || "").replace(/^private_ble_device_/, "").replace(/^private_ble_/, "").replace(/_/g, " ");
}

// --- Home Assistant's own widgets --------------------------------------------
//
// The panel renders with HA's form elements (ha-textfield, ha-select,
// ha-switch, ha-button) so it looks like Settings. They are part of HA's
// frontend, not ours; ensureHaComponents() pulls the editor bundle that
// defines them, and each helper falls back to a plain element when one is
// missing, so the panel never renders an inert unknown tag.
import { html, nothing } from "./lit.js";

let _haReady = null;
export function ensureHaComponents() {
  if (_haReady) return _haReady;
  _haReady = (async () => {
    try {
      if (!(customElements.get("ha-input") || customElements.get("ha-textfield")) || !customElements.get("ha-select") || !customElements.get("ha-switch")) {
        const helpers = await window.loadCardHelpers?.();
        // Creating an entities-card editor loads the shared form elements.
        const card = helpers?.createCardElement?.({ type: "entities", entities: [] });
        await card?.constructor?.getConfigElement?.();
      }
      await Promise.race([
        Promise.all(["ha-select", "ha-switch", "ha-formfield", "ha-button"].map((t) => customElements.whenDefined(t))),
        new Promise((r) => setTimeout(r, 2500)),
      ]);
    } catch { /* fall back to plain elements */ }
    return { textfield: !!(customElements.get("ha-input") || customElements.get("ha-textfield")), select: !!customElements.get("ha-select"),
             switch: !!customElements.get("ha-switch") && !!customElements.get("ha-formfield"), button: !!customElements.get("ha-button") || !!customElements.get("mwc-button") };
  })();
  return _haReady;
}

const has = (tag) => !!customElements.get(tag);

/** Text or number field. HA 2026.3+ ships ha-input (a Web Awesome input);
 *  older frontends ship ha-textfield; both emit a composed change event. */
export function uiField({ label, value, type = "text", step, min, max, placeholder, onChange, disabled = false, style = "", suffix }) {
  const v = value == null ? "" : String(value);
  if (has("ha-input")) {
    return html`<ha-input .label=${label ?? ""} .value=${v} .type=${type} .step=${step ?? nothing} .min=${min ?? nothing} .max=${max ?? nothing}
        .placeholder=${placeholder ?? ""} ?disabled=${disabled} style=${style} withoutSpinButtons
        @change=${(e) => onChange?.(e.target.value)}></ha-input>`;
  }
  if (has("ha-textfield")) {
    return html`<ha-textfield .label=${label ?? ""} .value=${v} .type=${type} .step=${step ?? nothing} .min=${min ?? nothing} .max=${max ?? nothing}
        .placeholder=${placeholder ?? ""} .suffix=${suffix ?? nothing} ?disabled=${disabled} style=${style}
        @change=${(e) => onChange?.(e.target.value)}></ha-textfield>`;
  }
  return html`<label class="field" style=${style}>${label ?? ""}<input type=${type} step=${step ?? nothing} min=${min ?? nothing} max=${max ?? nothing}
        placeholder=${placeholder ?? ""} .value=${v} ?disabled=${disabled} @change=${(e) => onChange?.(e.target.value)}></label>`;
}

/** Dropdown. options: [{value, label, disabled?}] */
export function uiSelect({ label, value, options, onChange, disabled = false, style = "" }) {
  const v = value == null ? "" : String(value);
  const Sel = customElements.get("ha-select");
  const opts = options.map((o) => ({ value: String(o.value), label: o.label, disabled: !!o.disabled }));
  const fire = (e) => { const nv = e.detail?.value ?? e.target?.value; if (nv != null && String(nv) !== v) onChange?.(String(nv)); };
  if (Sel && Sel.elementProperties?.has?.("options")) {
    // HA 2026.3+: options are a property; a choice arrives as `selected` (detail.value) on 2026.9,
    // as value-changed on earlier builds. Listen for all of them; `fire` ignores a repeat of the current value.
    return html`<ha-select .label=${label ?? ""} .value=${v} .options=${opts} ?disabled=${disabled} style=${style}
        @selected=${fire} @value-changed=${fire} @change=${fire} @closed=${(e) => e.stopPropagation()}></ha-select>`;
  }
  if (Sel && has("mwc-list-item")) {
    return html`<ha-select .label=${label ?? ""} .value=${v} ?disabled=${disabled} style=${style} naturalMenuWidth fixedMenuPosition
        @selected=${(e) => { const nv = e.target.value; if (nv !== v) onChange?.(nv); }} @closed=${(e) => e.stopPropagation()}>
      ${options.map((o) => html`<mwc-list-item .value=${String(o.value)} ?disabled=${!!o.disabled}>${o.label}</mwc-list-item>`)}
    </ha-select>`;
  }
  return html`<label class="field" style=${style}>${label ?? ""}<select ?disabled=${disabled} @change=${(e) => onChange?.(e.target.value)}>
      ${options.map((o) => html`<option value=${String(o.value)} ?selected=${String(o.value) === v} ?disabled=${!!o.disabled}>${o.label}</option>`)}</select></label>`;
}

/** On/off toggle with a label. */
export function uiSwitch({ label, checked, onChange, disabled = false }) {
  if (has("ha-switch") && has("ha-formfield")) {
    return html`<ha-formfield .label=${label ?? ""}><ha-switch .checked=${!!checked} ?disabled=${disabled} @change=${(e) => onChange?.(e.target.checked)}></ha-switch></ha-formfield>`;
  }
  return html`<label class="inline"><input type="checkbox" .checked=${!!checked} ?disabled=${disabled} @change=${(e) => onChange?.(e.target.checked)}> ${label ?? ""}</label>`;
}

/** Button. kind: "primary" | "outline" | "text" | "danger" */
/** An overflow menu behind a ⋮ button: occasional actions that do not earn a
 * permanent button each. `items` are {label, onClick, icon?, title?, danger?,
 * disabled?} or {divider: true}. Built on <details> so it opens, closes and
 * takes focus without a click-outside listener. */
export function uiMenu({ items, label = "More actions", icon = "mdi:dots-vertical" }) {
  return html`<details class="menu">
    <summary title=${label} aria-label=${label} role="button"><ha-icon icon=${icon}></ha-icon></summary>
    <div class="menu-items" @click=${(e) => { const d = e.currentTarget.parentElement; if (d) d.open = false; }}>
      ${(items || []).map((it) => it.divider
        ? html`<hr>`
        : html`<button class=${it.danger ? "menu-item danger" : "menu-item"} ?disabled=${it.disabled}
            title=${it.title ?? nothing} @click=${it.onClick}>
            ${it.icon ? html`<ha-icon icon=${it.icon}></ha-icon>` : nothing}<span>${it.label}</span></button>`)}
    </div>
  </details>`;
}

/** An action as an icon with a tooltip: for a row of actions on a card where
 * three pill buttons took a third of a phone screen. `title` is both the
 * tooltip and the accessible name, so it must say what the action does.
 * `active` marks a mode that is switched on (a marking in progress). */
export function uiIconButton({ icon, title, onClick, disabled = false, active = false, box = false }) {
  return html`<button class="iconbtn ${box ? "box" : ""} ${active ? "on" : ""}" ?disabled=${disabled} title=${title} aria-label=${title} aria-pressed=${active ? "true" : nothing} @click=${onClick}><ha-icon icon=${icon}></ha-icon></button>`;
}

/** A row of short choices, one of them on - Off · 6h · 24h · 7d - in the
 * space a dropdown's closed state takes, with every choice visible. Colours
 * come from the text colour inverted, the same as the floor tabs, so the
 * picked one reads on any theme without being a blue pill. */
export function uiSegmented({ label, value, options, onChange, disabled = false }) {
  return html`<span class="segctl-wrap">${label ? html`<span class="muted small">${label}</span>` : nothing}<span class="segctl" role="radiogroup" aria-label=${label ?? nothing}>
    ${options.map((o) => html`<button role="radio" class=${String(o.value) === String(value) ? "on" : ""} aria-checked=${String(o.value) === String(value)} ?disabled=${disabled} title=${o.title ?? nothing} @click=${() => onChange(o.value)}>${o.label}</button>`)}
  </span></span>`;
}

/** Home Assistant's own button in the look `kind` asks for. Since 2025.8 its
 * ha-button is a different element: it takes `appearance` (accent = filled,
 * outlined, plain) and `variant` (danger), puts an icon in the "start" slot,
 * and ignores the old raised/outlined flags - so every Sextant button had
 * become a filled blue pill, a "Cancel" as loud as the "Save" beside it. */
const NEW_HA_BUTTON = () => "appearance" in (customElements.get("ha-button")?.prototype || {});
const APPEARANCE = { primary: "accent", outline: "outlined", text: "plain", danger: "outlined" };

export function uiButton({ label, onClick, kind = "outline", disabled = false, icon, title }) {
  if (has("ha-button") && NEW_HA_BUTTON()) {
    return html`<ha-button appearance=${APPEARANCE[kind] || "outlined"} variant=${kind === "danger" ? "danger" : "brand"} size="small"
                           ?disabled=${disabled} title=${title ?? nothing} @click=${onClick}>${icon ? html`<ha-icon slot="start" icon=${icon}></ha-icon>` : nothing}${label}</ha-button>`;
  }
  if (has("ha-button") || has("mwc-button")) {
    const tag = has("ha-button") ? "ha-button" : "mwc-button";
    const raised = kind === "primary", outlined = kind === "outline" || kind === "danger";
    const cls = kind === "danger" ? "danger" : "";
    return tag === "ha-button"
      ? html`<ha-button ?raised=${raised} ?outlined=${outlined} ?disabled=${disabled} class=${cls} title=${title ?? nothing} @click=${onClick}>${icon ? html`<ha-icon slot="icon" icon=${icon}></ha-icon>` : nothing}${label}</ha-button>`
      : html`<mwc-button ?raised=${raised} ?outlined=${outlined} ?disabled=${disabled} class=${cls} title=${title ?? nothing} @click=${onClick}>${icon ? html`<ha-icon slot="icon" icon=${icon}></ha-icon>` : nothing}${label}</mwc-button>`;
  }
  return html`<button class=${kind === "primary" ? "primary" : kind === "danger" ? "danger" : kind === "text" ? "ghost" : ""} ?disabled=${disabled} title=${title ?? nothing} @click=${onClick}>${label}</button>`;
}

export const widgetStyles = css`
  ha-textfield { --mdc-text-field-fill-color: var(--card-background-color); min-width: 120px; }
  ha-textfield.narrow { width: 110px; }
  ha-select { min-width: 160px; }
  ha-button.danger, mwc-button.danger { --mdc-theme-primary: var(--error-color, #b00020); }
  ha-formfield { --mdc-typography-body2-font-size: 13px; }
`;

/** Floors top-down by their storey `level` (1 = the floor above ground, 0 = ground, -1 = basement); ties keep file order. */
// Kept in its own module so it can be tested without a DOM - the same reason
// the pronoun helpers live in sextant-pronouns.js.
export { sortFloors } from "./sextant-floors.js";

// --- Units and names ------------------------------------------------------------

/** True when Home Assistant's unit system is imperial (miles). Distances are metres inside Sextant. */
export function isImperial(hass) { return (hass?.config?.unit_system?.length || "km") === "mi"; }
export function lenUnit(hass) { return isImperial(hass) ? "ft" : "m"; }
/** Metres as the user's unit: "3.2 m", "10.5 ft", or inches under a foot. */
export function fmtLen(metres, hass, digits = 1) {
  if (metres == null || !isFinite(metres)) return "—";
  if (!isImperial(hass)) return `${Number(metres).toFixed(digits)} m`;
  const ft = metres * 3.28084;
  if (ft < 2) return `${Math.round(ft * 12)} in`;      // under two feet reads better in inches
  if (ft >= 5) return `${Math.round(ft)} ft`;          // past five feet a decimal is noise
  return `${ft.toFixed(digits)} ft`;
}
/** Metres per second as the user's unit: "0.42 m/s", or mph when imperial.
 *  Miles per hour rather than feet per second: nobody has a feel for 4.6 ft/s,
 *  everybody has one for 3 mph. Two decimals under 1 mph, where a dog shifting
 *  on a landing and a person crossing a room are a tenth of a mile apart. */
export function fmtSpeed(mps, hass) {
  if (mps == null || !isFinite(mps)) return "—";
  if (!isImperial(hass)) return `${Number(mps).toFixed(2)} m/s`;
  const mph = mps * 2.236936;
  return `${mph.toFixed(mph < 1 ? 2 : 1)} mph`;
}
/** Metres -> the number shown in an input field (feet when imperial), and back. */
export function toDisplayLen(metres, hass) { return metres == null || metres === "" ? "" : isImperial(hass) ? Math.round(metres * 3.28084 * 100) / 100 : metres; }
export function fromDisplayLen(value, hass) { if (value === "" || value == null) return null; const n = Number(value); return isImperial(hass) ? Math.round((n / 3.28084) * 1000) / 1000 : n; }
/** px per metre shown as px per foot when imperial. */
export function fmtScale(pxPerM, hass) { if (!pxPerM) return "no scale"; return isImperial(hass) ? `${fmtNum(pxPerM * 0.3048, 1)} px/ft` : `${fmtNum(pxPerM, 1)} px/m`; }

/** A thing's display name: the device name Bermuda / Home Assistant knows, else the slug tidied up. */
export function thingName(data, ent) {
  const known = data?.names?.[ent];
  if (known) return known;
  return slugLabel(ent).replace(/\b\w/g, (c) => c.toUpperCase());
}
/** A proxy's display name from the scanner directory (by address or slug), else the slug. */
export function proxyName(data, slugOrAddress) {
  const scanners = data?.scanners || {};
  // Bermuda appends " (aa:bb:cc:dd:ee:ff)" when two devices share a name; the address is not part of the name.
  const tidy = (n) => String(n).replace(/\s*\([0-9a-f]{2}(:[0-9a-f]{2}){5}\)\s*$/i, "");
  if (scanners[slugOrAddress]?.name) return tidy(scanners[slugOrAddress].name);
  for (const info of Object.values(scanners)) if (info.slug === slugOrAddress && info.name) return tidy(info.name);
  return slugOrAddress;
}

/** Thing classes: what a thing is, drawn as that icon on the map. */
export const THING_CLASSES = [
  ["", "No class (initials)", null],
  ["person", "Person", "mdi:account"],
  ["man", "Man", "mdi:face-man"],
  ["woman", "Woman", "mdi:face-woman"],
  ["child", "Child", "mdi:human-child"],
  ["paw", "Pet", "mdi:paw"],
  ["dog", "Dog", "mdi:dog"],
  ["cat", "Cat", "mdi:cat"],
  ["phone", "Phone", "mdi:cellphone"],
  ["watch", "Watch", "mdi:watch"],
  ["headphones", "Headphones", "mdi:headphones"],   // AirPods, earbuds and their case
  ["tablet", "Tablet", "mdi:tablet"],
  ["laptop", "Laptop", "mdi:laptop"],
  ["keys", "Keys", "mdi:key-chain-variant"],
  ["wallet", "Wallet", "mdi:wallet"],
  ["bag", "Bag", "mdi:bag-personal"],
  ["backpack", "Backpack", "mdi:bag-personal-outline"],
  ["purse", "Purse", "mdi:purse"],
  ["luggage", "Luggage", "mdi:bag-suitcase"],
  ["tag", "Tag / Tile", "mdi:tag"],
  ["car", "Car", "mdi:car"],
  ["bike", "Bike", "mdi:bike"],
  ["robot", "Robot vacuum", "mdi:robot-vacuum"],
];
export function classIcon(cls) { return (THING_CLASSES.find(([k]) => k === cls) || [])[2] || null; }

export { PRONOUNS, pronounKey, pronounsFor } from "./sextant-pronouns.js";

/** Classes that stand for a family rather than one kind of thing: a spot that
 * takes a Person takes a man, a woman or a child too (see CLASS_FAMILIES in
 * __init__.py, which decides it). */
export const CLASS_FAMILIES = {
  person: ["man", "woman", "child"],
  paw: ["dog", "cat"],
  bag: ["backpack", "purse", "luggage"],
};
