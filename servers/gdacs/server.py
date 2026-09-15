"""
GDACS (Disasters) MCP Server
============================
An MCP server providing global natural disaster and hazard monitoring
via GDACS (Global Disaster Alert and Coordination System), a joint
initiative of the UN and European Commission. Use to search for
natural disaster events worldwide covering earthquakes, floods,
volcanoes, wildfires, droughts, cyclones and tsunamis.

Tools:
    search_gdacs_events — search ongoing/recent disasters by hazard
                        type, alert level, and/or country

Usage:
    python server.py                              # stdio transport
    python server.py --transport http --port 8000  # HTTP transport

Requirements:
    pip install "mcp[cli]==1.27.0" httpx python-dotenv
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any
import httpx
from typing import Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger("disasters-mcp")

GDACS_SEARCH_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
TIMEOUT = 60
GDACS_USER_AGENT = "DisastersMCPServer"

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

mcp = FastMCP("Disasters", host="0.0.0.0", port=8000, stateless_http=True)


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


EventType = Literal["EQ", "TC", "FL", "VO", "WF", "DR", "TS"]
AlertLevel = Literal["Green", "Orange", "Red"]


@mcp.tool()
async def search_gdacs_events(
    eventlist: EventType | None = None,
    alertlevel: AlertLevel | None = None,
    country: str | None = None,
) -> str:
    """Search GDACS for ongoing or recent natural disaster events worldwide.

    Use this when the user asks about current or recent disasters, hazards,
    or emergencies — e.g. "any active volcanoes right now", "is there
    flooding in Bangladesh", "recent earthquakes near Japan". The data comes
    from the Global Disaster Alert and Coordination System (GDACS) and is
    not limited to any one country.

    Args:
    eventlist: The type of hazard to search for. One of:
        - "EQ": seismic events
        - "FL": river/flash flooding
        - "VO": volcanic eruptions or unrest
        - "WF": forest/bush fires
        - "DR": prolonged water shortage events
        - "TC": hurricanes, typhoons, cyclones
        - "TS": tsunami warnings/events
        Leave unset to return any/all categories of hazard.
    alertlevel: Optional severity filter. "Green" = low impact,
        "Orange" = medium impact, "Red" = high impact/major disaster.
        Leave unset to return events of any severity.
    country: Optional country name to restrict results to
        (e.g. "Philippines"). Leave unset to search globally.

    Returns:
        JSON string with matching events, including event name, country,
        alert level, coordinates, and event date range.
    """

    try:
        resp = await _http_get(
            GDACS_SEARCH_URL,
            params={
                "eventlist": eventlist,
                "alertlevel": alertlevel,
                "country": country,
            },
            headers={"User-Agent": GDACS_USER_AGENT},
        )
        results = resp.json()
    except Exception as exc:
        return json.dumps({"error": f"Disaster search failed: {exc}"})

    if not results.get("features"):
        return json.dumps({"error": "No results found for query."})

    return json.dumps(results)


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
