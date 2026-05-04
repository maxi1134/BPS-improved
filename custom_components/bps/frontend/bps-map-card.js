/**
 * Lovelace card: BPS floor map with one or more trackers.
 *
 * Resource (Settings -> Dashboards -> ... -> Resources):
 *   URL: /bps/bps-map-card.js
 *   Type: JavaScript module
 *
 * YAML example:
 *   type: custom:bps-map-card
 *   floor: Livingroom
 *   entities:
 *     - sensor.phone_alice
 *     - sensor.phone_bob
 *   show_labels: false
 *   scale_labels: 100
 *   scale_icon: 100
 *   zone_label: false
 *   poll_interval: 3
 *   map_file: livingroom.jpg
 */
class BpsMapCard extends HTMLElement {
  constructor() {
    super();
    this._config = null;
    this._hass = null;
    this._shadow = this.attachShadow({ mode: "open" });
    this._wrap = document.createElement("div");
    this._wrap.className = "wrap";
    this._canvas = document.createElement("canvas");
    this._status = document.createElement("div");
    this._status.className = "status";
    this._wrap.appendChild(this._canvas);
    this._wrap.appendChild(this._status);
    this._shadow.appendChild(this._wrap);

    const style = document.createElement("style");
    style.textContent = `
      .wrap { position: relative; width: 100%; }
      canvas {
        display: block;
        width: 100%;
        max-width: 100%;
        height: auto;
        background: #f4f4f4;
      }
      .status { font: 12px/1.4 sans-serif; color: var(--secondary-text-color, #888); margin-top: 6px; }
    `;
    this._shadow.appendChild(style);

    this._baseImage = null;
    this._mapUrlLoaded = "";
    this._imgNaturalW = 0;
    this._imgNaturalH = 0;
    this._positions = new Map();
    this._entityByTrackerKey = new Map();
    this._trackerIcons = {};
    this._iconCache = new Map();

    this._pollTimer = null;

    this._bootstrapPromise = null;
    this._runGeneration = 0;
    this._coordinateWidth = 2000; // Must match sidebar panel coordinate space.
    this._lastZoneLabelSignature = "";
    this._lastFloorPresenceSignature = "";
  }

  setConfig(config) {
    if (!config || !config.floor) {
      throw new Error("BPS card: set floor (must match floor name in BPS coordinates).");
    }
    if (!config.entities || !Array.isArray(config.entities) || config.entities.length === 0) {
      throw new Error("BPS card: set at least one entity (for example sensor.phone).");
    }
    this._config = {
      floor: config.floor,
      entities: config.entities,
      show_labels: Boolean(config.show_labels),
      scale_labels: BpsMapCard.normalizePercent(config.scale_labels),
      scale_icon: BpsMapCard.normalizePercent(config.scale_icon),
      zone_label: Boolean(config.zone_label),
      poll_interval: Number(config.poll_interval) > 0 ? Number(config.poll_interval) : 3,
      image: config.image || "",
      map_file: config.map_file || "",
    };
    this._entityByTrackerKey = new Map(
      this._config.entities.map((eid) => [this._trackerKeyFromEntity(eid), eid]),
    );
    this._runGeneration += 1;
    const gen = this._runGeneration;
    this._stopPolling();
    this._bootstrapPromise = null;
    this._positions.clear();
    this._lastZoneLabelSignature = "";
    this._lastFloorPresenceSignature = "";
    this._baseImage = null;
    this._mapUrlLoaded = "";
    if (this._hass) {
      this._bootstrapPromise = this._bootstrap(gen);
    }
    this._setStatus("Configuration loaded.");
  }

  getCardSize() {
    return 1;
  }

  static getConfigElement() {
    return document.createElement("bps-map-card-editor");
  }

  static getStubConfig() {
    return {
      floor: "MyFloor",
      entities: ["sensor.phone_alice"],
      show_labels: false,
      scale_labels: 100,
      scale_icon: 100,
      zone_label: false,
      poll_interval: 3,
    };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config || !hass) return;

    if (!this._bootstrapPromise) {
      this._runGeneration += 1;
      const gen = this._runGeneration;
      this._bootstrapPromise = this._config ? this._bootstrap(gen) : null;
    }

    if (!this._bootstrapPromise) {
      return;
    }
    const genAtSchedule = this._runGeneration;
    this._bootstrapPromise.then(() => {
      if (genAtSchedule !== this._runGeneration) {
        return;
      }
      this._stopPolling();
      this._startPolling();
    });

    if (
      this._config.zone_label &&
      this._config.show_labels &&
      this._baseImage &&
      this._positions.size > 0
    ) {
      const sig = this._zoneLabelSignature();
      if (sig !== this._lastZoneLabelSignature) {
        this._lastZoneLabelSignature = sig;
        this._redraw();
      }
    }

    if (this._baseImage && this._config?.entities?.length) {
      const fs = this._floorPresenceSignature();
      if (fs !== this._lastFloorPresenceSignature) {
        this._lastFloorPresenceSignature = fs;
        this._prunePositionsByFloor();
        this._redraw();
        this._updateFloorStatus();
      }
    }
  }

  disconnectedCallback() {
    this._stopPolling();
  }

  _setStatus(text) {
    this._status.textContent = text;
  }

  _friendlyLabel(trackerKey) {
    const ent = this._entityByTrackerKey.get(trackerKey);
    if (!ent || !this._hass?.states?.[ent]) return trackerKey;
    return this._hass.states[ent].attributes?.friendly_name || ent;
  }

  static normalizePercent(value) {
    if (value == null || value === "") return 100;
    if (typeof value === "number" && Number.isFinite(value) && value > 0) {
      return value;
    }
    const s = String(value).trim();
    const m = s.match(/^(\d+(?:\.\d+)?)\s*%?\s*$/);
    if (m) {
      const n = Number(m[1]);
      return Number.isFinite(n) && n > 0 ? n : 100;
    }
    return 100;
  }

  _zoneLabelSignature() {
    if (!this._hass?.states || !this._config?.entities) return "";
    return this._config.entities
      .map((eid) => {
        const k = this._trackerKeyFromEntity(eid);
        if (!this._entityOnThisFloor(k)) return "";
        return this._hass.states[`sensor.${k}_bps_zone`]?.state ?? "";
      })
      .join("|");
  }

  _floorPresenceSignature() {
    if (!this._hass?.states || !this._config?.entities) return "";
    return this._config.entities
      .map((eid) => {
        const k = this._trackerKeyFromEntity(eid);
        return this._hass.states[`sensor.${k}_bps_floor`]?.state ?? "";
      })
      .join("|");
  }

  _entityOnThisFloor(trackerKey) {
    const target = this._normalize(this._config.floor);
    const st = this._hass?.states?.[`sensor.${trackerKey}_bps_floor`]?.state;
    if (st == null || st === "unknown" || st === "unavailable") {
      return false;
    }
    return this._normalize(st) === target;
  }

  _prunePositionsByFloor() {
    for (const key of [...this._positions.keys()]) {
      if (!this._entityOnThisFloor(key)) {
        this._positions.delete(key);
      }
    }
  }

  _updateFloorStatus() {
    const tot = this._config.entities.length;
    const n = this._config.entities.filter((eid) =>
      this._entityOnThisFloor(this._trackerKeyFromEntity(eid)),
    ).length;
    this._setStatus(`Floor: ${this._config.floor} · ${n}/${tot} tracker(s) on this floor.`);
  }

  _markerLabelText(trackerKey, pos) {
    if (this._config.zone_label) {
      const zoneEnt = `sensor.${trackerKey}_bps_zone`;
      const st = this._hass?.states?.[zoneEnt]?.state;
      if (st && st !== "unknown" && st !== "unavailable") {
        return st;
      }
      if (pos && pos.zone != null && String(pos.zone).trim() !== "") {
        return String(pos.zone);
      }
      return "unknown";
    }
    if (pos && pos.label) return pos.label;
    return this._friendlyLabel(trackerKey);
  }

  _trackerIconUrl(trackerKey) {
    const DEFAULT_TRACKER_ICON = "/bps/person.svg";
    const storedIcon = this._trackerIcons?.[trackerKey];
    if (storedIcon === "person.svg") return "/bps/person.svg";
    if (storedIcon === "beacon.svg") return "/bps/beacon.svg";
    return storedIcon || DEFAULT_TRACKER_ICON;
  }

  _getIconImage(url) {
    if (!url) return null;
    const cached = this._iconCache.get(url);
    if (cached) return cached;
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => this._redraw();
    img.src = url;
    this._iconCache.set(url, img);
    return img;
  }

  async _bootstrap(expectedGen) {
    try {
      await this._loadFloorResources(expectedGen);
      if (expectedGen !== this._runGeneration) {
        return;
      }
      this._redraw();
      this._updateFloorStatus();
    } catch (e) {
      console.error(e);
      this._setStatus(e.message || String(e));
    }
  }

  _trackerKeyFromEntity(entityId) {
    if (!entityId || typeof entityId !== "string") return "";
    return entityId.replace(/^sensor\./, "");
  }

  _normalize(value) {
    return String(value || "").trim().toLowerCase();
  }

  async _loadFloorResources(expectedGen) {
    const res = await fetch("/api/bps/read_text");
    if (!res.ok) throw new Error(`Could not read BPS data (${res.status})`);
    if (expectedGen !== this._runGeneration) {
      return;
    }
    const data = await res.json();
    if (expectedGen !== this._runGeneration) {
      return;
    }
    const coords = JSON.parse(data.coordinates);
    this._trackerIcons = coords.tracker_icons && typeof coords.tracker_icons === "object"
      ? coords.tracker_icons
      : {};
    const requestedFloor = this._normalize(this._config.floor);
    const floor = coords.floor.find((f) => this._normalize(f.name) === requestedFloor);
    if (!floor) {
      throw new Error(`No floor found with the name "${this._config.floor}".`);
    }
    if (expectedGen !== this._runGeneration) {
      return;
    }
    const mapUrl = await this._resolveMapUrl(floor.name);
    if (mapUrl !== this._mapUrlLoaded || !this._baseImage) {
      await this._loadFloorImage(mapUrl, expectedGen);
      if (expectedGen !== this._runGeneration) {
        return;
      }
      this._mapUrlLoaded = mapUrl;
    }
  }

  async _resolveMapUrl(resolvedFloorName) {
    if (this._config.image) return this._config.image;
    const explicitMap = String(this._config.map_file || "").trim();
    if (explicitMap) {
      return `/local/bps_maps/${explicitMap}`;
    }

    const floorName = String(resolvedFloorName || this._config.floor || "").trim();
    const mapsRes = await fetch("/api/bps/maps");
    if (!mapsRes.ok) {
      throw new Error(
        `Could not list map files (${mapsRes.status}). Set map_file explicitly or check /api/bps/maps.`,
      );
    }
    const maps = await mapsRes.json();
    if (!Array.isArray(maps) || maps.length === 0) {
      throw new Error("No map files found in /local/bps_maps.");
    }

    const normalizedFloor = this._normalize(floorName);
    const exactName = maps.find((m) => this._normalize(m) === normalizedFloor);
    if (exactName) {
      return `/local/bps_maps/${exactName}`;
    }

    const byBaseName = maps.find((m) => {
      const base = String(m).replace(/\.[^/.]+$/, "");
      return this._normalize(base) === normalizedFloor;
    });
    if (byBaseName) {
      return `/local/bps_maps/${byBaseName}`;
    }

    throw new Error(
      `No map image matched floor "${floorName}". Add map_file, image, or rename a file in /local/bps_maps.`,
    );
  }

  _loadFloorImage(url, expectedGen) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => {
        if (expectedGen !== this._runGeneration) {
          resolve();
          return;
        }
        this._baseImage = img;
        this._imgNaturalW = img.naturalWidth;
        this._imgNaturalH = img.naturalHeight;
        resolve();
      };
      img.onerror = () => reject(new Error(`Could not load floor image: ${url}`));
      img.src = url;
    });
  }

  _resizeCanvasToImage() {
    if (!this._baseImage) return;
    const ratio = this._baseImage.naturalHeight / this._baseImage.naturalWidth;
    const w = this._coordinateWidth;
    const h = Math.round(w * ratio);
    if (this._canvas.width !== w || this._canvas.height !== h) {
      this._canvas.width = w;
      this._canvas.height = h;
    }
    this._imgNaturalW = w;
    this._imgNaturalH = h;
    this._canvas.style.aspectRatio = `${w} / ${h}`;
  }

  _drawBase() {
    if (!this._baseImage) return;
    this._resizeCanvasToImage();
    const ctx = this._canvas.getContext("2d");
    ctx.clearRect(0, 0, this._canvas.width, this._canvas.height);
    // Keep the exact same coordinate space as the sidebar panel (normalized 2000px width).
    ctx.drawImage(this._baseImage, 0, 0, this._canvas.width, this._canvas.height);
  }

  _drawMarkers() {
    if (!this._imgNaturalW) return;
    const ctx = this._canvas.getContext("2d");
    const minSide = Math.min(this._canvas.width, this._canvas.height);
    const baseIconSize = Math.max(12, minSide * 0.04);
    const iconSize = baseIconSize * (this._config.scale_icon / 100);
    ctx.save();
    for (const [trackerKey, pos] of this._positions) {
      if (pos == null || pos.x == null || pos.y == null) continue;
      if (!this._entityOnThisFloor(trackerKey)) continue;
      const iconUrl = this._trackerIconUrl(trackerKey);
      const iconImg = this._getIconImage(iconUrl);
      if (iconImg && iconImg.complete && iconImg.naturalWidth > 0) {
        ctx.drawImage(iconImg, pos.x - iconSize / 2, pos.y - iconSize / 2, iconSize, iconSize);
      } else {
        ctx.beginPath();
        ctx.fillStyle = "rgba(33, 150, 243, 0.85)";
        ctx.strokeStyle = "#0d47a1";
        ctx.lineWidth = 2;
        ctx.arc(pos.x, pos.y, iconSize / 2, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }
      if (this._config.show_labels) {
        const text = this._markerLabelText(trackerKey, pos);
        if (text) {
          const scale = this._config.scale_labels / 100;
          const fontPx = Math.max(11, iconSize * 0.35) * scale;
          ctx.font = `bold ${fontPx}px sans-serif`;
          ctx.fillStyle = "#111";
          ctx.fillText(text, pos.x + iconSize / 2 + 4, pos.y + iconSize * 0.12);
        }
      }
    }
    ctx.restore();
  }

  _redraw() {
    this._drawBase();
    this._drawMarkers();
  }

  _startPolling() {
    this._stopPolling();
    const ms = this._config.poll_interval * 1000;
    this._pollTimer = window.setInterval(() => this._pollOnce(), ms);
    this._pollOnce();
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  async _pollOnce() {
    try {
      const res = await fetch("/api/bps/cords");
      if (!res.ok) return;
      const list = await res.json();
      if (!Array.isArray(list)) return;
      for (const ent of this._config.entities) {
        const key = this._trackerKeyFromEntity(ent);
        if (!this._entityOnThisFloor(key)) {
          this._positions.delete(key);
          continue;
        }
        const row = list.find((item) => item.ent === key);
        if (row && Array.isArray(row.cords) && row.cords.length >= 2) {
          this._positions.set(key, {
            x: row.cords[0],
            y: row.cords[1],
            label: this._friendlyLabel(key),
            zone: row.zone != null ? row.zone : "",
          });
        }
      }
      this._redraw();
      this._updateFloorStatus();
    } catch (e) {
      console.warn("BPS poll:", e);
    }
  }

}

customElements.define("bps-map-card", BpsMapCard);

class BpsMapCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
  }

  setConfig(config) {
    this._config = { ...config };
    this._config.scale_labels = BpsMapCard.normalizePercent(this._config.scale_labels);
    this._config.scale_icon = BpsMapCard.normalizePercent(this._config.scale_icon);
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    this.innerHTML = "";
    const root = document.createElement("div");
    root.style.padding = "8px";

    const mk = (label, key, type = "text", placeholder = "") => {
      const row = document.createElement("div");
      row.style.marginBottom = "8px";
      const l = document.createElement("label");
      l.textContent = label;
      l.style.display = "block";
      l.style.fontSize = "12px";
      const inp = document.createElement("input");
      inp.type = type;
      inp.placeholder = placeholder;
      inp.style.width = "100%";
      inp.value = this._config[key] != null ? this._config[key] : "";
      inp.addEventListener("change", () => {
        if (type === "number") this._config[key] = Number(inp.value);
        else if (type === "checkbox") this._config[key] = inp.checked;
        else this._config[key] = inp.value;
        this._fire();
      });
      row.appendChild(l);
      row.appendChild(inp);
      root.appendChild(row);
    };

    mk("Floor (floor name)", "floor", "text", "MyFloor");

    const entRow = document.createElement("div");
    entRow.style.marginBottom = "8px";
    const entLabel = document.createElement("label");
    entLabel.textContent = "Entities (comma separated sensor.*)";
    entLabel.style.display = "block";
    entLabel.style.fontSize = "12px";
    const entInp = document.createElement("input");
    entInp.type = "text";
    entInp.style.width = "100%";
    entInp.placeholder = "sensor.phone_alice, sensor.phone_bob";
    entInp.value = Array.isArray(this._config.entities) ? this._config.entities.join(", ") : "";
    entInp.addEventListener("change", () => {
      this._config.entities = entInp.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      this._fire();
    });
    entRow.appendChild(entLabel);
    entRow.appendChild(entInp);
    root.appendChild(entRow);

    mk("Map file (optional, filename in www/bps_maps)", "map_file", "text", "floor.png");
    mk("Image URL (optional, overrides map file)", "image", "text", "https://...");
    mk("Poll interval (seconds)", "poll_interval", "number", "3");
    mk("Label scale (percent, e.g. 100 or 200)", "scale_labels", "number", "100");
    mk("Icon scale (percent, e.g. 100 or 200)", "scale_icon", "number", "100");

    const labelsRow = document.createElement("div");
    labelsRow.style.marginBottom = "8px";
    const labelsLabel = document.createElement("label");
    labelsLabel.textContent = "Show labels";
    labelsLabel.style.display = "block";
    labelsLabel.style.fontSize = "12px";
    const labels = document.createElement("input");
    labels.type = "checkbox";
    labels.checked = Boolean(this._config.show_labels);
    labels.addEventListener("change", () => {
      this._config.show_labels = labels.checked;
      this._fire();
    });
    labelsRow.appendChild(labelsLabel);
    labelsRow.appendChild(labels);
    root.appendChild(labelsRow);

    const zoneRow = document.createElement("div");
    zoneRow.style.marginBottom = "8px";
    const zoneLabel = document.createElement("label");
    zoneLabel.textContent = "Show zone instead of device name";
    zoneLabel.style.display = "block";
    zoneLabel.style.fontSize = "12px";
    const zoneCb = document.createElement("input");
    zoneCb.type = "checkbox";
    zoneCb.checked = Boolean(this._config.zone_label);
    zoneCb.addEventListener("change", () => {
      this._config.zone_label = zoneCb.checked;
      this._fire();
    });
    zoneRow.appendChild(zoneLabel);
    zoneRow.appendChild(zoneCb);
    root.appendChild(zoneRow);

    this.appendChild(root);
  }

  _fire() {
    this._config.scale_labels = BpsMapCard.normalizePercent(this._config.scale_labels);
    this._config.scale_icon = BpsMapCard.normalizePercent(this._config.scale_icon);
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: this._config },
        bubbles: true,
        composed: true,
      }),
    );
  }
}

customElements.define("bps-map-card-editor", BpsMapCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "bps-map-card",
  name: "BPS Map",
  description: "Show one or more BPS trackers on a floor plan.",
  preview: true,
  documentationURL: "https://github.com/Hogster/BPS",
});
