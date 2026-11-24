"""
Builds the small JSON summary the in-site chart view draws (frontend/charts.js).

Unlike the Excel/CSV downloads, the charts never fetch the raw data (one row
per map square, per day, per country, per fishing method), which for a busy
region like the East China Sea can take GFW well over 15 minutes to build.
Instead GFW adds up the totals itself, in three small reports:

  1. hours per day for each country  -> daily chart, countries chart, country-by-day grid
  2. hours per fishing method         -> fishing methods chart
  3. hours per map square             -> hotspot map

This module turns those three lists of rows into what the page draws.
"""

import math
from collections import defaultdict

from countries import country_name
from spreadsheet import GEAR_EXPLAINED, gear_name, top_with_other

MAX_CELLS = 2500        # most squares the hotspot map should draw (more gets slow and cluttered)
TOP_COUNTRIES = 8       # rows in the "countries by day" grid
# Square sizes to try for the hotspot map, in degrees, smallest first.
# GFW's own squares are 0.1°; bigger areas get merged into bigger squares.
CELL_SIZES = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10]


def hotspot_grid(cells, bbox):
    """
    Add up the hours in each map square, using the smallest square size that
    keeps the number of squares under MAX_CELLS.
    `cells` is a list of (lon, lat, hours).
    Returns (size_in_degrees, [[centre_lon, centre_lat, hours], ...]).
    """
    for size in CELL_SIZES:
        grid = defaultdict(float)
        for lon, lat, hours in cells:
            # Which square is this point in? (floor works for negatives too)
            grid[(math.floor(lon / size + 1e-9), math.floor(lat / size + 1e-9))] += hours
        if len(grid) <= MAX_CELLS or size == CELL_SIZES[-1]:
            break

    west, south, east, north = bbox
    result = []
    for (x, y), hours in grid.items():
        lon, lat = (x + 0.5) * size, (y + 0.5) * size  # centre of the square
        if west <= lon <= east and south <= lat <= north and hours > 0:
            result.append([round(lon, 3), round(lat, 3), round(hours, 1)])
    return size, result


def build_chart_data(day_flag_rows, gear_rows, cell_rows, meta: dict, bbox: list[float]) -> dict:
    """
    day_flag_rows: [{"date", "flag", "hours"}, ...]  (report 1)
    gear_rows:     [{"gear_type", "hours"}, ...]     (report 2)
    cell_rows:     [{"lat", "lon", "hours"}, ...]    (report 3)
    """
    result = {
        "area": meta["area"],
        "start": meta["start"].isoformat(),
        "lastDay": meta["last_day"].isoformat(),
        "flag": meta["flag"],
        "gear": meta["gear"],
        "bbox": bbox,
        "totalHours": 0,
    }

    by_flag, by_day, by_day_flag = defaultdict(float), defaultdict(float), defaultdict(float)
    for r in day_flag_rows:
        hours, day = r.get("hours") or 0, (r.get("date") or "")[:10]  # "2026-10-01"
        if not day or not hours:
            continue
        flag = country_name(r.get("flag"))
        by_flag[flag] += hours
        by_day[day] += hours
        by_day_flag[(day, flag)] += hours

    total = sum(by_day.values())
    if not total:
        return result

    by_gear = defaultdict(float)
    for r in gear_rows:
        by_gear[r.get("gear_type") or ""] += r.get("hours") or 0

    cells = [(r["lon"], r["lat"], r.get("hours") or 0)
             for r in cell_rows if r.get("lon") is not None and r.get("lat") is not None]

    days = sorted(by_day)
    top_countries = sorted(by_flag, key=by_flag.get, reverse=True)[:TOP_COUNTRIES]
    cell_size, grid = hotspot_grid(cells, bbox)

    result.update({
        "totalHours": round(total),
        "byDay": [{"date": d, "hours": round(by_day[d], 1)} for d in days],
        "byCountry": [{"name": n, "hours": round(h, 1)} for n, h in top_with_other(by_flag)],
        "byGear": [
            {"name": gear_name(code), "explain": GEAR_EXPLAINED.get(code, ""), "hours": round(h, 1)}
            for code, h in sorted(by_gear.items(), key=lambda kv: kv[1], reverse=True) if h > 0
        ],
        "cellSize": cell_size,
        "cells": grid,
        "countryDay": {
            "countries": top_countries,
            "days": days,
            # [day index, country index, hours]
            "values": [
                [di, ci, round(by_day_flag.get((d, c), 0), 1)]
                for di, d in enumerate(days)
                for ci, c in enumerate(top_countries)
            ],
        },
    })
    return result
