"""Transparent, rule-based crop suitability screening.

Each criterion compares one indicator with a crop-specific threshold and reports
pass / warn / fail together with the value, the threshold and where the threshold
comes from. Thresholds are screening heuristics, not agronomic advice: the agent
should back them with literature from EVE retrieval before drawing conclusions.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

import numpy as np

SRC_FAO_WATER = "FAO Irrigation Water Management Training Manual 3 (Brouwer & Heibloem, 1986): seasonal crop water need"
SRC_ECOCROP = "FAO ECOCROP database: optimal / absolute temperature range (approx.)"
SRC_SYS = "FAO land evaluation crop requirements (after Sys et al., 1993): slope limits"
SRC_XIAO = "Xiao et al. (2005), Remote Sensing of Environment 95: flooding when LSWI + 0.05 >= vegetation index"
SRC_HEURISTIC = "Screening heuristic used by this tool (verify with EVE retrieval)"

# growing_months are for the northern hemisphere; they are shifted 6 months south of the equator.
CROPS: dict[str, dict[str, Any]] = {
    "rice": {
        "aliases": ["rice", "paddy"],
        "growing_months": [5, 6, 7, 8, 9],
        "water_need_mm": (450, 700),
        "usually_irrigated": True,
        "t_opt_c": (20, 30),
        "t_abs_c": (10, 36),
        "max_slope_pct": 2,
        "min_peak_ndvi": 0.6,
        "flood_months": [4, 5, 6],
    },
    "maize": {
        "aliases": ["maize", "corn"],
        "growing_months": [5, 6, 7, 8, 9],
        "water_need_mm": (500, 800),
        "usually_irrigated": False,
        "t_opt_c": (18, 33),
        "t_abs_c": (10, 40),
        "max_slope_pct": 8,
        "min_peak_ndvi": 0.6,
    },
    "wheat": {
        "aliases": ["wheat", "winter wheat", "cereal"],
        "growing_months": [3, 4, 5, 6],
        "water_need_mm": (450, 650),
        "usually_irrigated": False,
        "t_opt_c": (15, 23),
        "t_abs_c": (5, 30),
        "max_slope_pct": 8,
        "min_peak_ndvi": 0.6,
    },
    "soybean": {
        "aliases": ["soybean", "soya", "soy"],
        "growing_months": [6, 7, 8, 9],
        "water_need_mm": (450, 700),
        "usually_irrigated": False,
        "t_opt_c": (20, 33),
        "t_abs_c": (10, 38),
        "max_slope_pct": 8,
        "min_peak_ndvi": 0.6,
    },
    "generic": {
        "aliases": [],
        "growing_months": [4, 5, 6, 7, 8, 9],
        "water_need_mm": (400, 700),
        "usually_irrigated": False,
        "t_opt_c": (15, 30),
        "t_abs_c": (5, 38),
        "max_slope_pct": 8,
        "min_peak_ndvi": 0.5,
    },
}

STATUS_SCORE = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


def resolve_crop(crop: str) -> tuple[str, dict[str, Any]]:
    c = crop.strip().lower()
    for name, rules in CROPS.items():
        if c == name or any(a in c for a in rules["aliases"]):
            return name, rules
    return "generic", CROPS["generic"]


def growing_months(rules: dict, lat: float, override: list[int] | None = None) -> list[int]:
    months = override or rules["growing_months"]
    if lat < 0 and not override:
        months = [((m + 5) % 12) + 1 for m in months]
    return months


def _crit(name, status, value, threshold, source, detail, weight=1.0) -> dict:
    return {
        "criterion": name,
        "status": status,
        "value": value,
        "threshold": threshold,
        "source": source,
        "detail": detail,
        "weight": weight,
    }


def _band(value: float, good: float, ok: float, higher_is_better: bool = True) -> str:
    if higher_is_better:
        return "pass" if value >= good else "warn" if value >= ok else "fail"
    return "pass" if value <= good else "warn" if value <= ok else "fail"


def _season_years(scenes: list[dict], months: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for s in scenes:
        d = date.fromisoformat(s["date"])
        if d.month in months:
            # A season that crosses New Year belongs to the year in which it ends.
            year = d.year + 1 if months[0] > months[-1] and d.month >= months[0] else d.year
            out.setdefault(year, []).append(s)
    return out


def growing_season_weather(daily: dict, months: list[int], end: date, years_back: int = 12) -> list[tuple[int, dict]]:
    """(year, weather stats) for every complete growing season in the daily record, oldest first."""
    from sources import _window_stats

    seasons = []
    m0, m1 = months[0], months[-1]
    for year in range(end.year - years_back, end.year + 1):
        s_start = date(year - 1 if m0 > m1 else year, m0, 1)
        s_end = date(year, m1, calendar.monthrange(year, m1)[1])
        stats = _window_stats(daily, s_start, s_end)
        if stats and stats["n_days"] >= 0.9 * len(months) * 30 and s_end <= end:
            seasons.append((year, stats))
    return seasons


def _zscores(values: list[float | None]) -> list[float]:
    arr = np.array([np.nan if v is None else v for v in values], dtype=float)
    std = np.nanstd(arr, ddof=1) if np.isfinite(arr).sum() >= 2 else 0.0
    return list((arr - np.nanmean(arr)) / std) if std > 0 else [0.0] * len(values)


def drought_impact(crop: str, evidence: dict[str, Any], months_override: list[int] | None = None) -> dict[str, Any]:
    """How strongly droughts hit this area: per growing season weather and vegetation, plus a 0-1 index.

    The index averages three parts, each scaled to 0-1: how often growing seasons were dry
    (share of the record, 50% = 1), the drop of peak NDVI in the driest year of the period
    (0.2 = 1) and the drop of growing-season NDMI in that year (0.1 = 1).
    """
    _, rules = resolve_crop(crop)
    months = growing_months(rules, evidence["lat"], months_override)
    start: date = evidence["start"]
    end: date = evidence["end"]
    seasons = growing_season_weather(evidence["daily"], months, end) if evidence.get("daily") else []
    z_wb = _zscores([s["water_balance_mm"] for _, s in seasons])
    z_sm = _zscores([s["soil_moisture_0_7cm"] for _, s in seasons])
    weather = {
        y: {**s, "water_balance_z": round(float(a), 2), "soil_moisture_z": round(float(b), 2), "dry": bool(min(a, b) <= -1)}
        for (y, s), a, b in zip(seasons, z_wb, z_sm, strict=True)
    }
    by_year = {y: v for y, v in _season_years(evidence.get("scenes") or [], months).items() if len(v) >= 2}
    peak = {y: float(np.percentile([s["ndvi"] for s in v if s.get("ndvi") is not None], 90)) for y, v in by_year.items()}
    ndmi = {y: float(np.median([s["ndmi"] for s in v if s.get("ndmi") is not None])) for y, v in by_year.items()}

    years = []
    for y in range(start.year, end.year + 1):
        w = weather.get(y)
        if not w and y not in peak:
            continue
        years.append(
            {
                "year": y,
                "precip_mm": w["precip_mm"] if w else None,
                "water_balance_mm": w["water_balance_mm"] if w else None,
                "water_balance_z": w["water_balance_z"] if w else None,
                "soil_moisture_z": w["soil_moisture_z"] if w else None,
                "dry_season": w["dry"] if w else None,
                "peak_ndvi": round(peak[y], 3) if y in peak else None,
                "ndmi_median": round(ndmi[y], 3) if y in ndmi else None,
            }
        )

    dry_record = [y for y, w in weather.items() if w["dry"]]
    share_dry = len(dry_record) / len(weather) if weather else None
    in_period = [r for r in years if r["water_balance_z"] is not None]
    driest = min(in_period, key=lambda r: min(r["water_balance_z"], r["soil_moisture_z"]), default=None)
    ndvi_change = ndmi_change = None
    if driest and driest["peak_ndvi"] is not None:
        others = [r["peak_ndvi"] for r in years if r["year"] != driest["year"] and r["peak_ndvi"] is not None]
        ndvi_change = round(driest["peak_ndvi"] - float(np.mean(others)), 3) if others else None
    if driest and driest["ndmi_median"] is not None:
        others = [r["ndmi_median"] for r in years if r["year"] != driest["year"] and r["ndmi_median"] is not None]
        ndmi_change = round(driest["ndmi_median"] - float(np.mean(others)), 3) if others else None

    parts = []
    if share_dry is not None:
        parts.append(min(share_dry / 0.5, 1.0))
    if ndvi_change is not None:
        parts.append(min(max(-ndvi_change, 0.0) / 0.2, 1.0))
    if ndmi_change is not None:
        parts.append(min(max(-ndmi_change, 0.0) / 0.1, 1.0))
    index = round(float(np.mean(parts)), 2) if parts else None
    level = None if index is None else "low" if index < 0.33 else "moderate" if index < 0.66 else "high"
    return {
        "drought_impact_index": index,
        "drought_impact_level": level,
        "dry_seasons_in_record": dry_record,
        "share_dry_seasons": round(share_dry, 2) if share_dry is not None else None,
        "seasons_in_record": len(weather),
        "driest_year_in_period": driest["year"] if driest else None,
        "peak_ndvi_change_in_driest_year": ndvi_change,
        "ndmi_change_in_driest_year": ndmi_change,
        "growing_seasons": years,
        "how_to_read": "drought_impact_index 0-1 (higher = more affected) averages: share of dry growing seasons "
        "(50% = 1), peak-NDVI drop in the driest year of the period (0.2 = 1) and NDMI drop in that year (0.1 = 1).",
    }


def evaluate(crop: str, evidence: dict[str, Any], months_override: list[int] | None = None) -> dict[str, Any]:
    crop_key, rules = resolve_crop(crop)
    lat = evidence["lat"]
    months = growing_months(rules, lat, months_override)
    criteria: list[dict] = []
    caveats: list[str] = []
    if crop_key == "generic":
        caveats.append(f"No crop-specific rules for '{crop}'; generic thresholds were used.")

    # 1. Crop presence: WorldCereal survey samples and WorldCover cropland share.
    wc = evidence.get("worldcereal") or {}
    lc = evidence.get("landcover") or {}
    cropland = (lc.get("class_fractions") or {}).get("cropland")
    seen = [c for c in wc.get("crops", []) if any(a in c["label"].lower() for a in rules["aliases"] or [crop.lower()])]
    n_seen = sum(c["n_samples"] for c in seen)
    if cropland is not None or wc.get("available"):
        status = "pass" if n_seen and (cropland or 0) >= 0.3 else "warn" if n_seen or (cropland or 0) >= 0.3 else "fail"
        criteria.append(
            _crit(
                "crop_presence",
                status,
                {"worldcereal_samples_of_crop": n_seen, "worldcover_cropland_share": cropland},
                "crop surveyed nearby and cropland share >= 0.3",
                "ESA WorldCereal reference data; ESA WorldCover 2021",
                f"{n_seen} WorldCereal samples of {crop_key} within {wc.get('search_buffer_km', '?')} km; "
                f"cropland covers {cropland if cropland is not None else '?'} of the area.",
            )
        )

    # 2. Terrain.
    terrain = evidence.get("terrain") or {}
    if terrain.get("available"):
        classes = terrain["slope_classes"]
        share_ok = classes["flat_lt_2pct"] if rules["max_slope_pct"] <= 2 else classes["flat_lt_2pct"] + classes["gentle_2_to_8pct"]
        criteria.append(
            _crit(
                "terrain_slope",
                _band(share_ok, 0.7, 0.4),
                round(share_ok, 3),
                f">= 70% of the area below {rules['max_slope_pct']}% slope",
                SRC_SYS,
                f"{share_ok:.0%} of the area is below {rules['max_slope_pct']}% slope (Copernicus DEM 30 m).",
                weight=0.5,
            )
        )

    # 3-5. Weather per growing season (most recent complete season vs earlier ones).
    daily = evidence.get("daily")
    end: date = evidence["end"]
    if daily:
        seasons = growing_season_weather(daily, months, end)
        if seasons:
            year, latest = seasons[-1]
            base = [s for _, s in seasons[:-1]]
            t = latest["t_mean_c"]
            t_status = "pass" if rules["t_opt_c"][0] <= t <= rules["t_opt_c"][1] else "warn" if rules["t_abs_c"][0] <= t <= rules["t_abs_c"][1] else "fail"
            criteria.append(
                _crit(
                    "growing_season_temperature",
                    t_status,
                    {"year": year, "t_mean_c": t, "hot_days_gt_35c": latest["hot_days_gt_35c"]},
                    f"optimal {rules['t_opt_c'][0]}-{rules['t_opt_c'][1]} °C",
                    SRC_ECOCROP,
                    f"Mean temperature in months {months} of {year}: {t} °C.",
                    weight=0.75,
                )
            )
            need = rules["water_need_mm"][0]
            p = latest["precip_mm"]
            ratio = p / need
            status = _band(ratio, 1.0, 0.5)
            if rules["usually_irrigated"] and status != "pass":
                status = "warn"
            criteria.append(
                _crit(
                    "rainfall_vs_crop_water_need",
                    status,
                    {"year": year, "precip_mm": p, "et0_mm": latest["et0_mm"]},
                    f"{rules['water_need_mm'][0]}-{rules['water_need_mm'][1]} mm per season",
                    SRC_FAO_WATER,
                    f"Rain covered {ratio:.0%} of the minimum seasonal need"
                    + (" — this crop is usually irrigated, so the gap measures dependence on irrigation water." if rules["usually_irrigated"] else "."),
                    weight=0.75,
                )
            )
            if len(base) >= 2:
                sm = np.array([b["soil_moisture_0_7cm"] for b in base if b["soil_moisture_0_7cm"] is not None])
                wb = np.array([b["water_balance_mm"] for b in base])
                z_sm = (latest["soil_moisture_0_7cm"] - sm.mean()) / sm.std(ddof=1) if sm.size >= 2 and sm.std(ddof=1) > 0 else 0.0
                z_wb = (latest["water_balance_mm"] - wb.mean()) / wb.std(ddof=1) if wb.std(ddof=1) > 0 else 0.0
                z = float(min(z_sm, z_wb))
                criteria.append(
                    _crit(
                        "drought_signal_weather",
                        _band(z, -1.0, -2.0),
                        {"year": year, "soil_moisture_z": round(float(z_sm), 2), "water_balance_z": round(float(z_wb), 2), "baseline_seasons": len(base)},
                        "z-score > -1 (normal); -1 to -2 dry; < -2 severe",
                        SRC_HEURISTIC,
                        f"Growing season {year} vs {len(base)} earlier seasons (ERA5 via Open-Meteo).",
                        weight=1.25,
                    )
                )
            if len(seasons) >= 5:
                # Drought risk: how often a growing season was dry over the whole record, not just the latest one.
                sm_all = np.array([s["soil_moisture_0_7cm"] or np.nan for _, s in seasons], dtype=float)
                wb_all = np.array([s["water_balance_mm"] for _, s in seasons], dtype=float)
                z_sm_all = (sm_all - np.nanmean(sm_all)) / (np.nanstd(sm_all, ddof=1) or 1)
                z_wb_all = (wb_all - wb_all.mean()) / (wb_all.std(ddof=1) or 1)
                dry = [y for (y, _), a, b in zip(seasons, z_sm_all, z_wb_all, strict=True) if min(a, b) <= -1]
                share = len(dry) / len(seasons)
                criteria.append(
                    _crit(
                        "drought_frequency",
                        _band(share, 0.2, 0.4, higher_is_better=False),
                        {"dry_seasons": dry, "seasons_checked": len(seasons), "share_dry": round(share, 2)},
                        "dry growing season (z <= -1) in at most 20% of years",
                        SRC_HEURISTIC,
                        f"{len(dry)} of {len(seasons)} growing seasons {seasons[0][0]}-{seasons[-1][0]} were dry "
                        "(soil moisture or water balance at least 1 std below the mean).",
                        weight=1.25,
                    )
                )
            for y, s in seasons[-4:]:
                caveats_line = f"{y}: precip {s['precip_mm']} mm, P-ET0 {s['water_balance_mm']} mm, soil moisture {s['soil_moisture_0_7cm']}"
                evidence.setdefault("_season_weather", []).append(caveats_line)

    # 6-8. Vegetation from Sentinel-2.
    scenes = evidence.get("scenes") or []
    by_year = _season_years(scenes, months)
    by_year = {y: v for y, v in by_year.items() if len(v) >= 2}
    if by_year:
        latest_year = max(by_year)
        peak = {y: float(np.percentile([s["ndvi"] for s in v if s.get("ndvi") is not None], 90)) for y, v in by_year.items()}
        criteria.append(
            _crit(
                "vegetation_vigour_peak_ndvi",
                _band(peak[latest_year], rules["min_peak_ndvi"], rules["min_peak_ndvi"] - 0.2),
                {"year": latest_year, "peak_ndvi_p90": round(peak[latest_year], 3), "other_years": {y: round(v, 3) for y, v in peak.items() if y != latest_year}},
                f">= {rules['min_peak_ndvi']}",
                SRC_HEURISTIC,
                "Upper (p90) of the per-scene median NDVI over cropland in the growing season.",
            )
        )
        ndmi = {y: float(np.median([s["ndmi"] for s in v if s.get("ndmi") is not None])) for y, v in by_year.items()}
        others = [v for y, v in ndmi.items() if y != latest_year]
        if others:
            diff = ndmi[latest_year] - float(np.mean(others))
            criteria.append(
                _crit(
                    "vegetation_moisture_ndmi_anomaly",
                    _band(diff, -0.05, -0.1),
                    {"year": latest_year, "ndmi_median": round(ndmi[latest_year], 3), "anomaly": round(diff, 3), "other_years": {y: round(v, 3) for y, v in ndmi.items() if y != latest_year}},
                    "anomaly > -0.05 normal; -0.05 to -0.10 stressed; < -0.10 strongly stressed",
                    SRC_HEURISTIC,
                    f"Growing-season NDMI in {latest_year} vs the mean of {len(others)} other year(s).",
                    weight=1.25,
                )
            )
        if crop_key == "rice":
            flood_months = growing_months({"growing_months": rules["flood_months"]}, lat)
            flood_share: dict[int, float] = {}
            for s in scenes:
                d = date.fromisoformat(s["date"])
                if d.month in flood_months and s.get("flooded_fraction") is not None:
                    flood_share[d.year] = max(flood_share.get(d.year, 0.0), s["flooded_fraction"])
            if flood_share:
                flooded_years = sorted(y for y, v in flood_share.items() if v >= 0.2)
                share = len(flooded_years) / len(flood_share)
                criteria.append(
                    _crit(
                        "rice_flooding_signal",
                        "pass" if share >= 0.99 else "warn",
                        {"max_flooded_share_of_cropland": {y: round(v, 2) for y, v in sorted(flood_share.items())}, "years_with_signal": flooded_years},
                        ">= 20% of cropland looks flooded at least once in the flooding months, every year",
                        SRC_XIAO + " (per pixel; NDVI used instead of EVI)",
                        "Paddy rice is usually flooded around sowing/transplanting. A missing signal can mean little rice, "
                        "dry-seeded rice, clouds at the key dates (one scene per month), or no water for flooding.",
                        weight=0.75,
                    )
                )
    else:
        caveats.append("Too few clear Sentinel-2 scenes in the growing season to judge vegetation.")

    scored = [c for c in criteria if c["status"] in STATUS_SCORE]
    total_w = sum(c["weight"] for c in scored)
    score = sum(STATUS_SCORE[c["status"]] * c["weight"] for c in scored) / total_w if total_w else None
    expected = 9 if crop_key == "rice" else 8
    confidence = "high" if len(scored) >= expected - 1 else "medium" if len(scored) >= expected - 3 else "low"
    if score is None:
        verdict = "insufficient_data"
    elif score >= 0.75:
        verdict = "likely_suitable"
    elif score >= 0.5:
        verdict = "marginal_at_risk"
    else:
        verdict = "unlikely_suitable"
    return {
        "crop": crop_key,
        "growing_months": months,
        "verdict": verdict,
        "score": round(score, 2) if score is not None else None,
        "confidence": confidence,
        "criteria": criteria,
        "recent_growing_seasons_weather": evidence.pop("_season_weather", [])[-4:],
        "caveats": caveats
        + [
            "This is a screening from past observations, not a yield forecast.",
            "Weather is from one ERA5 grid cell at the bbox centre (~10 km).",
        ],
    }


def criteria_table(criteria: list[dict]) -> str:
    """Markdown table of every criterion, so the agent can show it without dropping rows."""
    icon = {"pass": "✅ pass", "warn": "⚠️ warn", "fail": "❌ fail"}
    lines = ["| Criterion | Status | Detail | Threshold | Source |", "|---|---|---|---|---|"]
    for c in criteria:
        cells = [c["criterion"].replace("_", " "), icon.get(c["status"], c["status"]), c["detail"], c["threshold"], c["source"]]
        lines.append("| " + " | ".join(str(x).replace("|", "/") for x in cells) + " |")
    return "\n".join(lines)
