"""
Burn-scar and terrain maths for the Wildfire Flood MCP server (NumPy only, no I/O).

All arrays are on one common grid: the Sentinel-2 20 m grid of the scene's UTM
zone (or a coarser multiple of it for large areas), so pixels are square and
their size is known in metres.

    * Burn severity: differenced Normalized Burn Ratio, dNBR = NBR_pre - NBR_post,
      with NBR = (NIR - SWIR2) / (NIR + SWIR2) (Sentinel-2 B08 and B12),
      classified with the USGS FIREMON thresholds (Key & Benson 2006).
    * Terrain: slope in degrees from the Copernicus DEM.
    * Land cover: ESA WorldCover 2021 classes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Sentinel-2 Scene Classification (SCL) values kept as clear land. 2 "dark area"
# is kept on purpose: fresh burn scars are dark and often land in that class.
# Dropped: 0 no data, 1 saturated, 3 cloud shadow, 6 water, 8-10 cloud/cirrus, 11 snow.
CLEAR_SCL = (2, 4, 5, 7)

# dNBR severity classes (x1000 scale), USGS FIREMON / Key & Benson (2006).
SEVERITY_CLASSES = [
    ("unburned", -10_000, 100),
    ("low", 100, 270),
    ("moderate-low", 270, 440),
    ("moderate-high", 440, 660),
    ("high", 660, 10_000),
]
BURNED_MIN_DNBR = 100  # lower bound of "low" severity: the burn footprint
MOD_HIGH_MIN_DNBR = 270  # moderate or high severity, the input to the debris-flow model
STEEP_SLOPE_DEG = 23.0  # Staley et al. (2017): slopes >= 23° drive post-fire debris flows

# ESA WorldCover 2021 class codes and names.
WORLDCOVER_CLASSES = {
    10: "tree cover",
    20: "shrubland",
    30: "grassland",
    40: "cropland",
    50: "built-up",
    60: "bare / sparse vegetation",
    70: "snow and ice",
    80: "permanent water",
    90: "herbaceous wetland",
    95: "mangroves",
    100: "moss and lichen",
}
BUILT_UP = 50
CROPLAND = 40


def reflectance(dn: np.ndarray, scale: float, offset: float, nodata: int = 0) -> np.ndarray:
    """Surface reflectance from Sentinel-2 L2A digital numbers; nodata -> NaN.

    Products from processing baseline 04.00 (2022) on carry an offset of -0.1,
    so the scale and offset must come from each asset's STAC metadata.
    """
    values = dn.astype("float32") * scale + offset
    values[dn == nodata] = np.nan
    return values


MIN_NBR_SIGNAL = 0.02  # NIR + SWIR2 reflectance below this is noise (deep shadow, water)


def nbr(nir: np.ndarray, swir2: np.ndarray) -> np.ndarray:
    """Normalized Burn Ratio, NaN where there is too little signal.

    With the -0.1 offset of recent products, reflectance can be ~0 or slightly
    negative in deep shadow; the ratio would blow up there, so such pixels are
    dropped and the result is clipped to its physical range [-1, 1].
    """
    total = nir + swir2
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = (nir - swir2) / total
    ratio[~np.isfinite(ratio) | ~(total > MIN_NBR_SIGNAL)] = np.nan
    return np.clip(ratio, -1.0, 1.0)


def clear_mask(scl: np.ndarray) -> np.ndarray:
    """True where the Scene Classification marks clear land."""
    return np.isin(scl, CLEAR_SCL)


def dnbr(nbr_pre: np.ndarray, nbr_post: np.ndarray, clear: np.ndarray) -> np.ndarray:
    """Differenced NBR on the x1000 scale used by the severity thresholds;
    NaN where `clear` is False (cloud, shadow or no data on either date)."""
    out = (nbr_pre - nbr_post) * 1000.0
    out[~clear] = np.nan
    return out


def severity(dnbr_x1000: np.ndarray) -> np.ndarray:
    """Severity class index per pixel (0 = unburned ... 4 = high, -1 = no data)."""
    classes = np.full(dnbr_x1000.shape, -1, dtype="int8")
    for index, (_, low, high) in enumerate(SEVERITY_CLASSES):
        classes[(dnbr_x1000 >= low) & (dnbr_x1000 < high)] = index
    return classes


def slope_degrees(dem: np.ndarray, pixel_m: float) -> np.ndarray:
    """Slope in degrees from elevation (m) on a square grid of pixel_m metres."""
    dz_dy, dz_dx = np.gradient(dem.astype("float64"), pixel_m)
    return np.degrees(np.arctan(np.hypot(dz_dx, dz_dy))).astype("float32")


def hillshade(dem: np.ndarray, pixel_m: float, azimuth: float = 315.0, altitude: float = 45.0) -> np.ndarray:
    """Hillshade (0-1) for map backgrounds, lit from the north-west."""
    dz_dy, dz_dx = np.gradient(dem.astype("float64"), pixel_m)
    slope = np.arctan(np.hypot(dz_dx, dz_dy))
    aspect = np.arctan2(-dz_dx, dz_dy)
    az, alt = np.radians(azimuth), np.radians(altitude)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(shade, 0, 1).astype("float32")


def box_sum(mask: np.ndarray, radius_px: int) -> np.ndarray:
    """Count of True pixels in the (2r+1) x (2r+1) window around each pixel,
    via cumulative sums (O(n), no SciPy dependency)."""

    def along(values: np.ndarray, axis: int) -> np.ndarray:
        """Window sums along one axis: difference of a zero-padded cumulative sum."""
        size = values.shape[axis]
        pad_before = np.zeros_like(values.take([0], axis=axis)).repeat(radius_px + 1, axis=axis)
        pad_after = np.zeros_like(values.take([0], axis=axis)).repeat(radius_px, axis=axis)
        sums = np.cumsum(np.concatenate([pad_before, values, pad_after], axis=axis), axis=axis)
        upper = sums.take(range(2 * radius_px + 1, 2 * radius_px + 1 + size), axis=axis)
        return upper - sums.take(range(size), axis=axis)

    return along(along(mask.astype("int32"), 0), 1)


def dilate(mask: np.ndarray, radius_px: int) -> np.ndarray:
    """Binary dilation with a square window of +-radius_px."""
    return mask.copy() if radius_px <= 0 else box_sum(mask, radius_px) > 0


def burn_footprint(dnbr_x1000: np.ndarray, radius_px: int = 2, min_share: float = 0.5) -> np.ndarray:
    """Burned pixels (dNBR >= 100) that sit in a mostly burned neighbourhood.

    Pre- and post-fire scenes weeks apart see different sun angles, so shaded
    slopes far from the fire show scattered "low severity" pixels. A real burn
    scar is contiguous; requiring half of the 5 x 5 window to be burned removes
    that speckle while keeping the scar and its edges.
    """
    burned = np.nan_to_num(dnbr_x1000, nan=-1e9) >= BURNED_MIN_DNBR
    window = (2 * radius_px + 1) ** 2
    return burned & (box_sum(burned, radius_px) >= min_share * window)


def scar_mask(dnbr_x1000: np.ndarray, land_cover: np.ndarray) -> np.ndarray:
    """The burn scar: the despeckled footprint without cropland.

    Cereal harvest between the pre- and post-fire scenes lowers NBR just like
    fire does, so in summer dNBR flags harvested fields as burned. Cropland is
    flat and not where debris flows start, so it is left out of the scar and
    reported separately as "cropland change".
    """
    return burn_footprint(dnbr_x1000) & (land_cover != CROPLAND)


def true_colour(red: np.ndarray, green: np.ndarray, blue: np.ndarray, white: float = 0.3) -> np.ndarray:
    """Natural-colour image (rows, cols, 3) uint8 from surface reflectance.

    Land reflectance rarely exceeds ~0.3 in the visible bands, so that value is
    mapped to white; a gamma of 1/1.8 lifts the dark vegetation and burn scars
    so they look as the eye would see them. No-data pixels become black.
    """
    stack = np.dstack([red, green, blue])
    scaled = np.clip(np.nan_to_num(stack, nan=0.0) / white, 0, 1) ** (1 / 1.8)
    return (scaled * 255).astype("uint8")


@dataclass
class DangerMasks:
    """Where a post-fire debris flow would start and whom it could reach."""

    burned: np.ndarray  # the burn scar inside the AOI with a valid dNBR
    steep_mod_high: np.ndarray  # moderate/high burn on slopes >= 23°: where debris flows start
    near_zone: np.ndarray  # within near_km of those slopes
    exposed_built_up: np.ndarray  # built-up land in the near zone, outside the scar


def danger_masks(
    dnbr_x1000: np.ndarray,
    slope_deg: np.ndarray,
    land_cover: np.ndarray,
    inside: np.ndarray,
    pixel_m: float,
    near_km: float = 2.0,
) -> DangerMasks:
    """The masks behind the burn statistics and the danger-zone map.

    The near zone is a plain distance buffer, a screening simplification: it
    does not follow flow paths, so it also covers land uphill of the scar.
    """
    valid = inside & np.isfinite(dnbr_x1000)
    burned = valid & scar_mask(dnbr_x1000, land_cover)
    steep_mod_high = burned & (dnbr_x1000 >= MOD_HIGH_MIN_DNBR) & (slope_deg >= STEEP_SLOPE_DEG)
    near_zone = dilate(steep_mod_high, round(near_km * 1000 / pixel_m))
    exposed = near_zone & (land_cover == BUILT_UP) & ~burned
    return DangerMasks(burned, steep_mod_high, near_zone, exposed)


def settlement_cells(
    land_cover: np.ndarray,
    near_zone: np.ndarray,
    pixel_m: float,
    cell_m: float = 500.0,
    max_cells: int = 8,
    min_separation_m: float = 3000.0,
) -> list[dict]:
    """Centres of the main built-up clusters, exposed ones first.

    The grid is cut into cell_m blocks; blocks are ranked by whether they hold
    built-up land in the near zone, then by how much built-up land they hold,
    and picked greedily at least min_separation_m apart (one label per village
    rather than one per street). Pure NumPy, no flood fill.

    Returns:
        [{"row", "col", "built_up_ha", "near_steep_burn"}] with row/col the
        block centre on the analysis grid.
    """
    block = max(1, round(cell_m / pixel_m))
    rows, cols = land_cover.shape[0] // block, land_cover.shape[1] // block
    if rows == 0 or cols == 0:
        return []
    trim = (slice(0, rows * block), slice(0, cols * block))
    built = (land_cover[trim] == BUILT_UP).reshape(rows, block, cols, block).sum(axis=(1, 3))
    exposed = ((land_cover[trim] == BUILT_UP) & near_zone[trim]).reshape(rows, block, cols, block).sum(axis=(1, 3))
    min_px = max(1, block * block // 20)  # at least 5 % built-up
    candidates = [(bool(exposed[r, c]), int(built[r, c]), r, c)
                  for r in range(rows) for c in range(cols) if built[r, c] >= min_px]
    candidates.sort(reverse=True)
    picked: list[dict] = []
    min_sep_blocks = min_separation_m / (block * pixel_m)
    for is_exposed, count, r, c in candidates:
        if all((r - p["_r"]) ** 2 + (c - p["_c"]) ** 2 >= min_sep_blocks**2 for p in picked):
            picked.append({"_r": r, "_c": c, "row": r * block + block // 2, "col": c * block + block // 2,
                           "built_up_ha": round(count * pixel_m * pixel_m / 10_000, 1), "near_steep_burn": is_exposed})
        if len(picked) == max_cells:
            break
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in picked]


@dataclass
class BurnStats:
    """Area statistics of a burn scar inside the AOI."""

    aoi_ha: float
    clear_share: float  # share of the AOI with a valid dNBR on both dates
    burned_ha: float
    severity_ha: dict[str, float]
    mean_dnbr_burned: float | None
    mod_high_ha: float
    steep_mod_high_ha: float
    steep_mod_high_share: float  # of the burned area: the "T" term of Staley M1
    mean_slope_burned_deg: float | None
    land_cover_burned_ha: dict[str, float]
    built_up_burned_ha: float
    cropland_change_ha: float  # harvest or burn on cropland; left out of the scar
    built_up_near_ha: float
    near_km: float


def burn_stats(
    dnbr_x1000: np.ndarray,
    slope_deg: np.ndarray,
    land_cover: np.ndarray,
    inside: np.ndarray,
    pixel_m: float,
    near_km: float = 2.0,
) -> BurnStats:
    """Burn-scar statistics over the AOI.

    The burn scar (dNBR >= 100, despeckled, cropland excluded; see scar_mask) is treated as one drainage area, a
    screening simplification of the per-basin USGS assessment. Built-up land
    "near" the scar is WorldCover built-up within near_km of moderate/high burn
    on steep slopes, where runoff and debris would come from; no flow routing.
    It is counted over the whole grid, which the caller reads with a margin
    around the AOI so that communities just outside a fire perimeter count.
    """
    pixel_ha = pixel_m * pixel_m / 10_000
    valid = inside & np.isfinite(dnbr_x1000)
    footprint = burn_footprint(dnbr_x1000)
    masks = danger_masks(dnbr_x1000, slope_deg, land_cover, inside, pixel_m, near_km)
    burned, steep_mod_high = masks.burned, masks.steep_mod_high
    classes = severity(np.where(burned, dnbr_x1000, np.nan))
    mod_high = burned & (dnbr_x1000 >= MOD_HIGH_MIN_DNBR)
    burned_px = int(burned.sum())
    built_up = land_cover == BUILT_UP

    land_cover_burned = {}
    for code, name in WORLDCOVER_CLASSES.items():
        hectares = float((burned & (land_cover == code)).sum()) * pixel_ha
        if hectares > 0:
            land_cover_burned[name] = round(hectares, 1)

    return BurnStats(
        aoi_ha=round(float(inside.sum()) * pixel_ha, 1),
        clear_share=round(float(valid.sum()) / max(int(inside.sum()), 1), 3),
        burned_ha=round(burned_px * pixel_ha, 1),
        severity_ha={
            name: round(float((classes == index).sum()) * pixel_ha, 1)
            for index, (name, _, _) in enumerate(SEVERITY_CLASSES)
            if index > 0
        },
        mean_dnbr_burned=round(float(np.nanmean(dnbr_x1000[burned])), 1) if burned_px else None,
        mod_high_ha=round(float(mod_high.sum()) * pixel_ha, 1),
        steep_mod_high_ha=round(float(steep_mod_high.sum()) * pixel_ha, 1),
        steep_mod_high_share=round(float(steep_mod_high.sum()) / burned_px, 3) if burned_px else 0.0,
        mean_slope_burned_deg=round(float(slope_deg[burned].mean()), 1) if burned_px else None,
        land_cover_burned_ha=land_cover_burned,
        built_up_burned_ha=round(float((burned & built_up).sum()) * pixel_ha, 1),
        cropland_change_ha=round(float((valid & footprint & (land_cover == CROPLAND)).sum()) * pixel_ha, 1),
        built_up_near_ha=round(float(masks.exposed_built_up.sum()) * pixel_ha, 1),
        near_km=near_km,
    )
