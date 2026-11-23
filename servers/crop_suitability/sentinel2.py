"""Sentinel-2 L2A vegetation-index time series (Planetary Computer, no account).

One value per scene: the spatial median of each index over clear (and, by default,
cropland) pixels. Scenes are then grouped into seasons, months, years or custom
periods, and each period is compared with the same season/month in other years.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from datetime import date
from typing import Any

import numpy as np
from sources import PC_STAC_URL, bbox_area_km2, bbox_center, cache_get, cache_put, pc_sign, utm_epsg

logger = logging.getLogger("crop-suitability")

COLLECTION = "sentinel-2-l2a"
BANDS = ["B04", "B08", "B8A", "B11", "SCL"]  # + B03 when NDWI is requested
MAX_SCENES = 60
INDICES = {
    # name: (band_a, band_b, description); index = (a - b) / (a + b)
    "ndvi": ("B08", "B04", "Normalised Difference Vegetation Index: canopy greenness / vigour"),
    "ndmi": ("B8A", "B11", "Normalised Difference Moisture Index (= LSWI): water content of vegetation and soil"),
    "ndwi": ("B03", "B08", "McFeeters NDWI: open water, e.g. flooded rice paddies before canopy closure"),
}
# Scene Classification Layer: 4 vegetation, 5 bare soil, 6 water. Clouds, shadows and snow are dropped.
CLEAR_SCL = (4, 5, 6)
SEASONS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM", 6: "JJA", 7: "JJA", 8: "JJA"}
SEASON_MONTHS = {"DJF": "Dec-Feb", "MAM": "Mar-May", "JJA": "Jun-Aug", "SON": "Sep-Nov"}


def choose_resolution(bbox) -> int:
    """Coarser pixels for bigger areas so a multi-year load stays well under a minute."""
    area_m2 = bbox_area_km2(bbox) * 1e6
    res = math.ceil(math.sqrt(area_m2 / 60_000) / 10) * 10
    return int(min(max(res, 20), 120))


def search_scenes(bbox, start: date, end: date, max_cloud_cover: float, scenes_per_month: int):
    """STAC search, then keep one MGRS tile and the clearest scenes of each month."""
    import pystac_client
    from shapely.geometry import box, shape

    catalog = pystac_client.Client.open(PC_STAC_URL)
    items = list(
        catalog.search(
            collections=[COLLECTION],
            bbox=list(bbox),
            datetime=f"{start.isoformat()}/{end.isoformat()}",
            query={"eo:cloud_cover": {"lt": max_cloud_cover}},
        ).items()
    )
    aoi = box(*bbox)
    covering = [i for i in items if shape(i.geometry).contains(aoi)]
    if covering:
        tile = Counter(i.properties.get("s2:mgrs_tile") for i in covering).most_common(1)[0][0]
        pool = [i for i in covering if i.properties.get("s2:mgrs_tile") == tile]
    else:
        tile, pool = None, items
    by_month: dict[str, list] = defaultdict(list)
    for item in pool:
        by_month[item.datetime.strftime("%Y-%m")].append(item)
    selected = []
    for month_items in by_month.values():
        selected.extend(sorted(month_items, key=lambda i: i.properties.get("eo:cloud_cover", 100))[:scenes_per_month])
    selected.sort(key=lambda i: i.datetime)
    return selected, {"n_found": len(items), "n_selected": len(selected), "mgrs_tile": tile}


def _boa_offset(item) -> float:
    # Processing baseline 04.00 (Jan 2022 onwards) adds +1000 to L2A digital numbers.
    try:
        return 1000.0 if float(item.properties.get("s2:processing_baseline", "0")) >= 4.0 else 0.0
    except ValueError:
        return 0.0


def load_scene_series(
    bbox,
    start: date,
    end: date,
    max_cloud_cover: float = 40,
    scenes_per_month: int = 2,
    cropland_only: bool = True,
    min_valid_fraction: float = 0.3,
    with_ndwi: bool = False,
) -> dict[str, Any]:
    """Per-scene spatial medians of NDVI and NDMI (and NDWI). Cached for an hour."""
    for ndwi in (True, False) if not with_ndwi else (True,):
        key = ("s2", bbox, start, end, max_cloud_cover, scenes_per_month, cropland_only, min_valid_fraction, ndwi)
        if (hit := cache_get(key)) is not None:
            return hit
    bands = (["B03"] if with_ndwi else []) + BANDS
    indices = {k: v for k, v in INDICES.items() if with_ndwi or k != "ndwi"}

    import odc.stac
    from sources import worldcover_cropland_mask

    odc.stac.configure_rio(cloud_defaults=True)
    items, search_info = search_scenes(bbox, start, end, max_cloud_cover, scenes_per_month)
    if len(items) > MAX_SCENES and scenes_per_month > 1:
        # Long periods: one clear scene per month keeps the load within the tool time limit.
        items, search_info = search_scenes(bbox, start, end, max_cloud_cover, 1)
        search_info["note"] = "Reduced to 1 scene per month to stay within the time limit."
    if not items:
        return {"ok": False, "error": "No Sentinel-2 scenes found for this area and period.", **search_info}

    resolution = choose_resolution(bbox)
    lon, lat = bbox_center(bbox)
    ds = odc.stac.load(
        items,
        bands=bands,
        bbox=list(bbox),
        crs=f"EPSG:{utm_epsg(lon, lat)}",
        resolution=resolution,
        groupby="solar_day",
        patch_url=pc_sign,
        chunks={},
    ).compute(scheduler="threads", num_workers=16)

    offsets = {i.datetime.date().isoformat(): _boa_offset(i) for i in items}
    clouds = {i.datetime.date().isoformat(): i.properties.get("eo:cloud_cover") for i in items}

    warnings = []
    area_mask = np.ones((ds.sizes["y"], ds.sizes["x"]), dtype=bool)
    cropland_fraction = None
    if cropland_only:
        try:
            crop_mask = worldcover_cropland_mask(ds.odc.geobox)
        except Exception as exc:  # noqa: BLE001 - the series is still useful without the mask
            crop_mask = None
            warnings.append(f"Cropland mask unavailable ({exc}); used all pixels.")
        if crop_mask is not None:
            cropland_fraction = float(crop_mask.mean())
            if cropland_fraction >= 0.05:
                area_mask = crop_mask
            else:
                warnings.append(f"Only {cropland_fraction:.0%} of the area is cropland in WorldCover; used all pixels.")

    scenes = []
    for t in range(ds.sizes["time"]):
        day = str(ds.time.values[t])[:10]
        scl = ds["SCL"].isel(time=t).values
        offset = offsets.get(day, 0.0)
        refl = {b: (ds[b].isel(time=t).values.astype("float32") - offset) / 10000.0 for b in bands if b != "SCL"}
        valid = np.isin(scl, CLEAR_SCL) & area_mask & (ds["B04"].isel(time=t).values > 0)
        valid_fraction = float(valid.sum() / max(area_mask.sum(), 1))
        if valid_fraction < min_valid_fraction:
            continue
        scene = {"date": day, "valid_fraction": round(valid_fraction, 3), "cloud_cover": clouds.get(day)}
        for name, (a, b, _) in indices.items():
            with np.errstate(divide="ignore", invalid="ignore"):
                idx = (refl[a] - refl[b]) / (refl[a] + refl[b])
            vals = idx[valid & np.isfinite(idx)]
            scene[name] = round(float(np.median(vals)), 4) if vals.size else None
        # Share of clear pixels that look flooded (LSWI + 0.05 >= vegetation index), as for paddy rice.
        with np.errstate(divide="ignore", invalid="ignore"):
            ndvi = (refl["B08"] - refl["B04"]) / (refl["B08"] + refl["B04"])
            lswi = (refl["B8A"] - refl["B11"]) / (refl["B8A"] + refl["B11"])
        ok = valid & np.isfinite(ndvi) & np.isfinite(lswi)
        scene["flooded_fraction"] = round(float((lswi[ok] + 0.05 >= ndvi[ok]).mean()), 3) if ok.any() else None
        scenes.append(scene)

    if not scenes:
        return {
            "ok": False,
            "error": "All Sentinel-2 scenes were too cloudy over the area. Raise max_cloud_cover or lower min_valid_fraction.",
            **search_info,
        }
    out = {
        "ok": True,
        "source": "Sentinel-2 L2A, Microsoft Planetary Computer (sentinel-2-l2a)",
        "resolution_m": resolution,
        "pixels_per_scene": int(ds.sizes["y"] * ds.sizes["x"]),
        "cropland_only": bool(cropland_only and not area_mask.all()),
        "cropland_fraction_worldcover": round(cropland_fraction, 3) if cropland_fraction is not None else None,
        "search": {**search_info, "n_usable": len(scenes)},
        "scenes": scenes,
        "warnings": warnings,
    }
    return cache_put(key, out)


# ---------------------------------------------------------------------------
# Period aggregation and anomalies
# ---------------------------------------------------------------------------


def _period_key(day: date, period: str) -> tuple[str, str]:
    """(label, climatology group) of a date for season / month / year aggregation."""
    if period == "month":
        return f"{day.year}-{day.month:02d}", f"{day.month:02d}"
    if period == "year":
        return str(day.year), "year"
    season = SEASONS.get(day.month, "SON")
    year = day.year + 1 if day.month == 12 else day.year
    return f"{year}-{season}", season


def _parse_custom_periods(custom_periods: list) -> list[tuple[str, date, date]]:
    parsed = []
    for i, p in enumerate(custom_periods):
        if isinstance(p, str):
            start_s, end_s = p.split("/")
            label = p
        else:
            start_s, end_s = p["start"], p["end"]
            label = p.get("label") or f"{start_s}/{end_s}"
        parsed.append((label or f"period_{i + 1}", date.fromisoformat(start_s), date.fromisoformat(end_s)))
    return parsed


def aggregate(scenes: list[dict], period: str, indices: list[str], custom_periods: list | None = None) -> list[dict]:
    """Group scene values into periods: median, p10, p90 and anomaly vs the same season in other years."""
    groups: dict[str, dict[str, Any]] = {}
    if period == "custom":
        for label, start, end in _parse_custom_periods(custom_periods or []):
            members = [s for s in scenes if start <= date.fromisoformat(s["date"]) <= end]
            groups[label] = {"clim": "custom", "start": start.isoformat(), "end": end.isoformat(), "scenes": members}
    else:
        for s in scenes:
            label, clim = _period_key(date.fromisoformat(s["date"]), period)
            g = groups.setdefault(label, {"clim": clim, "start": s["date"], "end": s["date"], "scenes": []})
            g["scenes"].append(s)
            g["end"] = s["date"]

    rows = []
    for label, g in groups.items():
        row = {"period": label, "first_scene": g["start"], "last_scene": g["end"], "n_scenes": len(g["scenes"])}
        if g["clim"] in SEASON_MONTHS:
            row["months"] = SEASON_MONTHS[g["clim"]]
        for name in indices:
            vals = np.array([s[name] for s in g["scenes"] if s.get(name) is not None], dtype=float)
            row[name] = (
                {
                    "median": round(float(np.median(vals)), 3),
                    "p10": round(float(np.percentile(vals, 10)), 3),
                    "p90": round(float(np.percentile(vals, 90)), 3),
                }
                if vals.size
                else None
            )
        row["_clim"] = g["clim"]
        rows.append(row)

    # Anomaly: this period's median minus the mean of the same season/month in the other years.
    if period in ("season", "month", "year"):
        for row in rows:
            for name in indices:
                if not row.get(name):
                    continue
                others = [
                    r[name]["median"]
                    for r in rows
                    if r is not row and r["_clim"] == row["_clim"] and r.get(name) and r["n_scenes"] >= 2
                ]
                if not others:
                    continue
                mean = float(np.mean(others))
                row[name]["anomaly"] = round(row[name]["median"] - mean, 3)
                if len(others) >= 2 and np.std(others, ddof=1) > 1e-6:
                    row[name]["z"] = round((row[name]["median"] - mean) / float(np.std(others, ddof=1)), 2)
                row[name]["baseline_n"] = len(others)
    for row in rows:
        row.pop("_clim")
    return rows
