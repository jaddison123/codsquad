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
    regionName: selectedRegion ? regions[selectedRegion].name : "Whole world",
  };
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
let selectedRegion = ""; // "" means the whole world (used by the stats chart later)

function selectRegion(id) {
  selectedRegion = id;
  announceFiltersChanged();

  // Highlight the chosen button
  regionButtons.querySelectorAll(".chip").forEach((chip) => {
    chip.classList.toggle("active", chip.dataset.region === id);
  });

  if (regionOutline) map.removeLayer(regionOutline);
  regionOutline = null;

  if (!id) {
    map.flyTo([30, 0], 3);
    return;
  }
  // bbox is [west, south, east, north]; Leaflet wants [[south, west], [north, east]]
  const [west, south, east, north] = regions[id].bbox;
  const bounds = [[south, west], [north, east]];
  regionOutline = L.rectangle(bounds, {
    color: "#ffffff",
    weight: 1.5,
    dashArray: "6 6",
    fill: false,
    interactive: false,
  }).addTo(map);
  map.flyToBounds(bounds, { padding: [30, 30] });
}

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

// ---- 7. CSV download: export the data for the filters currently selected ----
// The button itself is in index.html, right under "Update map".
const downloadButton = document.getElementById("download-csv-button");
const resolutionSelect = document.getElementById("download-resolution"); // Daily or Hourly rows

downloadButton.addEventListener("click", async () => {
  const start = startInput.value;
  const end = endInput.value;
  if (!start || !end || end <= start) {
    setStatus("Choose a valid date range before downloading.", true);
    return;
  }

  const resolution = resolutionSelect.value;
  const params = new URLSearchParams({ start, end, resolution });
  if (flagSelect.value) params.set("flag", flagSelect.value);
  if (gearSelect.value) params.set("gear", gearSelect.value);
  if (selectedRegion) params.set("region", selectedRegion);

  downloadButton.disabled = true;
  setStatus("Preparing your CSV download…");
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
    link.download = `fishing_activity_${resolution.toLowerCase()}_${start}_to_${end}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus("CSV downloaded successfully.");
  } catch (error) {
    setStatus(`Could not download CSV: ${error.message}`, true);
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
