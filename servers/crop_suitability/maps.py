"""Sentinel-2 map composites: true colour, NDVI and NDMI images of an area, one per year.

For each year the clear pixels of the scenes in one month (by default the middle of the
growing season) are combined into a median composite, so the same month can be compared
across years and across areas. Clouds, shadows and snow are masked with the Scene
Classification Layer.
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
from sentinel2 import CLEAR_SCL, COLLECTION, _boa_offset
from sources import PC_STAC_URL, bbox_area_km2, bbox_center, pc_sign, utm_epsg

logger = logging.getLogger("crop-suitability")

MAP_BANDS = ["B02", "B03", "B04", "B08", "B8A", "B11", "SCL"]
MAX_SCENES_PER_YEAR = 4


def map_resolution(bbox) -> int:
    """10 m (native) for small areas, coarser for big ones so a composite stays around 1 Mpx."""
    area = bbox_area_km2(bbox)
    return 10 if area <= 60 else 20 if area <= 250 else 30


def _pick_scenes(items, year: int, month: int, max_cloud_cover: float) -> list:
    """Clearest scenes of `month` in `year`; widen to the neighbouring months if none qualify."""
    for months in ([month], [month - 1, month, month + 1]):
        months = [((m - 1) % 12) + 1 for m in months]
        pool = [
            i
            for i in items
            if i.datetime.year == year
            and i.datetime.month in months
            and i.properties.get("eo:cloud_cover", 100) <= max_cloud_cover
        ]
        if pool:
            return sorted(pool, key=lambda i: i.properties.get("eo:cloud_cover", 100))[:MAX_SCENES_PER_YEAR]
    return []


def load_composites(bbox, years: list[int], month: int, max_cloud_cover: float = 30) -> dict:
    """Median composites per year: {"years": {year: {...arrays...}}, "resolution_m": ...}."""
    import odc.stac
    import pystac_client
    from shapely.geometry import box, shape

    odc.stac.configure_rio(cloud_defaults=True)
    catalog = pystac_client.Client.open(PC_STAC_URL)
    first, last = min(years), max(years)
    m_lo, m_hi = max(month - 1, 1), min(month + 1, 12)
    items = list(
        catalog.search(
            collections=[COLLECTION],
            bbox=list(bbox),
            datetime=f"{first}-{m_lo:02d}-01/{last}-{m_hi:02d}-28",
            query={"eo:cloud_cover": {"lt": max_cloud_cover}},
        ).items()
    )
    aoi = box(*bbox)
    covering = [i for i in items if shape(i.geometry).contains(aoi)]
    if covering:
        tiles = [i.properties.get("s2:mgrs_tile") for i in covering]
        tile = max(set(tiles), key=tiles.count)
        items = [i for i in covering if i.properties.get("s2:mgrs_tile") == tile]

    picked = {y: _pick_scenes(items, y, month, max_cloud_cover) for y in years}
    selected = [i for scenes in picked.values() for i in scenes]
    resolution = map_resolution(bbox)
    out = {"resolution_m": resolution, "month": month, "years": {}}
    if not selected:
        return out

    lon, lat = bbox_center(bbox)
    ds = odc.stac.load(
        selected,
        bands=MAP_BANDS,
        bbox=list(bbox),
        crs=f"EPSG:{utm_epsg(lon, lat)}",
        resolution=resolution,
        groupby="solar_day",
        resampling={"*": "bilinear", "SCL": "nearest"},
        patch_url=pc_sign,
        chunks={},
    ).compute(scheduler="threads", num_workers=16)

    offsets = {i.datetime.date().isoformat(): _boa_offset(i) for i in selected}
    days = [str(t)[:10] for t in ds.time.values]
    for year in picked:
        idx = [k for k, d in enumerate(days) if d[:4] == str(year)]
        if not idx:
            continue
        stack = {b: [] for b in MAP_BANDS if b != "SCL"}
        for k in idx:
            clear = np.isin(ds["SCL"].isel(time=k).values, CLEAR_SCL) & (ds["B04"].isel(time=k).values > 0)
            offset = offsets.get(days[k], 0.0)
            for b in stack:
                arr = (ds[b].isel(time=k).values.astype("float32") - offset) / 10000.0
                stack[b].append(np.where(clear, arr, np.nan))
        with np.errstate(all="ignore"):
            refl = {b: np.nanmedian(np.stack(v), axis=0) for b, v in stack.items()}
            ndvi = (refl["B08"] - refl["B04"]) / (refl["B08"] + refl["B04"])
            ndmi = (refl["B8A"] - refl["B11"]) / (refl["B8A"] + refl["B11"])
        rgb = np.dstack([refl["B04"], refl["B03"], refl["B02"]])
        clear_fraction = float(np.isfinite(ndvi).mean())
        out["years"][year] = {
            "dates": [days[k] for k in idx],
            "rgb": rgb,
            "ndvi": ndvi,
            "ndmi": ndmi,
            "clear_fraction": round(clear_fraction, 3),
            "ndvi_mean": round(float(np.nanmean(ndvi)), 3) if clear_fraction else None,
            "ndmi_mean": round(float(np.nanmean(ndmi)), 3) if clear_fraction else None,
        }
    return out


def default_month(lat: float, crop: str | None) -> int:
    """Middle of the crop's growing season (or July / January when no crop is given)."""
    import crop_rules

    if crop:
        _, rules = crop_rules.resolve_crop(crop)
        months = crop_rules.growing_months(rules, lat)
        return months[len(months) // 2]
    return 7 if lat >= 0 else 1


def summary(composites: dict) -> list[dict]:
    """JSON-friendly per-year summary (no arrays)."""
    return [
        {
            "year": y,
            "scene_dates": c["dates"],
            "clear_fraction": c["clear_fraction"],
            "ndvi_mean": c["ndvi_mean"],
            "ndmi_mean": c["ndmi_mean"],
        }
        for y, c in sorted(composites["years"].items())
    ]


def years_between(start: date, end: date) -> list[int]:
    return list(range(start.year, end.year + 1))[-6:]
