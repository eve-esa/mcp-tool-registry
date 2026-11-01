"""Live smoke test for the Forest Compliance server over MCP (needs internet, no LLM).

Start the server first (python server.py), then run: python test.py [url]
The maps are written to eudr_map.png and eudr_overview.png in the current directory.

Unlike the test_*.py files this is not a pytest module: it calls the running
server over streamable HTTP and reads the real rasters, checking known places
(a cocoa area, a national park, a city centre, a plot cleared after 2020).
"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"

# ~2x2 km area in the cocoa belt near Soubré, Côte d'Ivoire
SOUBRE = {
    "type": "Polygon",
    "coordinates": [[[-6.62, 5.78], [-6.60, 5.78], [-6.60, 5.80], [-6.62, 5.80], [-6.62, 5.78]]],
}
TAI_FOREST = {"type": "Point", "coordinates": [-7.35, 5.85]}  # inside Taï National Park
MADRID = {"type": "Point", "coordinates": [-3.7038, 40.4168]}  # city centre, no forest
# ~21 ha plot in western Côte d'Ivoire where 2020 forest was cleared in 2021-2024
CLEARED = {
    "type": "Polygon",
    "coordinates": [[[-7.73892, 6.222], [-7.73475, 6.222], [-7.73475, 6.22617], [-7.73892, 6.22617], [-7.73892, 6.222]]],
}


async def call(session: ClientSession, tool: str, args: dict) -> dict:
    """Call a tool, parse the JSON of its last content item and assert it is not an error."""
    started = time.monotonic()
    result = await session.call_tool(tool, args)
    payload = json.loads(result.content[-1].text)
    assert "error" not in payload, payload
    print(f"  ({tool} {time.monotonic() - started:.1f}s)")
    return payload


async def main() -> None:
    """Run every tool once against known locations and check the results make sense."""
    async with streamablehttp_client(URL, timeout=300) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            print("Tools:", names)
            assert {"eudr_check_plot", "eudr_check_plots", "eudr_plot_map"} <= set(names)

            soubre = await call(session, "eudr_check_plot", {"geometry": SOUBRE})
            a = soubre["areas_ha"]
            print(f"Soubré {soubre['plot_area_ha']} ha: {soubre['risk_level']} — forest 2020 {a['forest_2020_ha']} ha, "
                  f"deforested after cut-off {a['deforestation_after_cutoff_ha']} ha {a['deforestation_by_year_ha']}")
            print("  ", soubre["verdict"])
            assert 400 < soubre["plot_area_ha"] < 500
            assert soubre["provenance"]["hansen_tiles"] == ["10N_010W"]

            cleared = await call(session, "eudr_check_plot", {"geometry": CLEARED})
            print(f"Cleared plot: {cleared['risk_level']} — {cleared['verdict']}")
            assert cleared["risk_level"] == "high"
            assert set(cleared["areas_ha"]["deforestation_by_year_ha"]) <= {"2021", "2022", "2023", "2024"}

            batch = await call(session, "eudr_check_plots", {"plots": {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {"id": "tai-forest"}, "geometry": TAI_FOREST},
                    {"type": "Feature", "properties": {"id": "madrid"}, "geometry": MADRID},
                    {"type": "Feature", "properties": {"id": "broken"}, "geometry": {"type": "LineString",
                                                                                   "coordinates": [[0, 0], [1, 1]]}},
                ],
            }})
            for r in batch["results"]:
                print("  ", r["id"], r.get("risk_level"), r.get("forest_2020_ha"), r.get("error", ""))
            by_id = {r["id"]: r for r in batch["results"]}
            assert by_id["tai-forest"]["forest_2020_ha"] > 3.5  # a 4 ha circle, almost all forest
            assert by_id["madrid"]["forest_2020_ha"] < 0.5 and by_id["madrid"]["risk_level"] == "low"
            assert "error" in by_id["broken"]
            assert batch["summary"]["assessed"] == 2 and batch["summary"]["failed"] == 1
            print("Summary:", batch["summary"])

            started = time.monotonic()
            result = await session.call_tool("eudr_plot_map", {"geometry": SOUBRE})
            image = next(c for c in result.content if c.type == "image")
            with open("eudr_map.png", "wb") as f:
                f.write(base64.b64decode(image.data))
            print(f"Map: eudr_map.png ({image.mimeType}, {time.monotonic() - started:.1f}s)")

            with open(Path(__file__).parent / "examples" / "cocoa_cooperative.geojson") as f:
                cooperative = json.load(f)
            started = time.monotonic()
            result = await session.call_tool("eudr_overview_map", {"plots": cooperative, "title": "Demo cooperative"})
            image = next(c for c in result.content if c.type == "image")
            with open("eudr_overview.png", "wb") as f:
                f.write(base64.b64decode(image.data))
            summary = json.loads(result.content[-1].text)["summary"]
            print(f"Overview: eudr_overview.png, {summary['by_risk_level']} ({time.monotonic() - started:.1f}s)")
            assert summary["by_risk_level"]["high"] == 1
            print("OK")


if __name__ == "__main__":
    asyncio.run(main())
