"""
The choices shown in the sidebar dropdowns. They live here (on the server) so
there is ONE list: the page loads it from /api/options, and the server uses
the same list to reject anything unexpected before it reaches GFW.
"""

# Gear types used by GFW's fishing effort dataset -> friendly label
GEAR_TYPES = {
    "trawlers": "Trawlers",
    "purse_seines": "Purse seines",
    "tuna_purse_seines": "Tuna purse seines",
    "other_purse_seines": "Other purse seines",
    "drifting_longlines": "Drifting longlines",
    "set_longlines": "Set longlines",
    "set_gillnets": "Set gillnets",
    "pots_and_traps": "Pots and traps",
    "squid_jigger": "Squid jiggers",
    "pole_and_line": "Pole and line",
    "trollers": "Trollers",
    "dredge_fishing": "Dredges",
    "seiners": "Seiners",
    "other_seines": "Other seines",
    "driftnets": "Driftnets",
    "fixed_gear": "Fixed gear",
    "fishing": "Fishing (unknown gear)",
}

# Flag states (ISO 3-letter country codes) with large fishing fleets.
FLAGS = {
    "ARG": "Argentina", "AUS": "Australia", "BRA": "Brazil", "CAN": "Canada",
    "CHL": "Chile", "CHN": "China", "DEU": "Germany", "DNK": "Denmark",
    "ECU": "Ecuador", "ESP": "Spain", "FJI": "Fiji", "FRA": "France",
    "FRO": "Faroe Islands", "GBR": "United Kingdom", "GHA": "Ghana",
    "GRC": "Greece", "IDN": "Indonesia", "IND": "India", "IRL": "Ireland",
    "ISL": "Iceland", "ITA": "Italy", "JPN": "Japan", "KIR": "Kiribati",
    "KOR": "South Korea", "MAR": "Morocco", "MEX": "Mexico",
    "MRT": "Mauritania", "MUS": "Mauritius", "MYS": "Malaysia",
    "NAM": "Namibia", "NLD": "Netherlands", "NOR": "Norway",
    "NZL": "New Zealand", "PAN": "Panama", "PER": "Peru", "PHL": "Philippines",
    "PRT": "Portugal", "RUS": "Russia", "SEN": "Senegal", "SYC": "Seychelles",
    "THA": "Thailand", "TUR": "Türkiye", "TWN": "Taiwan",
    "USA": "United States", "VNM": "Vietnam", "VUT": "Vanuatu",
    "ZAF": "South Africa",
}

# Regions of interest as bounding boxes: [west, south, east, north] in degrees.
# Later (Stage 6) the stats chart will use these same boxes.
REGIONS = {
    "north-sea": {"name": "North Sea", "bbox": [-4.5, 51.0, 9.5, 61.5]},
    "barents-sea": {"name": "Barents Sea", "bbox": [15.0, 68.0, 60.0, 80.0]},
    "mediterranean": {"name": "Mediterranean", "bbox": [-6.0, 30.0, 36.5, 46.0]},
    "west-africa": {"name": "West Africa", "bbox": [-26.0, 5.0, -5.0, 28.0]},
    "grand-banks": {"name": "Grand Banks", "bbox": [-60.0, 40.0, -44.0, 52.0]},
    "gulf-of-mexico": {"name": "Gulf of Mexico", "bbox": [-98.0, 18.0, -80.0, 31.0]},
    "humboldt": {"name": "Peru & Chile", "bbox": [-90.0, -30.0, -70.0, 0.0]},
    "patagonia": {"name": "Patagonian Shelf", "bbox": [-70.0, -55.0, -54.0, -38.0]},
    "east-china-sea": {"name": "East China Sea", "bbox": [117.0, 24.0, 131.0, 41.0]},
    "south-china-sea": {"name": "South China Sea", "bbox": [105.0, 2.0, 122.0, 23.0]},
}
