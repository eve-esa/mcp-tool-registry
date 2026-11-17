"""
Remote data access for the Wildfire Flood MCP server.

Every source is public and needs no API key:

    * Sentinel-2 L2A (Copernicus), found through the Earth Search STAC API and
      read as Cloud-Optimized GeoTIFFs on AWS (bands B08, B12 and SCL)
    * Copernicus DEM GLO-30 (1° COG tiles on AWS)
    * ESA WorldCover 2021 v200 (3° COG tiles on AWS)
    * Open-Meteo forecast and historical-forecast APIs (15-minute and hourly rain)
    * OpenStreetMap place names via Nominatim reverse geocoding

All rasters are warped onto one analysis grid (see Grid) with GDAL WarpedVRTs,
so only the blocks around the area of interest are downloaded. Functions here
block on network I/O; the server calls them from a worker thread.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import aoi
import burn
import httpx
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform, transform_bounds, transform_geom
from rasterio.windows import from_bounds

STAC_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
S2_COLLECTION = "sentinel-2-l2a"
DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM/"
    "Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM.tif"
)
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_MIN_INTERVAL_S = 1.1  # Nominatim usage policy: at most one request per second
PLACE_ADDRESS_KEYS = ("village", "town", "city", "hamlet", "suburb", "municipality")
DISPLAY_MAX_PX = 520  # longest side of the before/after photos

BASE_PIXEL_M = 20  # Sentinel-2 B12 and SCL resolution
MAX_PIXELS = 2_500_000  # per raster; larger areas use a coarser multiple of 20 m
SCENE_SEARCH_DAYS = 120  # how far before / after the fire to look for clear scenes
MAX_SCENE_CLOUD = 60  # scene-level cloud filter (%); the AOI is checked pixel by pixel
GOOD_CLEAR_SHARE = 0.9  # accept the first candidate scene this usable over the AOI
MAX_CANDIDATES = 5  # scenes tried per date before settling for the clearest
FORECAST_PAST_DAYS = 90  # the forecast API serves roughly the last three months
MAX_RAIN_DAYS = 31

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",  # no directory listing per file
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "VSI_CACHE": "TRUE",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "AWS_NO_SIGN_REQUEST": "YES",
}

DATASETS = {
    "sentinel2": {
        "name": "Copernicus Sentinel-2 Level-2A (bands B08, B12, SCL)",
        "provider": "ESA / European Commission (Copernicus), COGs hosted by Element 84 on AWS",
        "resolution_m": 20,
        "catalogue": STAC_SEARCH_URL,
        "licence": "Copernicus Sentinel data, free and open (Copernicus data policy)",
    },
    "dem": {
        "name": "Copernicus DEM GLO-30",
        "provider": "ESA / Airbus (Copernicus), hosted on AWS Open Data",
        "resolution_m": 30,
        "url": DEM_URL.format(lat="<N/S lat>", lon="<E/W lon>"),
        "licence": "Copernicus DEM licence (free, attribution)",
    },
    "worldcover": {
        "name": "ESA WorldCover 10 m 2021 v200",
        "provider": "ESA WorldCover consortium, hosted on AWS Open Data",
        "resolution_m": 10,
        "url": WORLDCOVER_URL.format(tile="<tile>"),
        "licence": "CC BY 4.0",
    },
    "places": {
        "name": "OpenStreetMap place names (reverse geocoding of built-up clusters)",
        "provider": "OpenStreetMap contributors, via Nominatim",
        "url": NOMINATIM_REVERSE_URL,
        "licence": "ODbL 1.0",
    },
    "rain": {
        "name": "Open-Meteo forecast / historical-forecast API (weather-model precipitation)",
        "provider": "Open-Meteo.com (national weather services' models)",
        "url": FORECAST_URL,
        "licence": "CC BY 4.0; free API for non-commercial use",
        "note": (
            "15-minute precipitation is native in North America and central Europe; elsewhere it is "
            "interpolated from hourly data, which smooths short bursts."
        ),
    },
}


@dataclass
class Grid:
    """The analysis grid: a north-up raster in a UTM CRS with square pixels."""

    crs: str
    transform: rasterio.Affine
    width: int
    height: int
    pixel_m: float

    @property
    def shape(self) -> tuple[int, int]:
        """(rows, columns), the NumPy order."""
        return self.height, self.width

    def coarsened(self, max_px: int) -> Grid:
        """The same extent on a coarser grid whose longest side is at most max_px,
        for pictures that do not need full resolution."""
        factor = max(1, math.ceil(max(self.width, self.height) / max_px))
        return Grid(
            self.crs,
            self.transform @ rasterio.Affine.scale(factor),
            math.ceil(self.width / factor),
            math.ceil(self.height / factor),
            self.pixel_m * factor,
        )

    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        """Grid extent as (west, south, east, north) in degrees, to pick DEM and WorldCover tiles."""
        west, north = self.transform * (0, 0)
        east, south = self.transform * (self.width, self.height)
        return transform_bounds(self.crs, "EPSG:4326", west, south, east, north)


@dataclass
class BurnScene:
    """Everything one burn analysis produced, kept for the map and for caching."""

    grid: Grid
    inside: np.ndarray
    dnbr: np.ndarray
    slope: np.ndarray
    shade: np.ndarray
    land_cover: np.ndarray
    stats: burn.BurnStats
    scenes: dict[str, dict]
    tile: str
    sources: dict[str, list[str]] = field(default_factory=dict)
    # Natural-colour photos before and after the fire, on a coarser display grid.
    display_grid: Grid | None = None
    photo_pre: np.ndarray | None = None
    photo_post: np.ndarray | None = None
    # Named built-up places around the scar (see name_places), each flagged when it lies
    # within the danger zone of steep, badly burned slopes.
    places: list[dict] = field(default_factory=list)


# --- Sentinel-2 scene search -------------------------------------------------


def search_scenes(bbox: tuple[float, float, float, float], start: date, end: date) -> list[dict]:
    """Sentinel-2 L2A items over a bbox between two dates (inclusive)."""
    body = {
        "collections": [S2_COLLECTION],
        "bbox": list(bbox),
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": MAX_SCENE_CLOUD}},
        "limit": 200,
    }
    response = httpx.post(STAC_SEARCH_URL, json=body, timeout=60)
    response.raise_for_status()
    return response.json().get("features", [])


def _tile_of(item: dict) -> str:
    """MGRS tile code of a STAC item (e.g. "MGRS-30TXN"), whichever property carries it."""
    props = item["properties"]
    return props.get("grid:code") or props.get("s2:mgrs_tile") or item["id"].split("_")[1]


def _crs_of(item: dict) -> str:
    """CRS of a STAC item ("EPSG:32630"); older items only have proj:epsg."""
    props = item["properties"]
    return props.get("proj:code") or f"EPSG:{props['proj:epsg']}"


def choose_tile(geom: dict, pre: list[dict], post: list[dict]) -> str:
    """MGRS tile seen both before and after the fire that covers the AOI best.

    Pre and post scenes must come from the same tile so that their pixels line
    up exactly; neighbouring tiles overlap but sit on different UTM grids.
    """
    pre_tiles = {_tile_of(i) for i in pre}
    best, best_score = None, (-1.0, 0)
    for item in post:
        tile = _tile_of(item)
        if tile not in pre_tiles:
            continue
        score = (aoi.coverage(geom, item["geometry"]), sum(_tile_of(i) == tile for i in pre + post))
        if score > best_score:
            best, best_score = tile, score
    if best is None:
        raise ValueError("no Sentinel-2 tile has usable scenes both before and after the fire")
    return best


def _unique_dates(items: list[dict], newest_first: bool) -> list[dict]:
    """One item per acquisition date (reprocessed duplicates dropped), sorted."""
    by_date: dict[str, dict] = {}
    for item in items:
        by_date.setdefault(item["properties"]["datetime"][:10], item)
    return [by_date[d] for d in sorted(by_date, reverse=newest_first)]


# --- Analysis grid and raster reads --------------------------------------------


def make_grid(geom: dict, crs: str, margin_km: float) -> Grid:
    """Grid over the AOI bbox plus a margin, snapped to the Sentinel-2 20 m grid."""
    west, south, east, north = aoi.bbox(geom)
    left, bottom, right, top = transform_bounds("EPSG:4326", crs, west, south, east, north)
    margin = margin_km * 1000
    left, bottom, right, top = left - margin, bottom - margin, right + margin, top + margin
    area_px = (right - left) * (top - bottom) / BASE_PIXEL_M**2
    pixel_m = BASE_PIXEL_M * max(1, math.ceil(math.sqrt(area_px / MAX_PIXELS)))
    left = math.floor(left / pixel_m) * pixel_m
    top = math.ceil(top / pixel_m) * pixel_m
    width = math.ceil((right - left) / pixel_m)
    height = math.ceil((top - bottom) / pixel_m)
    return Grid(crs, from_origin(left, top, pixel_m, pixel_m), width, height, float(pixel_m))


def _warp_read(url: str, grid: Grid, resampling: Resampling, nodata: float, dtype: str) -> np.ndarray | None:
    """Read one remote raster warped onto the grid; None if the file is missing
    (e.g. a DEM tile over the sea)."""
    try:
        with rasterio.open("/vsicurl/" + url) as src, WarpedVRT(
            src,
            crs=grid.crs,
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=resampling,
            src_nodata=src.nodata if src.nodata is not None else nodata,
            nodata=nodata,
        ) as vrt:
            return vrt.read(1, out_dtype=dtype)
    except RasterioIOError:
        return None


def _mosaic(urls: list[str], grid: Grid, resampling: Resampling, nodata: float, dtype: str) -> tuple[np.ndarray, list[str]]:
    """Warp several tiles onto the grid and merge them; returns the URLs read."""
    out = np.full(grid.shape, nodata, dtype=dtype)
    used = []
    for url in urls:
        tile = _warp_read(url, grid, resampling, nodata, dtype)
        if tile is not None:
            out = np.where(tile != nodata, tile, out)
            used.append(url)
    return out, used


def dem_urls(bounds: tuple[float, float, float, float]) -> list[str]:
    """Copernicus DEM 1° tiles overlapping a lon/lat bbox (named by their SW corner)."""
    west, south, east, north = bounds
    urls = []
    for lat in range(math.floor(south), math.floor(north) + 1):
        for lon in range(math.floor(west), math.floor(east) + 1):
            urls.append(DEM_URL.format(
                lat=f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}", lon=f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}"
            ))
    return urls


def worldcover_urls(bounds: tuple[float, float, float, float]) -> list[str]:
    """WorldCover 3° tiles overlapping a lon/lat bbox (named by their SW corner)."""
    west, south, east, north = bounds
    urls = []
    for lat in range(math.floor(south / 3) * 3, math.floor(north / 3) * 3 + 1, 3):
        for lon in range(math.floor(west / 3) * 3, math.floor(east / 3) * 3 + 1, 3):
            tile = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"
            urls.append(WORLDCOVER_URL.format(tile=tile))
    return urls


# Sentinel-2 L2A digital numbers carry +1000 from processing baseline 04.00 on.
BOA_ADD_OFFSET_DN = 1000
# With that offset still in the pixels, almost no blue-band value is below 1000
# (it would mean negative reflectance); once removed, most land pixels are.
OFFSET_REMOVED_MIN_SHARE = 0.2
_offset_removed_cache: dict[str, bool] = {}


def _offset_already_removed(item: dict) -> bool:
    """Whether a scene's pixels already have the +1000 offset taken out.

    Element 84 reprocessed many scenes with the offset removed; some say so with
    ``earthsearch:boa_offset_applied: true``, but older catalogue records still
    say false for files that were rewritten. So the flag is trusted when true,
    and otherwise the data decide: a small overview read of the blue band (the
    darkest one) shows whether many land pixels sit below 1000. Cached per scene.
    """
    if item["properties"].get("earthsearch:boa_offset_applied"):
        return True
    if item["id"] not in _offset_removed_cache:
        with rasterio.open("/vsicurl/" + item["assets"]["blue"]["href"]) as src:
            sample = src.read(1, out_shape=(256, 256))  # served from a COG overview
        valid = sample[sample > 0]
        share_below = float((valid < BOA_ADD_OFFSET_DN).mean()) if valid.size else 0.0
        _offset_removed_cache[item["id"]] = share_below >= OFFSET_REMOVED_MIN_SHARE
    return _offset_removed_cache[item["id"]]


def _scale_offset(item: dict, asset: str) -> tuple[float, float]:
    """Scale and offset turning an asset's digital numbers into reflectance.

    The STAC raster:bands give offset -0.1 for products from 2022 on, but the
    offset must not be applied twice: when the pixels already have it removed
    (see _offset_already_removed), dark surfaces would turn negative and the
    burn ratio would be biased.
    """
    band = (item["assets"][asset].get("raster:bands") or [{}])[0]
    offset = band.get("offset", 0.0)
    if offset and _offset_already_removed(item):
        offset = 0.0
    return band.get("scale", 0.0001), offset


def _band(item: dict, asset: str, grid: Grid, resampling: Resampling) -> np.ndarray:
    """One Sentinel-2 band on the grid, as reflectance (or raw codes for SCL)."""
    meta = item["assets"][asset]
    raw = _warp_read(meta["href"], grid, resampling, 0, "uint16")
    if raw is None:
        raise ValueError(f"could not read {asset} of scene {item['id']}")
    if asset == "scl":
        return raw
    return burn.reflectance(raw, *_scale_offset(item, asset))


def _best_nbr(items: list[dict], grid: Grid, inside: np.ndarray) -> tuple[dict, np.ndarray, float]:
    """NBR of the first candidate scene usable over enough of the AOI, else of
    the most usable one.

    Usable = clear land in the Scene Classification AND enough NIR + SWIR2
    signal for a stable ratio. The second condition matters in winter: with a
    low sun, steep slopes facing away from it sit in deep shadow, so a scene
    can be cloud-free yet useless over a mountain fire.
    """
    resampling = Resampling.average if grid.pixel_m > BASE_PIXEL_M else Resampling.bilinear
    best = None
    for item in items[:MAX_CANDIDATES]:
        clear = burn.clear_mask(_band(item, "scl", grid, Resampling.nearest))
        ratio = burn.nbr(_band(item, "nir", grid, resampling), _band(item, "swir22", grid, resampling))
        ratio[~clear] = np.nan
        share = float((np.isfinite(ratio) & inside).sum()) / max(int(inside.sum()), 1)
        if best is None or share > best[2]:
            best = (item, ratio, share)
        if share >= GOOD_CLEAR_SHARE:
            break
    if best is None:
        raise ValueError("no Sentinel-2 scene found")
    return best


def _scene_info(item: dict, usable_share: float) -> dict:
    """Scene identity and quality for the provenance block."""
    props = item["properties"]
    return {
        "id": item["id"],
        "datetime": props["datetime"],
        "scene_cloud_cover_pct": round(props.get("eo:cloud_cover", 0), 1),
        "aoi_usable_share": round(usable_share, 3),
        "processing_baseline": props.get("s2:processing_baseline"),
    }


def _display_band(item: dict, asset: str, grid: Grid) -> np.ndarray:
    """One band as reflectance on a display grid in the scene's own CRS.

    No reprojection is needed (the display grid is a coarsened analysis grid,
    which uses the tile's UTM zone), so a plain windowed read with out_shape
    lets GDAL pick the COG overview level: a few small range requests instead
    of warping the full 10 m band.
    """
    meta = item["assets"][asset]
    left, top = grid.transform * (0, 0)
    right, bottom = grid.transform * (grid.width, grid.height)
    with rasterio.open("/vsicurl/" + meta["href"]) as src:
        window = from_bounds(left, bottom, right, top, transform=src.transform)
        raw = src.read(1, window=window, out_shape=grid.shape, resampling=Resampling.average,
                       boundless=True, fill_value=0)
    return burn.reflectance(raw, *_scale_offset(item, asset))


def _photo(item: dict, grid: Grid) -> np.ndarray:
    """Natural-colour picture of a scene (bands B04, B03, B02) on a display grid."""
    return burn.true_colour(*(_display_band(item, asset, grid) for asset in ("red", "green", "blue")))


def name_places(cells: list[dict], grid: Grid) -> list[dict]:
    """Name built-up clusters (see burn.settlement_cells) with OpenStreetMap.

    Each cluster centre is reverse-geocoded at village level; clusters without a
    settlement name, and repeated names, are dropped. Requests are spaced to
    respect Nominatim's one-per-second policy. Network errors end the lookup
    early: names are a reading aid, so the caller carries on with what it has.

    Returns:
        [{"name", "kind", "lon", "lat", "x", "y", "built_up_ha", "near_steep_burn"}]
    """
    places: list[dict] = []
    seen: set[str] = set()
    for i, cell in enumerate(cells):
        x, y = grid.transform * (cell["col"] + 0.5, cell["row"] + 0.5)
        lons, lats = transform(grid.crs, "EPSG:4326", [x], [y])
        if i:
            time.sleep(NOMINATIM_MIN_INTERVAL_S)
        try:
            response = httpx.get(
                NOMINATIM_REVERSE_URL,
                params={"format": "jsonv2", "lat": round(lats[0], 5), "lon": round(lons[0], 5), "zoom": 14,
                        "addressdetails": 1},
                headers={"User-Agent": "eve-mcp-wildfire-flood/0.1 (EVE hackathon MCP server)"},
                timeout=15,
            )
            response.raise_for_status()
            address = response.json().get("address", {})
        except (httpx.HTTPError, ValueError):
            break
        kind = next((key for key in PLACE_ADDRESS_KEYS if key in address), None)
        if kind and address[kind] not in seen:
            seen.add(address[kind])
            places.append({"name": address[kind], "kind": kind, "lon": round(lons[0], 5), "lat": round(lats[0], 5),
                           "x": x, "y": y, "built_up_ha": cell["built_up_ha"],
                           "near_steep_burn": cell["near_steep_burn"]})
    return places


def analyse_burn(geom: dict, fire_start: date, fire_end: date, margin_km: float = 2.0) -> BurnScene:
    """Burn severity, slope and land cover for an AOI and a fire period.

    Picks the clearest pre-fire scene (closest before fire_start) and post-fire
    scene (closest after fire_end) from one Sentinel-2 tile, then warps B08,
    B12, SCL, the DEM and WorldCover onto one grid and computes the statistics.
    """
    today = datetime.now(UTC).date()
    if fire_end < fire_start:
        raise ValueError("fire_end is before fire_start")
    if fire_end >= today:
        raise ValueError("fire_end must be in the past: a post-fire Sentinel-2 scene is needed")
    box = aoi.bbox(geom)
    pre = search_scenes(box, fire_start - timedelta(days=SCENE_SEARCH_DAYS), fire_start - timedelta(days=1))
    post = search_scenes(box, fire_end + timedelta(days=1), min(today, fire_end + timedelta(days=SCENE_SEARCH_DAYS)))
    if not pre or not post:
        raise ValueError(
            f"no Sentinel-2 scene under {MAX_SCENE_CLOUD}% cloud within {SCENE_SEARCH_DAYS} days "
            f"{'before the fire' if not pre else 'after the fire'}"
        )
    tile = choose_tile(geom, pre, post)
    pre = _unique_dates([i for i in pre if _tile_of(i) == tile], newest_first=True)
    post = _unique_dates([i for i in post if _tile_of(i) == tile], newest_first=False)

    grid = make_grid(geom, _crs_of(post[0]), margin_km)
    inside = geometry_mask(
        [transform_geom("EPSG:4326", grid.crs, geom)], out_shape=grid.shape, transform=grid.transform, invert=True
    )
    with rasterio.Env(**GDAL_ENV):
        pre_item, pre_nbr, pre_share = _best_nbr(pre, grid, inside)
        post_item, post_nbr, post_share = _best_nbr(post, grid, inside)
        dnbr = burn.dnbr(pre_nbr, post_nbr, np.isfinite(pre_nbr) & np.isfinite(post_nbr))

        lonlat = grid.bounds_lonlat()
        dem, dem_used = _mosaic(dem_urls(lonlat), grid, Resampling.bilinear, -32768.0, "float32")
        dem[dem == -32768.0] = 0.0  # missing tiles are open sea
        land_cover, wc_used = _mosaic(worldcover_urls(lonlat), grid, Resampling.mode, 0, "uint8")

        display = grid.coarsened(DISPLAY_MAX_PX)
        photo_pre, photo_post = _photo(pre_item, display), _photo(post_item, display)

    slope = burn.slope_degrees(dem, grid.pixel_m)
    stats = burn.burn_stats(dnbr, slope, land_cover, inside, grid.pixel_m, near_km=margin_km)
    masks = burn.danger_masks(dnbr, slope, land_cover, inside, grid.pixel_m, margin_km)
    places = name_places(burn.settlement_cells(land_cover, masks.near_zone, grid.pixel_m), grid)
    return BurnScene(
        grid=grid,
        inside=inside,
        dnbr=dnbr,
        slope=slope,
        shade=burn.hillshade(dem, grid.pixel_m),
        land_cover=land_cover,
        stats=stats,
        scenes={"pre_fire": _scene_info(pre_item, pre_share), "post_fire": _scene_info(post_item, post_share)},
        tile=tile,
        sources={"dem_tiles": dem_used, "worldcover_tiles": wc_used},
        display_grid=display,
        photo_pre=photo_pre,
        photo_post=photo_post,
        places=places,
    )


# --- Rainfall --------------------------------------------------------------------


def fetch_rain(latitude: float, longitude: float, start: date, end: date) -> dict:
    """15-minute and hourly precipitation (mm per step) for a window of days.

    Recent and future days come from the forecast API (up to 16 days ahead),
    older ones from the historical-forecast archive of the same models. Each
    value is the accumulation over the step that ends at its timestamp.
    """
    if end < start:
        raise ValueError("rain_end is before rain_start")
    if (end - start).days + 1 > MAX_RAIN_DAYS:
        raise ValueError(f"the rain window is limited to {MAX_RAIN_DAYS} days")
    today = datetime.now(UTC).date()
    if end > today + timedelta(days=15):
        raise ValueError("rain forecasts reach at most 15 days ahead")
    url = FORECAST_URL if start >= today - timedelta(days=FORECAST_PAST_DAYS) else HISTORICAL_FORECAST_URL
    params = {
        "latitude": round(latitude, 4),
        "longitude": round(longitude, 4),
        "minutely_15": "precipitation",
        "hourly": "precipitation",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "UTC",
    }
    response = httpx.get(url, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    quarter = data.get("minutely_15") or {}
    mm_15 = quarter.get("precipitation")
    if not mm_15 or all(v is None for v in mm_15):
        mm_15 = None
    return {
        "times_15": quarter.get("time", []),
        "mm_15": mm_15,
        "times_60": data["hourly"]["time"],
        "mm_60": data["hourly"]["precipitation"],
        "request_url": str(response.request.url),
        "kind": "forecast" if url == FORECAST_URL and end >= today else "observed-period model run",
        "grid_point": {"latitude": data.get("latitude"), "longitude": data.get("longitude")},
    }
