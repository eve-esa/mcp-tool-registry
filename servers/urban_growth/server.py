"""
Urban Growth MCP Server
=======================
An MCP server that measures how fast a city's built-up area grows year by year,
and what kind of land it replaces, from 10 m annual land cover maps on the
Microsoft Planetary Computer. No API key needed.

Data:
    Esri / Impact Observatory 10 m Annual Land Use Land Cover v02 (2017-2023)
    ESA WorldCover 10 m (2020, 2021)

Tools:
    urban_growth_timeseries — built-up area per year, growth rate (SDG 11.3.1
                              land consumption rate), land converted, map links
    landcover_summary       — land cover class shares for one year (ESA WorldCover)

Usage:
    python server.py                              # HTTP transport
    python server.py --transport stdio            # stdio transport (local testing)

Requirements:
    pip install "mcp[cli]==1.27.0" httpx

Environment (optional):
    NOMINATIM_USER_AGENT — identifies this client to OpenStreetMap Nominatim
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("urban-growth-mcp")

PC_API = "https://planetarycomputer.microsoft.com/api"
STAC_SEARCH_URL = f"{PC_API}/stac/v1/search"
STATS_URL = f"{PC_API}/data/v1/item/statistics"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = os.getenv("NOMINATIM_USER_AGENT", "urban-growth-mcp/0.1 (agentic-eo-hackathon)")
TIMEOUT = 120
KM_PER_DEG_LAT = 111.0
MAX_AREA_KM2 = 2500.0  # roughly 50 km x 50 km; keeps requests fast and answers small

# Esri / Impact Observatory annual land cover (io-lulc-annual-v02).
IO_COLLECTION = "io-lulc-annual-v02"
IO_ASSET = "data"
IO_YEARS = range(2017, 2024)
IO_BUILT = 7
IO_IGNORE = {0, 10}  # no data, clouds
IO_LEGEND = {
    1: "Water",
    2: "Trees",
    4: "Flooded vegetation",
    5: "Crops",
    7: "Built area",
    8: "Bare ground",
    9: "Snow/ice",
    11: "Rangeland",
}

# ESA WorldCover.
WC_COLLECTION = "esa-worldcover"
WC_ASSET = "map"
WC_YEARS = {2020: "v100", 2021: "v200"}
WC_LEGEND = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare / sparse vegetation",
    70: "Snow and ice",
    80: "Permanent water bodies",
    90: "Herbaceous wetland",
    95: "Mangroves",
    100: "Moss and lichen",
}

mcp = FastMCP("Urban Growth", host="0.0.0.0", port=8000, stateless_http=True)

BBox = tuple[float, float, float, float]

RETRY_STATUS = {429, 500, 502, 503, 504}
RETRIES = 3


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """HTTP call that retries temporary upstream errors (rate limit, 5xx) with a short backoff."""
    for attempt in range(RETRIES):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code not in RETRY_STATUS or attempt == RETRIES - 1:
                resp.raise_for_status()
                return resp
        except (httpx.ConnectError, httpx.ReadTimeout):
            if attempt == RETRIES - 1:
                raise
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _parse_bbox(bbox: str) -> BBox:
    """Parse and validate a "west,south,east,north" string."""
    try:
        west, south, east, north = (float(v) for v in bbox.split(","))
    except ValueError as exc:
        raise ValueError(f"bbox must be 'west,south,east,north' in decimal degrees, got '{bbox}'") from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise ValueError("bbox values must be finite numbers")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must satisfy -180<=west<east<=180 and -90<=south<north<=90 (no antimeridian crossing)")
    return west, south, east, north


def _area_km2(west: float, south: float, east: float, north: float) -> float:
    """Approximate area of a lon/lat rectangle in km2."""
    mid_lat = math.radians((south + north) / 2)
    return (north - south) * KM_PER_DEG_LAT * (east - west) * KM_PER_DEG_LAT * math.cos(mid_lat)


def _polygon(west: float, south: float, east: float, north: float) -> dict[str, Any]:
    """GeoJSON Feature for a lon/lat rectangle."""
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


def _contains(outer: BBox, inner: BBox) -> bool:
    """True when the `inner` lon/lat rectangle lies inside `outer`."""
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _bbox_str(box: BBox) -> str:
    return ",".join(f"{v:.5f}" for v in box)


def _tile_bbox(item: dict[str, Any]) -> BBox:
    """Real WGS84 extent of a catalogue item.

    Esri tiles in UTM zones 01 and 60 straddle the date line, and the catalogue labels them with a
    global bbox (-180..180). Searches anywhere in their latitude band then return them, and asking
    for statistics outside their real data fails with a 500. The zone number is in the item id
    (e.g. "60H-2023"), so rebuild the extent from it: 6 degrees wide plus a small overlap margin.
    """
    west, south, east, north = item["bbox"]
    if east - west > 180:
        match = re.match(r"(\d{2})[C-X]-", item["id"])
        if not match:
            return (0.0, 0.0, 0.0, 0.0)  # unknown date-line item: empty extent, so it is ignored
        zone_west = -180.0 + 6.0 * (int(match.group(1)) - 1)
        return (zone_west - 0.6, south, zone_west + 6.0 + 0.6, north)
    return west, south, east, north


async def _resolve_area(place_name: str, bbox: str, radius_km: float) -> tuple[BBox, dict[str, Any] | None]:
    """Turn either a bbox string or a place name into a validated bbox (plus place info)."""
    if bbox:
        box = _parse_bbox(bbox)
        place = None
    elif place_name:
        if not 0 < radius_km <= 25:
            raise ValueError("radius_km must be greater than 0 and at most 25")
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": NOMINATIM_USER_AGENT}) as client:
            resp = await _request(
                client, "GET", NOMINATIM_URL, params={"q": place_name, "format": "json", "limit": "1", "accept-language": "en"}
            )
            matches = resp.json()
        if not matches:
            raise LookupError(f"No place found for '{place_name}'. Try a more specific name or pass a bbox.")
        lat, lon = float(matches[0]["lat"]), float(matches[0]["lon"])
        d_lat = radius_km / KM_PER_DEG_LAT
        d_lon = radius_km / (KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
        box = (max(lon - d_lon, -180.0), max(lat - d_lat, -90.0), min(lon + d_lon, 180.0), min(lat + d_lat, 90.0))
        place = {"name": matches[0].get("display_name", place_name), "lat": lat, "lon": lon, "radius_km": radius_km}
    else:
        raise ValueError("Pass either place_name or bbox.")
    area = _area_km2(*box)
    if area > MAX_AREA_KM2:
        raise ValueError(f"Area is about {area:.0f} km2; the maximum is {MAX_AREA_KM2:.0f} km2. Use a smaller box.")
    return box, place


async def _class_counts(
    client: httpx.AsyncClient, collection: str, asset: str, box: BBox, year: int
) -> tuple[dict[int, float], list[str]]:
    """Pixel counts per class inside the box for one year, summed over all tiles the box touches."""
    west, south, east, north = box
    search = await _request(
        client,
        "POST",
        STAC_SEARCH_URL,
        json={
            "collections": [collection],
            "bbox": [west, south, east, north],
            "datetime": f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z",
        },
    )
    # An annual map's interval ends on 1 January of the next year, so the search also
    # returns the previous year's tiles. Keep only the items that start in `year`.
    items = [
        f
        for f in search.json().get("features", [])
        if (f["properties"].get("start_datetime") or f["properties"].get("datetime") or "")[:4] == str(year)
    ]
    boxes = {f["id"]: _tile_bbox(f) for f in items}
    # Keep only tiles whose real extent touches the area (see _tile_bbox for the date-line tiles).
    items = [f for f in items if boxes[f["id"]][0] < east and boxes[f["id"]][2] > west]
    if not items:
        raise LookupError(f"No {collection} data for {year} covers this area (open ocean or outside coverage).")

    # Neighbouring tiles overlap by a few km. Decide up front which tiles to read, the same way
    # every year: start with the tile nearest the box centre and skip any tile whose part of the
    # box already lies inside a chosen tile. This avoids counting the overlap twice and keeps the
    # yearly series consistent.
    centre = ((west + east) / 2, (south + north) / 2)

    def _distance(f: dict[str, Any]) -> float:
        b = boxes[f["id"]]
        return abs((b[0] + b[2]) / 2 - centre[0]) + abs((b[1] + b[3]) / 2 - centre[1])

    chosen: list[tuple[dict[str, Any], BBox]] = []
    # Ties (a box in the overlap of two tiles) are broken by tile name without the year
    # ("36R-2023" -> "36R"), so every year reads the same tile. The two tiles' maps can disagree in the
    # overlap, and switching between them would show up as fake growth.
    for item in sorted(items, key=lambda f: (round(_distance(f), 6), f["id"].split("-")[0])):
        t_west, t_south, t_east, t_north = boxes[item["id"]]
        clip = (max(west, t_west), max(south, t_south), min(east, t_east), min(north, t_north))
        if clip[0] >= clip[2] or clip[1] >= clip[3]:
            continue
        if any(_contains(boxes[c["id"]], clip) for c, _ in chosen):
            continue
        chosen.append((item, clip))

    counts: dict[int, float] = {}
    used: list[tuple[str, float]] = []
    failed: list[tuple[BBox, httpx.HTTPStatusError]] = []
    for item, clip in chosen:
        try:
            resp = await _request(
                client,
                "POST",
                STATS_URL,
                params={"collection": collection, "item": item["id"], "assets": asset, "categorical": "true"},
                json=_polygon(*clip),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            failed.append((clip, exc))
            continue
        stats = next(iter(resp.json()["properties"]["statistics"].values()))
        values, classes = stats["histogram"][0], stats["histogram"][1]
        for n_pixels, code in zip(values, classes, strict=True):
            counts[int(code)] = counts.get(int(code), 0.0) + n_pixels
        used.append((item["id"], _area_km2(*clip)))
    # A tile that fails is only safe to skip when a tile that worked covers the same area;
    # otherwise the totals would be wrong, so raise.
    ok_boxes = [boxes[item_id] for item_id, _ in used]
    for clip, exc in failed:
        if not any(_contains(b, clip) for b in ok_boxes):
            raise exc
    # Largest share of the box first, so callers can use [0] for the map.
    return counts, [item_id for item_id, _ in sorted(used, key=lambda u: u[1], reverse=True)]


def _map_url(item_id: str, box: BBox) -> str:
    """PNG of the box in the official Esri land cover colours (built area is red)."""
    query = urlencode(
        {"collection": IO_COLLECTION, "item": item_id, "assets": IO_ASSET, "colormap_name": "io-lulc-9-class", "max_size": 512}
    )
    return f"{PC_API}/data/v1/item/bbox/{_bbox_str(box)}.png?{query}"


def _error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPError):
        return json.dumps({"error": f"Upstream request failed (Planetary Computer or Nominatim): {exc}"})
    return json.dumps({"error": str(exc)})


@mcp.tool()
async def urban_growth_timeseries(
    place_name: str = "",
    bbox: str = "",
    radius_km: float = 10.0,
    start_year: int = 2017,
    end_year: int = 2023,
) -> str:
    """
    Measure urban expansion year by year: built-up area, growth rate and the land it replaced.

    Use this for questions like "how fast is Cairo growing?", "how much did the built-up
    area of X increase since 2017?" or "is the city expanding into farmland?". Uses the
    Esri / Impact Observatory 10 m annual land cover maps (2017-2023). Free, no API key.
    Pass either place_name or bbox.

    Args:
        place_name: Place to analyse, e.g. "New Cairo, Egypt". A square box of
                    radius_km around its centre is used. Ignored if bbox is given.
        bbox: Area as "west,south,east,north" in decimal degrees (WGS84), the same
              format the geocode and EFFIS tools return. Maximum about 2500 km2.
        radius_km: Half the side of the box around place_name, in km (default 10, max 25).
        start_year: First year, 2017 to 2022 (default 2017).
        end_year: Last year, 2018 to 2023 (default 2023).

    Returns:
        JSON with:
        - "headline": one sentence summarising the change, safe to quote.
        - "years": per year built_km2, built_percent, change_km2 since the previous
          year, and map_url (PNG land cover map, built area in red; give the user
          this link exactly as returned, it is a complete URL with query string).
        - "summary": built area at start and end, total change in km2 and percent,
          average km2 per year, and land_consumption_rate (SDG 11.3.1 component,
          ln(end/start) / years).
        - "land_converted": net change in km2 of every other class between start and
          end (negative = lost), most lost first. This hints at what the city replaced.
        - "caveats": how to read the numbers.
        On failure returns JSON with an "error" field.
    """
    try:
        if not (IO_YEARS.start <= start_year < end_year <= IO_YEARS.stop - 1):
            raise ValueError(f"Need {IO_YEARS.start} <= start_year < end_year <= {IO_YEARS.stop - 1}.")
        box, place = await _resolve_area(place_name, bbox, radius_km)
        area = _area_km2(*box)
        years = list(range(start_year, end_year + 1))
        logger.info("Urban growth bbox=%s years=%s-%s", _bbox_str(box), start_year, end_year)

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            results = await asyncio.gather(*(_class_counts(client, IO_COLLECTION, IO_ASSET, box, y) for y in years))

        km2_per_class: list[dict[int, float]] = []
        rows = []
        for year, (counts, item_ids) in zip(years, results, strict=True):
            valid = sum(n for c, n in counts.items() if c not in IO_IGNORE)
            if valid == 0:
                raise LookupError(f"No valid land cover pixels in this area for {year}.")
            # Clouds are spread over the classes in proportion; no-data pixels (outside the map) are not.
            covered = area * (1 - counts.get(0, 0.0) / sum(counts.values()))
            km2 = {c: covered * n / valid for c, n in counts.items() if c not in IO_IGNORE}
            km2_per_class.append(km2)
            built = km2.get(IO_BUILT, 0.0)
            rows.append(
                {
                    "year": year,
                    "built_km2": round(built, 2),
                    "built_percent": round(100 * built / area, 2),
                    "change_km2": None if not rows else round(built - rows[-1]["built_km2"], 2),
                    "map_url": _map_url(item_ids[0], box),
                }
            )

        built_start, built_end = rows[0]["built_km2"], rows[-1]["built_km2"]
        n_years = end_year - start_year
        lcr = math.log(built_end / built_start) / n_years if built_start > 0 and built_end > 0 else None
        first, last = km2_per_class[0], km2_per_class[-1]
        converted = [
            {"class": IO_LEGEND.get(c, f"Unknown ({c})"), "change_km2": round(last.get(c, 0.0) - first.get(c, 0.0), 2)}
            for c in sorted(set(first) | set(last))
            if c != IO_BUILT
        ]
        converted.sort(key=lambda r: r["change_km2"])

        return json.dumps(
            {
                "dataset": "Esri / Impact Observatory 10 m Annual Land Use Land Cover v02 (Planetary Computer)",
                "place": place,
                "bbox": _bbox_str(box),
                "area_km2": round(area, 2),
                "headline": (
                    f"Built-up area {'grew' if built_end >= built_start else 'shrank'} from {built_start} km2 in "
                    f"{start_year} to {built_end} km2 in {end_year} "
                    f"({round(100 * (built_end - built_start) / built_start, 1) if built_start else 'n/a'}%), "
                    f"about {round((built_end - built_start) / n_years, 2)} km2 per year."
                ),
                "years": rows,
                "summary": {
                    "start_year": start_year,
                    "end_year": end_year,
                    "built_start_km2": built_start,
                    "built_end_km2": built_end,
                    "total_change_km2": round(built_end - built_start, 2),
                    "total_change_percent": round(100 * (built_end - built_start) / built_start, 1) if built_start else None,
                    "avg_change_km2_per_year": round((built_end - built_start) / n_years, 2),
                    "land_consumption_rate": round(lcr, 4) if lcr is not None else None,
                },
                "land_converted": converted,
                "caveats": (
                    "Annual maps are machine-learning classifications, so single-year changes can be noise; "
                    "trust the start-to-end trend more than year-to-year jumps. land_converted is a net "
                    "class balance for the box, not a pixel-by-pixel transition. "
                    "land_consumption_rate is one half of SDG 11.3.1 (it still needs a population growth rate)."
                ),
            }
        )
    except (ValueError, LookupError, httpx.HTTPError) as exc:
        return _error(exc)


@mcp.tool()
async def landcover_summary(
    place_name: str = "",
    bbox: str = "",
    radius_km: float = 5.0,
    year: int = 2021,
) -> str:
    """
    Summarise land cover for one year using ESA WorldCover (10 m, 11 classes).

    Use this to describe what covers an area right now: forest, shrubland, grassland,
    cropland, built-up, water, wetland. Useful context for fire, flood or drought
    analysis. For change over time use urban_growth_timeseries instead. Free, no API key.
    Pass either place_name or bbox.

    Args:
        place_name: Place to analyse, e.g. "Evia, Greece". A square box of radius_km
                    around its centre is used. Ignored if bbox is given.
        bbox: Area as "west,south,east,north" in decimal degrees (WGS84), e.g.
              "23.6,37.9,23.9,38.1" for Athens. Maximum about 2500 km2.
        radius_km: Half the side of the box around place_name, in km (default 5, max 25).
        year: WorldCover map year, 2020 or 2021 (default 2021).

    Returns:
        JSON with the land cover classes sorted by share (class, percent, area_km2),
        the dominant class, and the matched place if a name was given. On failure
        returns JSON with an "error" field.
    """
    try:
        if year not in WC_YEARS:
            raise ValueError(f"year must be one of {sorted(WC_YEARS)}, got {year}")
        box, place = await _resolve_area(place_name, bbox, radius_km)
        area = _area_km2(*box)
        logger.info("Land cover summary bbox=%s year=%s", _bbox_str(box), year)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            counts, item_ids = await _class_counts(client, WC_COLLECTION, WC_ASSET, box, year)
        total = sum(n for c, n in counts.items() if c != 0)
        if total == 0:
            raise LookupError("The area contains no valid land cover pixels.")
        covered = area * total / sum(counts.values())  # no-data pixels (outside the map) are not land
        classes = [
            {
                "class": WC_LEGEND.get(c, f"Unknown ({c})"),
                "percent": round(100 * n / total, 2),
                "area_km2": round(covered * n / total, 2),
            }
            for c, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            if c != 0
        ]
        return json.dumps(
            {
                "dataset": f"ESA WorldCover {year} ({WC_YEARS[year]}), 10 m (Planetary Computer)",
                "place": place,
                "bbox": _bbox_str(box),
                "area_km2": round(area, 2),
                "tiles_used": len(item_ids),
                "dominant_class": classes[0]["class"],
                "classes": classes,
            }
        )
    except (ValueError, LookupError, httpx.HTTPError) as exc:
        return _error(exc)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Urban Growth MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
