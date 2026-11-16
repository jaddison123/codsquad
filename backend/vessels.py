"""
Turns the GFW vessel data (deeply nested) into small, flat dictionaries that
are easy for the web page to display.

A GFW vessel record has several parts:
  - self_reported_info: what the ship broadcasts itself over AIS (name, flag,
    MMSI). One ship can have several of these, e.g. after being renamed.
  - registry_info: official registry records (IMO number, gear, size).
  - registry_owners / registry_public_authorizations: owners and fishing
    licences from public registries (e.g. regional fisheries bodies).
  - combined_sources_info: GFW's best guess at gear type, built from all of the above.
"""

VESSEL_DATASET = "public-global-vessel-identity:latest"

# Event datasets: "fishing" = periods when GFW's model thinks the ship was fishing,
# "port_visit" = stops in port.
EVENT_DATASETS = {
    "fishing": "public-global-fishing-events:latest",
    "port_visit": "public-global-port-visits-events:latest",
}


def _latest(items, date_key="transmission_date_to"):
    """Pick the most recent entry from a list (by its end date)."""
    items = items or []
    return max(items, key=lambda i: i.get(date_key) or "", default={})


def _nice(text):
    """'TUNA_PURSE_SEINES' -> 'Tuna purse seines'"""
    return text.replace("_", " ").capitalize() if text else None


def _gear(record):
    """Best available gear type: registry first, then GFW's combined estimate."""
    registry = _latest(record.get("registry_info"))
    if registry.get("gear_types"):
        return _nice(registry["gear_types"][0])
    for combined in record.get("combined_sources_info") or []:
        gears = combined.get("gear_types") or []
        if gears:
            return _nice(max(gears, key=lambda g: g.get("year_to") or 0).get("name"))
    return None


def _date(value):
    """'2022-06-29T07:13:18Z' -> '2022-06-29'"""
    return value[:10] if value else None


def summarise(record):
    """One short line per search result."""
    ais = _latest(record.get("self_reported_info"))
    registry = _latest(record.get("registry_info"))
    name = ais.get("ship_name") or registry.get("ship_name") or "Unknown name"
    # Ships get renamed; list older names so a search for an old name makes sense
    all_names = [i.get("ship_name") for i in record.get("self_reported_info") or []]
    all_names += [i.get("ship_name") for i in record.get("registry_info") or []]
    former = sorted({n for n in all_names if n and n != name})
    return {
        "id": ais.get("id"),
        "name": name,
        "formerNames": former,
        "flag": ais.get("flag") or registry.get("flag"),
        "mmsi": ais.get("ssvid"),
        "imo": ais.get("imo") or registry.get("imo"),
        "gear": _gear(record),
        "lastSeen": _date(ais.get("transmission_date_to")),
    }


def simplify_event(event):
    """One map marker: where, when, and a little extra about the event."""
    port = ((event.get("port_visit") or {}).get("end_anchorage") or {}).get("name")
    start, end = event.get("start"), event.get("end")
    return {
        "type": event.get("type"),
        "lat": event["position"]["lat"],
        "lon": event["position"]["lon"],
        "start": start[:16].replace("T", " ") if start else None,  # "2022-07-27 05:04"
        "end": end[:16].replace("T", " ") if end else None,
        "port": port.title() if port else None,
    }


def details(record):
    """Everything the details panel shows for one vessel."""
    ais = _latest(record.get("self_reported_info"))
    registry = _latest(record.get("registry_info"))

    # Licences: group by issuing body, keep the overall date span for each.
    licences = {}
    for auth in record.get("registry_public_authorizations") or []:
        for source in auth.get("source_code") or []:
            span = licences.setdefault(source, {"source": source, "from": None, "to": None})
            start, end = _date(auth.get("date_from")), _date(auth.get("date_to"))
            if start and (not span["from"] or start < span["from"]):
                span["from"] = start
            if end and (not span["to"] or end > span["to"]):
                span["to"] = end

    return {
        **summarise(record),
        # GFW stores events per AIS identity, so a renamed ship has several ids
        "ids": [i["id"] for i in record.get("self_reported_info") or [] if i.get("id")],
        "callSign": ais.get("call_sign") or registry.get("call_sign"),
        "lengthM": registry.get("length_m"),
        "tonnageGt": registry.get("tonnage_gt"),
        "firstSeen": _date(min((i.get("transmission_date_from") or "9999"
                                for i in record.get("self_reported_info") or []), default=None)),
        "owners": [
            {"name": o.get("name"), "flag": o.get("flag"),
             "from": _date(o.get("date_from")), "to": _date(o.get("date_to"))}
            for o in sorted(record.get("registry_owners") or [],
                            key=lambda o: o.get("date_to") or "", reverse=True)
        ],
        "licences": sorted(licences.values(), key=lambda l: l["to"] or "", reverse=True),
        # Every AIS identity the ship has used, newest first
        "aisHistory": [
            {"name": i.get("ship_name"), "flag": i.get("flag"), "mmsi": i.get("ssvid"),
             "from": _date(i.get("transmission_date_from")), "to": _date(i.get("transmission_date_to"))}
            for i in sorted(record.get("self_reported_info") or [],
                            key=lambda i: i.get("transmission_date_to") or "", reverse=True)
        ],
    }
