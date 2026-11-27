"""
GDACS (Disasters) MCP Server
============================
An MCP server providing global natural disaster and hazard monitoring
via GDACS (Global Disaster Alert and Coordination System), a joint
initiative of the UN and European Commission. Use to search for
natural disaster events worldwide covering earthquakes, floods,
volcanoes, wildfires, droughts and tropical cyclones (GDACS's search
has no tsunami events).

Tools:
    search_gdacs_events — search disasters by hazard, alert level, place
                        (country, region, sea or area), event name and
                        date range

Usage:
    python server.py                              # stdio transport
    python server.py --transport http --port 8000  # HTTP transport

Requirements:
    pip install "mcp[cli]==1.27.0" httpx python-dotenv
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from regions import (
    REGION_LOOKUP,
    SEA_BOXES,
    TRANSCONTINENTAL,
    UNSUPPORTED_REGIONS,
    WORLDWIDE,
    in_region,
    names_country,
    regions_of,
)
from regions import norm as _norm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)

GDACS_SEARCH_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
# Same filters as SEARCH (bar severity), but returns exact totals and one
# short row per matching event, with no paging.
GDACS_STATISTIC_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/statistic"
TIMEOUT = 60
GDACS_USER_AGENT = "DisastersMCPServer"
GDACS_PAGE_SIZE = 100  # fixed by the API; its pagesize parameter is ignored
MAX_RESULTS_LIMIT = 100  # events listed; more only invites the agent to pad its answer
# Every matching event up to this many is fetched, so the summary (counts,
# largest events, ongoing, most recent) is computed from all of them, not
# from whichever page GDACS happened to return first (it does not sort by date).
FETCH_LIMIT = 2000
DEFAULT_MAX_RESULTS = 20
RETRY_DELAYS = (0.5, 1.5)  # GDACS sometimes fails a valid request once
MAX_SINCE_DAYS = 3650
ACTIVE_WINDOW_DAYS = 7  # updated this recently = ongoing (droughts often lack the iscurrent flag)

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

mcp = FastMCP("Disasters", host="0.0.0.0", port=8000, stateless_http=True)


async def _http_get(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    not_found_ok: bool = False,
) -> httpx.Response:
    """Perform an async HTTP GET, retrying transient failures.

    not_found_ok returns a 404 as it is (the statistic endpoint's "no match").
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        for delay in (*RETRY_DELAYS, None):
            try:
                resp = await client.get(url, params=params, headers=headers)
                if not_found_ok and resp.status_code == 404:
                    return resp
                resp.raise_for_status()
                return resp
            except httpx.HTTPError:
                # Includes 4xx: arguments are validated before the request, and
                # GDACS has returned a one-off 400 for a query that then worked.
                if delay is None:
                    raise
                await asyncio.sleep(delay)
    raise AssertionError("unreachable")


EventType = Literal["EQ", "TC", "FL", "VO", "WF", "DR", "TS"]
AlertLevel = Literal["Green", "Orange", "Red"]
OutputFormat = Literal["compact", "geojson"]


# GDACS's country filter is a case-insensitive substring match on its own
# spellings, and it spells some countries several ways (seen in its data
# 2019-2026). For these countries every spelling is searched. For every
# country, only events naming it as whole words are kept (regions.names_country),
# so "Republic of Korea" does not pull in "Democratic Republic of Korea".
_COUNTRY_SPELLINGS: list[tuple[list[str], list[str]]] = [
    # (names a user may type, the first being the display name; spellings GDACS uses)
    (["Turkey", "Turkiye"], ["Türkiye"]),
    (["Mexico"], ["Mexico", "México"]),
    (["Vietnam", "Viet Nam"], ["Vietnam", "Viet Nam"]),
    (["Russia", "Russian Federation"], ["Russia", "Russian Federation"]),
    (["Iran", "Islamic Republic of Iran"], ["Iran", "Islamic Republic of Iran"]),
    (["South Korea", "Republic of Korea"], ["South Korea", "Republic of Korea"]),
    (["North Korea", "DPRK"], ["North Korea", "Democratic Republic of Korea"]),
    (
        ["DR Congo", "DRC", "Democratic Republic of Congo", "Democratic Republic of the Congo"],
        ["Democratic Republic of Congo", "Democratic Republic of the Congo",
         "The Democratic Republic of Congo"],
    ),
    # GDACS's "Congo" substring also returns DR Congo; names_country drops it.
    (["Republic of Congo", "Congo", "Congo-Brazzaville"], ["Congo"]),
    (["Eswatini", "Swaziland"], ["Eswatini", "Swaziland"]),
    (["Czechia", "Czech Republic"], ["Czech Republic"]),
    (["Ivory Coast", "Cote d'Ivoire"], ["Cote d'Ivoire"]),
    (["Cabo Verde", "Cape Verde"], ["Cape Verde"]),
    (["Myanmar", "Burma"], ["Myanmar"]),
    (["Laos", "Lao PDR"], ["Laos"]),
    (["Bahamas"], ["Bahamas", "The Bahamas"]),
    (["Sao Tome and Principe"], ["São Tomé and Príncipe"]),
    (["United States", "USA", "US", "United States of America"], ["United States"]),
    (["United Kingdom", "UK", "Great Britain", "Britain"], ["United Kingdom"]),
    (["Netherlands", "Holland"], ["Netherlands"]),
]
COUNTRY_ALIASES: dict[str, list[str]] = {
    _norm(typed): spellings
    for names, spellings in _COUNTRY_SPELLINGS
    for typed in names + spellings
}
# Every name that places an event in an aliased country.
COUNTRY_NAMES: dict[str, list[str]] = {
    _norm(typed): names + spellings
    for names, spellings in _COUNTRY_SPELLINGS
    for typed in names + spellings
}
# GDACS sometimes inverts a name around a comma, which splitting on commas
# would read as two countries ("Congo" and "The Democratic Republic of the").
_INVERTED_NAMES = {
    "congo, the democratic republic of the": "Democratic Republic of the Congo",
}
# Only a whole entry: "Democratic Republic of Korea, Republic of Korea" must not change.
_INVERTED = re.compile(
    r"(?:^|(?<=,))\s*(?:" + "|".join(re.escape(k) for k in _INVERTED_NAMES) + r")\s*(?=,|$)", re.IGNORECASE
)

DISPLAY_NAMES: dict[str, str] = {
    _norm(name): names[0]
    for names, spellings in _COUNTRY_SPELLINGS
    for name in names + spellings
}

ALERT_RANK = {"Red": 2, "Orange": 1, "Green": 0}
SEARCHABLE_TYPES = {"EQ", "TC", "FL", "VO", "WF", "DR"}  # "TS" returns nothing from GDACS
# Hazards that last, and so can be ongoing. GDACS records earthquakes and
# volcanic eruptions as single moments: they are recent, never ongoing.
LASTING_TYPES = {"DR", "FL", "TC", "WF"}
REGION_COUNTRY_QUERIES = 40  # most per-country searches to complete a region's details
NAME_SEARCH_YEARS = 5  # a search by event name with no dates looks back this far
DEFAULT_RADIUS_KM = 100

EVENT_TYPE_NAMES = {
    "EQ": "earthquake", "TC": "tropical cyclone", "FL": "flood",
    "VO": "volcano", "WF": "wildfire", "DR": "drought", "TS": "tsunami",
}


def _join(value: str | list[str] | None) -> str | None:
    """GDACS takes several values as one ';'-separated string."""
    if value is None or isinstance(value, str):
        return value
    return ";".join(dict.fromkeys(value)) or None


def _parse_date(name: str, value: str | None, end_of_month: bool = False) -> str | None:
    """Validate a YYYY-MM-DD date and return it in ISO form.

    With end_of_month, a day past the month's end ("2023-02-29", "2024-04-31")
    is read as the month's last day: agents often end a month on the 29th-31st.
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        parts = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
        if end_of_month and parts and 1 <= int(parts[2]) <= 12 and 28 < int(parts[3]) <= 31:
            year, month = int(parts[1]), int(parts[2])
            return (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).isoformat()
        raise ValueError(f"{name} {value!r} is not a valid date in YYYY-MM-DD format.") from None


def _country_names(props: dict, every_spelling: bool = False) -> list[str]:
    """The countries an event names, from affectedcountries and country.

    Neither field is complete on its own, and they spell countries
    differently ("South Korea" / "Republic of Korea", "Vanuatu" / "Vanuatu
    Islands"). Known spellings get one display name, and a name that only
    extends one already listed is dropped. every_spelling returns the raw
    names instead, for matching.
    """
    affected = [c.get("countryname", "") for c in props.get("affectedcountries") or []]
    country = _INVERTED.sub(lambda m: _INVERTED_NAMES[" ".join(m.group(0).lower().split())], props.get("country") or "")
    raw = [n.strip() for n in affected + country.split(",") if n.strip()]
    if every_spelling:
        return list(dict.fromkeys(raw))
    names: list[str] = []
    for name in raw:
        name = DISPLAY_NAMES.get(_norm(name), name)
        key = _norm(name)
        if not any(key == _norm(kept) or key.startswith(_norm(kept) + " ") for kept in names):
            names.append(name)
    return names


def _size(props: dict) -> tuple[float, str] | None:
    """(value, unit) of an event's size: magnitude, wind km/h, burnt ha or drought km2.

    Floods and volcanoes have no size in GDACS (severity 0, no unit).
    """
    data = props.get("severitydata") or {}
    value, unit = data.get("severity"), (data.get("severityunit") or "").strip()
    if not unit or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value), unit


def _public(event: dict) -> dict:
    """An event as returned: no internal keys, and for an ongoing event its
    to_date (GDACS's latest update) is shown as last_updated, since agents
    read to_date as an end date ("the drought ends on 21 November")."""
    out: dict[str, Any] = {}
    for k, v in event.items():
        if k.startswith("_"):
            continue
        out["last_updated" if k == "to_date" and event.get("is_current") else k] = v
    return out


def _compact(feature: dict) -> dict:
    """The fields an agent needs from one GDACS GeoJSON feature.

    is_current is only for hazards that last (droughts, floods, cyclones,
    wildfires): GDACS's iscurrent flag, or an update in the last
    ACTIVE_WINDOW_DAYS. GDACS moves a drought's todate forward at every
    update and often leaves iscurrent false, so a todate of last Tuesday
    means "still going", not "ended last Tuesday". Earthquakes and eruptions
    are single moments: _recent marks those in the last ACTIVE_WINDOW_DAYS.
    """
    props = feature.get("properties", {})
    to_date = (props.get("todate") or "")[:10] or None
    cutoff = (datetime.now(UTC).date() - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
    event_type = props.get("eventtype")
    return {
        "id": props.get("eventid"),
        "type": event_type,
        "type_name": EVENT_TYPE_NAMES.get(event_type, event_type),
        "name": props.get("name"),
        "alert_level": props.get("alertlevel"),
        "countries": _country_names(props),
        "from_date": (props.get("fromdate") or "")[:10] or None,
        "to_date": to_date,
        "is_current": event_type in LASTING_TYPES
        and (str(props.get("iscurrent")).lower() == "true" or (to_date or "") >= cutoff),
        "_recent": event_type not in LASTING_TYPES and (to_date or "") >= cutoff,
        "severity": (props.get("severitydata") or {}).get("severitytext"),
        "_size": _size(props),
        "lat": coords[1],
        "lon": coords[0],
        "report_url": (props.get("url") or {}).get("report"),
    }


async def _fetch(
    params: dict[str, Any], max_results: int, done: Callable[[list[dict]], bool] | None = None
) -> tuple[dict | None, list[dict], bool, int]:
    """Fetch up to max_results features, page by page, stopping early once
    done(features) is true.

    Returns the first page (for its top-level keys), the features, whether
    more may exist, and the number of requests made.
    """
    first: dict | None = None
    features: list[dict] = []
    seen: set[tuple] = set()
    page = 1
    while True:
        resp = await _http_get(
            GDACS_SEARCH_URL,
            params={**params, "pagenumber": page},
            headers={"User-Agent": GDACS_USER_AGENT},
        )
        if not resp.text:
            # GDACS: 204 No Content with an empty body for no results
            return first, features, False, page
        page_data = resp.json()
        page_features = page_data.get("features") or []
        if first is None:
            first = page_data
        count_before = len(features)
        for feature in page_features:
            props = feature.get("properties", {})
            key = (props.get("eventtype"), props.get("eventid"))
            if key not in seen:
                seen.add(key)
                features.append(feature)
        if len(page_features) < GDACS_PAGE_SIZE or len(features) == count_before:
            return first, features, False, page
        if done and done(features):
            return first, features, False, page
        if len(features) >= max_results:
            # A full page means there may be another one.
            return first, features, True, page
        page += 1


Box = tuple[float, float, float, float]  # west, south, east, north; west > east crosses 180


@dataclass
class _Place:
    """Where to search.

    queries has one entry per country asked for: the names to send as
    GDACS's country filter, and the names that place an event in that
    country (None, worldwide, keeps every match). GDACS has no region or
    coordinate filter, so region and box are applied here, to a worldwide
    (or country) search.
    """

    queries: list[tuple[list[str | None], list[str] | None]] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)  # GDACS spellings searched
    region: str | None = None
    box: Box | None = None
    near: tuple[float, float, float] | None = None  # lat, lon, radius_km
    event_name: str | None = None  # normalised text an event's name must contain
    label: str | None = None  # the sea or area asked for, for messages

    @property
    def by_coords(self) -> bool:
        return bool(self.box or self.near)

    @property
    def filtered_here(self) -> bool:
        return bool(self.region or self.box or self.near or self.event_name)

    @property
    def name(self) -> str | None:
        """The area in words, e.g. "the Caribbean", "Bay of Bengal"."""
        if self.region:
            return f"the {self.region}" if self.region in {"Caribbean", "Middle East", "Americas"} else self.region
        return self.label

    def keeps(self, names: list[str], lat: Any = None, lon: Any = None, title: str | None = None) -> bool:
        if self.region and not in_region(names, self.region):
            return False
        if self.event_name and not re.search(rf"(?<!\w){re.escape(self.event_name)}(?!\w)", _norm(title or "")):
            return False  # whole words: "Etna" must not match "Vietnam"
        if self.near:
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                return False
            return _distance_km(lat, lon, self.near[0], self.near[1]) <= self.near[2]
        if self.box:
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                return False
            west, south, east, north = self.box
            if not south <= lat <= north:
                return False
            return west <= lon <= east if west <= east else (lon >= west or lon <= east)
        return True


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(a)))  # rounding can put a just over 1


def _parse_near(near: str | list[float], radius_km: float) -> tuple[float, float, float]:
    """A point as "lat,lon" (geocode_place's top_result lat and lon) or [lat, lon], plus a radius."""
    parts = near.split(",") if isinstance(near, str) else list(near)
    try:
        lat, lon = (float(p) for p in parts)
    except (TypeError, ValueError):
        raise ValueError(f"near must be 'lat,lon' in degrees, got {near!r}.") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError(f"near {near!r} is out of range: lat -90 to 90, lon -180 to 180.")
    if not 0 < radius_km <= 5000:
        raise ValueError("radius_km must be between 0 and 5000.")
    return lat, lon, float(radius_km)


def _parse_bbox(bbox: str | list[float]) -> Box:
    """A box as "west,south,east,north" (geocode_place's format) or a list of four numbers."""
    parts = bbox.split(",") if isinstance(bbox, str) else list(bbox)
    try:
        west, south, east, north = (float(p) for p in parts)
    except (TypeError, ValueError):
        raise ValueError(f"bbox must be 'west,south,east,north' in degrees, got {bbox!r}.") from None
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= north <= 90):
        raise ValueError(
            f"bbox {bbox!r} is out of range: lon -180 to 180, lat -90 to 90, south <= north."
        )
    return west, south, east, north


def _parse_place(
    country: str | list[str] | None, bbox: str | list[float] | None = None,
    near: str | list[float] | None = None, radius_km: float = DEFAULT_RADIUS_KM, event_name: str | None = None,
) -> _Place:
    """Turn country (countries, a region or a sea), bbox, near and name into a _Place. Raises ValueError."""
    names = [country] if isinstance(country, str) else list(country or [])
    # "Myanmar (Burma)" -> "Myanmar": GDACS has no names with brackets.
    names = [re.sub(r"\s*\([^)]*\)", "", n).strip() or n.strip("() ") for n in names if n and n.strip()]
    place = _Place()
    for name in names:
        key = _norm(name)
        if key in WORLDWIDE:
            continue
        if key == "america":
            raise ValueError(
                "'America' could mean the United States or both continents: pass 'United States', "
                "or a region such as 'Americas', 'North America' or 'Latin America'."
            )
        if key in UNSUPPORTED_REGIONS:
            raise ValueError(
                f"{name!r} is not a region this tool knows. Pass its countries as a list "
                "instead, e.g. country=['Norway', 'Sweden', 'Denmark']."
            )
        if (key in REGION_LOOKUP or key in SEA_BOXES) and len(names) > 1:
            raise ValueError(f"{name!r} is a region or sea: search it on its own, not in a list.")
        if key in REGION_LOOKUP:
            place.region = REGION_LOOKUP[key]
        elif key in SEA_BOXES:
            place.label, place.box = SEA_BOXES[key]
        else:
            spellings = COUNTRY_ALIASES.get(key, [name])
            place.queries.append((spellings, COUNTRY_NAMES.get(key, [name])))
            place.countries += spellings
    if bbox is not None:
        if place.box:
            raise ValueError("Give a sea name or a bbox, not both.")
        place.box = _parse_bbox(bbox)
        place.label = "the area given"
    if near is not None:
        if place.box:
            raise ValueError("Give near or a bbox (or sea), not both.")
        place.near = _parse_near(near, radius_km)
        place.label = f"{place.near[2]:g} km of the point given"
    if event_name and event_name.strip():
        place.event_name = _norm(event_name.strip())
    if not place.queries:
        place.queries = [([None], None)]  # worldwide
    return place


def _no_results(place: _Place, included_green: bool, name_from: str | None = None) -> str:
    # A name GDACS has never recorded is caught earlier (_unknown_places), so
    # this is a real place with nothing in the period: no spelling hint.
    message = "No results found for query."
    if included_green:
        message += " Green (low-impact) events were included in the search."
    if place.name:
        message += f" Searched {place.name}."
    if name_from:
        # The 5-year default hides older named events ("Haiyan", 2013).
        message += (
            f" A name search without dates covers only the last {NAME_SEARCH_YEARS} years (since {name_from}). "
            "For an older event, search again with from_date and to_date around the year it happened."
        )
    return json.dumps({"error": message})


def _in_country(names: list[str], country: list[str] | None) -> bool:
    return country is None or names_country(names, country)


def _point(feature: dict) -> tuple[Any, Any]:
    """(lat, lon) of a GeoJSON point feature, or (None, None)."""
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
    return (coords[1], coords[0]) if len(coords) >= 2 else (None, None)


async def _statistic_rows(params: dict[str, Any]) -> list[dict]:
    """The statistic endpoint's rows; it answers 404 when nothing matches."""
    resp = await _http_get(
        GDACS_STATISTIC_URL, params=params, headers={"User-Agent": GDACS_USER_AGENT}, not_found_ok=True
    )
    if resp.status_code == 404 or not resp.text:
        return []
    return resp.json().get("details") or []


async def _statistic(params: dict[str, Any], place: _Place) -> tuple[dict[str | None, int], list[dict]]:
    """Every matching event as a short row, from GDACS's statistic endpoint.

    One call per country spelling, merged; rows are only type, id, alert
    level, country text, dates and a description (which holds a cyclone's or
    volcano's name), which is all the counts need. Returns how many events
    GDACS matched for each name sent (what SEARCH would page through), and
    the rows in the place. Rows have no coordinates, so for a box or near
    search no row is in the place and only the first number is useful.
    """
    rows: list[dict] = []
    seen: set[tuple] = set()
    matched: dict[str | None, int] = {}
    for names, country in place.queries:
        for name in names:
            query = {**params, "country": name} if name else params
            details = await _statistic_rows(query)
            matched[name] = len(details)
            for row in details:
                key = (row.get("eventtype"), row.get("eventid"))
                names_in_row = _country_names(row, every_spelling=True)
                if key in seen or not _in_country(names_in_row, country):
                    continue
                if place.keeps(names_in_row, title=row.get("description")):
                    seen.add(key)
                    rows.append(row)
    return matched, rows


def _row_event(row: dict) -> dict:
    """A statistic row in the shape _counts reads."""
    return {
        "type_name": EVENT_TYPE_NAMES.get(row.get("eventtype"), row.get("eventtype")),
        "alert_level": (row.get("alertlevel") or "").capitalize(),
        "countries": _country_names(row),
    }


@dataclass
class _Searched:
    first: dict | None  # GDACS's first page, for its top-level keys
    features: list[dict]  # the events in the place
    more: bool  # a name hit its page limit, so features may not be every match
    requests: int
    checked: int  # events downloaded before the place filter


async def _search(
    params: dict[str, Any], place: _Place, limit: int = FETCH_LIMIT,
    limits: dict[str | None, int] | None = None, want: int | None = None,
) -> _Searched:
    """Run one search per country spelling, merge, and keep the events in the place.

    limits sets the page limit for each name sent (default limit). With
    want, paging stops once that many events in the place are found.
    """
    found = _Searched(None, [], False, 0, 0)
    seen: set[tuple] = set()

    def kept(feature: dict, country: list[str] | None) -> bool:
        props = feature.get("properties", {})
        names_in_event = _country_names(props, every_spelling=True)
        return _in_country(names_in_event, country) and place.keeps(
            names_in_event, *_point(feature), props.get("name")
        )

    for names, country in place.queries:
        for name in names:
            query = {**params, "country": name} if name else params
            done = None
            if want is not None:
                def done(fs: list[dict], country: list[str] | None = country) -> bool:
                    return len(found.features) + sum(kept(f, country) for f in fs) >= want
            first, features, more, requests = await _fetch(query, (limits or {}).get(name, limit), done)
            found.first = found.first or first
            found.more = found.more or more
            found.requests += requests
            found.checked += len(features)
            for feature in features:
                props = feature.get("properties", {})
                key = (props.get("eventtype"), props.get("eventid"))
                if key not in seen and kept(feature, country):
                    seen.add(key)
                    found.features.append(feature)
    return found



def _countries(events: list[dict], region: str | None) -> dict[str, int]:
    """Events per country, most first. With a region, only its countries."""
    countries = Counter(
        c for e in events for c in e["countries"] if not region or region in regions_of(c)
    )
    for name in sorted(countries, key=len, reverse=True):
        # Events at sea name only a region, e.g. "Vanuatu Islands": count them
        # under the country if it appears too.
        base = next((c for c in countries if c != name and _norm(name).startswith(_norm(c) + " ")), None)
        if base:
            countries[base] += countries.pop(name)
    return dict(countries.most_common())


COUNTRIES_LISTED = 50  # worldwide searches name 300+ countries and sea areas


def _counts(events: list[dict], region: str | None) -> dict:
    """Counts by type, alert level and country (the 50 with most events)."""
    alerts = Counter(e["alert_level"] for e in events)
    countries = _countries(events, region)
    counts: dict[str, Any] = {
        "by_type": dict(Counter(e["type_name"] for e in events).most_common()),
        "by_alert_level": {lvl: alerts[lvl] for lvl in ("Red", "Orange", "Green") if alerts[lvl]},
        "countries": dict(list(countries.items())[:COUNTRIES_LISTED]),
    }
    if len(countries) > COUNTRIES_LISTED:
        counts["countries_not_listed"] = len(countries) - COUNTRIES_LISTED
    return counts


def _brief(e: dict) -> dict:
    return _public({k: e[k] for k in ("name", "alert_level", "countries", "from_date", "to_date", "is_current", "severity")})


def _details(events: list[dict]) -> dict:
    """What questions ask beyond counts, from the full event records."""
    details: dict[str, Any] = {"ongoing": sum(e["is_current"] for e in events)}
    starts = [e["from_date"] for e in events if e["from_date"]]
    ends = [e["to_date"] for e in events if e["to_date"]]
    if starts and ends:
        details["date_span"] = {"earliest_start": min(starts), "latest_date": max(ends)}
    largest: dict[str, dict] = {}
    for e in events:
        if e["_size"] and (e["type_name"] not in largest or e["_size"][0] > largest[e["type_name"]]["_size"][0]):
            largest[e["type_name"]] = e
    if largest:
        details["largest"] = {t: _brief(e) for t, e in largest.items()}
    burnt = sum(e["_size"][0] for e in events if e["type"] == "WF" and e["_size"] and e["_size"][1] == "ha")
    if burnt:
        details["wildfire_burnt_area_ha"] = round(burnt)
    return details


def _importance(e: dict) -> tuple:
    """Highest alert level first, then the latest. Sizes are not compared:
    a drought's km2 and an earthquake's magnitude are different units (the
    summary names the largest of each hazard)."""
    return (ALERT_RANK.get(e["alert_level"], 0), e["to_date"] or "", e["from_date"] or "")


@dataclass
class _Found:
    first: dict | None
    pairs: list[tuple[dict, dict]]  # (GeoJSON feature, compact event)
    more: bool  # not every match was fetched, so pairs is not every match
    requests: int
    used: dict[str, Any]  # the params that found the events
    serious_complete: bool
    stats: list[dict] | None = None  # every match in the place, from the statistic endpoint
    matched: int | None = None  # events GDACS matched before the place filter
    checked: int = 0  # of those, how many were downloaded and checked


async def _gather(
    params: dict[str, Any], place: _Place, alertlevel: Any, active_only: bool
) -> _Found:
    """Fetch every matching event (up to FETCH_LIMIT), with the follow-up searches an agent would otherwise make."""

    def keep(features: list[dict]) -> list[tuple[dict, dict]]:
        pairs = [(f, _compact(f)) for f in features]
        if active_only:
            pairs = [(f, e) for f, e in pairs if e["is_current"] or e["_recent"]]
        return pairs

    # Count first (one fast request per name). The statistic endpoint has no
    # severity filter, so with min_severity just page.
    matched: dict[str | None, int] | None = None
    stats: list[dict] | None = None
    if "severity" not in params:
        try:
            matched, stats = await _statistic(params, place)
        except Exception:
            matched, stats = None, None

    limits: dict[str | None, int] | None = None
    want: int | None = None
    limit = FETCH_LIMIT
    if place.event_name and stats is not None:
        # The rows' descriptions hold the names, so the count of named events
        # is known: page until all of them are found or the pages end.
        if not stats:
            return _Found(None, [], False, len(matched or {}), params, False, stats)
        want, limit = len(stats), sys.maxsize
    elif matched and not place.by_coords:
        # Each name on its own: under FETCH_LIMIT, fetch every match; over
        # it, 2,000 events would still not be all of them, so fetch one page
        # for the list and rely on the exact counts.
        limits = {n: FETCH_LIMIT if c <= FETCH_LIMIT else GDACS_PAGE_SIZE for n, c in matched.items()}
    # A box or near search keeps the default limit: GDACS has no location
    # filter, so the events in the area can be anywhere in the wider search.
    searched = await _search(params, place, limit, limits, want)
    features, more, requests = searched.features, searched.more, searched.requests

    # A region over FETCH_LIMIT worldwide is often small itself (Europe's
    # wildfires: 383 of 7,516). The statistic rows name its countries, so
    # fetch those countries in full for complete details.
    if more and place.region and stats and len(stats) <= FETCH_LIMIT:
        names = list(dict.fromkeys(
            n for r in stats for n in _country_names(r, every_spelling=True) if place.region in regions_of(n)
        ))
        if len(names) <= REGION_COUNTRY_QUERIES:
            by_key: dict[tuple, dict] = {}
            for name in names:
                _, country_features, _, country_requests = await _fetch({**params, "country": name}, FETCH_LIMIT)
                requests += country_requests
                for feature in country_features:
                    props = feature.get("properties", {})
                    if place.keeps(_country_names(props, every_spelling=True), *_point(feature), props.get("name")):
                        by_key[(props.get("eventtype"), props.get("eventid"))] = feature
            if len(by_key) >= len(stats):
                features, more = list(by_key.values()), False
    pairs = keep(features)

    # When not every match was fetched, Green events crowd out the serious
    # ones. Fetch Orange/Red separately (a small, complete set).
    levels = set([alertlevel] if isinstance(alertlevel, str) else alertlevel or [])
    serious = sorted(levels & {"Orange", "Red"})
    serious_complete = False
    if more and "Green" in levels and serious:
        serious_found = await _search({**params, "alertlevel": ";".join(serious)}, place)
        requests += serious_found.requests
        seen = {(e["type"], e["id"]) for _, e in pairs}
        pairs += [(f, e) for f, e in keep(serious_found.features) if (e["type"], e["id"]) not in seen]
        serious_complete = not serious_found.more

    pairs.sort(key=lambda p: _importance(p[1]), reverse=True)
    return _Found(
        searched.first, pairs, more, requests, params, serious_complete, stats,
        sum(matched.values()) if matched else None, searched.checked,
    )


async def _unknown_places(place: _Place, types: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Names GDACS has never recorded as a country, checked against everything since 2000.

    Returns the names it has never recorded at all ("Queensland", "Atlantis";
    the same whole-word rule, so "Guin" is unknown), and the names it has
    recorded only in earthquake region names, with examples ("California":
    "Southern California", "Gulf Of California"). GDACS names earthquakes by
    seismic region but other hazards by country, so a wildfire search for
    "California" finds nothing although the state has wildfires.
    """
    unknown: list[str] = []
    quake_regions: dict[str, list[str]] = {}
    for names, country in place.queries:
        for name in names:
            if not name or _norm(name) in COUNTRY_ALIASES:
                continue
            rows = await _statistic_rows({"country": name, "fromDate": "2000-01-01", "alertlevel": "Green;Orange;Red"})
            rows = [r for r in rows if _in_country(_country_names(r, every_spelling=True), country)]
            if not rows:
                unknown.append(name)
            elif types != ["EQ"] and all(r.get("eventtype") == "EQ" for r in rows):
                quake_regions[name] = [n for n, _ in Counter(r.get("country") or "" for r in rows).most_common(2)]
    return unknown, quake_regions


def _partial_area(place: _Place, found: _Found, counts: str = "") -> str:
    """tell_user text for a box or near search that hit FETCH_LIMIT."""
    wider = f"in {', '.join(place.countries)}" if place.countries else "worldwide"
    checked = f"the first {found.checked:,} of {found.matched:,}" if found.matched else f"the first {found.checked:,}"
    serious = " All Orange and Red events were checked." if found.serious_complete else ""
    return (
        f"GDACS has no location filter, so only {checked} events {wider} were checked for "
        f"{place.name}: the area was only partly searched.{serious}{counts} Narrow the dates or "
        "hazard, or add the country, for a complete search."
    )


def _latest(pairs: list[tuple[dict, dict]]) -> dict:
    """The event with the latest to_date (the list is sorted by importance, not date)."""
    e = max((e for _, e in pairs), key=lambda e: (e["to_date"] or "", e["from_date"] or ""))
    return _public({k: e[k] for k in ("name", "alert_level", "countries", "from_date", "to_date", "is_current")})


@mcp.tool()
async def search_gdacs_events(
    eventlist: EventType | list[EventType] | None = None,
    alertlevel: AlertLevel | list[AlertLevel] | None = None,
    country: str | list[str] | None = None,
    bbox: str | list[float] | None = None,
    near: str | list[float] | None = None,
    radius_km: float = DEFAULT_RADIUS_KM,
    name: str | None = None,
    since_days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    active_only: bool = False,
    min_severity: float | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    format: OutputFormat = "compact",
) -> str:
    """Search GDACS for natural disasters worldwide, current or past.

    Earthquakes, floods, tropical cyclones, volcanoes, wildfires and
    droughts, from the Global Disaster Alert and Coordination System.
    GDACS records events that have happened; it has no forecasts.

    Args:
    eventlist: "EQ" earthquake, "FL" flood, "TC" tropical cyclone, "VO"
        volcano, "WF" wildfire, "DR" drought, or a list. Unset: all.
        Tsunamis ("TS") cannot be searched.
    alertlevel: "Green", "Orange", "Red", or a list. Levels measure expected
        humanitarian impact, not size: a strong earthquake far from people
        can be Green. Unset: all three. Narrow it for "serious" or "worst".
    country: Where to search, in English. One country, a list of countries
        (counted together, each event once), a continent or region (Africa;
        North, West, East, Central, Southern Africa; Europe; Asia; Middle
        East; South, Southeast, East, Central Asia; North, Central, South,
        Latin America; Caribbean; Americas; Oceania) or a sea or ocean (Bay
        of Bengal, Arabian Sea, South China Sea, Philippine Sea, Coral Sea,
        Gulf of Mexico, Mediterranean, Indian Ocean, North Indian Ocean,
        Pacific, Atlantic, each with North and South, Western Pacific,
        Arctic). Unset: worldwide.
        A name GDACS has never recorded returns an error saying so.
    bbox: A state, province or island, as "west,south,east,north" in
        degrees (geocode_place's bbox). Combine with the country it lies in.
    near: For "near [place]": "lat,lon" (geocode_place's top_result lat and
        lon). Keeps events within radius_km (default 100) of that point.
    name: A named event: a cyclone ("Melissa") or volcano ("Etna"), matched
        as whole words. Without eventlist, searches cyclones and volcanoes;
        with no dates, the last 5 years.
    since_days: The last N days, counted from today. Not with from_date
        or to_date. Without any dates: about the last 12 months
        (date_range says which period was searched).
    from_date: Start date, YYYY-MM-DD.
    to_date: End date, YYYY-MM-DD; needs from_date.
    active_only: Droughts, floods, cyclones and wildfires still ongoing,
        plus earthquakes and eruptions from the last 7 days (single moments,
        so recent, not ongoing). If none match, the response says so and
        gives the most recent match instead.
    min_severity: Minimum size in the hazard's unit: magnitude (EQ), wind
        km/h (TC) or burnt hectares (WF). Set eventlist to that hazard.
    max_results: How many key events to list, 1-100 (default 20). The
        summary always covers every match.
    format: "compact" (default) or "geojson" (full features, for maps).

    Returns:
        JSON. note (when present): instructions for you. tell_user (when
        present): caveats to include in the answer. total: matching events.
        summary, computed from every match: by_type, by_alert_level,
        countries (most first, up to 50), ongoing (how many droughts,
        floods, cyclones and wildfires are still going), date_span, largest (per hazard: the biggest by magnitude, wind,
        burnt area or drought area) and wildfire_burnt_area_ha.
        most_recent: the event with the latest to_date. events: the key
        events, highest alert level first, then most recent, each with name,
        alert_level, countries, from_date, is_current, then to_date for an
        event that has ended or last_updated for one still going (GDACS's
        latest update, not an end date), severity, lat, lon, report_url. On
        failure or no match, "error" says why.
    """

    today = datetime.now(UTC).date()
    if max_results < 1:
        return json.dumps({"error": "max_results must be at least 1."})
    capped = max_results > MAX_RESULTS_LIMIT
    max_results = min(max_results, MAX_RESULTS_LIMIT)
    if since_days is not None:
        if from_date or to_date:
            return json.dumps({"error": "Use since_days or from_date/to_date, not both."})
        if not 1 <= since_days <= MAX_SINCE_DAYS:
            return json.dumps({"error": f"since_days must be between 1 and {MAX_SINCE_DAYS}."})
        from_date = (today - timedelta(days=since_days)).isoformat()
    try:
        start = _parse_date("from_date", from_date)
        end = _parse_date("to_date", to_date, end_of_month=True)
        place = _parse_place(country, bbox, near, radius_km, name)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if end and not start:
        # GDACS returns nothing for toDate alone.
        return json.dumps({
            "error": "to_date requires from_date. For everything up to a date, "
            "also set an early from_date such as '2000-01-01'."
        })
    if start and end and start > end:
        return json.dumps({"error": "from_date must be on or before to_date."})
    if start and start > today.isoformat():
        return json.dumps({
            "error": f"from_date {start} is after today ({today.isoformat()}). GDACS has no "
            "forecasts, only events that have already started."
        })
    if min_severity is not None and min_severity < 0:
        return json.dumps({"error": "min_severity must be 0 or more."})
    if place.event_name and not start:
        # Named events ("Hurricane Melissa") are often older than the default
        # 12-month window.
        start = (today - timedelta(days=365 * NAME_SEARCH_YEARS)).isoformat()

    # GDACS's search returns nothing for "TS", and a list containing it
    # corrupts the rest ("EQ;TS" gives a small subset of earthquakes,
    # "FL;TS" gives nothing). Drop it, and send no filter when every
    # searchable type is asked for.
    types = list(dict.fromkeys([eventlist] if isinstance(eventlist, str) else eventlist or []))
    dropped_tsunami = "TS" in types
    types = [t for t in types if t != "TS"]
    if dropped_tsunami and not types:
        return json.dumps({
            "error": "GDACS has no tsunami (TS) events, so tsunamis cannot be searched. Tell the "
            "user, and offer the earthquakes (eventlist='EQ') for the same place and period."
        })
    if place.event_name and not types:
        # Only cyclones ("MELISSA-25") and volcanoes ("Eruption Etna") have
        # names in GDACS; other events are named "Flood in [country]".
        types = ["TC", "VO"]
    if place.event_name and not set(types) & {"TC", "VO"}:
        # A name over earthquakes or floods only matches country words in their
        # titles: name="Guinea" finds "Earthquake in Papua New Guinea".
        return json.dumps({
            "error": "Only tropical cyclones (TC) and volcanoes (VO) have names in GDACS, e.g. 'MELISSA-25', "
            "'Etna'. Other events are titled after their country ('Earthquake in Peru'): search them with "
            "country instead of name."
        })
    if set(types) >= SEARCHABLE_TYPES:
        types = []

    # GDACS leaves Green out unless asked, but most events are Green, and
    # agents left alertlevel unset for "how many" and "what happened"
    # questions and got wrong counts. Every level is the default here; an
    # agent narrows it only for "serious" or "worst".
    if alertlevel is None:
        alertlevel = ["Green", "Orange", "Red"]
    levels = [alertlevel] if isinstance(alertlevel, str) else list(alertlevel)

    params = {
        "eventlist": _join(types),
        "alertlevel": _join(alertlevel),
        "fromDate": start,
        "toDate": end,
        "severity": min_severity,
    }
    params = {k: v for k, v in params.items() if v is not None and v != ""}
    included_green = "Green" in levels
    # GDACS's dates match events that overlap the period, so for active_only
    # only events updated in the active window are needed: the same results,
    # without paging through a year of finished events.
    cutoff = (today - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()
    active_params = {**params, "fromDate": max(start or cutoff, cutoff)}

    # What was searched: in every response, including empty ones.
    searched_meta: dict[str, Any] = {
        "date_range": (
            {"from": start, "to": end or today.isoformat()} if start
            else {"from": "about 12 months ago (default)", "to": today.isoformat()}
        )
    }
    if place.countries:
        searched_meta["country_searched"] = place.countries
    if place.region:
        searched_meta["region_searched"] = place.region
    if place.box:
        searched_meta["area_searched"] = {"name": place.name, "bbox": list(place.box)}
    if place.near:
        searched_meta["area_searched"] = {"near": list(place.near[:2]), "radius_km": place.near[2]}
    if place.event_name:
        searched_meta["name_searched"] = name

    try:
        found = await _gather(active_params if active_only else params, place, alertlevel, active_only)
        if active_only and not found.pairs:
            # "Is anything active?" -> no; the agent then needs the most
            # recent event, so fetch it here rather than leave a second
            # search to the agent.
            recent = await _gather(params, place, alertlevel, False)
            if recent.pairs:
                latest = _latest(recent.pairs)
                where = ", ".join(latest["countries"]) or "no country given"
                return json.dumps({
                    "tell_user": [
                        f"Nothing matching is active now. The most recent match was {latest['name']} "
                        f"({latest['alert_level']}, {where}), {latest['from_date']} to "
                        f"{latest.get('to_date') or latest.get('last_updated')}."
                    ],
                    "retrieved_at": today.isoformat(),
                    "total": 0,
                    "most_recent": latest,
                    "count": 0,
                    "truncated": False,
                    **searched_meta,
                    "events": [],
                })
    except Exception as exc:
        return json.dumps({"error": f"Disaster search failed: {exc}"})

    pairs = found.pairs
    partial_area = place.by_coords and found.more
    if partial_area and not pairs:
        # The events checked were only part of the wider search, so an empty
        # area is not "no results".
        return json.dumps({
            "tell_user": [_partial_area(place, found)],
            "retrieved_at": today.isoformat(),
            "total": 0,
            "total_is_lower_bound": True,
            "count": 0,
            "truncated": True,
            **searched_meta,
            "events": [],
        })
    if found.first is None or not pairs:
        try:
            unknown, quake_regions = await _unknown_places(place, types)
        except Exception:
            unknown, quake_regions = [], {}
        if unknown:
            return json.dumps({
                "error": f"{', '.join(map(repr, unknown))} is not a country, region or sea that GDACS "
                "has ever recorded. If it is a state, province, island or city, call geocode_place and "
                "pass its bbox (or near, for 'near [place]') with the country it lies in. Otherwise "
                "check the spelling, or tell the user GDACS has no data for it."
            })
        if quake_regions:
            name, examples = next(iter(quake_regions.items()))
            return json.dumps({
                "error": f"GDACS has recorded {name!r} only in earthquake region names (e.g. "
                f"{', '.join(map(repr, examples))}); other hazards are recorded by country. If it is a "
                "state, province, island or city, call geocode_place and pass its bbox (or near, for "
                "'near [place]') with the country it lies in."
            })
        name_from = start if place.event_name and not (from_date or since_days) else None
        return _no_results(place, included_green, name_from)
    events = [e for _, e in pairs]

    # Normally every match has been fetched, and everything is computed from
    # them. Above FETCH_LIMIT, GDACS's statistic endpoint still counts every
    # match exactly (it has no severity filter, "current" flag or
    # coordinates, so not with min_severity, active_only or a box); the
    # details then come from the events fetched.
    stats = found.stats if found.more and not active_only and not place.by_coords else None
    if not found.more:
        total, lower_bound = len(events), False
        summary = _counts(events, place.region) | _details(events)
    elif stats:
        total, lower_bound = max(len(stats), len(events)), False
        summary = _counts([_row_event(r) for r in stats], place.region) | _details(events)
    else:
        total, lower_bound = len(events), True
        summary = _counts(events, place.region) | _details(events)

    count = min(len(events), max_results)
    truncated = lower_bound or total > count

    # note: what the agent should know or do. tell_user: caveats for the answer.
    notes: list[str] = []
    tell_user: list[str] = []
    if place.region:
        notes.append(f"total and summary cover only {place.name}; summary.countries lists only its countries.")
        spanning = sorted(TRANSCONTINENTAL & set(summary["countries"]))
        if spanning and place.region in {"Europe", "Asia"}:
            tell_user.append(
                f"{' and '.join(spanning)} lie in both Europe and Asia; all their events are counted, "
                "including those in the other continent."
            )
    if place.box:
        tell_user.append(
            f"Events in {place.name} were matched by their coordinates, so the area is approximate "
            "and includes nearby coasts."
        )
    if place.near:
        tell_user.append(f"Only events within {place.near[2]:g} km of the place were included.")
    if not start and not active_only:
        tell_user.append("No dates were given, so this covers about the last 12 months.")
    if dropped_tsunami:
        tell_user.append("GDACS has no tsunami data, so tsunamis are not included.")
    if active_only and any(e["_recent"] for e in events):
        tell_user.append(
            f"Earthquakes and volcanic eruptions are single moments, so none is ongoing; those from "
            f"the last {ACTIVE_WINDOW_DAYS} days are included as recent events."
        )
    if capped:
        notes.append(f"max_results was lowered to the limit of {MAX_RESULTS_LIMIT}.")
    left_out = [lvl for lvl in ("Red", "Orange", "Green") if lvl not in levels]
    if left_out:
        searched = [lvl for lvl in ("Red", "Orange", "Green") if lvl in levels]
        tell_user.append(
            f"Only {' and '.join(searched)} alert events were searched; "
            f"{' and '.join(left_out)} events are not included."
        )
    if found.more and not lower_bound:
        tell_user.append(
            f"The counts are exact, but the largest events, ongoing count, burnt area and most recent "
            f"event come from {len(events)} of the {total} matches. Narrow the dates or area for "
            "complete figures."
        )
    elif partial_area:
        tell_user.append(_partial_area(place, found, " Counts are minimums."))
    elif lower_bound:
        complete = " Orange and Red counts are complete." if found.serious_complete else ""
        tell_user.append(
            f"Counts are minimums: more than {FETCH_LIMIT} events matched.{complete} Narrow the dates "
            "or area for exact figures."
        )
    if truncated:
        notes.append(
            f"events lists the {count} key events (highest alert, then most recent) of {total}; use "
            "summary for counts, largest events and how many are ongoing."
        )

    meta: dict[str, Any] = {}
    if notes:
        meta["note"] = " ".join(notes)
    if tell_user:
        meta["tell_user"] = tell_user
    meta |= {
        "retrieved_at": today.isoformat(),
        "total": total,
        "total_is_lower_bound": lower_bound,
        "summary": summary,
        "most_recent": _latest(pairs),
        "count": count,
        "truncated": truncated,
    }
    meta |= searched_meta

    if format == "geojson":
        first = found.first
        first["features"] = [f for f, _ in pairs[:max_results]]
        if found.requests > 1:
            # The API's bbox only covers its first page.
            first.pop("bbox", None)
        return json.dumps({**meta, **first})

    return json.dumps({**meta, "events": [_public(e) for e in events[:max_results]]})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Disasters MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    mcp.settings.port = args.port

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
