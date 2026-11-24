// =========================================================================
// "Fishing hours" chart: total hours by flag or gear for the current region,
// dates and filters. Data comes from /api/stats, which uses the GFW Python
// package's create_fishing_effort_report().
// The chart is plain HTML: each bar is a <div> whose width is a percentage.
// =========================================================================

const statsChart = document.getElementById("stats-chart");
const statsCaption = document.getElementById("stats-caption");
const groupButtons = document.querySelectorAll(".toggle button");
const tooltip = document.getElementById("tooltip");

let groupBy = "FLAG";
let latestRequest = 0; // lets us ignore slow, out-of-date replies

const formatHours = (h) => Math.round(h).toLocaleString("en-GB");

// Short form for the value next to each bar: 59400 -> "59.4k"
function compact(h) {
  if (h >= 1e6) return `${(h / 1e6).toFixed(1)}M`;
  if (h >= 1e3) return `${(h / 1e3).toFixed(1)}k`;
  return String(Math.round(h));
}

async function loadStats() {
  const f = currentFilters(); // from app.js
  if (!f.start || !f.end || f.end <= f.start) return;

  const params = new URLSearchParams({ start: f.start, end: f.end, group_by: groupBy });
  addAreaParams(params); // from app.js: preset region or drawn box
  if (f.flag) params.set("flag", f.flag);
  if (f.gear) params.set("gear", f.gear);

  const requestId = ++latestRequest;
  statsChart.classList.add("loading");
  statsCaption.textContent = `Loading ${f.regionName}…`;

  try {
    const response = await fetch(`/api/stats?${params}`);
    const data = await response.json();
    if (requestId !== latestRequest) return; // a newer request has started; drop this one
    if (!response.ok) throw new Error(data.detail || "Request failed");

    statsCaption.textContent =
      `${f.regionName} · ${formatHours(data.totalHours)} hours · ${f.start} to ${f.end}`;
    drawChart(data.rows, data.totalHours);
  } catch (err) {
    if (requestId !== latestRequest) return;
    statsCaption.textContent = `Could not load stats: ${err.message}`;
    statsChart.innerHTML = "";
  } finally {
    if (requestId === latestRequest) statsChart.classList.remove("loading");
  }
}

function drawChart(rows, total) {
  statsChart.innerHTML = "";
  if (rows.length === 0) {
    statsChart.innerHTML = `<p class="empty">No fishing recorded for these filters.</p>`;
    return;
  }
  const max = Math.max(...rows.map((r) => r.hours));

  rows.forEach((row) => {
    const share = total ? ((row.hours / total) * 100).toFixed(1) : 0;
    const el = document.createElement("div");
    el.className = "bar-row";
    el.innerHTML = `
      <span class="bar-label"></span>
      <div class="bar-track"><div class="bar" style="width:${(row.hours / max) * 100}%"></div></div>
      <span class="bar-value">${compact(row.hours)}</span>`;
    el.querySelector(".bar-label").textContent = row.label; // textContent = safe, no HTML

    // Hover tooltip with the exact numbers
    el.addEventListener("mousemove", (event) => {
      tooltip.innerHTML = `<strong></strong>${formatHours(row.hours)} hours · ${share}% of total<br>${row.vessels.toLocaleString("en-GB")} vessels`;
      tooltip.querySelector("strong").textContent = row.label;
      tooltip.style.left = `${event.clientX + 14}px`;
      tooltip.style.top = `${event.clientY + 14}px`;
      tooltip.hidden = false;
    });
    el.addEventListener("mouseleave", () => { tooltip.hidden = true; });

    statsChart.appendChild(el);
  });
}

// "By flag" / "By gear" toggle
groupButtons.forEach((button) => {
  button.addEventListener("click", () => {
    groupBy = button.dataset.group;
    groupButtons.forEach((b) => b.classList.toggle("active", b === button));
    loadStats();
  });
});

// Reload whenever app.js says the dates, filters or region changed
document.addEventListener("filters-changed", loadStats);
