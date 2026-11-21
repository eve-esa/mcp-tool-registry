"""
The reverse query (observed_areas tool): instead of "when is my area imaged?", "what will be imaged in this window?".

Footprints come from ESA's plans (Sentinel-1/2, all modes) up to each plan horizon, and from orbit prediction
beyond it and for the other satellites. They are written to a GeoJSON file; the model gets per-satellite counts and,
if a region is given, the % of it covered per mission and per day (overlaps counted once). Windows are limited to
10 days with a region and 3 days for the whole world (MAX_COVERAGE_DAYS).
"""

import datetime as dt
from typing import Any

from shapely.ops import unary_union

from .catalog import ACTIVE, SENSORS, available_utc, mission_satellites
from .config import CONF_COLORS, MAX_COVERAGE_DAYS, MAX_REGION_KM2
from .forecast import window
from .geo import UTC, _iso, area_km2, parse_aoi, parse_time
from .geojson import feature, polygons_only, save_geojson
from .orbits import ground_strips
from .plans import planned_takes


def observed_areas(region: str = "", start: str = "", days: float = 1.0, missions: list[str] | None = None,
                   satellite_names: list[str] | None = None, filename: str = "") -> dict[str, Any]:
    """
    Footprints imaged in a window (observed_areas tool): ESA plans up to each horizon, orbit prediction beyond
    and for other satellites.

    Writes a GeoJSON file; with a region, also returns % covered per mission and per day.

    Args:
        region: area to summarise (bbox, point or GeoJSON); empty = whole world.
        start: window start (ISO UTC); empty = now.
        days: window length (max 10 with a region, 3 without).
        missions: mission codes; default S1 and S2.
        satellite_names: specific satellites, instead of `missions`.
        filename: GeoJSON file name; default a timestamped name.
    """
    # --- Inputs: region (None = whole world), time window, satellites ---
    region_geom = parse_aoi(region, max_km2=MAX_REGION_KM2) if region else None
    max_days = MAX_COVERAGE_DAYS[region_geom is not None]  # shorter limit for the whole world
    if days > max_days:
        raise ValueError(f"days={days} is too long; the maximum is {max_days} "
                         + ("with a region." if region_geom is not None
                            else "without a region: pass a region for longer."))
    window_start, window_end = window(start, "", 1, days)
    if satellite_names:
        selected_satellites = [sat_name.upper().strip() for sat_name in satellite_names]
        unknown_satellites = [sat_name for sat_name in selected_satellites if sat_name not in ACTIVE]
        if unknown_satellites:
            raise ValueError(f"Unknown satellites {unknown_satellites}. Use one of {sorted(ACTIVE)}, or add one with "
                             "add_satellite.")
    else:
        _, selected_satellites = mission_satellites(missions)

    # Each footprint is a (geometry, properties) pair.
    footprints, warnings, plan_horizons, plan_files = [], [], {}, []

    # --- 1. ESA plan footprints (Sentinel-1/2): the real data takes, all modes, up to each plan horizon ---
    for mission in ("S1", "S2"):
        if not any(ACTIVE[sat_name] == mission for sat_name in selected_satellites):
            continue
        try:
            data_takes, plan_info = planned_takes(mission, region_geom, window_start, window_end)
            plan_horizons.update(plan_info["plan_horizon_utc"])
            plan_files += [plan_file["file"] for plan_file in plan_info["plan_files"]]
            warnings += plan_info.get("warnings", [])
            for take in data_takes:
                if take["satellite"] not in selected_satellites:
                    continue
                footprint_geom = take["geom"].intersection(region_geom) if region_geom is not None else take["geom"]
                footprints.append((footprint_geom, {
                    "satellite": take["satellite"], "mission": mission,
                    "sensor_type": "SAR" if mission == "S1" else "optical",
                    "start_utc": _iso(take["start"]), "end_utc": _iso(take["end"]) if take["end"] else None,
                    "expected_available_utc": _iso(available_utc(mission, take["end"] or take["start"])),
                    "mode": take["mode"], "polarisation": take["polarisation"],
                    "relative_orbit": take["relative_orbit"], "absolute_orbit": take["absolute_orbit"],
                    "confidence": "planned", "footprint_source": "ESA plan"}))
        except Exception as ex:
            warnings.append(f"{mission} plan unavailable ({ex}); {mission} footprints are orbit-predicted")

    # --- 2. Orbit-predicted strips: other satellites, and Sentinels after their plan horizon ---
    for sat_name in selected_satellites:
        horizon = parse_time(plan_horizons[sat_name]) if sat_name in plan_horizons else None
        predict_from = max(window_start, horizon) if horizon else window_start
        if predict_from >= window_end:  # the plan already covers the whole window
            continue
        for strip_start, strip_end, direction, strip_polygon in ground_strips(sat_name, predict_from, window_end):
            footprint_geom = strip_polygon.intersection(region_geom) if region_geom is not None else strip_polygon
            footprints.append((footprint_geom, {
                "satellite": sat_name, "mission": ACTIVE[sat_name],
                "sensor_type": "optical" if SENSORS[ACTIVE[sat_name]]["optical"] else "SAR",
                "start_utc": _iso(strip_start), "end_utc": _iso(strip_end), "direction": direction,
                "expected_available_utc": _iso(available_utc(ACTIVE[sat_name], strip_end)),
                "confidence": "geometric", "footprint_source": "orbit prediction"}))

    # Keep only polygon parts (clipping can leave lines/points), drop empty ones, sort by time.
    footprints = [(geom, props) for geom, props in ((polygons_only(geom), props) for geom, props in footprints)
                  if geom is not None]
    footprints.sort(key=lambda footprint: footprint[1]["start_utc"])

    # --- 3. GeoJSON file: region outline + one coloured feature per footprint ---
    decimals = 5 if region_geom is not None else 4  # fewer decimals for world-wide files (smaller)
    geojson_features = []
    if region_geom is not None:
        geojson_features.append(feature(region_geom, {"role": "region", "label": "Region", "stroke": "#000000",
                                                      "stroke-width": 2.5, "fill-opacity": 0}, decimals))
    for footprint_id, (geom, props) in enumerate(footprints, 1):
        colour = CONF_COLORS[props["confidence"]]
        start_label = props["start_utc"][:16].replace("T", " ")  # '2026-11-21 10:42'
        feature_props = {"role": "observed_area", "id": footprint_id,
                         "label": f'{start_label} {props["satellite"]} {props["confidence"]}',
                         **{key: value for key, value in props.items() if value is not None},
                         "stroke": colour, "stroke-width": 1.5, "fill": colour, "fill-opacity": 0.2}
        if region_geom is not None:
            feature_props["area_km2"] = round(area_km2(geom, region_geom.centroid.x))
        geojson_features.append(feature(geom, feature_props, decimals))
    metadata = {"window_utc": [_iso(window_start), _iso(window_end)], "generated_utc": _iso(dt.datetime.now(UTC)),
                "plan_horizon_utc": plan_horizons, "plan_files": plan_files, "warnings": warnings}
    geojson_path = save_geojson({"type": "FeatureCollection", "name": "orbitcast_observed_areas",
                                 "orbitcast": metadata, "features": geojson_features},
                                filename, "orbitcast_observed_areas")

    # --- 4. Summary for the model: counts and first/last time per satellite ---
    by_satellite = {}
    for _, props in footprints:
        sat_summary = by_satellite.setdefault(props["satellite"], {
            "footprints": 0, "planned": 0, "geometric": 0, "first_utc": props["start_utc"],
            "last_utc": props["start_utc"], "first_available_utc": props["expected_available_utc"]})
        sat_summary["footprints"] += 1
        sat_summary[props["confidence"]] += 1
        sat_summary["last_utc"] = props["start_utc"]  # footprints are sorted, so the last one wins
    result = {"window": [_iso(window_start), _iso(window_end)], "path": str(geojson_path),
              "footprints": len(footprints), "by_satellite": by_satellite, "plan_horizon_utc": plan_horizons,
              "warnings": warnings}

    # --- 5. With a region: % of the region covered per mission (all, planned only, per day) ---
    if region_geom is not None:
        region_area_km2, projection_lon = area_km2(region_geom), region_geom.centroid.x
        result["region_km2"] = round(region_area_km2)
        coverage = {}
        for mission in sorted({props["mission"] for _, props in footprints}):
            mission_footprints = [(geom, props) for geom, props in footprints if props["mission"] == mission]
            # Union first, so overlapping footprints are not counted twice.
            planned_union = unary_union([geom for geom, props in mission_footprints
                                         if props["confidence"] == "planned"])
            all_union = unary_union([geom for geom, _ in mission_footprints])
            coverage_by_day = {}
            for day in sorted({props["start_utc"][:10] for _, props in mission_footprints}):
                day_union = unary_union([geom for geom, props in mission_footprints
                                         if props["start_utc"][:10] == day])
                coverage_by_day[day] = round(min(100 * area_km2(day_union, projection_lon) / region_area_km2, 100), 1)
            coverage[mission] = {
                "covered_pct": round(min(100 * area_km2(all_union, projection_lon) / region_area_km2, 100), 1),
                "covered_pct_planned_only": round(min(100 * area_km2(planned_union, projection_lon)
                                                      / region_area_km2, 100), 1)
                if not planned_union.is_empty else 0.0,
                "by_day_pct": coverage_by_day}
        result["coverage"] = coverage
    result["how_to_view"] = ("Open the file in QGIS, geojson.io or GitHub. Blue = in ESA plan, orange = orbit "
                             "prediction (not confirmed); black = region.")
    return result
