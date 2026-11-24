"""
Turns GFW's raw fishing CSV into an Excel workbook that anyone can read.

The workbook has three sheets:
  1. Summary          - the key facts in plain sentences, small tables and two charts
  2. Fishing records  - every row of the data, with clear column names, real
                        country names instead of codes, and a map link per row
  3. What this means  - what each column means, and what the data can't tell you

It uses openpyxl's "write-only" mode, which streams rows straight to the file
instead of holding the whole spreadsheet in memory, so big downloads stay fast.
"""

import csv
import io
from collections import defaultdict
from datetime import date

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill

from countries import country_name
from options import GEAR_TYPES

EXCEL_MAX_ROWS = 1_048_576  # Excel can't open a sheet with more rows than this

# What each fishing method actually is, in one plain sentence
GEAR_EXPLAINED = {
    "trawlers": "Pull a large net through the water or along the seabed.",
    "purse_seines": "Surround a shoal of fish with a net, then close the bottom like a drawstring purse.",
    "tuna_purse_seines": "Purse seiners that target tuna: they surround a shoal with a net and close it.",
    "other_purse_seines": "Purse seiners that target fish other than tuna, such as sardines or mackerel.",
    "drifting_longlines": "Let out a very long line with thousands of baited hooks that drifts with the current.",
    "set_longlines": "Lay a long line of baited hooks that is anchored to the seabed.",
    "set_gillnets": "Anchor a wall of net in place; fish swim into it and are caught by their gills.",
    "pots_and_traps": "Leave baited cages on the seabed to catch crabs, lobsters and similar.",
    "squid_jigger": "Shine bright lights at night to attract squid, then catch them on jiggling lines.",
    "pole_and_line": "Catch fish one at a time with a pole and line, usually tuna.",
    "trollers": "Tow baited lines or lures behind a slowly moving boat.",
    "dredge_fishing": "Drag a metal frame along the seabed to collect shellfish such as scallops.",
    "seiners": "Use a large net to encircle a shoal of fish.",
    "other_seines": "Use a net to encircle fish, often pulled in from the shore or a small boat.",
    "driftnets": "Let a wall of net float freely with the current.",
    "fixed_gear": "Leave gear in place, such as pots, traps or anchored nets (exact type not known).",
    "fishing": "A fishing boat whose exact fishing method is not known.",
}

# Colours that match the website
NAVY = "12263D"
TEAL = "00A88F"


def gear_name(code: str | None) -> str:
    """'set_gillnets' -> 'Set gillnets'"""
    if not code:
        return "Unknown"
    return GEAR_TYPES.get(code, code.replace("_", " ").capitalize())


def hours_in_words(hours: float) -> str:
    """59400 -> 'the same as one boat fishing non-stop for 6.8 years'"""
    days = hours / 24
    if days < 2:
        amount = f"{hours:,.0f} hours"
    elif days < 365 * 2:
        amount = f"{days:,.0f} days"
    else:
        amount = f"{days / 365:,.1f} years"
    return f"the same as one boat fishing non-stop for {amount}"


def nice_date(d: date) -> str:
    """date(2026, 10, 1) -> 'Thu 1 Oct 2026'"""
    return f"{d:%a} {d.day} {d:%b %Y}"


# ---------------------------------------------------------------------------
# Reading the CSV
# ---------------------------------------------------------------------------
def read_rows(csv_bytes: bytes) -> list[dict]:
    """Parse GFW's CSV into simple dicts, skipping any row that can't be read."""
    rows = []
    for r in csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))):
        # "2026-10-01" for daily data, "2026-10-01 05:00" for hourly
        day_text, _, time_text = (r.get("Time Range") or "").partition(" ")
        try:
            rows.append({
                "date": date.fromisoformat(day_text),
                "time": time_text,
                "flag": r.get("Flag") or "",
                "gear": r.get("Geartype") or "",
                "boats": int(float(r.get("Vessel IDs") or 0)),
                "hours": float(r.get("Apparent Fishing Hours") or 0),
                "lat": float(r["Lat"]),
                "lon": float(r["Lon"]),
            })
        except (ValueError, KeyError):
            continue
    return rows


def top_with_other(totals: dict, limit: int = 10) -> list[tuple]:
    """Biggest `limit` entries, with the rest added together as 'Other'."""
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    top, rest = ranked[:limit], ranked[limit:]
    if rest:
        top.append((f"Others ({len(rest)})", sum(v for _, v in rest)))
    return top


# ---------------------------------------------------------------------------
# Small helpers for styled cells (write-only mode needs WriteOnlyCell for styles)
# ---------------------------------------------------------------------------
class Sheet:
    """Wraps a write-only sheet so we can add styled rows and know the row number."""

    def __init__(self, workbook: Workbook, title: str, widths: list[int]):
        self.ws = workbook.create_sheet(title)
        self.row = 0
        for i, width in enumerate(widths):
            self.ws.column_dimensions[chr(ord("A") + i)].width = width

    def cell(self, value, *, bold=False, size=None, color=None, fill=None,
             fmt=None, wrap=False, italic=False):
        c = WriteOnlyCell(self.ws, value)
        if bold or size or color or italic:
            c.font = Font(bold=bold, size=size, color=color, italic=italic)
        if fill:
            c.fill = PatternFill("solid", fgColor=fill)
        if fmt:
            c.number_format = fmt
        if wrap:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        return c

    def add(self, *values) -> int:
        """Append a row (plain values or styled cells) and return its row number."""
        self.ws.append(list(values))
        self.row += 1
        return self.row

    def header(self, *titles) -> int:
        return self.add(*(self.cell(t, bold=True, color="FFFFFF", fill=NAVY) for t in titles))

    def heading(self, text) -> int:
        return self.add(self.cell(text, bold=True, size=13, color=TEAL))


def bar_chart(title, sheet, label_col, value_col, first_row, last_row, *, horizontal):
    """A simple one-colour bar chart for a table already written to `sheet`."""
    chart = BarChart()
    chart.type = "bar" if horizontal else "col"
    chart.title = title
    chart.legend = None
    chart.height, chart.width = 7.5, 16
    chart.add_data(Reference(sheet.ws, min_col=value_col, min_row=first_row, max_row=last_row))
    chart.set_categories(Reference(sheet.ws, min_col=label_col, min_row=first_row, max_row=last_row))
    chart.series[0].graphicalProperties.solidFill = TEAL
    chart.series[0].graphicalProperties.line.solidFill = TEAL
    chart.y_axis.number_format = "#,##0"
    chart.y_axis.majorGridlines = None
    # Newer Excel hides the axes of openpyxl charts unless told not to
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    if horizontal:
        chart.x_axis.scaling.orientation = "maxMin"  # biggest bar at the top
    return chart


# ---------------------------------------------------------------------------
# The three sheets
# ---------------------------------------------------------------------------
def write_summary(wb, rows, meta):
    s = Sheet(wb, "Summary", [34, 58, 16, 14])
    s.add(s.cell("Fishing activity report", bold=True, size=18, color=NAVY))
    if meta["start"] >= meta["last_day"]:
        when = f"on {nice_date(meta['start'])}"
    else:
        when = f"{nice_date(meta['start'])} to {nice_date(meta['last_day'])}"
    s.add(s.cell(f"{meta['area']} · {when}", size=12))
    s.add(s.cell(f"Boats from: {meta['flag']}   ·   Fishing method: {meta['gear']}",
                 italic=True, color="666666"))
    s.add()

    if not rows:
        s.add(s.cell("No fishing was recorded for these choices.", bold=True))
        s.add("Try a longer date range, a bigger area, or removing a filter.")
        return

    total = sum(r["hours"] for r in rows)
    by_flag, by_gear, by_day = defaultdict(float), defaultdict(float), defaultdict(float)
    for r in rows:
        by_flag[country_name(r["flag"])] += r["hours"]
        by_gear[r["gear"]] += r["hours"]
        by_day[r["date"]] += r["hours"]
    busiest_day, busiest_hours = max(by_day.items(), key=lambda kv: kv[1])
    top_flag, top_flag_hours = max(by_flag.items(), key=lambda kv: kv[1])
    top_gear, top_gear_hours = max(by_gear.items(), key=lambda kv: kv[1])

    # --- Key facts, as short sentences ---
    s.heading("Key facts")
    facts = [
        ("Total time spent fishing", f"{total:,.0f} hours"),
        ("To put that in perspective", f"That's {hours_in_words(total)}."),
        ("Busiest day", f"{nice_date(busiest_day)}, with {busiest_hours:,.0f} hours of fishing"),
        ("Country whose boats fished most",
         f"{top_flag}: {top_flag_hours / total:.0%} of all fishing here"),
        ("Most common fishing method",
         f"{gear_name(top_gear)}: {top_gear_hours / total:.0%} of all fishing here"),
    ]
    for label, text in facts:
        s.add(s.cell(label, bold=True), s.cell(text, wrap=True))
    s.add()

    # --- Fishing by country (table + chart) ---
    s.heading("Which countries' boats were fishing")
    s.header("Country", "", "Hours fishing", "Share")
    first = s.row + 1
    for name, hours in top_with_other(by_flag):
        s.add(name, "", s.cell(round(hours), fmt="#,##0"), s.cell(round(hours / total, 3), fmt="0%"))
    flag_rows = (first, s.row)
    s.add()

    # --- Fishing by method, with a plain explanation of each ---
    s.heading("How they were fishing")
    s.header("Fishing method", "What it means", "Hours fishing", "Share")
    for code, hours in sorted(by_gear.items(), key=lambda kv: kv[1], reverse=True):
        s.add(s.cell(gear_name(code), bold=True),
              s.cell(GEAR_EXPLAINED.get(code, ""), wrap=True),
              s.cell(round(hours), fmt="#,##0"), s.cell(round(hours / total, 3), fmt="0%"))
    s.add()

    # --- Fishing per day (table + chart) ---
    s.heading("Fishing day by day")
    s.header("Day", "", "Hours fishing", "")
    first = s.row + 1
    for day in sorted(by_day):
        s.add(nice_date(day), "", s.cell(round(by_day[day]), fmt="#,##0"))
    day_rows = (first, s.row)

    s.ws.add_chart(bar_chart("Hours fishing by country", s, 1, 3, *flag_rows, horizontal=True), "F6")
    s.ws.add_chart(bar_chart("Hours fishing per day", s, 1, 3, *day_rows, horizontal=False), "F23")


def write_records(wb, rows, hourly):
    columns = ["Date"] + (["Time (UTC)"] if hourly else []) + [
        "Country of boat", "Fishing method", "Boats in this square",
        "Hours spent fishing", "Map", "Latitude", "Longitude",
    ]
    widths = [12] + ([11] if hourly else []) + [22, 24, 20, 20, 12, 11, 11]
    s = Sheet(wb, "Fishing records", widths)
    s.ws.freeze_panes = "A2"  # keep the column names visible while scrolling
    s.header(*columns)

    link_font = Font(color="0563C1", underline="single")
    for r in rows:
        map_link = WriteOnlyCell(
            s.ws,
            f'=HYPERLINK("https://www.google.com/maps/search/?api=1&query={r["lat"]},{r["lon"]}",'
            f'"Open map")',
        )
        map_link.font = link_font
        s.add(
            s.cell(r["date"], fmt="d mmm yyyy"),
            *([r["time"]] if hourly else []),
            country_name(r["flag"]),
            gear_name(r["gear"]),
            r["boats"],
            s.cell(round(r["hours"], 2), fmt="#,##0.0"),
            map_link,
            r["lat"],
            r["lon"],
        )
    # Little filter arrows on the header row, so people can pick e.g. one country
    last_col = chr(ord("A") + len(columns) - 1)
    s.ws.auto_filter.ref = f"A1:{last_col}{max(s.row, 2)}"


def write_explanations(wb, hourly):
    s = Sheet(wb, "What this means", [26, 90])
    s.add(s.cell("What the numbers mean", bold=True, size=16, color=NAVY))
    s.add()

    s.heading("The columns in 'Fishing records'")
    s.header("Column", "What it means")
    explained = [
        ("Date", "The day the fishing happened."),
        *([("Time (UTC)", "The hour the fishing started, in UTC (the same as UK winter time). "
                          "Each row covers one hour.")] if hourly else []),
        ("Country of boat", "The country the boat is registered in (the flag it flies). "
                            "This is not always where the owners are from."),
        ("Fishing method", "How the boats were fishing. The 'Summary' sheet explains each method."),
        ("Boats in this square", "How many different boats were fishing in this square at this time."),
        ("Hours spent fishing", "The total time those boats spent fishing in this square at this "
                                "time. Two boats fishing for 3 hours each counts as 6 hours."),
        ("Map", "Click 'Open map' to see this square on Google Maps."),
        ("Latitude / Longitude", "The position of the square on the map, in degrees."),
    ]
    for name, text in explained:
        s.add(s.cell(name, bold=True), s.cell(text, wrap=True))
    s.add()

    s.heading("Things to know")
    notes = [
        ("Squares", "The sea is split into squares about 11 km (7 miles) from north to south. "
                    "Each row adds up all the fishing in one square."),
        ("Where it comes from", "Most larger boats send out their position by radio every few "
                                "seconds (a system called AIS). Global Fishing Watch uses a computer "
                                "model to tell, from the way a boat moves, when it is fishing."),
        ("What's missing", "Many small boats don't send these signals, and some boats switch them "
                           "off. So the real amount of fishing is higher than shown here."),
        ("Estimates", "The hours are the model's best estimate, not official records."),
        ("Source", "Global Fishing Watch, www.globalfishingwatch.org"),
    ]
    for name, text in notes:
        s.add(s.cell(name, bold=True), s.cell(text, wrap=True))


def build_workbook(csv_bytes: bytes, meta: dict) -> bytes:
    """
    meta: area (str), start/last_day (date, both included), flag/gear (names), hourly (bool).
    Returns the .xlsx file as bytes.
    Raises ValueError if there are too many rows for Excel.
    """
    rows = read_rows(csv_bytes)
    if len(rows) >= EXCEL_MAX_ROWS:
        raise ValueError(f"This download has {len(rows):,} rows, more than Excel can open.")

    wb = Workbook(write_only=True)
    write_summary(wb, rows, meta)
    write_records(wb, rows, meta["hourly"])
    write_explanations(wb, meta["hourly"])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
