"""
Fire Detection MCP Server
==========================
An MCP server providing European wildfire detection tools via:

1. EFFIS (Copernicus Emergency Management Service) — burnt area polygons
   via OGC WFS/WMS + local shapefile cache.
2. CDSE Sentinel Hub — Sentinel-2 vegetation and burn index statistics
   for fire impact assessment.

Tools:
    geocode_place           — convert a place name to a bounding box
    get_effis_burnt_areas   — burnt area fires with bboxes
    compute_metrics         — burn-masked NDVI, NBR, BAIS2 series and VRR

Usage:
    python server.py                              # stdio transport
    python server.py --transport http --port 8000  # HTTP transport

Requirements:
    pip install "mcp[cli]>=1.2.0" httpx pyshp python-dotenv

Environment (optional ``effis/.env`` — loaded automatically):
    CDSE_CLIENT_ID, CDSE_CLIENT_SECRET — Copernicus Data Space / Sentinel Hub OAuth
    EFFIS_SHAPEFILE_DIR — path to local EFFIS ``modis.ba.poly`` shapefile directory
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re as _re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import matplotlib
import numpy as np
import pandas as pd
import tifffile
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from PIL import Image as PILImage

# Headless server: select Agg before any pyplot import (matplotlib.use docs).
matplotlib.use("Agg")

# ---------------------------------------------------------------------------
# Load .env (same directory as this file) before reading configuration
# ---------------------------------------------------------------------------

_SERVER_DIR = Path(__file__).resolve().parent
_ENV_PATH = _SERVER_DIR / ".env"
# Do not override variables already set in the process (e.g. Cursor MCP envFile).
load_dotenv(_ENV_PATH, override=False)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# EFFIS OGC endpoints (Copernicus Emergency Management Service)
# Official WMS/WFS base — confirmed at:
# https://forest-fire.emergency.copernicus.eu/downloads-instructions
EFFIS_OWS = "https://maps.effis.emergency.copernicus.eu/effis"


# CDSE Sentinel Hub (Copernicus Data Space Ecosystem)
# Register at https://dataspace.copernicus.eu/ then create OAuth credentials
# at https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings
def _cdse_client_id() -> str:
    return os.getenv("CDSE_CLIENT_ID", "")


def _cdse_client_secret() -> str:
    return os.getenv("CDSE_CLIENT_SECRET", "")


CDSE_TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

# CDSE Sentinel Hub Catalog (STAC search for scene metadata)
CDSE_CATALOG_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"

# Local EFFIS shapefile cache (for fast historical date queries).
# Download from: https://maps.effis.emergency.copernicus.eu/effis?service=WFS&request=getfeature&typename=ms:modis.ba.poly&version=1.1.0&outputformat=SHAPEZIP
_BUNDLED_SHAPEFILE_DIR = _SERVER_DIR / "effis_layer"
EFFIS_SHAPEFILE_DIR = os.getenv("EFFIS_SHAPEFILE_DIR", "") or (
    str(_BUNDLED_SHAPEFILE_DIR) if _BUNDLED_SHAPEFILE_DIR.is_dir() else ""
)

# HTTP client timeout (seconds) for EFFIS WFS/WMS and CDSE Process API
TIMEOUT = 120.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,  # MCP stdio transport: never write to stdout
)
logger = logging.getLogger("fire-mcp")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("Fire Detection Server", host="0.0.0.0", port=8000, stateless_http=True)


# ---------------------------------------------------------------------------
# Per-request credential resolution (header → env fallback)
# ---------------------------------------------------------------------------
# MCP clients can supply upstream API credentials via custom HTTP headers so
# the server acts as a proxy using the caller's own accounts.  When headers
# are absent (e.g. stdio transport or header not sent) the server falls back
# to the process-level env vars (CDSE_CLIENT_ID / CDSE_CLIENT_SECRET).
#
# Supported headers:
#   X-CDSE-Client-Id      — Copernicus Data Space client ID
#   X-CDSE-Client-Secret  — Copernicus Data Space client secret


def _get_request_headers() -> dict[str, str]:
    """Return HTTP headers from the current MCP request, or {} on stdio."""
    try:
        ctx = mcp.get_context()
        request = ctx.request_context.request
        if request is not None and hasattr(request, "headers"):
            return dict(request.headers)
    except Exception:
        pass
    return {}


def _resolve_cdse_creds() -> tuple[str, str]:
    """Resolve CDSE credentials: prefer per-request headers, fall back to env."""
    headers = _get_request_headers()
    cid = headers.get("x-cdse-client-id", "") or _cdse_client_id()
    csec = headers.get("x-cdse-client-secret", "") or _cdse_client_secret()
    return cid, csec


# ===== Helper utilities =====================================================


async def _http_get(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Perform an async HTTP GET with sensible defaults."""
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp


def _add_months(dt: datetime, months: int) -> datetime:
    """Add (or subtract) calendar months from a datetime, clamping the day."""
    import calendar

    total = dt.year * 12 + (dt.month - 1) + months
    year = total // 12
    month = total % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


# Default directory for saving downloaded map images
# Use /tmp in cloud runtimes (ephemeral but always writable)
DEFAULT_SAVE_DIR = Path(os.getenv("DEFAULT_SAVE_DIR", "/tmp/fire_maps"))


def _tool_error(error_type: str, error: str, **extra) -> str:
    """JSON domain-error payload (successful tool content, not an MCP isError)."""
    payload = {"ok": False, "error_type": error_type, "error": error}
    payload.update(extra)
    return json.dumps(payload)


# Axis-aligned lon/lat width scales with cos(latitude); warn above this.
_BBOX_HIGH_LAT = 70.0
_BBOX_VALIDITY = (
    "Geodesic envelopes are valid away from the poles and, unless "
    "antimeridian-split, away from ±180°."
)


def _split_antimeridian_bbox(west, south, east, north):
    """If west > east after wrap, cut at ±180 (RFC 7946 §3.1.9)."""
    if west > east:
        return [
            (west, south, 180.0, north),
            (-180.0, south, east, north),
        ]
    return [(west, south, east, north)]


def _bbox_record(boxes):
    """JSON fields for one bbox, or two when split at the antimeridian."""
    west, south, east, north = boxes[0]
    rec = {
        "bbox": f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}",
        "bbox_array": [
            round(west, 4), round(south, 4), round(east, 4), round(north, 4),
        ],
    }
    notes = []
    if len(boxes) > 1:
        rec["bboxes"] = [
            f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}" for w, s, e, n in boxes
        ]
        rec["bbox_arrays"] = [
            [round(w, 4), round(s, 4), round(e, 4), round(n, 4)]
            for w, s, e, n in boxes
        ]
        rec["antimeridian_split"] = True
        notes.append(
            "Antimeridian split: pass each entry in bboxes to downstream tools."
        )
    if max(abs(south), abs(north)) >= _BBOX_HIGH_LAT:
        notes.append(
            "High-latitude envelope: axis-aligned lon/lat boxes distort "
            "physical size as meridians converge (width ∝ cos(latitude)). "
            "Wildfires are uncommon near the poles."
        )
    if notes:
        notes.append(_BBOX_VALIDITY)
        rec["warning"] = " ".join(notes)
        logger.warning("%s", rec["warning"])
    return rec


def _expand_bbox_km(west, south, east, north, buffer_km):
    """Push each side of a WGS84 bbox outward by buffer_km.

    Returns a list of one bbox, or two if the envelope crosses the
    antimeridian. Callers must use every box.

    Geod.fwd walks from a point (Karney 2013;
    https://pyproj4.github.io/pyproj/stable/api/geod.html) and normalizes
    longitude to [-180, 180].

    North/south: walk from (west, north) / (west, south). Which longitude
    we use does not change the new latitude.

    East/west: a kilometre is a different Δlon at south than at north, so
    there is no single latitude to pick. Walk both corners, unwrap any
    ±180° jump, then keep the outer longitude (axis-aligned envelope).
    Then both east corners (and both west corners) are at least buffer_km
    out.

    If that envelope inverts (west > east), split into [west, 180] and
    [-180, east] rather than min/max, which would be an invalid box or a
    ~360° strip (RFC 7946 §3.1.9).

    Valid away from the poles and, unless antimeridian-split, away from
    ±180°. At high latitudes an axis-aligned lon/lat envelope distorts
    physical width because meridians converge (Snyder 1987;
    RFC 7946 §5.3).
    """
    if buffer_km <= 0:
        return _split_antimeridian_bbox(west, south, east, north)
    from pyproj import Geod
    geod = Geod(ellps="WGS84")
    dist_m = buffer_km * 1000.0
    _, north_lat, _ = geod.fwd(west, north, 0, dist_m)
    _, south_lat, _ = geod.fwd(west, south, 180, dist_m)
    east_south, _, _ = geod.fwd(east, south, 90, dist_m)
    east_north, _, _ = geod.fwd(east, north, 90, dist_m)
    west_south, _, _ = geod.fwd(west, south, 270, dist_m)
    west_north, _, _ = geod.fwd(west, north, 270, dist_m)

    def _unwrap_east(lon0, lon1):
        return lon1 + 360.0 if (lon1 - lon0) < -180.0 else lon1

    def _unwrap_west(lon0, lon1):
        return lon1 - 360.0 if (lon1 - lon0) > 180.0 else lon1

    east_u = max(_unwrap_east(east, east_south), _unwrap_east(east, east_north))
    west_u = min(_unwrap_west(west, west_south), _unwrap_west(west, west_north))
    east_lon = east_u - 360.0 if east_u > 180.0 else east_u
    west_lon = west_u + 360.0 if west_u < -180.0 else west_u
    south_lat = max(-90.0, min(90.0, south_lat))
    north_lat = max(-90.0, min(90.0, north_lat))
    return _split_antimeridian_bbox(west_lon, south_lat, east_lon, north_lat)


def _bbox_hash8(bbox: str, extra: str = "") -> str:
    return hashlib.sha1(f"{bbox}|{extra}".encode()).hexdigest()[:8]



def _read_burnt_areas_from_shapefile(
    shapefile_dir: str,
    target_date: str | None = None,
    bbox: str | None = None,
    max_features: int = 50,
) -> dict:
    """
    Read burnt area features from a local EFFIS shapefile.

    Uses pyshp to read the .shp/.dbf/.shx files downloaded from:
    https://maps.effis.emergency.copernicus.eu/effis?service=WFS&
    request=getfeature&typename=ms:modis.ba.poly&version=1.1.0&
    outputformat=SHAPEZIP

    Args:
        shapefile_dir: Path to directory containing modis.ba.poly.shp etc.
        target_date:   Filter to this date prefix (e.g. "2023-08-22", "2025-07", "2023").
        bbox:          Optional "west,south,east,north" bounding box filter.
        max_features:  Maximum features to return.

    Returns:
        GeoJSON FeatureCollection dict.
    """
    import shapefile  # pyshp

    shp_path = Path(shapefile_dir)
    # Find the .shp file
    shp_files = list(shp_path.glob("*.shp"))
    if not shp_files:
        raise FileNotFoundError(f"No .shp file found in {shapefile_dir}")

    # Parse bbox if provided
    bbox_filter = None
    if bbox:
        parts = [float(x) for x in bbox.split(",")]
        if len(parts) == 4:
            bbox_filter = (parts[0], parts[1], parts[2], parts[3])  # W, S, E, N

    sf = shapefile.Reader(str(shp_files[0]), encoding="utf-8", encodingErrors="replace")
    fields = [f[0] for f in sf.fields[1:]]  # skip DeletionFlag
    firedate_idx = fields.index("FIREDATE") if "FIREDATE" in fields else None

    features = []
    for i, rec in enumerate(sf.iterRecords()):
        # Date filter
        if target_date and firedate_idx is not None:
            fdate = str(rec[firedate_idx])
            if not fdate.startswith(target_date):
                continue

        # BBOX filter (rough — check record bbox against query bbox)
        if bbox_filter:
            try:
                shape = sf.shape(i)
                sb = shape.bbox  # (minx, miny, maxx, maxy) — but shapefile coords
                # shapefile bbox: (min_lon, min_lat, max_lon, max_lat)
                # Note: EFFIS shapefile is in EPSG:4326
                if sb[2] < bbox_filter[0] or sb[0] > bbox_filter[2] or sb[3] < bbox_filter[1] or sb[1] > bbox_filter[3]:
                    continue
            except Exception:
                pass

        # Build GeoJSON feature
        props = {}
        for j, fname in enumerate(fields):
            val = rec[j]
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            elif isinstance(val, (bytes, bytearray)):
                val = val.decode("utf-8", errors="replace")
            props[fname] = val

        # Read geometry
        try:
            shape = sf.shape(i)
            geom = shape.__geo_interface__
        except Exception:
            geom = None

        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": geom,
            }
        )

        if len(features) >= max_features:
            break

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def _gml_to_geojson(gml_text: str) -> dict:
    """
    Convert MapServer GML2 response to a GeoJSON FeatureCollection.

    Parses both properties (text elements) and geometry (gml:coordinates
    inside gml:Polygon / gml:MultiPolygon / gml:Point).
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(gml_text)
    ns = {
        "gml": "http://www.opengis.net/gml",
        "ms": "http://mapserver.gis.umn.edu/mapserver",
    }

    geojson_features = []

    members = root.findall(".//gml:featureMember", ns) or root.findall(".//{http://www.opengis.net/gml}featureMember")

    for member in members:
        feat_elem = member[0] if len(member) > 0 else member
        properties = {}
        geometry = None

        for child in feat_elem:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

            # Check if this child contains a GML geometry
            geom = _parse_gml_geometry(child, ns)
            if geom:
                geometry = geom
            elif child.text and child.text.strip():
                properties[tag] = child.text.strip()

        geojson_features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": geometry,
            }
        )

    return {
        "type": "FeatureCollection",
        "features": geojson_features,
    }


def _parse_gml_geometry(elem, ns: dict) -> dict | None:
    """Parse a GML geometry element into GeoJSON geometry."""

    # Look for gml:MultiPolygon
    multi_poly = elem.find(".//gml:MultiPolygon", ns)
    if multi_poly is not None:
        polygons = []
        for poly_member in multi_poly.findall(".//gml:Polygon", ns):
            rings = _parse_polygon_rings(poly_member, ns)
            if rings:
                polygons.append(rings)
        if polygons:
            return {"type": "MultiPolygon", "coordinates": polygons}

    # Look for gml:Polygon
    polygon = elem.find(".//gml:Polygon", ns)
    if polygon is not None:
        rings = _parse_polygon_rings(polygon, ns)
        if rings:
            return {"type": "Polygon", "coordinates": rings}

    # Look for gml:MultiPoint
    multi_point = elem.find(".//gml:MultiPoint", ns)
    if multi_point is not None:
        coords_elems = multi_point.findall(".//gml:coordinates", ns)
        points = []
        for ce in coords_elems:
            parsed = _parse_coordinates(ce.text)
            if parsed:
                points.append(parsed[0])
        if points:
            return {"type": "MultiPoint", "coordinates": points}

    # Look for gml:Point
    point = elem.find(".//gml:Point", ns)
    if point is not None:
        coords_elem = point.find(".//gml:coordinates", ns)
        if coords_elem is not None and coords_elem.text:
            parsed = _parse_coordinates(coords_elem.text)
            if parsed:
                return {"type": "Point", "coordinates": parsed[0]}

    # Look for gml:MultiLineString
    multi_line = elem.find(".//gml:MultiLineString", ns)
    if multi_line is not None:
        lines = []
        for coords_elem in multi_line.findall(".//gml:coordinates", ns):
            parsed = _parse_coordinates(coords_elem.text)
            if parsed:
                lines.append(parsed)
        if lines:
            return {"type": "MultiLineString", "coordinates": lines}

    return None


def _parse_polygon_rings(polygon_elem, ns: dict) -> list:
    """Parse outer + inner rings of a GML Polygon."""
    rings = []
    # Outer boundary
    outer = polygon_elem.find(".//gml:outerBoundaryIs//gml:coordinates", ns)
    if outer is not None and outer.text:
        rings.append(_parse_coordinates(outer.text))
    else:
        # Try direct coordinates under the polygon
        coords = polygon_elem.find(".//gml:coordinates", ns)
        if coords is not None and coords.text:
            rings.append(_parse_coordinates(coords.text))
    # Inner boundaries (holes)
    for inner in polygon_elem.findall(".//gml:innerBoundaryIs//gml:coordinates", ns):
        if inner.text:
            rings.append(_parse_coordinates(inner.text))
    return rings


def _parse_coordinates(text: str) -> list:
    """
    Parse GML coordinates text into a list of [lon, lat] pairs.

    WFS 1.1.0 with SRSNAME=EPSG:4326 returns coordinates as lat,lon
    (Y,X per the EPSG:4326 axis order spec). GeoJSON requires lon,lat
    (X,Y). So we swap them.
    """
    coords = []
    for pair in text.strip().split():
        parts = pair.split(",")
        if len(parts) >= 2:
            try:
                # GML gives lat,lon → swap to lon,lat for GeoJSON
                coords.append([float(parts[1]), float(parts[0])])
            except ValueError:
                continue
    return coords


# ===== Tool 0: Geocode Place Name ===========================================

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "FireDetectionMCPServer/1.0 (wildfire-research)"


@mcp.tool()
async def geocode_place(
    place_name: str,
    buffer_km: float = 0.0,
    limit: int = 5,
) -> str:
    """
    Convert a place name to a bounding box for use with the fire tools.

    Geocodes a place name (city, region, country, landmark, etc.) into a
    bounding box string in "west,south,east,north" format — the same format
    accepted by get_effis_burnt_areas and other tools.

    Uses the OpenStreetMap Nominatim API (free, no API key required).

    Args:
        place_name: Name of the place to geocode (e.g. "Greece",
                    "Athens", "Evia island", "Peloponnese").
        buffer_km:  Optional buffer in km to expand the bbox (default 0).
                    Expanded with a WGS84 geodesic (pyproj Geod.fwd), not a
                    fixed degrees-per-km approximation. A buffer that crosses
                    ±180° is split into two boxes.
        limit:      Maximum number of candidate results to return (default 5).

    Returns:
        JSON with the top result's bbox string (ready to pass to other tools)
        and all candidate matches. If a geodesic buffer crosses the
        antimeridian, the result includes ``bboxes`` (both halves) and
        ``antimeridian_split``.
    """
    logger.info("Geocoding place: %s", place_name)

    try:
        resp = await _http_get(
            NOMINATIM_SEARCH_URL,
            params={
                "q": place_name,
                "format": "json",
                "limit": str(limit),
                "addressdetails": "1",
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
        )
        results = resp.json()
    except Exception as exc:
        return json.dumps({"error": f"Geocoding failed: {exc}"})

    if not results:
        return json.dumps(
            {
                "error": f"No results found for '{place_name}'.",
                "place_name": place_name,
            }
        )

    def _parse_result(r: dict) -> dict:
        # Nominatim boundingbox is [south, north, west, east]
        bb = r.get("boundingbox", [])
        rec = _bbox_record(_expand_bbox_km(
            float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1]), buffer_km,
        ))
        rec.update({
            "display_name": r.get("display_name", ""),
            "lat": float(r.get("lat", 0)),
            "lon": float(r.get("lon", 0)),
            "osm_type": r.get("osm_type", ""),
            "class": r.get("class", ""),
            "type": r.get("type", ""),
        })
        return rec

    parsed = [_parse_result(r) for r in results]

    return json.dumps(
        {
            "place_name": place_name,
            "top_result": parsed[0],
            "all_results": parsed,
            "total_results": len(parsed),
        }
    )


# ===== Tool 1: EFFIS Burnt Areas (Copernicus) ==============================


@mcp.tool()
async def get_effis_burnt_areas(
    bbox: str = "-18,27,42,72",
    max_features: int = 50,
    date: str | None = None,
    buffer_km: float = 5.0,
    save_dir: str | None = None,
    shapefile_dir: str | None = None,
) -> str:
    """
    Detect burnt areas from EFFIS and return per-fire bboxes + metadata.

    Fetches burnt area polygons from EFFIS, saves the full GeoJSON to disk,
    and returns a compact list of fires sorted by area (largest first).
    Each fire entry includes a bbox string ready to pass directly to
    compute_metrics or other downstream tools.

    Workflow:
        1. Call get_effis_burnt_areas(date=...) → fires with bboxes
        2. Call compute_metrics(bbox=fires[0].bbox) → NDVI/BAIS2 time series

    Data sources:
      - "today"/"week"/"month" → pre-filtered WFS layers (live data)
      - Date (e.g. "2023-08-22", "2023-08", "2023") → local shapefile if available,
        otherwise paginated WFS
      - None → full fire season from WFS

    Args:
        bbox:           Bounding box "west,south,east,north" (default: Europe).
        max_features:   Maximum features to return (default 50).
        date:           Date filter. Options:
                        - "today"       → fires in the last 24 hours
                        - "week"        → fires in the last 7 days
                        - "month"       → fires in the last 30 days
                        - "2025-07-29"  → fires on that specific date
                        - "2025-07"     → fires on that specific month
                        - "2023"        → fires on that specific year
                        - None          → full fire season (default)
        buffer_km:      Buffer around each fire polygon in km (default 5).
                        Applied with a WGS84 geodesic when computing per-fire
                        bounding boxes. A buffer that crosses ±180° is split
                        into two boxes.
        save_dir:       Directory to save files. Defaults to ~/fire_maps/.
        shapefile_dir:  Path to local EFFIS shapefile directory (containing
                        modis.ba.poly.shp). Overrides EFFIS_SHAPEFILE_DIR env var.
                        Used for fast historical date queries.

    Returns:
        JSON with a list of fires (bbox, area_ha, commune, country,
        firedate), saved file paths, and WMS map image info. Fires that
        cross the antimeridian also include ``bboxes`` and
        ``antimeridian_split``.
    """

    # Resolve date shortcuts and build WFS params
    wfs_layer = "ms:modis.ba.poly"
    date_label = "season"
    use_sortby = False

    if date:
        d = date.strip().lower()
        if d == "today":
            wfs_layer = "ms:modis.ba.poly.today"
            date_label = "today"
        elif d == "week":
            wfs_layer = "ms:modis.ba.poly.week"
            date_label = "last_7_days"
        elif d == "month":
            wfs_layer = "ms:modis.ba.poly.month"
            date_label = "last_30_days"
        else:
            date_label = d
            use_sortby = True

    # ── WFS: vector data as GeoJSON ──
    #
    # NOTE on EFFIS MapServer limitations (verified March 2026):
    #   - FILTER param: silently ignored (not applied, no error)
    #   - CQL_FILTER: ignored (MapServer doesn't support CQL)
    #   - TIME param on WFS: ignored (only works on WMS)
    #   - POST XML GetFeature: rejected ("POST request is empty")
    #
    # What DOES work:
    #   - Pre-filtered layers: ms:modis.ba.poly.today/week/month
    #   - SORTBY=FIREDATE D (newest-first ordering)
    #   - STARTINDEX for pagination
    #
    # For specific dates, prefer local shapefile over WFS pagination.
    # For today/week/month, always use live WFS (pre-filtered layers).

    logger.info(
        "EFFIS burnt areas: wfs_layer=%s bbox=%s date=%s",
        wfs_layer,
        bbox,
        date_label,
    )

    result: dict[str, Any] = {
        "source": "EFFIS — Burnt Area Polygons",
        "date_filter": date_label,
    }

    # Resolve shapefile path
    shp_dir = shapefile_dir or EFFIS_SHAPEFILE_DIR or None

    try:
        if use_sortby and date and shp_dir:
            # ── Fast path: read from local shapefile ──
            d = date.strip()
            logger.info("  Reading from local shapefile: %s", shp_dir)
            geojson = _read_burnt_areas_from_shapefile(
                shapefile_dir=shp_dir,
                target_date=d,
                bbox=bbox,
                max_features=max_features,
            )
            result["data_source"] = "local_shapefile"
            result["shapefile_dir"] = shp_dir

        elif use_sortby and date:
            # ── Paginated fetch for a specific date ──
            # SORTBY=FIREDATE D returns newest fires first. We fetch in
            # batches, keep features matching our target date, and stop
            # once every feature in a batch is older than the target.
            #
            # Batch size is adaptive: recent dates use small batches,
            # older dates use large batches to minimize round-trips.
            d = date.strip()  # e.g. "2025-07-29", "2025-07", "2023"

            # Parse the date prefix to estimate how far back it is
            # (used only for adaptive batch sizing).
            for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
                try:
                    ref_dt = datetime.strptime(d, fmt)
                    break
                except ValueError:
                    continue
            else:
                ref_dt = datetime(2000, 1, 1)
            days_ago = (datetime.utcnow() - ref_dt).days
            if days_ago <= 7:
                batch_size = 200
            elif days_ago <= 90:
                batch_size = 500
            elif days_ago <= 365:
                batch_size = 1500
            else:
                # Very old date — use max batch to minimize requests.
                # EFFIS full season typically has 5k-30k features.
                batch_size = 5000

            offset = 0
            matched: list[dict] = []
            total_scanned = 0

            while len(matched) < max_features:
                wfs_params = {
                    "SERVICE": "WFS",
                    "REQUEST": "GetFeature",
                    "TYPENAME": wfs_layer,
                    "VERSION": "1.1.0",
                    "OUTPUTFORMAT": "GML2",
                    "BBOX": bbox,
                    "SRSNAME": "EPSG:4326",
                    "MAXFEATURES": str(batch_size),
                    "STARTINDEX": str(offset),
                    "SORTBY": "FIREDATE D",
                }
                logger.info("  Paginating: offset=%d batch=%d matched=%d", offset, batch_size, len(matched))

                resp = await _http_get(EFFIS_OWS, params=wfs_params)
                batch = _gml_to_geojson(resp.text)
                features = batch.get("features", [])

                if not features:
                    break  # No more data on server

                all_older = True
                for f in features:
                    fdate = f.get("properties", {}).get("FIREDATE", "")
                    if fdate.startswith(d):
                        matched.append(f)
                        all_older = False
                    elif fdate > d:
                        # Feature is newer than target — haven't reached it yet
                        all_older = False
                    # else: feature is older than target date

                total_scanned += len(features)

                if all_older:
                    # Every feature in this batch predates our target —
                    # we've gone past it, no point fetching more
                    break

                if len(features) < batch_size:
                    break  # Server returned fewer than requested = last page

                offset += batch_size

                # Safety cap: don't scan more than 50k features
                if offset >= 50_000:
                    logger.warning("  Hit 50k scan cap for date=%s", d)
                    break

            geojson = {
                "type": "FeatureCollection",
                "features": matched[:max_features],
            }
            n_batches = offset // batch_size + 1
            logger.info(
                "  Done: %d matches from %d scanned (%d batches of %d)",
                len(matched),
                total_scanned,
                n_batches,
                batch_size,
            )
            result["scan_info"] = {
                "batches": n_batches,
                "batch_size": batch_size,
                "total_scanned": total_scanned,
                "days_ago": days_ago,
            }

        else:
            # ── Direct fetch (pre-filtered layers or full season) ──
            wfs_params = {
                "SERVICE": "WFS",
                "REQUEST": "GetFeature",
                "TYPENAME": wfs_layer,
                "VERSION": "1.1.0",
                "OUTPUTFORMAT": "GML2",
                "BBOX": bbox,
                "SRSNAME": "EPSG:4326",
                "MAXFEATURES": str(max_features),
            }
            resp = await _http_get(EFFIS_OWS, params=wfs_params)
            geojson = _gml_to_geojson(resp.text)

        features = geojson.get("features", [])
        n_features = len(features)

        # Save full GeoJSON to file (keeps verbose polygon coordinates out
        # of the tool response that the model sees).
        out_dir = Path(save_dir) if save_dir else DEFAULT_SAVE_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        filepath = out_dir / f"burnt_areas_{date_label}_{_bbox_hash8(bbox, str(max_features))}.geojson"
        filepath.write_text(json.dumps(geojson, indent=2), encoding="utf-8")

        result["total_features"] = n_features
        result["saved_geojson"] = str(filepath)

        # Extract per-fire bounding boxes with metadata (sorted by area,
        # largest first). Each bbox is ready to pass to compute_metrics.
        fires = []
        for feat in features:
            props = feat.get("properties", {})
            geom = feat.get("geometry")
            if not geom:
                continue

            try:
                area_ha = float(props.get("AREA_HA", 0))
            except (ValueError, TypeError):
                area_ha = 0

            all_lons, all_lats = [], []
            stack = [geom.get("coordinates", [])]
            while stack:
                item = stack.pop()
                if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[0], (int, float)):
                    all_lons.append(item[0])
                    all_lats.append(item[1])
                elif isinstance(item, (list, tuple)):
                    stack.extend(item)

            if not all_lons:
                continue

            rec = _bbox_record(_expand_bbox_km(
                min(all_lons), min(all_lats), max(all_lons), max(all_lats),
                buffer_km,
            ))
            rec.update({
                "area_ha": area_ha,
                "commune": props.get("COMMUNE", props.get("PROVINCE", "")),
                "country": props.get("COUNTRY", ""),
                "firedate": props.get("FIREDATE", ""),
            })
            fires.append(rec)

        fires.sort(key=lambda f: f["area_ha"], reverse=True)
        result["buffer_km"] = buffer_km
        result["fires"] = fires

    except Exception as exc:
        result["geojson_error"] = str(exc)

    return json.dumps(result, default=str)


# ===== CDSE Authentication & Helpers ========================================


async def _cdse_get_token(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Get an OAuth2 access token from CDSE.

    When *client_id* / *client_secret* are supplied they are used directly;
    otherwise credentials are resolved via ``_resolve_cdse_creds()`` (HTTP
    request headers first, then env vars).
    """
    cid = client_id or _resolve_cdse_creds()[0]
    csec = client_secret or _resolve_cdse_creds()[1]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            CDSE_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": csec,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def _cdse_catalog_search(
    token: str,
    bbox: list[float],
    time_from: str,
    time_to: str,
    max_cloud_cover: float = 20.0,
    limit: int = 50,
) -> list[dict]:
    """
    Search the CDSE Sentinel Hub Catalog for Sentinel-2 L2A scenes.

    Cloud-cover filtering is done server-side via a CQL2 filter.
    Results are then deduplicated by acquisition date (keeping the
    least-cloudy tile per date) and sorted by cloud cover ascending.
    """
    body: dict[str, Any] = {
        "bbox": bbox,
        "datetime": f"{time_from}/{time_to}",
        "collections": ["sentinel-2-l2a"],
        "limit": limit,
        "filter": f"eo:cloud_cover <= {max_cloud_cover}",
        "filter-lang": "cql2-text",
    }

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(
            CDSE_CATALOG_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    features = data.get("features", [])

    # Deduplicate by date: multiple tiles can cover the same date,
    # keep only the least-cloudy one per acquisition date.
    best_by_date: dict[str, dict] = {}
    for feat in features:
        cc = feat.get("properties", {}).get("eo:cloud_cover", 100)
        acq_date = feat.get("properties", {}).get("datetime", "")[:10]
        if acq_date not in best_by_date or cc < best_by_date[acq_date]["cc"]:
            best_by_date[acq_date] = {"feature": feat, "cc": cc}

    result = [v["feature"] for v in best_by_date.values()]
    result.sort(key=lambda f: f.get("properties", {}).get("eo:cloud_cover", 100))
    return result


# ===== Tool 2: Compute Vegetation & Burn Metrics ============================
# Notebook pipeline: SCL classes 1/3/8/9/10/11 invalid (CDSE "Masking Out
# Cloudy Pixels", extended with defective and snow; class 2 dark-area kept),
# shared UTM Process API grid, AOI coverage pre-screen, median NDVI
# disturbance for VRR (Lin et al. 2005,
# https://doi.org/10.1016/j.foreco.2005.02.026; median compositing per
# Asam et al. 2023, https://doi.org/10.3390/rs15061631).

# ── Evalscripts and Process API helpers (SCL-aware, UTM grid) ──

# FIREMON unburned-vs-burned line (Key & Benson 2006), not a severity class.
# https://doi.org/10.2737/rmrs-gtr-164
BURN_THRESHOLD_DEFAULT = 0.10
PROCESS_MAX_PX = 2500
# Invalid inside the AOI: 1 saturated/defective, 3 cloud shadow, 8/9 cloud
# medium/high probability, 10 thin cirrus, 11 snow/ice (Sen2Cor SCL).
# CDSE "Masking Out Cloudy Pixels" drops cloud classes 8–10
# (https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Evalscript/Examples.html).
# This set also drops defective pixels and snow/ice, and keeps class 2
# ("dark area") because burned pixels can be dark.
_SCL_INVALID = (1, 3, 8, 9, 10, 11)


def _process_grid(bbox, resolution_m, width, height):
    """Shared Process API bounds + size so every download uses the same grid.

    When resolution_m > 0, request a local UTM CRS with resx/resy in metres
    (not width/height). Clamp so neither side exceeds 2500 px.
    """
    west, south, east, north = [float(x) for x in bbox]
    if resolution_m and int(resolution_m) > 0:
        from pyproj import Transformer
        lon_c = (west + east) / 2.0
        lat_c = (south + north) / 2.0
        zone = int((lon_c + 180.0) / 6.0) + 1
        zone = min(max(zone, 1), 60)
        epsg_code = (32600 if lat_c >= 0 else 32700) + zone
        transformer = Transformer.from_crs(
            "EPSG:4326", f"EPSG:{epsg_code}", always_xy=True,
        )
        x0, y0 = transformer.transform(west, south)
        x1, y1 = transformer.transform(east, north)
        xmin, xmax = min(x0, x1), max(x0, x1)
        ymin, ymax = min(y0, y1), max(y0, y1)
        res = float(resolution_m)
        nx = max((xmax - xmin) / res, 1.0)
        ny = max((ymax - ymin) / res, 1.0)
        if nx > PROCESS_MAX_PX or ny > PROCESS_MAX_PX:
            res = max(
                (xmax - xmin) / PROCESS_MAX_PX,
                (ymax - ymin) / PROCESS_MAX_PX,
            )
        crs_url = f"http://www.opengis.net/def/crs/EPSG/0/{epsg_code}"
        bounds = {
            "bbox": [xmin, ymin, xmax, ymax],
            "properties": {"crs": crs_url},
        }
        size = {"resx": res, "resy": res}
        meta = {
            "crs": f"EPSG:{epsg_code}",
            "actual_resolution_m": round(res, 4),
        }
        return bounds, size, meta
    bounds = {
        "bbox": [west, south, east, north],
        "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
    }
    size = {"width": int(width), "height": int(height)}
    meta = {"crs": "EPSG:4326", "actual_resolution_m": None}
    return bounds, size, meta


# Visualization evalscripts — produce colorized PNG images of each index
# via the Process API.
_METRIC_VIS_EVALSCRIPTS: dict[str, str] = {
    "ndvi": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(sample) {
  if (!sample.dataMask || sample.SCL === 1 || sample.SCL === 3 || sample.SCL === 8 || sample.SCL === 9 || sample.SCL === 10 || sample.SCL === 11) return [0, 0, 0];
  var ndvi = (sample.B08 - sample.B04) / (sample.B08 + sample.B04 + 1e-10);
  return colorBlend(ndvi,
    [-0.2, 0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.9],
    [[0.5,0,0], [0.8,0.2,0.1], [0.92,0.55,0.2], [0.95,0.85,0.3],
     [0.7,0.9,0.3], [0.4,0.75,0.2], [0.15,0.55,0.1], [0.05,0.3,0.02]]);
}""",
    "bais2": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B06", "B07", "B8A", "B12", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(sample) {
  if (!sample.dataMask || sample.SCL === 1 || sample.SCL === 3 || sample.SCL === 8 || sample.SCL === 9 || sample.SCL === 10 || sample.SCL === 11) return [0, 0, 0];
  var ratio = Math.max(0, (sample.B06 * sample.B07 * sample.B8A) / (sample.B04 + 1e-10));
  var swirSum = Math.max(0, sample.B12 + sample.B8A);
  var bais2 = (1 - Math.sqrt(ratio)) *
              ((sample.B12 - sample.B8A) / (Math.sqrt(swirSum) + 1e-10) + 1);
  return colorBlend(bais2,
    [-1, -0.3, 0, 0.3, 0.6, 1.0, 3.0],
    [[0,0,0.5], [0,0.3,0.8], [0.95,0.95,0.1], [0.9,0.5,0.1],
     [0.8,0.2,0.1], [0.6,0,0], [0.8,0,0.8]]);
}""",

    "nbr": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B08", "B12", "SCL", "dataMask"] }],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(sample) {
  if (!sample.dataMask || sample.SCL === 1 || sample.SCL === 3 || sample.SCL === 8 || sample.SCL === 9 || sample.SCL === 10 || sample.SCL === 11) return [0, 0, 0];
  var nbr = (sample.B08 - sample.B12) / (sample.B08 + sample.B12 + 1e-10);
  return colorBlend(nbr,
    [-0.5, -0.2, 0, 0.2, 0.4, 0.6, 0.8],
    [[0.4,0.0,0.0], [0.7,0.15,0.05], [0.9,0.45,0.15], [0.95,0.85,0.3],
     [0.7,0.9,0.3], [0.4,0.75,0.2], [0.1,0.45,0.08]]);
}""",
}

# Evalscript for per-pixel median NDVI composite (multi-temporal).
# Uses mosaicking: "ORBIT" to access all scenes in the window, computes
# the per-pixel median NDVI to suppress transient noise (clouds, shadows)
# and produce a stable baseline.
_COMPOSITE_NDVI_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"], mosaicking: "ORBIT" }],
    output: [{ id: "default", bands: 1, sampleType: "FLOAT32" }]
  };
}
function sclOk(s) {
  if (!s.dataMask) return false;
  var c = s.SCL;
  if (c === 1 || c === 3 || c === 8 || c === 9 || c === 10 || c === 11) return false;
  return true;
}
function median(arr) {
  arr.sort(function(a, b) { return a - b; });
  var mid = Math.floor(arr.length / 2);
  return arr.length % 2 !== 0 ? arr[mid] : (arr[mid - 1] + arr[mid]) / 2;
}
function evaluatePixel(samples) {
  var vals = [];
  for (var i = 0; i < samples.length; i++) {
    var s = samples[i];
    if (!sclOk(s)) continue;
    var denom = s.B08 + s.B04;
    if (denom < 0.01) continue;
    var ndvi = (s.B08 - s.B04) / denom;
    vals.push(Math.max(-1, Math.min(1, ndvi)));
  }
  if (vals.length === 0) return [NaN];
  return [median(vals)];
}"""

# Evalscript for per-pixel median NBR composite (multi-temporal).
_COMPOSITE_NBR_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B08", "B12", "SCL", "dataMask"], mosaicking: "ORBIT" }],
    output: [{ id: "default", bands: 1, sampleType: "FLOAT32" }]
  };
}
function sclOk(s) {
  if (!s.dataMask) return false;
  var c = s.SCL;
  if (c === 1 || c === 3 || c === 8 || c === 9 || c === 10 || c === 11) return false;
  return true;
}
function median(arr) {
  arr.sort(function(a, b) { return a - b; });
  var mid = Math.floor(arr.length / 2);
  return arr.length % 2 !== 0 ? arr[mid] : (arr[mid - 1] + arr[mid]) / 2;
}
function evaluatePixel(samples) {
  var vals = [];
  for (var i = 0; i < samples.length; i++) {
    var s = samples[i];
    if (!sclOk(s)) continue;
    var denom = s.B08 + s.B12;
    if (denom < 0.01) continue;
    var nbr = (s.B08 - s.B12) / denom;
    vals.push(Math.max(-1, Math.min(1, nbr)));
  }
  if (vals.length === 0) return [NaN];
  return [median(vals)];
}"""


_COMPOSITE_NDVI_MEDIAN_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"], mosaicking: "ORBIT" }],
    output: [{ id: "default", bands: 1, sampleType: "FLOAT32" }]
  };
}
function sclOk(s) {
  if (!s.dataMask) return false;
  var c = s.SCL;
  if (c === 1 || c === 3 || c === 8 || c === 9 || c === 10 || c === 11) return false;
  return true;
}
function median(arr) { arr.sort(function(a,b){return a-b;}); var m=Math.floor(arr.length/2); return arr.length%2!==0?arr[m]:(arr[m-1]+arr[m])/2; }
function evaluatePixel(samples) {
  var ndviVals = [];
  for (var i = 0; i < samples.length; i++) {
    var s = samples[i];
    if (!sclOk(s)) continue;
    var denom = s.B08 + s.B04;
    if (denom < 0.01) continue;
    ndviVals.push(Math.max(-1, Math.min(1, (s.B08 - s.B04) / denom)));
  }
  if (ndviVals.length === 0) return [NaN];
  return [median(ndviVals)];
}"""

# Evalscripts for downloading a single-scene index raster as FLOAT32 TIFF.
_INDEX_RASTER_EVALSCRIPTS: dict[str, str] = {
    "ndvi": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: [{ id: "default", bands: 1, sampleType: "FLOAT32" }]
  };
}
function evaluatePixel(s) {
  if (!s.dataMask || s.SCL === 1 || s.SCL === 3 || s.SCL === 8 || s.SCL === 9 || s.SCL === 10 || s.SCL === 11) return [NaN];
  var denom = s.B08 + s.B04;
  if (denom < 0.01) return [NaN];
  var ndvi = (s.B08 - s.B04) / denom;
  return [Math.max(-1, Math.min(1, ndvi))];
}""",
    "bais2": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B06", "B07", "B8A", "B12", "SCL", "dataMask"] }],
    output: [{ id: "default", bands: 1, sampleType: "FLOAT32" }]
  };
}
function evaluatePixel(s) {
  if (!s.dataMask || s.SCL === 1 || s.SCL === 3 || s.SCL === 8 || s.SCL === 9 || s.SCL === 10 || s.SCL === 11) return [NaN];
  if (s.B04 < 0.001 || s.B8A < 0.001) return [NaN];
  var r = (s.B06 * s.B07 * s.B8A) / s.B04;
  if (r < 0) r = 0;
  var sw = s.B12 + s.B8A;
  if (sw < 0.001) return [NaN];
  var bais2 = (1 - Math.sqrt(r)) * ((s.B12 - s.B8A) / Math.sqrt(sw) + 1);
  return [Math.max(-10, Math.min(10, bais2))];
}""",

    "nbr": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B08", "B12", "SCL", "dataMask"] }],
    output: [{ id: "default", bands: 1, sampleType: "FLOAT32" }]
  };
}
function evaluatePixel(s) {
  if (!s.dataMask || s.SCL === 1 || s.SCL === 3 || s.SCL === 8 || s.SCL === 9 || s.SCL === 10 || s.SCL === 11) return [NaN];
  var denom = s.B08 + s.B12;
  if (denom < 0.01) return [NaN];
  var nbr = (s.B08 - s.B12) / denom;
  return [Math.max(-1, Math.min(1, nbr))];
}""",
}

# Single-band SCL-only screen: 1.0 where the pixel is usable, NaN
# otherwise. Used to check AOI coverage for a scene *before* spending a
# download per requested metric on it.
_SCL_COVERAGE_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["SCL", "dataMask"] }],
    output: [{ id: "default", bands: 1, sampleType: "FLOAT32" }]
  };
}
function evaluatePixel(s) {
  if (!s.dataMask || s.SCL === 1 || s.SCL === 3 || s.SCL === 8 || s.SCL === 9 || s.SCL === 10 || s.SCL === 11) return [NaN];
  return [1.0];
}"""


def _generate_metric_plots(
    pre_fire: list[dict],
    post_fire: list[dict],
    metrics: list[str],
    fire_date: str,
    save_dir: Path,
) -> dict[str, str]:
    """Generate time-series plots for each metric. Returns {metric: filepath}."""
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fire_dt = datetime.strptime(fire_date, "%Y-%m-%d")
    save_dir.mkdir(parents=True, exist_ok=True)
    plots: dict[str, str] = {}

    for metric_name in metrics:
        fig, ax = plt.subplots(figsize=(12, 5))

        for phase_label, phase_data, color, marker in [
            ("Pre-fire", pre_fire, "#1565C0", "o"),
            ("Post-fire", post_fire, "#C62828", "s"),
        ]:
            rows: list[tuple[datetime, float, float, float]] = []
            for obs in phase_data:
                md = obs.get(metric_name)
                if not isinstance(md, dict) or md.get("mean") is None:
                    continue
                mean_val = md["mean"]
                if isinstance(mean_val, float) and (math.isnan(mean_val) or math.isinf(mean_val)):
                    continue
                p25 = md.get("p25")
                p75 = md.get("p75")
                if p25 is None or p75 is None:
                    std_val = md.get("stDev") or 0
                    if isinstance(std_val, float) and (math.isnan(std_val) or math.isinf(std_val)):
                        std_val = 0
                    p25 = mean_val - std_val
                    p75 = mean_val + std_val
                dt = datetime.strptime(obs["date"], "%Y-%m-%d")
                rows.append((dt, mean_val, float(p25), float(p75)))

            if not rows:
                continue
            rows.sort(key=lambda r: r[0])
            dates = [r[0] for r in rows]
            means = [r[1] for r in rows]
            lo = [r[2] for r in rows]
            hi = [r[3] for r in rows]

            ax.plot(
                dates, means, marker=marker, color=color,
                label=phase_label, markersize=5, linewidth=1.5,
            )
            ax.fill_between(dates, lo, hi, color=color, alpha=0.15)

        ax.axvline(
            fire_dt, color="#E65100", linestyle="--",
            linewidth=2, label="Fire date", zorder=5,
        )
        ax.set_xlabel("Date", fontsize=11)
        ax.set_ylabel(metric_name.upper(), fontsize=11)
        ax.set_title(
            f"{metric_name.upper()} \u2014 Time Series Around Fire ({fire_date})",
            fontsize=13, fontweight="bold",
        )
        ax.legend(fontsize=10)
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate(rotation=30)

        filepath = save_dir / f"{metric_name}_timeseries_{fire_date}.png"
        fig.savefig(
            str(filepath), dpi=150, bbox_inches="tight",
            facecolor="white", edgecolor="none",
        )
        plt.close(fig)
        plots[metric_name] = str(filepath)

    return plots


_METRIC_VALID_RANGE: dict[str, tuple[float, float]] = {
    "ndvi": (-1.0, 1.0),
    "nbr": (-1.0, 1.0),
    "bais2": (-5.0, 10.0),
}


def _tiff_to_numpy(
    data: bytes,
    metric: str | None = None,
) -> np.ndarray:
    """Parse a single-band FLOAT32 TIFF response into a numpy array.

    Uses *tifffile* for reliable FLOAT32 parsing (Pillow silently
    corrupts float pixel values).  Replaces non-finite values and
    values outside the physically valid range for *metric* with NaN.
    """
    arr = tifffile.imread(io.BytesIO(data)).astype(np.float32)
    arr[~np.isfinite(arr)] = np.nan

    lo, hi = _METRIC_VALID_RANGE.get(metric or "", (-10.0, 10.0))
    arr[(arr < lo) | (arr > hi)] = np.nan
    return arr


async def _download_composite(
    token: str,
    bounds: dict[str, Any],
    size: dict[str, Any],
    time_from: str,
    time_to: str,
    max_cloud_cover: float,
    metric: str,
) -> np.ndarray:
    """Download a per-pixel median composite via the Process API.

    Uses mosaicking: "ORBIT" so all cloud-free scenes in the time range
    are composited server-side into a single per-pixel median.
    """
    if metric == "ndvi":
        evalscript = _COMPOSITE_NDVI_EVALSCRIPT
    elif metric == "nbr":
        evalscript = _COMPOSITE_NBR_EVALSCRIPT
    else:
        raise ValueError(f"Unsupported composite metric: {metric}")

    req_body = {
        "input": {
            "bounds": bounds,
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": time_from, "to": time_to},
                    "maxCloudCoverage": max_cloud_cover,
                },
            }],
        },
        "output": {
            **size,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": evalscript,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(
            CDSE_PROCESS_URL,
            json=req_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
    return _tiff_to_numpy(resp.content, metric=metric)


def _compute_burn_mask(
    pre_composite: np.ndarray,
    post_composite: np.ndarray,
    threshold: float,
) -> np.ndarray:
    """Burn mask from dNBR = pre_median - post_median > threshold."""
    valid = np.isfinite(pre_composite) & np.isfinite(post_composite)
    dnbr = (pre_composite.astype(np.float64)
            - post_composite.astype(np.float64))
    return valid & (dnbr > threshold)


async def _download_index_raster(
    token: str,
    bounds: dict[str, Any],
    size: dict[str, Any],
    scene_date: str,
    metric: str,
    max_cloud_cover: float,
) -> np.ndarray:
    """Download a single-scene index raster (NDVI or BAIS2) as a numpy array."""
    scene_from = f"{scene_date}T00:00:00Z"
    scene_to = f"{scene_date}T23:59:59Z"
    req_body = {
        "input": {
            "bounds": bounds,
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": scene_from, "to": scene_to},
                    "maxCloudCoverage": max_cloud_cover,
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {
            **size,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": _INDEX_RASTER_EVALSCRIPTS[metric],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(
            CDSE_PROCESS_URL,
            json=req_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
    return _tiff_to_numpy(resp.content, metric=metric)


async def _download_scl_coverage(
    token: str,
    bounds: dict[str, Any],
    size: dict[str, Any],
    scene_date: str,
    max_cloud_cover: float,
) -> float:
    """AOI coverage of a single scene from a cheap SCL-only download.

    Screens a catalog candidate *before* downloading a full raster per
    requested metric: one small single-band request tells us whether the
    scene is worth spending NDVI/NBR/BAIS2 downloads on at all.
    """
    scene_from = f"{scene_date}T00:00:00Z"
    scene_to = f"{scene_date}T23:59:59Z"
    req_body = {
        "input": {
            "bounds": bounds,
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": scene_from, "to": scene_to},
                    "maxCloudCoverage": max_cloud_cover,
                    "mosaickingOrder": "leastCC",
                },
            }],
        },
        "output": {
            **size,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": _SCL_COVERAGE_EVALSCRIPT,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(
            CDSE_PROCESS_URL,
            json=req_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
    arr = tifffile.imread(io.BytesIO(resp.content)).astype(np.float32)
    return _aoi_coverage_fraction(arr)


# ── Recovery analysis helpers (VRR — Lin et al. 2005) ────────────────────

_COMPOSITE_MULTIBAND_EVALSCRIPT = """//VERSION=3
function setup() {
  return { input: [{ bands: ["B04", "B08", "SCL", "dataMask"], mosaicking: "ORBIT" }],
           output: [{ id: "default", bands: 2, sampleType: "FLOAT32" }] };
}
function sclOk(s) {
  if (!s.dataMask) return false;
  var c = s.SCL;
  if (c === 1 || c === 3 || c === 8 || c === 9 || c === 10 || c === 11) return false;
  return true;
}
function median(arr) { arr.sort(function(a,b){return a-b;}); var m=Math.floor(arr.length/2); return arr.length%2!==0?arr[m]:(arr[m-1]+arr[m])/2; }
function evaluatePixel(samples) {
  var b04=[], b08=[];
  for (var i=0; i<samples.length; i++) {
    var s=samples[i];
    if (!sclOk(s)) continue;
    b04.push(s.B04); b08.push(s.B08);
  }
  if (b04.length===0) return [NaN, NaN];
  return [median(b04), median(b08)];
}"""

_RECOVERY_BANDS = {"red": 0, "nir": 1}


async def _download_multiband_composite(
    token: str,
    bounds: dict[str, Any],
    size: dict[str, Any],
    time_from: str,
    time_to: str,
    max_cloud_cover: float,
) -> np.ndarray:
    """Download a RED+NIR median composite as a (2, H, W) float32 array."""
    req_body = {
        "input": {
            "bounds": bounds,
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": time_from, "to": time_to},
                    "maxCloudCoverage": max_cloud_cover,
                },
            }],
        },
        "output": {
            **size,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": _COMPOSITE_MULTIBAND_EVALSCRIPT,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(
            CDSE_PROCESS_URL, json=req_body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
    arr = tifffile.imread(io.BytesIO(resp.content)).astype(np.float32)
    arr[~np.isfinite(arr)] = np.nan
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    elif arr.ndim == 3 and arr.shape[-1] == 2:
        arr = np.transpose(arr, (2, 0, 1))
    return arr


async def _download_ndvi_median_composite(
    token: str,
    bounds: dict[str, Any],
    size: dict[str, Any],
    time_from: str,
    time_to: str,
    max_cloud_cover: float,
) -> np.ndarray:
    """SCL-cleared per-pixel median NDVI over the time window (2-D array)."""
    req_body = {
        "input": {
            "bounds": bounds,
            "data": [{
                "type": "sentinel-2-l2a",
                "dataFilter": {
                    "timeRange": {"from": time_from, "to": time_to},
                    "maxCloudCoverage": max_cloud_cover,
                },
            }],
        },
        "output": {
            **size,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": _COMPOSITE_NDVI_MEDIAN_EVALSCRIPT,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.post(
            CDSE_PROCESS_URL, json=req_body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
    return _tiff_to_numpy(resp.content, metric="ndvi")


def _compute_severity_map(
    pre_composite: np.ndarray,
    post_composite: np.ndarray,
) -> np.ndarray:
    """Classify dNBR into severity classes per Key & Benson (2006).

    1=Low (0.1-0.27), 2=Moderate-low (0.27-0.44),
    3=Moderate-high (0.44-0.66), 4=High (>=0.66).
    0=unburned/regrowth (valid pixel, dNBR < 0.1).
    5=no data (cloud/snow/defective in either composite) — kept distinct
    from 0 so a cloud/snow gap in the AOI is never displayed or counted
    as "confirmed unburned".
    """
    valid = np.isfinite(pre_composite) & np.isfinite(post_composite)
    dnbr = np.where(
        valid,
        pre_composite.astype(np.float64) - post_composite.astype(np.float64),
        0.0,
    )
    severity = np.zeros(dnbr.shape, dtype=np.uint8)
    severity[(dnbr >= 0.1) & (dnbr < 0.27)] = 1
    severity[(dnbr >= 0.27) & (dnbr < 0.44)] = 2
    severity[(dnbr >= 0.44) & (dnbr < 0.66)] = 3
    severity[dnbr >= 0.66] = 4
    severity[~valid] = 5
    return severity


def build_recovery_table(
    img_pre: np.ndarray,
    img_dist: np.ndarray,
    imgs_post: dict[str, np.ndarray],
    burn_mask: np.ndarray,
    bands: dict[str, int],
    severity_map: np.ndarray,
) -> Any:
    """Build a recovery table with VRR (%) and raw NDVI.

    VRR(%) = (NDVI_2 - NDVI_1) / (NDVI_0 - NDVI_1) x 100

    Reference: Lin, W.T. et al. (2005). Forest Ecology and Management,
    210, 55-66. https://doi.org/10.1016/j.foreco.2005.02.026

    VRR classification (Table 2):
        >100 % = Excellent  |  75-100 % = Very good  |  50-75 % = Good
        25-50 % = Average   |  0-25 % = Poor         |  <0 % = Very poor
    """
    nir_i, red_i = bands["nir"], bands["red"]

    def _as_ndvi(img: np.ndarray) -> np.ndarray:
        if img.ndim == 2:
            return img.astype(np.float64)
        if img.ndim == 3 and img.shape[0] == 1:
            return img[0].astype(np.float64)
        nir = img[nir_i].astype(np.float64)
        red = img[red_i].astype(np.float64)
        d = nir + red
        return np.where(np.abs(d) > 1e-10, (nir - red) / d, np.nan)

    ndvi_pre = _as_ndvi(img_pre)
    ndvi_dist = _as_ndvi(img_dist)

    sorted_labels = sorted(
        imgs_post.keys(),
        key=lambda lbl: int(m.group(1)) if (m := _re.search(r"(\d+)", lbl)) else 0,
    )

    severity_labels = {1: "Low", 2: "Mod-low", 3: "Mod-high", 4: "High"}
    present_classes = {}
    for cls, cls_label in severity_labels.items():
        if np.any(burn_mask & (severity_map == cls)):
            present_classes[cls] = cls_label

    _VRR_CLASSES = [
        (-np.inf, 0.0, "Very poor"), (0.0, 25.0, "Poor"),
        (25.0, 50.0, "Average"), (50.0, 75.0, "Good"),
        (75.0, 100.0, "Very good"), (100.0, np.inf, "Excellent"),
    ]

    rows: list[dict] = []
    for label in sorted_labels:
        ndvi_t = _as_ndvi(imgs_post[label])

        m_pre = ndvi_pre[burn_mask]
        m_dist = ndvi_dist[burn_mask]
        m_t = ndvi_t[burn_mask]

        denom = m_pre - m_dist
        safe_denom = np.where(np.abs(denom) > 1e-10, denom, np.nan)
        vrr = (m_t - m_dist) / safe_denom * 100.0

        vrr_mean = round(float(np.nanmean(vrr)), 2)

        finite_vrr = vrr[np.isfinite(vrr)]
        vrr_dist: dict[str, float] = {}
        for lo, hi, cat in _VRR_CLASSES:
            cnt = int(np.sum((finite_vrr >= lo) & (finite_vrr < hi)))
            vrr_dist[cat] = round(cnt / max(len(finite_vrr), 1) * 100, 1)

        row: dict[str, Any] = {
            "Time step": label,
            "NDVI": round(float(np.nanmean(m_t)), 4),
            "NDVI pre": round(float(np.nanmean(m_pre)), 4),
            "NDVI dist": round(float(np.nanmean(m_dist)), 4),
            "VRR (%)": vrr_mean,
        }
        for cat_label in ["Very poor", "Poor", "Average", "Good", "Very good", "Excellent"]:
            row[f"% {cat_label}"] = vrr_dist.get(cat_label, 0.0)

        for cls, cls_label in present_classes.items():
            cls_mask = burn_mask & (severity_map == cls)
            c_pre = ndvi_pre[cls_mask]
            c_dist = ndvi_dist[cls_mask]
            c_t = ndvi_t[cls_mask]
            c_denom = c_pre - c_dist
            c_safe = np.where(np.abs(c_denom) > 1e-10, c_denom, np.nan)
            c_vrr = (c_t - c_dist) / c_safe * 100.0
            row[f"VRR ({cls_label})"] = round(float(np.nanmean(c_vrr)), 2)

        rows.append(row)

    return pd.DataFrame(rows)


def _generate_recovery_plot(
    recovery_df: Any,
    fire_date: str,
    save_dir: Path,
) -> str | None:
    """Generate a two-panel recovery plot: raw NDVI and VRR (%)."""
    import matplotlib.pyplot as plt

    if recovery_df.empty:
        return None

    save_dir.mkdir(parents=True, exist_ok=True)

    months: list[int] = []
    for label in recovery_df["Time step"]:
        m = _re.search(r"(\d+)", label)
        months.append(int(m.group(1)) if m else 0)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 8), sharex=True)

    ndvi_pre = recovery_df["NDVI pre"].values
    ndvi_dist = recovery_df["NDVI dist"].values
    ndvi_rec = recovery_df["NDVI"].values

    ax1.plot(months, ndvi_pre, "b--", label="NDVI pre-fire", linewidth=1.5)
    ax1.axhline(ndvi_dist[0], color="red", linestyle=":", linewidth=1.2, label="NDVI disturbance")
    ax1.plot(months, ndvi_rec, "g-o", label="NDVI recovery", markersize=6, linewidth=1.5)
    ax1.fill_between(months, ndvi_rec, ndvi_pre, alpha=0.12, color="green")
    ax1.set_ylabel("NDVI (burnt pixels)")
    ax1.set_title(f"Post-Fire Recovery \u2014 {fire_date}", fontsize=13, fontweight="bold")
    ax1.legend(loc="lower right", fontsize=9)
    ax1.grid(True, alpha=0.3)

    vrr_vals = recovery_df["VRR (%)"].values
    ax2.plot(months, vrr_vals, "m-s", label="VRR (%)", markersize=6, linewidth=1.5)
    ax2.axhline(100, color="green", linestyle="--", linewidth=0.8, alpha=0.6, label="100 % (full recovery)")
    ax2.axhline(0, color="red", linestyle="--", linewidth=0.8, alpha=0.6)
    ax2.axhspan(75, 100, alpha=0.06, color="green")
    ax2.axhspan(0, 25, alpha=0.06, color="orange")
    ax2.set_xlabel("Months after fire")
    ax2.set_ylabel("VRR (%)")
    ax2.legend(loc="lower right", fontsize=9)
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    filepath = save_dir / f"recovery_{fire_date}.png"
    fig.savefig(str(filepath), dpi=150, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close(fig)
    return str(filepath)


def _masked_statistics(
    arr: np.ndarray,
    mask: np.ndarray,
    metric: str | None = None,
) -> dict:
    """Compute statistics over masked (fire-affected) pixels only.

    Values outside the physically valid range for *metric* are treated
    as nodata so they cannot corrupt the aggregation.
    """
    lo, hi = _METRIC_VALID_RANGE.get(metric or "", (-10.0, 10.0))
    masked_vals = arr[mask].astype(np.float64)
    valid = np.isfinite(masked_vals) & (masked_vals >= lo) & (masked_vals <= hi)
    n_valid = int(np.sum(valid))
    n_nodata = int(mask.sum()) - n_valid

    if n_valid == 0:
        return {
            "error": "no valid pixels in burn mask",
            "sampleCount": 0,
            "noDataCount": n_nodata,
        }

    values = masked_vals[valid]
    return {
        "mean": float(np.mean(values)),
        "stDev": float(np.std(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "p10": float(np.percentile(values, 10)),
        "p25": float(np.percentile(values, 25)),
        "p50": float(np.percentile(values, 50)),
        "p75": float(np.percentile(values, 75)),
        "p90": float(np.percentile(values, 90)),
        "sampleCount": n_valid,
        "noDataCount": n_nodata,
    }


def _aoi_coverage_fraction(arr: np.ndarray) -> float:
    """Fraction of *all* pixels in arr that are finite after SCL/dataMask
    screening, i.e. how much of the whole processed bbox grid has usable
    data — independent of where the burn mask happens to fall.

    Complements `_mask_coverage_fraction`, which only looks at burn-mask
    pixels: a tile can be ranked "least cloudy" at the granule level and
    still be mostly cloudy specifically over this AOI, so a whole-array
    check is needed in addition to a mask-restricted one.
    """
    if arr.size == 0:
        return 0.0
    return float(np.mean(np.isfinite(arr)))


def _mask_coverage_fraction(obs: dict, metric_names: list[str]) -> float:
    """Return the minimum valid-pixel fraction across all metrics.

    For each metric the fraction is sampleCount / (sampleCount + noDataCount),
    i.e. the share of burn-mask pixels that have valid satellite data in this
    scene.  The *minimum* across metrics is returned so that a scene is only
    kept when every index meets the coverage threshold.
    """
    fractions: list[float] = []
    for m in metric_names:
        mdata = obs.get(m)
        if not isinstance(mdata, dict):
            return 0.0
        n_valid = mdata.get("sampleCount", 0)
        n_nodata = mdata.get("noDataCount", 0)
        total = n_valid + n_nodata
        if total == 0:
            return 0.0
        fractions.append(n_valid / total)
    return min(fractions) if fractions else 0.0


@mcp.tool()
async def compute_metrics(
    bbox: str, fire_date: str,
    months_before: int = 3, months_after: int = 3,
    metrics: list[str] | None = None, max_cloud_cover: float = 20.0,
    resolution_m: int = 100, width: int = 512, height: int = 512,
    min_mask_coverage: float = 0.8, burn_threshold: float = BURN_THRESHOLD_DEFAULT,
    min_aoi_coverage: float = 0.5,
    recovery_months: list[int] | None = None,
    save_dir: str | None = None,
) -> str:
    """Compute spectral-index time series before and after a fire event,
    restricted to fire-affected pixels identified via a burn mask.
    Includes NDVI recovery analysis at the specified post-fire months.

    Args:
        bbox: Bounding box "west,south,east,north" (lon/lat).
        fire_date: Date of the fire event (YYYY-MM-DD).
        months_before: Months of data before the fire (default 3).
        months_after: Months of data after the fire (default 3). Also bounds the VRR disturbance (NDVI_1) median composite.
        metrics: List of metrics: "ndvi", "nbr", "bais2", "regrowth" (default: all four). "regrowth" triggers NDVI recovery analysis and saves composite images.
        max_cloud_cover: Maximum cloud cover percentage 0-100 (default 20).
        resolution_m: Pixel resolution in metres on a local UTM grid (default 100). Set to 0 to use width/height.
        width: Image width in pixels when resolution_m is 0.
        height: Image height in pixels when resolution_m is 0.
        min_mask_coverage: Min fraction (0-1) of *burn-mask* pixels with valid data after SCL screening (default 0.8). Rejects a scene/composite whose fire-affected pixels are too cloudy/snowy, even if the rest of the AOI is clear.
        min_aoi_coverage: Min fraction (0-1) of valid pixels across the *whole requested bbox* after SCL screening (default 0.5). This is the AOI-level cloud-rejection rule: applied first as a cheap pre-screen of catalog candidates (before any per-metric raster download), then again on the pre/post-fire composites used to build the burn mask and on each per-scene observation, independent of where the burn mask falls.
        burn_threshold: dNBR cutoff for the binary burn mask (default 0.10). Not a severity class.
        recovery_months: Months after fire for NDVI₂ / VRR (default [12,24,36,48]). Each date uses a ±1 month median composite. Future months are auto-skipped. Pass [] to skip. Only used when "regrowth" is in metrics.
        save_dir: Directory to save results.

    Returns:
        Compact JSON. On success, ok is true. On domain failure, ok is false with
        error_type (cdse_auth, invalid_bbox, invalid_fire_date, unknown_metric,
        burn_mask_unavailable) and error (sentence for the user). Tell the user
        spectral metrics were not computed and quote error when ok is false.
        A "warnings" list may be present even when ok is true — e.g. one phase
        had zero usable scenes after catalog search/AOI screening, so that
        phase's observations and images are empty while the other phase and
        the burn mask may still be fine. Surface these warnings to the user.
    """

    if recovery_months is None:
        recovery_months = [12, 24, 36, 48]

    cdse_cid, cdse_csec = _resolve_cdse_creds()
    if not cdse_cid or not cdse_csec:
        return _tool_error(
            "cdse_auth",
            "CDSE credentials not set. Either send them as HTTP "
            "headers (X-CDSE-Client-Id / X-CDSE-Client-Secret) or "
            "set CDSE_CLIENT_ID / CDSE_CLIENT_SECRET env vars on the server.",
        )

    try:
        parts = [float(x) for x in bbox.split(",")]
        assert len(parts) == 4
        west, south, east, north = parts
    except Exception:
        return _tool_error("invalid_bbox", f"Invalid bbox: {bbox}")

    try:
        fire_dt = datetime.strptime(fire_date, "%Y-%m-%d")
    except ValueError:
        return _tool_error(
            "invalid_fire_date",
            f"Invalid fire_date: {fire_date}. Expected YYYY-MM-DD.",
        )

    if metrics is None:
        metrics = ["ndvi", "nbr", "bais2", "regrowth"]
    valid_metrics = {"ndvi", "nbr", "bais2", "regrowth"}
    for m in metrics:
        if m not in valid_metrics:
            return _tool_error(
                "unknown_metric",
                f"Unknown metric '{m}'. Valid: {', '.join(sorted(valid_metrics))}",
            )
    run_regrowth = "regrowth" in metrics
    scene_metrics = [m for m in metrics if m != "regrowth"]

    pre_start = _add_months(fire_dt, -months_before)
    pre_end = fire_dt - timedelta(days=1)
    post_start = fire_dt + timedelta(days=1)
    post_end = _add_months(fire_dt, months_after)
    phases = [("pre_fire", pre_start, pre_end), ("post_fire", post_start, post_end)]

    try:
        token = await _cdse_get_token(cdse_cid, cdse_csec)
    except Exception as exc:
        return _tool_error("cdse_auth", f"CDSE authentication failed: {exc}")
    token_acquired = datetime.utcnow()

    async def _ensure_token() -> None:
        nonlocal token, token_acquired
        if (datetime.utcnow() - token_acquired).total_seconds() > 240:
            try:
                token = await _cdse_get_token(cdse_cid, cdse_csec)
                token_acquired = datetime.utcnow()
            except Exception as exc:
                logger.warning("Token refresh failed: %s", exc)

    logger.info("compute_metrics: fire=%s bbox=%s metrics=%s %d+%d months", fire_date, bbox, metrics, months_before, months_after)

    result: dict[str, Any] = {
        "source": "CDSE Sentinel Hub — Burn-Masked Per-Scene Statistics",
        "fire_date": fire_date, "bbox": bbox,
        "config": {
            "months_before": months_before, "months_after": months_after,
            "metrics": metrics, "max_cloud_cover": max_cloud_cover,
            "resolution_m": resolution_m,
            "width": width, "height": height,
            "min_mask_coverage": min_mask_coverage, "burn_threshold": burn_threshold,
            "min_aoi_coverage": min_aoi_coverage,
            "scl_invalid": list(_SCL_INVALID),
        },
        "pre_fire": [], "post_fire": [],
        "images": {"pre_fire": [], "post_fire": []},
        "plots": {}, "summary": {},
    }

    out_dir = Path(save_dir) if save_dir else Path(DEFAULT_SAVE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    series_dir = out_dir / f"metrics_{fire_date}_{_bbox_hash8(bbox)}"

    proc_bounds, proc_size, grid_meta = _process_grid(
        [west, south, east, north], resolution_m, width, height,
    )
    result["config"]["grid"] = grid_meta
    result["config"]["actual_resolution_m"] = grid_meta.get("actual_resolution_m")

    # ── Catalog search for both phases (parallel) ──
    async def _catalog_one(phase, phase_start, phase_end):
        time_from = phase_start.strftime("%Y-%m-%dT00:00:00Z")
        time_to = phase_end.strftime("%Y-%m-%dT23:59:59Z")
        try:
            scenes = await _cdse_catalog_search(token, [west, south, east, north], time_from, time_to, max_cloud_cover, 50)
            logger.info("  Catalog %s: %d cloud-free scenes", phase, len(scenes))
            return phase, scenes
        except Exception as exc:
            logger.warning("  Catalog search failed for %s: %s", phase, exc)
            return phase, []

    await _ensure_token()
    catalog_results = await asyncio.gather(*[_catalog_one(p, s, e) for p, s, e in phases])
    phase_scenes: dict[str, list[dict]] = dict(catalog_results)
    catalog_scene_count = sum(len(v) for v in phase_scenes.values())

    # ── AOI-level screening of catalog candidates ──
    # Catalog eo:cloud_cover and Process API leastCC only rank whole
    # granules; a granule ranked least-cloudy can still be mostly cloudy
    # specifically over this AOI. Run one cheap SCL-only download per
    # candidate scene and drop it here if it fails min_aoi_coverage,
    # before spending a download per requested metric on it.
    aoi_prescreen_discarded = 0
    if min_aoi_coverage > 0 and catalog_scene_count > 0:
        _screen_sem = asyncio.Semaphore(6)

        async def _screen_one(phase, scene):
            scene_date = scene.get("properties", {}).get("datetime", "")[:10]
            async with _screen_sem:
                try:
                    coverage = await _download_scl_coverage(
                        token, proc_bounds, proc_size, scene_date, max_cloud_cover,
                    )
                except Exception as exc:
                    logger.warning("  AOI screen failed for %s %s: %s", phase, scene_date, exc)
                    coverage = 0.0
            return phase, scene, coverage

        screen_tasks = [
            _screen_one(phase, scene)
            for phase, _, _ in phases
            for scene in phase_scenes.get(phase, [])
        ]
        await _ensure_token()
        screen_results = await asyncio.gather(*screen_tasks) if screen_tasks else []

        screened_phase_scenes: dict[str, list[dict]] = {phase: [] for phase, _, _ in phases}
        for phase, scene, coverage in screen_results:
            if coverage < min_aoi_coverage:
                aoi_prescreen_discarded += 1
                continue
            screened_phase_scenes[phase].append(scene)
        phase_scenes = screened_phase_scenes

        for phase, _, _ in phases:
            logger.info(
                "  %s: %d/%d catalog scenes pass AOI screening (min_aoi_coverage=%.2f)",
                phase, len(phase_scenes.get(phase, [])),
                len(dict(catalog_results).get(phase, [])), min_aoi_coverage,
            )

    # ── Per-phase scene availability, made explicit (not just a zero count) ──
    phase_warnings: list[str] = []
    catalog_by_phase = dict(catalog_results)
    result["catalog"] = {}
    for phase, _, _ in phases:
        found = len(catalog_by_phase.get(phase, []))
        kept = len(phase_scenes.get(phase, []))
        result["catalog"][phase] = {"found": found, "after_aoi_screening": kept}
        if kept == 0:
            if found == 0:
                phase_warnings.append(
                    f"No Sentinel-2 L2A scenes found in the catalog for {phase} "
                    f"(max_cloud_cover={max_cloud_cover}); time-series statistics "
                    f"and visualization images will be empty for this phase."
                )
            else:
                phase_warnings.append(
                    f"{found} catalog scene(s) found for {phase} but all were "
                    f"discarded by AOI cloud/snow screening (min_aoi_coverage="
                    f"{min_aoi_coverage}); time-series statistics and visualization "
                    f"images will be empty for this phase. Try a higher "
                    f"max_cloud_cover, a lower min_aoi_coverage, or a wider "
                    f"months_before/months_after window."
                )
            logger.warning("  %s", phase_warnings[-1])
    if phase_warnings:
        result["warnings"] = phase_warnings

    # ── Compute burn mask via median NBR composites ──
    pre_from = pre_start.strftime("%Y-%m-%dT00:00:00Z")
    pre_to = pre_end.strftime("%Y-%m-%dT23:59:59Z")
    post_from = post_start.strftime("%Y-%m-%dT00:00:00Z")
    post_to = post_end.strftime("%Y-%m-%dT23:59:59Z")

    pre_composite = None
    post_composite = None
    burn_mask = None
    try:
        logger.info("  Downloading pre/post-fire median NBR composites (parallel) ...")
        await _ensure_token()
        pre_composite, post_composite = await asyncio.gather(
            _download_composite(token, proc_bounds, proc_size, pre_from, pre_to, max_cloud_cover, "nbr"),
            _download_composite(token, proc_bounds, proc_size, post_from, post_to, max_cloud_cover, "nbr"),
        )

        # AOI-level check: is the composite itself usable across the whole
        # bbox, independent of where the (not-yet-known) burn mask falls?
        aoi_coverage_pre = _aoi_coverage_fraction(pre_composite)
        aoi_coverage_post = _aoi_coverage_fraction(post_composite)
        logger.info(
            "  AOI coverage: pre=%.2f post=%.2f (min_aoi_coverage=%.2f)",
            aoi_coverage_pre, aoi_coverage_post, min_aoi_coverage,
        )
        if min_aoi_coverage > 0 and (aoi_coverage_pre < min_aoi_coverage or aoi_coverage_post < min_aoi_coverage):
            raise RuntimeError(
                f"AOI cloud/snow coverage too low for a reliable burn mask "
                f"(pre={aoi_coverage_pre:.2f}, post={aoi_coverage_post:.2f}, "
                f"min_aoi_coverage={min_aoi_coverage}). Try a wider "
                f"months_before/months_after window or a higher max_cloud_cover."
            )

        series_dir.mkdir(parents=True, exist_ok=True)
        for label, comp in [("pre", pre_composite), ("post", post_composite)]:
            vis = np.nan_to_num(comp, nan=-1.0).astype(np.float64)
            vis = np.clip((vis + 1) / 2 * 255, 0, 255).astype(np.uint8)
            PILImage.fromarray(vis, mode="L").save(str(series_dir / f"composite_nbr_{label}.png"))

        if burn_threshold and burn_threshold > 0:
            burn_mask = _compute_burn_mask(pre_composite, post_composite, burn_threshold)
        else:
            burn_mask = np.isfinite(pre_composite) | np.isfinite(post_composite)

        n_burned = int(np.sum(burn_mask))
        n_total = int(burn_mask.size)
        logger.info("  Burn mask: %d/%d pixels (%.1f%%)", n_burned, n_total, 100.0 * n_burned / max(n_total, 1))

        mask_path = series_dir / "burn_mask.png"
        PILImage.fromarray((burn_mask.astype(np.uint8) * 255), mode="L").save(str(mask_path))

        result["burn_mask"] = {
            "metric": "dnbr",
            "status": "ok",
            "burned_pixels": n_burned, "total_pixels": n_total,
            "burn_fraction": round(n_burned / max(n_total, 1), 4),
            "threshold": burn_threshold,
            "aoi_coverage_pre": round(aoi_coverage_pre, 4),
            "aoi_coverage_post": round(aoi_coverage_post, 4),
            "mask_path": str(mask_path),
        }
        if n_burned == 0:
            result["burn_mask"]["warning"] = (
                "No burned pixels detected — statistics will be empty. "
                "Try lowering burn_threshold."
            )
    except Exception as exc:
        # Fail closed: missing mask ≠ burned. Do not allocate an all-True array.
        logger.error("  Burn mask computation failed: %s", exc)
        burn_mask = None
        result["ok"] = False
        result["error_type"] = "burn_mask_unavailable"
        result["error"] = str(exc)
        result["burn_mask"] = {
            "status": "unavailable",
            "error_type": "burn_mask_unavailable",
            "error": str(exc),
        }

    # ── Compute severity map from dNBR (only when the burn mask is a real array) ──
    severity_map = None
    if burn_mask is not None and pre_composite is not None and post_composite is not None:
        try:
            severity_map = _compute_severity_map(pre_composite, post_composite)
            sev_path = series_dir / "severity_map.png"
            series_dir.mkdir(parents=True, exist_ok=True)
            sev_vis = np.zeros((*severity_map.shape, 3), dtype=np.uint8)
            sev_vis[severity_map == 1] = [255, 255, 0]
            sev_vis[severity_map == 2] = [255, 165, 0]
            sev_vis[severity_map == 3] = [255, 69, 0]
            sev_vis[severity_map == 4] = [139, 0, 0]
            sev_vis[severity_map == 5] = [128, 128, 128]  # no data, distinct from unburned (black)
            PILImage.fromarray(sev_vis, mode="RGB").save(str(sev_path))
            sev_in_mask = severity_map[burn_mask]
            result["severity_map"] = {
                "path": str(sev_path),
                # burn_mask already excludes invalid pixels, so class 5
                # never appears in sev_in_mask; reported separately below
                # as a whole-AOI count, complementing aoi_coverage_pre/post.
                "no_data_pixels": int(np.sum(severity_map == 5)),
                "classes": {
                    "Low (1)": int(np.sum(sev_in_mask == 1)),
                    "Mod-low (2)": int(np.sum(sev_in_mask == 2)),
                    "Mod-high (3)": int(np.sum(sev_in_mask == 3)),
                    "High (4)": int(np.sum(sev_in_mask == 4)),
                },
            }
            logger.info("  Severity map: %s", result["severity_map"]["classes"])
        except Exception as exc:
            logger.error("  Severity map computation failed: %s", exc)
            result["severity_map"] = {"error": str(exc)}

    # ── NDVI Recovery Analysis (triggered by "regrowth" metric) ──
    if run_regrowth and recovery_months and burn_mask is not None and severity_map is not None and np.any(burn_mask):
        try:
            now = datetime.utcnow()
            valid_months = [rm for rm in recovery_months if _add_months(fire_dt, rm) <= now]
            if not valid_months:
                result["recovery"] = {"error": f"All recovery months {recovery_months} fall in the future (fire: {fire_date})"}
            else:
                if len(valid_months) < len(recovery_months):
                    skipped = sorted(set(recovery_months) - set(valid_months))
                    logger.info("  Skipping future recovery months: %s", skipped)
                recovery_months = valid_months
                logger.info("  Starting NDVI recovery analysis for months: %s", recovery_months)

                await _ensure_token()
                img_pre, img_dist_ndvi = await asyncio.gather(
                    _download_multiband_composite(
                        token, proc_bounds, proc_size, pre_from, pre_to, max_cloud_cover,
                    ),
                    _download_ndvi_median_composite(
                        token, proc_bounds, proc_size, post_from, post_to, max_cloud_cover,
                    ),
                )
                nir_band_pre = img_pre[_RECOVERY_BANDS["nir"]]
                pre_coverage = float(np.sum(np.isfinite(nir_band_pre[burn_mask]))) / max(int(np.sum(burn_mask)), 1)
                dist_coverage = float(np.sum(np.isfinite(img_dist_ndvi[burn_mask]))) / max(int(np.sum(burn_mask)), 1)
                logger.info("    Pre-fire multiband composite: coverage=%.2f", pre_coverage)
                logger.info("    Disturbance median-NDVI composite: coverage=%.2f", dist_coverage)

                if pre_coverage < min_mask_coverage or dist_coverage < min_mask_coverage:
                    result["recovery"] = {"error": f"Composite coverage too low (pre={pre_coverage:.2f}, dist={dist_coverage:.2f}, min={min_mask_coverage})"}
                else:
                    imgs_post_recovery = {}
                    for rm in sorted(recovery_months):
                        win_center = _add_months(fire_dt, rm)
                        win_start = _add_months(win_center, -1)
                        win_end = _add_months(win_center, 1)
                        t_from = win_start.strftime("%Y-%m-%dT00:00:00Z")
                        t_to = win_end.strftime("%Y-%m-%dT23:59:59Z")
                        label = f"T+{rm}mo"
                        try:
                            comp = await _download_multiband_composite(
                                token, proc_bounds, proc_size, t_from, t_to, max_cloud_cover,
                            )
                            nir_band = comp[_RECOVERY_BANDS["nir"]]
                            valid_in_mask = np.isfinite(nir_band[burn_mask])
                            coverage = float(np.sum(valid_in_mask)) / max(int(np.sum(burn_mask)), 1)
                            if coverage >= min_mask_coverage:
                                imgs_post_recovery[label] = comp
                                logger.info("    %s: OK (coverage=%.2f)", label, coverage)
                            else:
                                logger.info("    %s: discarded (coverage=%.2f < %.2f)", label, coverage, min_mask_coverage)
                        except Exception as exc:
                            logger.warning("    %s: download failed: %s", label, exc)

                    if imgs_post_recovery:
                        recovery_df = build_recovery_table(
                            img_pre, img_dist_ndvi, imgs_post_recovery, burn_mask, _RECOVERY_BANDS, severity_map,
                        )
                        recovery_dir = series_dir / "recovery"
                        recovery_dir.mkdir(parents=True, exist_ok=True)

                        def _save_ndvi_composite(img_2band, tag, out_dir):
                            nir = img_2band[_RECOVERY_BANDS["nir"]].astype(np.float64)
                            red = img_2band[_RECOVERY_BANDS["red"]].astype(np.float64)
                            denom = nir + red
                            ndvi = np.where(np.abs(denom) > 1e-10, (nir - red) / denom, np.nan)
                            vis = np.nan_to_num(ndvi, nan=-1.0)
                            vis = np.clip((vis + 1) / 2 * 255, 0, 255).astype(np.uint8)
                            path = out_dir / f"composite_ndvi_{tag}.png"
                            PILImage.fromarray(vis, mode="L").save(str(path))
                            return str(path)

                        def _save_ndvi(ndvi, tag, out_dir):
                            vis = np.nan_to_num(ndvi.astype(np.float64), nan=-1.0)
                            vis = np.clip((vis + 1) / 2 * 255, 0, 255).astype(np.uint8)
                            path = out_dir / f"composite_ndvi_{tag}.png"
                            PILImage.fromarray(vis, mode="L").save(str(path))
                            return str(path)

                        composite_paths = {}
                        composite_paths["pre_fire"] = _save_ndvi_composite(img_pre, "pre_fire", recovery_dir)
                        composite_paths["disturbance"] = _save_ndvi(img_dist_ndvi, "disturbance", recovery_dir)
                        for ts_label, ts_img in imgs_post_recovery.items():
                            safe_tag = ts_label.replace("+", "plus_").replace(" ", "_")
                            composite_paths[ts_label] = _save_ndvi_composite(ts_img, safe_tag, recovery_dir)
                        csv_path = recovery_dir / f"recovery_{fire_date}.csv"
                        recovery_df.to_csv(str(csv_path), index=False)
                        recovery_plot = _generate_recovery_plot(recovery_df, fire_date, recovery_dir)
                        result["recovery"] = {
                            "table": recovery_df.to_dict(orient="records"),
                            "csv_path": str(csv_path),
                            "plot_path": recovery_plot,
                            "composite_images": composite_paths,
                            "time_steps": list(imgs_post_recovery.keys()),
                        }
                        logger.info("  Recovery analysis complete: %d time steps", len(imgs_post_recovery))
                    else:
                        result["recovery"] = {"error": "No recovery composites passed coverage filter"}
        except Exception as exc:
            logger.error("  Recovery analysis failed: %s", exc)
            result["recovery"] = {"error": str(exc)}

    # ── Per-scene masked statistics (parallel) ──
    total_stats_obs = 0
    filtered_stats_obs = 0
    _sem = asyncio.Semaphore(6)

    if burn_mask is None:
        logger.warning("  Skipping masked stats: burn mask unavailable")
    else:
        async def _stats_one_scene(scene, phase):
            scene_date = scene.get("properties", {}).get("datetime", "")[:10]
            obs: dict[str, Any] = {"date": scene_date, "phase": phase}

            async def _one_metric(mn):
                async with _sem:
                    raster = await _download_index_raster(
                        token, proc_bounds, proc_size, scene_date, mn, max_cloud_cover,
                    )
                return mn, _masked_statistics(raster, burn_mask, metric=mn), _aoi_coverage_fraction(raster)

            metric_results = await asyncio.gather(*[_one_metric(mn) for mn in scene_metrics], return_exceptions=True)
            aoi_fracs: list[float] = []
            for mr in metric_results:
                if isinstance(mr, Exception):
                    continue
                mn, stat, aoi_frac = mr
                obs[mn] = stat
                aoi_fracs.append(aoi_frac)
            # Min across metrics: same "every index must clear the bar" logic
            # as _mask_coverage_fraction, but over the whole AOI grid.
            obs["aoi_coverage"] = min(aoi_fracs) if aoi_fracs else 0.0
            for mn in scene_metrics:
                if mn not in obs:
                    obs[mn] = {"error": "download failed"}
            return obs

        scene_tasks = []
        for phase, _, _ in phases:
            for scene in phase_scenes.get(phase, []):
                scene_tasks.append((phase, _stats_one_scene(scene, phase)))

        await _ensure_token()
        task_results = await asyncio.gather(*[t for _, t in scene_tasks]) if scene_tasks else []

        for (phase, _), obs in zip(scene_tasks, task_results, strict=True):
            total_stats_obs += 1
            coverage = _mask_coverage_fraction(obs, scene_metrics)
            obs["mask_coverage"] = round(coverage, 4)
            aoi_coverage = obs.get("aoi_coverage", 0.0)
            obs["aoi_coverage"] = round(aoi_coverage, 4)
            mask_fails = min_mask_coverage > 0 and coverage < min_mask_coverage
            aoi_fails = min_aoi_coverage > 0 and aoi_coverage < min_aoi_coverage
            if mask_fails or aoi_fails:
                filtered_stats_obs += 1
                continue
            result[phase].append(obs)
        for phase, _, _ in phases:
            logger.info("  %s -> %d observations (masked stats)", phase, len(result[phase]))

    # ── Download visualization images ──
    kept_dates = None
    if min_mask_coverage > 0 or min_aoi_coverage > 0:
        kept_dates = {obs["date"] for obs in result["pre_fire"] + result["post_fire"] if "date" in obs}

    async def _download_vis_one(phase, scene_date, metric_name, cloud_cover):
        vis_eval = _METRIC_VIS_EVALSCRIPTS.get(metric_name)
        if not vis_eval:
            return None
        input_block = {
            "bounds": proc_bounds,
            "data": [{"type": "sentinel-2-l2a", "dataFilter": {"timeRange": {"from": f"{scene_date}T00:00:00Z", "to": f"{scene_date}T23:59:59Z"}, "maxCloudCoverage": max_cloud_cover, "mosaickingOrder": "leastCC"}}],
        }
        phase_dir = series_dir / phase / metric_name
        phase_dir.mkdir(parents=True, exist_ok=True)
        async with _sem:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                resp = await client.post(
                    CDSE_PROCESS_URL,
                    json={"input": input_block, "output": {**proc_size, "responses": [{"identifier": "default", "format": {"type": "image/png"}}]}, "evalscript": vis_eval},
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
        if resp.status_code != 200:
            return None
        filepath = phase_dir / f"{metric_name}_{scene_date}.png"
        filepath.write_bytes(resp.content)
        return {"phase": phase, "date": scene_date, "metric": metric_name, "cloud_cover_pct": cloud_cover, "path": str(filepath), "size_bytes": len(resp.content)}

    vis_tasks = []
    for phase, _, _ in phases:
        for scene in phase_scenes.get(phase, []):
            scene_date = scene.get("properties", {}).get("datetime", "")[:10]
            if kept_dates is not None and scene_date not in kept_dates:
                continue
            cloud_cover = scene.get("properties", {}).get("eo:cloud_cover", -1)
            for metric_name in scene_metrics:
                vis_tasks.append(_download_vis_one(phase, scene_date, metric_name, cloud_cover))

    await _ensure_token()
    vis_results = await asyncio.gather(*vis_tasks, return_exceptions=True) if vis_tasks else []
    for vr in vis_results:
        if isinstance(vr, dict):
            phase = vr.pop("phase")
            result["images"][phase].append(vr)

    # ── Generate time-series plots ──
    try:
        plot_dir = series_dir / "plots"
        result["plots"] = _generate_metric_plots(result["pre_fire"], result["post_fire"], scene_metrics, fire_date, plot_dir)
    except Exception as exc:
        result["plots"] = {"error": str(exc)}

    # ── Compute summary ──
    def _phase_means(phase_data, mn):
        vals = [o[mn]["mean"] for o in phase_data if isinstance(o.get(mn), dict) and o[mn].get("mean") is not None and not math.isnan(o[mn]["mean"])]
        return sum(vals) / len(vals) if vals else None

    summary: dict[str, Any] = {
        "pre_fire_observations": len([o for o in result["pre_fire"] if "error" not in o]),
        "post_fire_observations": len([o for o in result["post_fire"] if "error" not in o]),
    }
    if isinstance(result.get("catalog"), dict):
        summary["catalog"] = result["catalog"]
    if result.get("warnings"):
        summary["warnings"] = result["warnings"]
    for mn in scene_metrics:
        pre_mean = _phase_means(result["pre_fire"], mn)
        post_mean = _phase_means(result["post_fire"], mn)
        summary[f"pre_fire_mean_{mn}"] = round(pre_mean, 4) if pre_mean is not None else None
        summary[f"post_fire_mean_{mn}"] = round(post_mean, 4) if post_mean is not None else None
        summary[f"{mn}_change"] = round(post_mean - pre_mean, 4) if pre_mean is not None and post_mean is not None else None
    summary["images_downloaded"] = len(result["images"].get("pre_fire", [])) + len(result["images"].get("post_fire", []))
    if min_mask_coverage > 0 or min_aoi_coverage > 0:
        summary["coverage_filter"] = {
            "min_mask_coverage": min_mask_coverage,
            "min_aoi_coverage": min_aoi_coverage,
            "catalog_scenes_before_aoi_prescreen": catalog_scene_count,
            "catalog_scenes_discarded_by_aoi_prescreen": aoi_prescreen_discarded,
            "total_observations_before_filter": total_stats_obs,
            "observations_discarded": filtered_stats_obs,
        }
    if isinstance(result.get("burn_mask"), dict):
        bm = result["burn_mask"]
        if bm.get("status") == "unavailable" or bm.get("error_type"):
            summary["burn_mask"] = {
                "status": bm.get("status", "unavailable"),
                "error_type": bm.get("error_type", "burn_mask_unavailable"),
                "error": bm.get("error"),
            }
        elif "error" not in bm:
            summary["burn_mask"] = {
                "burned_pixels": bm.get("burned_pixels"),
                "total_pixels": bm.get("total_pixels"),
                "burn_fraction": bm.get("burn_fraction"),
                "threshold": burn_threshold,
                "status": bm.get("status", "ok"),
                "aoi_coverage_pre": bm.get("aoi_coverage_pre"),
                "aoi_coverage_post": bm.get("aoi_coverage_post"),
            }
    if isinstance(result.get("severity_map"), dict) and "error" not in result.get("severity_map", {}):
        summary["severity_map"] = result["severity_map"].get("classes", {})
    if isinstance(result.get("recovery"), dict) and "error" not in result.get("recovery", {}):
        summary["recovery"] = {
            "time_steps": result["recovery"].get("time_steps", []),
            "csv_path": result["recovery"].get("csv_path"),
            "plot_path": result["recovery"].get("plot_path"),
            "composite_images": result["recovery"].get("composite_images", {}),
        }
    result["summary"] = summary

    series_dir.mkdir(parents=True, exist_ok=True)
    filepath = series_dir / f"metrics_{fire_date}.json"
    filepath.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    compact = {
        "ok": result.get("ok", True) and result.get("error_type") is None,
        "source": result["source"], "fire_date": fire_date, "bbox": bbox,
        "burn_mask": result.get("burn_mask", {}),
        "severity_map": result.get("severity_map", {}),
        "summary": summary, "plots": result.get("plots", {}),
        "images_pre_fire": len(result["images"]["pre_fire"]),
        "images_post_fire": len(result["images"]["post_fire"]),
        "saved_json": str(filepath), "save_dir": str(series_dir),
    }
    if result.get("warnings"):
        compact["warnings"] = result["warnings"]
    if result.get("error_type"):
        compact["ok"] = False
        compact["error_type"] = result["error_type"]
        compact["error"] = result.get("error")
    if isinstance(result.get("recovery"), dict) and "error" not in result.get("recovery", {}):
        compact["recovery"] = {
            "time_steps": result["recovery"]["time_steps"],
            "csv_path": result["recovery"].get("csv_path"),
            "plot_path": result["recovery"].get("plot_path"),
            "composite_images": result["recovery"].get("composite_images", {}),
            "table": result["recovery"].get("table"),
        }
    return json.dumps(compact, default=str)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fire Detection MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
        help="Transport type (default: http for AgentCore, use stdio for local MCP clients)",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport")
    args = parser.parse_args()

    mcp.settings.port = args.port

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
