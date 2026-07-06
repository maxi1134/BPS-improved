document.addEventListener('DOMContentLoaded', async () => {
    const canvas = document.getElementById('canvas');
    const ctx = canvas.getContext('2d');
    const upload = document.getElementById('upload');
    const mapSelector = document.getElementById('mapSelector');
    const entSelector = document.getElementById('entSelector');
    const trackerIconSelector = document.getElementById('trackerIconSelector');
    const trackerIconUpload = document.getElementById('trackerIconUpload');
    const uploadTrackerIconButton = document.getElementById('uploadTrackerIcon');
    const mapbuttondiv = document.getElementById('mapbuttondiv');
    const savebuttondiv = document.getElementById('savebuttondiv');
    const trackdiv = document.getElementById('trackdiv');
    const zonediv = document.getElementById('zonediv');
    const messdiv = document.getElementById('message');
    const saveButton = document.createElement('button');

    //Delete button
    const deleteButton = document.createElement('button');
    deleteButton.className = 'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&amp;_svg]:pointer-events-none [&amp;_svg]:size-4 [&amp;_svg]:shrink-0 text-primary-foreground hover:bg-primary/90 h-10 px-4 py-2';
    deleteButton.style = 'background-color: red';
    deleteButton.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-save w-4 h-4 mr-2" data-component-name="Save"><path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"></path><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"></path><path d="M7 3v4a1 1 0 0 0 1 1h7"></path></svg>
            Delete Floor
        `;

    const mapname = document.getElementById('mapname');
    const starttrackbtn = document.getElementById('starttrack');
    const stoptrackbtn = document.getElementById('stoptrack');
    const drawAreaButton = document.createElement('button');
    const drawSubZoneButton = document.createElement('button');
    const addDeviceButton = document.createElement('button');
    const clearCanvasButton = document.createElement('button');
    const saveReceiverButton = document.createElement('button');
    const SetScaleButton = document.createElement('button');
    let img = new Image();
    let tmpcords = null;
    let finalcords = {
        floor: [] // Array to manage multiple floors
      };
    let tmpfinalcords = [];
    // Array to store circles
    const circles = [];
    let receiverName = "";
    let receiverOptions = []; // Receiver names known from Bermuda sensors
    let zoneName = "";
    // Floor names come from two places that can disagree in case/whitespace:
    // the typed floor-name field and the map file basename. Always compare
    // them normalized (the map card does the same).
    const sameFloorName = (a, b) =>
        String(a || "").trim().toLowerCase() === String(b || "").trim().toLowerCase();
    // Receiver/zone ids can be user-typed (Custom name…); never trust them in HTML.
    const escHtml = s => String(s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // Map viewport: zoom/pan applied as one canvas transform. All stored
    // coordinates stay in the fixed 2000px-wide world space; only rendering
    // and mouse conversion go through the view.
    const view = { zoom: 1, x: 0, y: 0 };

    function clampView() {
        view.zoom = Math.max(1, Math.min(8, view.zoom));
        view.x = Math.max(canvas.width * (1 - view.zoom), Math.min(0, view.x));
        view.y = Math.max(canvas.height * (1 - view.zoom), Math.min(0, view.y));
    }

    function worldFromEvent(event) {
        const rect = canvas.getBoundingClientRect();
        const px = (event.clientX - rect.left) * (canvas.width / rect.width);
        const py = (event.clientY - rect.top) * (canvas.height / rect.height);
        return { x: (px - view.x) / view.zoom, y: (py - view.y) / view.zoom };
    }

    function cssFromWorld(wx, wy) {
        const rect = canvas.getBoundingClientRect();
        // Floating inputs are position:absolute children of <body>; go through
        // the canvas rect (robust to the canvas's offsetParent) to viewport
        // coords, then to document coords with scroll.
        const px = (wx * view.zoom + view.x) * (rect.width / canvas.width);
        const py = (wy * view.zoom + view.y) * (rect.height / canvas.height);
        return {
            left: rect.left + window.scrollX + px,
            top: rect.top + window.scrollY + py,
        };
    }

    // A map is ready to draw/navigate only while a floor is selected; Clear
    // Canvas leaves the last image loaded but blanks SelMapName, so this stops
    // a stray wheel/pan from resurrecting the cleared floor.
    function mapReady() {
        return img.naturalWidth > 0 && !!SelMapName;
    }

    // Synchronously drawable images. Icons drawn via fresh Image objects and
    // async onload callbacks ghosted the canvas during drags: pending loads
    // fired after newer frames had already cleared, stamping stale positions.
    const imageCache = new Map();
    function getCachedImage(url) {
        let cached = imageCache.get(url);
        if (!cached) {
            cached = new Image();
            cached.src = url;
            cached.onload = () => {
                if (mapReady() && !drawToolActive()) redrawAll();
            };
            imageCache.set(url, cached);
        }
        return cached.complete && cached.naturalWidth > 0 ? cached : null;
    }

    function redrawAll() {
        clearCanvas();
        drawElements();
        drawTrackOverlay();
    }
    const createZoneId = () => `zone_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    let isDrawing = false;
    let SelMapName = "";
    let new_floor = true;
    let removefile = false;
    let imgfilename = "";
    let device = "";
    let myScaleVal = null;
    const DEFAULT_TRACKER_ICON = "/bps/person.svg";

    function ensureTrackerIconsStore() {
        if (!finalcords.tracker_icons || typeof finalcords.tracker_icons !== "object") {
            finalcords.tracker_icons = {};
        }
    }

    function getTrackerEntityKey() {
        return device.replace("sensor.", "");
    }

    function getSelectedTrackerIcon() {
        ensureTrackerIconsStore();
        const trackerKey = getTrackerEntityKey();
        const storedIcon = finalcords.tracker_icons[trackerKey];
        if (storedIcon === "person.svg") {
            return "/bps/person.svg";
        }
        if (storedIcon === "beacon.svg") {
            return "/bps/beacon.svg";
        }
        return storedIcon || DEFAULT_TRACKER_ICON;
    }

    function ensureIconOption(value, label = null) {
        if (!trackerIconSelector) {
            return;
        }
        if (!value) {
            return;
        }
        const existing = Array.from(trackerIconSelector.options).find(option => option.value === value);
        if (existing) {
            if (label) {
                existing.textContent = label;
            }
            return;
        }
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label || value;
        trackerIconSelector.appendChild(option);
    }

    async function loadTrackerIcons() {
        if (!trackerIconSelector) {
            return;
        }
        // Keep defaults available even if API call fails.
        ensureIconOption("/bps/person.svg", "Person (default)");
        ensureIconOption("/bps/beacon.svg", "Beacon");
        try {
            const response = await fetch("/api/bps/tracker_icons");
            if (!response.ok) {
                return;
            }
            const icons = await response.json();
            icons.forEach(icon => {
                if (icon && icon.value) {
                    ensureIconOption(icon.value, icon.label || icon.value);
                }
            });
        } catch (error) {
            console.error("Failed loading tracker icons:", error);
        }
    }

    // =================================================================
    // Fetch existing maps
    // =================================================================

        async function getSavedMaps(){
            const mapsResponse = await fetch('/api/bps/maps');
            if (!mapsResponse.ok) {
                console.error('Failed to fetch maps:', mapsResponse.statusText);
                alert('Could not load maps.');
                return false;
            }
        
            const maps = await mapsResponse.json();
            mapSelector.innerHTML = '<option value="">--Please choose an option--</option>';
            maps.forEach(map => {
                const option = document.createElement('option');
                option.value = map;
                option.textContent = map;
                mapSelector.appendChild(option);
            });
            return true;
        }
        
    
        // Once the maps are loaded, call fetchBPSData
        await loadTrackerIcons();
        let tmpsaved = await getSavedMaps();
        if (tmpsaved){
            await fetchBPSData();
        }


        let stoptrackstat = false;
        let pollTrackActive = false; // interval-based tracking session running
        function startTrackfunc(){
            stoptrackstat = false;
            pollTrackActive = true;
            starttrackbtn.style.display = "none";
            stoptrackbtn.style.display = "";
            const interval = setInterval(async () => {
                if (stoptrackstat) {
                    clearInterval(interval);
                    pollTrackActive = false;
                    stoptrackstat = false;
                    lastTrack = null;
                    starttrackbtn.style.display = "";
                    stoptrackbtn.style.display = "none";
                    zonediv.style.display = "none";
                    if (img.naturalWidth > 0) redrawAll();
                    return;
                }
                let apiresponse = await fetchBPSCords();
                if (!Array.isArray(apiresponse) || apiresponse.length === 0) {
                    return;
                }
                let result = apiresponse.find(item => item.ent === device.replace("sensor.",""));
                if (!result || !Array.isArray(result.cords) || result.cords.length < 2) {
                    return;
                }
                let dt = {x: result.cords[0], y:result.cords[1]};
                const circles = sameFloorName(result.floor, SelMapName) ? result.radii : null;
                drawTracker(dt, circles);
                zonediv.style.display = "";
                document.getElementById("zonevalue").textContent = result.zone || "unknown";
            }, 500); // Run every half second
        }

        function stoptrackfunc(){
            stoptrackstat = true;
        }

        starttrackbtn.addEventListener("click", function() {
            if (device == "") {
                alert("You must choose a device to track!");
                return;
            }
            startTrackfunc();
        });
        stoptrackbtn.addEventListener("click", stoptrackfunc);


    // =================================================================
    // Triliterate functionality
    // =================================================================
    let lastTrack = null;

    function drawTrackOverlay() {
        if (!lastTrack || !pollTrackActive) return;
        drawDistanceCircles(lastTrack.circles);
        const iconSize = canvas.width * 0.04;
        const icon = getCachedImage(getSelectedTrackerIcon());
        if (icon) {
            ctx.drawImage(icon, lastTrack.x - iconSize / 2, lastTrack.y - iconSize / 2, iconSize, iconSize);
        }
    }

    // Hue keyed to the receiver's index on the floor (matched by placed
    // coordinates), so a receiver's icon and its distance circle always share
    // a color; golden-angle spacing keeps neighboring hues contrasting.
    function floorReceiverHue(x, y) {
        const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
        const receivers = (floor && floor.receivers) || [];
        for (let i = 0; i < receivers.length; i++) {
            const c = receivers[i].cords;
            if (c && Math.abs(c.x - x) < 0.5 && Math.abs(c.y - y) < 0.5) {
                return Math.round((i * 137.508) % 360);
            }
        }
        return Math.abs(Math.round(x) * 31 + Math.round(y) * 17) % 360;
    }

    const beaconBase = new Image();
    beaconBase.src = "beacon.svg";
    const beaconTintCache = new Map();

    function tintedBeacon(hue, size) {
        if (!beaconBase.complete || beaconBase.naturalWidth === 0) return null;
        const px = Math.max(8, Math.round(size));
        const key = `${hue}|${px}`;
        let tile = beaconTintCache.get(key);
        if (!tile) {
            tile = document.createElement("canvas");
            tile.width = px;
            tile.height = px;
            const tctx = tile.getContext("2d");
            tctx.drawImage(beaconBase, 0, 0, px, px);
            tctx.globalCompositeOperation = "source-in";
            tctx.fillStyle = `hsl(${hue}, 90%, 42%)`;
            tctx.fillRect(0, 0, px, px);
            beaconTintCache.set(key, tile);
        }
        return tile;
    }

    // With circles enabled the icon takes the circle's color, so each circle
    // can be traced back to its receiver at a glance.
    function drawReceiverIcon(x, y, iconSize) {
        if (circleToggle.checked) {
            const tinted = tintedBeacon(floorReceiverHue(x, y), iconSize);
            if (tinted) {
                ctx.drawImage(tinted, x - iconSize / 2, y - iconSize / 2, iconSize, iconSize);
                return;
            }
        }
        const base = getCachedImage("beacon.svg");
        if (base) {
            ctx.drawImage(base, x - iconSize / 2, y - iconSize / 2, iconSize, iconSize);
        }
    }

    // A rounded, filled pill with centered white text, clamped to the canvas
    // so it stays readable over whatever it covers.
    function drawLabelPill(text, cx, cy, hue) {
        ctx.font = "600 18px system-ui, sans-serif";
        const textWidth = ctx.measureText(text).width;
        const padX = 8;
        const height = 26;
        const width = textWidth + padX * 2;
        const x = Math.max(2, Math.min(canvas.width - width - 2, cx - width / 2));
        const y = Math.max(2, Math.min(canvas.height - height - 2, cy - height / 2));
        ctx.beginPath();
        if (ctx.roundRect) {
            ctx.roundRect(x, y, width, height, 8);
        } else {
            ctx.rect(x, y, width, height);
        }
        ctx.fillStyle = `hsla(${hue}, 85%, 30%, 0.92)`;
        ctx.fill();
        ctx.fillStyle = "#ffffff";
        ctx.fillText(text, x + padX, y + height - 8);
    }

    // Each receiver's measured distance as a circle: the device is where
    // they intersect. Bold stroke in the receiver's own color, faint fill so
    // overlapping regions darken toward the intersection. Each receiver also
    // gets a pill on its icon with the measured distance (grid unit applies).
    function drawDistanceCircles(circles) {
        if (!circleToggle.checked || !Array.isArray(circles)) return;
        let focusCoords = null;
        if (focusedReceiver) {
            const focusFloor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
            const focused = focusFloor && (focusFloor.receivers || []).find(r => r.entity_id === focusedReceiver);
            if (focused && focused.cords) focusCoords = focused.cords;
        }
        const valid = circles.filter(c =>
            Array.isArray(c) && [c[0], c[1], c[2]].every(Number.isFinite) && c[2] > 0
            && (!focusCoords || (Math.abs(c[0] - focusCoords.x) < 0.5 && Math.abs(c[1] - focusCoords.y) < 0.5)));
        valid.forEach((c) => {
            const hue = floorReceiverHue(c[0], c[1]);
            ctx.beginPath();
            ctx.arc(c[0], c[1], c[2], 0, Math.PI * 2);
            ctx.fillStyle = `hsla(${hue}, 95%, 55%, 0.10)`;
            ctx.fill();
            ctx.strokeStyle = `hsla(${hue}, 95%, 48%, 0.95)`;
            ctx.lineWidth = 4;
            ctx.stroke();
        });
        // Labels after every circle so no stroke crosses the text.
        const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
        const scale = floor && floor.scale;
        if (!scale) return;
        valid.forEach((c) => {
            const meters = c[2] / scale;
            const text = gridUnit === "ft"
                ? `${(meters * 3.28084).toFixed(1)} ft`
                : `${meters.toFixed(1)} m`;
            drawLabelPill(text, c[0], c[1], floorReceiverHue(c[0], c[1]));
        });
    }

    function drawTracker(tricords, circles){
        lastTrack = { x: tricords.x, y: tricords.y, circles: circles };
        redrawAll();
    }

    // =================================================================
    // Other functions
    // =================================================================
        // Data saved before floor names were compared normalized can contain
        // floors differing only in case (e.g. "Main" and "MAIN"). All lookups
        // take the first match, so fold such duplicates into one entry.
        function mergeDuplicateFloors() {
            if (!Array.isArray(finalcords.floor)) return;
            const merged = [];
            finalcords.floor.forEach(floor => {
                const existing = merged.find(f => sameFloorName(f.name, floor.name));
                if (!existing) {
                    merged.push(floor);
                    return;
                }
                existing.receivers = existing.receivers || [];
                (floor.receivers || []).forEach(receiver => {
                    if (!existing.receivers.some(r => r.entity_id === receiver.entity_id)) {
                        existing.receivers.push(receiver);
                    }
                });
                existing.zones = (existing.zones || []).concat(floor.zones || []);
                existing.subzones = (existing.subzones || []).concat(floor.subzones || []);
                if (existing.scale == null) {
                    existing.scale = floor.scale;
                }
            });
            finalcords.floor = merged;
        }

        // Function to fetch data from the API and display it on page
        async function fetchBPSData() {
            const apiUrl = "/api/bps/read_text"; // API endpoint to read the file
        
            try {
                const response = await fetch(apiUrl); // Make a GET request to the API
        
            if (!response.ok) {
                console.error("Failed to fetch BPS data:", response.statusText); // Handle error status
                return;
            }
        
            const data = await response.json();

            // Read the receiver list before parsing the coordinates: on a
            // fresh install bpsdata.txt is empty and JSON.parse would throw,
            // yet the receiver picker is needed exactly then.
            receiverOptions = Array.isArray(data.receivers) ? [...data.receivers].sort() : [];
            console.log("Known receivers:", receiverOptions);

            if (data.coordinates) {
                finalcords = JSON.parse(data.coordinates);
                mergeDuplicateFloors();
                tmpfinalcords = finalcords; //Store original cords in a temp to compare later if it is changed
            }
            ensureTrackerIconsStore();
            console.log("Coordinates loaded:", finalcords);
            let ents = data.entities;
            console.log("Entities to track:", ents);

            entSelector.innerHTML = '<option value="">--Please choose an option--</option>';
            ents.forEach(ent => {
                const option = document.createElement('option');
                option.value = ent;
                option.textContent = ent;
                entSelector.appendChild(option);
            });

            } catch (error) {
                console.error("Error fetching BPS data:", error); // Handle possible error during fetch-call
            }
        }

        async function fetchBPSCords() {
            const apiUrl = "/api/bps/cords"; 
        
            try {
                const response = await fetch(apiUrl); // Make a GET request to the API
        
            if (!response.ok) {
                console.error("Failed to fetch BPS data:", response.statusText); // Handle error status
                return [];
            }
        
            const data = await response.json();
            return data;

            } catch (error) {
            // Handle possible error during fetch-call
            console.error("Error fetching BPS data:", error);
            return [];
            }
        }

        // Choose which entity to track
        entSelector.addEventListener('change', async () => {
            if(entSelector.value != "--Please choose an option--"){
                console.log("väljare");
                stoptrackstat = true;
                device = "sensor."+entSelector.value;
                const selectedIcon = getSelectedTrackerIcon();
                if (trackerIconSelector) {
                    ensureIconOption(selectedIcon);
                    trackerIconSelector.value = selectedIcon;
                }
                starttrackbtn.style.display = "";
            } else {
                starttrackbtn.style.display = "none";
            }
        });

        if (trackerIconSelector) {
            trackerIconSelector.addEventListener("change", () => {
                if (!device) {
                    return;
                }
                ensureTrackerIconsStore();
                finalcords.tracker_icons[getTrackerEntityKey()] = trackerIconSelector.value;
                savebuttondiv.appendChild(saveButton);
            });
        }

        if (uploadTrackerIconButton && trackerIconUpload) {
            uploadTrackerIconButton.addEventListener("click", async () => {
                if (!device) {
                    alert("Choose tracker first.");
                    return;
                }
                const iconFile = trackerIconUpload.files[0];
                if (!iconFile) {
                    alert("Choose an icon file first.");
                    return;
                }
                const uploadData = new FormData();
                uploadData.append("icon", iconFile);
                try {
                    const response = await fetch("/api/bps/upload_tracker_icon", {
                        method: "POST",
                        body: uploadData,
                    });
                    if (!response.ok) {
                        alert("Could not upload icon.");
                        return;
                    }
                    const payload = await response.json();
                    if (!payload || !payload.icon_url) {
                        alert("Could not upload icon.");
                        return;
                    }
                    ensureIconOption(payload.icon_url, payload.icon_name || payload.icon_url);
                    if (trackerIconSelector) {
                        trackerIconSelector.value = payload.icon_url;
                    }
                    ensureTrackerIconsStore();
                    finalcords.tracker_icons[getTrackerEntityKey()] = payload.icon_url;
                    savebuttondiv.appendChild(saveButton);
                    alert("Tracker icon uploaded. Click Save Floor Plan to persist.");
                } catch (error) {
                    console.error("Icon upload failed:", error);
                    alert("Could not upload icon.");
                }
            });
        }
    
    
    // Check if the image is loaded in the canvas
    function checkCanvasImage() {
        if (canvas.width === 0 || canvas.height === 0) {
            alert("Please load a floorplan first.");
            return false;
        }
        return true;
    }

    // Remove all listeners
    function removeListeners(){
        canvas.removeEventListener("mousedown", zoneMouseDown);
        canvas.removeEventListener("mousemove", zoneMouseMove);
        canvas.removeEventListener("mouseup", zoneMouseUp);
        canvas.removeEventListener("contextmenu", zoneUndoPoint);
        canvas.removeEventListener("mousedown", startDrawingScale);
        canvas.removeEventListener("mouseup", endDrawingScale);
        canvas.removeEventListener('click', placeReceiver);
    }

    //Reset all buttons
    function buttonreset(){
        if (scaleInputElement) {scaleInputElement.style.display = "none";}
        SetScaleButton.innerHTML = SetScaleButton.innerHTML.replace("Save Scale","Set Scale");
        SetScaleButton.setAttribute('data-active', 'false');
        if (entityInput) {entityInput.style.display = "none";}
        addDeviceButton.innerHTML = addDeviceButton.innerHTML.replace("Save Receiver","Place Receiver");
        addDeviceButton.setAttribute('data-active', 'false');
        if (zoneInputElement) {zoneInputElement.style.display = "none"; zoneInputElement.value = "";}
        drawAreaButton.innerHTML = drawAreaButton.innerHTML.replace("Save Zone","Draw Zone");
        drawAreaButton.setAttribute('data-active', 'false');
        drawSubZoneButton.innerHTML = drawSubZoneButton.innerHTML.replace("Save Sub-Zone","Draw Sub-Zone");
        drawSubZoneButton.setAttribute('data-active', 'false');
        focusedReceiver = null;
        messdiv.innerHTML = "";
    }

    document.addEventListener('click', (event) => {
        // Check if the clicked element has the attribute data-type="removerec"
        if (event.target.closest('[data-type="removerec"]')) {
            const button = event.target.closest('[data-type="removerec"]'); // Get the button that was pressed
            const idToRemove = button.getAttribute('data-id'); // Get the value from data-id
            // A stale sidebar (e.g. after Clear Canvas) must not fake a save.
            if (!finalcords.floor.some(f => sameFloorName(f.name, SelMapName))) return;
            // Loop through each floor and remove receivers where the entity_id matches
            finalcords.floor.forEach(floor => {
                if (sameFloorName(floor.name, SelMapName)) {
                    floor.receivers = floor.receivers.filter(receiver => receiver.entity_id !== idToRemove);
                }
            });
            console.log(`Removed receiver "${idToRemove}"`);
            savebuttondiv.appendChild(saveButton);
            clearCanvas();
            drawElements(); // re-renders the sidebar tree too
        }
        if (event.target.closest('[data-type="removezone"]')) {
            const button = event.target.closest('[data-type="removezone"]'); // Get the button that was pressed
            const idToRemove = button.getAttribute('data-id'); // Get the value from data-id
            // A stale sidebar (e.g. after Clear Canvas) must not fake a save.
            if (!finalcords.floor.some(f => sameFloorName(f.name, SelMapName))) return;
            // Loop through each floor and remove zones where the internal zone id matches
            finalcords.floor.forEach(floor => {
                if (sameFloorName(floor.name, SelMapName)) {
                    const removed = (floor.zones || []).find(zone => (zone.zone_id || zone.entity_id) === idToRemove);
                    floor.zones = floor.zones.filter(zone => (zone.zone_id || zone.entity_id) !== idToRemove);
                    // A sub-zone can't outlive the zone it sits in (matched by
                    // stable id, so a same-named sibling zone keeps its own).
                    if (removed && Array.isArray(floor.subzones)) {
                        floor.subzones = floor.subzones.filter(s => s.parent !== (removed.zone_id || removed.entity_id));
                    }
                }
            });
            console.log(`Removed zone "${idToRemove}"`);
            savebuttondiv.appendChild(saveButton);
            clearCanvas();
            drawElements(); // re-renders the sidebar tree too
        }
        if (event.target.closest('[data-type="removesubzone"]')) {
            const idToRemove = event.target.closest('[data-type="removesubzone"]').getAttribute('data-id');
            if (!finalcords.floor.some(f => sameFloorName(f.name, SelMapName))) return;
            finalcords.floor.forEach(floor => {
                if (sameFloorName(floor.name, SelMapName) && Array.isArray(floor.subzones)) {
                    floor.subzones = floor.subzones.filter(s => (s.sub_zone_id || s.entity_id) !== idToRemove);
                }
            });
            console.log(`Removed sub-zone "${idToRemove}"`);
            savebuttondiv.appendChild(saveButton);
            clearCanvas();
            drawElements();
        }
        if (event.target.closest('[data-type="editzone"]')) {
            const idToEdit = event.target.closest('[data-type="editzone"]').getAttribute('data-id');
            const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
            const zone = floor && (floor.zones || []).find(z => (z.zone_id || z.entity_id) === idToEdit);
            if (zone) beginEditZone(zone);
        }
        if (event.target.closest('[data-type="editsubzone"]')) {
            const idToEdit = event.target.closest('[data-type="editsubzone"]').getAttribute('data-id');
            const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
            const sub = floor && (floor.subzones || []).find(s => (s.sub_zone_id || s.entity_id) === idToEdit);
            if (sub) beginEditSubZone(sub);
        }
        if (event.target.closest('[data-type="collapse"]')) {
            const collapseDiv = event.target.closest('[data-type="collapse"]');
            const parent = collapseDiv.closest('.fixed'); // Find the nearest parent element to collapseDiv
        
            // Toggle between minimized and normal size
            if (parent.classList.contains('collapsed')) {
                // Reset size
                parent.classList.remove('collapsed');
                parent.style.maxHeight = '80vh'; // Reset height
                parent.querySelectorAll('.space-y-4, #message').forEach(el => {
                    el.style.display = ''; // Show element
                });
            } else {
                // Minimize
                parent.classList.add('collapsed');
                const computedStyleCD = window.getComputedStyle(collapseDiv);
                const computedStyleP = window.getComputedStyle(parent);
                const newheight = parseFloat(computedStyleCD.height) + parseFloat(computedStyleP.paddingTop) + parseFloat(computedStyleP.paddingBottom) - parseFloat(computedStyleCD.paddingBottom);
                parent.style.maxHeight = `${newheight}px`; // Adjust height to collapseDiv
                parent.querySelectorAll('.space-y-4, #message').forEach(el => {
                    el.style.display = 'none'; // Hide element
                });
            }
        }
    });

    // =================================================================
    // Clear canvas functionality
    // =================================================================

    clearCanvasButton.addEventListener('click', () => {
        if (!checkCanvasImage()) return;
        removeListeners();
        drawAreaButton.remove();
        drawSubZoneButton.remove();
        addDeviceButton.remove();
        clearCanvasButton.remove();
        SetScaleButton.remove();
        saveButton.remove();
        deleteButton.remove();
        view.zoom = 1;
        view.x = 0;
        view.y = 0;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        mapname.value = "";
        SelMapName = "";
        buttonreset();
        mapSelector.selectedIndex = 0;
        renderEntityTree(null);
    });

    function clearCanvas(){
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.setTransform(view.zoom, 0, 0, view.zoom, view.x, view.y);
        if (mapReady()) {
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        }
        messdiv.innerHTML = "";
    }

    // =================================================================
    // Draw zones
    // =================================================================

    // Zones are polygons: click to add vertices, drag a vertex to move it,
    // drag the shape's inside to move the whole zone. Legacy zones (from the
    // old rectangle tool) stored their four corners in scan order; new zones
    // carry poly: true and keep the order they were drawn in.
    let zonePoints = [];
    let selectedVertex = null;
    let draggingZone = false;
    let dragLast = null;
    let zoneInputElement = null; // För att hantera input-fältet
    // What the polygon editor is currently building/editing:
    //   {kind:'zone'|'subzone', id:<existing id|null>, parent, parentPoints, color}
    // New zones/sub-zones have id null; sidebar "edit" loads an existing id.
    let editTarget = null;

    const handleSize = 15;

    // Points in drawable perimeter order for both formats.
    function zonePerimeterPoints(zone) {
        const pts = zone.cords || [];
        if (!zone.poly && pts.length === 4) {
            return [pts[0], pts[1], pts[3], pts[2]]; // legacy TL,TR,BL,BR
        }
        return pts;
    }

    function pointInPolygon(x, y, pts) {
        let inside = false;
        for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
            const intersects = (pts[i].y > y) !== (pts[j].y > y)
                && x < ((pts[j].x - pts[i].x) * (y - pts[i].y)) / (pts[j].y - pts[i].y) + pts[i].x;
            if (intersects) inside = !inside;
        }
        return inside;
    }

    function zoneMousePos(event) {
        const world = worldFromEvent(event);
        return {
            x: Math.max(0, Math.min(canvas.width, world.x)),
            y: Math.max(0, Math.min(canvas.height, world.y)),
        };
    }

    const createSubZoneId = () => `subzone_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

    // Each sub-zone gets its own hue so overlapping ones stay distinguishable.
    function randomZoneColor() {
        return `hsl(${Math.floor(Math.random() * 360)}, 70%, 45%)`;
    }

    // Nearest point on segment ab to p.
    function projectPointToSegment(p, a, b) {
        const dx = b.x - a.x, dy = b.y - a.y;
        const len2 = dx * dx + dy * dy;
        if (len2 === 0) return { x: a.x, y: a.y };
        let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2;
        t = Math.max(0, Math.min(1, t));
        return { x: a.x + t * dx, y: a.y + t * dy };
    }

    // Keep a point inside a polygon: unchanged when inside, else projected onto
    // the nearest edge. This is how a sub-zone corner is stopped from leaving
    // its parent zone (the client-side mirror of the backend's snap-into-zone).
    function clampPointToPolygon(pt, polyPts) {
        if (!polyPts || polyPts.length < 3) return pt;
        if (pointInPolygon(pt.x, pt.y, polyPts)) return pt;
        let best = null, bestD = Infinity;
        for (let i = 0, j = polyPts.length - 1; i < polyPts.length; j = i++) {
            const proj = projectPointToSegment(pt, polyPts[j], polyPts[i]);
            const d = Math.hypot(proj.x - pt.x, proj.y - pt.y);
            if (d < bestD) { bestD = d; best = proj; }
        }
        return best || pt;
    }

    // A sub-zone's points are constrained to its parent zone; a main zone's are
    // only clamped to the canvas (already done by zoneMousePos).
    function constrainForEdit(pos) {
        if (editTarget && editTarget.kind === 'subzone' && editTarget.parentPoints) {
            return clampPointToPolygon(pos, editTarget.parentPoints);
        }
        return pos;
    }

    // True when p is inside the polygon or essentially on its edge. Used for
    // whole-shape sub-zone drags: a strict inside test would reject the move as
    // soon as any corner sits on the parent boundary (which clamping puts there),
    // so a sub-zone traced along a wall could never be moved.
    function insideOrOnParent(p, poly) {
        if (!poly) return true;
        const c = clampPointToPolygon(p, poly);
        return Math.hypot(c.x - p.x, c.y - p.y) <= 0.75;
    }

    // Topmost saved zone (last drawn) whose polygon contains pos.
    function hitZoneAt(pos) {
        const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
        if (!floor) return null;
        const zones = floor.zones || [];
        for (let i = zones.length - 1; i >= 0; i--) {
            const pts = zonePerimeterPoints(zones[i]);
            if (pts.length >= 3 && pointInPolygon(pos.x, pos.y, pts)) return zones[i];
        }
        return null;
    }

    function attachZoneHandlers() {
        canvas.addEventListener("mousedown", zoneMouseDown);
        canvas.addEventListener("mousemove", zoneMouseMove);
        canvas.addEventListener("mouseup", zoneMouseUp);
        canvas.addEventListener("contextmenu", zoneUndoPoint);
    }

    drawAreaButton.addEventListener("click", () => {
        if (!checkCanvasImage()) return;
        removeListeners();
        clearCanvas();
        drawElements();

        if (drawAreaButton.dataset.active === 'false') {
            buttonreset();
            zonePoints = [];
            selectedVertex = null;
            draggingZone = false;
            editTarget = { kind: 'zone', id: null };
            attachZoneHandlers();
            drawAreaButton.innerHTML = drawAreaButton.innerHTML.replace("Draw Zone","Save Zone");
            drawAreaButton.setAttribute('data-active', 'true');
            messdiv.innerHTML = '<h4 class="font-medium mb-2">Instructions</h4><p class="text-sm text-gray-500">Click the floor image to place the zone\'s corners, one by one — any shape with three or more corners works. Drag a corner to adjust it, or drag the inside of the zone to move the whole zone. Right-click a corner to delete it. Enter the zone name (matching your Home Assistant areas is a good idea) and press Save Zone.</p>';
        } else if (drawAreaButton.dataset.active === 'true') {
            if (finalizeShape()) {
                buttonreset();
                if (zoneInputElement) zoneInputElement.value = "";
                zonePoints = [];
                editTarget = null;
                clearCanvas();
                drawElements();
            }
        }
    });

    // Sub-zone tool: draw a polygon inside a chosen parent zone (a couch, a
    // desk). Shares the polygon editor with Draw Zone; the difference is a
    // parent that every corner is clamped inside, and a random color.
    drawSubZoneButton.addEventListener("click", () => {
        if (!checkCanvasImage()) return;
        removeListeners();
        clearCanvas();
        drawElements();

        if (drawSubZoneButton.dataset.active === 'false') {
            const floor = finalcords.floor.find(f => sameFloorName(f.name, mapname.value));
            if (!floor || !((floor.zones || []).length)) {
                alert("Draw at least one zone first — a sub-zone is placed inside a zone.");
                return;
            }
            buttonreset();
            zonePoints = [];
            selectedVertex = null;
            draggingZone = false;
            editTarget = { kind: 'subzone', id: null, parent: null, parentPoints: null, color: randomZoneColor() };
            attachZoneHandlers();
            drawSubZoneButton.innerHTML = drawSubZoneButton.innerHTML.replace("Draw Sub-Zone","Save Sub-Zone");
            drawSubZoneButton.setAttribute('data-active', 'true');
            messdiv.innerHTML = '<h4 class="font-medium mb-2">Instructions</h4><p class="text-sm text-gray-500">Click inside the zone you want to add a sub-zone to — that becomes its parent — then keep clicking to place corners (they stay inside the parent). Drag a corner to adjust it, drag the inside to move it, right-click a corner to delete it. Name it (e.g. Couch) and press Save Sub-Zone.</p>';
        } else if (drawSubZoneButton.dataset.active === 'true') {
            if (finalizeShape()) {
                buttonreset();
                if (zoneInputElement) zoneInputElement.value = "";
                zonePoints = [];
                editTarget = null;
                clearCanvas();
                drawElements();
            }
        }
    });

    // Commit the polygon in the editor into finalcords: add a new zone/sub-zone,
    // or update the existing one when editTarget.id is set.
    function finalizeShape() {
        if (!mapname.value) { alert("Please enter a floor name!"); return false; }
        SelMapName = mapname.value;
        const isSub = !!(editTarget && editTarget.kind === 'subzone');
        if (zonePoints.length < 3) {
            alert((isSub ? "A sub-zone" : "A zone") + " needs at least three corners.");
            return false;
        }
        const nameEl = document.getElementById('zoneName');
        const name = (nameEl ? nameEl.value : "").trim();
        if (!name) { alert("Please provide a name."); return false; }
        const cords = zonePoints.map(p => ({ x: p.x, y: p.y }));
        const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));

        if (isSub) {
            if (!editTarget.parent) { alert("Click inside a zone first to choose the sub-zone's parent."); return false; }
            if (!floor) { alert("Select a floor first."); return false; }
            if (!Array.isArray(floor.subzones)) floor.subzones = [];
            if (editTarget.id) {
                const sz = floor.subzones.find(s => (s.sub_zone_id || s.entity_id) === editTarget.id);
                if (sz) { sz.entity_id = name; sz.cords = cords; sz.parent = editTarget.parent; sz.poly = true; }
            } else {
                floor.subzones.push({
                    sub_zone_id: createSubZoneId(),
                    entity_id: name,
                    parent: editTarget.parent,
                    poly: true,
                    color: editTarget.color || randomZoneColor(),
                    cords,
                });
            }
            savebuttondiv.appendChild(saveButton);
            alert(`Sub-zone saved: ${name}`);
            return true;
        }

        // Main zone: update in place when editing, else add a new one.
        if (editTarget && editTarget.id) {
            const z = floor && (floor.zones || []).find(zz => (zz.zone_id || zz.entity_id) === editTarget.id);
            if (z) { z.entity_id = name; z.cords = cords; z.poly = true; }
            savebuttondiv.appendChild(saveButton);
            alert(`Zone saved: ${name}`);
            return true;
        }
        zoneName = name;
        const newZone = { zone_id: createZoneId(), entity_id: name, poly: true, cords };
        if (addDataToFloor(finalcords, SelMapName, "zones", newZone)) {
            alert(`Zone saved: ${name}`);
            return true;
        }
        return false;
    }

    // Load a saved zone/sub-zone into the polygon editor (reusing the draw
    // machinery); the matching tool button flips to its "Save" state so the
    // next click on it commits the edit in place.
    function beginEditZone(zone) {
        if (!checkCanvasImage()) return;
        removeListeners();
        buttonreset();
        SelMapName = mapname.value || SelMapName;
        zonePoints = zonePerimeterPoints(zone).map(p => ({ x: p.x, y: p.y }));
        selectedVertex = null;
        draggingZone = false;
        editTarget = { kind: 'zone', id: zone.zone_id || zone.entity_id };
        attachZoneHandlers();
        drawAreaButton.setAttribute('data-active', 'true');
        drawAreaButton.innerHTML = drawAreaButton.innerHTML.replace("Draw Zone","Save Zone");
        drawZonePreview();
        const zn = document.getElementById('zoneName');
        if (zn) zn.value = zone.entity_id || "";
        messdiv.innerHTML = '<h4 class="font-medium mb-2">Editing zone</h4><p class="text-sm text-gray-500">Drag a corner to move it, drag the inside to move the whole zone, right-click a corner to delete it, or click empty space to add one. Press Save Zone to keep the changes.</p>';
    }

    function beginEditSubZone(sub) {
        if (!checkCanvasImage()) return;
        removeListeners();
        buttonreset();
        SelMapName = mapname.value || SelMapName;
        const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
        const parentZone = floor && (floor.zones || []).find(z => (z.zone_id || z.entity_id) === sub.parent);
        zonePoints = (sub.cords || []).map(p => ({ x: p.x, y: p.y }));
        selectedVertex = null;
        draggingZone = false;
        editTarget = {
            kind: 'subzone',
            id: sub.sub_zone_id || sub.entity_id,
            parent: sub.parent,
            parentPoints: parentZone ? zonePerimeterPoints(parentZone).map(p => ({ x: p.x, y: p.y })) : null,
            color: sub.color || randomZoneColor(),
        };
        attachZoneHandlers();
        drawSubZoneButton.setAttribute('data-active', 'true');
        drawSubZoneButton.innerHTML = drawSubZoneButton.innerHTML.replace("Draw Sub-Zone","Save Sub-Zone");
        drawZonePreview();
        const zn = document.getElementById('zoneName');
        if (zn) zn.value = sub.entity_id || "";
        messdiv.innerHTML = '<h4 class="font-medium mb-2">Editing sub-zone</h4><p class="text-sm text-gray-500">Corners stay inside the parent zone. Drag a corner, drag the inside to move it, right-click a corner to delete it. Press Save Sub-Zone to keep the changes.</p>';
    }

    function zoneMouseDown(event) {
        const pos = zoneMousePos(event);

        // Sub-zone: the first click chooses the parent zone and seeds corner 1.
        if (editTarget && editTarget.kind === 'subzone' && !editTarget.parent) {
            const parent = hitZoneAt(pos);
            if (!parent) { alert("Click inside the zone you want to add a sub-zone to."); return; }
            if (!parent.zone_id) parent.zone_id = createZoneId();
            editTarget.parent = parent.zone_id; // stable id, survives renames / same-named zones
            editTarget.parentPoints = zonePerimeterPoints(parent).map(p => ({ x: p.x, y: p.y }));
            zonePoints = [clampPointToPolygon(pos, editTarget.parentPoints)];
            drawZonePreview();
            return;
        }

        selectedVertex = null;
        draggingZone = false;

        for (let i = 0; i < zonePoints.length; i++) {
            if (Math.hypot(zonePoints[i].x - pos.x, zonePoints[i].y - pos.y) <= handleSize * 2) {
                selectedVertex = i;
                return;
            }
        }
        if (zonePoints.length >= 3 && pointInPolygon(pos.x, pos.y, zonePoints)) {
            draggingZone = true;
            dragLast = pos;
            return;
        }
        zonePoints.push(constrainForEdit(pos));
        drawZonePreview();
    }

    function zoneMouseMove(event) {
        if (selectedVertex === null && !draggingZone) return;
        const pos = zoneMousePos(event);

        if (selectedVertex !== null) {
            zonePoints[selectedVertex] = constrainForEdit(pos);
        } else if (draggingZone) {
            let dx = pos.x - dragLast.x;
            let dy = pos.y - dragLast.y;
            if (editTarget && editTarget.kind === 'subzone' && editTarget.parentPoints) {
                // Move only if every corner stays inside the parent zone, so the
                // sub-zone keeps its shape and never leaks out of its parent.
                const moved = zonePoints.map(p => ({ x: p.x + dx, y: p.y + dy }));
                if (moved.every(p => insideOrOnParent(p, editTarget.parentPoints))) {
                    zonePoints = moved;
                    dragLast = pos;
                }
            } else {
                // Main zone: clamp the translation so no corner can leave the
                // canvas — a corner outside the visible area can't be grabbed.
                const xs = zonePoints.map(p => p.x);
                const ys = zonePoints.map(p => p.y);
                dx = Math.max(-Math.min(...xs), Math.min(canvas.width - Math.max(...xs), dx));
                dy = Math.max(-Math.min(...ys), Math.min(canvas.height - Math.max(...ys), dy));
                zonePoints = zonePoints.map(p => ({ x: p.x + dx, y: p.y + dy }));
                dragLast = pos;
            }
        }
        drawZonePreview();
    }

    function zoneMouseUp() {
        selectedVertex = null;
        draggingZone = false;
        dragLast = null;
    }

    function zoneUndoPoint(event) {
        event.preventDefault();
        if (!zonePoints.length) return;
        const pos = zoneMousePos(event);
        let idx = -1;
        for (let i = 0; i < zonePoints.length; i++) {
            if (Math.hypot(zonePoints[i].x - pos.x, zonePoints[i].y - pos.y) <= handleSize * 2) {
                idx = i;
                break;
            }
        }
        if (idx >= 0) {
            // Right-clicked a corner: delete it, but never below a triangle.
            if (zonePoints.length > 3) zonePoints.splice(idx, 1);
        } else {
            // Right-clicked empty space: undo the last placed corner.
            zonePoints.pop();
        }
        drawZonePreview();
    }

    function drawZonePreview() {
        clearCanvas();
        drawElements();

        // Create the input field and place it above the zone
        if (!zoneInputElement) {
            zoneInputElement = document.createElement("input");
            zoneInputElement.type = "text";
            zoneInputElement.id = "zoneName";
            zoneInputElement.placeholder = "Name";
            zoneInputElement.classList.add("zone-input");
            document.body.appendChild(zoneInputElement);
        }

        if (!zonePoints.length) {
            zoneInputElement.style.display = "none";
            return;
        }

        const cx = zonePoints.reduce((s, p) => s + p.x, 0) / zonePoints.length;
        const topY = Math.min(...zonePoints.map(p => p.y));
        const css = cssFromWorld(cx, topY);

        zoneInputElement.style.left = `${css.left - 40}px`;
        zoneInputElement.style.top = `${css.top - 40}px`;
        zoneInputElement.style.display = "block";
        zoneInputElement.style.position = "absolute";

        // Draw the polygon so far
        ctx.beginPath();
        ctx.moveTo(zonePoints[0].x, zonePoints[0].y);
        for (let i = 1; i < zonePoints.length; i++) {
            ctx.lineTo(zonePoints[i].x, zonePoints[i].y);
        }
        const shapeColor = (editTarget && editTarget.kind === 'subzone') ? (editTarget.color || "#3f51b5") : "red";
        if (zonePoints.length >= 3) {
            ctx.closePath();
            ctx.save();
            ctx.globalAlpha = 0.12; // shows where the shape can be dragged
            ctx.fillStyle = shapeColor;
            ctx.fill();
            ctx.restore();
        }
        ctx.strokeStyle = shapeColor;
        ctx.lineWidth = 2;
        ctx.stroke();

        // Draw handles on every corner
        zonePoints.forEach(point => {
            ctx.beginPath();
            ctx.arc(point.x, point.y, handleSize, 0, Math.PI * 2);
            ctx.fillStyle = shapeColor;
            ctx.fill();
        });
    }

    // =================================================================
    // Set the scale for the floor
    // =================================================================

    let startPoint = null;
    let endPoint = null;
    let scaleInputElement = null; 

    SetScaleButton.addEventListener("click", () => {
        if (!checkCanvasImage()) return;
        removeListeners();
        clearCanvas();
        drawElements();

        if (SetScaleButton.dataset.active === 'false') {
            buttonreset();
            SetScaleButton.innerHTML = SetScaleButton.innerHTML.replace("Set Scale","Save Scale");
            messdiv.innerHTML = '<h4 class="font-medium mb-2">Instructions</h4><p class="text-sm text-gray-500">Set the scale by clicking on the desired starting point and then again on the desired end point. Enter the actual (real-world) distance in the input element</p>';
            startPoint = null;
            endPoint = null;

            canvas.addEventListener("mousedown", startDrawingScale);
            canvas.addEventListener("mouseup", endDrawingScale);
            SetScaleButton.setAttribute('data-active', 'true');
        } else if (SetScaleButton.dataset.active === 'true') {
            saveScale();
        }
    });

    let countclick = 0;
    function startDrawingScale(event) {
        if(countclick === 0){
            startPoint = worldFromEvent(event);
            isDrawing = false;
            countclick++; // Add one to variable

            //Draw starting point
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = 'red'; // Set fill color
            ctx.beginPath(); // Draw a circle
            ctx.arc(startPoint.x, startPoint.y, 10, 0, Math.PI * 2); // Rita en cirkel
            ctx.fill(); // Fill circle

            return;
        }
        if(countclick === 1){
            isDrawing = true;
            countclick = 0;
        }
    }

    function endDrawingScale(event) {
        if (!isDrawing) return;
        endPoint = worldFromEvent(event);
        isDrawing = false;

        if (startPoint.x === endPoint.x && startPoint.y === endPoint.y) {
            console.log("No line drawn")
            return;
        }

        ctx.beginPath();
        ctx.moveTo(startPoint.x, startPoint.y);
        ctx.lineTo(endPoint.x, endPoint.y);
        ctx.strokeStyle = "red"; // Make line red
        ctx.lineWidth = 4;       // Set thickness of line
        ctx.stroke();

        // Create input field and place it above line
        if (!scaleInputElement) {
            scaleInputElement = document.createElement("input");
            scaleInputElement.type = "number";
            scaleInputElement.id = "scaleValue";
            scaleInputElement.placeholder = "m";
            scaleInputElement.classList.add("scale-input");
            document.body.appendChild(scaleInputElement);
        }

        const lineMidpoint = {
            x: (startPoint.x + endPoint.x) / 2,
            y: (startPoint.y + endPoint.y) / 2
        };
        
        const css = cssFromWorld(lineMidpoint.x, lineMidpoint.y);

        scaleInputElement.style.left = `${css.left - scaleInputElement.offsetWidth / 2 + 40}px`;
        scaleInputElement.style.top = `${css.top - 40}px`;
        scaleInputElement.style.display = "block";
        scaleInputElement.style.position = "absolute";
        scaleInputElement.style.width = "60px";
    }

    function saveScale() {
        if (!startPoint || !endPoint || startPoint.x === endPoint.x || startPoint.y === endPoint.y) {
            alert("Please draw a line first.");
            return;
        }

        const scaleInput = parseFloat(scaleValue.value);
        if (isNaN(scaleInput) || scaleInput <= 0) {
            alert("Please enter the actual length in meters.");
            return;
        }

        if (!mapname.value) {
            alert("Floor name must be set.");
            return;
        }
        SelMapName = mapname.value;

        const dx = endPoint.x - startPoint.x;
        const dy = endPoint.y - startPoint.y;
        const lineLength = Math.sqrt(dx * dx + dy * dy); // Calculate length of drawn line
        (`Line length: ${lineLength}`);
        
        // Save scale
        myScaleVal = lineLength / scaleInput;
        if(addDataToFloor(finalcords, SelMapName, "scale", myScaleVal)){
            buttonreset(); //Reset buttons
            clearCanvas(); //Clear canvas
            drawElements(); //Draw elements
        }
    }
    // =================================================================

    // =================================================================
    // Place receiver functionality
    // =================================================================

    let entityInput = null; // Floating receiver picker (select + custom input)
    let receiverSelect = null;
    let receiverCustomInput = null;
    const CUSTOM_RECEIVER_OPTION = "__custom__";

    function getPickedReceiverName() {
        if (!receiverSelect) return "";
        if (receiverSelect.value === CUSTOM_RECEIVER_OPTION) {
            return receiverCustomInput.value.trim();
        }
        return receiverSelect.value.trim();
    }

    function populateReceiverSelect() {
        // A receiver physically exists on one floor: placing the same name on
        // several floors makes them compete for the tracker's floor. Hide
        // names placed on ANY floor; "Custom name…" remains the escape hatch.
        const placedAnywhere = new Set();
        if (Array.isArray(finalcords.floor)) {
            finalcords.floor.forEach(floor => {
                (floor.receivers || []).forEach(r => placedAnywhere.add(r.entity_id));
            });
        }
        receiverSelect.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "-- Select receiver --";
        receiverSelect.appendChild(placeholder);
        receiverOptions
            .filter(name => !placedAnywhere.has(name))
            .forEach(name => {
                const option = document.createElement("option");
                option.value = name;
                option.textContent = name;
                receiverSelect.appendChild(option);
            });
        const custom = document.createElement("option");
        custom.value = CUSTOM_RECEIVER_OPTION;
        custom.textContent = "Custom name…";
        receiverSelect.appendChild(custom);
        receiverSelect.value = "";
        receiverCustomInput.value = "";
        receiverCustomInput.style.display = "none";
    }

    addDeviceButton.addEventListener('click', () => {
        if (!checkCanvasImage()) return;
        removeListeners();
        receiverName = "";

        if (addDeviceButton.dataset.active === 'false') {
            buttonreset();
            messdiv.innerHTML = '<h4 class="font-medium mb-2">Instructions</h4><p class="text-sm text-gray-500">Place a BLE receiver by clicking its location on the floorplan, then pick it from the list. The list shows every receiver Bermuda currently reports (the part after "_distance_to_" in its sensors); receivers already placed on any floor are hidden, since a receiver belongs to one floor. Pick "Custom name…" to type a name manually. Finish with Save Receiver.</p>';

            // A new placement session must not inherit coordinates or a
            // picked name from the previous one.
            tmpcords = null;
            if (receiverSelect) populateReceiverSelect();
            canvas.addEventListener('click', placeReceiver);
            addDeviceButton.setAttribute('data-active', 'true');
            addDeviceButton.innerHTML = addDeviceButton.innerHTML.replace("Place Receiver","Save Receiver");

        } else if (addDeviceButton.dataset.active === 'true') {
            if (!mapname.value) {
                alert("Floor name must be set.");
                return;
            }
            SelMapName = mapname.value;
            receiverName = getPickedReceiverName();

            if (!tmpcords || !Number.isFinite(tmpcords.x) || !Number.isFinite(tmpcords.y)) {
                alert("Receiver coordinates must be set — click the floorplan first.");
                return;
            }
            if (!receiverName) {
                alert("Select a receiver from the list (or pick Custom name… and type one).");
                return;
            }

            let newReceiver = {
                entity_id: receiverName,
                cords: tmpcords
              };

            if(addDataToFloor(finalcords, SelMapName, "receivers", newReceiver)){
                buttonreset();
                clearCanvas();
                drawElements();
                console.log("Receiver saved successfully!");
            } else {
                console.log("Could not save data to array");
            }
        }
    });

    // =================================================================
    // Placera en BLE mottagare
    // =================================================================

    function placeReceiver(event) {

        clearCanvas(); // Remove all drawn elements from canvas
        const pos = zoneMousePos(event);

        drawElements(pos.x, pos.y, "receiver");

        if (!entityInput) {
            entityInput = document.createElement("div");
            entityInput.classList.add("rec-input-wrap");

            receiverSelect = document.createElement("select");
            receiverSelect.id = "receiverName";
            receiverSelect.classList.add("rec-input");
            receiverSelect.addEventListener("change", () => {
                const custom = receiverSelect.value === CUSTOM_RECEIVER_OPTION;
                receiverCustomInput.style.display = custom ? "block" : "none";
                if (custom) receiverCustomInput.focus();
            });

            receiverCustomInput = document.createElement("input");
            receiverCustomInput.type = "text";
            receiverCustomInput.id = "receiverCustomName";
            receiverCustomInput.placeholder = "Custom name";
            receiverCustomInput.classList.add("rec-input");
            receiverCustomInput.style.display = "none";

            entityInput.appendChild(receiverSelect);
            entityInput.appendChild(receiverCustomInput);
            document.body.appendChild(entityInput);
            // Populate only on creation; the session-start populate happens
            // in the Place Receiver activation. Repopulating on every canvas
            // click would wipe the user's pick while repositioning.
            populateReceiverSelect();
        }

        const css = cssFromWorld(pos.x + canvas.width * 0.02, pos.y);
        entityInput.style.left = `${css.left + 8}px`;
        entityInput.style.top = `${css.top - 16}px`;
        entityInput.style.display = "flex";
        entityInput.style.position = "absolute";
    }

    // =================================================================
    // Move existing receivers by dragging them on the map
    // =================================================================

    let dragReceiverRef = null;
    let dragOffset = null;
    let dragMoved = false;
    let panState = null;
    let clickCandidate = null;
    let focusedReceiver = null;
    const moveToggle = document.getElementById("moveToggle");
    const viewReset = document.getElementById("viewReset");

    function drawToolActive() {
        return drawAreaButton.dataset.active === 'true'
            || drawSubZoneButton.dataset.active === 'true'
            || addDeviceButton.dataset.active === 'true'
            || SetScaleButton.dataset.active === 'true';
    }

    function hitReceiverAt(pos) {
        const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
        if (!floor) return null;
        const hitRadius = canvas.width * 0.02 + 10; // icon radius plus slack
        return (floor.receivers || []).find(r =>
            r.cords && Math.hypot(r.cords.x - pos.x, r.cords.y - pos.y) <= hitRadius) || null;
    }

    function endReceiverDrag() {
        if (!dragReceiverRef) return;
        dragReceiverRef = null;
        dragOffset = null;
        if (dragMoved) {
            savebuttondiv.appendChild(saveButton);
        }
        dragMoved = false;
    }

    canvas.addEventListener("mousedown", (event) => {
        if (drawToolActive() || dragReceiverRef || panState) return;
        if (!mapReady()) return;
        const pos = zoneMousePos(event);
        const hit = hitReceiverAt(pos);
        if (moveToggle.checked && !pollTrackActive && hit) {
            // Grab offset: the receiver moves with the cursor instead of
            // teleporting its center onto it.
            dragReceiverRef = hit;
            dragOffset = { x: hit.cords.x - pos.x, y: hit.cords.y - pos.y };
            dragMoved = false;
            return;
        }
        panState = {
            startX: event.clientX,
            startY: event.clientY,
            viewX: view.x,
            viewY: view.y,
            moved: false,
        };
        clickCandidate = hit;
    });

    canvas.addEventListener("mousemove", (event) => {
        if (dragReceiverRef) {
            if (event.buttons === 0) {
                // The button was released outside the canvas/iframe.
                endReceiverDrag();
                return;
            }
            const pos = zoneMousePos(event);
            dragReceiverRef.cords.x = Math.max(0, Math.min(canvas.width, pos.x + dragOffset.x));
            dragReceiverRef.cords.y = Math.max(0, Math.min(canvas.height, pos.y + dragOffset.y));
            dragMoved = true;
            redrawAll();
            return;
        }
        if (panState) {
            if (event.buttons === 0) {
                panState = null;
                return;
            }
            const rect = canvas.getBoundingClientRect();
            const dx = (event.clientX - panState.startX) * (canvas.width / rect.width);
            const dy = (event.clientY - panState.startY) * (canvas.height / rect.height);
            if (Math.abs(dx) + Math.abs(dy) > 4) panState.moved = true;
            view.x = panState.viewX + dx;
            view.y = panState.viewY + dy;
            clampView();
            redrawAll();
        }
    });

    document.addEventListener("mouseup", () => {
        endReceiverDrag();
        if (!panState) return;
        const wasClick = !panState.moved;
        panState = null;
        if (!wasClick) return;
        // A plain click outside move mode: focus a receiver (only it and its
        // circle stay on the map); click it again or click empty space to
        // show everything.
        const target = clickCandidate;
        clickCandidate = null;
        const next = target && target.entity_id !== focusedReceiver ? target.entity_id : null;
        if (next !== focusedReceiver) {
            focusedReceiver = next;
            redrawAll();
        }
    });

    canvas.addEventListener("wheel", (event) => {
        if (!mapReady()) return;
        event.preventDefault();
        if (drawToolActive()) return; // mid-tool zoom would wipe the overlay
        const rect = canvas.getBoundingClientRect();
        const px = (event.clientX - rect.left) * (canvas.width / rect.width);
        const py = (event.clientY - rect.top) * (canvas.height / rect.height);
        const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
        const newZoom = Math.max(1, Math.min(8, view.zoom * factor));
        // Keep the world point under the cursor fixed while zooming.
        view.x = px - ((px - view.x) / view.zoom) * newZoom;
        view.y = py - ((py - view.y) / view.zoom) * newZoom;
        view.zoom = newZoom;
        clampView();
        redrawAll();
    }, { passive: false });

    viewReset.addEventListener("click", () => {
        view.zoom = 1;
        view.x = 0;
        view.y = 0;
        if (mapReady()) redrawAll();
    });

    // =================================================================
    // Add data to array
    // =================================================================

    function addDataToFloor(finalcords, floorName, dataType, data) {
        // Check if floor is arratm else initiate it
        if (!Array.isArray(finalcords.floor)) {
            finalcords.floor = [];
        }
        
        let floorExists = finalcords.floor.some(floor => sameFloorName(floor.name, floorName)); // Check if floor exists

        if (!floorExists) {
            // Add floor if it does not exists
            finalcords.floor.push({
            name: floorName,
            scale: null,
            receivers: [],
            zones: []
            });
            console.log(`Added new floor: ${floorName}`);
        } else {
            console.log(`Floor '${floorName}' already exists.`);
        }    
        
        let floor = finalcords.floor.find(floor => sameFloorName(floor.name, floorName)); // Find correct floor

        if (floor) {
            // Control if receiver/zone with the name already exists on the floor
            let enitityExists = null;
            let tmpname = null;
            if(dataType === "receivers"){
                enitityExists = floor[dataType].some(receiver => receiver.entity_id === receiverName);
                tmpname = receiverName;
            }
            if(dataType === "zones"){
                // Allow multiple zones with the same display name.
                enitityExists = false;
                tmpname = zoneName;
            }
            if(dataType === "scale"){
                floor.scale = data;
                savebuttondiv.appendChild(saveButton);
                return true;
            }

            if (!enitityExists) {
                // Add new receiver if it does not exist
                floor[dataType].push(data);
                savebuttondiv.appendChild(saveButton);
                return true;
              } else {
                console.log(`'${dataType}' with the name '${tmpname}' already exists on ${floorName}.`);
                alert(`'${dataType}' with the name '${tmpname}' already exists on ${floorName}.`);
                buttonreset();
                clearCanvas();
                drawElements();
                return false;
              }
        } else {
            console.log(`Floor with name '${floorName}' not found.`);
            return false;
        }
      }

    // =================================================================
    // Draw elements on canvas
    // =================================================================

    function scaleStatus(value){
        if(value == null){
            document.getElementById("scalenok").style.display = "flex";
            document.getElementById("scaleok").style.display = "none";
        } else {
            document.getElementById("scalenok").style.display = "none";
            document.getElementById("scaleok").style.display = "flex";
        }
    }

    // Text with a light halo, horizontally centered on cx and clamped to the
    // canvas so labels stay fully readable at the edges.
    function drawCenteredLabel(text, cx, baselineY, font, color) {
        if (!text) return;
        ctx.font = font;
        const width = ctx.measureText(text).width;
        let lx = cx - width / 2;
        lx = Math.max(4, Math.min(canvas.width - width - 4, lx));
        ctx.lineJoin = "round";
        ctx.lineWidth = 5;
        ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
        ctx.strokeText(text, lx, baselineY);
        ctx.fillStyle = color;
        ctx.fillText(text, lx, baselineY);
    }

    function drawElements(xp, yp, type){
        const tmpdrawcords = [];
        const iconSize = canvas.width * 0.04; // Adjust size as needed
        deleteButton.remove();
        drawGrid();

        // Only a placement click (placeReceiver passes world coordinates) may
        // update the pending receiver coordinates; plain redraws (grid
        // toggle, sidebar deletes) must not clobber them.
        if (xp !== undefined && yp !== undefined) {
            tmpcords = { x: xp, y: yp };
        }
        // Keep the pending (unsaved) receiver marker visible across repaints
        // while Place Receiver mode is active.
        if (addDeviceButton.dataset.active === 'true' && tmpcords
            && Number.isFinite(tmpcords.x) && Number.isFinite(tmpcords.y)) {
            tmpdrawcords.push({
                entity_id: receiverName,
                type: "receiver",
                cords: tmpcords,
            });
        }

        let floor = finalcords.floor.find(floor => sameFloorName(floor.name, SelMapName)); //Add all existing

        // A focused receiver that no longer exists releases the focus.
        if (focusedReceiver && !(floor && (floor.receivers || []).some(r => r.entity_id === focusedReceiver))) {
            focusedReceiver = null;
        }

        if (floor) {
            myScaleVal = floor.scale; // Get the scalevalue for the floor
            scaleStatus(myScaleVal)//Show or hide status for scale value
            savebuttondiv.appendChild(deleteButton); //If there is data add the delete button to be able to delete the floor.

            if (floor.receivers.length < 3) {
                trackdiv.style.display = "none";
            } else {
                trackdiv.style.display = "";
            }

            // Loopa through all receivers in floor
            floor.receivers.forEach((receiver, index) => {
                receiver.type = "receiver";
                tmpdrawcords.push(receiver);
            });
            // Loopa through all zones in floor
            floor.zones.forEach((zone, index) => {
                if (!zone.zone_id) {
                    zone.zone_id = createZoneId();
                }
                zone.type = "zone";
                tmpdrawcords.push(zone);
            });
            // Sub-zones are pushed after zones so they paint on top of them.
            (floor.subzones || []).forEach(sub => {
                if (!sub.sub_zone_id) {
                    sub.sub_zone_id = createSubZoneId();
                }
                sub.type = "subzone";
                tmpdrawcords.push(sub);
            });
        } else {
            // No saved floor matches the selection: tracking has nothing to
            // iterate, so do not offer it.
            trackdiv.style.display = "none";
        }

        tmpdrawcords.forEach((item, index) => {

            if (item.type == "receiver"){
                if (focusedReceiver && item.entity_id !== focusedReceiver) return;
                const x = item.cords.x;
                const y = item.cords.y;
                drawReceiverIcon(x, y, iconSize);

                // Name centered above the icon; below it when too close to
                // the top edge.
                let labelY = y - iconSize / 2 - 8;
                if (labelY < 24) {
                    labelY = y + iconSize / 2 + 24;
                }
                drawCenteredLabel(item.entity_id, x, labelY, "600 22px system-ui, sans-serif", "#111111");
            }
            if (item.type == "zone"){
                const pts = zonePerimeterPoints(item);
                if (pts.length < 3) return;

                // Draw polygon
                ctx.beginPath();
                ctx.moveTo(pts[0].x, pts[0].y);
                for (let p = 1; p < pts.length; p++) {
                    ctx.lineTo(pts[p].x, pts[p].y);
                }
                ctx.closePath();
                ctx.strokeStyle = "red";
                ctx.lineWidth = 2;
                ctx.stroke();

                // Zone name centered in the room
                const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
                const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
                drawCenteredLabel(item.entity_id, cx, cy + 8, "600 24px system-ui, sans-serif", "#d32f2f");
            }
            if (item.type == "subzone"){
                const pts = item.cords || [];
                if (pts.length < 3) return;
                const color = item.color || "#3f51b5";
                ctx.beginPath();
                ctx.moveTo(pts[0].x, pts[0].y);
                for (let p = 1; p < pts.length; p++) {
                    ctx.lineTo(pts[p].x, pts[p].y);
                }
                ctx.closePath();
                ctx.save();
                ctx.globalAlpha = 0.18;
                ctx.fillStyle = color;
                ctx.fill();
                ctx.restore();
                ctx.strokeStyle = color;
                ctx.lineWidth = 2;
                ctx.stroke();

                const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
                const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
                drawCenteredLabel(item.entity_id, cx, cy + 6, "600 18px system-ui, sans-serif", color);
            }
        });

        renderEntityTree(floor);
    }

    // Sidebar: one section per zone with the receivers placed inside it.
    function renderEntityTree(floor) {
        const tree = document.getElementById("entitytree");
        if (!floor) {
            tree.innerHTML = '<p class="bps-empty">Select a floor to see its zones and receivers.</p>';
            return;
        }
        const trashSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"></path><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"></path></svg>';
        const pencilSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>';
        const subzones = (floor.subzones || []);
        const subRow = s => {
            const sid = s.sub_zone_id || s.entity_id;
            return `<li class="bps-subzone-row"><span class="bps-subzone-dot" style="background:${escHtml(s.color || '#3f51b5')}"></span>`
                + `<span title="${escHtml(s.entity_id)}">${escHtml(s.entity_id)}</span>`
                + `<button class="bps-icon-btn" title="Edit sub-zone" data-type="editsubzone" data-id="${escHtml(sid)}">${pencilSvg}</button>`
                + `<button class="bps-icon-btn" title="Remove sub-zone" data-type="removesubzone" data-id="${escHtml(sid)}">${trashSvg}</button></li>`;
        };
        const receiverRow = r =>
            `<li><span title="${escHtml(r.entity_id)}">${escHtml(r.entity_id)}</span>`
            + `<button class="bps-icon-btn" title="Remove receiver" data-type="removerec" data-id="${escHtml(r.entity_id)}">${trashSvg}</button></li>`;

        const receivers = (floor.receivers || []).filter(r => r && r.entity_id && r.cords);
        const claimed = new Set();
        let html = "";
        (floor.zones || []).forEach(zone => {
            const pts = zonePerimeterPoints(zone);
            const inside = pts.length >= 3
                ? receivers.filter(r => !claimed.has(r.entity_id) && pointInPolygon(r.cords.x, r.cords.y, pts))
                : [];
            inside.forEach(r => claimed.add(r.entity_id));
            const zoneDomId = zone.zone_id || zone.entity_id;
            const subs = subzones.filter(s => s.parent === zoneDomId);
            html += '<div class="bps-zone-group">'
                + `<div class="bps-zone-head"><span title="${escHtml(zone.entity_id)}">${escHtml(zone.entity_id)}<span class="bps-count"> · ${inside.length}</span></span>`
                + '<span class="bps-zone-actions">'
                + `<button class="bps-icon-btn" title="Edit zone" data-type="editzone" data-id="${escHtml(zoneDomId)}">${pencilSvg}</button>`
                + `<button class="bps-icon-btn" title="Remove zone" data-type="removezone" data-id="${escHtml(zoneDomId)}">${trashSvg}</button>`
                + '</span></div>';
            if (inside.length) {
                html += "<ul>" + inside.map(receiverRow).join("") + "</ul>";
            }
            if (subs.length) {
                html += '<ul class="bps-subzone-list">' + subs.map(subRow).join("") + "</ul>";
            }
            html += "</div>";
        });
        // Sub-zones whose parent zone no longer exists — still listed so they
        // can be edited or deleted.
        const zoneIds = new Set((floor.zones || []).map(z => z.zone_id || z.entity_id));
        const orphanSubs = subzones.filter(s => !zoneIds.has(s.parent));
        if (orphanSubs.length) {
            html += '<div class="bps-zone-group">'
                + `<div class="bps-zone-head"><span>Unlinked sub-zones<span class="bps-count"> · ${orphanSubs.length}</span></span></div>`
                + '<ul class="bps-subzone-list">' + orphanSubs.map(subRow).join("") + "</ul></div>";
        }
        const unzoned = receivers.filter(r => !claimed.has(r.entity_id));
        if (unzoned.length) {
            html += '<div class="bps-zone-group">'
                + `<div class="bps-zone-head"><span>No zone<span class="bps-count"> · ${unzoned.length}</span></span></div>`
                + "<ul>" + unzoned.map(receiverRow).join("") + "</ul></div>";
        }
        tree.innerHTML = html || '<p class="bps-empty">No zones or receivers on this floor yet.</p>';
    }

    // =================================================================
    // Trilateration distance circles toggle
    // =================================================================

    const circleToggle = document.getElementById("circleToggle");
    circleToggle.checked = localStorage.getItem("bpsCircles") === "on"; // off by default
    circleToggle.addEventListener("change", () => {
        localStorage.setItem("bpsCircles", circleToggle.checked ? "on" : "off");
        // Re-tint the receiver icons right away when not mid-tracking.
        if (!pollTrackActive && img.naturalWidth > 0) {
            clearCanvas();
            drawElements();
        }
    });

    // =================================================================
    // Distance grid overlay (toggleable, meters or feet)
    // =================================================================

    let gridUnit = localStorage.getItem("bpsGridUnit") || "off"; // off | m | ft
    const gridToggle = document.getElementById("gridToggle");

    function updateGridButton() {
        gridToggle.textContent = gridUnit === "off" ? "Grid: off"
            : gridUnit === "m" ? "Grid: meters" : "Grid: feet";
    }
    updateGridButton();

    gridToggle.addEventListener("click", () => {
        gridUnit = gridUnit === "off" ? "m" : gridUnit === "m" ? "ft" : "off";
        localStorage.setItem("bpsGridUnit", gridUnit);
        updateGridButton();
        // Redraw only when a floor plan is actually loaded; the preference
        // itself always cycles and persists.
        if (img.naturalWidth > 0) {
            clearCanvas();
            drawElements();
        }
    });

    function drawGrid() {
        if (gridUnit === "off") return;
        const floor = finalcords.floor.find(f => sameFloorName(f.name, SelMapName));
        const scale = floor && floor.scale; // pixels per meter
        if (!scale) return;
        const unitPx = gridUnit === "m" ? scale : scale * 0.3048;
        // Thin the grid out when single units would be too dense on screen
        // at the current zoom level.
        let step = unitPx;
        let unitsPerLine = 1;
        while (step * view.zoom < 45) {
            step += unitPx;
            unitsPerLine += 1;
        }
        // Labels pinned to the visible edges, constant size on screen.
        const topWorld = -view.y / view.zoom;
        const leftWorld = -view.x / view.zoom;
        ctx.save();
        ctx.strokeStyle = "rgba(30, 60, 120, 0.18)";
        ctx.lineWidth = 1 / view.zoom;
        ctx.fillStyle = "rgba(30, 60, 120, 0.75)";
        ctx.font = `${14 / view.zoom}px system-ui, sans-serif`;
        for (let gx = step, i = unitsPerLine; gx < canvas.width; gx += step, i += unitsPerLine) {
            ctx.beginPath();
            ctx.moveTo(gx, 0);
            ctx.lineTo(gx, canvas.height);
            ctx.stroke();
            ctx.fillText(`${i}${gridUnit}`, gx + 3 / view.zoom, topWorld + 16 / view.zoom);
        }
        for (let gy = step, i = unitsPerLine; gy < canvas.height; gy += step, i += unitsPerLine) {
            ctx.beginPath();
            ctx.moveTo(0, gy);
            ctx.lineTo(canvas.width, gy);
            ctx.stroke();
            ctx.fillText(`${i}${gridUnit}`, leftWorld + 3 / view.zoom, gy - 4 / view.zoom);
        }
        ctx.restore();
    }

    // Display selected map
    async function selectExistingMap(value) {
        // Abandon any in-progress draw/edit so its handlers and half-built
        // polygon can't bleed onto the newly-selected floor.
        removeListeners();
        buttonreset();
        zonePoints = [];
        selectedVertex = null;
        draggingZone = false;
        editTarget = null;
        img.src = `/local/bps_maps/${value}`;
        imgfilename = value;
        mapname.value = removeExtension(value);
        SelMapName = mapname.value;
        await setupCanvasWithImage(img, canvas);
        new_floor = false;
        drawElements();
    }

    mapSelector.addEventListener('change', async () => {
        if (!mapSelector.value) return;
        await selectExistingMap(mapSelector.value);
    });

    upload.addEventListener('change', event => {
        const file = event.target.files[0];
        if (!file) return;

        // A new image invalidates any running tracking session (it iterates
        // the selected floor's receivers, which are about to change).
        stoptrackstat = true;

        // Switch the selection to the new floor right away. Leaving the
        // previous floor selected kept ITS receivers listed in the sidebar
        // under the new image, where deleting them silently edited that floor.
        mapname.value = removeExtension(file.name);
        SelMapName = mapname.value;

        const reader = new FileReader();
        reader.onload = function () {
            img.src = reader.result;
            setupCanvasWithImage(img, canvas).then(() => {
                drawElements();
            });
        };
        reader.readAsDataURL(file);
        new_floor = true;
    });

    function setupCanvasWithImage(img, canvas) {
        return new Promise((resolve) => {
            const ctx = canvas.getContext('2d');
            
            img.onload = () => {
                setupImageSize(img, canvas);
                resolve(); // Resolve when completed
            };
            img.onerror = () => {
                // Settle the promise so callers (including the startup
                // auto-open) continue instead of hanging forever.
                console.error(`Failed to load floor image: ${img.src}`);
                messdiv.innerHTML = '<p class="text-sm text-gray-500">Could not load the floor image for this map. Re-select it or upload a new image.</p>';
                resolve();
            };
    
            // Add the buttons
            drawAreaButton.className = 'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&amp;_svg]:pointer-events-none [&amp;_svg]:size-4 [&amp;_svg]:shrink-0 border border-input bg-background hover:bg-accent hover:text-accent-foreground h-10 px-4 py-2';
            drawAreaButton.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-pencil w-4 h-4 mr-2" data-component-name="Pencil"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path><path d="m15 5 4 4"></path></svg>
                    Draw Zone
                `;
            drawAreaButton.setAttribute('data-active', 'false');

            drawSubZoneButton.className = drawAreaButton.className;
            drawSubZoneButton.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-box-select w-4 h-4 mr-2" data-component-name="SubZone"><rect x="3" y="3" width="18" height="18" rx="2"></rect><rect x="8" y="8" width="8" height="8" rx="1"></rect></svg>
                    Draw Sub-Zone
                `;
            drawSubZoneButton.setAttribute('data-active', 'false');

            addDeviceButton.className = 'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0 border border-input bg-background hover:bg-accent hover:text-accent-foreground h-10 px-4 py-2';
            addDeviceButton.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-radio w-4 h-4 mr-2" data-component-name="Radio"><path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"></path><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"></path><circle cx="12" cy="12" r="2"></circle><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"></path><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"></path></svg>
                    Place Receiver
                `;
            addDeviceButton.setAttribute('data-active', 'false');

            SetScaleButton.className = 'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0 border border-input bg-background hover:bg-accent hover:text-accent-foreground h-10 px-4 py-2';
            SetScaleButton.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-ruler w-4 h-4 mr-2" data-component-name="Ruler"><path d="M21.3 15.3a2.4 2.4 0 0 1 0 3.4l-2.6 2.6a2.4 2.4 0 0 1-3.4 0L2.7 8.7a2.41 2.41 0 0 1 0-3.4l2.6-2.6a2.41 2.41 0 0 1 3.4 0Z"></path><path d="m14.5 12.5 2-2"></path><path d="m11.5 9.5 2-2"></path><path d="m8.5 6.5 2-2"></path><path d="m17.5 15.5 2-2"></path></svg>
                    Set Scale
                `;
            SetScaleButton.setAttribute('data-active', 'false');
            
            clearCanvasButton.className = 'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0 border border-input bg-background hover:bg-accent hover:text-accent-foreground h-10 px-4 py-2';
            clearCanvasButton.innerHTML = `
                <svg width="24px" height="24px" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M9 9L15 15" stroke="#000000" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M15 9L9 15" stroke="#000000" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="12" cy="12" r="9" stroke="#000000" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    Clear Canvas
                `;
            
            saveButton.className = 'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&amp;_svg]:pointer-events-none [&amp;_svg]:size-4 [&amp;_svg]:shrink-0 bg-primary text-primary-foreground hover:bg-primary/90 h-10 px-4 py-2';
            saveButton.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-save w-4 h-4 mr-2" data-component-name="Save"><path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"></path><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"></path><path d="M7 3v4a1 1 0 0 0 1 1h7"></path></svg>
                    Save Floor Plan
                `;
            
            mapbuttondiv.appendChild(addDeviceButton);
            mapbuttondiv.appendChild(drawAreaButton);
            mapbuttondiv.appendChild(drawSubZoneButton);
            mapbuttondiv.appendChild(SetScaleButton);
            mapbuttondiv.appendChild(clearCanvasButton);
        });
    }


    function setupImageSize(img, canvas, fixedWidth = 2000) {
        const ctx = canvas.getContext('2d');
    
        const imgratio = img.height / img.width;
        const newwidth = fixedWidth; // Fixed width in pixels
        const newheight = newwidth * imgratio; // Height based on aspect ratio
    
        // Update canvas size (this also resets the canvas transform)
        canvas.width = newwidth;
        canvas.height = newheight;

        // A new floor image starts from the default viewport.
        view.zoom = 1;
        view.x = 0;
        view.y = 0;
    
        // Draw image on canvas
        ctx.drawImage(img, 0, 0, newwidth, newheight);
    }
    

    function removeExtension(fileName) {
        const lastDotIndex = fileName.lastIndexOf('.');
        if (lastDotIndex === -1) {
            return fileName;
        }
        return fileName.substring(0, lastDotIndex);
    }

    // Save data
    saveButton.addEventListener('click', async () => {
        let saveresult = await savedata();
        if(saveresult){
            tmpfinalcords = finalcords;
            saveButton.remove();
            alert('Saved successfully!');
            getSavedMaps();
        }
    });

    //When clicking the delete button, remove the floor and reset the canvas.
    deleteButton.addEventListener("click", async function () {
        const userConfirmed = confirm("Are you sure you want to remove the floor named "+SelMapName+"?");
        if (!userConfirmed) {
            alert("Action canceled. No changes were made.");
            return;
        }

        // Keep the removed entries so a failed save can restore them.
        const removedFloors = finalcords.floor.filter(floor => sameFloorName(floor.name, SelMapName));
        finalcords.floor = finalcords.floor.filter(floor => !sameFloorName(floor.name, SelMapName));

        // Remove the map image belonging to the floor being deleted — not
        // whichever file the dropdown happened to point at last.
        const mapFile = Array.from(mapSelector.options)
            .map(option => option.value)
            .find(value => sameFloorName(removeExtension(value), SelMapName));
        imgfilename = mapFile || "";
        removefile = Boolean(mapFile);

        // A delete is never a new-floor save: without this, savedata would
        // suppress the 'remove' field and upload any pending image instead.
        new_floor = false;
        upload.value = "";

        // Deleting must not be blocked by the scale requirement of normal saves.
        let saveresult = false;
        try {
            saveresult = await savedata(true);
        } finally {
            if (!saveresult) {
                finalcords.floor = finalcords.floor.concat(removedFloors); //If not able to delete, restore the array
            }
            removefile = false;
        }
        if(saveresult){
            alert("The floor named "+SelMapName+" has been removed!");
            console.log("Updated data:", finalcords); // Control the updated data
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            mapname.value = "";
            getSavedMaps();
        }
    });

    async function savedata(skipScaleCheck = false){
        if(!skipScaleCheck && myScaleVal == null){
            alert("You have not added a scale, it won't work without it!");
            return;
        }

        removeListeners();
        const data = new FormData();
        data.append('coordinates', JSON.stringify(finalcords)); 
        data.append('new_floor', new_floor);

        if(removefile === true && new_floor === false){
            data.append('remove', imgfilename);
        }

        if(new_floor){ // Add filedata to variable 'data' if there is a new floor
            const file = upload.files[0];
            if (!file) {
                alert("No floor image is selected — please choose the floor image again.");
                return;
            }
            const extension = file.name.substring(file.name.lastIndexOf('.')); // Get the old file ending
            // Reuse the exact name of an already saved map for this floor so
            // re-uploads overwrite it instead of forking a case-variant file.
            const existingMap = Array.from(mapSelector.options)
                .map(option => option.value)
                .find(value => value && sameFloorName(removeExtension(value), SelMapName));
            const newFileName = existingMap
                ? removeExtension(existingMap) + extension
                : `${SelMapName}${extension}`; // Build the new filename
            const renamedFile = new File([file], newFileName, { type: file.type });

            if (renamedFile) {
                data.append('file', renamedFile);
            } else {
                console.log("No file uploaded.");
            }
        }

        try {
            const response = await fetch('/api/bps/save_text', {
                method: 'POST',
                body: data,
            });
            if (response.ok) {
                drawAreaButton.remove();
                addDeviceButton.remove();
                clearCanvasButton.remove();
                SetScaleButton.remove();
                saveButton.remove();
                new_floor = false;
                return true;
            } else {
                alert('Error saving data!');
            }
        } catch (error) {
            console.error('Error saving data:', error);
            alert('Error saving data!');
        }
    }

    // =================================================================
    // Receiver calibration
    // =================================================================

    const calibStart = document.getElementById('calibStart');
    const calibCancel = document.getElementById('calibCancel');
    const calibApply = document.getElementById('calibApply');
    const calibReset = document.getElementById('calibReset');
    const calibStatus = document.getElementById('calibStatus');
    const calibResults = document.getElementById('calibResults');
    const calibDuration = document.getElementById('calibDuration');
    const calibAuto = document.getElementById('calibAuto');
    const calibManualControls = document.getElementById('calibManualControls');
    let calibTimer = null;
    let calibLastResults = {};

    function selectedFloorResult(results) {
        for (const name of Object.keys(results || {})) {
            if (sameFloorName(name, mapname.value)) return results[name];
        }
        return null;
    }

    async function calibRequest(body) {
        const res = await fetch('/api/bps/calibration', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Calibration request failed (${res.status})`);
        return data;
    }

    function renderCalibrationResult(result) {
        calibStatus.textContent =
            `Floor ${result.floor}: ${result.pairs_used} pairs (${result.bidirectional_pairs} bidirectional), ` +
            `typical error ×${result.error_factor_before} → ×${result.error_factor_after} predicted after correction.`;

        const slugs = Object.keys(result.receivers).sort();
        const cells = {};
        (result.matrix || []).forEach(m => { cells[`${m.tx}|${m.rx}`] = m; });
        const lowConfidence = result.low_confidence || [];

        let html = '<table style="border-collapse:collapse; font-size:11px; margin-top:8px">'
            + '<tr><th style="text-align:left; padding:2px 6px">tx \\ rx</th>';
        slugs.forEach(s => {
            html += `<th style="padding:2px 3px"><div style="writing-mode:vertical-rl; transform:rotate(180deg); max-height:140px; overflow:hidden">${escHtml(s)}</div></th>`;
        });
        html += '<th style="padding:2px 6px">correction</th></tr>';
        slugs.forEach(tx => {
            html += `<tr><td style="padding:2px 6px; white-space:nowrap">${escHtml(tx)}${lowConfidence.includes(tx) ? ' ⚠' : ''}</td>`;
            slugs.forEach(rx => {
                const m = cells[`${tx}|${rx}`];
                if (!m) {
                    html += '<td style="background:#eee"></td>';
                    return;
                }
                // Blue = measuring short, green = accurate, red = measuring long.
                const p = Math.max(-1, Math.min(1, m.error_pct / 100));
                const hue = 240 - (p + 1) * 120;
                html += `<td title="true ${m.true_m} m · measured ${m.measured_m} m · corrected ${m.corrected_m} m · ${m.samples} samples" `
                    + `style="background:hsl(${hue},55%,55%); color:#fff; text-align:center; padding:2px 4px">`
                    + `${m.error_pct > 0 ? '+' : ''}${Math.round(m.error_pct)}%</td>`;
            });
            html += `<td style="text-align:center; padding:2px 6px">×${result.receivers[tx]}</td></tr>`;
        });
        html += '</table>';
        if (lowConfidence.length) {
            html += `<p class="text-sm text-gray-500" style="margin-top:6px">⚠ Low confidence (aggressive correction or few pairs): ${escHtml(lowConfidence.join(', '))} — verify those receivers before applying.</p>`;
        }
        calibResults.innerHTML = html;
    }

    function renderCalibration(status) {
        const auto = status.mode === 'auto';
        const manualSampling = status.mode === 'manual' && status.state === 'sampling';
        calibLastResults = status.results || {};
        calibAuto.checked = auto;
        calibManualControls.style.display = auto ? 'none' : '';
        calibStart.style.display = manualSampling ? 'none' : '';
        calibCancel.style.display = manualSampling ? '' : 'none';
        const result = selectedFloorResult(calibLastResults);
        // Auto mode applies by itself; the button is for manual runs only.
        calibApply.style.display = !auto && result ? '' : 'none';

        const pairs = Object.keys(status.pair_counts || {}).length;
        if (auto) {
            if (result) renderCalibrationResult(result);
            else calibResults.innerHTML = '';
            // Set the status line AFTER rendering the result, which writes its
            // own summary into it; the auto line carries the result summary too.
            let line = `Auto calibration running · ${pairs} receiver pairs in the window`;
            if (status.error) line += ` · ${status.error}`;
            else if (status.last_solved_at) line += ` · last solve ${status.last_solved_at}`;
            else line += ' · first solve after a few minutes of data';
            if (result) {
                line += ` · floor ${result.floor}: ${result.pairs_used} pairs used, `
                    + `typical error ×${result.error_factor_before} → ×${result.error_factor_after}`;
            }
            calibStatus.textContent = line;
            return;
        }
        if (manualSampling) {
            const total = status.duration || 1;
            const left = status.seconds_left || 0;
            const done = total - left;
            calibStatus.textContent =
                `Sampling floor ${status.floor}… ${Math.floor(done / 60)}:${String(done % 60).padStart(2, '0')}`
                + ` of ${Math.floor(total / 60)} min · ${pairs} receiver pairs heard so far.`;
            return;
        }
        if (status.state === 'error') {
            calibStatus.textContent = `Calibration failed: ${status.error}`;
            return;
        }
        if (result) {
            renderCalibrationResult(result);
            return;
        }
        calibStatus.textContent = 'Idle. Select a floor, then start a run — or enable auto calibration.';
        calibResults.innerHTML = '';
    }

    async function pollCalibration() {
        try {
            const res = await fetch('/api/bps/calibration');
            if (!res.ok) return;
            const status = await res.json();
            renderCalibration(status);
            const wantInterval = status.mode === 'auto' ? 10000
                : status.state === 'sampling' ? 3000 : 0;
            if (calibTimer) { clearInterval(calibTimer); calibTimer = null; }
            if (wantInterval) calibTimer = setInterval(pollCalibration, wantInterval);
        } catch (e) {
            console.warn('Calibration status:', e);
        }
    }

    calibAuto.addEventListener('change', async () => {
        try {
            renderCalibration(await calibRequest({ action: 'auto', enabled: calibAuto.checked }));
            pollCalibration();
        } catch (e) {
            calibAuto.checked = !calibAuto.checked;
            alert(e.message);
        }
    });

    // Switching floors should switch the displayed matrix too.
    mapSelector.addEventListener('change', () => {
        const result = selectedFloorResult(calibLastResults);
        if (result) renderCalibrationResult(result);
        else calibResults.innerHTML = '';
    });

    calibStart.addEventListener('click', async () => {
        if (!mapname.value) {
            alert('Select a floor first.');
            return;
        }
        calibResults.innerHTML = '';
        try {
            renderCalibration(await calibRequest({
                action: 'start',
                floor: mapname.value,
                duration: Number(calibDuration.value),
            }));
            if (!calibTimer) calibTimer = setInterval(pollCalibration, 3000);
        } catch (e) {
            alert(e.message);
        }
    });

    calibCancel.addEventListener('click', async () => {
        try {
            renderCalibration(await calibRequest({ action: 'cancel' }));
        } catch (e) {
            alert(e.message);
        }
    });

    calibApply.addEventListener('click', async () => {
        try {
            const data = await calibRequest({ action: 'apply', floor: mapname.value });
            alert(`Corrections applied to ${data.applied} receiver(s).`);
            // The backend edited bpsdata.txt; reload so a later panel save
            // does not overwrite the corrections with stale data.
            await fetchBPSData();
        } catch (e) {
            alert(e.message);
        }
    });

    calibReset.addEventListener('click', async () => {
        if (!mapname.value) {
            alert('Select a floor first.');
            return;
        }
        const warning = calibAuto.checked
            ? `Remove calibration corrections from floor "${mapname.value}"? Auto calibration is on and will learn and re-apply them again.`
            : `Remove calibration corrections from floor "${mapname.value}"?`;
        if (!confirm(warning)) return;
        try {
            const data = await calibRequest({ action: 'reset', floor: mapname.value });
            alert(`Corrections removed from ${data.reset} receiver(s).`);
            await fetchBPSData();
        } catch (e) {
            alert(e.message);
        }
    });

    pollCalibration();

    // Tab switching: show one panel at a time. The Map tab is active in the
    // markup so the canvas is visible (and sized) on load; the calibration
    // poll keeps running regardless of which tab is showing.
    const tabButtons = document.querySelectorAll(".bps-tab");
    const tabPanels = document.querySelectorAll(".bps-tabpanel");
    tabButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
            const name = btn.dataset.tab;
            tabButtons.forEach((b) => b.classList.toggle("active", b === btn));
            tabPanels.forEach((pnl) => pnl.classList.toggle("active", pnl.dataset.tabpanel === name));
            scheduleSidebarSync(); // sidebar becomes visible/hidden with the tab
        });
    });

    // Cap the Zones & Receivers sidebar at the bottom of the viewport so a long
    // receiver list scrolls inside it instead of running off-screen. In the
    // stacked (narrow) layout the sidebar isn't sticky, so leave it uncapped.
    let sidebarRaf = null;
    function syncSidebarHeight() {
        const sb = document.querySelector(".bps-sidebar");
        if (!sb) return;
        if (getComputedStyle(sb).position !== "sticky") {
            sb.style.maxHeight = "";
            return;
        }
        const top = sb.getBoundingClientRect().top;
        sb.style.maxHeight = Math.max(200, window.innerHeight - top - 12) + "px";
    }
    function scheduleSidebarSync() {
        if (sidebarRaf) return;
        sidebarRaf = requestAnimationFrame(() => { sidebarRaf = null; syncSidebarHeight(); });
    }
    window.addEventListener("resize", scheduleSidebarSync);
    window.addEventListener("scroll", scheduleSidebarSync, { passive: true });
    syncSidebarHeight();

    // Light / dark theme toggle. The `dark` class on <html> and <body> drives
    // the CSS variable set; without it the light (:root) variables apply.
    const themeToggle = document.getElementById("themeToggle");
    function applyTheme(theme) {
        const dark = theme !== "light";
        document.documentElement.classList.toggle("dark", dark);
        document.body.classList.toggle("dark", dark);
        if (themeToggle) {
            themeToggle.textContent = dark ? "🌙" : "☀️";
            themeToggle.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
        }
    }
    applyTheme(localStorage.getItem("bpsTheme") || "dark");
    if (themeToggle) {
        themeToggle.addEventListener("click", () => {
            const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
            localStorage.setItem("bpsTheme", next);
            applyTheme(next);
        });
    }

    // With a single configured floor there is nothing to choose: open it
    // right away. This must run LAST — drawElements touches state declared
    // throughout this closure, so everything has to be initialized first.
    if (tmpsaved) {
        const savedMaps = Array.from(mapSelector.options)
            .map(option => option.value)
            .filter(Boolean);
        if (savedMaps.length === 1) {
            mapSelector.value = savedMaps[0];
            await selectExistingMap(savedMaps[0]);
        }
    }

});
