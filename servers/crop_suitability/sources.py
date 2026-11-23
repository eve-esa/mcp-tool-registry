"""Data clients for the crop_suitability server.

All sources are public and need no credentials:
    - Microsoft Planetary Computer STAC (ESA WorldCover, Copernicus DEM)
    - ESA WorldCereal Reference Data Module (field-surveyed crop types)
    - Open-Meteo historical weather (ERA5 / ERA5-Land reanalysis)
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import os
import time
from collections import Counter
from datetime import date, timedelta
from typing import Any

import httpx
import numpy as np

logger = logging.getLogger("crop-suitability")

USER_AGENT = os.getenv("CROP_SUITABILITY_USER_AGENT", "crop-suitability-mcp/0.1 (EVE MCP tool registry)")

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"
RDM_URL = "https://ewoc-rdm-api.iiasa.ac.at"
WORLDCEREAL_LEGEND_URL = (
    "https://artifactory.vgt.vito.be/artifactory/auxdata-public/worldcereal/legend/WorldCereal_LC_CT_legend_latest.csv"
)
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

MAX_AREA_KM2 = 400.0

# ESA WorldCover 2021 legend (product user manual v2).
WORLDCOVER_CLASSES = {
    10: "tree_cover",
    20: "shrubland",
    30: "grassland",
    40: "cropland",
    50: "built_up",
    60: "bare_sparse_vegetation",
    70: "snow_ice",
    80: "permanent_water",
    90: "herbaceous_wetland",
    95: "mangroves",
    100: "moss_lichen",
}


class ToolInputError(ValueError):
    """Bad tool arguments; the message is shown to the user."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


# ---------------------------------------------------------------------------
# Small TTL cache shared by the tools (the server process outlives requests)
# ---------------------------------------------------------------------------

_CACHE: dict[tuple, tuple[float, Any]] = {}
_CACHE_TTL_S = 3600
_CACHE_MAX = 64


def cache_get(key: tuple) -> Any | None:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    return None


def cache_put(key: tuple, value: Any) -> Any:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]))
    _CACHE[key] = (time.time(), value)
    return value


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_bbox(bbox: str | list[float]) -> tuple[float, float, float, float]:
    """Parse "west,south,east,north" (or a 4-number list) and validate it."""
    try:
        if isinstance(bbox, str):
            parts = [float(p) for p in bbox.strip().strip("[]()").split(",")]
        else:
            parts = [float(p) for p in bbox]
        west, south, east, north = parts
    except (TypeError, ValueError):
        raise ToolInputError(
            "invalid_bbox", f"Invalid bbox {bbox!r}. Use 'west,south,east,north' in degrees, e.g. '8.30,45.28,8.42,45.36'."
        ) from None
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ToolInputError(
            "invalid_bbox", f"Invalid bbox {bbox!r}: need west < east and south < north, in degrees (lon/lat order)."
        )
    area = bbox_area_km2((west, south, east, north))
    if area > MAX_AREA_KM2:
        raise ToolInputError(
            "bbox_too_large",
            f"The bbox covers {area:.0f} km²; the limit is {MAX_AREA_KM2:.0f} km² (about 20 × 20 km). "
            "Pick a smaller area around the fields of interest.",
        )
    return west, south, east, north


def parse_dates(start_date: str | None, end_date: str | None, default_years: int = 3) -> tuple[date, date]:
    """Parse YYYY-MM-DD dates; default to the last `default_years` full years."""
    today = date.today()
    try:
        end = date.fromisoformat(end_date) if end_date else today
        start = date.fromisoformat(start_date) if start_date else date(end.year - default_years, 1, 1)
    except ValueError:
        raise ToolInputError("invalid_date", "Dates must be YYYY-MM-DD, e.g. '2020-01-01'.") from None
    end = min(end, today)
    if start >= end:
        raise ToolInputError("invalid_date", f"start_date {start} must be before end_date {end}.")
    if start < date(2017, 1, 1):
        raise ToolInputError("invalid_date", "Sentinel-2 L2A data starts in 2017; use a start_date from 2017-01-01.")
    if (end - start).days > 6 * 366:
        raise ToolInputError("invalid_date", "The period is limited to 6 years; shorten the time range.")
    return start, end


def bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    west, south, east, north = bbox
    lat = math.radians((south + north) / 2)
    return (east - west) * 111.32 * math.cos(lat) * (north - south) * 110.57


def bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    west, south, east, north = bbox
    return (west + east) / 2, (south + north) / 2


def expand_bbox(bbox: tuple[float, float, float, float], km: float) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    d_lat = km / 110.57
    d_lon = km / (111.32 * math.cos(math.radians((south + north) / 2)))
    return max(west - d_lon, -180), max(south - d_lat, -90), min(east + d_lon, 180), min(north + d_lat, 90)


def utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def http_get_json(url: str, params: Any = None, timeout: float = 30.0) -> Any | None:
    """GET JSON. Returns None for "no content" answers (204, empty body, 404)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(url, params=params)
    # The WorldCereal RDM answers "no items in bbox" with a 302 to an error page.
    if resp.status_code in (204, 302, 404) or not resp.content:
        return None
    resp.raise_for_status()
    return resp.json()


def pc_sign(href: str) -> str:
    """Append a Planetary Computer SAS token for the blob's storage account and container.

    Tokens are per account/container, not per collection: the DEM, for one, lives in
    another account than its collection name suggests. Cached until near expiry.
    """
    from urllib.parse import urlparse

    url = urlparse(href)
    if not url.netloc.endswith(".blob.core.windows.net"):
        return href
    account, container = url.netloc.split(".")[0], url.path.lstrip("/").split("/")[0]
    key = ("pc_token", account, container)
    token = cache_get(key)
    if not token:
        resp = httpx.get(PC_TOKEN_URL.format(account=account, container=container), timeout=30)
        resp.raise_for_status()
        token = resp.json()["token"]
        _CACHE[key] = (time.time() - _CACHE_TTL_S + 30 * 60, token)  # keep for 30 minutes
    return href + ("&" if url.query else "?") + token


def pc_load(collection: str, bbox, bands: list[str], resolution: float, datetime: str | None = None, **kwargs):
    """Search a Planetary Computer collection and load it with odc-stac on a UTM grid."""
    import odc.stac
    import pystac_client

    odc.stac.configure_rio(cloud_defaults=True)
    catalog = pystac_client.Client.open(PC_STAC_URL)
    items = list(catalog.search(collections=[collection], bbox=list(bbox), datetime=datetime).items())
    if not items:
        return None
    lon, lat = bbox_center(bbox)
    ds = odc.stac.load(
        items,
        bands=bands,
        bbox=list(bbox),
        crs=f"EPSG:{utm_epsg(lon, lat)}",
        resolution=resolution,
        patch_url=pc_sign,
        chunks={},
        **kwargs,
    )
    return ds.compute(scheduler="threads", num_workers=16)


# ---------------------------------------------------------------------------
# ESA WorldCover (land-cover fractions)
# ---------------------------------------------------------------------------


def worldcover_fractions(bbox) -> dict[str, Any]:
    key = ("worldcover", bbox)
    if (hit := cache_get(key)) is not None:
        return hit
    ds = pc_load("esa-worldcover", bbox, ["map"], resolution=30, datetime="2021", groupby="solar_day")
    if ds is None:
        return {"available": False}
    arr = ds["map"].max("time").values
    valid = arr[arr > 0]
    counts = Counter(valid.tolist())
    total = max(len(valid), 1)
    fractions = {WORLDCOVER_CLASSES.get(k, str(k)): round(v / total, 3) for k, v in counts.most_common()}
    return cache_put(key, {"available": True, "year": 2021, "resolution_m": 30, "class_fractions": fractions})


def worldcover_cropland_mask(geobox):
    """Boolean cropland mask (WorldCover class 40) on the given odc-geo GeoBox, or None."""
    import odc.stac
    import pystac_client

    catalog = pystac_client.Client.open(PC_STAC_URL)
    bbox = geobox.geographic_extent.boundingbox
    items = list(
        catalog.search(
            collections=["esa-worldcover"], bbox=[bbox.left, bbox.bottom, bbox.right, bbox.top], datetime="2021"
        ).items()
    )
    if not items:
        return None
    ds = odc.stac.load(
        items, bands=["map"], geobox=geobox, patch_url=pc_sign, groupby="solar_day", chunks={}
    )
    arr = ds["map"].max("time").compute(scheduler="threads").values
    return arr == 40


# ---------------------------------------------------------------------------
# ESA WorldCereal reference data (crop types)
# ---------------------------------------------------------------------------


async def worldcereal_legend() -> dict[int, dict[str, str]]:
    key = ("worldcereal_legend",)
    if (hit := cache_get(key)) is not None:
        return hit
    resp = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=60, headers={"User-Agent": USER_AGENT}) as client:
                resp = await client.get(WORLDCEREAL_LEGEND_URL)
                resp.raise_for_status()
            break
        except httpx.HTTPError as exc:
            logger.warning("WorldCereal legend download failed (attempt %d): %r", attempt + 1, exc)
            resp = None
            await asyncio.sleep(2)
    if resp is None:
        return {}
    legend = {}
    for row in csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig")), delimiter=";"):
        try:
            code = int(row["ewoc_code"].replace("-", ""))
        except (KeyError, ValueError):
            continue
        legend[code] = {
            "label": row.get("label_full") or row.get("level_3") or "",
            "group": row.get("level_2") or row.get("level_1") or "",
            "land_cover": row.get("LC_label", ""),
        }
    return cache_put(key, legend)


def _feature_lonlat(feature: dict) -> tuple[float, float] | None:
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    try:
        if geom.get("type") == "Point":
            return float(coords[0]), float(coords[1])
        if geom.get("type") == "Polygon":
            ring = np.asarray(coords[0], dtype=float)
            return float(ring[:, 0].mean()), float(ring[:, 1].mean())
        if geom.get("type") == "MultiPolygon":
            ring = np.asarray(coords[0][0], dtype=float)
            return float(ring[:, 0].mean()), float(ring[:, 1].mean())
    except (TypeError, IndexError, ValueError):
        return None
    return None


async def _rdm_collection_items(collection_id: str, search_bbox) -> list[dict]:
    params = [("Bbox", v) for v in search_bbox] + [("MaxResultCount", 1000)]
    try:
        data = await http_get_json(f"{RDM_URL}/collections/{collection_id}/items", params=params, timeout=20)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("WorldCereal %s skipped: %s", collection_id, exc)
        return []
    if not data:
        return []
    for f in data.get("features", []):
        f["_collection"] = collection_id
    return data.get("features", [])


async def worldcereal_crops(bbox, search_buffer_km: float = 10.0) -> dict[str, Any]:
    """Crop types surveyed in and around the bbox, from the WorldCereal Reference Data Module."""
    key = ("worldcereal", bbox, search_buffer_km)
    if (hit := cache_get(key)) is not None:
        return hit
    search_bbox = expand_bbox(bbox, search_buffer_km)
    params = [("Bbox", round(v, 5)) for v in search_bbox]
    try:
        collections = await http_get_json(f"{RDM_URL}/collections/search", params=params, timeout=30) or []
    except (httpx.HTTPError, ValueError) as exc:
        return {"available": False, "error": f"WorldCereal reference data unavailable: {exc}"}
    collections = [c for c in collections if c.get("accessType", "Public") == "Public"]
    results = await asyncio.gather(
        *(_rdm_collection_items(c["collectionId"], [round(v, 5) for v in search_bbox]) for c in collections)
    )
    features = [f for batch in results for f in batch]
    legend = await worldcereal_legend()
    west, south, east, north = bbox
    crops: dict[int, dict[str, Any]] = {}
    for f in features:
        props = f.get("properties", {})
        code = props.get("ewoc_code")
        if code is None:
            continue
        info = legend.get(int(code), {})
        entry = crops.setdefault(
            int(code),
            {
                "ewoc_code": int(code),
                "label": info.get("label") or f"ewoc_code {int(code)}",
                "group": info.get("group", ""),
                "land_cover": info.get("land_cover", ""),
                "n_samples": 0,
                "n_inside_bbox": 0,
                "years": set(),
                "collections": set(),
            },
        )
        entry["n_samples"] += 1
        lonlat = _feature_lonlat(f)
        if lonlat and west <= lonlat[0] <= east and south <= lonlat[1] <= north:
            entry["n_inside_bbox"] += 1
        if props.get("valid_time"):
            entry["years"].add(str(props["valid_time"])[:4])
        entry["collections"].add(f["_collection"])
    ranked = sorted(crops.values(), key=lambda c: (-c["n_samples"], c["label"]))
    for c in ranked:
        c["years"] = sorted(c["years"])
        c["collections"] = sorted(c["collections"])
    # ewoc codes starting with 1 are cropland (temporary or perennial crops).
    cropland = [c for c in ranked if str(c["ewoc_code"]).startswith("1") and c["ewoc_code"] != 1000000000]
    out = {
        "available": True,
        "search_bbox": [round(v, 4) for v in search_bbox],
        "search_buffer_km": search_buffer_km,
        "n_samples": len(features),
        "n_collections_queried": len(collections),
        "crops": cropland[:10],
        "other_land_cover": [{"label": c["label"], "n_samples": c["n_samples"]} for c in ranked if c not in cropland][:5],
    }
    return cache_put(key, out)


def crop_detection(worldcereal: dict[str, Any], crop: str | None) -> dict[str, Any]:
    """Decide whether WorldCereal tells us the crop, or the user must be asked."""
    crops = worldcereal.get("crops", []) if worldcereal.get("available") else []
    # Skip generic classes such as "temporary_crops" or "cereals" (no crop level in the ewoc code).
    crops = [
        c
        for c in crops
        if (c["ewoc_code"] // 10000) % 100 != 0 and not c["label"].startswith(("ewoc_code", "unknown"))
    ]
    total = sum(c["n_samples"] for c in crops)
    candidates = [{"crop": c["label"], "n_samples": c["n_samples"], "years": c["years"]} for c in crops[:5]]
    if crop:
        seen = [c for c in crops if crop.lower() in c["label"].lower()]
        return {
            "crop_status": "user_specified",
            "crop": crop,
            "seen_in_worldcereal": bool(seen),
            "n_samples_for_crop": sum(c["n_samples"] for c in seen),
            "candidates": candidates,
            "ask_user": None,
        }
    if total == 0:
        return {
            "crop_status": "not_found",
            "crop": None,
            "candidates": [],
            "ask_user": "WorldCereal has no surveyed crop samples near this area. Which crop do you want to assess "
            "(e.g. rice, maize, wheat, soybean)?",
        }
    top = crops[0]
    share = top["n_samples"] / total
    if share >= 0.5 and top["n_samples"] >= 3:
        return {
            "crop_status": "detected",
            "crop": top["label"],
            "share_of_samples": round(share, 2),
            "candidates": candidates,
            "ask_user": f"WorldCereal suggests '{top['label']}' ({top['n_samples']} of {total} crop samples, "
            f"surveyed {', '.join(top['years'])}). Assess this crop, or another one?",
        }
    return {
        "crop_status": "ambiguous",
        "crop": None,
        "candidates": candidates,
        "ask_user": "WorldCereal finds several crops near this area: "
        + ", ".join(f"{c['crop']} ({c['n_samples']})" for c in candidates)
        + ". Which crop do you want to assess?",
    }


# ---------------------------------------------------------------------------
# Copernicus DEM (terrain)
# ---------------------------------------------------------------------------


def terrain_stats(bbox) -> dict[str, Any]:
    key = ("terrain", bbox)
    if (hit := cache_get(key)) is not None:
        return hit
    ds = pc_load("cop-dem-glo-30", bbox, ["data"], resolution=30)
    if ds is None:
        return {"available": False}
    dem = ds["data"].max("time").values.astype("float64")
    dem[dem < -500] = np.nan
    dy, dx = np.gradient(dem, 30.0)
    slope_pct = np.hypot(dx, dy) * 100
    valid = np.isfinite(dem) & np.isfinite(slope_pct)
    elev, slope = dem[valid], slope_pct[valid]
    rel = elev - np.nanpercentile(elev, 2)
    out = {
        "available": True,
        "source": "Copernicus DEM GLO-30 (Planetary Computer cop-dem-glo-30)",
        "resolution_m": 30,
        "elevation_m": {
            "min": round(float(elev.min()), 1),
            "mean": round(float(elev.mean()), 1),
            "max": round(float(elev.max()), 1),
            "range": round(float(elev.max() - elev.min()), 1),
        },
        "slope_pct": {
            "mean": round(float(slope.mean()), 2),
            "p90": round(float(np.percentile(slope, 90)), 2),
        },
        "slope_classes": {
            "flat_lt_2pct": round(float((slope < 2).mean()), 3),
            "gentle_2_to_8pct": round(float(((slope >= 2) & (slope < 8)).mean()), 3),
            "moderate_8_to_15pct": round(float(((slope >= 8) & (slope < 15)).mean()), 3),
            "steep_ge_15pct": round(float((slope >= 15).mean()), 3),
        },
        "relative_height_above_local_low_m": {
            "share_within_2m": round(float((rel <= 2).mean()), 3),
            "share_within_5m": round(float((rel <= 5).mean()), 3),
            "note": "Height above the lowest 2% of the area: low-lying ground tends to stay wetter.",
        },
    }
    return cache_put(key, out)


# ---------------------------------------------------------------------------
# Open-Meteo historical weather
# ---------------------------------------------------------------------------

DAILY_WEATHER_VARS = [
    "precipitation_sum",
    "temperature_2m_mean",
    "temperature_2m_max",
    "et0_fao_evapotranspiration",
    "soil_moisture_0_to_7cm_mean",
    "soil_moisture_7_to_28cm_mean",
]


async def weather_daily(lon: float, lat: float, start: date, end: date) -> dict[str, list]:
    key = ("weather", round(lon, 3), round(lat, 3), start, end)
    if (hit := cache_get(key)) is not None:
        return hit
    end = min(end, date.today() - timedelta(days=6))  # the archive lags a few days
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(DAILY_WEATHER_VARS),
        "timezone": "auto",
    }
    data = await http_get_json(OPEN_METEO_ARCHIVE_URL, params=params, timeout=60)
    if not data or "daily" not in data:
        raise RuntimeError(f"Open-Meteo returned no data: {data}")
    return cache_put(key, data["daily"])


# ---------------------------------------------------------------------------
# Weather aggregation per period, with anomalies vs the previous N years
# ---------------------------------------------------------------------------

_SEASON_START = {"DJF": 12, "MAM": 3, "JJA": 6, "SON": 9}


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_windows(start: date, end: date, period: str, custom_periods: list | None = None) -> list[dict]:
    """Calendar windows (season / month / year / custom) overlapping [start, end]."""
    if period == "custom":
        from sentinel2 import _parse_custom_periods

        return [
            {"period": label, "start": s, "end": e, "complete": True}
            for label, s, e in _parse_custom_periods(custom_periods or [])
        ]
    step = {"month": 1, "season": 3, "year": 12}[period]
    if period == "year":
        cursor = date(start.year, 1, 1)
    elif period == "month":
        cursor = date(start.year, start.month, 1)
    else:
        cursor = date(start.year, ((start.month % 12) // 3) * 3 or 12, 1)
        if cursor.month == 12 and start.month != 12:
            cursor = date(start.year - 1, 12, 1)
    windows = []
    while cursor <= end:
        nxt = _add_months(cursor, step)
        w_end = nxt - timedelta(days=1)
        if period == "month":
            label = f"{cursor.year}-{cursor.month:02d}"
        elif period == "year":
            label = str(cursor.year)
        else:
            season = next(k for k, v in _SEASON_START.items() if v == cursor.month)
            label = f"{cursor.year + 1 if season == 'DJF' else cursor.year}-{season}"
        windows.append(
            {"period": label, "start": max(cursor, start), "end": min(w_end, end), "complete": cursor >= start and w_end <= end}
        )
        cursor = nxt
    return windows


def _window_stats(daily: dict[str, list], start: date, end: date) -> dict[str, float] | None:
    times = daily["time"]
    lo, hi = start.isoformat(), end.isoformat()
    idx = [i for i, t in enumerate(times) if lo <= t <= hi]
    if not idx:
        return None

    def col(name):
        vals = np.array([daily[name][i] for i in idx if daily[name][i] is not None], dtype=float)
        return vals

    p, et0 = col("precipitation_sum"), col("et0_fao_evapotranspiration")
    stats = {
        "n_days": len(idx),
        "precip_mm": round(float(p.sum()), 1),
        "et0_mm": round(float(et0.sum()), 1),
        "water_balance_mm": round(float(p.sum() - et0.sum()), 1),
        "t_mean_c": round(float(col("temperature_2m_mean").mean()), 1),
        "t_max_mean_c": round(float(col("temperature_2m_max").mean()), 1),
        "hot_days_gt_35c": int((col("temperature_2m_max") > 35).sum()),
    }
    for name, out in (("soil_moisture_0_to_7cm_mean", "soil_moisture_0_7cm"), ("soil_moisture_7_to_28cm_mean", "soil_moisture_7_28cm")):
        vals = col(name)
        stats[out] = round(float(vals.mean()), 3) if vals.size else None
    return stats


def _shift_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


def aggregate_weather(daily: dict[str, list], windows: list[dict], baseline_years: int) -> list[dict]:
    rows = []
    for w in windows:
        stats = _window_stats(daily, w["start"], w["end"])
        if not stats:
            continue
        base = [
            b
            for k in range(1, baseline_years + 1)
            if (b := _window_stats(daily, _shift_years(w["start"], k), _shift_years(w["end"], k)))
        ]
        row = {"period": w["period"], "start": w["start"].isoformat(), "end": w["end"].isoformat(), "complete": w["complete"], **stats}
        if base:
            anomaly = {"baseline_years": len(base)}
            for name in ("precip_mm", "water_balance_mm", "t_mean_c", "soil_moisture_0_7cm", "soil_moisture_7_28cm"):
                vals = np.array([b[name] for b in base if b.get(name) is not None], dtype=float)
                if not vals.size or stats.get(name) is None:
                    continue
                mean = float(vals.mean())
                anomaly[f"{name}_diff"] = round(stats[name] - mean, 3)
                if len(vals) >= 3 and vals.std(ddof=1) > 1e-9:
                    anomaly[f"{name}_z"] = round((stats[name] - mean) / float(vals.std(ddof=1)), 2)
            if base and np.mean([b["precip_mm"] for b in base]) > 0:
                anomaly["precip_pct_of_normal"] = round(100 * stats["precip_mm"] / float(np.mean([b["precip_mm"] for b in base])))
            row["anomaly_vs_previous_years"] = anomaly
        rows.append(row)
    return rows
