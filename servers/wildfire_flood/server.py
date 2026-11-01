"""
Wildfire Flood — MCP Server
===========================
Compound hazard: a wildfire scar turns the next heavy rain into a debris flow
or flash flood. Burned slopes lose the vegetation and litter that held water
and soil, and their surface often turns water-repellent, so short bursts of
rain that were harmless before the fire now run off and carry debris.

The server chains three free, keyless data sources and one published model:

    1. Burn scar  — Sentinel-2 L2A dNBR, pre vs post fire (Earth Search STAC, AWS COGs)
    2. Terrain    — slope from Copernicus DEM GLO-30; land cover from ESA WorldCover 2021
    3. Rain       — Open-Meteo 15-minute / hourly precipitation, forecast or past
    4. Hazard     — USGS M1 debris-flow likelihood model (Staley et al. 2017)
    5. Places     — names of the main built-up areas from OpenStreetMap (Nominatim)

Tools:
    burn_severity       — how big and how severe is the burn scar?
    postfire_flood_risk — will the forecast (or a past) rain trigger debris flows?
    cascade_figure      — PNG: danger-zone map + rain against the threshold
    burn_scar_photos    — JPEG: natural-colour satellite photos before and after

Module layout: aoi.py (input geometry), burn.py (raster maths), hazard.py
(the model), sources.py (all network I/O), figure.py (rendering), server.py
(tools, caching, provenance). Tools never raise: errors come back as
{"error": ...} JSON so the agent can correct its call.

Usage:
    python server.py                               # HTTP transport (default)
    python server.py --transport stdio             # stdio transport
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aoi
import figure
import hazard
import sources
from mcp.server.fastmcp import FastMCP, Image
from rasterio.warp import transform_geom

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("wildfire-flood-mcp")

SERVER_NAME = "Wildfire Flood"
SERVER_VERSION = "0.1.0"
DEFAULT_FORECAST_DAYS = 7
NEAR_KM = 2.0  # radius around steep burned slopes where built-up land counts as exposed
CACHE_SIZE = 6  # burn analyses kept in memory; the three tools often run on the same fire
# Without a containment date the post-fire scene is searched from this many days
# after ignition, so it does not catch the fire still burning (EFFIS, for one,
# only gives the start date). Large Mediterranean fires mostly burn out within two weeks.
DEFAULT_FIRE_DAYS = 14

mcp = FastMCP(SERVER_NAME, host="0.0.0.0", port=8000, stateless_http=True)

_burn_cache: OrderedDict[tuple, sources.BurnScene] = OrderedDict()


# --- Helpers ---------------------------------------------------------------------


def _parse_date(value: str | None, name: str) -> date | None:
    """ISO date (YYYY-MM-DD) or None; a clear error names the bad argument."""
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"{name} must be a date like 2025-01-31") from exc


def _burn(geom: dict, fire_start: date, fire_end: date) -> sources.BurnScene:
    """Burn analysis with a small LRU cache (blocking; run in a worker thread)."""
    key = (aoi.fingerprint(geom), fire_start, fire_end)
    if key in _burn_cache:
        _burn_cache.move_to_end(key)
        return _burn_cache[key]
    started = time.monotonic()
    scene = sources.analyse_burn(geom, fire_start, fire_end, margin_km=NEAR_KM)
    logger.info("Burn analysis %s in %.1fs: %.0f ha burned", key[0], time.monotonic() - started,
                scene.stats.burned_ha)
    _burn_cache[key] = scene
    if len(_burn_cache) > CACHE_SIZE:
        _burn_cache.popitem(last=False)
    return scene


def _fire_period(fire_start: str | None, fire_end: str | None) -> tuple[date, date]:
    """Fire start and end; the end defaults to DEFAULT_FIRE_DAYS after the start,
    but never later than yesterday (a post-fire scene must exist)."""
    start = _parse_date(fire_start, "fire_start")
    if start is None:
        raise ValueError("fire_start is required")
    end = _parse_date(fire_end, "fire_end")
    if end is None:
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        end = max(start, min(start + timedelta(days=DEFAULT_FIRE_DAYS), yesterday))
    return start, end


def _rain_window(rain_start: str | None, rain_end: str | None) -> tuple[date, date]:
    """Rain window; by default today and the next DEFAULT_FORECAST_DAYS days (UTC)."""
    start = _parse_date(rain_start, "rain_start") or datetime.now(UTC).date()
    end = _parse_date(rain_end, "rain_end") or start + timedelta(days=DEFAULT_FORECAST_DAYS)
    return start, end


def _model_terms(scene: sources.BurnScene, soil_kf: float | None) -> tuple[float, float, float]:
    """T, F, S inputs of Staley M1 from the burn statistics."""
    stats = scene.stats
    soil = hazard.DEFAULT_SOIL_KF if soil_kf is None else float(soil_kf)
    if not 0 < soil <= 0.7:
        raise ValueError("soil_kf must be between 0 and 0.7 (typical 0.1-0.4)")
    return stats.steep_mod_high_share, (stats.mean_dnbr_burned or 0.0) / 1000, soil


def _burn_summary(scene: sources.BurnScene) -> dict[str, Any]:
    """Burn statistics as a JSON-ready dict, with the named places around the scar."""
    stats = asdict(scene.stats)
    stats["near_km"] = NEAR_KM
    stats["places"] = [
        {k: p[k] for k in ("name", "kind", "lat", "lon", "built_up_ha", "near_steep_burn")} for p in scene.places
    ]
    return stats


def _places_at_risk(scene: sources.BurnScene) -> list[str]:
    """Names of the settlements within the danger zone, most built-up first."""
    return [p["name"] for p in scene.places if p["near_steep_burn"]]


def _outline(scene: sources.BurnScene, geom: dict) -> list:
    """The analysed area's outer rings in the grid CRS, for drawing."""
    projected = transform_geom("EPSG:4326", scene.grid.crs, geom)
    polygons = [projected["coordinates"]] if projected["type"] == "Polygon" else projected["coordinates"]
    return [polygon[0] for polygon in polygons]


def _verdict(risk: dict, scene: sources.BurnScene, rain: dict) -> str:
    """One plain sentence for the agent and the user."""
    stats = scene.stats
    if stats.burned_ha < 10:
        return "No significant burn scar was found in the area, so there is no post-fire debris-flow hazard."
    minutes = risk["burst_duration_min"]
    entry = risk["by_duration"][f"{minutes}_min"]
    period = "forecast" if rain["kind"] == "forecast" else "rain"
    text = (
        f"{risk['risk_level'].capitalize()} debris-flow / flash-flood risk: the burn scar "
        f"({stats.burned_ha:,.0f} ha, {stats.steep_mod_high_share:.0%} moderate/high severity on steep slopes) "
        f"reaches a 50 % debris-flow likelihood at about {entry['threshold_50pct_mm']} mm in {minutes} min "
        f"({entry['threshold_50pct_mm_per_h']} mm/h); the {period} peaks at {entry['peak_rain_mm']} mm in "
        f"{minutes} min"
    )
    if entry["peak_at_utc"]:
        text += f" ({entry['peak_at_utc']} UTC)"
    text += f", giving a likelihood of {risk['likelihood']:.0%}."
    if stats.built_up_near_ha > 0:
        text += f" {stats.built_up_near_ha:,.0f} ha of built-up land lie within {NEAR_KM:g} km of steep burned slopes"
        places = _places_at_risk(scene)
        text += f", including {', '.join(places[:5])}." if places else "."
    return text


def _provenance(geom: dict, how: str, scene: sources.BurnScene, rain: dict | None) -> dict[str, Any]:
    """Audit trail: inputs, exact scenes and tiles, methods and assumptions."""
    datasets = [
        {**sources.DATASETS["sentinel2"], "scenes": scene.scenes, "mgrs_tile": scene.tile},
        {**sources.DATASETS["dem"], "tiles": scene.sources.get("dem_tiles", [])},
        {**sources.DATASETS["worldcover"], "tiles": scene.sources.get("worldcover_tiles", [])},
        sources.DATASETS["places"],
    ]
    if rain is not None:
        datasets.append({**sources.DATASETS["rain"], "request_url": rain["request_url"],
                         "grid_point": rain["grid_point"]})
    return {
        "aoi_id": aoi.fingerprint(geom),
        "aoi_from": how,
        "aoi_area_km2": round(aoi.area_km2(geom), 2),
        "datasets": datasets,
        "grid": {"crs": scene.grid.crs, "pixel_m": scene.grid.pixel_m},
        "method": (
            "dNBR = NBR(pre) - NBR(post) from Sentinel-2 B08/B12, classified with USGS FIREMON thresholds "
            "(Key & Benson 2006) and despeckled (burned pixels must sit in a mostly burned 5x5 window). "
            "Debris-flow likelihood: USGS M1 (Staley et al. 2017, Geomorphology 278:149-162) with "
            "T = share of the burned area at moderate/high severity on slopes >= 23°, "
            "F = mean dNBR/1000 of the burned area, S = soil KF factor."
        ),
        "assumptions": [
            "The whole burn footprint is treated as one drainage basin (screening, not per-basin routing).",
            f"Soil erodibility KF defaults to {hazard.DEFAULT_SOIL_KF} unless given.",
            "M1 was calibrated in the western United States; elsewhere treat results as indicative.",
            f"Built-up land within {NEAR_KM:g} km of steep burned slopes counts as exposed; no flow routing.",
        ],
        "disclaimer": "Screening for situational awareness, not an official warning. Follow local authorities.",
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "server": f"{SERVER_NAME} MCP server v{SERVER_VERSION}",
    }


def _error(message: str) -> str:
    """Log a tool failure and return it as {"error": ...} JSON for the agent."""
    logger.warning("Tool error: %s", message)
    return json.dumps({"error": message})


async def _risk(
    geometry, bbox, latitude, longitude, radius_km, fire_start, fire_end, rain_start, rain_end, soil_kf
) -> tuple[dict, str, sources.BurnScene, dict, dict]:
    """Shared pipeline of postfire_flood_risk and cascade_figure."""
    geom, how = aoi.to_area(geometry, bbox, latitude, longitude, radius_km)
    start, end = _fire_period(fire_start, fire_end)
    rain_from, rain_to = _rain_window(rain_start, rain_end)
    scene = await asyncio.to_thread(_burn, geom, start, end)
    lat, lon = aoi.centroid(geom)
    rain = await asyncio.to_thread(sources.fetch_rain, lat, lon, rain_from, rain_to)
    result = hazard.assess(*_model_terms(scene, soil_kf), rain)
    return geom, how, scene, rain, result


# --- Tools -----------------------------------------------------------------------
# The docstrings below are what the LLM reads to choose and call a tool.


@mcp.tool()
async def burn_severity(
    fire_start: str,
    fire_end: str | None = None,
    geometry: dict | str | None = None,
    bbox: list[float] | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: float | None = None,
) -> str:
    """
    Map how large and how severe a wildfire burn scar is, from Sentinel-2.

    Use this to assess a past or recent fire, e.g. one found with the EFFIS
    burnt-area tool (pass its bbox and firedate): burned area, severity classes,
    how much burned on steep slopes, which land cover burned (forest,
    shrubland, built-up) and how much built-up land lies near the scar.
    Compares the clearest Sentinel-2 scene before fire_start with the clearest
    after fire_end (dNBR).

    Args:
        fire_start: First day of the fire, YYYY-MM-DD.
        fire_end:   Containment day, YYYY-MM-DD, in the past (default:
                    fire_start + 14 days, e.g. when only the EFFIS fire date
                    is known).
        geometry:   Area as GeoJSON Polygon/MultiPolygon/Feature (object or
                    string), e.g. the fire perimeter, up to 2,500 km²...
        bbox:       ...or [west, south, east, north] in degrees (a list or a
                    "west,south,east,north" string, as EFFIS returns it)...
        latitude:   ...or a centre point (with longitude) and radius_km
        longitude:  (default radius 5 km).
        radius_km:  Radius around latitude/longitude, up to 30 km.

    Returns:
        JSON with burned_ha, severity_ha (low, moderate-low, moderate-high,
        high), mean dNBR, steep moderate/high share, burned land cover,
        built-up land burned and nearby, the scenes used, and provenance.
    """
    try:
        geom, how = aoi.to_area(geometry, bbox, latitude, longitude, radius_km)
        start, end = _fire_period(fire_start, fire_end)
        scene = await asyncio.to_thread(_burn, geom, start, end)
        return json.dumps({"burn": _burn_summary(scene), "provenance": _provenance(geom, how, scene, None)})
    except Exception as exc:  # noqa: BLE001 - every failure goes back to the agent as JSON
        return _error(str(exc))


@mcp.tool()
async def postfire_flood_risk(
    fire_start: str,
    fire_end: str | None = None,
    geometry: dict | str | None = None,
    bbox: list[float] | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: float | None = None,
    rain_start: str | None = None,
    rain_end: str | None = None,
    soil_kf: float | None = None,
) -> str:
    """
    Will rain on a wildfire burn scar trigger debris flows or flash floods?

    Use this after a fire, for the coming days (rain forecast, the default) or
    to replay a past storm. Combines the Sentinel-2 burn scar, slope and rain
    with the USGS post-fire debris-flow model (Staley et al. 2017): returns
    the rainfall threshold (mm in 15/30/60 min) at which debris flows become
    likely (50 %), the peak rain in the window, the likelihood and a risk
    level, the rain bursts above the threshold and the built-up land exposed.

    Args:
        fire_start: First day of the fire, YYYY-MM-DD.
        fire_end:   Containment day, YYYY-MM-DD (default: fire_start + 14 days).
        geometry:   Area as GeoJSON (fire perimeter), up to 2,500 km²...
        bbox:       ...or [west, south, east, north]...
        latitude:   ...or a centre point (with longitude) and radius_km.
        longitude:  Longitude of the centre point.
        radius_km:  Radius around the centre, up to 30 km (default 5).
        rain_start: First day of the rain window, YYYY-MM-DD (default today, UTC).
        rain_end:   Last day (default rain_start + 7 days; at most 31 days and
                    15 days into the future).
        soil_kf:    Soil erodibility KF factor if known (default 0.25).

    Returns:
        JSON with verdict (one sentence), risk_level ("low", "medium",
        "high"), likelihood (0-1), thresholds and peaks per duration, rain
        bursts above the threshold, burn summary, and provenance.
    """
    try:
        geom, how, scene, rain, result = await _risk(
            geometry, bbox, latitude, longitude, radius_km, fire_start, fire_end, rain_start, rain_end, soil_kf
        )
        payload = {
            "verdict": _verdict(result, scene, rain),
            **result,
            "rain_window": {"from": rain["times_60"][0] if rain["times_60"] else None,
                            "to": rain["times_60"][-1] if rain["times_60"] else None, "kind": rain["kind"],
                            "has_15_min_data": rain["mm_15"] is not None},
            "burn": _burn_summary(scene),
            "provenance": _provenance(geom, how, scene, rain),
        }
        return json.dumps(payload)
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc))


# Image + text content; no structured (JSON-schema) output for this tool.
@mcp.tool(structured_output=False)
async def cascade_figure(
    fire_start: str,
    fire_end: str | None = None,
    geometry: dict | str | None = None,
    bbox: list[float] | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: float | None = None,
    rain_start: str | None = None,
    rain_end: str | None = None,
    soil_kf: float | None = None,
    title: str | None = None,
) -> list:
    """
    Draw the wildfire-to-flood cascade: burn severity map + rain vs threshold.

    Use this to show the result of postfire_flood_risk (same arguments). Left:
    burn severity (yellow low to dark red high) over terrain shading, built-up
    land in blue, the area outline in black. Right: rain per 15 minutes (or
    hour) as bars, red when above the dashed 50 % debris-flow threshold.

    Args:
        fire_start, fire_end, geometry, bbox, latitude, longitude, radius_km,
        rain_start, rain_end, soil_kf: as in postfire_flood_risk.
        title: Optional figure title, e.g. the fire name.

    Returns:
        The PNG figure followed by a JSON block with the verdict, risk level,
        likelihood and provenance.
    """
    try:
        geom, how, scene, rain, result = await _risk(
            geometry, bbox, latitude, longitude, radius_km, fire_start, fire_end, rain_start, rain_end, soil_kf
        )
        outline = _outline(scene, geom)
        heading = title or f"Wildfire {fire_start} → rain {rain['times_60'][0][:10]} to {rain['times_60'][-1][:10]}"
        footer = (
            "Sentinel-2 L2A dNBR · Copernicus DEM GLO-30 · ESA WorldCover 2021 · Open-Meteo · © OpenStreetMap · "
            "USGS M1 (Staley et al. 2017) · screening, not an official warning"
        )
        png = await asyncio.to_thread(figure.cascade_png, scene, outline, rain, result, heading, footer)
        payload = {
            "verdict": _verdict(result, scene, rain),
            "risk_level": result["risk_level"],
            "likelihood": result["likelihood"],
            "places_at_risk": _places_at_risk(scene),
            "image": {"format": "png", "bytes": len(png)},
            "provenance": _provenance(geom, how, scene, rain),
        }
        return [Image(data=png, format="png"), json.dumps(payload)]
    except Exception as exc:  # noqa: BLE001
        return [_error(str(exc))]


# Image + text content; no structured (JSON-schema) output for this tool.
@mcp.tool(structured_output=False)
async def burn_scar_photos(
    fire_start: str,
    fire_end: str | None = None,
    geometry: dict | str | None = None,
    bbox: list[float] | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: float | None = None,
    title: str | None = None,
) -> list:
    """
    Show natural-colour satellite photos of an area before and after a wildfire.

    Use this to let people see the burn scar with their own eyes, e.g. at the
    start of a report or next to cascade_figure. The two Sentinel-2 photos are
    the same scenes the burn analysis uses; the analysed area is outlined in
    yellow and the main towns and villages are named (in bold when they lie
    within 2 km of steep, badly burned slopes).

    Args:
        fire_start, fire_end, geometry, bbox, latitude, longitude, radius_km:
            as in burn_severity.
        title: Optional figure title, e.g. the fire name.

    Returns:
        The JPEG image followed by a JSON block with the photo dates, the named
        places and provenance.
    """
    try:
        geom, how = aoi.to_area(geometry, bbox, latitude, longitude, radius_km)
        start, end = _fire_period(fire_start, fire_end)
        scene = await asyncio.to_thread(_burn, geom, start, end)
        footer = "Copernicus Sentinel-2 L2A (Element 84 / AWS) · place names © OpenStreetMap contributors"
        jpeg = await asyncio.to_thread(figure.before_after_jpeg, scene, _outline(scene, geom),
                                       title or f"Wildfire of {fire_start}", footer)
        payload = {
            "before": scene.scenes["pre_fire"]["datetime"][:10],
            "after": scene.scenes["post_fire"]["datetime"][:10],
            "burned_ha": scene.stats.burned_ha,
            "places": _burn_summary(scene)["places"],
            "image": {"format": "jpeg", "bytes": len(jpeg)},
            "provenance": _provenance(geom, how, scene, None),
        }
        return [Image(data=jpeg, format="jpeg"), json.dumps(payload)]
    except Exception as exc:  # noqa: BLE001
        return [_error(str(exc))]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Wildfire Flood MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
