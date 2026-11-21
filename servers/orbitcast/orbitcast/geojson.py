"""
Footprints of the forecast images as GeoJSON files. Geometry goes to files, never to the model: the tools return
only the file path and a short summary.

    forecast_with_footprints  attaches a polygon to each forecast pass: ESA's data-take polygon if one matches,
                              else the predicted swath
    forecast_geojson          AOI outline + footprints clipped around the AOI (region_box), styled by confidence
                              with simplestyle properties, so geojson.io, GitHub and QGIS show the colours
    save_geojson              writes to OUTPUT_DIR with a sanitised file name
    export_forecast           backend of export_forecast_geojson

feature, polygons_only and save_geojson are also used by coverage.py.
"""

import contextlib
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any

import shapely
import shapely.geometry
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

from .catalog import mission_satellites
from .config import CONF_COLORS, OUTPUT_DIR
from .forecast import forecast
from .geo import UTC, _iso, parse_aoi, parse_time
from .orbits import predict_geometric
from .plans import planned_takes

# Pass fields copied into the properties of each footprint feature (when present).
GEOJSON_KEYS = ("time_utc", "expected_available_utc", "satellite", "mission", "sensor_type", "direction",
                "confidence", "coverage_pct", "cloud_pct", "mode", "polarisation", "relative_orbit", "datatake",
                "incidence_deg", "off_nadir_deg")

# How far apart a timeline pass and a planned data-take / predicted swath may be and still be the same image.
PLAN_TIME_TOLERANCE = dt.timedelta(minutes=3)
ORBIT_TIME_TOLERANCE_S = 120


def region_box(aoi_geom: BaseGeometry, min_pad_deg: float = 4.0) -> Polygon:
    """
    Lon/lat box centred on the AOI, used to clip footprints to the area around it.

    S1/S2 strips are thousands of km long, so unclipped they would hide the AOI on a map. The box is widened in
    longitude at high latitudes and capped at +-85 deg latitude.

    Args:
        aoi_geom: area of interest.
        min_pad_deg: minimum margin around the AOI centre, in degrees (or 2.5x the AOI height if larger).
    """
    centroid = aoi_geom.centroid
    _, south, _, north = aoi_geom.bounds
    pad_lat = max(min_pad_deg, (north - south) * 2.5)
    pad_lon = pad_lat / max(math.cos(math.radians(centroid.y)), 0.2)
    return box(max(centroid.x - pad_lon, -180), max(centroid.y - pad_lat, -85),
               min(centroid.x + pad_lon, 180), min(centroid.y + pad_lat, 85))


def forecast_with_footprints(aoi: str, start: str = "", end: str = "", missions: list[str] | None = None,
                             max_cloud_pct: float = 30.0, min_coverage_pct: float = 20.0, days: float = 0
                             ) -> tuple[dict[str, Any], BaseGeometry,
                                        list[tuple[dict[str, Any], BaseGeometry | None, str]]]:
    """
    Run the forecast and attach a footprint polygon to each pass in its timeline.

    The footprint is ESA's planned data-take polygon when one matches the pass (same satellite, within 3 min),
    otherwise the swath from orbit prediction (within 2 min), otherwise None.
    Returns (forecast, AOI geometry, [(pass, footprint or None, footprint source)]).

    Args:
        aoi, start, end, missions, max_cloud_pct, min_coverage_pct, days: as in forecast().
    """

    forecast_result = forecast(aoi, start, end, missions, max_cloud_pct, min_coverage_pct, days)
    aoi_geom = parse_aoi(aoi)
    window_start, window_end = parse_time(forecast_result["window"][0]), parse_time(forecast_result["window"][1])
    selected_missions, satellite_names = mission_satellites(missions)
    predicted_passes, _, _ = predict_geometric(aoi_geom, window_start, window_end, satellite_names, 1.0,
                                               include_unusable=True, footprints=True)
    planned_datatakes: list[dict[str, Any]] = []

    for mission in ("S1", "S2"):
        if mission in selected_missions:
            with contextlib.suppress(Exception):  # plans are optional: fall back to orbit prediction
                planned_datatakes += planned_takes(mission, aoi_geom, window_start, window_end)[0]

    def match_footprint(timeline_pass: dict[str, Any]) -> tuple[BaseGeometry | None, str]:
        """
        (polygon, source) of the planned data-take or predicted swath matching a pass, or (None, '').

        Args:
            timeline_pass: one entry of the forecast timeline.
        """
        pass_time = parse_time(timeline_pass["time_utc"])
        for datatake in planned_datatakes:
            take_start = datatake["start"] - PLAN_TIME_TOLERANCE
            take_end = (datatake["end"] or datatake["start"]) + PLAN_TIME_TOLERANCE
            if datatake["satellite"] == timeline_pass["satellite"] and take_start <= pass_time <= take_end:
                return datatake["geom"], "ESA plan"
        for predicted in predicted_passes:
            time_gap_s = abs((parse_time(predicted["time_utc"]) - pass_time).total_seconds())
            if predicted["satellite"] == timeline_pass["satellite"] and time_gap_s < ORBIT_TIME_TOLERANCE_S:
                return predicted["footprint"], "orbit prediction"
        return None, ""

    return forecast_result, aoi_geom, [(timeline_pass, *match_footprint(timeline_pass))
                                       for timeline_pass in forecast_result["timeline"]]


def polygons_only(geom: BaseGeometry) -> BaseGeometry | None:
    """
    Keep only the polygons of a geometry (a clip can leave points and lines), as one Polygon/MultiPolygon.

    Returns None if there are none.

    Args:
        geom: any shapely geometry.
    """
    polygons = [part for part in shapely.get_parts(geom) if part.geom_type == "Polygon" and not part.is_empty]
    if not polygons:
        return None
    return polygons[0] if len(polygons) == 1 else shapely.MultiPolygon(polygons)


def round_coords(value: Any, decimals: int = 5) -> Any:
    """
    Round every float in nested dicts/lists/tuples; keeps GeoJSON files small.

    Args:
        value: number, or dict/list/tuple of them (e.g. a GeoJSON geometry).
        decimals: decimal places to keep (5 = ~1 m).
    """
    if isinstance(value, dict):
        return {key: round_coords(item, decimals) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [round_coords(item, decimals) for item in value]
    return round(value, decimals) if isinstance(value, float) else value


def feature(geom: BaseGeometry, properties: dict[str, Any], decimals: int = 5) -> dict[str, Any]:
    """
    GeoJSON Feature dict from a shapely geometry and its properties.

    Args:
        geom: the feature's geometry.
        properties: the feature's properties (pass fields, simplestyle colours...).
        decimals: decimal places kept in the coordinates.
    """
    return {"type": "Feature", "geometry": round_coords(shapely.geometry.mapping(geom), decimals),
            "properties": properties}


def forecast_geojson(aoi: str, start: str = "", end: str = "", missions: list[str] | None = None,
                     max_cloud_pct: float = 30.0, min_coverage_pct: float = 20.0, days: float = 0) -> dict[str, Any]:
    """
    Build the forecast FeatureCollection: the AOI outline plus one clipped footprint per upcoming image.

    Footprints carry the pass fields and simplestyle colours by confidence; passes without a footprint are skipped.
    Window, warnings and data sources go in the top-level "orbitcast" member.

    Args:
        aoi, start, end, missions, max_cloud_pct, min_coverage_pct, days: as in forecast().
    """

    forecast_result, aoi_geom, passes = forecast_with_footprints(aoi, start, end, missions, max_cloud_pct,
                                                                 min_coverage_pct, days)
    clip_box = region_box(aoi_geom)
    features = [feature(aoi_geom, {"role": "aoi", "label": "Area of interest", "stroke": "#000000",
                                   "stroke-width": 2.5, "fill-opacity": 0})]

    for pass_id, (timeline_pass, footprint, footprint_source) in enumerate(passes, 1):

        clipped = polygons_only(footprint.intersection(clip_box)) if footprint is not None else None
        if clipped is None:
            continue
        color = CONF_COLORS[timeline_pass["confidence"]]

        features.append(feature(clipped, {
            "role": "tasked_image", "pass_id": pass_id,
            "label": f'#{pass_id} {timeline_pass["time_utc"][:16].replace("T", " ")} {timeline_pass["satellite"]} '
                     f'{timeline_pass["confidence"]}',
            **{key: timeline_pass[key] for key in GEOJSON_KEYS if timeline_pass.get(key) is not None},
            "footprint_source": footprint_source, "stroke": color, "stroke-width": 2, "stroke-opacity": 1,
            "fill": color, "fill-opacity": 0.25}))

    provenance = forecast_result["provenance"]

    # Forecast metadata
    metadata = {
        "window_utc": forecast_result["window"], "generated_utc": _iso(dt.datetime.now(UTC)),
        "skipped": forecast_result["skipped"], "warnings": forecast_result["warnings"],
        "tle_source": provenance.get("tle_source"),
        "plan_files": [plan_file["file"] for mission in ("S1", "S2")
                       for plan_file in provenance.get(f"{mission}_plan", {}).get("plan_files", [])],
        "cloud_source": provenance.get("cloud_source")}

    return {"type": "FeatureCollection", "name": "orbitcast_forecast", "orbitcast": metadata, "features": features}


def save_geojson(geojson: dict[str, Any], filename: str, prefix: str) -> Path:
    """
    Write a GeoJSON dict to OUTPUT_DIR and return the absolute path.

    Args:
        geojson: the FeatureCollection to write.
        filename: file name; only its base name is kept and unsafe characters become "_", so the file cannot
            land outside OUTPUT_DIR.
        prefix: used for the default name `<prefix>_<UTC timestamp>.geojson` when `filename` is empty.
    """
    if filename:
        name = re.sub(r"[^\w.-]", "_", Path(filename).name)
    else:
        name = f"{prefix}_{dt.datetime.now(UTC):%Y%m%dT%H%M%S}.geojson"
    if not name.endswith(".geojson"):
        name += ".geojson"
    path = (OUTPUT_DIR / name).resolve()
    path.write_text(json.dumps(geojson))
    return path


def export_forecast(aoi: str, start: str = "", end: str = "", missions: list[str] | None = None,
                    max_cloud_pct: float = 30.0, min_coverage_pct: float = 20.0, filename: str = "",
                    days: float = 0) -> dict[str, Any]:
    """
    Write the forecast footprints to a GeoJSON file (export_forecast_geojson tool).

    Returns only what the agent needs: the file path, the number of images, a one-line summary per image and
    how to read the colours.

    Args:
        aoi, start, end, missions, max_cloud_pct, min_coverage_pct, days: as in forecast().
        filename: GeoJSON file name; default a timestamped name.
    """

    geojson = forecast_geojson(aoi, start, end, missions, max_cloud_pct, min_coverage_pct, days)
    path = save_geojson(geojson, filename, "orbitcast_forecast")
    images = [feat["properties"] for feat in geojson["features"] if feat["properties"]["role"] == "tasked_image"]

    return {"path": str(path), "images": len(images),
            "passes": [{key: image.get(key) for key in ("pass_id", "time_utc", "expected_available_utc", "satellite",
                                                         "confidence")} for image in images],
            "how_to_view": "Open in QGIS, geojson.io or GitHub. Colours: blue planned, green likely usable, orange "
                           "not confirmed, grey cloudy; black = area of interest."}
