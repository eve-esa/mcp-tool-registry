"""Exact Sentinel-2 L2A product context, tile timestamp and bounded public COG crop."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
import numpy as np
import rasterio
from defusedxml import ElementTree
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.windows import Window, bounds, from_bounds
from shapely.geometry import box, shape

from common import MaritimeError, iso, timestamp

CDSE = "https://stac.dataspace.copernicus.eu/v1/collections/sentinel-2-l2a/items/"
SEARCH = "https://earth-search.aws.element84.com/v1/search"


def trusted_asset(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "sentinel-cogs.s3.us-west-2.amazonaws.com"
    ):
        raise MaritimeError("source_schema_error", "Unsupported scene asset host.")
    return url


def load_scene(scene_id, bbox, client=None):
    if not re.fullmatch(
        r"S2[ABC]_MSIL2A_\d{8}T\d{6}_N\d{4}_R\d{3}_T\w{5}_\d{8}T\d{6}", scene_id
    ):
        raise MaritimeError(
            "invalid_request",
            "Use an exact ESA Sentinel-2 L2A product ID without .SAFE.",
        )
    owned = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        response = client.get(CDSE + scene_id)
        if response.status_code == 404:
            raise MaritimeError(
                "scene_unavailable",
                "Exact Sentinel-2 product was not found in Copernicus STAC.",
            )
        response.raise_for_status()
        authoritative = response.json()
        if authoritative.get("id") != scene_id:
            raise ValueError
        footprint = shape(authoritative["geometry"])
        if not footprint.intersects(box(*bbox)):
            raise MaritimeError(
                "invalid_request", "AOI does not intersect the scene footprint."
            )
        acquisition = timestamp(authoritative["properties"]["datetime"])
        day = acquisition.date().isoformat()
        response = client.post(
            SEARCH,
            json={
                "collections": ["sentinel-2-l2a"],
                "bbox": bbox,
                "datetime": f"{day}T00:00:00Z/{day}T23:59:59Z",
                "limit": 100,
            },
        )
        response.raise_for_status()
        matches = [
            f
            for f in response.json()["features"]
            if f["properties"].get("s2:product_uri", "").removesuffix(".SAFE")
            == scene_id
        ]
        if len(matches) != 1:
            raise MaritimeError(
                "imagery_unavailable",
                "No unique public COG copy of the exact ESA product was found.",
            )
        public = matches[0]
        visual = trusted_asset(public["assets"]["visual"]["href"])
        metadata_url = trusted_asset(visual.rsplit("/", 1)[0] + "/granule_metadata.xml")
        response = client.get(metadata_url)
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError
        xml = ElementTree.fromstring(response.content)
        sensing = next(
            e.text for e in xml.iter() if e.tag.split("}")[-1] == "SENSING_TIME"
        )
        reference = timestamp(sensing)
        return (
            {
                "product_id": scene_id,
                "public_item_id": public["id"],
                "catalogue_datetime": iso(acquisition),
                "reference_time": iso(reference),
                "timestamp_source": "Tile metadata SENSING_TIME; per-pixel acquisition time unverified.",
                "metadata_url": metadata_url,
                "copernicus_item_url": CDSE + scene_id,
                "public_catalogue": SEARCH,
                "visual_url": visual,
            },
            reference,
            footprint,
        )
    except httpx.HTTPError:
        raise MaritimeError(
            "source_unavailable", "Scene metadata request failed or timed out."
        ) from None
    except (KeyError, ValueError, StopIteration, ElementTree.ParseError):
        raise MaritimeError(
            "source_schema_error",
            "Scene metadata lacks a verified identity, footprint or tile timestamp.",
        ) from None
    finally:
        if owned:
            client.close()


def read_crop(visual_url, bbox, max_pixels=1600):
    try:
        with (
            rasterio.Env(
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_TIMEOUT=45,
                GDAL_HTTP_MAX_RETRY=1,
                AWS_NO_SIGN_REQUEST="YES",
            ),
            rasterio.open(trusted_asset(visual_url)) as ds,
        ):
            transform = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            projected = transform.transform_bounds(*bbox, densify_pts=21)
            window = from_bounds(*projected, ds.transform).intersection(
                Window(0, 0, ds.width, ds.height)
            )
            scale = max(window.width / max_pixels, window.height / max_pixels, 1)
            height, width = (
                max(1, int(window.height / scale)),
                max(1, int(window.width / scale)),
            )
            rgb = ds.read(
                [1, 2, 3],
                window=window,
                out_shape=(3, height, width),
                resampling=Resampling.bilinear,
            )
            extent = bounds(window, ds.transform)
            return np.moveaxis(rgb, 0, -1), extent, transform, str(ds.crs)
    except (rasterio.errors.RasterioError, ValueError):
        raise MaritimeError(
            "imagery_unavailable",
            "Exact scene COG crop could not be read; no substitute image was used.",
        ) from None
