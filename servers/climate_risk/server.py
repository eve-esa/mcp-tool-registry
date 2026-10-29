"""
Climate Risk — MCP Server
=========================
Climate anomalies and drought indices for any point on Earth, computed from
the ERA5 reanalysis (ECMWF / Copernicus Climate Change Service) through the
Open-Meteo Historical Weather API. No API key required.

Tools:
    climate_anomaly — temperature and precipitation of a period compared with
                      the same calendar window in a 30-year baseline
    drought_index   — SPI and SPEI (1–24 months) plus recent hot days, with a
                      normalized risk level
    drought_chart   — PNG chart of monthly SPEI/SPI over the last months

Every response carries a ``provenance`` block (dataset, model, grid cell,
request URL, retrieval time, method) so an agent can cite its sources.

Module layout (this file is the MCP / I/O layer only):
    server.py     MCP tool definitions, input validation, the Open-Meteo HTTP
                  download and JSON/Image formatting of the results.
    era5_store.py on-disk cache per ERA5 grid cell and year (saves API budget).
    climate.py    pure statistics: anomalies, hot days, SPI/SPEI, McKee classes.
    charts.py     matplotlib rendering of the drought chart.

Tools never raise: any failure is returned as ``{"error": "..."}`` JSON so the
calling agent can read the reason and retry with corrected arguments.

Configuration (environment or ``.env`` next to this file):
    OPEN_METEO_API_KEY      optional paid-plan key (uses the customer endpoint)
    CLIMATE_RISK_CACHE_DIR  cache folder (default: <system temp>/climate_risk_cache)

Usage:
    python server.py                               # HTTP transport (default)
    python server.py --transport stdio             # stdio transport
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import charts
import climate
import httpx
from dotenv import load_dotenv
from era5_store import ERA5Store, snap_to_cell
from mcp.server.fastmcp import FastMCP, Image

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("climate-risk-mcp")

SERVER_NAME = "Climate Risk"
SERVER_VERSION = "0.1.0"

PUBLIC_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CUSTOMER_ARCHIVE_URL = "https://customer-archive-api.open-meteo.com/v1/archive"
# Optional paid-plan key. Without it the free, non-commercial tier is used.
OPEN_METEO_API_KEY = os.getenv("OPEN_METEO_API_KEY", "").strip()
# Default is the system temp dir: it survives server restarts, and if the OS
# clears it the only cost is a re-download.
CACHE_DIR = Path(os.getenv("CLIMATE_RISK_CACHE_DIR") or Path(tempfile.gettempdir()) / "climate_risk_cache")
# Open-Meteo "models" value: pure ERA5 (0.25°), not the "best match" blend of
# reanalyses, so results are reproducible and citable as ERA5.
ERA5_MODEL = "era5"
TIMEOUT = 120  # seconds per HTTP request; a cold multi-decade download is slow
# Open-Meteo also has a per-minute quota; waiting one full minute plus a second
# of margin guarantees the retry falls in a new minute window.
RATE_LIMIT_WAIT_S = 61
DEFAULT_BASELINE = (1991, 2020)  # WMO climatological standard normal
MIN_BASELINE_YEARS = 10  # below this, ranks and percentiles are too coarse to mean much
FIRST_ERA5_YEAR = 1940  # start of the ERA5 archive on Open-Meteo

# stateless_http: each HTTP request is independent (no MCP session state),
# so the server can sit behind a load balancer or be restarted freely.
mcp = FastMCP(SERVER_NAME, host="0.0.0.0", port=8000, stateless_http=True)


# ─── Data access ─────────────────────────────────────────────────────────────


def _params(lat: float, lon: float, start: date, end: date) -> dict[str, str]:
    """Build the Open-Meteo archive query for one location and date range.

    Shared by the real download and the public ``request_url`` in the
    provenance, so the cited URL is exactly what was requested. Days are
    aggregated in UTC so values do not depend on the location's time zone.
    """
    return {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(climate.VARIABLES),
        "models": ERA5_MODEL,
        "timezone": "UTC",
    }


async def _download(lat: float, lon: float, start: date, end: date) -> tuple[dict[str, list], dict[str, Any]]:
    """Download daily ERA5 data with one Open-Meteo archive request.

    This is the ``Fetcher`` injected into ``ERA5Store``. If the per-minute
    limit is hit it waits ``RATE_LIMIT_WAIT_S`` and retries once; the daily
    limit is not retried because waiting would not help.

    Returns:
        ``(daily, grid_cell)``: Open-Meteo's ``daily`` block
        (``{"time": [...], "<variable>": [...]}``) and the coordinates and
        elevation of the grid cell Open-Meteo actually used.

    Raises:
        RuntimeError: On rate limiting (after the retry) or any non-200 status.
    """
    params = _params(lat, lon, start, end)
    url = PUBLIC_ARCHIVE_URL
    if OPEN_METEO_API_KEY:
        url, params = CUSTOMER_ARCHIVE_URL, {**params, "apikey": OPEN_METEO_API_KEY}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for attempt in (1, 2):
            started = time.monotonic()
            resp = await client.get(url, params=params)
            # Only the per-minute limit is worth waiting for (its 429 message mentions "minutely").
            if resp.status_code == 429 and "minutely" in resp.text.lower() and attempt == 1:
                logger.warning("Open-Meteo per-minute limit hit; retrying in %ss", RATE_LIMIT_WAIT_S)
                await asyncio.sleep(RATE_LIMIT_WAIT_S)
                continue
            break
    if resp.status_code == 429:
        raise RuntimeError(
            "Open-Meteo rate limit reached (free tier: 10,000 weighted calls/day). "
            f"Try again later or set OPEN_METEO_API_KEY. Detail: {resp.text[:200]}"
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Open-Meteo returned {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    logger.info("Downloaded %s..%s for cell (%.2f, %.2f) in %.1fs", start, end, lat, lon, time.monotonic() - started)
    grid_cell = {
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "elevation_m": payload.get("elevation"),
    }
    return payload["daily"], grid_cell


def _utc_today() -> date:
    """Return today's date in UTC.

    Open-Meteo rejects dates after the current UTC day, whatever the local
    time zone; using the local date could ask for "tomorrow" in the evening
    east of Greenwich (or early morning in the Americas the other way round).
    """
    return datetime.now(UTC).date()


# One shared cache for all tools, so climate_anomaly and drought_index on the
# same location reuse the same downloaded years.
_store = ERA5Store(CACHE_DIR, _download, today=_utc_today)


async def _load(lat: float, lon: float, first_year: int) -> tuple[climate.Series, dict[str, Any]]:
    """Return the ERA5 daily series for the location's grid cell plus provenance metadata.

    Adds to the store's metadata a public, reproducible request URL for the
    whole range and the ERA5 cell centre used as cache key.
    """
    series, meta = await _store.series(lat, lon, first_year)
    cell = snap_to_cell(lat, lon)
    # Reproducible public URL for the whole range; never includes the API key.
    query = urlencode(_params(*cell, date(first_year, 1, 1), _utc_today()))
    return series, {"request_url": f"{PUBLIC_ARCHIVE_URL}?{query}", "era5_cell_centre": list(cell), **meta}


def _baseline(start_year: int, end_year: int) -> range:
    """Validate the baseline years and return them as an inclusive ``range``.

    Raises:
        ValueError: If the range starts before ERA5, is reversed, or is
            shorter than ``MIN_BASELINE_YEARS``.
    """
    if start_year < FIRST_ERA5_YEAR or end_year < start_year:
        raise ValueError(f"invalid baseline {start_year}-{end_year} (ERA5 starts in {FIRST_ERA5_YEAR})")
    if end_year - start_year + 1 < MIN_BASELINE_YEARS:
        raise ValueError(f"baseline must span at least {MIN_BASELINE_YEARS} years")
    return range(start_year, end_year + 1)


def _first_year(baseline: range, earliest_year: int | None = None) -> int:
    """Return the first year to load for a request.

    Both tools ask for the same range by default so they share the cache for
    a location. It starts two years before the baseline so 24-month
    accumulations (the longest SPI/SPEI scale) are complete for the first
    baseline year.

    Args:
        baseline: Baseline years from ``_baseline``.
        earliest_year: Extra year that must be covered too (e.g. a period
            before the baseline in climate_anomaly, or the chart's first month).
    """
    year = baseline.start - 2
    if earliest_year is not None:
        year = min(year, earliest_year)
    return max(year, FIRST_ERA5_YEAR)


def _provenance(meta: dict, lat: float, lon: float, how: str, baseline: range, method: str) -> dict:
    """Build the ``provenance`` block attached to every tool response.

    It lets an agent cite the dataset, licence, exact grid cell, request URL
    and method, and tells it about ERA5's publication delay.

    Args:
        meta: Metadata from ``_load`` (grid cell, request URL, cache info).
        lat: Requested latitude, before snapping to the ERA5 grid.
        lon: Requested longitude, before snapping to the ERA5 grid.
        how: "point", "geometry centroid" or "bbox centre", from ``resolve_location``.
        baseline: Baseline years used.
        method: One-sentence description of the computation, per tool.
    """
    return {
        "dataset": "ERA5 reanalysis (ECMWF, Copernicus Climate Change Service), daily aggregates",
        "access": "Open-Meteo Historical Weather API",
        "model": ERA5_MODEL,
        "requested_location": {"latitude": round(lat, 5), "longitude": round(lon, 5), "from": how},
        **meta,
        "baseline_years": f"{baseline.start}-{baseline.stop - 1}",
        "method": method,
        "data_latency": "ERA5 is published with about 5-7 days of delay",
        "licence": "ERA5: Copernicus licence; Open-Meteo: CC BY 4.0 (attribution required)",
        "server": f"{SERVER_NAME} MCP server v{SERVER_VERSION}",
    }


def _error(message: str) -> str:
    """Log a tool failure and return it as ``{"error": message}`` JSON for the agent."""
    logger.warning("Tool error: %s", message)
    return json.dumps({"error": message})


# ─── Tools ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def climate_anomaly(
    start_date: str,
    end_date: str,
    latitude: float | None = None,
    longitude: float | None = None,
    geometry: dict | str | None = None,
    baseline_start_year: int = DEFAULT_BASELINE[0],
    baseline_end_year: int = DEFAULT_BASELINE[1],
) -> str:
    """
    Compare temperature and precipitation of a period with the climate normal.

    Use this to answer questions such as "was last week unusually hot in
    Madrid?" or "how much rain fell this season compared with normal?". The
    period is compared with the same calendar window in every baseline year
    (default 1991-2020, the WMO standard normal), using ERA5 reanalysis data.

    Args:
        start_date:  First day of the period, YYYY-MM-DD.
        end_date:    Last day of the period, YYYY-MM-DD (at most 366 days after
                     start_date; ERA5 lags real time by about a week).
        latitude:    Latitude in decimal degrees (WGS84). Use with longitude.
        longitude:   Longitude in decimal degrees (WGS84).
        geometry:    Alternatively, a GeoJSON Point/Polygon/Feature (object or string), or a
                     "west,south,east,north" bbox as geocode_place returns it;
                     its centre is used.
        baseline_start_year: First year of the reference climate (default 1991).
        baseline_end_year:   Last year of the reference climate (default 2020).

    Returns:
        JSON with, for mean temperature (°C), mean daily maximum temperature (°C)
        and total precipitation (mm): the period value, the baseline mean,
        the anomaly, a z-score, the percentile within the baseline and, for
        precipitation, the percent of normal. Includes a provenance block.
    """
    try:
        lat, lon, how = climate.resolve_location(latitude, longitude, geometry)
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start:
            raise ValueError("end_date is before start_date")
        if (end - start).days > 366:
            raise ValueError("period longer than 366 days")
        baseline = _baseline(baseline_start_year, baseline_end_year)

        # start.year may predate the baseline (e.g. a 1980s period); load it too.
        series, meta = await _load(lat, lon, _first_year(baseline, start.year))
        latest = climate.last_complete_day(series)
        if end > latest:
            raise ValueError(f"ERA5 data is available until {latest}; choose an earlier end_date")

        result = {
            "location": {"latitude": lat, "longitude": lon},
            "period": {"start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days + 1},
            "temperature_mean_c": climate.period_anomaly(
                series, "temperature_2m_mean", start, end, baseline, "mean"
            ),
            "temperature_max_mean_c": climate.period_anomaly(
                series, "temperature_2m_max", start, end, baseline, "mean"
            ),
            "precipitation_total_mm": climate.period_anomaly(
                series, "precipitation_sum", start, end, baseline, "sum"
            ),
            "provenance": _provenance(
                meta, lat, lon, how, baseline,
                "Period mean (temperature) or sum (precipitation) compared with the same "
                "calendar window in each baseline year; percentile is the empirical rank.",
            ),
        }
        return json.dumps(result)
    except Exception as exc:  # noqa: BLE001 - every failure goes back to the agent as JSON
        return _error(str(exc))


@mcp.tool()
async def drought_index(
    latitude: float | None = None,
    longitude: float | None = None,
    geometry: dict | str | None = None,
    end_month: str | None = None,
    scales: list[int] | None = None,
    baseline_start_year: int = DEFAULT_BASELINE[0],
    baseline_end_year: int = DEFAULT_BASELINE[1],
) -> str:
    """
    Drought indices (SPI and SPEI) and recent heat stress for a location.

    Use this to assess agricultural or hydrological drought risk for a farm,
    a plot or a city, for example before sourcing crops from it. SPI uses
    precipitation only; SPEI uses precipitation minus reference
    evapotranspiration (FAO-56), so it also captures heat-driven drying.
    Negative values are drier than normal: -1 to -1.5 moderately dry,
    -1.5 to -2 severely dry, below -2 extremely dry.

    Args:
        latitude:  Latitude in decimal degrees (WGS84). Use with longitude.
        longitude: Longitude in decimal degrees (WGS84).
        geometry:  Alternatively, a GeoJSON Point/Polygon/Feature (object or string), or a
                   "west,south,east,north" bbox as geocode_place returns it;
                   its centre is used.
        end_month: Last month of the accumulation window, YYYY-MM. Defaults to
                   the latest complete month available in ERA5; a later month
                   is moved back to it (see "note" in the result).
        scales:    Accumulation windows in months, each 1-24 (default [3, 6]).
                   3 months reflects soil moisture / crops, 6-12 months
                   reservoirs and groundwater.
        baseline_start_year: First year of the reference climate (default 1991).
        baseline_end_year:   Last year of the reference climate (default 2020).

    Returns:
        JSON with SPI and SPEI per scale (value, index, McKee class, risk
        level), hot days in the last 90 days compared with what is normal
        there, an overall risk_level ("low", "medium" or "high") and a
        provenance block.
    """
    try:
        lat, lon, how = climate.resolve_location(latitude, longitude, geometry)
        scales = sorted(set(scales or [3, 6]))
        if any(s < 1 or s > 24 for s in scales):
            raise ValueError("each scale must be between 1 and 24 months")
        baseline = _baseline(baseline_start_year, baseline_end_year)

        series, meta = await _load(lat, lon, _first_year(baseline))

        precip = climate.monthly_totals(series, "precipitation_sum")  # SPI input
        balance = climate.water_balance(series)  # SPEI input
        year, month, note = climate.choose_end_month(end_month, climate.last_complete_month(precip))

        spi = [climate.standardized_for_month(precip, year, month, s, baseline) for s in scales]
        spei = [climate.standardized_for_month(balance, year, month, s, baseline) for s in scales]
        # ERA5 temperature can lag a day or two behind precipitation, so the heat window ends on its own last day.
        heat = climate.hot_days(series, climate.last_complete_day(series, "temperature_2m_max"), 90, baseline)

        # Overall risk is the worst signal: any one dry index or heat stress is enough to flag it.
        overall = climate.worst_risk([e["risk_level"] for e in spi + spei] + [heat["risk_level"]])
        driest = min(spi + spei, key=lambda e: e["index"])
        result = {
            "location": {"latitude": lat, "longitude": lon},
            "end_month": f"{year}-{month:02d}",
            "spi": spi,
            "spei": spei,
            "hot_days_last_90": heat,
            "risk_level": overall,
            **({"note": note} if note else {}),
            "summary": (
                f"Overall {overall} risk. Driest signal: "
                f"{'SPEI' if driest in spei else 'SPI'}-{driest['scale_months']} = {driest['index']} "
                f"({driest['class']}); {heat['hot_days']} hot days in the last 90 "
                f"(normal ≈ {heat['expected_hot_days']})."
            ),
            "provenance": _provenance(
                meta, lat, lon, how, baseline,
                "Empirical (non-parametric) SPI/SPEI: the accumulated value is ranked against "
                "the same months in each baseline year (Gringorten plotting position) and "
                "mapped to a standard normal. Classes after McKee et al. (1993).",
            ),
        }
        return json.dumps(result)
    except Exception as exc:  # noqa: BLE001 - every failure goes back to the agent as JSON
        return _error(str(exc))


# Image + text content; no structured (JSON-schema) output for this tool.
@mcp.tool(structured_output=False)
async def drought_chart(
    latitude: float | None = None,
    longitude: float | None = None,
    geometry: dict | str | None = None,
    label: str | None = None,
    scale: int = 3,
    months: int = 36,
    baseline_start_year: int = DEFAULT_BASELINE[0],
    baseline_end_year: int = DEFAULT_BASELINE[1],
) -> list:
    """
    Draw a chart of drought conditions over the last months for a location.

    Use this when the user wants to see how drought developed at a farm or
    city, typically after drought_index. Bars are the monthly SPEI (water
    balance) coloured by drought class (orange/brown = dry, green = wet), the
    dark line is the SPI (precipitation only), and dashed lines mark the
    moderately (-1), severely (-1.5) and extremely (-2) dry thresholds.

    Args:
        latitude:  Latitude in decimal degrees (WGS84). Use with longitude.
        longitude: Longitude in decimal degrees (WGS84).
        geometry:  Alternatively, a GeoJSON Point/Polygon/Feature (object or string), or a
                   "west,south,east,north" bbox as geocode_place returns it;
                   its centre is used.
        label:     Optional name shown in the chart title, e.g. a farm id.
        scale:     Accumulation window in months, 1-24 (default 3, crops).
        months:    How many months to show, 12-120 (default 36).
        baseline_start_year: First year of the reference climate (default 1991).
        baseline_end_year:   Last year of the reference climate (default 2020).

    Returns:
        The PNG chart followed by a JSON summary (latest SPEI/SPI, months in
        drought, driest month) with a provenance block.
    """
    try:
        lat, lon, how = climate.resolve_location(latitude, longitude, geometry)
        if not 1 <= scale <= 24:
            raise ValueError("scale must be between 1 and 24 months")
        if not 12 <= months <= 120:
            raise ValueError("months must be between 12 and 120")
        baseline = _baseline(baseline_start_year, baseline_end_year)
        # The first plotted month needs `scale` earlier months; one spare year absorbs rounding.
        first_year = _first_year(baseline, _utc_today().year - (months + scale) // 12 - 1)
        series, meta = await _load(lat, lon, first_year)

        precip = climate.monthly_totals(series, "precipitation_sum")
        balance = climate.water_balance(series)
        year, month = climate.last_complete_month(precip)
        spei = climate.index_series(balance, year, month, months, scale, baseline)
        spi = climate.index_series(precip, year, month, months, scale, baseline)
        if not spei:
            raise ValueError("not enough data to draw the chart")

        place = label or f"{lat:.3f}, {lon:.3f}"
        latest = spei[-1]
        title = f"Drought at {place}: SPEI-{scale} {latest['index']:+.2f} ({latest['class']}) in {latest['month']}"
        footer = (
            f"ERA5 (ECMWF/C3S) via Open-Meteo · ERA5 cell {meta['era5_cell_centre'][0]:.2f}, "
            f"{meta['era5_cell_centre'][1]:.2f} · baseline {baseline.start}-{baseline.stop - 1} · "
            "empirical SPI/SPEI (McKee classes)"
        )
        # matplotlib is blocking and CPU-bound; a worker thread keeps the event loop responsive.
        png = await asyncio.to_thread(charts.drought_chart_png, spei, spi, scale, title, footer)

        driest = min(spei, key=lambda e: e["index"])
        summary = {
            "location": {"latitude": lat, "longitude": lon},
            "label": label,
            "scale_months": scale,
            "period": f"{spei[0]['month']} to {spei[-1]['month']}",
            "latest": {"month": latest["month"], "spei": latest["index"], "class": latest["class"],
                       "spi": spi[-1]["index"] if spi else None},
            "months_moderately_dry_or_worse": sum(1 for e in spei if e["index"] < -1.0),
            "driest_month": driest,
            "image": {"format": "png", "bytes": len(png)},
            "provenance": _provenance(
                meta, lat, lon, how, baseline,
                "Monthly empirical SPEI (precipitation minus FAO-56 ET0) and SPI ranked against "
                "the same months of the baseline years; McKee et al. (1993) classes.",
            ),
        }
        return [Image(data=png, format="png"), json.dumps(summary)]
    except Exception as exc:  # noqa: BLE001 - every failure goes back to the agent as JSON
        return [_error(str(exc))]  # a list too, to match the success shape


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Climate Risk MCP Server")
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

