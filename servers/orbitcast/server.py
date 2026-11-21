"""
OrbitCast — MCP Server
======================
Forecasts future Earth-observation acquisitions: when a satellite will image an area, whether ESA has scheduled
it, whether an optical image will be clear, when it can be downloaded, and which areas will be imaged in a time
window.

Sentinel-1/2 are discovered from ESA's plans, Landsat 8/9 is built in, and users can add any other satellite.

This file is the MCP interface only. One function per tool, with the docstring the model reads. The rest
is in the orbitcast/ folder.

Tools:
    forecast_observations    when will my area be imaged? (timeline, labels, download time)
    observed_areas           which areas will be imaged in a time window?
    predict_passes           orbit geometry only
    get_acquisition_plan     ESA's scheduled data takes
    next_revisit_after       same-geometry revisits of a reference scene
    export_forecast_geojson  footprints of the upcoming images as a GeoJSON file
    sync_acquisition_plans   pre-download ESA's plans, report plan horizons
    list_eo_satellites       the satellite catalogue
    add_satellite            add any satellite (NORAD id or TLE + sensor model)
    remove_satellite         remove a user-added satellite

Data (open, no credentials): CelesTrak GP (TLE), sentinels.copernicus.eu acquisition plans, Open-Meteo.

Usage:
    python server.py                              # HTTP transport (default, AgentCore), http://localhost:8000/mcp
    python server.py --transport http --port 8000  # HTTP transport on a chosen port
    python server.py --transport stdio            # stdio for MCP clients (Claude Desktop, Cursor)
    python test.py                                # end-to-end checks against the live data sources


Environment (optional):
    ORBITCAST_CACHE        cache folder (default ~/.cache/orbitcast, else the temp dir)
    ORBITCAST_OUTPUT       GeoJSON output folder (default ./orbitcast_outputs, else the temp dir)
    ORBITCAST_LATENCY_H    download delay per mission, e.g. "S1=12,S2=24" (default 24 h)
    ORBITCAST_SATELLITES   restrict the catalogue, e.g. "SENTINEL-1C,SENTINEL-2B"
"""

from __future__ import annotations

import json
import logging
import sys

from mcp.server.fastmcp import FastMCP
from orbitcast import catalog, coverage, forecast, geojson

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("orbitcast-mcp")

# Instructions are optional, but help the model at the initialization of the MCP server
INSTRUCTIONS = (
    "Forecasts future Earth-observation acquisitions: when Sentinel-1, Sentinel-2, Landsat or a user-added "
    "satellite will image an area (forecast_observations), which areas will be imaged in a time window "
    "(observed_areas), and when each image can be downloaded. Every result has a confidence label: planned "
    "(in ESA's plan), geometric (not confirmed), likely_usable / cloudy (optical only). Use days=N for 'the next N "
    "days'; do not guess dates. Open data: CelesTrak, ESA, Open-Meteo."
)

mcp = FastMCP("OrbitCast", host="0.0.0.0", port=8000, stateless_http=True, instructions=INSTRUCTIONS)


def _run(fn, *args, **kwargs) -> str:
    """Refresh the satellite catalogue, run the tool and return its result (or the error) as JSON."""
    try:
        catalog.refresh_catalog()
        return json.dumps(fn(*args, **kwargs))
    except Exception as ex:  # Every failure is reported to the model as data
        logger.info("%s failed: %s", getattr(fn, "__name__", fn), ex)
        return json.dumps({"error": str(ex)})


@mcp.tool()
def forecast_observations(aoi: str, start: str = "", end: str = "", missions: list[str] | None = None,
                          max_cloud_pct: float = 30.0, min_coverage_pct: float = 20.0, days: int = 0) -> str:
    """
    Ranked timeline of upcoming satellite observations of an area: the main tool for questions like
    'when will Sentinel-1 next see this flood?' or 'will I get a clear Sentinel-2 image before Friday?'.
    Combines orbit prediction, ESA's acquisition plans and, for OPTICAL passes only, the forecast cloud cover at
    overpass. Radar (sensor_type 'SAR', e.g. Sentinel-1) is not affected by cloud or smoke.

    Every image has expected_available_utc: when it should be downloadable (acquisition + typical latency,
    24 h by default). Use it for 'when can I get the image?'.

    Only passes that can produce an image are returned: an overpass for which ESA's published plan has no
    data take is dropped (counted in 'skipped').

    Confidence labels: planned = in ESA's plan; geometric = beyond the published plan or no plan exists,
    acquisition not confirmed; likely_usable = optical pass with forecast cloud <= max_cloud_pct;
    cloudy = optical pass, too cloudy.

    Args:
        aoi: 'west,south,east,north' in degrees (e.g. the bbox from geocode_local), a GeoJSON polygon, or a
            point 'lon,lat' (e.g. a GDACS event), which is expanded to a 10 km box.
        start: ISO-8601 UTC start; leave empty for now (past dates are rejected).
        end: ISO-8601 UTC end; default start + 10 days (max 30). Prefer days=N.
        days: length of the window from start, e.g. 7 for 'the next 7 days' (used when end is empty).
        missions: any of "S1", "S2", "LANDSAT" or a mission added with add_satellite; default ["S1", "S2"].
        max_cloud_pct: cloud threshold for 'likely_usable' on optical passes (default 30); ignored for SAR.
        min_coverage_pct: ignore passes covering less of the area (default 20).
    """

    return _run(forecast.forecast, aoi, start, end, missions, max_cloud_pct, min_coverage_pct, days)


@mcp.tool()
def observed_areas(region: str = "", start: str = "", days: float = 1.0, missions: list[str] | None = None,
                   satellites: list[str] | None = None, filename: str = "") -> str:
    """
    Which areas will be imaged in a time window: the reverse of forecast_observations. Use it for questions
    like 'what will Sentinel-1 image over Spain tomorrow?' or 'how much of my region gets an image this week?'.

    Footprints come from ESA's acquisition plans for Sentinel-1/2 ('planned', authoritative, incl. EW/SM modes)
    and from orbit prediction for other satellites and for times beyond a plan ('geometric': where the satellite
    CAN image).

    Saves all footprints as a GeoJSON file and returns the path plus a summary: footprints per satellite and,
    when a region is given, the % of the region covered per mission and per day. Percentages are of the region
    polygon: a bbox from a geocoder also contains sea, so say '% of the area' unless a real outline was passed.

    Always give the user the returned file path.

    Args:
        region: optional 'west,south,east,north', 'lon,lat' or GeoJSON (up to 25 million km2). Empty = whole
            world (window limited to 3 days).
        start: ISO-8601 UTC start. Leave empty for now (past dates are rejected).
        days: window length, e.g. 1 for 'tomorrow/next 24 h', 7 for 'this week' (max 10 with a region).
        missions: any of "S1", "S2", "LANDSAT" or a user-added mission. Default ["S1", "S2"].
        satellites: optional subset, e.g. ["SENTINEL-1C"]; overrides missions.
        filename: optional file name in the OrbitCast output folder. Default time-stamped.
    """
    return _run(coverage.observed_areas, region, start, days, missions, satellites, filename)


@mcp.tool()
def predict_passes(aoi: str, start: str = "", end: str = "", satellites: list[str] | None = None,
                   min_coverage_pct: float = 10.0, days: int = 0) -> str:
    """
    Predict when satellites fly over an area, from public orbit data (TLE + SGP4) and each sensor's swath.

    Each pass includes expected_available_utc (acquisition + typical latency, 24 h by default).

    Results are confidence='geometric': the satellite CAN image the area. The acquisition is not confirmed
    (use forecast_observations for plan-checked, cloud-scored results).

    Args:
        aoi: 'west,south,east,north' in degrees (e.g. the bbox from geocode_local) or a GeoJSON polygon.
        start: ISO-8601 UTC start; leave empty for now (past dates are rejected).
        end: ISO-8601 UTC end; default start + 7 days (max 30 days). Prefer days=N.
        days: length of the window from start, e.g. 7 for 'the next 7 days' (used when end is empty).
        satellites: e.g. ["SENTINEL-1C", "SENTINEL-2B"]; default every satellite in the catalogue.
        min_coverage_pct: drop passes covering less of the area than this (default 10).
    """
    return _run(forecast.passes, aoi, start, end, satellites, min_coverage_pct, days)


@mcp.tool()
def get_acquisition_plan(mission: str, aoi: str, start: str = "", end: str = "", days: int = 0) -> str:
    """
    Data takes that ESA has scheduled over an area, from the official Sentinel-1 or Sentinel-2 acquisition
    plans (confidence='planned'). Plan files are downloaded automatically the first time they are needed and
    cached. Plans usually reach two weeks ahead; plan_horizon_utc says how far, per satellite.

    A data take is a continuous recording strip: the area is seen somewhere between its start and end time
    (use forecast_observations for the overpass time over the area itself).

    Args:
        mission: "S1" (SAR: gives mode IW/EW and polarisation) or "S2" (optical).
        aoi: 'west,south,east,north' in degrees or a GeoJSON polygon.
        start: ISO-8601 UTC start; leave empty for now (past dates are rejected).
        end: ISO-8601 UTC end; default start + 10 days. Prefer days=N.
        days: length of the window from start, e.g. 7 for 'the next 7 days' (used when end is empty).
    """
    return _run(forecast.acquisition_plan, mission, aoi, start, end, days)


@mcp.tool()
def next_revisit_after(satellite: str, reference_time_utc: str, aoi: str, count: int = 3) -> str:
    """
    Future acquisitions with the same viewing geometry (same relative orbit) as a reference scene, for
    before/after change detection. Uses the exact repeat cycle (S1 12 d, S2 10 d, Landsat 16 d,
    or repeat_days of a user-added satellite) and confirms each date with orbit propagation when it
    is within 30 days.

    Args:
        satellite: e.g. "SENTINEL-1C" (the platform of the reference scene).
        reference_time_utc: sensing time of the reference scene (e.g. from a STAC search).
        aoi: 'west,south,east,north' in degrees or a GeoJSON polygon.
        count: how many future revisits to return (default 3).
    """
    return _run(forecast.revisits, satellite, reference_time_utc, aoi, count)


@mcp.tool()
def export_forecast_geojson(aoi: str, start: str = "", end: str = "", missions: list[str] | None = None,
                            max_cloud_pct: float = 30.0, min_coverage_pct: float = 20.0, filename: str = "",
                            days: int = 0) -> str:
    """
    Save the footprints of the upcoming (tasked) images over an area as a GeoJSON file, for maps and GIS.

    One feature per image with time, download time, satellite, confidence, cloud, mode and relative orbit. The
    footprint is ESA's data-take polygon when planned, else the orbit-predicted swath.

    Returns the file path and a short summary, not the geometry. Same labels and rules as forecast_observations.

    Args:
        aoi: 'west,south,east,north' in degrees, 'lon,lat', or a GeoJSON polygon.
        start: ISO-8601 UTC start; leave empty for now (past dates are rejected).
        end: ISO-8601 UTC end; default start + 10 days (max 30). Prefer days=N.
        days: length of the window from start, e.g. 7 for 'the next 7 days' (used when end is empty).
        missions: any of "S1", "S2", "LANDSAT" or a user-added mission; default ["S1", "S2"].
        max_cloud_pct: cloud threshold for 'likely_usable' on optical passes (default 30).
        min_coverage_pct: ignore passes covering less of the area (default 20).
        filename: optional file name (saved in the OrbitCast output folder); default is time-stamped.
    """
    return _run(geojson.export_forecast, aoi, start, end, missions, max_cloud_pct, min_coverage_pct, filename, days)


@mcp.tool()
def sync_acquisition_plans(missions: list[str] | None = None, days_ahead: int = 10,
                           force_refresh: bool = False) -> str:
    """
    Download (if not already cached) ESA's current Sentinel-1/2 acquisition-plan files and report how far
    ahead each satellite is planned, and which Sentinel satellites ESA currently plans for. Optional: the other
    tools download what they need on demand.

    Args:
        missions: any of ["S1", "S2"]; default both.
        days_ahead: window to cover from now (default 10).
        force_refresh: re-read ESA's index and re-download files even if cached.
    """
    return _run(forecast.sync_plans, missions, days_ahead, force_refresh)


@mcp.tool()
def list_eo_satellites() -> str:
    """
    List the satellites OrbitCast can forecast right now and where each comes from: Sentinel-1/2 units that
    ESA currently publishes acquisition plans for (discovered automatically), built-in Landsat 8/9, and
    satellites added by users with add_satellite. Gives sensor, swath, repeat cycle, download latency and the
    age of the public orbit data (TLE)."""
    return _run(forecast.catalogue)


@mcp.tool()
def add_satellite(name: str, norad_id: int = 0, tle_line1: str = "", tle_line2: str = "", swath_km: float = 0.0,
                  near_km: float | None = None, far_km: float | None = None, optical: bool = True,
                  descending_only: bool = False, repeat_days: float = 0.0, latency_h: float = 24.0,
                  sensor: str = "", mission: str = "") -> str:
    """
    Add (or update) a satellite that OrbitCast should forecast, beyond the Sentinels and Landsat: any satellite
    with public orbit data, e.g. Sentinel-3A (NORAD 41335) or a commercial or national mission. Its results are
    'geometric' (orbit prediction, no ESA plan). Stored on the server, so it stays available after restarts.

    Args:
        name: the name to use in other tools, e.g. "SENTINEL-3A".
        norad_id: NORAD catalogue number; the orbit (TLE) is fetched from CelesTrak and refreshed automatically.
        tle_line1: alternatively, the first TLE line (then also tle_line2); not refreshed automatically.
        tle_line2: the second TLE line.
        swath_km: full swath width in km, centred on the ground track (most optical sensors).
        near_km: for side-looking sensors (SAR) instead of swath_km: near edge, km from the track, positive =
            right of the flight direction.
        far_km: far edge, km from the track (must be larger than near_km).
        optical: true for optical sensors (daylight only, cloud forecast applies); false for radar.
        descending_only: true if it images only on the descending pass (typical for sun-synchronous optical).
        repeat_days: exact repeat cycle in days, for next_revisit_after (0 = unknown).
        latency_h: typical hours from acquisition to download (default 24).
        sensor: optional sensor name, e.g. "OLCI".
        mission: optional mission/constellation name to group satellites (default: the satellite name).
    """
    return _run(catalog.add_satellite, name, norad_id, tle_line1, tle_line2, swath_km, near_km, far_km, optical,
                descending_only, repeat_days, latency_h, sensor, mission)


@mcp.tool()
def remove_satellite(name: str) -> str:
    """
    Remove a satellite that was added with add_satellite. Built-in and ESA-discovered satellites stay.

    Args:
        name: the satellite name used in add_satellite.
    """
    return _run(catalog.remove_satellite, name)


# The tool functions
TOOLS = [forecast_observations, observed_areas, predict_passes, get_acquisition_plan, next_revisit_after,
         export_forecast_geojson, sync_acquisition_plans, list_eo_satellites, add_satellite, remove_satellite]


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(description="OrbitCast MCP server")

    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()

    mcp.settings.port = args.port

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
