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

    const newelement = `
                <ul class="space-y-2" id="idxxx">
                        <li class="flex items-center justify-between bg-gray-50 p-2 rounded">
                            <span class="text-sm truncate">typename</span>
                            <div class="flex gap-2">
                                <button data-type="removexxx" data-id="idxxx" class="inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&amp;_svg]:pointer-events-none [&amp;_svg]:size-4 [&amp;_svg]:shrink-0 hover:bg-accent hover:text-accent-foreground w-10">
                                    <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-trash w-4 h-4"><path d="M3 6h18"></path><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"></path></svg>
                                </button>
                            </div>
                        </li>
                    </ul>
                `;

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
            fetchBPSData();
        }

        let socket = null;
        let hassConnection = null;
        let hassAuth = null;
        let socketMessageHandler = null;
        let bpsSubscribeId = null;
        const tracked = [];
        let NewEnts = [];
        let socketIdCounter = 1; 

        async function getHAConnection() {
            if (hassConnection && hassConnection.socket) {
                return hassConnection;
            }

            const parentConnection = window.parent && window.parent.hassConnection;
            const localConnection = window.hassConnection;
            const connectionPromise = parentConnection || localConnection;

            if (!connectionPromise) {
                return null;
            }

            const connectionObject = await connectionPromise;
            hassAuth = connectionObject.auth || hassAuth;
            hassConnection = connectionObject.conn || connectionObject;

            if (!hassConnection || !hassConnection.socket) {
                return null;
            }

            return hassConnection;
        }

        async function getHassAccessToken() {
            if (!hassAuth) {
                return null;
            }

            if (hassAuth.data && hassAuth.data.access_token) {
                return hassAuth.data.access_token;
            }

            if (typeof hassAuth.accessToken === "function") {
                return await hassAuth.accessToken();
            }

            if (hassAuth.accessToken) {
                return hassAuth.accessToken;
            }

            return null;
        }

        function startTracking() {
            if (!checkCanvasImage()) return;
            if (!mapname.value) {
                alert("Please add or select a floor!");
                return;
            }
            if (socket){
                alert("Already active connection");
                return;
            }
            
            //Build the array with tracked devices
            if (device == ""){
                alert("You must choose a device to track!");
                return;
            }
            let floor = finalcords.floor.find(floor => sameFloorName(floor.name, SelMapName));
            if (!floor) {
                alert("No saved floor matches the current selection. Save the floor first.");
                return;
            }
            floor.receivers.forEach((entity, index) => {
                tracked.push(`${device}_distance_to_${entity.entity_id}`);
            });

            // Check if there are enough points for trilateration
            if (tracked.length < 3) {
                alert("At least three beacons are required for tracking.");
                return;
            }
    
            getHAConnection().then((conn) => {
                if (!conn) {
                    alert("Could not access Home Assistant connection.");
                    return;
                }

                getHassAccessToken().then((accessToken) => {
                    if (!accessToken) {
                        alert("Could not access Home Assistant token.");
                        return;
                    }

                    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
                    socket = new WebSocket(`${wsProtocol}//${window.location.host}/api/websocket`);
                    bpsSubscribeId = Date.now();
                    socketIdCounter = bpsSubscribeId;

                    socketMessageHandler = async (event) => {
                        let message = null;
                        try {
                            message = JSON.parse(event.data);
                        } catch (e) {
                            return;
                        }

                        if (message.type === "auth_required") {
                            socket.send(JSON.stringify({ type: "auth", access_token: accessToken }));
                            return;
                        }

                        if (message.type === "auth_ok") {
                            socket.send(JSON.stringify({
                                id: bpsSubscribeId,
                                type: "bps/subscribe",
                                entities: tracked,
                            }));
                            return;
                        }

                        if (message.type === "state_changed") {
                            await updateEntArray(message.entity_id, message.new_state);
                            socketIdCounter++;
                            const triData = NewEnts.map(item => item.cords);
                            socket.send(JSON.stringify({
                                id: socketIdCounter,
                                type: "bps/known_points",
                                knownPoints: triData,
                                tracker: device.replace("sensor.", ""),
                            }));
                        }

                        // Handle the response from knownPoints
                        if (message.type === "tri_result" && message.success) {
                            drawTracker(message.result);
                        } else if (message.type === "tri_result" && !message.success) {
                            console.log("Tri Error: "+message);
                        }

                        let current = false;
                        if (message.current_states && Array.isArray(message.current_states)) {
                            current = true;
                        } else {
                            current = false;
                        }

                        if (message.type === "result" && message.id === bpsSubscribeId && current) {
                            starttrackbtn.style.display = "none";
                            stoptrackbtn.style.display = "";
                            let floor = finalcords.floor.find(floor => sameFloorName(floor.name, SelMapName));
                            message.current_states.forEach((entity, index) => {
                                updateEntArray(entity.entity_id, entity.state);
                            });
                            console.log("Registered array");
                            console.log(NewEnts);
                        } else if (message.type === "result" && message.id === bpsSubscribeId && !message.success) {
                            console.log("Result Error: "+message);
                        }
                    };

                    socket.addEventListener("message", socketMessageHandler);
                });
            });

        }

        function stopTracking(){
            if (!checkCanvasImage()) return;
            if (!mapname.value) {
                alert("Please enter a floor name!");
                return;
            }
            if (!socket){
                alert("There is no active connection");
                return;
            }
            socketIdCounter++;
            socket.send(JSON.stringify({
                id: socketIdCounter, // Unique ID for this message
                type: "bps/unsubscribe",
                entities: tracked,
            }));
            if (socketMessageHandler) {
                socket.removeEventListener("message", socketMessageHandler);
                socketMessageHandler = null;
            }
            socket.close();
            socket = null;
            bpsSubscribeId = null;
            console.log(`Unsubscribed`);
            starttrackbtn.style.display = "";
            stoptrackbtn.style.display = "none";
        }

        let stoptrackstat = false;
        function startTrackfunc(){
            stoptrackstat = false;
            starttrackbtn.style.display = "none";
            stoptrackbtn.style.display = "";
            const interval = setInterval(async () => {
                if (stoptrackstat) {
                    clearInterval(interval);
                    stoptrackstat = false;
                    starttrackbtn.style.display = "";
                    stoptrackbtn.style.display = "none";
                    zonediv.style.display = "none";
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
                drawTracker(dt);
                zonediv.style.display = "";
                document.getElementById("zonevalue").textContent = result.zone || "unknown";
            }, 500); // Run every half second
        }

        function stoptrackfunc(){
            stoptrackstat = true;
        }

        starttrackbtn.addEventListener("click", function() {
            if (isChecked = document.getElementById("myCheckbox").checked) {
                startTracking();
                stoptrackbtn.addEventListener("click", stopTracking);
            } else {
                startTrackfunc();
                stoptrackbtn.addEventListener("click", stoptrackfunc);
            }
        });

        async function updateEntArray(eid, state){
            let newEid = eid.split("_distance_to_")[1];
            let index = NewEnts.findIndex(item => item.eid === newEid);
            if (state !== 'unknown') {
                
                let floor = finalcords.floor.find(floor => sameFloorName(floor.name, SelMapName));
                let rec = floor.receivers.find(element => element.entity_id === newEid);
                if (index !== -1) {
                    //The entity exists, update
                    NewEnts.splice(index, 1, {
                        eid: newEid,
                        cords: [
                            NewEnts[index].cords[0], // Keep existing x
                            NewEnts[index].cords[1], // Keep existing y
                            state * floor.scale      // Update z
                        ]
                    });
                    
                } else {
                    NewEnts.push({
                        eid: newEid, 
                        cords: [rec.cords.x, rec.cords.y, state * floor.scale]
                    });
                }
            } 
            if (state == 'unknown') {
                if (index !== -1) {
                    //Remove the entity from the array
                    NewEnts = NewEnts.filter(item => item.eid !== newEid);
                } 
            }
            await new Promise((resolve) => setTimeout(resolve, 100));
        }

    // =================================================================
    // Triliterate functionality
    // =================================================================
    const dataURL = null;
    let urlBol = false;

    function drawTracker(tricords){
        if(!urlBol){
            const dataURL = canvas.toDataURL('image/png');
            img.src = dataURL;
            urlBol = true;
        }
        clearCanvas();
        
        const iconSize = canvas.width * 0.04; // Adjust size as needed
        const x = tricords.x;
        const y = tricords.y;
        const icon = new Image();
        icon.src = getSelectedTrackerIcon();
        icon.onload = () => {
            ctx.drawImage(icon, x - iconSize / 2, y - iconSize / 2, iconSize, iconSize);
        };
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
                if (socket) {
                    stopTracking();
                } else {
                    stoptrackstat = true;
                }
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
        canvas.removeEventListener("mousedown", selectHandle);
        canvas.removeEventListener("mousemove", resizeRectangle);
        canvas.removeEventListener("mouseup", setHandles);
        canvas.removeEventListener("mousedown", startDrawingZone);
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
        if (zoneInputElement) {zoneInputElement.style.display = "none";}
        drawAreaButton.innerHTML = drawAreaButton.innerHTML.replace("Save Zone","Draw Zone");
        drawAreaButton.setAttribute('data-active', 'false');
        messdiv.innerHTML = "";
    }

    document.addEventListener('click', (event) => {
        // Check if the clicked element has the attribute data-type="removerec"
        if (event.target.closest('[data-type="removerec"]')) {
            const button = event.target.closest('[data-type="removerec"]'); // Get the button that was pressed
            const idToRemove = button.getAttribute('data-id'); // Get the value from data-id
            const elementToRemove = document.getElementById(idToRemove); // Find the element with the specific ID
            if (elementToRemove) { // Remove element if it exists
                console.log(`Receiver with ID "${idToRemove}" was removed.`);
                elementToRemove.remove();
            } else {
                console.log(`Receiver with ID "${idToRemove}" was not found.`);
                return;
            }
            // Loop through each floor and remove receivers where the entity_id matches
            finalcords.floor.forEach(floor => {
                if (sameFloorName(floor.name, SelMapName)) {
                    floor.receivers = floor.receivers.filter(receiver => receiver.entity_id !== idToRemove);
                }
            });
            console.log("Removed receiver");
            savebuttondiv.appendChild(saveButton);
            clearCanvas();
            drawElements();
        }
        if (event.target.closest('[data-type="removezone"]')) {
            const button = event.target.closest('[data-type="removezone"]'); // Get the button that was pressed
            const idToRemove = button.getAttribute('data-id'); // Get the value from data-id
            const elementToRemove = document.getElementById(idToRemove); // Find the element with the specific ID
            if (elementToRemove) { // Remove element if it exists
                elementToRemove.remove();
                console.log(`Zone with ID "${idToRemove}" was removed.`);
            } else {
                console.log(`Zone with ID "${idToRemove}" was not found.`);
                return;
            }
            // Loop through each floor and remove zones where the internal zone id matches
            finalcords.floor.forEach(floor => {
                if (sameFloorName(floor.name, SelMapName)) {
                    floor.zones = floor.zones.filter(zone => (zone.zone_id || zone.entity_id) !== idToRemove);
                }
            });
            console.log("Removed zone");
            savebuttondiv.appendChild(saveButton);
            clearCanvas();
            drawElements();
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
        addDeviceButton.remove();
        clearCanvasButton.remove();
        SetScaleButton.remove();
        saveButton.remove();
        deleteButton.remove();
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        mapname.value = "";
        SelMapName = "";
        buttonreset();
        mapSelector.selectedIndex = 0;
    });

    function clearCanvas(){
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        setupImageSize(img, canvas);
        messdiv.innerHTML = "";
    }

    // =================================================================
    // Draw zones
    // =================================================================

    let rectangle = null;
    let handles = [];
    let tmphandles = [];
    let selectedHandle = null;
    let zonecords = [];
    let zoneInputElement = null; // För att hantera input-fältet

    drawAreaButton.addEventListener("click", () => {
        if (!checkCanvasImage()) return;
        removeListeners();
        clearCanvas();
        drawElements();

        if (drawAreaButton.dataset.active === 'false') {
            buttonreset();
            canvas.addEventListener("mousedown", startDrawingZone);
            drawAreaButton.innerHTML = drawAreaButton.innerHTML.replace("Draw Zone","Save Zone");
            drawAreaButton.setAttribute('data-active', 'true');
            messdiv.innerHTML = '<h4 class="font-medium mb-2">Instructions</h4><p class="text-sm text-gray-500">Please a zone by clicking on the floor image. Scale the zone by draging the corner circles and enter the zone name. A good idea is to match the name with areas you have in Home Assistant.</p>';
        } else if (drawAreaButton.dataset.active === 'true') {
            if (!mapname.value) {
                alert("Please enter a floor name!");
                return;
            }
            SelMapName = mapname.value;
            if (!rectangle) {
                alert("No zone has been drawn.");
                return;
            }
            zoneName = document.getElementById('zoneName').value.trim();
            if (!zoneName) {
                alert("Please provide a name for the zone.");
                return;
            }

            zonecords = [
                { x: rectangle.x, y: rectangle.y },
                { x: rectangle.x + rectangle.width, y: rectangle.y },
                { x: rectangle.x, y: rectangle.y + rectangle.height },
                { x: rectangle.x + rectangle.width, y: rectangle.y + rectangle.height }
            ];
            let newZone = {
                zone_id: createZoneId(),
                entity_id: zoneName,
                cords: zonecords
              }; 
            if(addDataToFloor(finalcords, SelMapName, "zones", newZone)){
                alert(`Zone saved: ${zoneName}`);
                console.log("Saved coordinates:", zonecords);
                buttonreset();
                zoneInputElement.value = "";
                clearCanvas();
                drawElements();
            }
            
        }
    });

    const handleSize = 15;
    function startDrawingZone(event) {
        const rect = canvas.getBoundingClientRect();
        
        const scaleX = canvas.width / rect.width; // Horisontal scale
        const scaleY = canvas.height / rect.height; // Vertical scale

        const centerX = (event.clientX - rect.left) * scaleX;
        const centerY = (event.clientY - rect.top) * scaleY;
    
        rectangle = {
            x: centerX - 100,
            y: centerY - 100,
            width: 200,
            height: 200
        };
    
        handles = [
            { x: rectangle.x - handleSize, y: rectangle.y - handleSize },
            { x: rectangle.x + rectangle.width - handleSize, y: rectangle.y - handleSize },
            { x: rectangle.x - handleSize, y: rectangle.y + rectangle.height - handleSize },
            { x: rectangle.x + rectangle.width - handleSize, y: rectangle.y + rectangle.height - handleSize }
        ];

        tmphandles = handles;
    
        drawRectangle();
        canvas.removeEventListener("mousedown", startDrawingZone);
        canvas.addEventListener("mousedown", selectHandle);
        canvas.addEventListener("mousemove", resizeRectangle);
        canvas.addEventListener("mouseup", setHandles);
    }

    function setHandles(event){
        selectedHandle = null;
        tmphandles = handles;
    }

    function drawRectangle() {
        clearCanvas();
        drawElements();
    
        // Create the input field and place it above the line
        if (!zoneInputElement) {
            zoneInputElement = document.createElement("input");
            zoneInputElement.type = "text";
            zoneInputElement.id = "zoneName";
            zoneInputElement.placeholder = "Name";
            zoneInputElement.classList.add("zone-input");
            document.body.appendChild(zoneInputElement);
        }

        const rect = canvas.getBoundingClientRect();
        
        const scaleX = canvas.width / rect.width; // Horisontal scale
        const scaleY = canvas.height / rect.height; // Vertical scale

        const inputPosition = {
            left: ((rectangle.x + (rectangle.width/2))/ scaleX) + canvas.offsetLeft - zoneInputElement.offsetWidth / 2 + 40,
            top: (rectangle.y / scaleY) + canvas.offsetTop - 30 // 30 pixles above the line 
        };

        zoneInputElement.style.left = `${inputPosition.left - 20}px`;
        zoneInputElement.style.top = `${inputPosition.top - 10}px`;
        zoneInputElement.style.display = "block";
        zoneInputElement.style.position = "absolute";

        // Draw rectangle
        ctx.beginPath();
        ctx.rect(rectangle.x, rectangle.y, rectangle.width, rectangle.height);
        ctx.strokeStyle = "red";
        ctx.lineWidth = 2;
        ctx.stroke();
    
        // Draw handles
        handles.forEach(handle => {
            ctx.beginPath();
            ctx.arc(handle.x + handleSize, handle.y + handleSize, handleSize, 0, Math.PI * 2);
            ctx.fillStyle = "red";
            ctx.fill();
        });
    }

    function selectHandle(event) {
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width; // Horisontal scale
        const scaleY = canvas.height / rect.height; // Vertical scale
        const mouseX = (event.clientX - rect.left) * scaleX;
        const mouseY = (event.clientY - rect.top) * scaleY;
    
        selectedHandle = handles.find(
            handle =>
                mouseX >= handle.x - (handleSize * 2) &&
                mouseX <= handle.x + (handleSize * 2) &&
                mouseY >= handle.y - (handleSize * 2) &&
                mouseY <= handle.y + (handleSize * 2)
        );
    }

    function resizeRectangle(event) {
        if (!selectedHandle) return;
    
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width; // Horisontal scale
        const scaleY = canvas.height / rect.height; // Vertical scale
        const mouseX = (event.clientX - rect.left) * scaleX;
        const mouseY = (event.clientY - rect.top) * scaleY;
    
        if (selectedHandle === tmphandles[0]) {
            rectangle.width += rectangle.x - mouseX;
            rectangle.height += rectangle.y - mouseY;
            rectangle.x = mouseX;
            rectangle.y = mouseY;
        } else if (selectedHandle === tmphandles[1]) {
            rectangle.width = mouseX - rectangle.x;
            rectangle.height += rectangle.y - mouseY;
            rectangle.y = mouseY;
        } else if (selectedHandle === tmphandles[2]) {
            rectangle.width += rectangle.x - mouseX;
            rectangle.x = mouseX;
            rectangle.height = mouseY - rectangle.y;
        } else if (selectedHandle === tmphandles[3]) {
            rectangle.width = mouseX - rectangle.x;
            rectangle.height = mouseY - rectangle.y;
        }
    
        handles = [
            { x: rectangle.x - handleSize, y: rectangle.y - handleSize },
            { x: rectangle.x + rectangle.width - handleSize, y: rectangle.y - handleSize },
            { x: rectangle.x - handleSize, y: rectangle.y + rectangle.height - handleSize },
            { x: rectangle.x + rectangle.width - handleSize, y: rectangle.y + rectangle.height - handleSize }
        ];

        drawRectangle();
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
        const rect = canvas.getBoundingClientRect();
        if(countclick === 0){
            const scaleX = canvas.width / rect.width; // Horisontal scale
            const scaleY = canvas.height / rect.height; // Vertical scale
            startPoint = { x: (event.clientX - rect.left) * scaleX, y: (event.clientY - rect.top) * scaleY };
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
        const rect = canvas.getBoundingClientRect();
        const scaleX = canvas.width / rect.width; // Horisontal scale
        const scaleY = canvas.height / rect.height; // Vertical scale
        endPoint = { x: (event.clientX - rect.left) * scaleX, y: (event.clientY - rect.top) * scaleY };
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
        
        const inputPosition = {
            left: (lineMidpoint.x / scaleX) + canvas.offsetLeft - scaleInputElement.offsetWidth / 2 + 40,
            top: (lineMidpoint.y / scaleY) + canvas.offsetTop - 30
        };

        scaleInputElement.style.left = `${inputPosition.left}px`;
        scaleInputElement.style.top = `${inputPosition.top - 10}px`;
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
        const x = event.clientX;
        const y = event.clientY;

        drawElements(x, y, "receiver");

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

        const element = document.body;
        const myrect = element.getBoundingClientRect();
        const mx = event.clientX - myrect.left; // X relative element
        const my = event.clientY - myrect.top;  // Y relative element

        const inputPosition = {
            left: mx + (canvas.width * 0.04 / 2),
            top: my - (32/2)
        };
        entityInput.style.left = `${inputPosition.left}px`;
        entityInput.style.top = `${inputPosition.top}px`;
        entityInput.style.display = "flex";
        entityInput.style.position = "absolute";
    }

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

    function drawElements(xp, yp, type){
        const rect = canvas.getBoundingClientRect();
        const tmpdrawcords = [];
        const iconSize = canvas.width * 0.04; // Adjust size as needed
        deleteButton.remove();

        // Beräkna skalning mellan CSS-storlek och ritningsstorlek
        const scaleX = canvas.width / rect.width; // Horisontal scale
        const scaleY = canvas.height / rect.height; // Vertical scale

        const x = (xp - rect.left) * scaleX;
        const y = (yp - rect.top) * scaleY;

        tmpcords = { x, y };
        let newReceiver = {
            entity_id: receiverName,
            type: type,
            cords: tmpcords
          };
        tmpdrawcords.push(newReceiver); // Add new coordinates

        let floor = finalcords.floor.find(floor => sameFloorName(floor.name, SelMapName)); //Add all existing

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
        } else {
            // No saved floor matches the selection: tracking has nothing to
            // iterate, so do not offer it.
            trackdiv.style.display = "none";
        }

        let tmpHTMLrec = ""; 
        let tmpHTMLzone = "";
        tmpdrawcords.forEach((item, index) => {

            if (item.type == "receiver"){
                const x = item.cords.x;
                const y = item.cords.y;
                const icon = new Image();
                icon.src = "beacon.svg";
                icon.onload = () => {
                    ctx.drawImage(icon, x - iconSize / 2, y - iconSize / 2, iconSize, iconSize);
                };

                // Show id for receiver
                ctx.font = "bold 25px Arial";
                ctx.fillStyle = "black";
                ctx.fillText(item.entity_id, x + iconSize / 2 + 5, y);
                if(item.entity_id){
                    tmpHTMLrec = tmpHTMLrec + newelement.replace("typename", item.entity_id).replace("removexxx", "removerec").replace("idxxx", item.entity_id).replace("idxxx", item.entity_id);
                }
            }
            if (item.type == "zone"){
                const x = item.cords[0].x;
                const y = item.cords[0].y;
                const w = item.cords[1].x - x;
                const h = item.cords[2].y - y;
                
                // Draw rectangle
                ctx.beginPath();
                ctx.rect(x, y, w, h);
                ctx.strokeStyle = "red";
                ctx.lineWidth = 2;
                ctx.stroke();

                // Show id for Zone
                ctx.font = "25px Arial";
                ctx.fillStyle = "red";
                ctx.fillText(item.entity_id, x + 10, y + iconSize / 4);
                if(item.entity_id){
                    const zoneDomId = item.zone_id || item.entity_id;
                    tmpHTMLzone = tmpHTMLzone + newelement.replace("typename", item.entity_id).replace("removexxx", "removezone").replace("idxxx", zoneDomId).replace("idxxx", zoneDomId);
                }
            }
        });
        if(tmpHTMLrec !== ""){
            document.getElementById('divrec').innerHTML = tmpHTMLrec;
        } else{
            document.getElementById('divrec').innerHTML = '<p class="text-sm text-gray-500">No receivers placed</p>';
        }
        if(tmpHTMLzone !== ""){
            document.getElementById('divzones').innerHTML = tmpHTMLzone;
        } else{
            document.getElementById('divzones').innerHTML = '<p class="text-sm text-gray-500">No zones drawn</p>';
        }

    }

    // Display selected map
    mapSelector.addEventListener('change', async () => {
        img.src = `/local/bps_maps/${mapSelector.value}`;
        imgfilename = mapSelector.value;
        mapname.value = removeExtension(mapSelector.value);
        SelMapName = mapname.value;
        await setupCanvasWithImage(img, canvas);
        new_floor = false;
        drawElements();
    });

    upload.addEventListener('change', event => {
        const file = event.target.files[0];
        if (!file) return;

        // A new image invalidates any running tracking session (it iterates
        // the selected floor's receivers, which are about to change).
        if (socket) {
            stopTracking();
        } else {
            stoptrackstat = true;
        }

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
    
            // Add the buttons
            drawAreaButton.className = 'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&amp;_svg]:pointer-events-none [&amp;_svg]:size-4 [&amp;_svg]:shrink-0 border border-input bg-background hover:bg-accent hover:text-accent-foreground h-10 px-4 py-2';
            drawAreaButton.innerHTML = `
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-pencil w-4 h-4 mr-2" data-component-name="Pencil"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path><path d="m15 5 4 4"></path></svg>
                    Draw Zone
                `;
            drawAreaButton.setAttribute('data-active', 'false');

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
            mapbuttondiv.appendChild(SetScaleButton);
            mapbuttondiv.appendChild(clearCanvasButton);
        });
    }


    function setupImageSize(img, canvas, fixedWidth = 2000) {
        const ctx = canvas.getContext('2d');
    
        const imgratio = img.height / img.width;
        const newwidth = fixedWidth; // Fixed width in pixels
        const newheight = newwidth * imgratio; // Height based on aspect ratio
    
        // Update canvas size
        canvas.width = newwidth;
        canvas.height = newheight;
    
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

    // Receiver ids can be user-typed (Custom name…); never trust them in HTML.
    const escHtml = s => String(s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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
            let line = `Auto calibration running · ${pairs} receiver pairs in the window`;
            if (status.error) line += ` · ${status.error}`;
            else if (status.last_solved_at) line += ` · last solve ${status.last_solved_at}`;
            else line += ' · first solve after a few minutes of data';
            calibStatus.textContent = line;
            if (result) renderCalibrationResult(result);
            else calibResults.innerHTML = '';
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

});
