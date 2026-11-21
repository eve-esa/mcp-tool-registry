"""
Orbits: where each satellite will be and which ground it can image. Results here are 'geometric': the satellite
can image the area, but no acquisition is confirmed.

    load_tles / fetch_tle_by_norad   orbital elements (TLE) from CelesTrak, cached 6 h in CACHE/tles.json
                                     (stale copy used if CelesTrak is down)
    satellites                       TLEs -> skyfield EarthSatellite objects for the catalogue
    predict_geometric                overpasses of an AOI: SGP4 positions (60 s screening, 10 s refinement), swath
                                     polygon from the sensor model, % of the AOI covered; optical night and
                                     ascending passes are skipped
    ground_strips                    swath strips over a whole window, for the coverage (reverse) query
"""

import datetime as dt
import json
import time
from typing import Any

import httpx
import numpy as np
import skyfield
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from skyfield.api import EarthSatellite, load, wgs84

from .catalog import ACTIVE, CUSTOM, LATENCY_H, SENSORS, available_utc
from .config import CACHE, MAX_HORIZON_DAYS, UA
from .geo import (
    UTC,
    _iso,
    area_km2,
    bearing_deg,
    cross_track_km,
    destination,
    haversine_km,
    normalize_lon,
    sun_elevation_deg,
    sun_elevation_np,
    view_angles,
)

TS = load.timescale(builtin=True)  # skyfield timescale; builtin leap-second table: no download


# ---------- TLEs ----------
CELESTRAK_GP = "https://celestrak.org/NORAD/elements/gp.php"  # CelesTrak query endpoint for current TLEs
TLE_CACHE_TTL_S = 6 * 3600  # TLEs are re-downloaded after 6 h
SCREEN_STEP_S = 60  # predict_geometric: coarse time step to find when a satellite is near the AOI
REFINE_STEP_S = 10  # predict_geometric: fine time step around each approach


def _parse_tle_text(text: str) -> dict[str, tuple[str, str]]:
    """
    Split CelesTrak's 3-line TLE text into {NAME: (line1, line2)}.

    Args:
        text: TLE text as returned by CelesTrak (name, line 1, line 2, repeated).
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    tles = {}
    for i in range(len(lines) - 2):
        if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            tles[lines[i].strip().upper()] = (lines[i + 1], lines[i + 2])
    return tles


def fetch_tle_by_norad(norad_id: int, client: httpx.Client | None = None) -> tuple[str, str, str]:
    """
    Download the current TLE of one satellite: (CelesTrak name, line1, line2).

    ValueError if CelesTrak has no TLE for that id.

    Args:
        norad_id: NORAD catalogue id of the satellite.
        client: HTTP client to reuse; a new one is created if None.
    """
    http = client or httpx.Client(timeout=20, headers=UA)
    response = http.get(CELESTRAK_GP, params={"CATNR": int(norad_id), "FORMAT": "tle"})
    response.raise_for_status()
    found = _parse_tle_text(response.text)
    if not found:
        raise ValueError(f"CelesTrak has no current orbit data (TLE) for NORAD id {norad_id}. Check the id at "
                         "https://celestrak.org/satcat/search.php, or pass tle_line1/tle_line2 yourself.")
    celestrak_name, (line1, line2) = next(iter(found.items()))
    return celestrak_name, line1, line2


def load_tles() -> tuple[dict[str, tuple[str, str]], dict[str, Any]]:
    """
    TLEs of all Sentinel and Landsat satellites plus user satellites added by NORAD id.

    Cached for 6 h in CACHE/tles.json (re-downloaded sooner if the user satellites change); the stale cache is used
    if CelesTrak is down. Returns ({NAME: (line1, line2)}, provenance). RuntimeError if unreachable and no cache.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / "tles.json"
    custom_norad_ids = {sat_name: record["norad_id"] for sat_name, record in CUSTOM.items() if record.get("norad_id")}
    # The query list is stored with the cache, so adding/removing a user satellite invalidates it.
    queries = ["NAME=SENTINEL", "NAME=LANDSAT"] + sorted(f"CATNR={norad}" for norad in custom_norad_ids.values())
    cached = json.loads(cache_file.read_text()) if cache_file.exists() else None

    if cached and cached.get("queries") == queries and time.time() - cached["fetched_ts"] < TLE_CACHE_TTL_S:
        return cached["tles"], cached["prov"]

    try:  # Download TLEs for the available satellites
        tles, warnings = {}, []
        with httpx.Client(timeout=20, headers=UA) as http:

            # Sentinel and Landsat downloads (one request per group, all units)
            for group in ("SENTINEL", "LANDSAT"):
                response = http.get(CELESTRAK_GP, params={"NAME": group, "FORMAT": "tle"})
                response.raise_for_status()
                tles.update(_parse_tle_text(response.text))

            # Custom satellites download (a failure only skips that satellite)
            for sat_name, norad in custom_norad_ids.items():
                try:
                    _, line1, line2 = fetch_tle_by_norad(norad, http)
                    tles[sat_name] = (line1, line2)
                except Exception as ex:
                    warnings.append(f"{sat_name}: {str(ex).splitlines()[0]}")

        provenance = {"tle_source": f"{CELESTRAK_GP} ({', '.join(queries)})",
                      "tle_fetched_utc": _iso(dt.datetime.now(UTC))}

        if warnings:
            provenance["tle_warnings"] = warnings

        # Save TLEs
        cache_file.write_text(json.dumps({"fetched_ts": time.time(), "queries": queries, "tles": tles,
                                          "prov": provenance}))

        return tles, provenance

    except Exception as e:
        if cached:
            return cached["tles"], {**cached["prov"],
                                    "warning": f"CelesTrak unreachable ({type(e).__name__}); cached TLEs"}
        raise RuntimeError(f"No TLEs: CelesTrak unreachable ({type(e).__name__}) and nothing cached.") from e


def satellites(names: list[str] | None = None) -> tuple[dict[str, EarthSatellite], dict[str, Any]]:
    """
    Orbit objects (skyfield EarthSatellite) for the catalogue satellites.

    Returns ({name: EarthSatellite}, provenance); catalogue satellites without a TLE are listed in the provenance.
    ValueError if a requested name is unknown or has no TLE.

    Args:
        names: only these satellites; default the whole catalogue.
    """
    tles, provenance = load_tles()
    # Add user satellites given as raw TLE lines (not downloaded).
    tles = {**tles, **{sat_name: tuple(record["tle"]) for sat_name, record in CUSTOM.items() if record.get("tle")}}
    orbits = {sat_name: EarthSatellite(line1, line2, sat_name, TS)
              for sat_name, (line1, line2) in tles.items() if sat_name in ACTIVE}
    without_orbit = sorted(sat_name for sat_name in ACTIVE if sat_name not in orbits)
    if without_orbit:
        provenance = {**provenance, "no_orbit_data": without_orbit}
    if names:
        requested = [sat_name.upper().strip() for sat_name in names]
        missing = [sat_name for sat_name in requested if sat_name not in orbits]
        if missing:
            raise ValueError(f"Unknown satellites or no orbit data for {missing}. Available: {sorted(orbits)}. "
                             "Add another satellite with add_satellite.")
        orbits = {sat_name: orbits[sat_name] for sat_name in requested}
    return orbits, provenance


def predict_geometric(aoi: BaseGeometry, start: dt.datetime, end: dt.datetime, sat_names: list[str] | None = None,
                      min_coverage_pct: float = 1.0, include_unusable: bool = False,
                      footprints: bool = False) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    """
    Every time a satellite's swath covers enough of the AOI in [start, end].

    Each pass gets its time (closest approach), direction, % of the AOI covered, viewing angle, sun elevation and
    expected download time. Returns (passes sorted by time, skipped counts, provenance).

    Args:
        aoi: area of interest.
        start, end: time window (max MAX_HORIZON_DAYS).
        sat_names: satellites to include; default the whole catalogue.
        min_coverage_pct: minimum % of the AOI a pass must cover.
        include_unusable: keep (and flag) optical night passes and ascending passes of descending-only sensors,
            instead of only counting them in `skipped`.
        footprints: add the swath polygon to each pass (for maps; never sent to the model).
    """
    if end <= start:
        raise ValueError("end must be after start.")
    if (end - start).days > MAX_HORIZON_DAYS:
        raise ValueError(f"Window is {(end - start).days} days; the maximum is {MAX_HORIZON_DAYS}. "
                         "Use next_revisit_after for longer horizons.")
    orbits, provenance = satellites(sat_names)

    # AOI centre, radius (centre to farthest vertex) and area.
    centroid = aoi.centroid
    center_lat, center_lon = centroid.y, centroid.x
    aoi_polygons = getattr(aoi, "geoms", [aoi])
    aoi_radius_km = max(float(haversine_km(center_lat, center_lon, vertex_lat, vertex_lon))
                        for polygon in aoi_polygons for vertex_lon, vertex_lat in polygon.exterior.coords)
    aoi_area = area_km2(aoi, center_lon)

    def wrap_lon(lon: np.ndarray) -> np.ndarray:
        """
        Keep longitudes within 180 deg of the AOI centre, so swaths near the antimeridian stay continuous.

        Args:
            lon: longitudes in degrees.
        """
        return (lon - center_lon + 180) % 360 - 180 + center_lon

    def swath_polygon(lat: np.ndarray, lon: np.ndarray, track_bearing: np.ndarray,
                      sensor: dict[str, Any]) -> list[tuple[float, float]]:
        """
        Ring of the swath along ground-track points: near edge forwards, far edge backwards.

        Args:
            lat, lon: ground-track points, in degrees.
            track_bearing: track direction at each point, degrees from north.
            sensor: sensor model from SENSORS (near_km, far_km).
        """
        near_lat, near_lon = destination(lat, lon, track_bearing + 90, sensor["near_km"])
        far_lat, far_lon = destination(lat, lon, track_bearing + 90, sensor["far_km"])
        return (list(zip(wrap_lon(near_lon), near_lat, strict=True))
                + list(zip(wrap_lon(far_lon)[::-1], far_lat[::-1], strict=True)))

    # Coarse time grid over the whole window (one point per SCREEN_STEP_S).
    window_start = TS.from_datetime(start)
    n_coarse = int((end - start).total_seconds() // SCREEN_STEP_S) + 1
    coarse_times = TS.tt_jd(window_start.tt + np.arange(n_coarse) * SCREEN_STEP_S / 86400)

    passes, skipped = [], {}
    for sat_name, orbit in orbits.items():
        mission = ACTIVE[sat_name]
        sensor = SENSORS[mission]
        # Farthest the ground track can be from the AOI centre while the swath still touches the AOI (+50 km margin).
        reach_km = max(abs(sensor["near_km"]), abs(sensor["far_km"])) + aoi_radius_km + 50
        tle_epoch = orbit.epoch.utc_datetime()

        # 1. Screening: coarse steps where the ground track is near the AOI (+500 km so no approach is missed).
        coarse_track = wgs84.subpoint_of(orbit.at(coarse_times))
        coarse_dist_km = haversine_km(coarse_track.latitude.degrees, coarse_track.longitude.degrees,
                                      center_lat, center_lon)
        near_steps = np.flatnonzero(coarse_dist_km < reach_km + 500)
        # Consecutive near steps form one approach.
        approaches = np.split(near_steps, np.flatnonzero(np.diff(near_steps) > 1) + 1) if near_steps.size else []

        for approach in approaches:
            # 2. Refinement: REFINE_STEP_S steps from one coarse step before to one after the approach.
            first_s = max(approach[0] - 1, 0) * SCREEN_STEP_S
            last_s = min(approach[-1] + 1, n_coarse - 1) * SCREEN_STEP_S
            fine_times = TS.tt_jd(window_start.tt + np.arange(first_s, last_s + 1, REFINE_STEP_S) / 86400)
            positions = orbit.at(fine_times)
            ground_point = wgs84.subpoint_of(positions)
            lat, lon = ground_point.latitude.degrees, ground_point.longitude.degrees
            alt_km = wgs84.height_of(positions).km
            dist_km = haversine_km(lat, lon, center_lat, center_lon)
            if dist_km.min() >= reach_km or len(dist_km) < 3:
                continue

            # Swath over the track points within reach (track direction from the neighbouring points).
            in_reach = np.arange(1, len(dist_km) - 1)[dist_km[1:-1] < reach_km]
            track_bearing = bearing_deg(lat[in_reach - 1], lon[in_reach - 1], lat[in_reach + 1], lon[in_reach + 1])
            ring = swath_polygon(lat[in_reach], lon[in_reach], track_bearing, sensor)
            if len(ring) < 4:
                continue
            swath = Polygon(ring).buffer(0)
            overlap = swath.intersection(aoi)
            coverage_pct = 100 * area_km2(overlap, center_lon) / aoi_area if not overlap.is_empty else 0.0
            if coverage_pct < min_coverage_pct:
                continue

            # Pass time = closest approach to the AOI centre; direction and geometry at that moment.
            closest = int(np.clip(np.argmin(dist_km), 1, len(dist_km) - 2))
            pass_time = fine_times[closest].utc_datetime().replace(microsecond=0)
            bearing_at_pass = float(bearing_deg(lat[closest - 1], lon[closest - 1],
                                                lat[closest + 1], lon[closest + 1]))
            offset_km = cross_track_km(lat[closest], lon[closest], bearing_at_pass, center_lat, center_lon)
            ascending = bool(lat[closest + 1] > lat[closest - 1])
            sun_elev = sun_elevation_deg(pass_time, center_lat, center_lon)

            # Passes the sensor does not acquire: optical at night, or ascending for descending-only sensors.
            reason = ("night" if sensor["optical"] and sun_elev <= 0 else
                      "ascending_not_acquired" if sensor["descending_only"] and ascending else None)
            if reason and not include_unusable:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue

            off_nadir, incidence = view_angles(float(alt_km[closest]), offset_km)
            tle_age_days = abs((pass_time - tle_epoch).total_seconds()) / 86400  # older TLE = less accurate time
            overpass = {
                "time_utc": _iso(pass_time), "satellite": sat_name, "mission": mission,
                "sensor_type": "optical" if sensor["optical"] else "SAR",
                "direction": "ascending" if ascending else "descending",
                "coverage_pct": round(min(coverage_pct, 100), 1),
                # optical sensors report the off-nadir angle, SAR the incidence angle
                ("off_nadir_deg" if sensor["optical"] else "incidence_deg"):
                    round(off_nadir if sensor["optical"] else incidence, 1),
                "sun_elev_deg": round(sun_elev, 1), "tle_age_days": round(tle_age_days, 1), "confidence": "geometric",
                "expected_available_utc": _iso(available_utc(mission, pass_time))}
            if reason:
                overpass["not_acquired_reason"] = reason
            if footprints:  # shapely polygon for plotting/GeoJSON; never returned to the model
                # use the whole refined segment (~±700 km along track), not just the part near the AOI
                segment = np.arange(1, len(dist_km) - 1)
                segment_bearing = bearing_deg(lat[segment - 1], lon[segment - 1], lat[segment + 1], lon[segment + 1])
                overpass["footprint"] = Polygon(swath_polygon(lat[segment], lon[segment], segment_bearing,
                                                              sensor)).buffer(0)
            passes.append(overpass)

    passes.sort(key=lambda overpass: overpass["time_utc"])
    provenance = {**provenance, "propagator": f"SGP4 (skyfield {skyfield.__version__})",
                  "availability_model": {"rule": "expected_available_utc = acquisition + typical latency (hours)",
                                         "latency_h": LATENCY_H},
                  "swath_km": {mission: [round(model["near_km"]), round(model["far_km"])]
                               for mission, model in SENSORS.items()}}
    return passes, skipped, provenance


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """
    (start, stop) index pairs of each run of consecutive True values (stop exclusive).

    Args:
        mask: 1-D boolean array.
    """
    edges = np.flatnonzero(np.diff(np.r_[0, mask.astype(int), 0]))  # where the mask switches on/off
    return list(zip(edges[::2], edges[1::2], strict=True))


def ground_strips(name: str, start: dt.datetime, end: dt.datetime, step_s: float = 30,
                  points_per_strip: int = 8) -> list[tuple[dt.datetime, dt.datetime, str, BaseGeometry]]:
    """
    Everything one satellite images over [start, end], as swath strips of ~4 minutes each.

    Only the parts the sensor acquires are kept (optical: daylight; descending-only: descending). Strips are split
    at the antimeridian. Returns [(strip_start, strip_end, direction, polygon)].

    Args:
        name: satellite name.
        start, end: time window.
        step_s: time step of the ground track, in seconds.
        points_per_strip: track steps per strip (8 x 30 s = 4 min).
    """
    orbit = satellites([name])[0][name]
    sensor = SENSORS[ACTIVE[name]]
    n_steps = int((end - start).total_seconds() // step_s) + 1
    if n_steps < 2:
        return []

    # Ground track every `step_s` seconds.
    window_start = TS.from_datetime(start)
    times = TS.tt_jd(window_start.tt + np.arange(n_steps) * step_s / 86400)
    ground_track = wgs84.subpoint_of(orbit.at(times))
    lat, lon = ground_track.latitude.degrees, ground_track.longitude.degrees

    # Track direction from the previous and next points (clipped at both ends).
    prev_idx = np.clip(np.arange(n_steps) - 1, 0, n_steps - 1)
    next_idx = np.clip(np.arange(n_steps) + 1, 0, n_steps - 1)
    track_bearing = bearing_deg(lat[prev_idx], lon[prev_idx], lat[next_idx], lon[next_idx])
    ascending = lat[next_idx] > lat[prev_idx]

    # Steps where the sensor acquires.
    acquiring = np.ones(n_steps, bool)
    if sensor["descending_only"]:
        acquiring &= ~ascending
    if sensor["optical"]:
        acquiring &= sun_elevation_np(start.timestamp() + np.arange(n_steps) * step_s, lat, lon) > 0

    strips = []
    for run_start, run_stop in _runs(acquiring):
        # Cut each acquiring run into strips of `points_per_strip` steps (consecutive strips share one point).
        for strip_first in range(run_start, run_stop - 1, points_per_strip):
            idx = np.arange(strip_first, min(strip_first + points_per_strip + 1, run_stop))
            if len(idx) < 2:
                continue
            near_lat, near_lon = destination(lat[idx], lon[idx], track_bearing[idx] + 90, sensor["near_km"])
            far_lat, far_lon = destination(lat[idx], lon[idx], track_bearing[idx] + 90, sensor["far_km"])
            ring = np.column_stack([np.r_[near_lon, far_lon[::-1]], np.r_[near_lat, far_lat[::-1]]])
            polygon = normalize_lon(Polygon(ring))
            if not polygon.is_empty:
                strips.append((start + dt.timedelta(seconds=float(idx[0] * step_s)),
                               start + dt.timedelta(seconds=float(idx[-1] * step_s)),
                               "ascending" if ascending[idx[len(idx) // 2]] else "descending", polygon))
    return strips
