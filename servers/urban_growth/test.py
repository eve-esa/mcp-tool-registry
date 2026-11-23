"""Test the Urban Growth server over HTTP. Start it first: python server.py"""

import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://localhost:8000/mcp"


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    return json.loads(result.content[0].text)


async def main() -> None:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("Tools:", [t.name for t in tools.tools])

            # 1. A fast-growing desert city: built-up area should rise and bare ground should shrink
            cairo = await call(session, "urban_growth_timeseries", {"place_name": "New Cairo, Egypt"})
            assert "error" not in cairo, cairo
            assert [r["year"] for r in cairo["years"]] == list(range(2017, 2024)), cairo["years"]
            assert cairo["summary"]["total_change_km2"] > 0, cairo["summary"]
            assert cairo["land_converted"][0]["class"] == "Bare ground", cairo["land_converted"]
            print("New Cairo:", [r["built_km2"] for r in cairo["years"]], cairo["summary"])

            # 2. A Nile Delta city: growth should come out of cropland
            tanta = await call(session, "urban_growth_timeseries", {"place_name": "Tanta, Egypt"})
            assert tanta["land_converted"][0]["class"] == "Crops", tanta["land_converted"]
            print("Tanta:", tanta["summary"]["total_change_percent"], "% growth, lost:", tanta["land_converted"][0])

            # 3. A bbox across a map tile edge (UTM zones 35R/36R at lon 30) with a custom year range
            edge = await call(
                session, "urban_growth_timeseries", {"bbox": "29.9,31.1,30.1,31.25", "start_year": 2020, "end_year": 2023}
            )
            assert len(edge["years"]) == 4, edge
            # The box lies in the overlap of two tiles: every year must read the same tile, or the
            # series jumps (2023 used to jump by about 36 km2 when the tile order changed).
            tiles = {r["map_url"].split("item=")[1].split("-")[0] for r in edge["years"]}
            assert len(tiles) == 1, tiles
            print("Tile edge: change", edge["summary"]["total_change_km2"], "km2")

            # 3b. Munich used to fail: Esri date-line tiles are catalogued with a global bbox
            munich = await call(session, "urban_growth_timeseries", {"place_name": "Munich, Germany"})
            assert "error" not in munich, munich
            print("Munich:", munich["headline"])

            # 3c. Auckland sits in the date-line zone 60, which has real data but a wrong global bbox
            auckland = await call(session, "urban_growth_timeseries", {"place_name": "Auckland, New Zealand"})
            assert "error" not in auckland, auckland
            print("Auckland:", auckland["headline"])

            # 3d. The documented maximum radius (25 km = a 2500 km2 box) must be accepted
            big = await call(
                session, "urban_growth_timeseries", {"place_name": "Karachi, Pakistan", "radius_km": 25, "start_year": 2022}
            )
            assert "error" not in big, big
            print("Karachi 25 km:", big["area_km2"], "km2 box")

            # 4. One-year land cover snapshot (ESA WorldCover)
            evia = await call(session, "landcover_summary", {"place_name": "Evia, Greece", "radius_km": 10})
            assert evia["dominant_class"] == "Tree cover", evia
            print("Evia:", [(c["class"], c["percent"]) for c in evia["classes"][:3]])

            # 5. Errors come back as JSON, not exceptions
            for args in ({}, {"place_name": "Cairo", "start_year": 2015}, {"bbox": "0,0,10,10"}):
                err = await call(session, "urban_growth_timeseries", args)
                assert "error" in err, err
                print("Error OK:", err["error"])

            print("All checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
