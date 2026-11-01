"""Live smoke test for the Climate Risk server over MCP (needs internet, no LLM).

Connects as an MCP client over streamable HTTP, lists the tools and calls them
with real locations (Madrid, a cocoa plot in Côte d'Ivoire) against the live
Open-Meteo API, printing the key numbers. Not collected by pytest (the file
name does not start with ``test_``).

Start the server first (python server.py), then run: python test.py [url]
"""

import asyncio
import json
import sys
from datetime import date, timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"

MADRID = {"latitude": 40.4168, "longitude": -3.7038}
# ~2x2 km cocoa plot near Soubré, Côte d'Ivoire, as GeoJSON
SOUBRE_PLOT = json.dumps({
    "type": "Polygon",
    "coordinates": [[[-6.62, 5.78], [-6.60, 5.78], [-6.60, 5.80], [-6.62, 5.80], [-6.62, 5.78]]],
})


async def call(session: ClientSession, tool: str, args: dict) -> dict:
    """Call a tool, parse its first (JSON) content item and fail on an ``error`` payload."""
    result = await session.call_tool(tool, args)
    payload = json.loads(result.content[0].text)
    assert "error" not in payload, payload
    return payload


async def main() -> None:
    """Exercise climate_anomaly and drought_index end to end and print a short report."""
    async with streamablehttp_client(URL, timeout=180) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            print("Tools:", names)
            assert {"climate_anomaly", "drought_index"} <= set(names)

            # Hackathon thread B: "average temperature in Madrid last week (ERA5)".
            end = date.today() - timedelta(days=8)  # beyond ERA5's ~5-7 day publication delay
            start = end - timedelta(days=6)
            anomaly = await call(session, "climate_anomaly", {
                **MADRID, "start_date": start.isoformat(), "end_date": end.isoformat(),
            })
            t = anomaly["temperature_mean_c"]
            p = anomaly["precipitation_total_mm"]
            print(f"Madrid {start}..{end}: {t['value']} °C vs normal {t['baseline_mean']} "
                  f"(anomaly {t['anomaly']:+}, p{t['percentile']}); rain {p['value']} mm "
                  f"({p['percent_of_normal']}% of normal)")
            assert t["baseline_samples"] == 30
            assert anomaly["provenance"]["model"] == "era5"

            drought = await call(session, "drought_index", {"geometry": SOUBRE_PLOT, "scales": [3, 6, 12]})
            print("Soubré:", drought["summary"])
            assert drought["risk_level"] in {"low", "medium", "high"}
            assert [e["scale_months"] for e in drought["spei"]] == [3, 6, 12]
            assert drought["provenance"]["requested_location"]["from"] == "geometry centroid"

            # Agents may send GeoJSON as an object instead of a string; same answer, served from cache.
            as_object = await call(session, "drought_index", {
                "geometry": json.loads(SOUBRE_PLOT), "scales": [3, 6, 12],
            })
            assert as_object["spei"] == drought["spei"]

            bad = await session.call_tool("drought_index", {"latitude": 95, "longitude": 0})
            assert "error" in json.loads(bad.content[0].text)
            print("Grid cell:", drought["provenance"]["grid_cell"])
            print("OK")


if __name__ == "__main__":
    asyncio.run(main())
