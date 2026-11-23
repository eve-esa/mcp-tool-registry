"""
Cloud forecast at overpass time, from Open-Meteo (free, no key, up to 16 days ahead). Optical sensors only:
Sentinel-1 radar sees through clouds and never gets a cloud value.

    cloud_series   one request for the hourly cloud cover at up to 5 points inside the AOI
    cloud_at       cloud % at the pass time: interpolated between hours and averaged over the points

forecast.py compares the result with max_cloud_pct to label optical passes 'likely_usable' or 'cloudy'.
"""

import datetime as dt
from typing import Any

import httpx
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from .config import UA
from .geo import UTC, _iso

# Open-Meteo forecast API (free, no key, ~16-day horizon).
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def cloud_series(aoi: BaseGeometry) -> tuple[list[dict[str, float]], dict[str, Any]]:
    """
    Hourly cloud forecast for up to 5 points in the area, in one request.

    Returns (one {hour: cloud %} dict per point, provenance).

    Args:
        aoi: area of interest.
    """
    # Sample points: one guaranteed inside the area, plus the midpoints between the centre and each bbox corner
    sample_points = [aoi.representative_point()]
    west, south, east, north = aoi.bounds
    for corner_lon, corner_lat in ((west, south), (west, north), (east, south), (east, north)):
        midpoint = Point((corner_lon + aoi.centroid.x) / 2, (corner_lat + aoi.centroid.y) / 2)
        if aoi.contains(midpoint):
            sample_points.append(midpoint)

    # One request for all points.
    response = httpx.get(OPEN_METEO, timeout=20, headers=UA, params={
        "latitude": ",".join(f"{point.y:.3f}" for point in sample_points),
        "longitude": ",".join(f"{point.x:.3f}" for point in sample_points),
        "hourly": "cloud_cover", "forecast_days": 16, "timezone": "UTC"})

    response.raise_for_status()
    forecasts = response.json()
    forecasts = forecasts if isinstance(forecasts, list) else [forecasts]  # a single point returns a dict, not a list

    # Per point: hour ('2026-10-22T11:00') -> cloud cover in %.
    point_forecasts = [dict(zip(forecast["hourly"]["time"], forecast["hourly"]["cloud_cover"], strict=True))
                       for forecast in forecasts]

    return point_forecasts, {"cloud_source": "Open-Meteo hourly cloud_cover (best_match)",
                             "cloud_points": len(sample_points), "cloud_retrieved_utc": _iso(dt.datetime.now(UTC))}


def cloud_at(point_forecasts: list[dict[str, float]], pass_time: dt.datetime) -> int | None:
    """
    Cloud % at the pass time, interpolated between hours and averaged over the points (None if not covered).

    Args:
        point_forecasts: output of cloud_series, one {hour: cloud %} dict per point.
        pass_time: time of the satellite pass.
    """

    hour_before = pass_time.replace(minute=0, second=0, microsecond=0)
    hour_after = hour_before + dt.timedelta(hours=1)
    fraction_of_hour = (pass_time - hour_before).total_seconds() / 3600

    point_values = []
    for forecast in point_forecasts:
        cloud_before = forecast.get(hour_before.strftime("%Y-%m-%dT%H:00"))
        cloud_after = forecast.get(hour_after.strftime("%Y-%m-%dT%H:00"))
        if cloud_before is not None and cloud_after is not None:
            point_values.append(cloud_before + (cloud_after - cloud_before) * fraction_of_hour)
    return round(sum(point_values) / len(point_values)) if point_values else None
