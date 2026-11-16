// =========================================================================
// Vessel search and the vessel details panel.
// Talks to our backend's /api/vessels/... routes, which use the GFW
// Python package. Loaded after app.js, so it can use `flagNames` from there.
// =========================================================================

const vesselForm = document.getElementById("vessel-form");
const vesselQuery = document.getElementById("vessel-query");
const vesselStatus = document.getElementById("vessel-status");
const vesselResults = document.getElementById("vessel-results");
const vesselPanel = document.getElementById("vessel-panel");
const vesselDetails = document.getElementById("vessel-details");

// Vessel names come from outside data, so we "escape" them before putting
// them into HTML. Otherwise a name like "<script>" could run as code.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// "ESP" -> "Spain (ESP)"; unknown codes are shown as-is
function flagLabel(code) {
  if (!code) return "Unknown flag";
  return flagNames[code] ? `${flagNames[code]} (${code})` : code;
}

function setVesselStatus(message, isError = false) {
  vesselStatus.textContent = message;
  vesselStatus.classList.toggle("error", isError);
}

// ---- 1. Search ------------------------------------------------------------
vesselForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = vesselQuery.value.trim();
  const button = vesselForm.querySelector("button");

  button.disabled = true;
  vesselResults.innerHTML = "";
  setVesselStatus("Searching…");

  try {
    const response = await fetch(`/api/vessels/search?q=${encodeURIComponent(query)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Search failed");

    if (data.results.length === 0) {
      setVesselStatus(`No vessels found for "${query}".`);
      return;
    }
    setVesselStatus(`${data.results.length} result${data.results.length > 1 ? "s" : ""}. Click one for details.`);
    data.results.forEach(showResult);
  } catch (err) {
    setVesselStatus(err.message, true);
  } finally {
    button.disabled = false;
  }
});

function showResult(vessel) {
  const meta = [
    flagLabel(vessel.flag),
    vessel.gear,
    vessel.mmsi && `MMSI ${vessel.mmsi}`,
  ].filter(Boolean).join(" · ");
  const formerly = vessel.formerNames.length ? ` (formerly ${vessel.formerNames.join(", ")})` : "";

  const item = document.createElement("li");
  item.innerHTML = `
    <button type="button">
      <span class="result-name">${esc(vessel.name)}<span class="result-meta">${esc(formerly)}</span></span>
      <span class="result-meta">${esc(meta)}</span>
    </button>`;
  item.querySelector("button").addEventListener("click", () => openVessel(vessel.id));
  vesselResults.appendChild(item);
}

// ---- 2. Details panel -----------------------------------------------------
let openVesselId = null; // which vessel is showing, so late replies for an old one are ignored

async function openVessel(id) {
  openVesselId = id;
  clearEvents();
  vesselPanel.hidden = false;
  vesselDetails.innerHTML = `<p class="empty">Loading vessel…</p>`;

  try {
    const response = await fetch(`/api/vessels/${encodeURIComponent(id)}`);
    const v = await response.json();
    if (!response.ok) throw new Error(v.detail || "Lookup failed");
    if (openVesselId !== id) return;
    vesselDetails.innerHTML = renderVessel(v);
    loadEvents(id, v.ids);
  } catch (err) {
    vesselDetails.innerHTML = `<p class="empty">Could not load this vessel: ${esc(err.message)}</p>`;
  }
}

// ---- 3. Fishing events and port visits on the map -------------------------
// Colours checked for colour-blind safety; port visits also get a white ring
// and a bigger size, so the two types differ by more than colour alone.
const EVENT_STYLE = {
  fishing: { radius: 4, color: "#d9741f", fillColor: "#d9741f", weight: 0, fillOpacity: 0.85 },
  port_visit: { radius: 6, color: "#ffffff", fillColor: "#5b8def", weight: 1.5, fillOpacity: 1 },
};
// Drawing 1000+ dots is much faster on one <canvas> than as 1000 separate SVG shapes
const eventRenderer = L.canvas({ padding: 0.5 });
let eventLayer = null;

function clearEvents() {
  if (eventLayer) map.removeLayer(eventLayer); // `map` comes from app.js
  eventLayer = null;
}

// Ships that cross the 180° line (e.g. in the Pacific) would otherwise be drawn
// at both edges of the map. Shift longitudes so they stay next to each other.
function keepNear(lon, referenceLon) {
  while (lon - referenceLon > 180) lon -= 360;
  while (lon - referenceLon < -180) lon += 360;
  return lon;
}

function popupText(e) {
  const title = e.type === "port_visit" ? `Port visit${e.port ? `: ${e.port}` : ""}` : "Fishing event";
  return `<strong>${esc(title)}</strong><br>${esc(e.start)} → ${esc(e.end)} UTC`;
}

async function loadEvents(id, ids) {
  const section = document.getElementById("vessel-events");
  try {
    const response = await fetch(`/api/vessels/events?ids=${encodeURIComponent(ids.join(","))}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed");
    if (openVesselId !== id) return; // user has opened a different vessel meanwhile

    const all = [...data.fishing, ...data.portVisits];
    if (all.length === 0) {
      section.innerHTML = `<p class="empty">No fishing events or port visits on record.</p>`;
      return;
    }

    // Port visits are drawn last so they sit on top of fishing dots
    const refLon = all[0].lon;
    eventLayer = L.layerGroup(
      [...data.fishing, ...data.portVisits].map((e) =>
        L.circleMarker([e.lat, keepNear(e.lon, refLon)], { ...EVENT_STYLE[e.type], renderer: eventRenderer })
          .bindPopup(popupText(e))
      )
    ).addTo(map);
    zoomToEvents();

    section.innerHTML = `
      <div class="event-legend">
        <span><span class="dot fishing"></span>${data.fishing.length} fishing events</span>
        <span><span class="dot port"></span>${data.portVisits.length} port visits</span>
      </div>
      ${data.truncated ? `<p class="empty">Showing the most recent 1,000 of each type.</p>` : ""}
      <button type="button" class="link" id="zoom-events">Zoom to activity</button>`;
    document.getElementById("zoom-events").addEventListener("click", zoomToEvents);
  } catch (err) {
    if (openVesselId === id) section.innerHTML = `<p class="empty">Could not load events: ${esc(err.message)}</p>`;
  }
}

function zoomToEvents() {
  if (!eventLayer) return;
  const points = eventLayer.getLayers().map((m) => m.getLatLng());
  map.flyToBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: 8 });
}

// Builds the HTML for the panel. Every value from GFW goes through esc().
function renderVessel(v) {
  // Key facts: only show the rows we actually have data for
  const facts = [
    ["Flag", flagLabel(v.flag)],
    ["MMSI", v.mmsi],
    ["IMO", v.imo],
    ["Call sign", v.callSign],
    ["Length", v.lengthM && `${v.lengthM.toFixed(1)} m`],
    ["Tonnage", v.tonnageGt && `${Math.round(v.tonnageGt)} GT`],
    ["AIS seen", v.firstSeen && `${v.firstSeen} → ${v.lastSeen}`],
  ].filter(([, value]) => value)
    .map(([label, value]) => `<dt>${label}</dt><dd>${esc(value)}</dd>`)
    .join("");

  const list = (items, toHtml, emptyText) =>
    items.length ? `<ul class="rows">${items.map(toHtml).join("")}</ul>` : `<p class="empty">${emptyText}</p>`;

  return `
    <h3>${esc(v.name)}</h3>
    ${v.formerNames.length ? `<p class="aka">Formerly ${esc(v.formerNames.join(", "))}</p>` : ""}
    ${v.gear ? `<span class="tag">${esc(v.gear)}</span>` : ""}

    <h4>Activity on the map</h4>
    <div id="vessel-events"><p class="empty">Loading fishing events and port visits…</p></div>

    <h4>Identity</h4>
    <dl class="facts">${facts}</dl>

    <h4>Owners (from registries)</h4>
    ${list(v.owners, (o) => `
      <li>${esc(o.name)} · ${esc(flagLabel(o.flag))}
        <span class="when">${esc(o.from)} → ${esc(o.to)}</span></li>`,
      "No public owner records.")}

    <h4>Fishing authorisations</h4>
    ${list(v.licences, (l) => `
      <li>${esc(l.source)}
        <span class="when">${esc(l.from)} → ${esc(l.to)}</span></li>`,
      "No public authorisation records.")}

    <h4>AIS identity history</h4>
    ${list(v.aisHistory, (a) => `
      <li>${esc(a.name)} · ${esc(flagLabel(a.flag))} · MMSI ${esc(a.mmsi)}
        <span class="when">${esc(a.from)} → ${esc(a.to)}</span></li>`,
      "No AIS records.")}
  `;
}

document.getElementById("vessel-close").addEventListener("click", () => {
  vesselPanel.hidden = true;
  openVesselId = null;
  clearEvents(); // closing the panel also removes the vessel's dots from the map
});
