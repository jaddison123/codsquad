// =========================================================================
// Chart view: the "Charts" option under "Update map".
// Opens a full-screen panel over the site with:
//   - four key facts
//   - a flat hotspot map where the 10 busiest squares pop out as small columns
//   - a "countries by day" grid
//   - three charts: per day, by country, by fishing method
// The style is "2.5D": flat, fast charts with a few raised, shadowed elements,
// instead of full 3D models.
// Data: /api/download?format=chart (the same data as the Excel/CSV download,
// summed up on the server). Charts are drawn with ECharts.
// The chart library and coastline data are only downloaded the first time
// someone opens the view, so the map page itself stays fast.
// =========================================================================

const CHART_LIBS = {
  echarts: "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
  topojson: "https://cdn.jsdelivr.net/npm/topojson-client@3.1.0/dist/topojson-client.min.js",
  // Coastlines of the world (Natural Earth, 1:50 million scale)
  land: "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-50m.json",
};

// Colours: the site's navy + teal. Low values are dim teal, high values bright,
// so busy areas glow like the heatmap on the main map.
const CV = {
  text: "#e6eef7",
  muted: "#8ea5bd",
  grid: "#1f3a58",
  panel: "#12263d",
  sea: "#0a1b2e",
  land: "#2c3f55",
  coast: "#4d6784",
  teal: "#00e6c3",
  ramp: ["#0f4c5c", "#0b7a78", "#00a88f", "#00e6c3", "#b5fff0"],
};
const TOP_HOTSPOTS = 10; // how many squares pop out on the map

const chartView = document.getElementById("chart-view");
const cvMessage = document.getElementById("cv-message");
const cvContent = document.getElementById("cv-content");
const cvCaption = document.getElementById("cv-caption");
const cvHint = document.getElementById("cv-hint");
const viewButtons = chartView.querySelectorAll("[data-view]");

let charts = {};        // ECharts instances by element id, so we can resize/dispose them
let chartData = null;   // the last data from the server
let currentView = "map";
let chartRequest = 0;   // ignore replies to requests the user has since abandoned

// ---- Loading the libraries (once) -----------------------------------------
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = resolve;
    script.onerror = () => reject(new Error("Could not load the chart library. Check your internet connection."));
    document.head.appendChild(script);
  });
}

let libsPromise = null;
function loadChartLibs() {
  if (!libsPromise) {
    libsPromise = Promise.all([loadScript(CHART_LIBS.echarts), loadScript(CHART_LIBS.topojson)]);
    libsPromise.catch(() => { libsPromise = null; }); // allow a retry
  }
  return libsPromise;
}

let landPromise = null;
function loadLand() {
  if (!landPromise) {
    landPromise = fetch(CHART_LIBS.land)
      .then((r) => r.json())
      .then((topo) => topojson.feature(topo, topo.objects.land).features)
      .catch(() => { landPromise = null; return []; }); // no coastlines: still show the data
  }
  return landPromise;
}

// ---- Small helpers ---------------------------------------------------------
const fmtHours = (h) => Math.round(h).toLocaleString("en-GB");
const shortDate = (iso) => new Date(`${iso}T00:00:00Z`)
  .toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
const longDate = (iso) => new Date(`${iso}T00:00:00Z`)
  .toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const place = (lon, lat) => `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? "N" : "S"}, `
  + `${Math.abs(lon).toFixed(1)}°${lon >= 0 ? "E" : "W"}`;

// 59400 -> "59.4k"
function compactHours(h) {
  if (h >= 1e6) return `${(h / 1e6).toFixed(1)}M`;
  if (h >= 1e3) return `${(h / 1e3).toFixed(h >= 1e4 ? 0 : 1)}k`;
  return String(Math.round(h));
}

// Escape text before putting it in a tooltip (tooltips are HTML)
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// Lighter / darker versions of a colour, for the lit and shaded faces
const shade = (color, amount) => echarts.color.lift(color, amount);

function makeChart(id) {
  if (charts[id]) charts[id].dispose();
  charts[id] = echarts.init(document.getElementById(id));
  return charts[id];
}

function disposeCharts() {
  Object.values(charts).forEach((c) => c.dispose());
  charts = {};
}

const tooltipStyle = {
  backgroundColor: CV.panel,
  borderColor: CV.grid,
  textStyle: { color: CV.text, fontFamily: "Inter, system-ui, sans-serif", fontSize: 12 },
  extraCssText: "box-shadow: 0 6px 20px rgba(0,0,0,.45); border-radius: 8px;",
};

function colourScale(max, dimension) {
  return {
    type: "continuous",
    dimension,
    min: 0,
    max,
    inRange: { color: CV.ramp },
    calculable: true,
    text: ["More fishing", "Less"],
    textStyle: { color: CV.muted },
    left: 8,
    bottom: 8,
    itemHeight: 110,
    formatter: (v) => fmtHours(v),
  };
}

// ---- Clipping coastlines to the chosen area --------------------------------
// The map shows only the chosen rectangle, so cut every land shape to it
// (the Sutherland–Hodgman method: trim the shape against each edge in turn).
function clipRing(ring, [west, south, east, north]) {
  const atX = (a, b, x) => [x, a[1] + ((b[1] - a[1]) * (x - a[0])) / (b[0] - a[0])];
  const atY = (a, b, y) => [a[0] + ((b[0] - a[0]) * (y - a[1])) / (b[1] - a[1]), y];
  const edges = [
    [(p) => p[0] >= west, (a, b) => atX(a, b, west)],
    [(p) => p[0] <= east, (a, b) => atX(a, b, east)],
    [(p) => p[1] >= south, (a, b) => atY(a, b, south)],
    [(p) => p[1] <= north, (a, b) => atY(a, b, north)],
  ];
  let points = ring;
  for (const [inside, cross] of edges) {
    const input = points;
    points = [];
    input.forEach((current, i) => {
      const previous = input[(i + input.length - 1) % input.length];
      if (inside(current)) {
        if (!inside(previous)) points.push(cross(previous, current));
        points.push(current);
      } else if (inside(previous)) {
        points.push(cross(previous, current));
      }
    });
    if (points.length < 3) return null;
  }
  points.push(points[0]); // GeoJSON rings end where they start
  return points;
}

function clipLand(features, bbox) {
  const [west, south, east, north] = bbox;
  const polygons = [];
  for (const feature of features) {
    const geom = feature.geometry;
    const list = geom.type === "Polygon" ? [geom.coordinates] : geom.coordinates;
    for (const polygon of list) {
      // Quick skip for shapes entirely outside the area
      const xs = polygon[0].map((p) => p[0]);
      const ys = polygon[0].map((p) => p[1]);
      if (Math.max(...xs) < west || Math.min(...xs) > east
        || Math.max(...ys) < south || Math.min(...ys) > north) continue;
      const outline = clipRing(polygon[0], bbox);
      if (!outline) continue;
      // Any further rings are holes (lakes); keep the ones still inside the area
      const holes = polygon.slice(1).map((ring) => clipRing(ring, bbox)).filter(Boolean);
      polygons.push([outline, ...holes]);
    }
  }
  return polygons;
}

// ---- Hotspot map: flat squares, with the busiest ones popping out ----------
// One raised column, drawn as three faces (front, lit top, shaded side) plus a
// soft shadow on the ground, so it looks like it stands up off the map.
function columnShapes(x, y, height, colour, label) {
  const w = 10;  // width of the front face, in pixels
  const d = 5;   // depth: how far the top and side faces lean back
  const top = y - height;
  return [
    { type: "ellipse", shape: { cx: x + d / 2, cy: y, rx: w * 0.9, ry: 4 }, style: { fill: "rgba(0,0,0,0.45)" } },
    { type: "polygon", shape: { points: [[x + w / 2, y], [x + w / 2 + d, y - d], [x + w / 2 + d, top - d], [x + w / 2, top]] },
      style: { fill: shade(colour, -0.35) } },
    { type: "rect", shape: { x: x - w / 2, y: top, width: w, height }, style: { fill: colour } },
    { type: "polygon", shape: { points: [[x - w / 2, top], [x + w / 2, top], [x + w / 2 + d, top - d], [x - w / 2 + d, top - d]] },
      style: { fill: shade(colour, 0.35) } },
    ...(label ? [{ type: "text", x: x + d / 2, y: top - d - 4,
      style: { text: label, fill: CV.text, font: "600 11px Inter, system-ui, sans-serif", align: "center", verticalAlign: "bottom",
        stroke: CV.sea, lineWidth: 3 } }] : []),
  ];
}

async function drawHotspotMap(data) {
  const [west, south, east, north] = data.bbox;
  const land = clipLand(await loadLand(), data.bbox);
  if (currentView !== "map" || chartData !== data) return; // switched away while loading

  // The sea is a rectangle the size of the area, with the land drawn on top.
  // Registering both as one "map" makes the map exactly the chosen area.
  echarts.registerMap("fishing-area", {
    type: "FeatureCollection",
    features: [
      { type: "Feature", properties: { name: "Sea" },
        geometry: { type: "Polygon", coordinates: [[[west, south], [east, south], [east, north], [west, north], [west, south]]] } },
      ...(land.length ? [{ type: "Feature", properties: { name: "Land" },
        geometry: { type: "MultiPolygon", coordinates: land } }] : []),
    ],
  });

  const maxHours = Math.max(...data.cells.map((c) => c[2]), 1);
  const half = data.cellSize / 2;
  // The busiest squares, back to front (north first) so nearer columns overlap farther ones
  const hotspots = [...data.cells].sort((a, b) => b[2] - a[2]).slice(0, TOP_HOTSPOTS)
    .map((cell, i) => [...cell, i + 1])
    .sort((a, b) => b[1] - a[1]);
  const topHours = hotspots.reduce((m, c) => Math.max(m, c[2]), 1);

  cvCaption.textContent = `Each square is about ${squareSize(data.cellSize)} across; brighter = more hours of fishing. `
    + `The ${hotspots.length} busiest squares stand up as numbered columns (taller = busier).`;
  cvHint.textContent = "Scroll to zoom · drag to move · hover for details";

  const chart = makeChart("cv-map");
  chart.setOption({
    tooltip: {
      ...tooltipStyle,
      formatter: (p) => {
        const [lon, lat, hours, rank] = p.value;
        const title = rank ? `<span style="color:${CV.teal}">#${rank} busiest spot</span><br>` : "";
        return `${title}<strong>${fmtHours(hours)} hours</strong> of fishing<br>`
          + `<span style="color:${CV.muted}">Near ${place(lon, lat)}</span>`;
      },
    },
    visualMap: { ...colourScale(maxHours, 2), seriesIndex: [0, 1] },
    geo: {
      map: "fishing-area",
      roam: true,               // scroll to zoom, drag to move
      scaleLimit: { min: 1, max: 12 },
      // Squash east-west the way a flat map does at this latitude
      aspectScale: Math.max(0.3, Math.cos((((south + north) / 2) * Math.PI) / 180)),
      layoutCenter: ["50%", "50%"],
      layoutSize: "96%",
      silent: true,
      itemStyle: { areaColor: CV.land, borderColor: CV.coast, borderWidth: 0.8 },
      regions: [{ name: "Sea", itemStyle: { areaColor: CV.sea, borderWidth: 0 } }],
    },
    series: [
      {
        // Every square, flat, coloured by hours
        type: "custom",
        coordinateSystem: "geo",
        data: data.cells,
        renderItem: (params, api) => {
          const a = api.coord([api.value(0) - half, api.value(1) + half]);
          const b = api.coord([api.value(0) + half, api.value(1) - half]);
          return {
            type: "rect",
            shape: { x: a[0], y: a[1], width: Math.max(1, b[0] - a[0] - 0.6), height: Math.max(1, b[1] - a[1] - 0.6) },
            style: { fill: api.visual("color") },
            emphasis: { style: { fill: "#ffffff" } },
          };
        },
      },
      {
        // The busiest squares, raised
        type: "custom",
        coordinateSystem: "geo",
        data: hotspots,
        z: 5,
        renderItem: (params, api) => {
          const [x, y] = api.coord([api.value(0), api.value(1)]);
          const height = 18 + 52 * (api.value(2) / topHours);
          // Skip the "#n" label if a busier column right next to it already has one,
          // so labels don't pile up where hotspots are close together
          const rank = api.value(3);
          const crowded = hotspots.some(([lon, lat, , r]) => {
            if (r >= rank) return false;
            const [bx, by] = api.coord([lon, lat]);
            return Math.hypot(bx - x, by - y) < 26;
          });
          return { type: "group", children: columnShapes(x, y, height, api.visual("color"), crowded ? "" : `#${rank}`) };
        },
      },
    ],
  });
}

// "0.1° square" -> "11 km"
function squareSize(degrees) {
  const km = degrees * 111; // north-south: 1° of latitude is about 111 km
  return km >= 10 ? `${Math.round(km)} km` : `${km.toFixed(1)} km`;
}

// ---- Countries by day: a grid of tiles that lift on hover ------------------
function drawCountriesByDay(data) {
  const { countries, days, values } = data.countryDay;
  const max = Math.max(...values.map((v) => v[2]), 1);
  cvCaption.textContent = `The ${countries.length} countries whose boats fished most here, day by day. `
    + "Brighter = more hours of fishing that day.";
  cvHint.textContent = "Hover a square for details";

  const chart = makeChart("cv-heat");
  chart.setOption({
    grid: { left: 8, right: 16, top: 8, bottom: 70, containLabel: true },
    tooltip: {
      ...tooltipStyle,
      formatter: (p) => `<strong>${escapeHtml(countries[p.value[1]])}</strong><br>`
        + `${longDate(days[p.value[0]])}<br>${fmtHours(p.value[2])} hours of fishing`,
    },
    visualMap: { ...colourScale(max, 2), orient: "horizontal", left: "center", bottom: 8, itemWidth: 14, itemHeight: 160 },
    xAxis: {
      type: "category",
      data: days.map(shortDate),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: CV.muted, fontSize: 11 },
    },
    yAxis: {
      type: "category",
      data: countries,
      inverse: true, // busiest country at the top
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: CV.text, fontSize: 12 },
    },
    series: [{
      type: "heatmap",
      data: values,
      itemStyle: { borderColor: CV.panel, borderWidth: 3, borderRadius: 5 },
      emphasis: {
        // The hovered square "pops": white rim and a teal glow
        itemStyle: { borderColor: "#ffffff", borderWidth: 2, shadowBlur: 16, shadowColor: "rgba(0,230,195,0.8)" },
      },
    }],
  });
}

// ---- Line chart: fishing per day -------------------------------------------
const flatAxis = {
  axisLine: { lineStyle: { color: CV.grid } },
  axisTick: { show: false },
  axisLabel: { color: CV.muted, fontSize: 11 },
  splitLine: { lineStyle: { color: CV.grid, opacity: 0.7 } },
};

function drawDaily(data) {
  const chart = makeChart("cv-daily");
  chart.setOption({
    grid: { left: 52, right: 16, top: 16, bottom: 28 },
    tooltip: {
      ...tooltipStyle,
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: CV.muted, type: "dashed" } },
      formatter: ([p]) => `${longDate(data.byDay[p.dataIndex].date)}<br><strong>${fmtHours(p.value)} hours</strong> of fishing`,
    },
    xAxis: { type: "category", data: data.byDay.map((d) => shortDate(d.date)), boundaryGap: false, ...flatAxis, splitLine: { show: false } },
    yAxis: { type: "value", ...flatAxis, axisLine: { show: false }, axisLabel: { ...flatAxis.axisLabel, formatter: compactHours } },
    series: [{
      type: "line",
      data: data.byDay.map((d) => d.hours),
      smooth: 0.3,
      symbol: "circle",
      symbolSize: 8,
      showSymbol: data.byDay.length <= 31,
      // A glow under the line lifts it off the background
      lineStyle: { width: 2, color: CV.teal, shadowBlur: 12, shadowColor: "rgba(0,230,195,0.6)", shadowOffsetY: 4 },
      itemStyle: { color: CV.teal, borderColor: CV.panel, borderWidth: 2 },
      emphasis: { scale: 1.6 },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: "rgba(0,230,195,0.35)" },
          { offset: 1, color: "rgba(0,230,195,0)" },
        ]),
      },
    }],
  });
}

// ---- Bar charts with raised ("2.5D") bars -----------------------------------
// Each bar is a flat front face plus a lit top face and a shaded end face.
function raisedBar(params, api) {
  const [x0, yMid] = api.coord([0, api.value(1)]);
  const [x1] = api.coord([api.value(0), api.value(1)]);
  const t = 12;  // bar thickness
  const d = 5;   // depth of the top and end faces
  const y = yMid - t / 2 + d / 2;
  const w = Math.max(2, x1 - x0);
  const lit = shade(CV.teal, 0.35);
  const dark = shade(CV.teal, -0.4);
  return {
    type: "group",
    children: [
      { type: "rect", shape: { x: x0, y, width: w, height: t }, style: { fill: CV.teal },
        emphasis: { style: { fill: "#5cf2da" } } },
      { type: "polygon", shape: { points: [[x0, y], [x0 + d, y - d], [x0 + w + d, y - d], [x0 + w, y]] },
        style: { fill: lit } },
      { type: "polygon", shape: { points: [[x0 + w, y], [x0 + w + d, y - d], [x0 + w + d, y + t - d], [x0 + w, y + t]] },
        style: { fill: dark } },
      { type: "text", x: x0 + w + d + 6, y: y + t / 2 - d / 2,
        style: { text: compactHours(api.value(0)), fill: CV.muted, font: "11px Inter, system-ui, sans-serif", verticalAlign: "middle" } },
    ],
  };
}

function drawBars(id, rows, total, extra) {
  const chart = makeChart(id);
  chart.setOption({
    grid: { left: 8, right: 52, top: 10, bottom: 4, containLabel: true },
    tooltip: {
      ...tooltipStyle,
      trigger: "item",
      formatter: (p) => {
        const row = rows[p.dataIndex];
        const share = total ? Math.round((row.hours / total) * 100) : 0;
        return `<strong>${escapeHtml(row.name)}</strong><br>${fmtHours(row.hours)} hours · ${share}% of all fishing`
          + (extra ? extra(row) : "");
      },
    },
    xAxis: { type: "value", show: false, max: (v) => v.max * 1.05 },
    yAxis: {
      type: "category",
      data: rows.map((r) => r.name),
      inverse: true, // biggest at the top
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: CV.text, fontSize: 12, width: 130, overflow: "truncate" },
    },
    series: [{
      type: "custom",
      renderItem: raisedBar,
      encode: { x: 0, y: 1 },
      data: rows.map((r, i) => [r.hours, i]),
    }],
  });
}

// ---- Key facts -------------------------------------------------------------
function drawTiles(data) {
  const busiest = data.byDay.reduce((a, b) => (b.hours > a.hours ? b : a));
  const topCountry = data.byCountry[0];
  const topGear = data.byGear[0];
  const share = (h) => `${Math.round((h / data.totalHours) * 100)}% of all fishing`;
  const days = data.totalHours / 24;
  const perspective = days < 2 ? `${fmtHours(data.totalHours)} hours`
    : days < 730 ? `${fmtHours(days)} days` : `${(days / 365).toFixed(1)} years`;

  const tiles = [
    ["Time spent fishing", `${fmtHours(data.totalHours)} h`, `Like one boat fishing non-stop for ${perspective}`],
    ["Busiest day", shortDate(busiest.date), `${fmtHours(busiest.hours)} hours of fishing`],
    ["Top country", topCountry.name, share(topCountry.hours)],
    ["Main fishing method", topGear.name, share(topGear.hours)],
  ];
  const container = document.getElementById("cv-tiles");
  container.innerHTML = "";
  for (const [label, value, note] of tiles) {
    const tile = document.createElement("div");
    tile.className = "cv-tile";
    tile.innerHTML = `<span class="cv-tile-label"></span><span class="cv-tile-value"></span><span class="cv-tile-note"></span>`;
    tile.querySelector(".cv-tile-label").textContent = label;
    tile.querySelector(".cv-tile-value").textContent = value;
    tile.querySelector(".cv-tile-note").textContent = note;
    container.appendChild(tile);
  }
}

// ---- Switching between the map and the countries grid ----------------------
function showView(view) {
  currentView = view;
  viewButtons.forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.getElementById("cv-map").hidden = view !== "map";
  document.getElementById("cv-heat").hidden = view !== "countries";
  if (!chartData) return;
  // Each is drawn when it's first shown (a hidden chart has no size yet)
  if (view === "map") drawHotspotMap(chartData);
  else drawCountriesByDay(chartData);
}
viewButtons.forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

// ---- Opening and closing ---------------------------------------------------
function showMessage(text, isError = false) {
  cvMessage.textContent = text;
  cvMessage.hidden = !text;
  cvMessage.classList.toggle("error", isError);
}

async function openChartView(params) {
  const requestId = ++chartRequest;
  const f = currentFilters(); // from app.js
  document.getElementById("cv-subtitle").textContent = `${f.regionName} · loading…`;
  chartView.hidden = false;
  document.body.classList.add("cv-open");
  cvContent.hidden = true;
  disposeCharts();
  showMessage("Fetching fishing data… big areas or long date ranges can take up to a minute.");

  try {
    const [response] = await Promise.all([fetch(`/api/download?${params}`), loadChartLibs()]);
    const data = await response.json();
    if (requestId !== chartRequest) return; // closed or reopened meanwhile
    if (!response.ok) throw new Error(data.detail || "Request failed");

    const filters = [data.flag, data.gear].filter((x) => !x.startsWith("All")).join(" · ");
    document.getElementById("cv-subtitle").textContent =
      `${data.area} · ${longDate(data.start)} to ${longDate(data.lastDay)}${filters ? ` · ${filters}` : ""}`;

    if (!data.totalHours) {
      showMessage("No fishing was recorded for these choices. Try a longer date range, a bigger area or fewer filters.");
      return;
    }
    chartData = data;
    showMessage("");
    cvContent.hidden = false;

    drawTiles(data);
    drawDaily(data);
    drawBars("cv-countries", data.byCountry, data.totalHours);
    drawBars("cv-gear", data.byGear, data.totalHours,
      (row) => (row.explain ? `<br><span style="color:${CV.muted}">${escapeHtml(row.explain)}</span>` : ""));
    showView(currentView);
  } catch (err) {
    if (requestId !== chartRequest) return;
    showMessage(`Could not load the charts: ${err.message}`, true);
  }
}

function closeChartView() {
  chartRequest++; // drop any reply still on its way
  chartView.hidden = true;
  document.body.classList.remove("cv-open");
  disposeCharts();
  chartData = null;
}

document.getElementById("cv-close").addEventListener("click", closeChartView);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !chartView.hidden) closeChartView();
});
window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
