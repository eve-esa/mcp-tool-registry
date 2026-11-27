"""Local tests for the Satellite Passes MCP server.

Run from this folder:  python test.py
The first group needs no network. The second calls ESA, USGS and Open-Meteo.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import server

logging.disable(logging.WARNING)

BERLIN = "13.0884,52.3383,13.7612,52.6755"
BRANDENBURG = "11.2662,51.3592,14.7658,53.5591"


def check(name: str, ok: bool) -> None:
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        raise SystemExit(1)


def offline_tests() -> None:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    check("point inside a square", server._inside(5, 5, square))
    check("point outside a square", not server._inside(15, 5, square))

    left_half = [(0.0, 0.0), (5.0, 0.0), (5.0, 10.0), (0.0, 10.0)]
    pts = server._sample_points(0, 0, 10, 10)
    check("coverage of half the area is 50%", round(server._coverage(pts, [left_half])) == 50)
    check("tiny area is sampled as one point", len(server._sample_points(13.4, 52.5, 13.4, 52.5)) == 1)

    # A descending datatake from 70N to 30N over 10 minutes is over 50N after 5 minutes.
    take = {
        "begin": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 10, tzinfo=UTC),
        "rings": [[(10.0, 70.0), (20.0, 70.0), (20.0, 30.0), (10.0, 30.0)]],
    }
    check("time over the area is interpolated along the datatake", server._time_over_area(take, 50.0).minute == 5)

    plan = """<kml><Placemark><TimeSpan><begin>2026-10-29T10:21:00.000</begin><end>2026-10-29T10:32:07.000</end></TimeSpan>
    <ExtendedData><Data name="Mode"><value>NOBS</value></Data></ExtendedData>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>10,60,0 20,60,0 20,40,0 10,40,0 10,60,0</coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark><Placemark><TimeSpan><begin>2026-10-29T11:00:00</begin><end>2026-10-29T11:01:00</end></TimeSpan>
    <ExtendedData><Data name="Mode"><value>DARK-O</value></Data></ExtendedData>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>0,0,0 1,0,0 1,1,0 0,0,0</coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark></kml>"""
    takes = server._parse_plan(plan, "S2A")
    check("plan parser reads imaging datatakes and skips calibration", len(takes) == 1 and takes[0]["mode"] == "NOBS")


async def error_tests() -> None:
    cases = [
        ("bbox with three numbers", {"bbox": "13,52,14"}),
        ("bbox in the wrong order", {"bbox": "14,53,13,52"}),
        ("area that is too large", {"bbox": "0,0,40,40"}),
        ("days_ahead out of range", {"bbox": BERLIN, "days_ahead": 60}),
        ("unknown satellite group", {"bbox": BERLIN, "satellites": "worldview"}),
        ("min_coverage_pct out of range", {"bbox": BERLIN, "min_coverage_pct": 150}),
    ]
    for name, args in cases:
        out = json.loads(await server.find_next_satellite_passes(**args))
        check(f"clear error for {name}", "error" in out)


async def live_tests() -> None:
    out = json.loads(await server.find_next_satellite_passes(BERLIN, days_ahead=7))
    passes = out["passes"]
    check("Berlin: passes found", len(passes) > 0)
    check("Berlin: optical and radar passes", {"optical", "radar"} <= {p["type"] for p in passes})
    keys = [server._sort_key(p) for p in passes]
    check("Berlin: passes sorted by time", keys == sorted(keys))
    check("Berlin: cloud forecast on near optical passes", any("cloud_forecast_pct" in p for p in passes))
    check("Berlin: every satellite group reports its schedule",
          {s["source"] for s in out["schedules"]} >= {"ESA acquisition plan", "USGS 16-day acquisition calendar"})
    check("Berlin: output is compact (< 8000 characters)", len(json.dumps(out)) < 8000)

    out = json.loads(await server.find_next_satellite_passes(BRANDENBURG, days_ahead=7, min_coverage_pct=80))
    check("Brandenburg: coverage filter keeps only passes covering >= 80%",
          all(p["area_covered_pct"] >= 80 for p in out["passes"]))

    out = json.loads(await server.find_next_satellite_passes(BERLIN, satellites="sentinel-1"))
    check("Sentinel-1 only: radar passes with orbit details",
          out["passes"] and all(p["type"] == "radar" and "relative_orbit" in p for p in out["passes"]))

    for p in json.loads(await server.find_next_satellite_passes(BERLIN, days_ahead=7))["passes"]:
        print("      ", p.get("time_utc") or p["date_utc"], p["satellite"], p["type"],
              f"{p['area_covered_pct']}%", f"cloud {p.get('cloud_forecast_pct', '-')}")


if __name__ == "__main__":
    offline_tests()
    asyncio.run(error_tests())
    asyncio.run(live_tests())
    print("All tests passed.")
