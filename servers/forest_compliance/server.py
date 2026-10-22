"""
Forest Compliance — MCP Server
==============================
EU Deforestation Regulation (EUDR) screening for farm plots: was the land
forest on 31 December 2020 (the EUDR cut-off date), and has that forest been
lost since? Uses two public datasets read in place, no API key required:

    * JRC Global Forest Cover 2020 V4 (10 m) — the EU Observatory reference map
    * Hansen Global Forest Change v1.12 (30 m) — year of tree cover loss to 2024

Tools:
    eudr_check_plot  — screen one plot (polygon, point or lat/lon)
    eudr_check_plots — screen a supplier list (GeoJSON FeatureCollection)
    eudr_plot_map    — PNG map of a plot: 2020 forest and loss before/after the cut-off
    eudr_overview_map — PNG map of a supplier list, each plot coloured by risk
    supply_chain_resilience — EUDR risk x drought risk matrix and decision map for a supplier list

Results are a risk screening for due diligence, not a legal determination.
Every response carries a ``provenance`` block for the audit trail.

Module layout (this file only orchestrates; the work happens elsewhere):
    server.py     MCP tools: parse arguments, run the blocking steps in a worker
                  thread, build the JSON / image responses, turn errors into
                  ``{"error": ...}`` so the agent can react instead of crashing.
    forest.py     pure logic: plot geometry and area, Hansen tiles, pixel
                  tallies, risk assessment and the single-plot PNG map.
    sources.py    remote reads of the two rasters (rasterio over /vsicurl/).
    overview.py   matplotlib overview map of many plots.
    resilience.py EUDR x drought supply-chain matrix.

Flow of one plot: forest.to_plot_geometry -> forest.plot_area_check ->
sources.measure (-> forest.tally per tile) -> forest.assess -> JSON.

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
from datetime import UTC, datetime
from typing import Any

import forest
import overview
import resilience
import sources
from mcp.server.fastmcp import FastMCP, Image

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("forest-compliance-mcp")

SERVER_NAME = "Forest Compliance"
SERVER_VERSION = "0.1.0"
# Limits that keep one tool call within a reasonable time and response size;
# plots are read one after another, each costing a few remote reads.
MAX_PLOTS_PER_CALL = 25
MAX_MAP_PLOT_HA = 2_500.0  # larger plots would make the 10 m map image too big
MAX_OVERVIEW_SPAN_DEG = 5.0  # wider regions are better split into regional maps

# Stateless HTTP: every request is self-contained, so the server can be scaled
# or restarted without losing client sessions.
mcp = FastMCP(SERVER_NAME, host="0.0.0.0", port=8000, stateless_http=True)

# Severity order of the risk levels, used to find the worst plot of a batch.
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _provenance(tiles: list[str], how: str, geom_id: str) -> dict[str, Any]:
    """Provenance block attached to every response, for the due-diligence audit trail.

    It records what was assessed (plot fingerprint and how the geometry was
    built), with which data (datasets, versions, tiles), by which method, the
    known data gap (clearing after the last Hansen year) and when.

    Args:
        tiles: Hansen tile names read.
        how: How the plot geometry was obtained (from ``forest.to_plot_geometry``).
        geom_id: Plot fingerprint (``forest.plot_id``).
    """
    return {
        "plot_id": geom_id,
        "plot_geometry_from": how,
        "datasets": sources.DATASETS,
        "hansen_tiles": tiles,
        "cutoff_date": f"{forest.CUTOFF_YEAR}-12-31",
        "method": (
            "Pixels whose centre lies inside the plot are counted on the 10 m GFC2020 grid; Hansen "
            "loss years are resampled onto it (nearest). Deforestation after the cut-off = forest in "
            "GFC2020 AND Hansen loss year 2021 or later."
        ),
        "data_gap": (
            f"Hansen loss is available up to {sources.HANSEN_LAST_YEAR}; clearing in "
            f"{sources.HANSEN_LAST_YEAR + 1} or later is not yet covered. Check recent Sentinel-2 imagery."
        ),
        "disclaimer": "Risk screening for due diligence, not a legal determination under Regulation (EU) 2023/1115.",
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "server": f"{SERVER_NAME} MCP server v{SERVER_VERSION}",
    }


def _round(stats: dict[str, Any]) -> dict[str, Any]:
    """Round areas to 3 decimals (10 m^2) for the JSON output.

    Year keys become strings, as JSON object keys must be strings anyway.
    """
    out = {k: round(v, 3) if isinstance(v, float) else v for k, v in stats.items()}
    out["deforestation_by_year_ha"] = {str(y): round(v, 3) for y, v in stats["deforestation_by_year_ha"].items()}
    return out


def _check_one(geom: dict, how: str) -> dict[str, Any]:
    """Area check, raster reads and assessment for one plot; the full eudr_check_plot result.

    Blocking (remote raster reads): run it through ``asyncio.to_thread`` so the
    event loop keeps serving other requests.

    Args:
        geom: Plot Polygon/MultiPolygon from ``forest.to_plot_geometry``.
        how: How the geometry was obtained, for the provenance block.
    """
    started = time.monotonic()
    plot_ha = forest.plot_area_check(geom)
    stats, tiles = sources.measure(geom)
    result = {
        "plot_area_ha": round(plot_ha, 3),
        **forest.assess(stats, plot_ha),
        "areas_ha": _round(stats),
        "provenance": _provenance(tiles, how, forest.plot_id(geom)),
    }
    logger.info("Checked %.1f ha plot in %.1fs -> %s", plot_ha, time.monotonic() - started, result["risk_level"])
    return result


def _error(message: str) -> str:
    """Log a tool failure and return it as ``{"error": message}`` JSON for the agent."""
    logger.warning("Tool error: %s", message)
    return json.dumps({"error": message})


async def _assess_collection(plots: dict | str) -> tuple[list, dict, dict, list]:
    """Assess every feature of a FeatureCollection; shared by all batch tools.

    Plots are assessed one after another; a plot that fails (bad geometry,
    outside coverage, read error) gets an ``{"id", "error"}`` entry instead of
    failing the batch, so one bad record in a supplier list does not hide the
    others.

    Args:
        plots: GeoJSON FeatureCollection as an object or JSON string.

    Returns:
        (results, summary, provenance, drawn): one compact entry per plot, the
        batch summary, a batch provenance block (tiles of all plots), and
        (id, geometry, risk_level) for each plot that could be assessed (used
        to draw the overview map).

    Raises:
        ValueError: not a FeatureCollection, empty, or more than MAX_PLOTS_PER_CALL features.
    """
    collection = json.loads(plots) if isinstance(plots, str) else plots
    if collection.get("type") != "FeatureCollection":
        raise ValueError("plots must be a GeoJSON FeatureCollection")
    features = collection.get("features", [])
    if not features:
        raise ValueError("the FeatureCollection has no features")
    if len(features) > MAX_PLOTS_PER_CALL:
        raise ValueError(f"at most {MAX_PLOTS_PER_CALL} plots per call; split the list")

    results = []
    drawn = []
    for index, feature in enumerate(features):
        props = feature.get("properties") or {}
        plot_ref = props.get("id") or props.get("name") or f"plot-{index + 1}"  # 1-based, as users count
        try:
            geom, how = forest.to_plot_geometry(feature)
            full = await asyncio.to_thread(_check_one, geom, how)
            areas = full["areas_ha"]
            results.append({
                "id": plot_ref,
                "risk_level": full["risk_level"],
                "verdict": full["verdict"],
                "notes": full["notes"],
                "plot_area_ha": full["plot_area_ha"],
                "forest_2020_ha": areas["forest_2020_ha"],
                "deforestation_after_cutoff_ha": areas["deforestation_after_cutoff_ha"],
                "deforestation_by_year_ha": areas["deforestation_by_year_ha"],
                "plot_id": full["provenance"]["plot_id"],
                "hansen_tiles": full["provenance"]["hansen_tiles"],
            })
            drawn.append((plot_ref, geom, full["risk_level"]))
        except Exception as exc:  # noqa: BLE001 - one bad plot must not sink the batch
            results.append({"id": plot_ref, "error": str(exc)})

    assessed = [r for r in results if "error" not in r]
    counts = {level: sum(1 for r in assessed if r["risk_level"] == level) for level in RISK_ORDER}
    summary = {
        "plots": len(results),
        "assessed": len(assessed),
        "failed": len(results) - len(assessed),
        "by_risk_level": counts,
        "high_risk_share": round(counts["high"] / len(assessed), 3) if assessed else None,
        "total_area_ha": round(sum(r["plot_area_ha"] for r in assessed), 2),
        "total_deforestation_after_cutoff_ha": round(
            sum(r["deforestation_after_cutoff_ha"] for r in assessed), 3
        ),
        "worst_risk_level": max((r["risk_level"] for r in assessed), key=RISK_ORDER.get, default=None),
    }
    provenance = _provenance(
        sorted({t for r in assessed for t in r["hansen_tiles"]}), "per plot, see results", "per plot, see results"
    )
    return results, summary, provenance, drawn


# The docstrings of the tool functions below are the tool descriptions the LLM
# sees: they say when to use the tool and what the arguments and result mean.
# Implementation notes belong in comments, not in those docstrings.


@mcp.tool()
async def eudr_check_plot(
    geometry: dict | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    """
    Screen one farm plot for deforestation after the EUDR cut-off date (31 Dec 2020).

    Use this to check whether a commodity (cocoa, coffee, soy, palm oil, rubber,
    cattle, wood) from a plot can be deforestation-free under the EU
    Deforestation Regulation. It answers: was the plot forest on 31 Dec 2020
    (JRC GFC2020, the EU reference map) and has that forest lost tree cover
    since (Hansen Global Forest Change, up to 2024)?

    Args:
        geometry:  The plot as GeoJSON (Polygon, MultiPolygon, Point, Feature or a
                   one-feature FeatureCollection), as an object or a string,
                   WGS84 lon/lat. Points are treated as a 4 ha circle, the EUDR
                   threshold below which a point is accepted instead of a polygon.
        latitude:  Alternatively, the plot location as latitude (decimal degrees)...
        longitude: ...and longitude; treated as a 4 ha circle.

    Returns:
        JSON with risk_level ("low", "medium" or "high"), a verdict sentence,
        review notes, areas in hectares (plot, forest in 2020, loss before and
        after the cut-off, deforestation after the cut-off by year) and a
        provenance block (datasets, versions, URLs, plot fingerprint, data gap).
        This is a risk screening, not a legal determination.
    """
    # Errors are returned as JSON, not raised, so the agent can read them and retry.
    try:
        geom, how = forest.to_plot_geometry(geometry, latitude, longitude)
        return json.dumps(await asyncio.to_thread(_check_one, geom, how))
    except Exception as exc:  # noqa: BLE001 - every failure goes back to the agent as JSON
        return _error(str(exc))


@mcp.tool()
async def eudr_check_plots(plots: dict | str) -> str:
    """
    Screen a list of supplier plots for EUDR deforestation risk in one call.

    Use this for a due-diligence batch, e.g. every farm of a cocoa cooperative.
    Each plot is assessed exactly like eudr_check_plot.

    Args:
        plots: GeoJSON FeatureCollection (object or string) with up to 25
               features. Each feature is a Polygon, MultiPolygon or Point (a
               point becomes a 4 ha circle). An "id" or "name" in the feature
               properties is echoed back to identify the plot.

    Returns:
        JSON with one entry per plot (id, risk_level, verdict, key areas or an
        error), a summary (count per risk level, total area, total deforestation
        after the cut-off, share of plots at high risk) and a provenance block.
    """
    try:
        results, summary, provenance, _ = await _assess_collection(plots)
        return json.dumps({"summary": summary, "results": results, "provenance": provenance})
    except Exception as exc:  # noqa: BLE001
        return _error(str(exc))


# Image + text content; no structured (JSON-schema) output for this tool.
@mcp.tool(structured_output=False)
async def eudr_plot_map(
    geometry: dict | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> list:
    """
    Draw a map of a plot for an EUDR due-diligence report.

    Use this after eudr_check_plot when the user wants to see the evidence.
    The PNG shows the plot (black outline) and its surroundings (faded):
    dark green = forest on 31 Dec 2020 (JRC GFC2020); dark red = that forest
    lost after the cut-off (deforestation risk); light red = tree cover lost
    after the cut-off on land that was not forest in 2020; yellow = loss
    before the cut-off (2001-2020, allowed); beige = not forest in 2020.

    Args:
        geometry:  The plot as GeoJSON (same formats as eudr_check_plot), at
                   most 2,500 ha and inside one 10° tile.
        latitude:  Alternatively, latitude of a point (drawn as a 4 ha circle)...
        longitude: ...and longitude.

    Returns:
        The PNG image followed by a JSON legend with the plot fingerprint and
        the data sources.
    """
    try:
        geom, how = forest.to_plot_geometry(geometry, latitude, longitude)
        plot_ha = forest.plot_area_check(geom)
        if plot_ha > MAX_MAP_PLOT_HA:
            raise ValueError(f"maps are limited to {MAX_MAP_PLOT_HA:,.0f} ha plots")
        forest_2020, loss_year, inside = await asyncio.to_thread(sources.plot_rasters, geom)
        # No assessment here: the map is evidence for a result already obtained with eudr_check_plot.
        png = forest.render_map(forest_2020, loss_year, inside)
        legend = {
            "legend": {
                "dark green": "forest on 31 Dec 2020 (JRC GFC2020)",
                "dark red": "2020 forest lost after the cut-off (deforestation risk)",
                "light red": "tree cover lost after the cut-off on non-forest land",
                "yellow": "tree cover lost 2001-2020 (before the cut-off)",
                "beige": "not forest in 2020",
                "black line": "plot boundary; surroundings are faded",
            },
            "plot_area_ha": round(plot_ha, 3),
            "provenance": _provenance([t for t, _ in forest.hansen_tiles(forest.bbox(geom))], how, forest.plot_id(geom)),
        }
        return [Image(data=png, format="png"), json.dumps(legend)]
    except Exception as exc:  # noqa: BLE001
        return [_error(str(exc))]


# Image + text content; no structured (JSON-schema) output for this tool.
@mcp.tool(structured_output=False)
async def eudr_overview_map(plots: dict | str, title: str | None = None) -> list:
    """
    Draw one map of a supplier list with every plot coloured by EUDR risk.

    Use this to show a cooperative or sourcing region at a glance, e.g. after
    eudr_check_plots. Each plot is screened exactly like eudr_check_plot and
    drawn as an outline and a labelled marker: red = high risk, orange =
    medium, green = low. The background shades forest on 31 Dec 2020 (JRC
    GFC2020) from beige (no forest) to dark green.

    Args:
        plots: GeoJSON FeatureCollection (object or string) with up to 25
               features (Polygon, MultiPolygon or Point; a point becomes a 4 ha
               circle). properties.id or properties.name labels the plot. All
               plots together must fit within 5° x 5°.
        title: Optional map title, e.g. the cooperative name.

    Returns:
        The PNG map followed by a JSON block with the batch summary, one short
        entry per plot (id, risk level, deforestation after the cut-off) and a
        provenance block.
    """
    try:
        results, summary, provenance, drawn = await _assess_collection(plots)
        heading = title or "EUDR screening"
        heading += f": {summary['by_risk_level']['high']} of {summary['assessed']} plots at high risk"
        png, extent = await _render_overview(drawn, heading)
        # The image carries the detail; keep the text part short to save agent context.
        brief = [
            {k: r[k] for k in ("id", "risk_level", "deforestation_after_cutoff_ha", "plot_area_ha")}
            if "error" not in r else r
            for r in results
        ]
        payload = {
            "summary": summary,
            "plots": brief,
            "map": {"bounds": [round(v, 5) for v in extent], "bytes": len(png)},
            "provenance": provenance,
        }
        return [Image(data=png, format="png"), json.dumps(payload)]
    except Exception as exc:  # noqa: BLE001
        return [_error(str(exc))]


async def _render_overview(
    drawn: list[tuple[str, dict, str]],
    heading: str,
    classes: overview.MapClasses = overview.EUDR_CLASSES,
) -> tuple[bytes, tuple[float, float, float, float]]:
    """Draw assessed plots on one map over the 2020 forest background.

    Shared by eudr_overview_map (plots coloured by EUDR risk) and
    supply_chain_resilience (plots coloured by supply status).

    Args:
        drawn: (label, geometry, class key) per plot, as built by _assess_collection.
        heading: Map title.
        classes: Colours and legend of the class keys.

    Returns:
        (png, extent): the compact PNG and the (west, south, east, north) it covers.

    Raises:
        ValueError: when no plot could be assessed or the plots span too wide an area.
    """
    if not drawn:
        raise ValueError("no plot could be assessed; nothing to draw")
    bounds = overview.map_bounds([geom for _, geom, _ in drawn])
    if bounds[2] - bounds[0] > MAX_OVERVIEW_SPAN_DEG or bounds[3] - bounds[1] > MAX_OVERVIEW_SPAN_DEG:
        raise ValueError(f"plots span more than {MAX_OVERVIEW_SPAN_DEG:g}°; split them into regional maps")
    # extent is the pixel-snapped window, slightly larger than bounds; use it to place the image.
    fraction, extent = await asyncio.to_thread(sources.forest_background, bounds)
    footer = (
        "Forest 31 Dec 2020: JRC GFC2020 V4 (10 m) · loss: Hansen GFC v1.12 (to 2024) · "
        "risk screening, not a legal determination"
    )
    png = await asyncio.to_thread(overview.overview_png, drawn, fraction, extent, heading, footer, classes)
    return png, extent


# Image + text content; no structured (JSON-schema) output for this tool.
@mcp.tool(structured_output=False)
async def supply_chain_resilience(plots: dict | str, drought: dict | str | None = None) -> list:
    """
    Build the supply-chain resilience matrix: EUDR risk x drought risk per plot.

    Use this as the last step of a supplier audit, after getting each plot's
    drought risk (e.g. drought_index of the climate_risk server). Each plot is
    screened exactly like eudr_check_plot, then combined with its drought risk
    into a supply status: blocked (EUDR high), review (EUDR medium), at risk
    (EUDR low but drought medium or high) or secure, with a recommendation.
    Also draws a decision map with every plot coloured by its supply status.
    Show the map and copy matrix_markdown into the answer exactly as returned.

    Args:
        plots:   GeoJSON FeatureCollection (object or string) with up to 25
                 features, as for eudr_check_plots; properties.id names each plot.
        drought: Required. Object mapping each plot id to its drought risk:
                 either the level ("low", "medium", "high") or the full
                 drought_index result for that plot (its top-level risk_level
                 is used). Plots left out show drought risk "unknown" and
                 supply status "review".

    Returns:
        The decision map (PNG: red = blocked, yellow = review, orange = at risk,
        green = secure) followed by JSON with matrix_markdown (the table to
        show), one row per plot (EUDR risk, deforestation after the cut-off,
        drought risk, supply status, recommendation), a summary (count per
        supply status, plots missing a drought result) and a provenance block.
    """
    try:
        # Parse the drought argument first: a malformed one fails fast, before the slow raster reads.
        drought_by_id = resilience.parse_drought(drought)
        resilience.require_drought(drought_by_id)
        results, eudr_summary, provenance, drawn = await _assess_collection(plots)
        rows = resilience.build_rows(results, drought_by_id)
        status_by_id = {row["id"]: row["supply_status"] for row in rows}
        summary = resilience.summarise(rows)
        counts = summary["by_supply_status"]
        heading = (
            f"Supply status: {counts[resilience.STATUS_BLOCKED]} blocked, "
            f"{counts[resilience.STATUS_REVIEW]} review, {counts[resilience.STATUS_AT_RISK]} at risk, "
            f"{counts[resilience.STATUS_SECURE]} secure"
        )
        png, extent = await _render_overview(
            [(ref, geom, status_by_id[ref]) for ref, geom, _ in drawn],
            heading,
            overview.MapClasses(colours=resilience.STATUS_COLOURS, legend=resilience.STATUS_LEGEND),
        )
        # Drought ids that match no plot usually mean a typo; report them so the agent can fix it.
        unmatched = sorted(set(drought_by_id) - {row["id"] for row in rows})
        payload = {
            "matrix_markdown": resilience.markdown_table(rows),
            "rows": rows,
            "summary": {**summary, "eudr": eudr_summary},
            "map": {"bounds": [round(v, 5) for v in extent], "bytes": len(png)},
            "provenance": {
                **provenance,
                "drought_risk_from": "passed in by the caller (e.g. climate_risk drought_index, ERA5)",
                "unmatched_drought_ids": unmatched,
            },
        }
        return [Image(data=png, format="png"), json.dumps(payload)]
    except Exception as exc:  # noqa: BLE001
        return [_error(str(exc))]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Forest Compliance MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
