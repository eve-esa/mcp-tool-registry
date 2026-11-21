"""
OrbitCast's geometry toolbox: the maths for turning user input and satellite positions into shapes on the Earth.

    Input       parse_aoi (bbox, 'lon,lat' point or GeoJSON -> valid polygon, size-checked), parse_time, _iso
    Geometry    area_km2 (equal-area), haversine_km, bearing_deg, destination, cross_track_km: used to build
                swath edges from the ground track and to measure coverage
    Footprints  normalize_lon splits polygons at the antimeridian so they stay valid in lon/lat
    Sensors     _ground_offset_km and view_angles convert between incidence angle and distance from the track
    Sun         sun_elevation_deg / _np: night passes are dropped for optical sensors

Conventions: angles in degrees, distances in km, times in UTC, Earth as a sphere of radius R_E.
"""

import datetime as dt
import json
import math
import re
from typing import Any

import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import Polygon, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .config import MAX_AOI_KM2

UTC = dt.UTC
FloatArray = float | np.ndarray  # functions that accept a number or a numpy array of them


R_E = 6371.0088  # mean Earth radius, km
KM_PER_DEG_LAT = 111.32  # length of one degree of latitude, km
POINT_HALF_KM = 5.0  # a point AOI becomes a 10 km x 10 km box


def _ground_offset_km(alt_km: float, incidence_deg: float) -> float:
    """
    Ground distance (km) from nadir to the point a satellite sees at a given incidence angle.

    Args:
        alt_km: satellite altitude in km.
        incidence_deg: incidence angle at the ground, in degrees.
    """
    incidence = math.radians(incidence_deg)
    # Look angle at the satellite (law of sines in the Earth centre - satellite - ground point triangle).
    look = math.asin(R_E / (R_E + alt_km) * math.sin(incidence))
    # Earth-centre angle between nadir and the point = incidence - look; times R_E gives the ground distance.
    return R_E * (incidence - look)


def _iso(when: dt.datetime) -> str:
    """
    Datetime -> compact UTC string, e.g. '2026-10-23T06:01:12Z'.

    Args:
        when: timezone-aware datetime.
    """
    return when.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(text: str | None, default: dt.datetime | None = None) -> dt.datetime | None:
    """
    ISO-8601 string -> UTC datetime (no timezone = UTC). ValueError if unreadable.

    Args:
        text: time such as '2026-10-22T12:00:00Z'.
        default: returned when `text` is empty.
    """
    if not text:
        return default
    try:
        parsed = dt.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Could not parse time '{text}'. Use ISO-8601 UTC, e.g. 2026-10-22T12:00:00Z.") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _point_box(lon: float, lat: float, half_km: float = POINT_HALF_KM) -> Polygon:
    """
    Square box around a point (a point AOI becomes a 10 km box).

    Args:
        lon, lat: centre of the box, in degrees.
        half_km: half the side of the box, in km.
    """
    half_lat = half_km / KM_PER_DEG_LAT
    # Degrees of longitude shrink with cos(latitude); the 0.01 floor avoids dividing by zero at the poles.
    half_lon = half_km / (KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
    return box(max(lon - half_lon, -180), max(lat - half_lat, -90), min(lon + half_lon, 180), min(lat + half_lat, 90))


def normalize_lon(geom: BaseGeometry) -> BaseGeometry:
    """
    Bring a footprint into [-180, 180] and split it at the antimeridian.

    ESA's polar data takes use longitudes down to -265 deg or jump across +-180; without this they would span the
    whole globe. Rings around a pole are closed along the pole.

    Args:
        geom: polygon(s) in lon/lat, possibly with longitudes outside [-180, 180].
    """
    pieces = []
    for polygon in shapely.get_parts(geom):
        if polygon.geom_type != "Polygon" or polygon.is_empty:
            continue
        lons, lats = np.asarray(polygon.exterior.coords).T
        # Unwrap: replace each longitude step by its shortest equivalent (-180..180), so the ring is continuous
        # even if it leaves [-180, 180] (e.g. 170 -> -170 becomes 170 -> 190).
        lons = lons[0] + np.concatenate([[0.0], np.cumsum((np.diff(lons) + 180) % 360 - 180)])
        if abs(lons[-1] - lons[0]) > 180:  # ring goes around a pole: close it along the pole
            pole_lat = 90.0 if lats.mean() > 0 else -90.0
            lons, lats = np.r_[lons, lons[-1], lons[0]], np.r_[lats, pole_lat, pole_lat]
        unwrapped = Polygon(np.column_stack([lons, lats])).buffer(0)
        # Cut the unwrapped polygon into 360-deg-wide windows and shift each piece back into [-180, 180].
        for shift in range(-2, 3):
            piece = unwrapped.intersection(box(-180 + 360 * shift, -90, 180 + 360 * shift, 90))
            if not piece.is_empty:
                pieces += [part for part in shapely.get_parts(shapely.affinity.translate(piece, xoff=-360 * shift))
                           if part.geom_type == "Polygon" and not part.is_empty]
    return unary_union(pieces) if pieces else Polygon()


def parse_aoi(aoi: str | list[float] | tuple[float, ...] | dict[str, Any],
              max_km2: float = MAX_AOI_KM2) -> BaseGeometry:
    """
    User area -> valid shapely polygon.

    ValueError with a fix-it message if unreadable, not a polygon, or larger than `max_km2`.

    Args:
        aoi: 'west,south,east,north', a 4-number list, 'lon,lat', or GeoJSON (text or dict). Points and
            zero-size boxes become a 10 km box.
        max_km2: largest accepted area, in km2.
    """
    # Strings: GeoJSON text, or numbers separated by commas/spaces (2 = point, 4 = bbox).
    if isinstance(aoi, str):
        text = aoi.strip()
        if text.startswith("{"):
            aoi = json.loads(text)
        else:
            numbers = [token for token in re.split(r"[,\s]+", text.strip("[]()")) if token]
            if len(numbers) == 2:
                return _point_box(float(numbers[0]), float(numbers[1]))
            if len(numbers) != 4:
                raise ValueError("aoi must be 'west,south,east,north' in degrees (e.g. the bbox from "
                                 "geocode_local), 'lon,lat', or GeoJSON. Place names are not accepted: geocode first.")
            aoi = [float(token) for token in numbers]

    # Bounding box.
    if isinstance(aoi, (list, tuple)) and len(aoi) == 4:
        west, south, east, north = map(float, aoi)
        if west == east and south == north:  # a point disguised as a bbox (GDACS event centroids)
            return _point_box(west, south)
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox must be west,south,east,north with west < east and south < north.")
        geom = box(west, south, east, north)
    # GeoJSON: FeatureCollection (merged), Feature or bare geometry.
    elif isinstance(aoi, dict):
        geojson_type = aoi.get("type")
        if geojson_type == "FeatureCollection":
            geom = unary_union([shape(feature["geometry"]) for feature in aoi["features"]])
        else:
            geom = shape(aoi["geometry"] if geojson_type == "Feature" else aoi)
        if geom.geom_type == "Point":
            return _point_box(geom.x, geom.y)
    else:
        raise ValueError("aoi must be a 'west,south,east,north' string or a GeoJSON polygon.")

    # Must be a non-empty, valid polygon within the size limit.
    if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("aoi must be a non-empty polygon.")
    geom = geom if geom.is_valid else geom.buffer(0)  # buffer(0) repairs self-intersections
    if area_km2(geom) > max_km2:
        raise ValueError(f"area is {area_km2(geom):,.0f} km2; the limit is {max_km2:,} km2. Use a smaller area.")
    return geom


def area_km2(geom: BaseGeometry, center_lon: float | None = None) -> float:
    """
    Area in km2, from a sinusoidal (equal-area) projection.

    Args:
        geom: polygon(s) in lon/lat.
        center_lon: central longitude of the projection; default the centroid of `geom`.
    """
    center_lon = geom.centroid.x if center_lon is None else center_lon

    def to_sinusoidal(lonlat: np.ndarray) -> np.ndarray:
        """
        Project points to sinusoidal x, y in km.

        Args:
            lonlat: array of (lon, lat) rows, in degrees.
        """
        lon, lat = lonlat[:, 0], lonlat[:, 1]
        return np.column_stack([R_E * np.radians(lon - center_lon) * np.cos(np.radians(lat)), R_E * np.radians(lat)])

    return shapely.transform(geom, to_sinusoidal).area


def haversine_km(lat1: FloatArray, lon1: FloatArray, lat2: FloatArray, lon2: FloatArray) -> FloatArray:
    """
    Great-circle distance in km between two points (or arrays of points).

    Args:
        lat1, lon1: first point(s), in degrees.
        lat2, lon2: second point(s), in degrees.
    """
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    half_chord = (np.sin((phi2 - phi1) / 2) ** 2
                  + np.cos(phi1) * np.cos(phi2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R_E * np.arcsin(np.sqrt(np.clip(half_chord, 0, 1)))  # clip: rounding can push it just above 1


def bearing_deg(lat1: FloatArray, lon1: FloatArray, lat2: FloatArray, lon2: FloatArray) -> FloatArray:
    """
    Direction from point 1 to point 2, in degrees clockwise from north (0-360).

    Args:
        lat1, lon1: start point(s), in degrees.
        lat2, lon2: end point(s), in degrees.
    """
    phi1, phi2, delta_lon = np.radians(lat1), np.radians(lat2), np.radians(lon2 - lon1)
    east = np.sin(delta_lon) * np.cos(phi2)
    north = np.cos(phi1) * np.sin(phi2) - np.sin(phi1) * np.cos(phi2) * np.cos(delta_lon)
    return (np.degrees(np.arctan2(east, north)) + 360) % 360


def destination(lat: FloatArray, lon: FloatArray, bearing: FloatArray,
                dist_km: FloatArray) -> tuple[FloatArray, FloatArray]:
    """
    (lat, lon) reached by travelling a distance along a bearing (longitude not wrapped).

    Args:
        lat, lon: start point(s), in degrees.
        bearing: direction of travel, degrees clockwise from north.
        dist_km: distance to travel, in km.
    """
    phi1, lambda1 = np.radians(lat), np.radians(lon)
    heading, angular_dist = np.radians(bearing), np.asarray(dist_km) / R_E
    phi2 = np.arcsin(np.sin(phi1) * np.cos(angular_dist) + np.cos(phi1) * np.sin(angular_dist) * np.cos(heading))
    lambda2 = lambda1 + np.arctan2(np.sin(heading) * np.sin(angular_dist) * np.cos(phi1),
                                   np.cos(angular_dist) - np.sin(phi1) * np.sin(phi2))
    return np.degrees(phi2), np.degrees(lambda2)


def cross_track_km(lat: float, lon: float, track_bearing: float, point_lat: float, point_lon: float) -> float:
    """
    Signed distance (km) of a point from a satellite track; positive = right of the flight direction.

    Args:
        lat, lon: a point on the track, in degrees.
        track_bearing: direction of the track at that point, degrees from north.
        point_lat, point_lon: the point to measure, in degrees.
    """
    angular_dist = haversine_km(lat, lon, point_lat, point_lon) / R_E  # track position -> point
    bearing_to_point = np.radians(bearing_deg(lat, lon, point_lat, point_lon))
    # Sideways component of that distance, relative to the track direction.
    return float(np.arcsin(np.sin(angular_dist) * np.sin(bearing_to_point - np.radians(track_bearing))) * R_E)


def sun_elevation_deg(when: dt.datetime, lat: float, lon: float) -> float:
    """
    Sun elevation in degrees (NOAA formula, ~0.1 deg accuracy); below 0 = night.

    Args:
        when: time (timezone-aware).
        lat, lon: position on the ground, in degrees.
    """
    days = when.timestamp() / 86400 + 2440587.5 - 2451545.0  # days since J2000 (Julian date - 2451545)
    # Sun position along its yearly path (ecliptic longitude).
    mean_lon = (280.460 + 0.9856474 * days) % 360
    mean_anomaly = math.radians((357.528 + 0.9856003 * days) % 360)
    ecliptic_lon = math.radians(mean_lon + 1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly))
    # Convert to sky coordinates (right ascension, declination) using the tilt of the Earth's axis.
    axial_tilt = math.radians(23.439 - 4e-7 * days)
    right_ascension = math.atan2(math.cos(axial_tilt) * math.sin(ecliptic_lon), math.cos(ecliptic_lon))
    declination = math.asin(math.sin(axial_tilt) * math.sin(ecliptic_lon))
    # Hour angle: how far the Sun is from the local meridian (sidereal time + longitude - right ascension).
    hour_angle = math.radians((280.46061837 + 360.98564736629 * days) % 360 + lon) - right_ascension
    phi = math.radians(lat)
    return math.degrees(math.asin(math.sin(phi) * math.sin(declination)
                                  + math.cos(phi) * math.cos(declination) * math.cos(hour_angle)))


def sun_elevation_np(unix_s: FloatArray, lat: FloatArray, lon: FloatArray) -> np.ndarray:
    """
    Vectorised sun_elevation_deg (same steps, numpy instead of math).

    Args:
        unix_s: times as Unix seconds.
        lat, lon: positions on the ground, in degrees.
    """
    days = np.asarray(unix_s) / 86400 + 2440587.5 - 2451545.0
    mean_lon = (280.460 + 0.9856474 * days) % 360
    mean_anomaly = np.radians((357.528 + 0.9856003 * days) % 360)
    ecliptic_lon = np.radians(mean_lon + 1.915 * np.sin(mean_anomaly) + 0.020 * np.sin(2 * mean_anomaly))
    axial_tilt = np.radians(23.439 - 4e-7 * days)
    right_ascension = np.arctan2(np.cos(axial_tilt) * np.sin(ecliptic_lon), np.cos(ecliptic_lon))
    declination = np.arcsin(np.sin(axial_tilt) * np.sin(ecliptic_lon))
    hour_angle = np.radians((280.46061837 + 360.98564736629 * days) % 360 + np.asarray(lon)) - right_ascension
    phi = np.radians(lat)
    return np.degrees(np.arcsin(np.sin(phi) * np.sin(declination)
                                + np.cos(phi) * np.cos(declination) * np.cos(hour_angle)))


def view_angles(alt_km: float, offset_km: float) -> tuple[float, float]:
    """
    (off-nadir angle, incidence angle) in degrees for a ground point beside the track.

    Args:
        alt_km: satellite altitude in km.
        offset_km: distance of the point from the ground track, in km (sign ignored).
    """
    earth_angle = abs(offset_km) / R_E  # angle at the Earth centre between nadir and the point
    look = math.atan2(math.sin(earth_angle), (R_E + alt_km) / R_E - math.cos(earth_angle))  # off-nadir at satellite
    return math.degrees(look), math.degrees(look + earth_angle)  # incidence = off-nadir + Earth-centre angle
