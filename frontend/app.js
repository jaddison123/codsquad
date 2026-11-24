// =========================================================================
// Frontend logic: draws the map and asks OUR backend for the heatmap.
// (It never talks to Global Fishing Watch directly, so no token here.)
// =========================================================================

// ---- 1. Create the map ---------------------------------------------------
// L is the Leaflet library. setView([latitude, longitude], zoom)
const map = L.map("map", {
  worldCopyJump: true,
  minZoom: 2, // stop zooming out so far that the world repeats side by side
  maxBounds: [[-85, -540], [85, 540]], // can't drag off the top/bottom of the world
  maxBoundsViscosity: 1,
}).setView([30, 0], 3);

// Dark basemap from Esri (free for non-commercial use, no API key needed).
// Two layers: the land/sea shapes, then country & place names on top.
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
L.tileLayer(`${ESRI}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, {
  attribution: "Basemap &copy; Esri",
  maxNativeZoom: 16,
  maxZoom: 18,
}).addTo(map);

// A separate "pane" (drawing layer) for the heatmap, so CSS can make it glow
// and so place names can sit above it.
map.createPane("heat").style.zIndex = 350;
map.createPane("labels").style.zIndex = 450;
map.getPane("labels").style.pointerEvents = "none";

L.tileLayer(`${ESRI}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, {
  pane: "labels",
  maxNativeZoom: 16,
  maxZoom: 18,
}).addTo(map);

// ---- 2. Grab the page elements we need -----------------------------------
const startInput = document.getElementById("start-date");
const endInput = document.getElementById("end-date");
const flagSelect = document.getElementById("flag-select");
const gearSelect = document.getElementById("gear-select");
const regionButtons = document.getElementById("region-buttons");
const updateButton = document.querySelector("#map-form button[type=submit]");
const statusEl = document.getElementById("status");

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

// The current choices in the sidebar, in one object. Other files (stats.js)
// read this so everything on screen describes the same data.
function currentFilters() {
  return {
    start: startInput.value,
    end: endInput.value,
    flag: flagSelect.value,
    gear: gearSelect.value,
    region: selectedRegion,
    bbox: customBbox,
    regionName: selectedRegion ? regions[selectedRegion].name
      : customBbox ? "Drawn area" : "Whole world",
  };
}

// Add the chosen area to a request: a preset region, a drawn box, or nothing
// (= whole world). Used by the stats chart and the download.
function addAreaParams(params) {
  if (selectedRegion) params.set("region", selectedRegion);
  else if (customBbox) params.set("bbox", customBbox.join(","));
}

// Tell any listener (the stats chart) that the filters changed.
function announceFiltersChanged() {
  document.dispatchEvent(new CustomEvent("filters-changed"));
}

// ---- 3. Default dates: the last 30 days of available data ----------------
// GFW data arrives a few days late, so we end the range 5 days ago.
function toISODate(d) {
  return d.toISOString().slice(0, 10); // "2026-11-11"
}
const defaultEnd = new Date();
defaultEnd.setDate(defaultEnd.getDate() - 5);
const defaultStart = new Date(defaultEnd);
defaultStart.setDate(defaultStart.getDate() - 30);
startInput.value = toISODate(defaultStart);
endInput.value = toISODate(defaultEnd);
endInput.max = toISODate(defaultEnd);

// ---- 4. Fill the dropdowns and region buttons from the server -------------
let regions = {}; // filled in below; we need the bounding boxes later
let flagNames = {}; // "ESP" -> "Spain"; also used by vessels.js

function addOptions(select, choices) {
  // choices looks like { "ESP": "Spain", ... }  -> sorted by the label
  Object.entries(choices)
    .sort((a, b) => a[1].localeCompare(b[1]))
    .forEach(([value, label]) => select.add(new Option(label, value)));
}

async function loadOptions() {
  const response = await fetch("/api/options");
  const options = await response.json();
  flagNames = options.flags;
  addOptions(flagSelect, options.flags);
  addOptions(gearSelect, options.gearTypes);

  regions = options.regions;
  for (const [id, region] of Object.entries(regions)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.dataset.region = id;
    button.textContent = region.name;
    regionButtons.appendChild(button);
  }
}

// ---- 5. Regions: zoom to a box and outline it ----------------------------
let regionOutline = null;
let selectedRegion = ""; // preset region id; "" means none
let customBbox = null;   // [west, south, east, north] of a box drawn on the map, or null
const drawButton = document.getElementById("draw-area");

const OUTLINE_STYLE = { color: "#ffffff", weight: 1.5, dashArray: "6 6", fill: false, interactive: false };

function showOutline(bounds) {
  if (regionOutline) map.removeLayer(regionOutline);
  regionOutline = bounds ? L.rectangle(bounds, OUTLINE_STYLE).addTo(map) : null;
}

// Highlight the chosen button ("" = whole world, null = none, e.g. a drawn box)
function highlightChip(id) {
  regionButtons.querySelectorAll(".chip").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.region === id);
  });
  drawButton.classList.toggle("active", Boolean(customBbox));
}

function selectRegion(id) {
  selectedRegion = id;
  customBbox = null;
  highlightChip(id);
  announceFiltersChanged();

  if (!id) {
    showOutline(null);
    map.flyTo([30, 0], 3);
    return;
  }
  // bbox is [west, south, east, north]; Leaflet wants [[south, west], [north, east]]
  const [west, south, east, north] = regions[id].bbox;
  const bounds = [[south, west], [north, east]];
  showOutline(bounds);
  map.flyToBounds(bounds, { padding: [30, 30] });
}

// ---- 5b. Draw your own area by dragging a box on the map ------------------
// Click "Draw an area", then press and drag on the map. Normal map dragging is
// switched off while drawing so the drag draws a box instead of moving the map.
const mapEl = map.getContainer();
let drawing = false;   // waiting for the user to drag
let dragStart = null;  // { latlng, x, y } where the drag began
let dragBox = null;    // the rectangle that follows the pointer

function setDrawMode(on) {
  drawing = on;
  dragStart = null;
  if (dragBox) map.removeLayer(dragBox);
  dragBox = null;
  drawButton.classList.toggle("waiting", on);
  mapEl.classList.toggle("drawing", on);
  if (on) {
    map.dragging.disable();
    setStatus("Press and drag on the map to draw an area. Press Esc to cancel.");
  } else {
    map.dragging.enable();
  }
}

// The map repeats sideways, so a drawn longitude can be e.g. 400°. Shift the box
// back into -180..180 and keep it off the poles (GFW has no data there).
function boundsToBbox(bounds) {
  const shift = Math.floor((bounds.getWest() + 180) / 360) * 360;
  const round = (v) => Math.round(v * 1000) / 1000;
  return {
    bbox: [
      round(bounds.getWest() - shift),
      round(Math.max(bounds.getSouth(), -85)),
      round(Math.min(bounds.getEast() - shift, 180)), // a box over the 180° line is cut there
      round(Math.min(bounds.getNorth(), 85)),
    ],
    shift,
  };
}

function selectCustomArea(bounds) {
  const { bbox, shift } = boundsToBbox(bounds);
  selectedRegion = "";
  customBbox = bbox;
  highlightChip(null);
  // Draw the outline where the user drew it (on the same copy of the world)
  const [west, south, east, north] = bbox;
  showOutline([[south, west + shift], [north, east + shift]]);
  setStatus("Stats and downloads now use your drawn area.");
  announceFiltersChanged();
}

drawButton.addEventListener("click", () => setDrawMode(!drawing));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && drawing) {
    setDrawMode(false);
    setStatus("Drawing cancelled.");
  }
});

// Pointer events cover mouse, pen and touch in one go
mapEl.addEventListener("pointerdown", (event) => {
  if (!drawing || event.button !== 0 || event.target.closest(".leaflet-control")) return;
  event.preventDefault();
  mapEl.setPointerCapture(event.pointerId); // keep getting moves even outside the map
  const latlng = map.mouseEventToLatLng(event);
  dragStart = { latlng, x: event.clientX, y: event.clientY };
  dragBox = L.rectangle([latlng, latlng], { ...OUTLINE_STYLE, dashArray: null, fill: true, fillOpacity: 0.08 }).addTo(map);
});

mapEl.addEventListener("pointermove", (event) => {
  if (!dragStart) return;
  dragBox.setBounds(L.latLngBounds(dragStart.latlng, map.mouseEventToLatLng(event)));
});

mapEl.addEventListener("pointerup", (event) => {
  if (!dragStart) return;
  const tooSmall = Math.abs(event.clientX - dragStart.x) < 10 || Math.abs(event.clientY - dragStart.y) < 10;
  const bounds = L.latLngBounds(dragStart.latlng, map.mouseEventToLatLng(event));
  setDrawMode(false);
  if (tooSmall) {
    setStatus("That box was too small. Click \"Draw an area\" to try again.", true);
    return;
  }
  selectCustomArea(bounds);
});

// e.g. a phone call or a second finger interrupts the drag
mapEl.addEventListener("pointercancel", () => {
  if (dragStart) setDrawMode(false);
});

// One click handler for all region buttons ("event delegation")
regionButtons.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) selectRegion(chip.dataset.region);
});

// ---- 6. Load the fishing heatmap -----------------------------------------
let heatmapLayer = null; // remember the current layer so we can replace it

async function loadHeatmap() {
  const start = startInput.value;
  const end = endInput.value;
  if (end <= start) {
    setStatus("The end date must be after the start date.", true);
    return;
  }

  // Build the query, e.g. ?start=2026-10-01&end=2026-11-10&flag=ESP
  const params = new URLSearchParams({ start, end });
  if (flagSelect.value) params.set("flag", flagSelect.value);
  if (gearSelect.value) params.set("gear", gearSelect.value);

  updateButton.disabled = true;
  setStatus("Loading fishing activity…");
  announceFiltersChanged();

  try {
    const response = await fetch(`/api/heatmap?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed");

    // Swap the old heatmap for the new one
    if (heatmapLayer) map.removeLayer(heatmapLayer);
    heatmapLayer = null;

    // Describe what's on screen, e.g. "Spain · Trawlers"
    const filterText = [
      flagSelect.value && flagSelect.selectedOptions[0].text,
      gearSelect.value && gearSelect.selectedOptions[0].text,
    ].filter(Boolean).join(" · ");
    const suffix = filterText ? ` (${filterText})` : "";

    if (!data.tileUrl) {
      setStatus(`No fishing activity found from ${start} to ${end}${suffix}.`);
      return;
    }

    heatmapLayer = L.tileLayer(data.tileUrl, {
      pane: "heat",
      maxNativeZoom: 12, // GFW has no extra detail past zoom 12; Leaflet scales tiles up
      maxZoom: 18,
      attribution: '<a href="https://globalfishingwatch.org">Global Fishing Watch</a>',
    }).addTo(map);

    setStatus(`Showing fishing activity from ${start} to ${end}${suffix}.`);
  } catch (err) {
    setStatus(`Could not load the heatmap: ${err.message}`, true);
  } finally {
    updateButton.disabled = false;
  }
}

// "Update map" button: reload instead of refreshing the page
document.getElementById("map-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadHeatmap();
});

// Changing a dropdown updates the map straight away
flagSelect.addEventListener("change", loadHeatmap);
gearSelect.addEventListener("change", loadHeatmap);

// ---- 7. Download: the data for the current filters, as Excel or raw CSV ----
// The button itself is in index.html, right under "Update map".
const downloadButton = document.getElementById("download-csv-button");
const resolutionSelect = document.getElementById("download-resolution"); // Daily or Hourly rows
const formatSelect = document.getElementById("download-format"); // Excel, CSV or chart

// The chart option shows the data in the site instead of saving a file
function updateDownloadLabel() {
  downloadButton.textContent = formatSelect.value === "chart" ? "Show chart" : "Download";
}
formatSelect.addEventListener("change", () => {
  updateDownloadLabel();
  if (formatSelect.value === "chart") preloadCharts(); // charts.js: fetch the library early
});
updateDownloadLabel();

downloadButton.addEventListener("click", async () => {
  const start = startInput.value;
  const end = endInput.value;
  if (!start || !end || end <= start) {
    setStatus("Choose a valid date range before downloading.", true);
    return;
  }

  const resolution = resolutionSelect.value;
  const format = formatSelect.value; // "xlsx" (easy-to-read Excel), "csv" (raw) or "chart"
  const params = new URLSearchParams({ start, end, resolution, format });
  if (flagSelect.value) params.set("flag", flagSelect.value);
  if (gearSelect.value) params.set("gear", gearSelect.value);
  addAreaParams(params);

  if (format === "chart") {
    openChartView(params); // charts.js: opens over the page, nothing is saved
    return;
  }

  downloadButton.disabled = true;
  setStatus(format === "xlsx" ? "Preparing your spreadsheet…" : "Preparing your CSV download…");
  try {
    const response = await fetch(`/api/download?${params.toString()}`);
    if (!response.ok) {
      let message = "Download failed.";
      try {
        const error = await response.json();
        message = error.detail || message;
      } catch (_) {}
      throw new Error(message);
    }

    // Save the file by pointing a temporary link at the downloaded data
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `fishing_activity_${resolution.toLowerCase()}_${start}_to_${end}.${format}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus("Download finished.");
  } catch (error) {
    setStatus(`Could not download: ${error.message}`, true);
  } finally {
    downloadButton.disabled = false;
  }
});

// ---- 8. Start up ---------------------------------------------------------
// Wait until every script on the page has loaded, so stats.js is listening
// before the first "filters-changed" announcement.
window.addEventListener("DOMContentLoaded", () => {
  loadOptions().catch(() => setStatus("Could not load filter options.", true));
  loadHeatmap();
});
