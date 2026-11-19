"""
GDACS (Disasters) MCP Server
============================
An MCP server providing global natural disaster and hazard monitoring
via GDACS (Global Disaster Alert and Coordination System), a joint
initiative of the UN and European Commission. Use to search for
natural disaster events worldwide covering earthquakes, floods,
volcanoes, wildfires, droughts, cyclones and tsunamis.

Tools:
    search_gdacs_events — search disasters by hazard type, alert level,
                        country and date range

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
import sys
import unicodedata
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)

GDACS_SEARCH_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
TIMEOUT = 60
GDACS_USER_AGENT = "DisastersMCPServer"
GDACS_PAGE_SIZE = 100  # fixed by the API; its pagesize parameter is ignored
MAX_RESULTS_LIMIT = 100  # events listed; more only invites the agent to pad its answer
FETCH_LIMIT = 500  # events counted for the summary, per country spelling
DEFAULT_MAX_RESULTS = 50
RETRY_DELAYS = (0.5, 1.5)  # GDACS sometimes fails a valid request once
MAX_SINCE_DAYS = 3650
ACTIVE_WINDOW_DAYS = 7  # droughts stay ongoing without the iscurrent flag

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

mcp = FastMCP("Disasters", host="0.0.0.0", port=8000, stateless_http=True)


async def _http_get(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Perform an async HTTP GET, retrying transient failures."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        for delay in (*RETRY_DELAYS, None):
            try:
                resp = await client.get(url, params=params, headers=headers)
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


def _norm(name: str) -> str:
    """Lower-case, accent-free, single-spaced form of a country name."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = " ".join(name.casefold().replace("’", "'").split())
    return name.removeprefix("the ")


# GDACS's country filter is a case-insensitive substring match on its own
# spellings, and it spells some countries several ways (seen in its data
# 2019-2026). For these countries every spelling is searched, and only events
# naming one of them exactly are kept, so "Republic of Korea" does not pull in
# "Democratic Republic of Korea".
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
    (["Eswatini", "Swaziland"], ["Eswatini", "Swaziland"]),
    (["Czechia", "Czech Republic"], ["Czech Republic"]),
    (["Ivory Coast", "Cote d'Ivoire"], ["Cote d'Ivoire"]),
    (["Cabo Verde", "Cape Verde"], ["Cape Verde"]),
    (["Myanmar", "Burma"], ["Myanmar"]),
    (["Laos", "Lao PDR"], ["Laos"]),
    (["Bahamas"], ["Bahamas", "The Bahamas"]),
    (["Sao Tome and Principe"], ["São Tomé and Príncipe"]),
    (["United States", "USA", "US", "United States of America", "America"], ["United States"]),
    (["United Kingdom", "UK", "Great Britain", "Britain"], ["United Kingdom"]),
    (["Netherlands", "Holland"], ["Netherlands"]),
]
COUNTRY_ALIASES: dict[str, list[str]] = {
    _norm(typed): spellings
    for names, spellings in _COUNTRY_SPELLINGS
    for typed in names + spellings
}

DISPLAY_NAMES: dict[str, str] = {
    _norm(name): names[0]
    for names, spellings in _COUNTRY_SPELLINGS
    for name in names + spellings
}

EVENT_TYPE_NAMES = {
    "EQ": "earthquake", "TC": "tropical cyclone", "FL": "flood",
    "VO": "volcano", "WF": "wildfire", "DR": "drought", "TS": "tsunami",
}


def _join(value: str | list[str] | None) -> str | None:
    """GDACS takes several values as one ';'-separated string."""
    if value is None or isinstance(value, str):
        return value
    return ";".join(dict.fromkeys(value)) or None


def _parse_date(name: str, value: str | None) -> str | None:
    """Validate a YYYY-MM-DD date and return it in ISO form."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(
            f"{name} must be a date in YYYY-MM-DD format, got {value!r}."
        ) from None


def _country_names(props: dict, every_spelling: bool = False) -> list[str]:
    """The countries an event names, from affectedcountries and country.

    Neither field is complete on its own, and they spell countries
    differently ("South Korea" / "Republic of Korea", "Vanuatu" / "Vanuatu
    Islands"). Known spellings get one display name, and a name that only
    extends one already listed is dropped. every_spelling returns the raw
    names instead, for matching.
    """
    affected = [c.get("countryname", "") for c in props.get("affectedcountries") or []]
    raw = [n.strip() for n in affected + (props.get("country") or "").split(",") if n.strip()]
    if every_spelling:
        return list(dict.fromkeys(raw))
    names: list[str] = []
    for name in raw:
        name = DISPLAY_NAMES.get(_norm(name), name)
        key = _norm(name)
        if not any(key == _norm(kept) or key.startswith(_norm(kept) + " ") for kept in names):
            names.append(name)
    return names


def _compact(feature: dict) -> dict:
    """The fields an agent needs from one GDACS GeoJSON feature."""
    props = feature.get("properties", {})
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
        "to_date": (props.get("todate") or "")[:10] or None,
        "is_current": str(props.get("iscurrent")).lower() == "true",
        "severity": (props.get("severitydata") or {}).get("severitytext"),
        "lat": coords[1],
        "lon": coords[0],
        "report_url": (props.get("url") or {}).get("report"),
    }


async def _fetch(params: dict[str, Any], max_results: int) -> tuple[dict | None, list[dict], bool, int]:
    """Fetch up to max_results features, page by page.

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
        if len(features) >= max_results:
            # A full page means there may be another one.
            return first, features, True, page
        page += 1


def _no_results(country: str | None, aliased: bool, included_green: bool, active_only: bool) -> str:
    message = "No results found for query."
    if included_green:
        message += " Green (low-impact) events were included in the search."
    if active_only:
        message += " Only active events were kept; retry without active_only for recent ones."
    if country and not aliased:
        message += (
            f" GDACS matches country names in its own spelling, so check {country!r}"
            " (e.g. 'Türkiye', 'Viet Nam'), or search without country and filter"
            " on the countries field."
        )
    return json.dumps({"error": message})


async def _search(
    params: dict[str, Any], searches: list[str | None], spellings: list[str] | None
) -> tuple[dict | None, list[dict], bool, int]:
    """Run one search per country spelling and merge the results."""
    first: dict | None = None
    features: list[dict] = []
    seen: set[tuple] = set()
    more = False
    requests = 0
    for name in searches:
        query = {**params, "country": name} if name else params
        page_first, page_features, page_more, page_requests = await _fetch(query, FETCH_LIMIT)
        first = first or page_first
        more = more or page_more
        requests += page_requests
        for feature in page_features:
            props = feature.get("properties", {})
            key = (props.get("eventtype"), props.get("eventid"))
            if key not in seen:
                seen.add(key)
                features.append(feature)
    if spellings:
        wanted = {_norm(s) for s in spellings}
        features = [
            f for f in features
            if wanted & {_norm(n) for n in _country_names(f.get("properties", {}), every_spelling=True)}
        ]
    return first, features, more, requests


def _summary(events: list[dict]) -> dict:
    """Counts across every matching event, not only the ones returned."""
    alerts = Counter(e["alert_level"] for e in events)
    countries = Counter(c for e in events for c in e["countries"])
    for name in sorted(countries, key=len, reverse=True):
        # Events at sea name only a region, e.g. "Vanuatu Islands": count them
        # under the country if it appears too.
        base = next((c for c in countries if c != name and _norm(name).startswith(_norm(c) + " ")), None)
        if base:
            countries[base] += countries.pop(name)
    return {
        "by_type": dict(Counter(e["type_name"] for e in events).most_common()),
        "by_alert_level": {lvl: alerts[lvl] for lvl in ("Red", "Orange", "Green") if alerts[lvl]},
        "top_countries": dict(countries.most_common(10)),
    }


@mcp.tool()
async def search_gdacs_events(
    eventlist: EventType | list[EventType] | None = None,
    alertlevel: AlertLevel | list[AlertLevel] | None = None,
    country: str | None = None,
    since_days: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    active_only: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
    format: OutputFormat = "compact",
) -> str:
    """Search GDACS for natural disaster events worldwide, current or past.

    Use this when the user asks about disasters, hazards or emergencies,
    e.g. "any active volcanoes right now", "is there flooding in
    Bangladesh", "red alert earthquakes in Turkey in 2023". The data comes
    from the Global Disaster Alert and Coordination System (GDACS) and is
    not limited to any one country. GDACS reports events that have
    happened; it has no forecasts.

    Choosing arguments:
    - "right now", "currently", "active" -> active_only=True
    - "this week", "last few weeks", "recently" -> since_days (7, 21, ...)
    - a fixed past period ("in 2023") -> from_date and to_date
    - "small", "minor", "all", "every" -> alertlevel=["Green", "Orange", "Red"]
    - "how many" -> read summary and total; do not raise max_results
    - "list every ..." -> keep the default max_results, give the summary
      and say the list is partial if truncated is true
    Report only what the returned fields say: do not add places, magnitudes
    or statistics that are not in them.

    Args:
    eventlist: Hazard type, or a list of them (e.g. ["EQ", "TC"]):
        - "EQ": seismic events
        - "FL": river/flash flooding
        - "VO": volcanic eruptions or unrest
        - "WF": forest/bush fires
        - "DR": prolonged water shortage events
        - "TC": hurricanes, typhoons, cyclones
        - "TS": tsunami warnings/events
        Leave unset to return all hazard types.
    alertlevel: Severity, or a list of them. "Green" = low impact,
        "Orange" = medium impact, "Red" = high impact/major disaster.
        Leave unset for Orange and Red only (GDACS leaves Green out by
        default; if nothing matches, Green events are returned instead).
        Pass ["Green", "Orange", "Red"] when the user asks for all, small or
        minor events.
    country: Optional country name in English (e.g. "Philippines", "Turkey").
        One country only: for several, call once per country. Common names
        are mapped to GDACS's spellings. Leave unset to search globally.
    since_days: Events from the last N days (1-3650), counted from today on
        the server. Use this for relative periods ("this week" = 7, "last
        few weeks" = 21, "this year" = days since 1 January) instead of
        working out dates yourself. Cannot be combined with from_date or
        to_date.
    from_date: Optional start date, YYYY-MM-DD, for fixed past periods
        ("in 2023", "February 2020"). Leave all dates unset for roughly the
        last 12 months, plus long-running events such as droughts.
    to_date: Optional end date, YYYY-MM-DD. Requires from_date.
    active_only: True keeps only events still ongoing (GDACS marks them
        current, or updated them in the last 7 days). Use for "right now",
        "currently", "active".
    max_results: Number of events to list (1-100, default 50), newest first.
        Values above 100 are lowered to 100. The summary counts every match
        (up to 500), so for "how many" questions read the summary instead.
    format: "compact" (default) returns a short record per event.
        "geojson" returns GDACS's full GeoJSON features, about five times
        larger; use it only to map the events.

    Returns:
        JSON string with:
        - note (when present, always first): read it before the data, act on
          it if it changes the answer (e.g. retry with Green included), and
          tell the user what it says
        - retrieved_at: today's date (UTC)
        - total: events matching the query; total_is_lower_bound=true means
          GDACS has more than were counted
        - summary: counts of all matches by type, alert level and country
        - count, truncated: how many events are listed, and whether that is
          fewer than total. A truncated list is partial: say so.
        - events: newest first, each with id, type, type_name, name,
          alert_level, countries, from_date, to_date, is_current, severity,
          lat, lon and report_url
        If nothing matches, an "error" says so and why.
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
        end = _parse_date("to_date", to_date)
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

    params = {
        "eventlist": _join(eventlist),
        "alertlevel": _join(alertlevel),
        "fromDate": start,
        "toDate": end,
    }
    params = {k: v for k, v in params.items() if v}

    spellings = COUNTRY_ALIASES.get(_norm(country)) if country else None
    searches: list[str | None] = spellings or [country]

    fell_back_to_green = False
    try:
        first, features, more, requests = await _search(params, searches, spellings)
        if not features and alertlevel is None:
            # GDACS leaves Green out by default; an agent asking "any
            # earthquakes on 31 December?" wants the Green ones too.
            first, features, more, requests = await _search(
                {**params, "alertlevel": "Green"}, searches, spellings
            )
            fell_back_to_green = bool(features)
    except Exception as exc:
        return json.dumps({"error": f"Disaster search failed: {exc}"})

    pairs = [(f, _compact(f)) for f in features]
    if active_only:
        cutoff = (today - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()
        pairs = [(f, e) for f, e in pairs if e["is_current"] or (e["to_date"] or "") >= cutoff]
    pairs.sort(key=lambda p: p[1]["from_date"] or "", reverse=True)

    if first is None or not pairs:
        included_green = alertlevel is None or "Green" in (
            [alertlevel] if isinstance(alertlevel, str) else alertlevel
        )
        return _no_results(country, bool(spellings), included_green, active_only)

    total = len(pairs)
    count = min(total, max_results)
    truncated = more or total > max_results
    plus = "+" if more else ""
    meta: dict[str, Any] = {
        "retrieved_at": today.isoformat(),
        "total": total,
        "total_is_lower_bound": more,
        "summary": _summary([e for _, e in pairs]),
        "count": count,
        "truncated": truncated,
    }
    notes = []
    if capped:
        notes.append(f"max_results was lowered to the limit of {MAX_RESULTS_LIMIT}.")
    if fell_back_to_green:
        notes.append("No Orange or Red events matched, so these are Green (low-impact) events.")
    elif alertlevel is None:
        notes.append(
            "Green (low-impact) events are not included; pass "
            "alertlevel=['Green', 'Orange', 'Red'] to include them."
        )
    if truncated:
        notes.append(
            f"Listing the newest {count} of {total}{plus} matching events: this is a partial "
            f"list. The summary counts all {total}{plus}."
        )
    if notes:
        # First key in the response, so the agent reads it before the data.
        meta = {"note": " ".join(notes), **meta}
    if start:
        meta["date_range"] = {"from": start, "to": end or today.isoformat()}
    if spellings:
        meta["country_searched"] = spellings

    if format == "geojson":
        first["features"] = [f for f, _ in pairs[:max_results]]
        if requests > 1:
            # The API's bbox only covers its first page.
            first.pop("bbox", None)
        return json.dumps({**meta, **first})

    return json.dumps({**meta, "events": [e for _, e in pairs[:max_results]]})


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
