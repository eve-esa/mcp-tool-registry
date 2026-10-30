"""
Crop Suitability — MCP Server
=============================
Drought-aware screening of whether an area (bbox) suits a crop, from multi-year
Sentinel-2 vegetation indices, ERA5 weather, terrain, land cover / surveyed crop
types. All sources are public: no credentials.

Tools:
    plan_crop_analysis        — check the area, detect the crop, return a menu of options (call first)
    get_vegetation_indices    — NDVI / NDMI / NDWI per season, month, year or custom period, with anomalies
    get_weather_drought       — rain, ET0, water balance, temperature, soil moisture and their anomalies
    get_crop_landcover        — WorldCover land-cover shares + WorldCereal surveyed crop types
    get_terrain               — elevation, slope classes, low-lying share (Copernicus DEM 30 m)
    assess_crop_suitability   — rule-based verdict per criterion, with sources
    compare_areas             — rank 2-5 areas by suitability and drought impact
    get_sentinel2_maps        — real Sentinel-2 true colour / NDVI / NDMI maps per year, 1-5 areas

Usage:
    python server.py                      # HTTP transport (streamable-http on :8000/mcp)
    python server.py --transport stdio    # stdio transport for local MCP clients
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Literal

import charts
import crop_rules
import maps
import sentinel2
import sources
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from sources import ToolInputError

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("crop-suitability")
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("Crop Suitability", host="0.0.0.0", port=8000, stateless_http=True)

Period = Literal["season", "month", "year", "custom"]
RAG_COLLECTIONS = ["Wiley AI Gateway", "Wikipedia EO"]


def _error(error_type: str, error: str, **extra) -> str:
    return json.dumps({"ok": False, "error_type": error_type, "error": error, **extra})


def _tag(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8]


def _fmt_bbox(bbox) -> str:
    return ",".join(f"{v:.4f}" for v in bbox)


async def _run(coro_fn, *args, **kwargs) -> str:
    """Run a tool body and turn every failure into a JSON error the agent can relay."""
    try:
        return json.dumps(await coro_fn(*args, **kwargs), default=str)
    except ToolInputError as exc:
        return _error(exc.error_type, str(exc))
    except Exception as exc:  # noqa: BLE001 - an upstream service failed; report it to the user
        logger.exception("Tool failed")
        return _error("upstream_error", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Evidence for one area, shared by assess_crop_suitability and compare_areas
# ---------------------------------------------------------------------------


async def _gather_evidence(box, start: date, end: date) -> tuple[dict, list[str]]:
    """Every dataset the rules need, fetched in parallel (cached for an hour). Failures are listed, not raised."""
    lon, lat = sources.bbox_center(box)
    missing: list[str] = []

    async def safe(coro, label):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - assess with whatever evidence is available
            logger.warning("%s unavailable: %s", label, exc)
            missing.append(f"{label}: {exc}")
            return None

    series, daily, landcover, worldcereal, terrain = await asyncio.gather(
        safe(asyncio.to_thread(sentinel2.load_scene_series, box, start, end), "sentinel2"),
        safe(sources.weather_daily(lon, lat, sources._shift_years(start, 10), end), "weather"),
        safe(asyncio.to_thread(sources.worldcover_fractions, box), "worldcover"),
        safe(sources.worldcereal_crops(box), "worldcereal"),
        safe(asyncio.to_thread(sources.terrain_stats, box), "terrain"),
    )
    if series and not series.get("ok"):
        missing.append(f"sentinel2: {series.get('error')}")
    evidence = {
        "lat": lat,
        "start": start,
        "end": end,
        "scenes": series.get("scenes") if series and series.get("ok") else None,
        "daily": daily,
        "landcover": landcover,
        "worldcereal": worldcereal,
        "terrain": terrain,
    }
    return evidence, missing


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def plan_crop_analysis(
    bbox: str,
    start_date: str | None = None,
    end_date: str | None = None,
    crop: str | None = None,
) -> str:
    """Plan a crop suitability analysis. ALWAYS call this first, then show the plan to the user and wait.

    Checks the area and period, counts usable Sentinel-2 scenes, and looks up which
    crops ESA WorldCereal has surveyed in and around the area. Returns a menu of
    datasets, indices, period types and outputs with recommended defaults and
    estimated run times, plus a question about the crop when it is unclear.

    After this tool: present the plan and the `questions_for_user` to the user and
    STOP. Only run the other tools once the user has chosen.

    Args:
        bbox: Area as "west,south,east,north" in degrees (max ~400 km²), e.g. "8.30,45.28,8.42,45.36".
              Use a geocoding tool first if the user gave a place name.
        start_date: First day, YYYY-MM-DD (default: 1 January three years ago). Sentinel-2 starts 2017.
        end_date: Last day, YYYY-MM-DD (default: today).
        crop: Crop the user wants to assess (e.g. "rice"). Leave empty to detect it from WorldCereal.

    Returns:
        JSON with area info, crop detection (crop_status: user_specified | detected | ambiguous | not_found),
        the option menu, and questions_for_user.
    """

    async def body():
        box = sources.parse_bbox(bbox)
        start, end = sources.parse_dates(start_date, end_date)
        (items, search), worldcereal = await asyncio.gather(
            asyncio.to_thread(sentinel2.search_scenes, box, start, end, 40, 2),
            sources.worldcereal_crops(box),
        )
        detection = sources.crop_detection(worldcereal, crop)
        n = search["n_selected"]
        questions = []
        if detection.get("ask_user"):
            questions.append(detection["ask_user"])
        questions.append(
            "Which analyses do you want (vegetation indices, weather/drought, land cover, terrain, "
            "final suitability verdict), which indices, and which time grouping (season, month, year or custom ranges)?"
        )
        return {
            "ok": True,
            "step": "awaiting_user_choice",
            "instructions_for_agent": "Show this plan to the user, ask questions_for_user, and wait for the answer "
            "before calling any other crop_suitability tool.",
            "area": {
                "bbox": _fmt_bbox(box),
                "area_km2": round(sources.bbox_area_km2(box), 1),
                "centre_lon_lat": [round(v, 4) for v in sources.bbox_center(box)],
                "sentinel2_resolution_m": sentinel2.choose_resolution(box),
            },
            "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
            "crop_detection": detection,
            "sentinel2_scenes": search,
            "menu": {
                "analyses": [
                    {"id": "vegetation", "tool": "get_vegetation_indices", "default": True, "est_seconds": round(10 + 0.5 * n)},
                    {"id": "weather_drought", "tool": "get_weather_drought", "default": True, "est_seconds": 5},
                    {"id": "landcover_crops", "tool": "get_crop_landcover", "default": True, "est_seconds": 20},
                    {"id": "terrain", "tool": "get_terrain", "default": False, "est_seconds": 10},
                    {"id": "suitability", "tool": "assess_crop_suitability", "default": True, "est_seconds": round(30 + 0.5 * n)},
                    {"id": "literature", "tool": "retrieve (EVE retrieval server)", "default": True, "est_seconds": 15},
                ],
                "indices": {name: desc for name, (_, _, desc) in sentinel2.INDICES.items()},
                "default_indices": ["ndvi", "ndmi"] + (["ndwi"] if "rice" in str(detection.get("crop") or crop or "").lower() else []),
                "period_types": {
                    "season": "meteorological seasons DJF/MAM/JJA/SON (default)",
                    "month": "calendar months",
                    "year": "calendar years",
                    "custom": "your own ranges, e.g. growing seasons ['2022-05-01/2022-09-30', ...]",
                },
                "outputs": {
                    "json": "structured series (always returned)",
                    "charts": "PNG charts and CSV tables saved on the server (make_chart=true)",
                },
            },
            "questions_for_user": questions,
        }

    return await _run(body)


@mcp.tool()
async def get_vegetation_indices(
    bbox: str,
    start_date: str | None = None,
    end_date: str | None = None,
    indices: list[str] | None = None,
    period: Period = "season",
    custom_periods: list[str] | None = None,
    max_cloud_cover: float = 40,
    scenes_per_month: int = 2,
    cropland_only: bool = True,
    make_chart: bool = True,
    include_scenes: bool = False,
) -> str:
    """Sentinel-2 vegetation index time series (NDVI, NDMI, NDWI) for an area, grouped by period.

    For each scene the median index over clear cropland pixels is computed (clouds,
    shadows and snow masked with the Scene Classification Layer; cropland from ESA
    WorldCover). Scenes are grouped by period, and each period is compared with the
    same season/month in the other years (anomaly and z-score). Negative NDMI
    anomalies in the growing season indicate drought stress.

    Args:
        bbox: Area as "west,south,east,north" in degrees (max ~400 km²).
        start_date: First day, YYYY-MM-DD (default: 1 January three years ago).
        end_date: Last day, YYYY-MM-DD (default: today).
        indices: Any of "ndvi" (greenness), "ndmi" (vegetation water content), "ndwi" (open water / flooded paddies).
                 Default ["ndvi", "ndmi"].
        period: "season" (DJF/MAM/JJA/SON), "month", "year" or "custom".
        custom_periods: For period="custom": ranges as "YYYY-MM-DD/YYYY-MM-DD", e.g. growing seasons per year.
        max_cloud_cover: Skip scenes with more scene-level cloud cover than this (%, default 40).
        scenes_per_month: Clearest scenes kept per month (default 2; more = slower).
        cropland_only: Only use WorldCover cropland pixels (default true).
        make_chart: Save a PNG chart and a CSV of scene values on the server (default true).
        include_scenes: Also return every scene value in the JSON (default false; the CSV has them).

    Returns:
        JSON with per-period median / p10 / p90 per index, anomaly vs other years,
        scene counts, data-quality info, warnings and chart/CSV paths.
    """

    async def body():
        box = sources.parse_bbox(bbox)
        start, end = sources.parse_dates(start_date, end_date)
        chosen = [i.lower() for i in (indices or ["ndvi", "ndmi"])]
        unknown = [i for i in chosen if i not in sentinel2.INDICES]
        if unknown:
            raise ToolInputError("unknown_index", f"Unknown index {unknown}. Use: {', '.join(sentinel2.INDICES)}.")
        if period == "custom" and not custom_periods:
            raise ToolInputError("missing_custom_periods", "period='custom' needs custom_periods.")
        series = await asyncio.to_thread(
            sentinel2.load_scene_series, box, start, end, max_cloud_cover, max(1, min(scenes_per_month, 4)), cropland_only,
            with_ndwi="ndwi" in chosen,
        )
        if not series.get("ok"):
            return {**series, "error_type": "no_data"}
        try:
            periods = sentinel2.aggregate(series["scenes"], period, chosen, custom_periods)
        except (ValueError, KeyError):
            raise ToolInputError("invalid_custom_periods", "custom_periods must look like '2022-05-01/2022-09-30'.") from None
        out = {
            "ok": True,
            "bbox": _fmt_bbox(box),
            "period_type": period,
            "indices": {i: sentinel2.INDICES[i][2] for i in chosen},
            "data": {k: series[k] for k in ("source", "resolution_m", "cropland_only", "cropland_fraction_worldcover", "search")},
            "periods": periods,
            "warnings": series["warnings"],
            "how_to_read": "median/p10/p90 are across scenes in the period; anomaly = median minus the mean of the same "
            "season/month in the other years; z = anomaly / std of those years.",
        }
        if include_scenes:
            out["scenes"] = series["scenes"]
        if make_chart:
            title = f"Sentinel-2 indices over cropland, bbox {_fmt_bbox(box)}"
            out["files"] = charts.vegetation_chart(
                _tag(box, start, end, period, chosen), series["scenes"], periods, chosen, title
            )
        return out

    return await _run(body)


@mcp.tool()
async def get_weather_drought(
    bbox: str,
    start_date: str | None = None,
    end_date: str | None = None,
    period: Period = "season",
    custom_periods: list[str] | None = None,
    baseline_years: int = 10,
    make_chart: bool = True,
) -> str:
    """Weather and drought indicators per period, compared with the previous years.

    From the ERA5 / ERA5-Land reanalysis (Open-Meteo archive, at the bbox centre):
    precipitation, reference evapotranspiration (ET0), water balance P−ET0, mean and
    max temperature, hot days, and soil moisture at 0–7 cm and 7–28 cm. Each period
    is compared with the same calendar window in the previous `baseline_years` years.

    Args:
        bbox: Area as "west,south,east,north" in degrees.
        start_date: First day, YYYY-MM-DD (default: 1 January three years ago).
        end_date: Last day, YYYY-MM-DD (default: today; the archive lags ~5 days).
        period: "season", "month", "year" or "custom".
        custom_periods: For period="custom": ranges as "YYYY-MM-DD/YYYY-MM-DD".
        baseline_years: Number of earlier years used as the normal (default 10, max 30).
        make_chart: Save a PNG chart on the server (default true).

    Returns:
        JSON with one row per period (totals, means, anomaly diffs, z-scores, % of normal
        precipitation) and the chart path.
    """

    async def body():
        box = sources.parse_bbox(bbox)
        start, end = sources.parse_dates(start_date, end_date)
        years = max(1, min(baseline_years, 30))
        if period == "custom" and not custom_periods:
            raise ToolInputError("missing_custom_periods", "period='custom' needs custom_periods.")
        lon, lat = sources.bbox_center(box)
        daily = await sources.weather_daily(lon, lat, sources._shift_years(start, years), end)
        try:
            windows = sources.period_windows(start, end, period, custom_periods)
        except (ValueError, KeyError):
            raise ToolInputError("invalid_custom_periods", "custom_periods must look like '2022-05-01/2022-09-30'.") from None
        rows = sources.aggregate_weather(daily, windows, years)
        out = {
            "ok": True,
            "bbox": _fmt_bbox(box),
            "source": "ERA5 / ERA5-Land reanalysis via Open-Meteo historical weather API (CC BY 4.0)",
            "location_used": {"lon": round(lon, 4), "lat": round(lat, 4), "note": "one grid cell (~10 km) at the bbox centre"},
            "period_type": period,
            "baseline": f"same window in the previous {years} years",
            "periods": rows,
        }
        if make_chart and rows:
            out["files"] = charts.weather_chart(
                _tag(box, start, end, period, "wx"), rows, f"Weather per {period}, ({lon:.3f}, {lat:.3f})"
            )
        return out

    return await _run(body)


@mcp.tool()
async def get_crop_landcover(bbox: str, crop: str | None = None, search_buffer_km: float = 10.0) -> str:
    """Land cover and surveyed crop types for an area.

    Combines ESA WorldCover 2021 (10 m land-cover class shares: cropland, tree cover,
    built-up, water, ...) with field-surveyed crop types from the ESA WorldCereal
    Reference Data Module (LUCAS, national parcel registers, etc.) in and around the
    area. Survey samples are sparse and from various years: treat them as evidence,
    not as a map of every field.

    Args:
        bbox: Area as "west,south,east,north" in degrees.
        crop: Optional crop to look for (e.g. "rice").
        search_buffer_km: Search WorldCereal samples this far around the bbox (default 10 km).

    Returns:
        JSON with land-cover shares, the crops found (label, sample count, survey years,
        samples inside the bbox) and the crop detection result.
    """

    async def body():
        box = sources.parse_bbox(bbox)
        landcover, worldcereal = await asyncio.gather(
            asyncio.to_thread(sources.worldcover_fractions, box),
            sources.worldcereal_crops(box, max(0.0, min(search_buffer_km, 50.0))),
        )
        return {
            "ok": True,
            "bbox": _fmt_bbox(box),
            "worldcover": landcover,
            "worldcereal": worldcereal,
            "crop_detection": sources.crop_detection(worldcereal, crop),
        }

    return await _run(body)


@mcp.tool()
async def get_terrain(bbox: str) -> str:
    """Terrain of an area from the Copernicus DEM (30 m): elevation, slope classes and low-lying share.

    Flat land (< 2% slope) is needed for flooded crops such as paddy rice; low-lying
    ground relative to its surroundings tends to keep more moisture.

    Args:
        bbox: Area as "west,south,east,north" in degrees.

    Returns:
        JSON with elevation min/mean/max, slope statistics, slope class shares and
        the share of the area within 2 m / 5 m of its lowest ground.
    """

    async def body():
        box = sources.parse_bbox(bbox)
        return {"ok": True, "bbox": _fmt_bbox(box), **await asyncio.to_thread(sources.terrain_stats, box)}

    return await _run(body)


@mcp.tool()
async def assess_crop_suitability(
    crop: str,
    bbox: str,
    start_date: str | None = None,
    end_date: str | None = None,
    growing_months: list[int] | None = None,
    make_chart: bool = True,
) -> str:
    """Rule-based screening of whether an area suits a crop, given recent drought.

    Gathers (or reuses from earlier calls) the Sentinel-2 indices, weather, land cover,
    WorldCereal crop samples and terrain, then scores criteria such as
    crop presence, slope, growing-season temperature, rain vs crop water need, drought
    signal (soil moisture and water-balance anomalies), peak NDVI, NDMI anomaly, the
    drought frequency and the rice flooding signal. Each criterion reports pass / warn / fail,
    its value, threshold and source.

    This is a screening from past observations, not a yield forecast. Back the result
    with literature: call the EVE `retrieve` tool with the arguments in `retrieve_calls`, and cite
    only the `citationLine` values it returns.
    Show the user `criteria_table_markdown` as is: it lists every criterion, including warnings.

    Args:
        crop: Crop to assess: rice, maize, wheat, soybean (others use generic rules).
        bbox: Area as "west,south,east,north" in degrees.
        start_date: First day, YYYY-MM-DD (default: 1 January three years ago).
        end_date: Last day, YYYY-MM-DD (default: today).
        growing_months: Override the growing season months, e.g. [5, 6, 7, 8, 9].
        make_chart: Save seasonal vegetation and weather charts on the server (default true).

    Returns:
        JSON with verdict (likely_suitable | marginal_at_risk | unlikely_suitable |
        insufficient_data), score 0-1, confidence, per-criterion results, a markdown
        criteria table, caveats and retrieve_calls.
    """

    async def body():
        box = sources.parse_bbox(bbox)
        start, end = sources.parse_dates(start_date, end_date)
        if growing_months and not all(1 <= m <= 12 for m in growing_months):
            raise ToolInputError("invalid_growing_months", "growing_months must be month numbers 1-12.")
        lon, lat = sources.bbox_center(box)
        evidence, missing = await _gather_evidence(box, start, end)
        daily = evidence["daily"]
        result = crop_rules.evaluate(crop, evidence, growing_months)
        files = {}
        if make_chart and evidence["scenes"]:
            periods = sentinel2.aggregate(evidence["scenes"], "season", ["ndvi", "ndmi"])
            files["vegetation"] = charts.vegetation_chart(
                _tag(box, start, end, "season", "assess"), evidence["scenes"], periods, ["ndvi", "ndmi"],
                f"Sentinel-2 indices over cropland, bbox {_fmt_bbox(box)}",
            )
        if make_chart and daily:
            rows = sources.aggregate_weather(daily, sources.period_windows(start, end, "season"), 10)
            files["weather"] = charts.weather_chart(
                _tag(box, start, end, "season", "assess-wx"), rows, f"Weather per season, ({lon:.3f}, {lat:.3f})"
            )
        place = f"lat {lat:.2f}, lon {lon:.2f}"
        return {
            "ok": True,
            "bbox": _fmt_bbox(box),
            "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
            **result,
            "criteria_table_markdown": crop_rules.criteria_table(result["criteria"]),
            "files": files,
            "missing_evidence": missing,
            # Arguments for the EVE `retrieve` tool: it rejects calls without public_collections.
            "retrieve_calls": [
                {"query": q, "k": 4, "public_collections": RAG_COLLECTIONS}
                for q in (
                    f"{result['crop']} water requirements and drought sensitivity during the growing season",
                    f"NDVI and NDMI (LSWI) of healthy versus drought-stressed {result['crop']} from Sentinel-2",
                    f"drought impact on {result['crop']} yield near {place}",
                )
            ],
        }

    return await _run(body)


@mcp.tool()
async def compare_areas(
    bboxes: list[str],
    crop: str,
    names: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    growing_months: list[int] | None = None,
    make_chart: bool = True,
) -> str:
    """Compare 2 to 5 areas for one crop: which is most suitable, and which is most affected by drought.

    For every area this runs the full suitability screening (Sentinel-2 vegetation,
    ERA5 weather, land cover, WorldCereal crops, terrain) and a drought-impact analysis:
    how often growing seasons were dry since ~10 years before start_date, the driest
    growing season in the period, and how much peak NDVI and NDMI dropped in that season.
    Areas are ranked by suitability score (ties broken by lower drought impact).

    Show the user `comparison_table_markdown` as is, then the ranking and the most
    drought-affected area. Use the same crop for all areas.

    Args:
        bboxes: 2-5 areas, each "west,south,east,north" in degrees (max ~400 km² each, ~30 km² is fastest).
        crop: Crop to compare for: rice, maize, wheat, soybean (others use generic rules).
        names: Optional short names for the areas, in the same order (e.g. ["Szeged", "Szolnok"]).
        start_date: First day, YYYY-MM-DD (default: 1 January three years ago).
        end_date: Last day, YYYY-MM-DD (default: today).
        growing_months: Override the growing season months, e.g. [5, 6, 7, 8, 9].
        make_chart: Save a comparison chart on the server (default true).

    Returns:
        JSON with one entry per area (verdict, score, warnings, drought impact, per-season
        weather and vegetation), the ranking, the most drought-affected area, a markdown
        comparison table and the chart path.
    """

    async def body():
        if not 2 <= len(bboxes) <= 5:
            raise ToolInputError("invalid_areas", "Give between 2 and 5 bboxes to compare.")
        if names and len(names) != len(bboxes):
            raise ToolInputError("invalid_names", "names must have one entry per bbox, in the same order.")
        labels = list(names) if names else [f"Area {i + 1}" for i in range(len(bboxes))]
        boxes = []
        for label, bbox in zip(labels, bboxes, strict=True):
            try:
                boxes.append(sources.parse_bbox(bbox))
            except ToolInputError as exc:
                raise ToolInputError(exc.error_type, f"{label}: {exc}") from None
        start, end = sources.parse_dates(start_date, end_date)
        if growing_months and not all(1 <= m <= 12 for m in growing_months):
            raise ToolInputError("invalid_growing_months", "growing_months must be month numbers 1-12.")

        limit = asyncio.Semaphore(2)  # at most two Sentinel-2 loads at a time

        async def one(label, box):
            async with limit:
                evidence, missing = await _gather_evidence(box, start, end)
            result = crop_rules.evaluate(crop, evidence, growing_months)
            impact = crop_rules.drought_impact(crop, evidence, growing_months)
            return {
                "name": label,
                "bbox": _fmt_bbox(box),
                "area_km2": round(sources.bbox_area_km2(box), 1),
                "verdict": result["verdict"],
                "score": result["score"],
                "confidence": result["confidence"],
                "warnings": [f"{c['criterion']} ({c['status']})" for c in result["criteria"] if c["status"] != "pass"],
                "drought": impact,
                "missing_evidence": missing,
            }

        areas = await asyncio.gather(*(one(label, box) for label, box in zip(labels, boxes, strict=True)))

        def rank_key(a):
            impact = a["drought"]["drought_impact_index"]
            return (-(a["score"] if a["score"] is not None else -1), impact if impact is not None else 1)

        ranking = [a["name"] for a in sorted(areas, key=rank_key)]
        for a in areas:
            a["rank"] = ranking.index(a["name"]) + 1
        with_impact = [a for a in areas if a["drought"]["drought_impact_index"] is not None]
        most_affected = max(with_impact, key=lambda a: a["drought"]["drought_impact_index"])["name"] if with_impact else None
        least_affected = min(with_impact, key=lambda a: a["drought"]["drought_impact_index"])["name"] if with_impact else None

        def fmt(v, signed=False):
            return "–" if v is None else (f"{v:+.2f}" if signed else f"{v}")

        lines = [
            "| Rank | Area | Verdict | Score | Drought impact | Dry seasons in record | Driest season in period | "
            "Peak NDVI change then | NDMI change then | Warnings |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for a in sorted(areas, key=lambda a: a["rank"]):
            d = a["drought"]
            lines.append(
                f"| {a['rank']} | {a['name']} | {a['verdict'].replace('_', ' ')} | {fmt(a['score'])} | "
                f"{fmt(d['drought_impact_index'])} ({d['drought_impact_level'] or '–'}) | "
                f"{len(d['dry_seasons_in_record'])}/{d['seasons_in_record']} ({', '.join(map(str, d['dry_seasons_in_record'])) or 'none'}) | "
                f"{fmt(d['driest_year_in_period'])} | {fmt(d['peak_ndvi_change_in_driest_year'], True)} | "
                f"{fmt(d['ndmi_change_in_driest_year'], True)} | {', '.join(a['warnings']) or 'none'} |"
            )
        out = {
            "ok": True,
            "crop": crop_rules.resolve_crop(crop)[0],
            "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
            "ranking_by_suitability": ranking,
            "most_drought_affected": most_affected,
            "least_drought_affected": least_affected,
            "comparison_table_markdown": "\n".join(lines),
            "areas": areas,
            "how_to_read": "Rank 1 = most suitable (highest score; ties broken by lower drought impact). "
            + areas[0]["drought"]["how_to_read"],
            "caveats": [
                "A screening from past observations, not a yield forecast.",
                "Weather is one ERA5 grid cell (~10 km) per area: areas closer than ~10 km share the same weather.",
            ],
            "retrieve_calls": [
                {
                    "query": f"drought impact on {crop} yield and regional differences in drought vulnerability",
                    "k": 4,
                    "public_collections": RAG_COLLECTIONS,
                }
            ],
        }
        if make_chart:
            out["files"] = {
                "comparison": charts.comparison_chart(
                    _tag(*boxes, start, end, crop, "compare"), areas, f"{out['crop']} — {start.year}-{end.year}"
                )
            }
        return out

    return await _run(body)


@mcp.tool()
async def get_sentinel2_maps(
    bboxes: list[str],
    names: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    month: int | None = None,
    crop: str | None = None,
    max_cloud_cover: float = 30,
) -> str:
    """Real Sentinel-2 images of 1-5 areas: true colour, NDVI and NDMI maps, one per year, same month.

    For every year in the period, the clearest scenes of one month are combined into a
    cloud-free median composite at 10 m (20-30 m for big areas). With one area you get a
    grid of years × (true colour, NDVI, NDMI, NDVI change vs the other years); with
    several areas you get areas × years grids for true colour, NDVI and NDMI, so fields
    can be compared side by side. Use it to show the user what the fields looked like,
    e.g. in a drought year versus a normal year.

    Args:
        bboxes: 1-5 areas, each "west,south,east,north" in degrees (max ~400 km² each).
        names: Optional short names for the areas, in the same order.
        start_date: First day, YYYY-MM-DD (default: 1 January three years ago). One image per year, max 6 years.
        end_date: Last day, YYYY-MM-DD (default: today).
        month: Month to show, 1-12 (default: middle of the crop's growing season, or July).
        crop: Optional crop, used to choose the default month.
        max_cloud_cover: Only use scenes with at most this scene-level cloud cover (%, default 30).

    Returns:
        JSON with, per area and year, the scene dates used, the clear-pixel share and the
        mean NDVI / NDMI, plus the paths of the PNG map figures.
    """

    async def body():
        if not 1 <= len(bboxes) <= 5:
            raise ToolInputError("invalid_areas", "Give between 1 and 5 bboxes.")
        if names and len(names) != len(bboxes):
            raise ToolInputError("invalid_names", "names must have one entry per bbox, in the same order.")
        if month is not None and not 1 <= month <= 12:
            raise ToolInputError("invalid_month", "month must be 1-12.")
        labels = list(names) if names else [f"Area {i + 1}" for i in range(len(bboxes))]
        boxes = []
        for label, bbox in zip(labels, bboxes, strict=True):
            try:
                boxes.append(sources.parse_bbox(bbox))
            except ToolInputError as exc:
                raise ToolInputError(exc.error_type, f"{label}: {exc}") from None
        start, end = sources.parse_dates(start_date, end_date)
        years = maps.years_between(start, end)
        chosen_month = month or maps.default_month(sources.bbox_center(boxes[0])[1], crop)

        limit = asyncio.Semaphore(2)

        async def one(box):
            async with limit:
                return await asyncio.to_thread(maps.load_composites, box, years, chosen_month, max_cloud_cover)

        composites = await asyncio.gather(*(one(box) for box in boxes))
        tag = _tag(*boxes, years, chosen_month, max_cloud_cover, "maps")
        files: dict = {}
        if len(boxes) == 1:
            if composites[0]["years"]:
                files.update(charts.area_maps(tag, labels[0], composites[0], composites[0]["resolution_m"]))
        else:
            pairs = [(label, comp) for label, comp in zip(labels, composites, strict=True)]
            if any(comp["years"] for _, comp in pairs):
                for layer in ("rgb", "ndvi", "ndmi"):
                    files.update(charts.compare_maps(tag, pairs, layer))
        areas = [
            {
                "name": label,
                "bbox": _fmt_bbox(box),
                "resolution_m": comp["resolution_m"],
                "years": maps.summary(comp),
                "missing_years": [y for y in years if y not in comp["years"]],
            }
            for label, box, comp in zip(labels, boxes, composites, strict=True)
        ]
        if not files:
            return {"ok": False, "error_type": "no_data", "error": "No clear Sentinel-2 scene in that month for any year. "
                    "Try another month or raise max_cloud_cover.", "areas": areas}
        return {
            "ok": True,
            "source": "Sentinel-2 L2A, Microsoft Planetary Computer; cloud-masked median composites",
            "month": chosen_month,
            "areas": areas,
            "files": files,
            "how_to_read": "Same month every year. NDVI: green = dense, healthy canopy; red = bare or dry. "
            "NDMI: blue-green = moist vegetation; brown = dry. A drought shows as a shift of the whole area "
            "(compare the means); field-by-field changes are mostly crop rotation and harvest timing.",
        }

    return await _run(body)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crop Suitability MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    mcp.settings.port = args.port

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
