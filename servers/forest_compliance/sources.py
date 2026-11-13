"""
Remote raster access for the Forest Compliance MCP server.

This is the only module that touches the network. Both datasets are public
Cloud-Optimized / tiled GeoTIFFs read in place through GDAL's ``/vsicurl/``
virtual file system: rasterio asks for a window, GDAL fetches just the tiles
that cover it with HTTP range requests. Only the pixels around a plot are
downloaded, nothing is cached on disk and no API key is needed.

The functions here return plain numpy arrays and hand the logic to
``forest.py`` (tallies, map classes); ``server.py`` calls them from a worker
thread because the reads block; ``overview.py`` draws the background returned by
``forest_background``.

All functions are blocking; call them through ``asyncio.to_thread``.
"""

from __future__ import annotations

import forest
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

# JRC GFC2020 V4: one global COG at ~10 m (a third of a Hansen pixel) with overviews.
GFC2020_URL = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/FOREST/GFC2020/LATEST/single-cog/JRC_GFC2020_V4_COG.tif"
)
# Hansen GFC lossyear: 10 x 10 degree tiles at 30 m (0.00025 degree); {tile} is e.g. 10N_010W.
HANSEN_VERSION = "GFC-2024-v1.12"
HANSEN_URL = (
    "https://storage.googleapis.com/earthenginepartners-hansen/"
    f"{HANSEN_VERSION}/Hansen_{HANSEN_VERSION}_lossyear_{{tile}}.tif"
)
HANSEN_LAST_YEAR = 2024  # last loss year in HANSEN_VERSION; later clearing is not visible
# Latitude coverage of each dataset (degrees); plots outside both are rejected.
GFC2020_LAT_RANGE = (-60.0, 80.0)
HANSEN_LAT_RANGE = (-50.0, 80.0)

# Dataset descriptions copied verbatim into every provenance block (audit trail, licences).
DATASETS = [
    {
        "name": "JRC Global Forest Cover 2020 (GFC2020) V4",
        "role": "forest / non-forest on 31 December 2020 (EUDR reference map of the EU Observatory)",
        "provider": "European Commission, Joint Research Centre",
        "resolution_m": 10,
        "url": GFC2020_URL,
        "info": "https://forobs.jrc.ec.europa.eu/GFC",
        "licence": "© European Union; reuse authorised under Commission Decision 2011/833/EU",
    },
    {
        "name": f"Hansen/UMD/Google/USGS/NASA Global Forest Change {HANSEN_VERSION} (lossyear)",
        "role": f"year of tree cover loss, 2001-{HANSEN_LAST_YEAR}",
        "provider": "University of Maryland (Hansen et al. 2013), also used by Global Forest Watch",
        "resolution_m": 30,
        "url": HANSEN_URL.format(tile="<tile>"),
        "info": "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2024-v1.12/download.html",
        "licence": "CC BY 4.0",
    },
]

# GDAL options for fast, robust remote reads:
# - no directory listing on open (no extra HTTP request for sidecar files),
# - /vsicurl/ only for .tif URLs,
# - merge neighbouring byte ranges and use multi-range requests (fewer round trips),
# - in-memory cache of fetched blocks within one open dataset,
# - timeout and retries, since a public server can be slow or drop a request.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "VSI_CACHE": "TRUE",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


def _check_coverage(box: tuple[float, float, float, float]) -> None:
    """Fail early, with a clear message, if the bbox leaves the latitudes both datasets cover.

    Raises:
        ValueError: the bbox reaches outside the common latitude range.
    """
    _, south, _, north = box
    low = max(GFC2020_LAT_RANGE[0], HANSEN_LAT_RANGE[0])
    high = min(GFC2020_LAT_RANGE[1], HANSEN_LAT_RANGE[1])
    if south < low or north > high:
        raise ValueError(f"plot is outside the data coverage (latitudes {low}..{high})")


def measure(geom: dict) -> tuple[dict, list[str]]:
    """Area statistics for a plot, read on the 10 m GFC2020 grid.

    For each Hansen tile the plot overlaps, the GFC2020 window is read at native
    resolution and the Hansen loss-year raster is resampled onto it. Nearest
    neighbour is the right choice: loss year is a categorical code (averaging
    years makes no sense) and the grids are aligned, so each 30 m Hansen pixel
    becomes exactly 3x3 GFC pixels without shifting. Working on the finer grid
    keeps the 10 m detail of the EUDR reference map. Pixels count as inside
    when their centre falls inside the plot. Blocking: call it from a worker
    thread.

    Args:
        geom: Plot Polygon/MultiPolygon in WGS84 lon/lat.

    Returns:
        (stats, tiles): the merged ``forest.tally`` result and the Hansen tile
        names read, for the provenance block.

    Raises:
        ValueError: the plot is outside the data coverage.
        rasterio.errors.RasterioIOError: a remote read failed.
    """
    box = forest.bbox(geom)
    _check_coverage(box)
    parts = []
    tiles_used = []
    with rasterio.Env(**GDAL_ENV), rasterio.open("/vsicurl/" + GFC2020_URL) as gfc:
        for tile, part in forest.hansen_tiles(box):
            # Snap outwards to whole pixels so the window fully covers the plot part.
            window = from_bounds(*part, transform=gfc.transform)
            window = window.round_offsets(op="floor").round_lengths(op="ceil")
            forest_2020 = gfc.read(1, window=window)
            if forest_2020.size == 0:
                continue
            transform = gfc.window_transform(window)
            west, south, east, north = gfc.window_bounds(window)

            with rasterio.open("/vsicurl/" + HANSEN_URL.format(tile=tile)) as hansen:
                # Same ground bounds as the GFC window; boundless with 0 (= no loss)
                # in case the window pokes past the tile edge by a fraction of a pixel.
                hansen_window = from_bounds(west, south, east, north, transform=hansen.transform)
                loss_year = hansen.read(
                    1,
                    window=hansen_window,
                    out_shape=forest_2020.shape,
                    resampling=Resampling.nearest,
                    boundless=True,
                    fill_value=0,
                )

            inside = geometry_mask([geom], out_shape=forest_2020.shape, transform=transform, invert=True)
            centre_lat = (south + north) / 2
            pixel_ha = forest.pixel_area_ha(centre_lat, abs(transform.e))
            parts.append(forest.tally(forest_2020, loss_year, inside, pixel_ha))
            tiles_used.append(tile)
    return forest.merge_tallies(parts), tiles_used


def plot_rasters(geom: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """GFC2020, Hansen loss year and plot mask around a single-tile plot, for ``forest.render_map``.

    The window is padded by 15 % of the plot size on each side so the map shows
    the plot in context. Only single-tile plots are supported, which keeps the
    code simple and is the normal case for a farm.

    Args:
        geom: Plot Polygon/MultiPolygon in WGS84 lon/lat.

    Returns:
        (forest_2020, loss_year, inside) on the same 10 m grid.

    Raises:
        ValueError: the plot is outside the data coverage or crosses a 10 degree tile edge.
    """
    box = forest.bbox(geom)
    _check_coverage(box)
    tiles = forest.hansen_tiles(box)
    if len(tiles) != 1:
        raise ValueError("maps are only drawn for plots inside a single 10° tile")
    tile, part = tiles[0]
    pad = max(part[2] - part[0], part[3] - part[1]) * 0.15
    padded = (part[0] - pad, part[1] - pad, part[2] + pad, part[3] + pad)
    with rasterio.Env(**GDAL_ENV), rasterio.open("/vsicurl/" + GFC2020_URL) as gfc:
        window = from_bounds(*padded, transform=gfc.transform).round_offsets(op="floor").round_lengths(op="ceil")
        forest_2020 = gfc.read(1, window=window)
        transform = gfc.window_transform(window)
        bounds = gfc.window_bounds(window)
        with rasterio.open("/vsicurl/" + HANSEN_URL.format(tile=tile)) as hansen:
            loss_year = hansen.read(
                1,
                window=from_bounds(*bounds, transform=hansen.transform),
                out_shape=forest_2020.shape,
                resampling=Resampling.nearest,
                boundless=True,
                fill_value=0,
            )
    inside = geometry_mask([geom], out_shape=forest_2020.shape, transform=transform, invert=True)
    return forest_2020, loss_year, inside


def forest_background(box: tuple[float, float, float, float], max_px: int = 900) -> tuple[np.ndarray, tuple]:
    """Fraction of GFC2020 forest per output pixel over a bbox, for the overview map background.

    A region of up to 5 x 5 degrees is ~60,000 GFC pixels across, far too many
    to download. Asking for a small ``out_shape`` lets GDAL serve the read from
    the COG's pre-computed overviews, so large areas stay cheap. Average
    resampling of the 0/1 forest mask yields the forest fraction per output
    pixel (0..1), which shades smoothly instead of the noisy speckle a nearest
    read would give.

    Args:
        box: (west, south, east, north) in degrees.
        max_px: Maximum output size in pixels along the longer side; smaller
            windows are read at native resolution (no upsampling).

    Returns:
        (fraction, (west, south, east, north)): float32 array in 0..1 and the
        exact bounds of the pixel-snapped window, to use as the image extent.
    """
    _check_coverage(box)
    with rasterio.Env(**GDAL_ENV), rasterio.open("/vsicurl/" + GFC2020_URL) as gfc:
        window = from_bounds(*box, transform=gfc.transform).round_offsets(op="floor").round_lengths(op="ceil")
        scale = max(window.width, window.height) / max_px
        shape = (max(1, round(window.height / max(scale, 1))), max(1, round(window.width / max(scale, 1))))
        fraction = gfc.read(
            1, window=window, out_shape=shape, out_dtype="float32", resampling=Resampling.average
        )
        bounds = gfc.window_bounds(window)
    return fraction, bounds
