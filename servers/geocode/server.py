from __future__ import annotations

import json
import logging
import sys
from typing import Any

import httpx
from geopy.geocoders import Nominatim
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("geocode-mcp")

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "GeocodeMCPServer"
TIMEOUT = 60

mcp = FastMCP("Geocode", host="0.0.0.0", port=8000, stateless_http=True)
geolocator = Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=TIMEOUT)

async def _http_get(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Perform an async HTTP GET with sensible defaults."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp


@mcp.tool()
async def geocode_place(
    place_name: str,
    buffer_km: float = 0.0,
    limit: int = 5,
) -> str:
    """
    Convert a place name to a bounding box for use with the fire tools.

    Geocodes a place name (city, region, country, landmark, etc.) into a
    bounding box string in "west,south,east,north" format — the same format
    accepted by get_effis_burnt_areas and other tools.

    Uses the OpenStreetMap Nominatim API (free, no API key required).

    Args:
        place_name: Name of the place to geocode (e.g. "Greece",
                    "Athens", "Evia island", "Peloponnese").
        buffer_km:  Optional buffer in km to expand the bbox (default 0).
        limit:      Maximum number of candidate results to return (default 5).

    Returns:
        JSON with the top result's bbox string (ready to pass to other tools)
        and all candidate matches.
    """
    logger.info("Geocoding place: %s", place_name)

    try:
        resp = await _http_get(
            NOMINATIM_SEARCH_URL,
            params={
                "q": place_name,
                "format": "json",
                "limit": str(limit),
                "addressdetails": "1",
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
        )
        results = resp.json()
    except Exception as exc:
        return json.dumps({"error": f"Geocoding failed: {exc}"})

    if not results:
        return json.dumps(
            {
                "error": f"No results found for '{place_name}'.",
                "place_name": place_name,
            }
        )

    buf = buffer_km / 111.0

    def _parse_result(r: dict) -> dict:
        # Nominatim boundingbox is [south, north, west, east]
        bb = r.get("boundingbox", [])
        south, north, west, east = (
            float(bb[0]) - buf,
            float(bb[1]) + buf,
            float(bb[2]) - buf,
            float(bb[3]) + buf,
        )
        return {
            "display_name": r.get("display_name", ""),
            "bbox": f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}",
            "bbox_array": [
                round(west, 4),
                round(south, 4),
                round(east, 4),
                round(north, 4),
            ],
            "lat": float(r.get("lat", 0)),
            "lon": float(r.get("lon", 0)),
            "osm_type": r.get("osm_type", ""),
            "class": r.get("class", ""),
            "type": r.get("type", ""),
        }

    parsed = [_parse_result(r) for r in results]

    return json.dumps(
        {
            "place_name": place_name,
            "top_result": parsed[0],
            "all_results": parsed,
            "total_results": len(parsed),
        }
    )



@mcp.tool()
async def reverse_geocode_place(
    latitude: float,
    longitude: float,
) -> str:
    """
    Convert latitude and longitude to a place name (reverse geocoding).

    Uses the OpenStreetMap Nominatim API via geopy.

    Args:
        latitude:  Latitude coordinate.
        longitude: Longitude coordinate.

    Returns:
        JSON with the place name and address details.
    """
    logger.info("Reverse geocoding: %s, %s", latitude, longitude)

    try:
        location = geolocator.reverse(f"{latitude}, {longitude}")
    except Exception as exc:
        return json.dumps({"error": f"Reverse geocoding failed: {exc}"})

    return json.dumps(
        {
            "latitude": latitude,
            "longitude": longitude,
            "address": location.address,
        }
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Geocode MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="Transport type (default: streamable-http)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
