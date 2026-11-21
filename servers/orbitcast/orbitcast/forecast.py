"""
Logic behind the forecast tools: combines orbits, ESA plans and clouds into the results server.py returns.

    forecast          forecast_observations: orbit passes -> ESA plan labels (merge) -> cloud labels for optical
                      passes -> timeline sorted by time + next usable image per mission
    merge             a pass inside an ESA data take (+-3 min) is 'planned'; inside the plan horizon without one
                      it is dropped (no image will exist); beyond the horizon it stays 'geometric'
    passes / acquisition_plan / revisits / catalogue / sync_plans
                      backends of predict_passes, get_acquisition_plan, next_revisit_after, list_eo_satellites
                      and sync_acquisition_plans
    window            start/end/days -> UTC window; past dates are rejected

Functions return plain dicts and raise ValueError with a fix-it message; server.py turns that into JSON.
"""

import datetime as dt
from typing import Any

from .catalog import (
    ACTIVE,
    CATALOG_INFO,
    LATENCY_H,
    SENSORS,
    available_utc,
    mission_satellites,
    refresh_catalog,
    source_of,
)
from .clouds import cloud_at, cloud_series
from .config import MAX_HORIZON_DAYS
from .geo import UTC, _iso, area_km2, parse_aoi, parse_time
from .orbits import predict_geometric, satellites
from .plans import (
    PLAN_DIR,
    PLAN_INDEX,
    _bad_plan_urls,
    download_plan,
    plan_index,
    planned_takes,
)


def window(start: str, end: str, default_days: float, days: float = 0) -> tuple[dt.datetime, dt.datetime]:
    """
    (start, end) as UTC datetimes. Past windows are rejected with today's date in the error.

    Args:
        start: ISO UTC time; empty = now.
        end: ISO UTC time; empty = start + `days`.
        default_days: window length when neither `end` nor `days` is given.
        days: window length in days.
    """
    now = dt.datetime.now(UTC).replace(microsecond=0)
    s = parse_time(start, now)
    e = parse_time(end, s + dt.timedelta(days=days if days and days > 0 else default_days))
    today = f"Today is {now:%Y-%m-%d} (UTC)."
    if s < now - dt.timedelta(days=1):
        raise ValueError(f"start {_iso(s)} is in the past. {today} OrbitCast forecasts future acquisitions: leave "
                         "start empty for 'now' and use days=N for 'the next N days'. For past scenes use a "
                         "catalogue/STAC tool.")
    if e <= s:
        raise ValueError(f"end {_iso(e)} is not after start {_iso(s)}. {today} Leave end empty and use days=N.")
    return s, e


def is_optical(p: dict[str, Any]) -> bool:
    """
    True for optical passes (the only ones the cloud forecast affects).

    Args:
        p: a pass from predict_geometric.
    """
    return SENSORS.get(p.get("mission"), {}).get("optical", False)


def merge(geo: list[dict[str, Any]], takes: list[dict[str, Any]],
          horizon: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """
    Label S1/S2 passes: 'planned' if inside a data take (+-3 min), else 'geometric'.

    Passes inside the plan horizon with no data take are dropped (no image will exist). Unmatched data takes are
    added as extra 'planned' entries. Returns (passes, number dropped).

    Args:
        geo: passes from predict_geometric.
        takes: data takes from planned_takes.
        horizon: satellite -> last time its plans cover (ISO UTC).
    """
    used, dropped = set(), set()
    for k, p in enumerate(geo):
        if p["mission"] not in ("S1", "S2"):
            continue
        t = parse_time(p["time_utc"])
        for i, q in enumerate(takes):
            if i in used or q["satellite"] != p["satellite"]:
                continue
            lo, hi = q["start"] - dt.timedelta(minutes=3), (q["end"] or q["start"]) + dt.timedelta(minutes=3)
            if lo <= t <= hi:
                used.add(i)
                p.update(confidence="planned", mode=q["mode"], relative_orbit=q["relative_orbit"],
                         datatake=q["datatake"])
                if q["polarisation"]:
                    p["polarisation"] = q["polarisation"]
                break
        else:
            h = horizon.get(p["satellite"])
            if h and t <= parse_time(h):
                dropped.add(k)  # ESA scheduled no data take: no image will exist
    extra = [{"time_utc": _iso(q["start"]), "satellite": q["satellite"], "mission": ACTIVE.get(q["satellite"], "?"),
              "sensor_type": "optical" if SENSORS.get(ACTIVE.get(q["satellite"]), {}).get("optical") else "SAR",
              "coverage_pct": q["plan_coverage_pct"], "mode": q["mode"], "relative_orbit": q["relative_orbit"],
              "expected_available_utc": _iso(available_utc(ACTIVE.get(q["satellite"], "?"), q["end"] or q["start"])),
              "confidence": "planned", "note": "in ESA plan; orbit prediction puts the area at the swath edge"}
             for i, q in enumerate(takes) if i not in used]
    return [p for k, p in enumerate(geo) if k not in dropped] + extra, len(dropped)


def catalogue() -> dict[str, Any]:
    """Satellites that can be forecast now, with source, sensor model and TLE age (list_eo_satellites tool)."""
    sats, prov = satellites()
    now = dt.datetime.now(UTC)
    rows = []
    for n in sorted(ACTIVE):
        s = SENSORS[ACTIVE[n]]
        row = {"satellite": n, "mission": ACTIVE[n], "source": source_of(n), "sensor": s["sensor"],
               "sensor_type": "optical" if s["optical"] else "SAR",
               "swath_km_from_track": [round(s["near_km"]), round(s["far_km"])],
               "repeat_days": s["repeat_days"] or None, "typical_latency_h": LATENCY_H.get(ACTIVE[n], 24.0)}
        if n in sats:
            row["tle_age_days"] = round((now - sats[n].epoch.utc_datetime()).total_seconds() / 86400, 2)
        else:
            row["warning"] = "no orbit data (TLE) found: cannot be forecast"
        rows.append(row)
    return {"satellites": rows, "catalogue": CATALOG_INFO, "provenance": prov}


def passes(aoi: str, start: str = "", end: str = "", satellite_names: list[str] | None = None,
           min_coverage_pct: float = 10.0, days: float = 0) -> dict[str, Any]:
    """
    Orbit-only overpasses of an area, default 7 days (predict_passes tool).

    Args:
        aoi: area of interest (bbox, point or GeoJSON).
        start, end: window in ISO UTC; empty start = now.
        satellite_names: satellites to include; default the whole catalogue.
        min_coverage_pct: minimum % of the area a pass must cover.
        days: window length, instead of `end`.
    """
    g = parse_aoi(aoi)
    s, e = window(start, end, 7, days)
    found, skipped, prov = predict_geometric(g, s, e, satellite_names, min_coverage_pct)
    return {"window": [_iso(s), _iso(e)], "aoi_km2": round(area_km2(g)), "passes": found, "skipped": skipped,
            "provenance": prov}


def acquisition_plan(mission: str, aoi: str, start: str = "", end: str = "", days: float = 0) -> dict[str, Any]:
    """
    ESA data takes over an area, default 10 days (get_acquisition_plan tool).

    Args:
        mission: 'S1' or 'S2' (also 'Sentinel-1' / 'Sentinel-2').
        aoi: area of interest (bbox, point or GeoJSON).
        start, end: window in ISO UTC; empty start = now.
        days: window length, instead of `end`.
    """
    m = mission.upper().replace("SENTINEL-", "S").replace("SENTINEL", "S")
    if m not in PLAN_INDEX:
        raise ValueError("mission must be 'S1' or 'S2' (ESA publishes plans only for these).")
    g = parse_aoi(aoi)
    s, e = window(start, end, 10, days)
    takes, prov = planned_takes(m, g, s, e)
    rows = [{"datatake_start_utc": _iso(q["start"]), "datatake_end_utc": _iso(q["end"]) if q["end"] else None,
             "satellite": q["satellite"], "mode": q["mode"], "polarisation": q["polarisation"],
             "relative_orbit": q["relative_orbit"], "coverage_pct": q["plan_coverage_pct"],
             "expected_available_utc": _iso(available_utc(m, q["end"] or q["start"])), "confidence": "planned"}
            for q in takes]
    return {"window": [_iso(s), _iso(e)], "planned": rows, "provenance": prov}


def sync_plans(missions: list[str] | None = None, days_ahead: float = 10,
               force_refresh: bool = False) -> dict[str, Any]:
    """
    Pre-download ESA plans and report each satellite's plan horizon (sync_acquisition_plans tool).

    Args:
        missions: 'S1' and/or 'S2'; default both.
        days_ahead: download the plans covering this many days from now.
        force_refresh: re-download the plan list and files even if cached.
    """
    now = dt.datetime.now(UTC)
    end = now + dt.timedelta(days=days_ahead)
    report = {}
    for m in [x.upper() for x in (missions or ["S1", "S2"])]:
        if m not in PLAN_INDEX:
            raise ValueError(f"Unsupported mission {m}; ESA publishes plans for S1 and S2 only.")
        files, prov = plan_index(m, refresh=force_refresh)
        sats = {}
        for f in sorted(files, key=lambda x: x["start"], reverse=True):
            if f["satellite"] not in ACTIVE or f["stop"] < now or f["start"] > end:
                continue
            if sats.get(f["satellite"], {}).get("ok") and sats[f["satellite"]]["covered_from"] <= now:
                continue  # this satellite is already covered from now onward by a newer file
            if force_refresh:
                _bad_plan_urls.pop(f["url"], None)
            entry = sats.setdefault(f["satellite"], {"files": [], "horizon": None, "covered_from": f["start"],
                                                      "ok": False})
            try:
                st = download_plan(f, force=force_refresh)
                entry["files"].append({"file": f["file"], "status": st["status"], "bytes": st["bytes"]})
                entry["ok"] = True
                entry["horizon"] = max(entry["horizon"] or f["stop"], f["stop"])
                entry["covered_from"] = min(entry["covered_from"], f["start"])
            except Exception as e:
                err = "404 Not Found (broken link on ESA's page)" if "404" in str(e) else str(e).splitlines()[0]
                entry["files"].append({"file": f["file"], "status": "failed", "error": err})
        report[m] = {**prov, "satellites": {k: {"plan_horizon_utc": _iso(v["horizon"]) if v["horizon"] else None,
                                                "files": v["files"]} for k, v in sats.items()}}
    refresh_catalog()
    return {"cache_dir": str(PLAN_DIR), "missions": report, "catalogue": CATALOG_INFO}


def forecast(aoi: str, start: str = "", end: str = "", missions: list[str] | None = None,
             max_cloud_pct: float = 30.0, min_coverage_pct: float = 20.0, days: float = 0) -> dict[str, Any]:
    """
    Ranked timeline of upcoming images of an area (forecast_observations tool).

    Orbit prediction -> ESA plan labels -> cloud labels for optical passes -> next usable pass per mission.

    Args:
        aoi: area of interest (bbox, point or GeoJSON).
        start, end: window in ISO UTC; empty start = now (default 10 days).
        missions: mission codes; default S1 and S2.
        max_cloud_pct: optical passes above this forecast cloud % are labelled 'cloudy'.
        min_coverage_pct: minimum % of the area a pass must cover.
        days: window length, instead of `end`.
    """
    g = parse_aoi(aoi)
    s, e = window(start, end, 10, days)
    ms, names = mission_satellites(missions)
    found, skipped, prov = predict_geometric(g, s, e, names, min_coverage_pct)
    warnings = []
    for m in ("S1", "S2"):
        if m not in ms:
            continue
        try:
            takes, pprov = planned_takes(m, g, s, e)
            takes = [q for q in takes if q["plan_coverage_pct"] >= min_coverage_pct]
            mine = [p for p in found if p["mission"] == m]
            merged, n_dropped = merge(mine, takes, pprov["plan_horizon_utc"])
            found = [p for p in found if p["mission"] != m] + merged
            if n_dropped:
                skipped[f"{m}_no_data_take_in_esa_plan"] = n_dropped
            prov[f"{m}_plan"] = pprov
            warnings += pprov.get("warnings", [])
        except Exception as ex:
            warnings.append(f"{m} plan unavailable ({ex}); {m} passes are geometric only")
    # Cloud cover is applied to OPTICAL passes only. Radar (SAR) sees through clouds and smoke, so its labels
    # never depend on max_cloud_pct and no cloud value is attached to it.
    if any(is_optical(p) for p in found):
        try:
            series, cprov = cloud_series(g)
            prov.update(cprov)
            for p in found:
                if is_optical(p):
                    cc = cloud_at(series, parse_time(p["time_utc"]))
                    p["cloud_pct"] = cc
                    if cc is not None:
                        p["confidence"] = "likely_usable" if cc <= max_cloud_pct else "cloudy"
        except Exception as ex:
            warnings.append(f"cloud forecast unavailable ({type(ex).__name__})")
    found.sort(key=lambda p: p["time_utc"])
    keys = ("time_utc", "expected_available_utc", "satellite", "confidence", "coverage_pct", "cloud_pct", "mode")
    nxt = {}
    for m in ms:
        mine = [p for p in found if p["mission"] == m]
        usable = ("likely_usable", "geometric") if SENSORS[m]["optical"] else ("planned", "geometric")
        p = next((p for p in mine if p["confidence"] in usable), None)
        if p:
            nxt[m] = {k: p[k] for k in keys if p.get(k) is not None}
        elif mine:
            nxt[m] = {"none_usable_in_window": True,
                      "next_pass": {k: mine[0][k] for k in keys if mine[0].get(k) is not None}}
        else:
            nxt[m] = {"none_in_window": True}
    return {"window": [_iso(s), _iso(e)], "next_by_mission": nxt, "timeline": found, "skipped": skipped,
            "warnings": warnings, "provenance": prov}


def revisits(satellite: str, reference_time_utc: str, aoi: str, count: int = 3) -> dict[str, Any]:
    """
    Next same-geometry acquisitions: reference time + whole repeat cycles, refined by orbit within 30 days.

    Args:
        satellite: satellite name, e.g. 'SENTINEL-1C'.
        reference_time_utc: time of the reference acquisition (ISO UTC).
        aoi: area of interest (bbox, point or GeoJSON).
        count: number of revisits to return (max 10).
    """
    name = satellite.upper().strip()
    if name not in ACTIVE:
        raise ValueError(f"Unknown satellite '{satellite}'. Use one of {sorted(ACTIVE)}, or add it with add_satellite.")
    repeat = SENSORS[ACTIVE[name]]["repeat_days"]
    if not repeat:
        raise ValueError(f"The repeat cycle of {name} is unknown: set repeat_days with add_satellite.")
    g, ref = parse_aoi(aoi), parse_time(reference_time_utc)
    rep = dt.timedelta(days=repeat)
    now = dt.datetime.now(UTC)
    k = max(1, int((now - ref) / rep) + 1)
    out = []
    for i in range(k, k + max(1, min(count, 10))):
        t = ref + i * rep
        item = {"time_utc": _iso(t), "cycles_after_reference": i, "confidence": "repeat_cycle"}
        if (t - now).days <= MAX_HORIZON_DAYS:
            ps, _, _ = predict_geometric(g, t - dt.timedelta(minutes=20), t + dt.timedelta(minutes=20),
                                         [name], 1.0, include_unusable=True)
            if ps:
                item.update(time_utc=ps[0]["time_utc"], coverage_pct=ps[0]["coverage_pct"], confidence="geometric",
                            expected_available_utc=ps[0]["expected_available_utc"])
        out.append(item)
    return {"satellite": name, "reference_time_utc": _iso(ref), "repeat_days": rep.days, "revisits": out}
