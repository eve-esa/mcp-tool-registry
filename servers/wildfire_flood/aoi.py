"""
Area of interest (AOI) handling for the Wildfire Flood MCP server.

Turns whatever the agent passes (GeoJSON, a bounding box, or a point with a
radius) into one WGS84 Polygon/MultiPolygon plus the helpers the rest of the
server needs: bounding box, geodesic area, centroid and a stable fingerprint
for the audit trail. Pure Python, no I/O.

Each MCP server in the registry is deployed on its own, so this module does not
share code with other servers.
"""

from __future__ import annotations

import hashlib
import json
import math

EARTH_RADIUS_M = 6_371_008.8  # mean Earth radius (IUGG)
MAX_AREA_KM2 = 2_500.0  # keeps a call to a few hundred MB of pixel reads at most
DEFAULT_RADIUS_KM = 5.0
CIRCLE_VERTICES = 64

Ring = list[list[float]]


def to_area(
    geometry: dict | str | None = None,
    bbox: list[float] | str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    radius_km: float | None = None,
) -> tuple[dict, str]:
    """Build the AOI polygon from exactly one kind of input.

    Args:
        geometry: GeoJSON Polygon, MultiPolygon, Feature or one-feature
            FeatureCollection (object or JSON string), lon/lat order.
        bbox: [west, south, east, north] in degrees (list, JSON string or
            "west,south,east,north").
        latitude, longitude: Centre of a circular AOI...
        radius_km: ...and its radius (default 5 km).

    Returns:
        (geometry, how) where geometry is a GeoJSON Polygon or MultiPolygon and
        how says which input it came from, for the provenance block.

    Raises:
        ValueError: on missing, ambiguous or invalid input, or an AOI larger
            than MAX_AREA_KM2.
    """
    given = [geometry is not None, bbox is not None, latitude is not None or longitude is not None]
    if sum(given) != 1:
        raise ValueError("give exactly one of: geometry, bbox, or latitude + longitude")

    if geometry is not None:
        geom, how = _from_geojson(geometry), "GeoJSON geometry"
    elif bbox is not None:
        geom, how = _from_bbox(bbox), "bounding box"
    else:
        if latitude is None or longitude is None:
            raise ValueError("latitude and longitude must be given together")
        radius = DEFAULT_RADIUS_KM if radius_km is None else float(radius_km)
        geom, how = circle(float(latitude), float(longitude), radius), f"circle of {radius:g} km radius"

    area = area_km2(geom)
    if area > MAX_AREA_KM2:
        raise ValueError(
            f"the area is {area:,.0f} km²; the limit is {MAX_AREA_KM2:,.0f} km². Pass the bbox of one fire "
            "(get_effis_burnt_areas returns one per fire), not a whole region: outside a burn scar the "
            "before/after comparison only picks up noise"
        )
    return geom, how


def _from_geojson(geometry: dict | str) -> dict:
    """Unwrap Feature / FeatureCollection and check the geometry type."""
    geom = json.loads(geometry) if isinstance(geometry, str) else geometry
    if geom.get("type") == "FeatureCollection":
        features = geom.get("features") or []
        if len(features) != 1:
            raise ValueError("a FeatureCollection AOI must hold exactly one feature")
        geom = features[0]
    if geom.get("type") == "Feature":
        geom = geom.get("geometry") or {}
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("the AOI geometry must be a Polygon or MultiPolygon (use latitude/longitude for a point)")
    for ring in _outer_rings(geom):
        for lon, lat, *_ in ring:
            _check_lon_lat(lon, lat)
    return {"type": geom["type"], "coordinates": geom["coordinates"]}


def _from_bbox(bbox: list[float] | str) -> dict:
    """Polygon of a [west, south, east, north] box.

    Strings may be a JSON list or plain "west,south,east,north", the form the
    EFFIS burnt-area tool returns, so its bbox can be passed straight through.
    """
    if isinstance(bbox, str):
        text = bbox.strip()
        values = json.loads(text) if text.startswith("[") else text.split(",")
    else:
        values = bbox
    if len(values) != 4:
        raise ValueError("bbox must be [west, south, east, north]")
    west, south, east, north = (float(v) for v in values)
    _check_lon_lat(west, south)
    _check_lon_lat(east, north)
    if west >= east or south >= north:
        raise ValueError("bbox must be [west, south, east, north] with west < east and south < north")
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Polygon", "coordinates": [ring]}


def circle(latitude: float, longitude: float, radius_km: float) -> dict:
    """Polygon approximating a circle on the sphere (64 vertices)."""
    _check_lon_lat(longitude, latitude)
    if not 0 < radius_km <= 30:
        raise ValueError("radius_km must be between 0 and 30")
    angular = radius_km * 1000 / EARTH_RADIUS_M
    lat0, lon0 = math.radians(latitude), math.radians(longitude)
    ring = []
    for i in range(CIRCLE_VERTICES):
        bearing = 2 * math.pi * i / CIRCLE_VERTICES
        lat = math.asin(
            math.sin(lat0) * math.cos(angular) + math.cos(lat0) * math.sin(angular) * math.cos(bearing)
        )
        lon = lon0 + math.atan2(
            math.sin(bearing) * math.sin(angular) * math.cos(lat0),
            math.cos(angular) - math.sin(lat0) * math.sin(lat),
        )
        ring.append([round(math.degrees(lon), 7), round(math.degrees(lat), 7)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _check_lon_lat(lon: float, lat: float) -> None:
    """Reject coordinates outside the lon/lat range (often swapped lat/lon)."""
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"coordinate ({lon}, {lat}) is not a valid lon/lat pair")


def _outer_rings(geom: dict) -> list[Ring]:
    """Outer ring of each polygon of a Polygon or MultiPolygon."""
    polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [polygon[0] for polygon in polygons]


def _polygons(geom: dict) -> list[list[Ring]]:
    """A Polygon or MultiPolygon as a list of polygons (each a list of rings)."""
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def _ring_area_m2(ring: Ring) -> float:
    """Spherical excess area of a lon/lat ring (always positive)."""
    total = 0.0
    for (lon1, lat1, *_), (lon2, lat2, *_) in zip(ring, ring[1:], strict=False):
        total += math.radians(lon2 - lon1) * (2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2)))
    return abs(total) * EARTH_RADIUS_M**2 / 2


def area_km2(geom: dict) -> float:
    """Geodesic area of a Polygon/MultiPolygon, holes subtracted, in km²."""
    total = 0.0
    for polygon in _polygons(geom):
        total += _ring_area_m2(polygon[0]) - sum(_ring_area_m2(hole) for hole in polygon[1:])
    return total / 1e6


def bbox(geom: dict) -> tuple[float, float, float, float]:
    """(west, south, east, north) of the outer rings."""
    points = [p for ring in _outer_rings(geom) for p in ring]
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return min(lons), min(lats), max(lons), max(lats)


def centroid(geom: dict) -> tuple[float, float]:
    """(latitude, longitude) of the bbox centre; good enough to query rainfall."""
    west, south, east, north = bbox(geom)
    return (south + north) / 2, (west + east) / 2


def fingerprint(geom: dict) -> str:
    """Short SHA-256 of the canonical geometry JSON, to tie results to an input."""
    canonical = json.dumps(geom, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def point_in_ring(lon: float, lat: float, ring: Ring) -> bool:
    """Ray-casting point-in-polygon test for one ring."""
    inside = False
    for (x1, y1, *_), (x2, y2, *_) in zip(ring, ring[1:], strict=False):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def coverage(geom: dict, footprint: dict, samples: int = 12) -> float:
    """Share (0-1) of the AOI bbox sample grid that lies inside a footprint
    polygon, e.g. a Sentinel-2 tile; used to pick the tile covering the AOI best."""
    west, south, east, north = bbox(geom)
    rings = _outer_rings(footprint)
    hits = 0
    for i in range(samples):
        for j in range(samples):
            lon = west + (east - west) * (i + 0.5) / samples
            lat = south + (north - south) * (j + 0.5) / samples
            hits += any(point_in_ring(lon, lat, ring) for ring in rings)
    return hits / samples**2
