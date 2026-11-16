"""
The backend: a small web server that sits between the browser and Global Fishing Watch.

Why it exists: your API token must stay secret. The browser talks only to THIS
server, and this server adds the token when it talks to GFW.

Run it (from the backend folder, with the virtual environment active):
    uvicorn app:app --reload
Then open http://127.0.0.1:8000 in your browser.
"""

import asyncio
import os
import re
from datetime import date
from pathlib import Path

import gfwapiclient as gfw
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles

import vessels
from options import FLAGS, GEAR_TYPES, REGIONS

# ---------------------------------------------------------------------------
# 1. Setup: load the secret token and create the clients that talk to GFW
# ---------------------------------------------------------------------------
load_dotenv()  # reads backend/.env into environment variables
TOKEN = os.getenv("GFW_API_ACCESS_TOKEN")
if not TOKEN or TOKEN == "paste-your-token-here":
    raise RuntimeError("No GFW token found. Copy .env.example to .env and paste your token in.")

# The official GFW Python package. We use it for vessels, events and reports.
gfw_client = gfw.Client(access_token=TOKEN)

# A plain HTTP client for the map tiles (the Python package has no tile function).
# The token is attached here, on the server, so the browser never sees it.
GFW_4WINGS = "https://gateway.api.globalfishingwatch.org/v3/4wings"
gfw_http = httpx.AsyncClient(
    headers={"Authorization": f"Bearer {TOKEN}"},
    timeout=60,
)

FISHING_DATASET = "public-global-fishing-effort:latest"
HEATMAP_COLOR = "#00e6c3"  # teal glow, similar to GFW's own fishing layer

app = FastAPI(title="Fishing Activity Map API")


# ---------------------------------------------------------------------------
# 2. Health checks
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    """Simple check that the server is running."""
    return {"status": "ok"}


@app.get("/api/token-check")
async def token_check():
    """Makes one tiny real call to GFW to prove the token works."""
    try:
        result = await gfw_client.vessels.search_vessels(query="DON TITO", limit=1)
        return {"token_works": True, "example_results": len(result.data())}
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"GFW call failed: {err}")


# ---------------------------------------------------------------------------
# 3. Fishing heatmap
#
# How GFW map tiles work (two steps):
#   a) Ask GFW for a "style" for our date range. GFW works out a colour scale
#      that suits the data and gives back a tile URL template.
#   b) The map then requests lots of small 256x256 PNG images ("tiles"), one per
#      square of the map, using that template: .../{z}/{x}/{y}
#      (z = zoom level, x/y = which square).
# The browser calls OUR tile endpoint, and we forward each tile request to GFW
# with the token added.
# ---------------------------------------------------------------------------
@app.get("/api/options")
async def options():
    """The dropdown choices for the sidebar (flags, gear types, regions)."""
    return {"flags": FLAGS, "gearTypes": GEAR_TYPES, "regions": REGIONS}


def build_filter(start: date, end: date, flag: str | None, gear: str | None) -> str | None:
    """
    Check the user's choices and turn them into a GFW filter, e.g.
        flag in ('ESP') AND geartype in ('trawlers')
    Only values from our own lists are accepted. The filter is text that GFW
    reads like a database query, so we never paste in anything unchecked.
    Used by both the heatmap and the stats chart, so they always match.
    """
    if end <= start:
        raise HTTPException(status_code=400, detail="End date must be after start date.")
    conditions = []
    if flag:
        if flag not in FLAGS:
            raise HTTPException(status_code=400, detail=f"Unknown flag: {flag}")
        conditions.append(f"flag in ('{flag}')")
    if gear:
        if gear not in GEAR_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown gear type: {gear}")
        conditions.append(f"geartype in ('{gear}')")
    return " AND ".join(conditions) or None


@app.get("/api/heatmap")
async def heatmap(start: date, end: date, flag: str | None = None, gear: str | None = None):
    """
    Return a tile URL template (pointing at our own server) for the date range,
    optionally filtered to one flag state and/or one gear type.
    """
    filter_text = build_filter(start, end, flag, gear)

    params = {
        "interval": "DAY",
        "datasets[0]": FISHING_DATASET,
        "date-range": f"{start},{end}",
        "color": HEATMAP_COLOR,
    }
    if filter_text:
        params["filters[0]"] = filter_text

    r = await gfw_http.post(f"{GFW_4WINGS}/generate-png", params=params)
    if r.status_code == 204:  # GFW's way of saying "no data matches"
        return {"tileUrl": None}
    if r.is_error:
        raise HTTPException(status_code=502, detail=f"GFW style request failed: {r.text}")

    # GFW's URL looks like https://.../tile/heatmap/{z}/{x}/{y}?style=...&date-range=...
    # We keep only the part after "?" and point the map at our own /api/tiles route.
    query_string = r.json()["url"].split("?", 1)[1]
    return {"tileUrl": f"/api/tiles/{{z}}/{{x}}/{{y}}?{query_string}"}


@app.get("/api/tiles/{z}/{x}/{y}")
async def tile(z: int, x: int, y: int, request: Request):
    """Forward one map tile request to GFW, adding the secret token."""
    r = await gfw_http.get(
        f"{GFW_4WINGS}/tile/heatmap/{z}/{x}/{y}",
        params=list(request.query_params.multi_items()),
    )
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "image/png"),
        headers={"Cache-Control": "public, max-age=3600"},  # browser may reuse tiles for 1 hour
    )


# ---------------------------------------------------------------------------
# 4. Vessel search and details (using the official GFW Python package)
# ---------------------------------------------------------------------------
@app.get("/api/vessels/search")
async def search_vessels(q: str):
    """Search by name, MMSI, IMO or call sign. Returns a short list of matches."""
    q = q.strip()
    if len(q) < 3:
        raise HTTPException(status_code=400, detail="Type at least 3 characters.")
    try:
        result = await gfw_client.vessels.search_vessels(
            query=q,
            limit=20,
            datasets=[vessels.VESSEL_DATASET],
        )
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"GFW vessel search failed: {err}")

    # .data() gives Python objects; model_dump() turns each into a plain dict
    records = [r.model_dump(mode="json") for r in result.data()]
    return {"results": [vessels.summarise(r) for r in records]}


EVENT_LIMIT = 1000  # max events of each type, to keep the map responsive


# NOTE: this route must come BEFORE /api/vessels/{vessel_id}, otherwise FastAPI
# would think "events" is a vessel id.
@app.get("/api/vessels/events")
async def vessel_events(ids: str):
    """
    Fishing events and port visits for a vessel. `ids` is a comma-separated list
    of the vessel's AIS identity ids (a renamed ship has more than one).
    """
    id_list = [i for i in ids.split(",") if i]
    if not id_list or not all(re.fullmatch(r"[0-9a-f-]{10,64}", i) for i in id_list):
        raise HTTPException(status_code=400, detail="Invalid vessel id.")

    async def fetch(dataset):
        result = await gfw_client.events.get_all_events(
            datasets=[dataset],
            vessels=id_list,
            start_date="2012-01-01",
            end_date=date.today().isoformat(),
            limit=EVENT_LIMIT,
            sort="-start",  # newest first, so the limit cuts off the OLDEST events
        )
        return [vessels.simplify_event(e.model_dump(mode="json")) for e in result.data()]

    try:
        # asyncio.gather runs both requests at the same time instead of one after the other
        fishing, ports = await asyncio.gather(
            fetch(vessels.EVENT_DATASETS["fishing"]),
            fetch(vessels.EVENT_DATASETS["port_visit"]),
        )
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"GFW events request failed: {err}")

    return {
        "fishing": fishing,
        "portVisits": ports,
        "truncated": len(fishing) >= EVENT_LIMIT or len(ports) >= EVENT_LIMIT,
    }


@app.get("/api/vessels/{vessel_id}")
async def vessel_details(vessel_id: str):
    """Full details for one vessel, including owners and fishing licences."""
    try:
        result = await gfw_client.vessels.get_vessel_by_id(
            id=vessel_id,
            dataset=vessels.VESSEL_DATASET,
        )
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"GFW vessel lookup failed: {err}")
    return vessels.details(result.data().model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 5. Stats: fishing hours by flag or gear inside a region (GFW Python package)
# ---------------------------------------------------------------------------
WORLD_BBOX = [-180.0, -85.0, 180.0, 85.0]


def bbox_to_geojson(bbox):
    """[west, south, east, north] -> a GeoJSON rectangle (a closed ring of 5 corners)."""
    w, s, e, n = bbox
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


@app.get("/api/stats")
async def stats(
    start: date,
    end: date,
    group_by: str = "FLAG",
    region: str | None = None,
    flag: str | None = None,
    gear: str | None = None,
):
    """Total fishing hours per flag (or gear type) in the region, biggest first."""
    if group_by not in ("FLAG", "GEARTYPE"):
        raise HTTPException(status_code=400, detail="group_by must be FLAG or GEARTYPE.")
    if region and region not in REGIONS:
        raise HTTPException(status_code=400, detail=f"Unknown region: {region}")
    filter_text = build_filter(start, end, flag, gear)
    bbox = REGIONS[region]["bbox"] if region else WORLD_BBOX

    try:
        result = await gfw_client.fourwings.create_fishing_effort_report(
            spatial_resolution="LOW",
            temporal_resolution="ENTIRE",   # one total for the whole date range...
            spatial_aggregation=True,       # ...and for the whole area (no grid cells)
            group_by=group_by,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            geojson=bbox_to_geojson(bbox),
            filters=[filter_text] if filter_text else None,
        )
    except Exception as err:
        raise HTTPException(status_code=502, detail=f"GFW report failed: {err}")

    rows = []
    for item in result.data():
        row = item.model_dump()
        code = row.get("flag") if group_by == "FLAG" else row.get("gear_type")
        if group_by == "FLAG":
            label = FLAGS.get(code, code) if code else "Unknown flag"
        else:
            label = GEAR_TYPES.get(code, (code or "Unknown gear").replace("_", " ").capitalize())
        rows.append({"code": code, "label": label,
                     "hours": round(row.get("hours") or 0), "vessels": row.get("vessel_ids") or 0})
    rows.sort(key=lambda r: r["hours"], reverse=True)

    # Keep the chart readable: top 10, everything else folded into "Other"
    top, rest = rows[:10], rows[10:]
    if rest:
        top.append({"code": None, "label": f"Other ({len(rest)})",
                    "hours": sum(r["hours"] for r in rest),
                    "vessels": sum(r["vessels"] for r in rest)})
    return {"rows": top, "totalHours": sum(r["hours"] for r in rows)}


# ---------------------------------------------------------------------------
# 6. Serve the website itself (the frontend folder) at http://127.0.0.1:8000/
#    This must come LAST so it doesn't hide the /api routes above.
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
