"""
Satellite Passes — MCP Server
=============================
When will a free Earth-observation satellite next image an area, how much of the
area will each image cover, and how cloudy will it be?

Reads the official acquisition schedules that are otherwise only published as
Google Earth files or web tools:

- Sentinel-2A/2B/2C (optical) and Sentinel-1C/1D (radar): ESA acquisition plans (KML)
  https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-2/acquisition-plans
- Landsat 8/9 (optical): USGS 16-day acquisition calendar and WRS-2 footprints
  https://landsat.usgs.gov/landsat_acq

For every pass it reports the share of the area the photo covers and, for optical
passes in the next 7 days, the Open-Meteo cloud forecast at that hour.

Tools:
    find_next_satellite_passes — upcoming photos of an area, with coverage and clouds

Usage:
    python server.py                               # HTTP transport on port 8000
    python server.py --transport stdio             # stdio transport for MCP clients
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("satellite-passes-mcp")

mcp = FastMCP("Satellite Passes", host="0.0.0.0", port=8000, stateless_http=True)

TIMEOUT = 60
CACHE_SECONDS = 3 * 3600
MAX_DAYS = 21
CLOUD_FORECAST_DAYS = 7
MAX_PASSES = 40
GRID = 20  # coverage is estimated on a GRID x GRID sample of points inside the area
USER_AGENT = "satellite-passes-mcp/0.1 (agentic-eo-hackathon)"

ESA_SITE = "https://sentinels.copernicus.eu"
ESA_PLAN_PAGES = {
    "sentinel-2": f"{ESA_SITE}/web/sentinel/copernicus/sentinel-2/acquisition-plans",
    "sentinel-1": f"{ESA_SITE}/web/sentinel/copernicus/sentinel-1/acquisition-plans",
}
# Plan file links, e.g. .../s2a_mp_acq__kml_20261015t150000_20261102t180000 or .../s1c_mp_user_..._...
ESA_LINK_RE = re.compile(r'href="([^"]*?/(s[12][a-d])_mp_(?:acq__kml|user)_(\d{8}t\d{6})_(\d{8}t\d{6})[^"]*)"')
# Sentinel-2 calibration datatakes (dark signal) produce no image.
SKIPPED_MODES = ("DARK",)

LANDSAT_CYCLES_URL = "https://landsat.usgs.gov/sites/default/files/landsat_acq/assets/json/cycles_full.json"
LANDSAT_WRS_URL = "https://nimbus.cr.usgs.gov/arcgis/rest/services/LLook_Outlines/MapServer/1/query"
LANDSAT_CYCLE_DAYS = 16

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

SATELLITE_GROUPS = ("sentinel-2", "sentinel-1", "landsat")

_cache: dict[str, tuple[float, Any]] = {}


# ---------------------------------------------------------------------------
# HTTP and cache helpers
# ---------------------------------------------------------------------------


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> httpx.Response:
    """GET a URL and raise on HTTP errors."""
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp


async def _cached(key: str, loader) -> Any:
    """Return a cached value, or load and cache it for CACHE_SECONDS."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = await loader()
    _cache[key] = (time.time(), value)
    return value


def _parse_ts(stamp: str) -> datetime:
    """Parse ESA timestamps such as '20261015t150000' or '2026-10-15T15:41:47.686'."""
    stamp = stamp.strip()
    if re.fullmatch(r"\d{8}t\d{6}", stamp, re.IGNORECASE):
        return datetime.strptime(stamp.lower(), "%Y%m%dt%H%M%S").replace(tzinfo=UTC)
    return datetime.fromisoformat(stamp[:19]).replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    """Format a UTC time as e.g. 2026-10-24T10:16Z."""
    return moment.strftime("%Y-%m-%dT%H:%MZ")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    """Parse "west,south,east,north" and check it is a valid, not too large area."""
    parts = [p.strip() for p in str(bbox).replace(";", ",").split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must have four numbers: west,south,east,north")
    west, south, east, north = (float(p) for p in parts)
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("bbox values are out of range (longitude -180..180, latitude -90..90)")
    if east < west or north < south:
        raise ValueError("bbox must be west,south,east,north with east >= west and north >= south")
    if east - west > 20 or north - south > 20:
        raise ValueError("area is larger than 20 x 20 degrees; use a smaller area")
    return west, south, east, north


def _sample_points(west: float, south: float, east: float, north: float) -> list[tuple[float, float]]:
    """A GRID x GRID sample of points inside the area (the centre for a tiny area)."""
    if east - west < 1e-3 and north - south < 1e-3:
        return [((west + east) / 2, (south + north) / 2)]
    return [
        (west + (east - west) * (i + 0.5) / GRID, south + (north - south) * (j + 0.5) / GRID)
        for i in range(GRID)
        for j in range(GRID)
    ]


def _inside(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test on a lon/lat ring."""
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def _coverage(points: list[tuple[float, float]], rings: list[list[tuple[float, float]]]) -> float:
    """Share (0-100) of the sample points that fall inside any of the rings."""
    boxes = [(min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r)) for r in rings]
    inside = 0
    for x, y in points:
        for ring, (w, s, e, n) in zip(rings, boxes, strict=True):
            if w <= x <= e and s <= y <= n and _inside(x, y, ring):
                inside += 1
                break
    return 100.0 * inside / len(points)


def _size_km(west: float, south: float, east: float, north: float) -> tuple[float, float]:
    """Approximate width and height of the area in km."""
    mid = math.radians((south + north) / 2)
    return (east - west) * 111.32 * math.cos(mid), (north - south) * 110.57


# ---------------------------------------------------------------------------
# ESA acquisition plans (Sentinel-1, Sentinel-2)
# ---------------------------------------------------------------------------


async def _esa_plan_links(client: httpx.AsyncClient, group: str) -> dict[str, list[dict]]:
    """Plan files listed on ESA's page, per satellite, newest first."""

    async def load():
        html = (await _get(client, ESA_PLAN_PAGES[group])).text
        files: dict[str, dict] = {}
        for href, sat, start, end in ESA_LINK_RE.findall(html):
            url = href if href.startswith("http") else ESA_SITE + href
            files.setdefault(url, {"url": url, "sat": sat.upper(), "start": _parse_ts(start), "end": _parse_ts(end)})
        by_sat: dict[str, list[dict]] = {}
        for f in files.values():
            by_sat.setdefault(f["sat"], []).append(f)
        for lst in by_sat.values():
            lst.sort(key=lambda f: f["start"], reverse=True)
        return by_sat

    return await _cached(f"links:{group}", load)


def _parse_plan(kml: str, sat: str) -> list[dict]:
    """Datatakes in an ESA plan file: begin/end time, mode, extra fields and footprint rings."""
    takes = []
    for block in re.findall(r"<Placemark>.*?</Placemark>", kml, re.DOTALL):
        begin = re.search(r"<begin>([^<]+)</begin>", block)
        end = re.search(r"<end>([^<]+)</end>", block)
        if not begin or not end:
            continue
        data = dict(re.findall(r'<Data name="(\w+)">\s*<value>([^<]*)</value>', block))
        mode = data.get("Mode", "")
        if mode.upper().startswith(SKIPPED_MODES):
            continue
        rings = []
        for coords in re.findall(r"<coordinates>(.*?)</coordinates>", block, re.DOTALL):
            ring = [tuple(map(float, p.split(",")[:2])) for p in coords.split()]
            if len(ring) >= 3:
                rings.append(ring)
        if rings:
            takes.append({
                "sat": sat,
                "begin": _parse_ts(begin.group(1)),
                "end": _parse_ts(end.group(1)),
                "mode": mode,
                "polarisation": data.get("Polarisation"),
                "relative_orbit": data.get("OrbitRelative"),
                "rings": rings,
            })
    return takes


async def _load_plan(client: httpx.AsyncClient, url: str, sat: str) -> list[dict]:
    """Download and parse one ESA plan file (cached); fail if ESA returns an error page instead of KML."""
    async def load():
        resp = await _get(client, url)
        if "kml" not in resp.headers.get("content-type", "") and "<kml" not in resp.text[:2000]:
            raise ValueError("not a KML plan file")
        return _parse_plan(resp.text, sat)

    return await _cached(f"plan:{url}", load)


async def _esa_satellite_takes(client: httpx.AsyncClient, files: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """Datatakes for one satellite from its newest working plan file (plus the previous one if needed).

    Returns the datatakes and a schedule record (planned_until, status, note).
    """
    candidates = [f for f in files if f["end"] > now]
    sat = files[0]["sat"] if files else "?"
    takes: list[dict] = []
    used: list[dict] = []
    skipped = 0
    for f in candidates:
        try:
            plan = await _load_plan(client, f["url"], sat)
        except Exception as exc:  # broken link, HTML error page, timeout; reported in the tool's warnings
            logger.info("Plan %s unavailable: %s", f["url"], exc)
            skipped += 1
            continue
        # A newer plan replaces an older one from its own start time onwards.
        cutoff = used[-1]["start"] if used else None
        takes.extend(t for t in plan if cutoff is None or t["begin"] < cutoff)
        used.append(f)
        if f["start"] <= now:
            break
    if not candidates:  # no current plan published at all (e.g. a retired satellite)
        return [], {}
    record = {"satellite": _sat_name(sat), "source": "ESA acquisition plan"}
    if used:
        record["planned_until"] = _iso(max(f["end"] for f in used))
        record["status"] = "ok"
        if skipped:
            record["status"] = "partial"
            record["note"] = (f"ESA's newest plan file is a broken link on ESA's website, so passes after "
                              f"{record['planned_until']} are missing for this satellite")
    else:
        record["status"] = "unavailable"
        record["note"] = "no current plan file could be downloaded from ESA"
    return takes, record


def _sat_name(code: str) -> str:
    """S2A -> Sentinel-2A, S1C -> Sentinel-1C."""
    return {"S1": "Sentinel-1", "S2": "Sentinel-2"}.get(code[:2], code) + code[2:].upper()


def _time_over_area(take: dict, lat: float) -> datetime:
    """Interpolate when the satellite is over the area's latitude along the datatake.

    The first vertex of ESA's footprint ring is on the side where the datatake starts.
    """
    lats = [p[1] for ring in take["rings"] for p in ring]
    lat_start = take["rings"][0][0][1]
    lat_end = min(lats) if abs(lat_start - max(lats)) < abs(lat_start - min(lats)) else max(lats)
    span = lat_end - lat_start
    frac = 0.0 if span == 0 else min(max((lat - lat_start) / span, 0.0), 1.0)
    return take["begin"] + (take["end"] - take["begin"]) * frac


async def _esa_passes(client, group, points, center_lat, now, until, warnings) -> tuple[list[dict], list[dict]]:
    """Sentinel-1 or Sentinel-2 passes over the area between now and until, with each satellite's schedule."""
    try:
        links = await _esa_plan_links(client, group)
    except Exception as exc:
        warnings.append(f"Could not read ESA's {group} plan page: {exc}")
        return [], [{"satellite": group, "source": "ESA acquisition plan", "status": "unavailable"}]
    results = await asyncio.gather(*(_esa_satellite_takes(client, files, now) for files in links.values()))
    passes, schedules = [], []
    for takes, record in results:
        if not record:
            continue
        schedules.append(record)
        ends_early = "planned_until" not in record or \
            datetime.strptime(record["planned_until"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC) < until
        if record["status"] != "ok" and "note" in record and ends_early:
            warnings.append(f"{record['satellite']}: {record['note']}.")
        for t in takes:
            if t["end"] < now or t["begin"] > until:
                continue
            cover = _coverage(points, t["rings"])
            if cover <= 0:
                continue
            moment = _time_over_area(t, center_lat)
            if not now <= moment <= until:
                continue
            p = {
                "time_utc": _iso(moment),
                "satellite": _sat_name(t["sat"]),
                "type": "radar" if group == "sentinel-1" else "optical",
                "area_covered_pct": round(cover),
                "source": "ESA acquisition plan",
            }
            if group == "sentinel-1":
                p["radar_mode"] = t["mode"]
                p["polarisation"] = t["polarisation"]
                p["relative_orbit"] = int(t["relative_orbit"]) if (t["relative_orbit"] or "").isdigit() else None
            passes.append(p)
    return passes, schedules


# ---------------------------------------------------------------------------
# Landsat 8/9 (USGS 16-day calendar)
# ---------------------------------------------------------------------------


async def _landsat_passes(client, bbox, points, now, until, warnings) -> tuple[list[dict], list[dict]]:
    """Landsat 8/9 passes: WRS-2 paths covering the area, dated with the USGS 16-day calendar."""
    west, south, east, north = bbox
    try:
        cycles = await _cached("landsat:cycles", lambda: _get_json(client, LANDSAT_CYCLES_URL))
        params = {
            "where": "MODE='D'",
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "PATH,ROW",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
        }
        key = "landsat:wrs:" + ",".join(f"{v:.2f}" for v in bbox)
        wrs = await _cached(key, lambda: _get_json(client, LANDSAT_WRS_URL, params))
    except Exception as exc:
        warnings.append(f"Could not read the USGS Landsat calendar: {exc}")
        return [], [{"satellite": "Landsat 8/9", "source": "USGS acquisition calendar", "status": "unavailable"}]

    rings_by_path: dict[int, list] = {}
    for feat in wrs.get("features", []):
        path = int(feat["attributes"]["PATH"])
        for ring in feat["geometry"]["rings"]:
            rings_by_path.setdefault(path, []).append([tuple(p) for p in ring])

    passes, schedules = [], []
    for key, label in (("landsat_8", "Landsat 8"), ("landsat_9", "Landsat 9")):
        days = cycles.get(key, {})
        for path, rings in rings_by_path.items():
            cover = _coverage(points, rings)
            if cover <= 0:
                continue
            listed = [datetime.strptime(d, "%m/%d/%Y").replace(tzinfo=UTC) for d, v in days.items()
                      if str(path) in v.get("path", "").split(",")]
            if not listed:
                continue
            day = max(listed)
            steps = max(0, math.ceil((now.replace(hour=0, minute=0) - day).days / LANDSAT_CYCLE_DAYS))
            day += timedelta(days=steps * LANDSAT_CYCLE_DAYS)
            while day <= until:
                if day.date() >= now.date():
                    passes.append({
                        "date_utc": day.strftime("%Y-%m-%d"),
                        "satellite": label,
                        "type": "optical",
                        "area_covered_pct": round(cover),
                        "wrs_path": path,
                        "source": "USGS 16-day acquisition calendar",
                    })
                day += timedelta(days=LANDSAT_CYCLE_DAYS)
        schedules.append({"satellite": label, "source": "USGS 16-day acquisition calendar", "status": "ok",
                          "note": "repeats every 16 days; time of day not published (late morning local time)"})
    return passes, schedules


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None) -> Any:
    """GET a URL and parse the JSON response."""
    return (await _get(client, url, params)).json()


# ---------------------------------------------------------------------------
# Cloud forecast (Open-Meteo)
# ---------------------------------------------------------------------------


async def _add_cloud_forecast(client, bbox, passes, now, warnings) -> None:
    """Add cloud_forecast_pct to optical passes in the next CLOUD_FORECAST_DAYS (mean of 5 points)."""
    west, south, east, north = bbox
    horizon = (now + timedelta(days=CLOUD_FORECAST_DAYS)).replace(hour=23, minute=59)
    targets = [p for p in passes if p["type"] == "optical" and "time_utc" in p
               and datetime.strptime(p["time_utc"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC) <= horizon]
    if not targets:
        return
    cx, cy = (west + east) / 2, (south + north) / 2
    qx, qy = (east - west) / 4, (north - south) / 4
    pts = [(cy, cx), (cy - qy, cx - qx), (cy - qy, cx + qx), (cy + qy, cx - qx), (cy + qy, cx + qx)]
    params = {
        "latitude": ",".join(f"{p[0]:.4f}" for p in pts),
        "longitude": ",".join(f"{p[1]:.4f}" for p in pts),
        "hourly": "cloud_cover",
        "forecast_days": CLOUD_FORECAST_DAYS + 1,
        "timezone": "UTC",
    }
    try:
        data = await _get_json(client, OPEN_METEO_URL, params)
    except Exception as exc:
        warnings.append(f"Cloud forecast unavailable (Open-Meteo): {exc}")
        return
    series = data if isinstance(data, list) else [data]
    for p in targets:
        hour = datetime.strptime(p["time_utc"], "%Y-%m-%dT%H:%MZ") + timedelta(minutes=30)
        key = hour.strftime("%Y-%m-%dT%H:00")
        values = [s["hourly"]["cloud_cover"][s["hourly"]["time"].index(key)]
                  for s in series if key in s["hourly"]["time"]]
        values = [v for v in values if v is not None]
        if values:
            p["cloud_forecast_pct"] = round(sum(values) / len(values))


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_next_satellite_passes(
    bbox: str,
    days_ahead: int | None = None,
    satellites: str = "sentinel-2,sentinel-1,landsat",
    min_coverage_pct: float = 30.0,
) -> str:
    """List the upcoming photos that free Earth-observation satellites will take of an area.

    Uses the official schedules: ESA acquisition plans for Sentinel-2 (optical) and
    Sentinel-1 (radar), and the USGS 16-day calendar for Landsat 8/9 (optical). For each
    pass it gives the time over the area (UTC), the share of the area the photo covers,
    and, for optical passes in the next 7 days, the forecast cloud cover at that hour.
    Radar images are not affected by clouds or darkness.

    Args:
        bbox: Area as "west,south,east,north" in degrees (WGS84), e.g. the bbox returned
            by geocode_place. A city, region or field; for a single point use a tiny box.
        days_ahead: How many days to look ahead, 1 to 21. Leave empty to get everything
            the published schedules contain (ESA publishes about 1 to 3 weeks ahead).
        satellites: Comma-separated groups to include: "sentinel-2", "sentinel-1",
            "landsat". Default: all three. Keep the default unless the user asks for
            specific satellites or for radar only: radar is the fallback when optical
            passes are cloudy, and Landsat adds more optical passes.
        min_coverage_pct: Hide passes that cover less than this share of the area
            (0 to 100, default 30).

    Returns:
        JSON with "highlights" (the first optical and the first radar pass covering at
        least 80% of the area, and the least cloudy such optical pass), "passes"
        (sorted by time; each with time_utc or date_utc, satellite, type optical/radar,
        area_covered_pct, cloud_forecast_pct when available),
        "schedules" (how far each satellite's published plan reaches), "warnings",
        "sources" (name and link of each service used; cite these) and "notes".
        On bad input or failure, JSON with "error" and a "hint".
    """
    try:
        box = _parse_bbox(bbox)
    except (TypeError, ValueError) as exc:
        return json.dumps({"error": f"Invalid bbox: {exc}", "hint": "Pass the bbox string from geocode_place, e.g. '13.09,52.34,13.76,52.68'."})
    if days_ahead is not None and not 1 <= days_ahead <= MAX_DAYS:
        return json.dumps({"error": f"days_ahead must be between 1 and {MAX_DAYS}.", "hint": "Leave it empty to use everything that is published."})
    if not 0 <= min_coverage_pct <= 100:
        return json.dumps({"error": "min_coverage_pct must be between 0 and 100."})
    groups = [g.strip().lower() for g in satellites.split(",") if g.strip()]
    unknown = [g for g in groups if g not in SATELLITE_GROUPS]
    if unknown or not groups:
        return json.dumps({"error": f"Unknown satellite group(s): {unknown or satellites!r}.", "hint": f"Use any of: {', '.join(SATELLITE_GROUPS)}."})

    now = datetime.now(UTC)
    until = (now + timedelta(days=days_ahead or MAX_DAYS)).replace(hour=23, minute=59, second=0, microsecond=0)
    points = _sample_points(*box)
    center_lat = (box[1] + box[3]) / 2
    warnings: list[str] = []
    logger.info("Passes for bbox=%s days_ahead=%s satellites=%s", bbox, days_ahead, groups)

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        jobs = []
        for g in groups:
            if g == "landsat":
                jobs.append(_landsat_passes(client, box, points, now, until, warnings))
            else:
                jobs.append(_esa_passes(client, g, points, center_lat, now, until, warnings))
        results = await asyncio.gather(*jobs)
        passes = [p for ps, _ in results for p in ps]
        schedules = [s for _, ss in results for s in ss]

        # Without days_ahead, stop where the published ESA plans stop.
        if days_ahead is None:
            ends = [datetime.strptime(s["planned_until"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
                    for s in schedules if s.get("planned_until")]
            if ends:
                until = min(until, max(ends))
                passes = [p for p in passes if _pass_day(p) <= until]

        passes = [p for p in passes if p["area_covered_pct"] >= min_coverage_pct]
        passes.sort(key=_sort_key)
        hidden = len(passes) - MAX_PASSES
        passes = passes[:MAX_PASSES]
        await _add_cloud_forecast(client, box, passes, now, warnings)

    if hidden > 0:
        warnings.append(f"{hidden} later passes not shown; use a smaller days_ahead or a higher min_coverage_pct.")
    ended = [f"{s['satellite']} {s['planned_until'][:10]}" for s in schedules
             if s.get("planned_until") and s["status"] == "ok"
             and datetime.strptime(s["planned_until"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC) < until]
    if ended:
        warnings.append(f"Published plans end before {_iso(until)[:10]} for: {', '.join(ended)}; "
                        "later passes of these satellites are not known yet.")

    width, height = _size_km(*box)
    result = {
        "area": {"bbox": ",".join(f"{v:.4f}" for v in box), "width_km": round(width, 1), "height_km": round(height, 1)},
        "window_utc": {"from": _iso(now), "to": _iso(until)},
        "highlights": _highlights(passes),
        "passes": passes,
        "schedules": schedules,
        "warnings": warnings,
        "sources": _sources(groups, passes),
        "notes": [
            "Times are UTC, for when the satellite is over the area.",
            (f"cloud_forecast_pct comes from the Open-Meteo weather forecast (open-meteo.com): mean cloud "
             f"cover over the area at the pass hour, for optical passes within {CLOUD_FORECAST_DAYS} days. "
             "Forecasts more than about 3 days ahead are uncertain."),
            "Radar (Sentinel-1) sees through clouds and works at night.",
            "Schedules are plans and can change.",
        ],
    }
    if not passes:
        result["hint"] = "No passes found. Try a longer days_ahead, a lower min_coverage_pct, or more satellites."
    return json.dumps(result)


FULL_COVERAGE_PCT = 80


def _highlights(passes: list[dict]) -> dict:
    """Factual picks from the pass list: no ranking of radar against optical."""

    def brief(p: dict) -> dict:
        keys = ("time_utc", "date_utc", "satellite", "type", "area_covered_pct", "cloud_forecast_pct")
        return {k: p[k] for k in keys if k in p}

    full = [p for p in passes if p["area_covered_pct"] >= FULL_COVERAGE_PCT]
    optical = [p for p in full if p["type"] == "optical"]
    radar = [p for p in full if p["type"] == "radar"]
    forecast = [p for p in optical if "cloud_forecast_pct" in p]
    out = {}
    if optical:
        out["first_optical_full_coverage"] = brief(optical[0])
    if forecast:
        out["least_cloudy_optical_full_coverage"] = brief(min(forecast, key=lambda p: p["cloud_forecast_pct"]))
    if radar:
        out["first_radar_full_coverage"] = brief(radar[0])
    if out:
        out["full_coverage_means"] = f"covers at least {FULL_COVERAGE_PCT}% of the area"
    return out


def _sources(groups: list[str], passes: list[dict]) -> list[dict]:
    """The services behind this answer, with their real links, so the agent can cite them."""
    out = [{"name": f"ESA {g.title()} acquisition plans", "url": ESA_PLAN_PAGES[g]} for g in groups if g in ESA_PLAN_PAGES]
    if "landsat" in groups:
        out.append({"name": "USGS Landsat acquisition calendar", "url": "https://landsat.usgs.gov/landsat_acq"})
    if any("cloud_forecast_pct" in p for p in passes):
        out.append({"name": "Open-Meteo weather forecast (cloud cover)", "url": "https://open-meteo.com/"})
    return out


def _sort_key(p: dict) -> str:
    """Passes with a date only (Landsat) sort at midday of that date."""
    return p.get("time_utc") or p["date_utc"] + "T12:00Z"


def _pass_day(p: dict) -> datetime:
    """Time of a pass (midnight of its date for Landsat)."""
    if "time_utc" in p:
        return datetime.strptime(p["time_utc"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
    return datetime.strptime(p["date_utc"], "%Y-%m-%d").replace(tzinfo=UTC)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Satellite Passes MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.port != mcp.settings.port:
        mcp.settings.port = args.port
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
