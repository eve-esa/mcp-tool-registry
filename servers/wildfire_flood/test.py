"""Live smoke test for the Wildfire Flood server over MCP (needs internet, no LLM).

Start the server first (python server.py), then run: python test.py [url]
The figures are written to cascade.png and before_after.jpg in the current directory.

Unlike the test_*.py files this is not a pytest module: it calls the running
server over streamable HTTP with the real data. The reference case is the
Eaton fire (Los Angeles, 7-31 January 2025) and the storm of 13-14 February
2025, which triggered debris flows in the burn area: the server must flag
that storm as high risk and a dry summer week as low risk.
"""

import asyncio
import base64
import json
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"

EATON = {"bbox": [-118.17, 34.17, -118.02, 34.27], "fire_start": "2025-01-07", "fire_end": "2025-01-31"}
STORM = {"rain_start": "2025-02-12", "rain_end": "2025-02-15"}
DRY_WEEK = {"rain_start": "2025-07-01", "rain_end": "2025-07-07"}


async def call(session: ClientSession, tool: str, args: dict, expect_error: bool = False) -> dict:
    """Call a tool and parse the JSON of its last content item."""
    started = time.monotonic()
    result = await session.call_tool(tool, args)
    payload = json.loads(result.content[-1].text)
    assert ("error" in payload) == expect_error, payload
    print(f"  ({tool} {time.monotonic() - started:.1f}s)")
    return payload


async def main() -> None:
    """Run every tool on the Eaton fire and check the results make sense."""
    async with streamablehttp_client(URL, timeout=600) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            print("Tools:", sorted(names))
            assert names == {"burn_severity", "postfire_flood_risk", "cascade_figure", "burn_scar_photos"}

            burn = (await call(session, "burn_severity", EATON))["burn"]
            print(f"Eaton: {burn['burned_ha']:,.0f} ha burned, severity {burn['severity_ha']}, "
                  f"steep mod/high share {burn['steep_mod_high_share']}")
            assert 4_500 < burn["burned_ha"] < 7_500  # official perimeter: ~5,700 ha
            severe = burn["severity_ha"]["moderate-high"] + burn["severity_ha"]["high"]
            assert severe > burn["severity_ha"]["low"]
            assert "Altadena" in {p["name"] for p in burn["places"]} or not burn["places"]  # names need Nominatim
            assert burn["built_up_burned_ha"] > 300  # Altadena

            storm = await call(session, "postfire_flood_risk", {**EATON, **STORM})
            print("Storm:", storm["verdict"])
            assert storm["risk_level"] == "high"
            assert storm["bursts_above_threshold"], "the 14 Feb burst should cross the threshold"

            dry = await call(session, "postfire_flood_risk", {**EATON, **DRY_WEEK})
            print("Dry week:", dry["verdict"])
            assert dry["risk_level"] == "low"

            forecast = await call(session, "postfire_flood_risk", EATON)
            print("Next 7 days:", forecast["risk_level"], forecast["rain_window"])

            await call(session, "burn_severity", {**EATON, "fire_end": "2099-01-01"}, expect_error=True)
            await call(session, "postfire_flood_risk", {"fire_start": "2025-01-07"}, expect_error=True)

            started = time.monotonic()
            result = await session.call_tool("cascade_figure", {**EATON, **STORM, "title": "Eaton fire → 14 Feb 2025"})
            image = next(c for c in result.content if c.type == "image")
            png = base64.b64decode(image.data)
            with open("cascade.png", "wb") as f:
                f.write(png)
            print(f"Figure: cascade.png ({len(png) // 1024} KB, {time.monotonic() - started:.1f}s)")
            assert len(png) < 120_000
            started = time.monotonic()
            result = await session.call_tool("burn_scar_photos", {**EATON, "title": "Eaton fire, January 2025"})
            image = next(c for c in result.content if c.type == "image")
            jpeg = base64.b64decode(image.data)
            with open("before_after.jpg", "wb") as f:
                f.write(jpeg)
            print(f"Photos: before_after.jpg ({len(jpeg) // 1024} KB, {image.mimeType}, {time.monotonic() - started:.1f}s)")
            assert image.mimeType == "image/jpeg" and len(jpeg) < 200_000
            print("OK")


if __name__ == "__main__":
    asyncio.run(main())
