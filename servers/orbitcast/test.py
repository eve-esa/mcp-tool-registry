"""
End-to-end checks for the OrbitCast MCP server: python test.py

The goal is to confirm that the server works as an agent would use it. The tools are called through
the MCP protocol (in-memory client, no port) against the live data sources (CelesTrak, ESA,
Open-Meteo), so a pass means the whole chain works: tool registration, catalogue, orbit prediction,
ESA plans, clouds and file output.

What is checked (16 checks):
    Tools        all tools in server.TOOLS are exposed over MCP
    Catalogue    Sentinels are discovered from ESA's plan pages
    Forecast     a 7-day forecast for Valencia has a timeline, every image has a download time, labels are the
                 known ones, and SAR passes never get a cloud value
    Errors       bad input (place name, past date, unknown mission) returns an actionable error, not a crash
    GeoJSON      an exported file stays inside the output folder even with a '../' file name
    Coverage     observed_areas returns coverage per mission for a region (Iberian Peninsula, 1 day)
    User sats    add a satellite by NORAD id, forecast it (geometric labels only), reject an invalid one, remove it

Takes about a minute the first time (plan downloads), a few seconds after that. Output files go to a temp dir.
Exit code 0 = all checks passed, 1 = at least one failed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# Set up before importing the server (hence "noqa E402" on the imports below).
HERE = Path(__file__).resolve().parent
os.environ.setdefault("ORBITCAST_OUTPUT", tempfile.mkdtemp(prefix="orbitcast_test_"))  # test files go to a temp dir
sys.path.insert(0, str(HERE))  # so "import server" works from any folder

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402
from orbitcast.config import OUTPUT_DIR  # noqa: E402

import server  # noqa: E402

VALENCIA = "-0.55,39.25,-0.25,39.55"  # test area: west,south,east,north
FAILED = []  # names of failed checks


def check(name, cond, detail=""):
    """Print PASS/FAIL for one check and remember failures."""
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILED.append(name)


async def main():
    # MCP client connected to the server in memory (no network port).
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        async def call(tool, **args):
            """Call a tool and return its JSON result as a dict (errors come back as {"error": ...})."""
            r = await client.call_tool(tool, args)
            return json.loads(r.content[0].text)

        # Tool list matches server.TOOLS
        tools = {t.name for t in (await client.list_tools()).tools}
        check("10 tools exposed", tools == {f.__name__ for f in server.TOOLS}, sorted(tools))

        # List satellites catalogue: Sentinels discovered from ESA
        cat = await call("list_eo_satellites")
        names = {r["satellite"]: r for r in cat["satellites"]}
        check("ESA-discovered Sentinels in catalogue", any(r["source"] == "ESA acquisition plans" for r in names.values()),
              cat["catalogue"]["esa_source"])
        check("retired S1A/S1B not in catalogue", "SENTINEL-1A" not in names and "SENTINEL-1B" not in names)

        # Forecast for Valencia: download times, known labels, no cloud value on radar
        fc = await call("forecast_observations", aoi=VALENCIA, days=7)
        check("forecast has a timeline", bool(fc.get("timeline")), fc.get("error", ""))

        if fc.get("timeline"):
            check("every image has a download time", all("expected_available_utc" in p for p in fc["timeline"]))
            check("labels are known", {p["confidence"] for p in fc["timeline"]} <= {"planned", "geometric",
                                                                                     "likely_usable", "cloudy"})
            check("SAR never gets a cloud value", all("cloud_pct" not in p for p in fc["timeline"]
                                                      if p["sensor_type"] == "SAR"))

        # Bad input returns a helpful error instead of crashing
        err = await call("forecast_observations", aoi="Valencia")
        check("place name -> actionable error", "geocode" in err.get("error", ""), err.get("error", "")[:60])
        err = await call("forecast_observations", aoi=VALENCIA, start="2024-06-01T00:00:00Z")
        check("past date -> error with today's date", "Today is" in err.get("error", ""))
        err = await call("forecast_observations", aoi=VALENCIA, missions=["NOPE"])
        check("unknown mission -> lists available ones", "Available" in err.get("error", ""))

        # A file name with "../" must not escape the output folder
        ex = await call("export_forecast_geojson", aoi=VALENCIA, days=5, filename="../escape")
        check("GeoJSON export stays in the output folder",
              Path(ex.get("path", "/")).parent == OUTPUT_DIR.resolve(), ex.get("path"))

        # Reverse query: coverage of a region
        oa = await call("observed_areas", region="-9.5,36.0,3.4,43.8", days=1)
        check("observed_areas gives coverage per mission", bool(oa.get("coverage")), oa.get("error", ""))

        # User satellite (Sentinel-3A): add, forecast, invalid add, remove

        # Add custom satellite
        add = await call("add_satellite", name="SENTINEL-3A-TEST", norad_id=41335, swath_km=1270, optical=True,
                         descending_only=True, repeat_days=27, sensor="OLCI")
        check("add_satellite by NORAD id", add.get("added") == "SENTINEL-3A-TEST", add.get("error", ""))

        # Forecast observations of specific satellite
        fc3 = await call("forecast_observations", aoi=VALENCIA, days=3, missions=["SENTINEL-3A-TEST"])
        check("user satellite is forecast (geometric)", bool(fc3.get("timeline")) and
              {p["confidence"] for p in fc3["timeline"]} <= {"geometric", "likely_usable", "cloudy"}, fc3.get("error", ""))

        # Adding a satellite without orbit data must fail
        bad = await call("add_satellite", name="X", norad_id=0)  # no orbit and no swath: must return an error
        check("add_satellite without orbit -> error", "error" in bad)

        # Remove satellite
        rm = await call("remove_satellite", name="SENTINEL-3A-TEST")  # cleanup
        check("remove_satellite", rm.get("removed") == "SENTINEL-3A-TEST", rm.get("error", ""))

    print(f"\n{'ALL PASSED' if not FAILED else f'{len(FAILED)} FAILED: {FAILED}'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
