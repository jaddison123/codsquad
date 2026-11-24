"""
The backend: a small web server that sits between the browser and Global Fishing Watch.

Why it exists: your API token must stay secret. The browser talks only to THIS
server, and this server adds the token when it talks to GFW.

Run it (from the backend folder, with the virtual environment active):
    uvicorn app:app --reload
Then open http://127.0.0.1:8000 in your browser.
"""

import asyncio
import csv
import io
import os
import re
import struct
import zipfile
import zlib
from datetime import date, timedelta
from pathlib import Path

import gfwapiclient as gfw
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

import chartdata
from cache import TTLCache
import spreadsheet
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

# GFW lets a token run only ONE report at a time (it answers "429 Too Many
# Requests" otherwise). The stats chart and the downloads both use reports, so
# they queue up here and take turns instead of failing.
report_lock = asyncio.Lock()
REPORT_RETRY_SECONDS = 3
REPORT_MAX_WAIT = 90  # seconds to keep retrying while GFW is busy

# Remember GFW's answers for a while (see cache.py), so repeat views are
# near-instant and e.g. "Charts" then "Excel" for the same choices only asks
# GFW once. Six hours is short enough to pick up GFW's late-arriving data.
CACHE_SECONDS = 6 * 60 * 60
heatmap_cache = TTLCache(max_items=500, ttl=CACHE_SECONDS)
tile_cache = TTLCache(max_items=10_000, ttl=CACHE_SECONDS)    # tiles are ~2-3 KB each
stats_cache = TTLCache(max_items=500, ttl=CACHE_SECONDS)
report_cache = TTLCache(max_items=30, ttl=CACHE_SECONDS, max_bytes=200 * 1024 * 1024)  # CSVs can be big


def gfw_busy(result) -> bool:
    """True if GFW said "429 Too Many Requests" (a report is still running)."""
    if isinstance(result, httpx.Response):
        return result.status_code == 429
    return "429" in str(result)  # the GFW Python package raises an error instead


async def queued_report(make_request):
    """
    Run one GFW report at a time. If GFW still says it's busy (e.g. a report
    from a closed browser tab is still finishing on its side), wait and retry.
    `make_request` is a function that starts the request, so it can be retried.
    """
    async with report_lock:
        waited = 0
        while True:
            try:
                result = await make_request()
                if not gfw_busy(result) or waited >= REPORT_MAX_WAIT:
                    return result
            except Exception as err:
                if not gfw_busy(err) or waited >= REPORT_MAX_WAIT:
                    raise
            await asyncio.sleep(REPORT_RETRY_SECONDS)
            waited += REPORT_RETRY_SECONDS
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
    cache_key = (start, end, filter_text)
    cached = heatmap_cache.get(cache_key)
    if cached is not None:
        return cached

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
        heatmap_cache.set(cache_key, {"tileUrl": None})
        return {"tileUrl": None}
    if r.is_error:
        raise HTTPException(status_code=502, detail=f"GFW style request failed: {r.text}")

    # GFW's URL looks like https://.../tile/heatmap/{z}/{x}/{y}?style=...&date-range=...
    # We keep only the part after "?" and point the map at our own /api/tiles route.
    query_string = r.json()["url"].split("?", 1)[1]
    result = {"tileUrl": f"/api/tiles/{{z}}/{{x}}/{{y}}?{query_string}"}
    heatmap_cache.set(cache_key, result)
    return result


def make_transparent_png() -> bytes:
    """Build a valid 1x1 fully transparent PNG (with correct checksums)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)  # 1x1, 8-bit RGBA
    pixels = zlib.compress(b"\x00" + b"\x00\x00\x00\x00")  # filter byte + one clear pixel
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


TRANSPARENT_PNG = make_transparent_png()


@app.get("/api/tiles/{z}/{x}/{y}")
async def tile(z: int, x: int, y: int, request: Request):
    """Forward map tile requests to Global Fishing Watch."""
    cache_key = f"{request.url.path}?{request.url.query}"
    cached = tile_cache.get(cache_key)
    if cached is not None:
        content, media_type, max_age = cached
        return Response(content=content, media_type=media_type,
                        headers={"Cache-Control": f"public, max-age={max_age}"})

    try:
        r = await gfw_http.get(
            f"{GFW_4WINGS}/tile/heatmap/{z}/{x}/{y}",
            params=list(request.query_params.multi_items()),
        )

        # A missing or empty tile means an area with no fishing data.
        # Return a transparent PNG instead of displaying a broken tile.
        if r.status_code in (204, 404) or (r.status_code == 200 and not r.content):
            tile_cache.set(cache_key, (TRANSPARENT_PNG, "image/png", 300), size=len(TRANSPARENT_PNG))
            return Response(
                content=TRANSPARENT_PNG,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=300"},
            )

        if r.is_error:
            raise HTTPException(
                status_code=502,
                detail=f"Global Fishing Watch tile request failed: {r.status_code}"
            )

        media_type = r.headers.get("content-type", "image/png")
        tile_cache.set(cache_key, (r.content, media_type, 3600), size=len(r.content))
        return Response(
            content=r.content,
            status_code=200,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    except httpx.RequestError as err:
        raise HTTPException(
            status_code=502,
            detail=f"Could not contact Global Fishing Watch: {str(err)}"
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


# A drawn box at most this big (in square degrees) gets the same download limits
# as a preset region. It's about the size of the biggest preset (Mediterranean).
MAX_REGION_AREA = 1000


def pick_area(region: str | None, bbox: str | None) -> tuple[list[float], bool]:
    """
    Work out which area the user chose: a preset region, a box they drew on the
    map ("west,south,east,north"), or the whole world.
    Returns (bbox, is_small): is_small is True when the area is region-sized.
    """
    if region and bbox:
        raise HTTPException(status_code=400, detail="Choose a region or a drawn box, not both.")
    if bbox:
        try:
            west, south, east, north = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(status_code=400, detail="bbox must be four numbers: west,south,east,north.")
        # Written this way round so "nan" fails too (any comparison with nan is False)
        if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
            raise HTTPException(status_code=400, detail="The drawn box is off the map or has no size.")
        area = (east - west) * (north - south)
        return [west, south, east, north], area <= MAX_REGION_AREA
    if region:
        if region not in REGIONS:
            raise HTTPException(status_code=400, detail=f"Unknown region: {region}")
        return REGIONS[region]["bbox"], True
    return WORLD_BBOX, False


@app.get("/api/stats")
async def stats(
    start: date,
    end: date,
    group_by: str = "FLAG",
    region: str | None = None,
    bbox: str | None = None,
    flag: str | None = None,
    gear: str | None = None,
):
    """Total fishing hours per flag (or gear type) in the area, biggest first."""
    if group_by not in ("FLAG", "GEARTYPE"):
        raise HTTPException(status_code=400, detail="group_by must be FLAG or GEARTYPE.")
    area, _ = pick_area(region, bbox)
    filter_text = build_filter(start, end, flag, gear)
    cache_key = (start, end, group_by, tuple(area), filter_text)
    cached = stats_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        result = await queued_report(lambda: gfw_client.fourwings.create_fishing_effort_report(
            spatial_resolution="LOW",
            temporal_resolution="ENTIRE",   # one total for the whole date range...
            spatial_aggregation=True,       # ...and for the whole area (no grid cells)
            group_by=group_by,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            geojson=bbox_to_geojson(area),
            filters=[filter_text] if filter_text else None,
        ))
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
    answer = {"rows": top, "totalHours": sum(r["hours"] for r in rows)}
    stats_cache.set(cache_key, answer)
    return answer


# ---------------------------------------------------------------------------
# 6. Download detailed fishing activity as CSV
#
# Each row is one grid cell for one day (or one hour), so the file grows fast:
# a whole-world, 90-day daily download is ~190 MB and takes GFW over 100
# seconds, and hourly files have roughly 4x the rows of daily ones. So the
# longest allowed date range depends on the detail level and on whether a
# region is selected.
# ---------------------------------------------------------------------------
# resolution -> (max days with a region, max days for the whole world)
DOWNLOAD_LIMITS = {
    "DAILY": (90, 7),
    "HOURLY": (30, 2),
}


def sort_csv_by_time(csv_bytes: bytes) -> bytes:
    """GFW returns rows in no particular order; sort them by time, then position."""
    lines = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    if not lines:
        return csv_bytes
    header, rows = lines[0], lines[1:]
    if "Time Range" not in header:
        return csv_bytes
    t = header.index("Time Range")
    lat = header.index("Lat") if "Lat" in header else None
    lon = header.index("Lon") if "Lon" in header else None

    def key(row):
        def num(i):
            try:
                return float(row[i]) if i is not None else 0.0
            except (ValueError, IndexError):
                return 0.0
        return (row[t] if t < len(row) else "", num(lat), num(lon))

    rows.sort(key=key)
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows([header, *rows])
    return out.getvalue().encode("utf-8")


@app.get("/api/download")
async def download_fishing_data(
    start: date,
    end: date,
    region: str | None = None,
    bbox: str | None = None,
    flag: str | None = None,
    gear: str | None = None,
    resolution: str = "DAILY",
    format: str = "xlsx",
):
    """
    Download fishing-effort records for the selected date range, one row per
    grid cell per day (resolution=DAILY) or per hour (resolution=HOURLY).
    These are grid-cell totals, not individual AIS positions.

    format: "xlsx" (easy-to-read Excel), "csv" (raw), or "chart" (JSON totals
    for the chart view in the site, so nothing is downloaded to disk).
    """
    if resolution not in DOWNLOAD_LIMITS:
        raise HTTPException(status_code=400, detail="resolution must be DAILY or HOURLY.")
    if format not in ("xlsx", "csv", "chart"):
        raise HTTPException(status_code=400, detail="format must be xlsx, csv or chart.")
    if end <= start:
        raise HTTPException(status_code=400, detail="End date must be after start date.")
    area, is_small = pick_area(region, bbox)
    region_days, world_days = DOWNLOAD_LIMITS[resolution]
    max_days = region_days if is_small else world_days
    if end - start > timedelta(days=max_days):
        level = resolution.lower()
        if is_small:
            detail = f"{level.capitalize()} downloads are limited to {max_days} days."
        else:
            where = "Large-area" if bbox else "Whole-world"
            detail = (f"{where} {level} downloads are limited to {max_days} days. "
                      f"Pick a region or draw a smaller box for up to {region_days} days.")
        raise HTTPException(status_code=400, detail=detail)

    filter_text = build_filter(start, end, flag, gear)
    report_params = {
        "format": "CSV",
        "datasets[0]": FISHING_DATASET,
        "date-range": f"{start.isoformat()},{end.isoformat()}",
        "spatial-resolution": "LOW",
        "spatial-aggregation": "false",
        "temporal-resolution": resolution,
        "group-by": "FLAGANDGEARTYPE",
    }
    if filter_text:
        report_params["filters[0]"] = filter_text

    # The same GFW data feeds all three formats, so it's cached before the
    # Excel/CSV/chart step: switching format for the same choices skips GFW.
    cache_key = (start, end, tuple(area), filter_text, resolution)
    csv_content = report_cache.get(cache_key)
    if csv_content is None:
        try:
            report = await queued_report(lambda: gfw_http.post(
                f"{GFW_4WINGS}/report",
                params=report_params,
                json={"geojson": bbox_to_geojson(area)},
                headers={"Accept": "application/zip, text/csv, application/json"},
                timeout=100,
            ))
        except httpx.RequestError as err:
            raise HTTPException(status_code=502, detail=f"Could not contact Global Fishing Watch: {err}")

        if report.is_error:
            detail = report.text[:1000]
            raise HTTPException(
                status_code=502,
                detail=f"Global Fishing Watch report failed ({report.status_code}): {detail}",
            )

        content = report.content
        csv_content = content
        if zipfile.is_zipfile(io.BytesIO(content)):
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    csv_files = [name for name in archive.namelist() if name.lower().endswith(".csv")]
                    if not csv_files:
                        raise HTTPException(status_code=502, detail="GFW returned a ZIP file without a CSV.")
                    csv_content = archive.read(csv_files[0])
            except zipfile.BadZipFile:
                raise HTTPException(status_code=502, detail="GFW returned an invalid ZIP file.")

        if not csv_content or b"," not in csv_content[:4096]:
            raise HTTPException(status_code=502, detail="GFW did not return a readable CSV report.")

        csv_content = sort_csv_by_time(csv_content)
        report_cache.set(cache_key, csv_content, size=len(csv_content))
    filename = f"fishing_activity_{resolution.lower()}_{start.isoformat()}_to_{end.isoformat()}"

    if format == "csv":
        return Response(
            content=csv_content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )

    # Excel workbook and chart view both describe the choices in words
    if region:
        area_name = REGIONS[region]["name"]
    elif bbox:
        w, s, e, n = area
        area_name = f"Drawn area ({s:g}° to {n:g}° latitude, {w:g}° to {e:g}° longitude)"
    else:
        area_name = "Whole world"
    meta = {
        "area": area_name,
        "start": start,
        "last_day": end - timedelta(days=1),  # GFW's end date is not included
        "flag": FLAGS[flag] if flag else "All countries",
        "gear": GEAR_TYPES[gear] if gear else "All methods",
        "hourly": resolution == "HOURLY",
    }

    if format == "chart":
        # Totals for the in-site chart view (see chartdata.py); FastAPI sends it as JSON
        return await asyncio.to_thread(chartdata.build_chart_data, csv_content, meta, area)

    # Easy-to-read Excel workbook (see spreadsheet.py)
    try:
        # Building a big workbook takes a few seconds; run it off the main thread
        # so the map tiles keep loading for everyone else meanwhile.
        xlsx = await asyncio.to_thread(spreadsheet.build_workbook, csv_content, meta)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=f"{err} Choose a smaller area or a shorter date range.")
    return Response(
        content=xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
    )


# ---------------------------------------------------------------------------
# 7. Serve the website itself (the frontend folder) at http://127.0.0.1:8000/
#    This must come LAST so it doesn't hide the /api routes above.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def revalidate_site_files(request: Request, call_next):
    """
    Make browsers check for a newer app.js / style.css / index.html on every
    page load, so a new deploy shows up without a hard refresh. Unchanged files
    still come back as a quick "304 Not Modified", so this costs very little.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class CompressText:
    """
    Gzip text-like responses (pages, scripts, JSON, CSV), which shrinks them
    4-10x. Map tiles and Excel files are already compressed, so those are
    passed through untouched instead of wasting time squeezing them again.
    """

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1000, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self._worth_compressing(scope):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    @staticmethod
    def _worth_compressing(scope) -> bool:
        path = scope["path"]
        if path.startswith("/api/tiles/"):
            return False  # PNG images
        if path == "/api/download":
            query = scope.get("query_string", b"")
            return b"format=csv" in query or b"format=chart" in query  # not Excel (the default)
        return True


app.add_middleware(CompressText)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
