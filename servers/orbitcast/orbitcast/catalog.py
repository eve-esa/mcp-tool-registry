"""
The satellite catalogue: decides which satellites OrbitCast forecasts and describes each one's sensor (swath,
optical or SAR, repeat cycle) and download delay. Sentinel-1/2 units are taken from ESA's plan pages, Landsat 8/9
is built in, and users can add any other satellite. The result lives in ACTIVE (name -> mission), SENSORS and
LATENCY_H, which are changed in place so every module sees the current catalogue.

    refresh_catalog     rebuild ACTIVE: Sentinels with a plan ending less than 14 days ago (built-in list if ESA is
                        down), plus Landsat and user satellites, filtered by ORBITCAST_SATELLITES
    mission_satellites  check mission codes ('S1', 'S2', ...) and return the satellite names of those missions
    source_of           where a satellite comes from (ESA plans, built-in, fallback or user), for list_eo_satellites
    available_utc       expected download time: acquisition + the mission's latency (24 h by default)
    add_satellite       add or update a user satellite: orbit by NORAD id (CelesTrak) or TLE lines, plus its
                        swath; saved to CUSTOM_FILE so it survives restarts
    remove_satellite    delete a user satellite (built-in and ESA ones cannot be removed)
"""

import datetime as dt
import json
import os
from typing import Any

from .config import CACHE
from .geo import UTC, _ground_offset_km, _iso

# Swath = signed cross-track offsets in km (positive = right of flight direction).
SENSORS: dict[str, dict[str, Any]] = {
    # Sentinel-1 IW is right-looking: incidence 29.1-46.0 deg at ~693 km  ->  ~345-617 km right of the track
    "S1": {"sensor": "C-SAR IW", "near_km": _ground_offset_km(693, 29.1), "far_km": _ground_offset_km(693, 46.0),
           "repeat_days": 12, "optical": False, "descending_only": False},
    "S2": {"sensor": "MSI", "near_km": -145.0, "far_km": 145.0, "repeat_days": 10, "optical": True,
           "descending_only": True},
    "LANDSAT": {"sensor": "OLI/TIRS", "near_km": -92.5, "far_km": 92.5, "repeat_days": 16, "optical": True,
                "descending_only": True},
}


# Delay between acquisition and the product being available for download, in hours.
# 24 h is the value for Sentinel-1/2 and is assumed for Landsat.
# Override with ORBITCAST_LATENCY_H, e.g. "S1=12,S2=24,LANDSAT=24".
LATENCY_H: dict[str, float] = {"S1": 24.0, "S2": 24.0, "LANDSAT": 24.0}

# Check if any custom latency is defined
for _latency_setting in filter(None, os.getenv("ORBITCAST_LATENCY_H", "").split(",")):
    _latency_mission, _latency_hours = _latency_setting.split("=")
    LATENCY_H[_latency_mission.strip().upper()] = float(_latency_hours)


def available_utc(mission: str, acquired: dt.datetime) -> dt.datetime:
    """
    Expected download time: acquisition + the mission's typical latency (24 h by default).

    Args:
        mission: mission code, e.g. 'S1'.
        acquired: acquisition time.
    """
    return acquired + dt.timedelta(hours=LATENCY_H.get(mission, 24.0))


# ---------- Satellites to be considered ----------
# Sentinel-1/2 satellites are obtained from ESA's acquisition-plan pages.
# ESA_FALLBACK is only used when ESA's pages cannot be reached.
ESA_FALLBACK: dict[str, str] = {"SENTINEL-1C": "S1", "SENTINEL-1D": "S1", "SENTINEL-2A": "S2", "SENTINEL-2B": "S2",
                                "SENTINEL-2C": "S2"}
OTHER_BUILTIN: dict[str, str] = {"LANDSAT 8": "LANDSAT", "LANDSAT 9": "LANDSAT"}
RESERVED_MISSIONS: set[str] = {"S1", "S2", "LANDSAT"}
CUSTOM_FILE = CACHE / "custom_satellites.json"


def _register_custom(record: dict[str, Any]) -> None:
    """
    Add a user satellite's sensor model and latency to SENSORS and LATENCY_H.

    Args:
        record: the satellite's saved record (mission, swath edges, optical, latency...).
    """
    mission_name: str = record["mission"]
    SENSORS[mission_name] = {"sensor": record.get("sensor") or "user-defined", "near_km": record["near_km"],
                             "far_km": record["far_km"], "repeat_days": record.get("repeat_days") or 0,
                             "optical": record["optical"], "descending_only": record["descending_only"]}
    LATENCY_H[mission_name] = record.get("latency_h", 24.0)


# User-added satellites: satellite name -> saved record ({} if the file is missing or unreadable).
try:
    CUSTOM: dict[str, dict[str, Any]] = json.loads(CUSTOM_FILE.read_text())
except (OSError, ValueError):
    CUSTOM = {}

for _custom_record in CUSTOM.values():
    _register_custom(_custom_record)


# Satellite name -> mission. Mutated in place by refresh_catalog(), so every function sees the current catalogue.
ACTIVE: dict[str, str] = {**ESA_FALLBACK, **OTHER_BUILTIN,
                          **{sat_name: record["mission"] for sat_name, record in CUSTOM.items()}}
CATALOG_INFO: dict[str, Any] = {"esa_source": "built-in fallback (not refreshed yet)",
                                "esa_satellites": sorted(ESA_FALLBACK)}


# ---------- Rebuild catalogue with updated one ----------
def refresh_catalog(max_age_days: float = 14) -> dict[str, str]:
    """
    Rebuild ACTIVE: Sentinels with a recent ESA plan, plus Landsat and user satellites.

    Falls back to ESA_FALLBACK per mission if ESA is unreachable. Returns a copy of ACTIVE.

    Args:
        max_age_days: a Sentinel counts if its newest plan is current or ended less than this many days ago.
    """
    from .plans import (
        plan_index,  # here, not at module level: plans imports the catalogue
    )

    now = dt.datetime.now(UTC)
    esa_satellites: dict[str, str] = {}  # satellite name -> mission
    esa_status: dict[str, str] = {}  # mission -> where its satellite list came from
    for mission in ("S1", "S2"):
        try:  # Get the list of plan files from ESA's webpage
            plan_files, index_info = plan_index(mission)
            recent_satellites = {plan["satellite"] for plan in plan_files
                                 if plan["stop"] >= now - dt.timedelta(days=max_age_days)}
            esa_satellites.update(dict.fromkeys(recent_satellites, mission))
            esa_status[mission] = f"ESA plans ({index_info['index_status']})"

        except Exception as ex:  # Fallback when the download fails
            esa_satellites.update({sat_name: fallback_mission for sat_name, fallback_mission in ESA_FALLBACK.items()
                                   if fallback_mission == mission})
            esa_status[mission] = f"built-in fallback (ESA unreachable: {type(ex).__name__})"

    new_catalogue: dict[str, str] = {**esa_satellites, **OTHER_BUILTIN,
                                     **{sat_name: record["mission"] for sat_name, record in CUSTOM.items()}}

    # Limit the satellites to the ones defined by ORBITCAST_SATELLITES
    allowed_names = [sat_name.strip().upper() for sat_name in os.getenv("ORBITCAST_SATELLITES", "").split(",")
                     if sat_name.strip()]
    if allowed_names:
        new_catalogue = {sat_name: sat_mission for sat_name, sat_mission in new_catalogue.items()
                         if sat_name in allowed_names}

    ACTIVE.clear()
    ACTIVE.update(new_catalogue)
    CATALOG_INFO.update(esa_source=esa_status, esa_satellites=sorted(esa_satellites), refreshed_utc=_iso(now))
    return dict(ACTIVE)


# ---------- Using and extending the catalogue ----------
def mission_satellites(missions: list[str] | None = None,
                       default: tuple[str, ...] = ("S1", "S2")) -> tuple[list[str], list[str]]:
    """
    Validate mission codes and return (missions, satellite names). ValueError lists the available ones.

    Args:
        missions: mission codes such as ['S1', 'LANDSAT']; empty uses `default`.
        default: missions used when none are given.
    """
    requested_missions = [mission.upper().strip() for mission in (missions or default)]
    known_missions = sorted(set(ACTIVE.values()))
    unknown_missions = [mission for mission in requested_missions if mission not in known_missions]
    if unknown_missions:
        raise ValueError(f"Unknown missions {unknown_missions}. Available: {known_missions}. "
                         "Add another satellite with add_satellite.")
    return requested_missions, [sat_name for sat_name, sat_mission in ACTIVE.items()
                                if sat_mission in requested_missions]


def source_of(name: str) -> str:
    """
    Where a satellite comes from: ESA plans, built-in, fallback, or user.

    Args:
        name: satellite name, e.g. 'SENTINEL-2B'.
    """
    if name in CUSTOM:
        return "user (add_satellite)"
    if name in OTHER_BUILTIN:
        return "built-in (no ESA plan)"
    mission = ACTIVE.get(name)
    if "fallback" in str(CATALOG_INFO.get("esa_source", {}).get(mission, "fallback")):
        return "built-in fallback (ESA unreachable)"
    return "ESA acquisition plans"


def _save_custom() -> None:
    """Write the user satellites to disk atomically (temp file, then rename)."""
    temporary_file = CUSTOM_FILE.with_suffix(".part")
    temporary_file.write_text(json.dumps(CUSTOM, indent=1))
    temporary_file.replace(CUSTOM_FILE)


def add_satellite(name: str, norad_id: int = 0, tle_line1: str = "", tle_line2: str = "", swath_km: float = 0.0,
                  near_km: float | None = None, far_km: float | None = None, optical: bool = True,
                  descending_only: bool = False, repeat_days: float = 0.0, latency_h: float = 24.0, sensor: str = "",
                  mission: str = "") -> dict[str, Any]:
    """
    Add or update a user satellite and save it to disk. ValueError on invalid input.

    Args:
        name: satellite name, e.g. 'SENTINEL-3A'.
        norad_id: NORAD catalogue id; the orbit is fetched from CelesTrak.
        tle_line1, tle_line2: the two TLE lines, instead of `norad_id`.
        swath_km: full swath width, centred on the ground track.
        near_km, far_km: swath edges in km from the track (positive = right), instead of `swath_km`.
        optical: True for optical sensors (night passes dropped, cloud forecast applied).
        descending_only: True if the sensor only acquires on descending passes.
        repeat_days: repeat cycle, used by next_revisit_after.
        latency_h: hours from acquisition to download.
        sensor: sensor name, for display.
        mission: mission code; default the satellite name.
    """
    from skyfield.api import (  # local: keeps the catalogue light to import
        EarthSatellite,
        load,
    )

    from .orbits import fetch_tle_by_norad

    refresh_catalog()
    satellite_name: str = (name or "").upper().strip()
    if not satellite_name:
        raise ValueError("name is required, e.g. 'SENTINEL-3A' or 'MY-SAT'.")
    if satellite_name in ACTIVE and satellite_name not in CUSTOM:
        raise ValueError(f"{satellite_name} is already in the catalogue ({source_of(satellite_name)}); "
                         "no need to add it.")

    # Swath edges in km from the ground track (positive = right of the flight direction).
    near_edge_km: float
    far_edge_km: float
    if near_km is None and far_km is None:
        if not 0 < swath_km <= 3000:
            raise ValueError("Give swath_km (full swath width in km, 0-3000, centred on the ground track) or "
                             "near_km and far_km for a side-looking sensor (km from the track, positive = right).")
        near_edge_km, far_edge_km = -swath_km / 2, swath_km / 2
    else:
        if near_km is None or far_km is None or not near_km < far_km or max(abs(near_km), abs(far_km)) > 3000:
            raise ValueError("near_km must be smaller than far_km (km from the ground track, positive = right of "
                             "the flight direction), both within +-3000 km.")
        near_edge_km, far_edge_km = float(near_km), float(far_km)

    mission_name: str = (mission or satellite_name).upper().strip()
    if mission_name in RESERVED_MISSIONS:
        raise ValueError(f"Mission name {mission_name} is reserved for the built-in satellites; choose another.")
    if latency_h < 0 or repeat_days < 0:
        raise ValueError("latency_h and repeat_days must be >= 0.")

    # What is saved to CUSTOM_FILE for this satellite.
    record: dict[str, Any] = {
        "mission": mission_name, "sensor": sensor or "user-defined", "near_km": near_edge_km, "far_km": far_edge_km,
        "optical": bool(optical), "descending_only": bool(descending_only), "repeat_days": float(repeat_days),
        "latency_h": float(latency_h), "added_utc": _iso(dt.datetime.now(UTC))}

    # Orbit: from CelesTrak by NORAD id, or the TLE lines given by the user.
    tle_first_line: str
    tle_second_line: str
    if norad_id and int(norad_id) > 0:
        celestrak_name, tle_first_line, tle_second_line = fetch_tle_by_norad(int(norad_id))
        record.update(norad_id=int(norad_id), celestrak_name=celestrak_name)
    elif tle_line1 and tle_line2:
        tle_first_line, tle_second_line = tle_line1.strip(), tle_line2.strip()
        if not (tle_first_line.startswith("1 ") and tle_second_line.startswith("2 ")
                and len(tle_first_line) >= 69 and len(tle_second_line) >= 69):
            raise ValueError("tle_line1/tle_line2 must be the two 69-character TLE lines starting with '1 ' and '2 '.")
        record["tle"] = [tle_first_line, tle_second_line]
    else:
        raise ValueError("Give norad_id (orbit fetched from CelesTrak) or both tle_line1 and tle_line2.")

    # Building the satellite object also checks that the TLE can be read; its epoch tells how old the orbit is.
    satellite = EarthSatellite(tle_first_line, tle_second_line, satellite_name, load.timescale(builtin=True))
    tle_epoch: dt.datetime = satellite.epoch.utc_datetime()

    # Save mission and refresh catalog
    CUSTOM[satellite_name] = record
    _save_custom()
    _register_custom(record)
    refresh_catalog()

    return {"added": satellite_name, "mission": mission_name, "source": "user (add_satellite)",
            "orbit": record.get("celestrak_name", "TLE lines"),
            "tle_epoch_utc": _iso(tle_epoch),
            "tle_age_days": round((dt.datetime.now(UTC) - tle_epoch).total_seconds() / 86400, 1),
            "swath_km_from_track": [round(near_edge_km, 1), round(far_edge_km, 1)], "optical": record["optical"],
            "typical_latency_h": record["latency_h"],
            "how_to_use": f"Pass satellites=['{satellite_name}'] or missions=['{mission_name}'] to any OrbitCast "
                          "tool. ESA publishes no plan for it, so its results are 'geometric' (orbit prediction).",
            "stored_in": str(CUSTOM_FILE)}


def remove_satellite(name: str) -> dict[str, Any]:
    """
    Remove a user-added satellite (built-in and ESA ones cannot be removed).

    Args:
        name: name of the user satellite.
    """
    satellite_name: str = (name or "").upper().strip()
    if satellite_name not in CUSTOM:
        raise ValueError(f"{satellite_name} is not a user-added satellite. User-added: {sorted(CUSTOM) or 'none'}. "
                         "Built-in and ESA-discovered satellites cannot be removed.")
    record = CUSTOM.pop(satellite_name)
    _save_custom()

    # Drop the sensor model only if no other user satellite shares the same mission.
    mission_name: str = record["mission"]
    if mission_name not in {other["mission"] for other in CUSTOM.values()}:
        SENSORS.pop(mission_name, None)
        LATENCY_H.pop(mission_name, None)
    refresh_catalog()
    return {"removed": satellite_name, "remaining_user_satellites": sorted(CUSTOM)}
