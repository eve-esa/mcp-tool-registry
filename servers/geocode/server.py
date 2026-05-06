from __future__ import annotations

import json
import logging
import sys

from geopy.geocoders import Nominatim
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("geocode-mcp")

NOMINATIM_USER_AGENT = "FireDetectionMCPServer/1.0 (wildfire-research)"
TIMEOUT = 60

mcp = FastMCP("Geocode", host="0.0.0.0", port=8000, stateless_http=True)
geolocator = Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=TIMEOUT)


@mcp.tool()
async def geocode_place(
    place_name: str,
) -> str:
    """
    Convert a place name to a a pair of (latitude and longitude).

    Uses the OpenStreetMap Nominatim API via geopy.

    Args:
        place_name: Name of the place to geocode (e.g. "Greece",
                    "Athens", "Evia island", "Peloponnese").
    Returns:
        JSON with the latitude and longitude of the place
    """
    logger.info("Geocoding place: %s", place_name)

    try:
        results = geolocator.geocode(place_name)
    except Exception as exc:
        return json.dumps({"error": f"Geocoding failed: {exc}"})

    return json.dumps(
        {
            "place_name": place_name,
            "latitude": results.latitude,
            "longitude": results.longitude,
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