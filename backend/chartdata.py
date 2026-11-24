"""
Turns GFW's raw fishing CSV into the small JSON summary the in-site chart view
draws (frontend/charts.js). Same data as the Excel download, just summed up:

  - key facts (total hours, busiest day, top country, top method)
  - hours per day, per country and per fishing method
  - a grid of fishing "hotspots" for the hotspot map (hours per map square)
  - a day x country table for the "countries by day" grid

The browser only ever receives a few thousand numbers, however big the area.
"""

import math
from collections import defaultdict

from countries import country_name
from spreadsheet import GEAR_EXPLAINED, gear_name, read_rows, top_with_other

MAX_CELLS = 2500        # most squares the hotspot map should draw (more gets slow and cluttered)
TOP_COUNTRIES = 8    # rows in the "countries by day" grid
# Square sizes to try for the hotspot map, in degrees, smallest first.
# GFW's own squares are 0.1°; bigger areas get merged into bigger squares.
CELL_SIZES = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10]


def hotspot_grid(rows, bbox):
    """
    Add up the hours in each map square, using the smallest square size that
    keeps the number of squares under MAX_CELLS.
    Returns (size_in_degrees, [[centre_lon, centre_lat, hours], ...]).
    """
    for size in CELL_SIZES:
        cells = defaultdict(float)
        for r in rows:
            # Which square is this point in? (floor works for negatives too)
            cells[(math.floor(r["lon"] / size + 1e-9), math.floor(r["lat"] / size + 1e-9))] += r["hours"]
        if len(cells) <= MAX_CELLS or size == CELL_SIZES[-1]:
            break

    west, south, east, north = bbox
    grid = []
    for (x, y), hours in cells.items():
        lon, lat = (x + 0.5) * size, (y + 0.5) * size  # centre of the square
        if west <= lon <= east and south <= lat <= north and hours > 0:
            grid.append([round(lon, 3), round(lat, 3), round(hours, 1)])
    return size, grid


def build_chart_data(csv_bytes: bytes, meta: dict, bbox: list[float]) -> dict:
    rows = read_rows(csv_bytes)
    result = {
        "area": meta["area"],
        "start": meta["start"].isoformat(),
        "lastDay": meta["last_day"].isoformat(),
        "flag": meta["flag"],
        "gear": meta["gear"],
        "bbox": bbox,
        "totalHours": 0,
    }
    if not rows:
        return result

    total = sum(r["hours"] for r in rows)
    by_flag, by_gear, by_day = defaultdict(float), defaultdict(float), defaultdict(float)
    by_day_flag = defaultdict(float)
    for r in rows:
        flag = country_name(r["flag"])
        by_flag[flag] += r["hours"]
        by_gear[r["gear"]] += r["hours"]
        by_day[r["date"]] += r["hours"]
        by_day_flag[(r["date"], flag)] += r["hours"]

    days = sorted(by_day)
    top_countries = [name for name, _ in sorted(by_flag.items(), key=lambda kv: kv[1], reverse=True)]
    top_countries = top_countries[:TOP_COUNTRIES]
    cell_size, cells = hotspot_grid(rows, bbox)

    result.update({
        "totalHours": round(total),
        "byDay": [{"date": d.isoformat(), "hours": round(by_day[d], 1)} for d in days],
        "byCountry": [{"name": n, "hours": round(h, 1)} for n, h in top_with_other(by_flag)],
        "byGear": [
            {"name": gear_name(code), "explain": GEAR_EXPLAINED.get(code, ""), "hours": round(h, 1)}
            for code, h in sorted(by_gear.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "cellSize": cell_size,
        "cells": cells,
        "countryDay": {
            "countries": top_countries,
            "days": [d.isoformat() for d in days],
            # [day index, country index, hours]
            "values": [
                [di, ci, round(by_day_flag.get((d, c), 0), 1)]
                for di, d in enumerate(days)
                for ci, c in enumerate(top_countries)
            ],
        },
    })
    return result
