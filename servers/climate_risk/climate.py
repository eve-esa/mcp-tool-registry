"""
Climate statistics for the Climate Risk MCP server (the "maths" layer).

Responsibility: turn a daily ERA5 series into the numbers the tools report —
location handling, period anomalies, hot-day counts and the empirical SPI/SPEI
drought indices with their McKee et al. (1993) classes.

How it fits:
    server.py     calls these functions from the MCP tools and formats the JSON.
    era5_store.py produces the ``Series`` consumed here (from its disk cache).
    charts.py     draws the monthly index lists produced by ``index_series``.

Everything here is a pure function (no network, no MCP, no disk), so it can be
unit-tested offline with synthetic climates (see test_climate.py).

Data model: a daily series is a ``dict[date, dict[str, float | None]]`` keyed
by the variable names in ``VARIABLES``; ``None`` marks a day ERA5 has not
published yet. Monthly aggregates are ``dict[(year, month), float]``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date, timedelta
from statistics import NormalDist, mean, pstdev
from typing import Any

# Open-Meteo daily variable names requested from ERA5. Units: °C for the two
# temperatures (2 m above ground), mm/day for precipitation and for the FAO-56
# Penman-Monteith reference evapotranspiration (ET0) used by SPEI.
VARIABLES = (
    "temperature_2m_mean",
    "temperature_2m_max",
    "precipitation_sum",
    "et0_fao_evapotranspiration",
)

# Daily series: date -> {variable: value or None when not yet published}.
Series = dict[date, dict[str, float | None]]

# McKee et al. (1993) classes, shared by SPI and SPEI: (lower bound, label, risk).
# Checked top-down, so the first bound the index reaches wins; anything below
# -2.0 falls through to "extremely dry" in classify_index. Only dry classes
# raise the risk level: this server reports drought risk, not flood risk.
_CLASSES = (
    (2.0, "extremely wet", "low"),
    (1.5, "very wet", "low"),
    (1.0, "moderately wet", "low"),
    (-1.0, "near normal", "low"),
    (-1.5, "moderately dry", "medium"),
    (-2.0, "severely dry", "high"),
)
# Severity rank of each risk level, so the worst one can be picked with max().
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


# ─── Location ────────────────────────────────────────────────────────────────


def _positions(obj: Any) -> Iterable[tuple[float, float]]:
    """Yield every (lon, lat) position found in a GeoJSON object.

    Walks any GeoJSON shape (FeatureCollection, Feature, GeometryCollection or
    a bare geometry) recursively, so callers need not care which one an agent
    sent. A position is recognised as a list whose first two items are
    numbers; GeoJSON order is (longitude, latitude), and any altitude is
    ignored.
    """
    if isinstance(obj, dict):
        kind = obj.get("type")
        if kind == "FeatureCollection":
            for feature in obj.get("features", []):
                yield from _positions(feature)
        elif kind == "Feature":
            yield from _positions(obj.get("geometry"))
        elif kind == "GeometryCollection":
            for geom in obj.get("geometries", []):
                yield from _positions(geom)
        elif "coordinates" in obj:
            yield from _positions(obj["coordinates"])
    elif isinstance(obj, (list, tuple)):
        if len(obj) >= 2 and all(isinstance(v, (int, float)) for v in obj[:2]):
            yield float(obj[0]), float(obj[1])
        else:
            for item in obj:
                yield from _positions(item)


def geometry_centroid(geometry: str | dict) -> tuple[float, float]:
    """Return (lat, lon) as the mean of all vertices of a GeoJSON geometry.

    Good enough for farm-sized plots: ERA5 cells are ~25 km wide, so the exact
    centroid formula does not change which cell is selected. (A closed ring's
    repeated first vertex is counted twice; that bias is negligible here.)

    Args:
        geometry: GeoJSON as a dict or as a JSON string.

    Raises:
        ValueError: If the string is not valid JSON or holds no coordinates.
    """
    try:
        obj = json.loads(geometry) if isinstance(geometry, str) else geometry
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"geometry is not valid GeoJSON ({exc.msg}); pass a GeoJSON object, a "
            "'west,south,east,north' bbox, or latitude and longitude"
        ) from exc
    points = list(_positions(obj))
    if not points:
        raise ValueError("geometry has no coordinates")
    lon = mean(p[0] for p in points)
    lat = mean(p[1] for p in points)
    return lat, lon


def parse_bbox(text: str) -> tuple[float, float, float, float] | None:
    """Return (west, south, east, north) from "west,south,east,north", or None if ``text`` is not one."""
    parts = text.split(",")
    if len(parts) != 4:
        return None
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError:
        return None
    return west, south, east, north


def resolve_location(
    latitude: float | None,
    longitude: float | None,
    geometry: str | dict | None,
) -> tuple[float, float, str]:
    """Return (lat, lon, how) from either a lat/lon pair or a GeoJSON geometry.

    The geometry wins when both are given. It may also be a bounding box string
    "west,south,east,north", the format geocode_place returns, so a place name
    can go straight from the geocoder to these tools. ``how`` ("point",
    "geometry centroid" or "bbox centre") is echoed in the provenance so the
    agent knows which input was used.

    Raises:
        ValueError: If neither input is usable or the point is off the globe.
    """
    bbox = parse_bbox(geometry) if isinstance(geometry, str) else None
    if bbox:
        west, south, east, north = bbox
        lat, lon, how = (south + north) / 2, (west + east) / 2, "bbox centre"
    elif geometry:
        lat, lon = geometry_centroid(geometry)
        how = "geometry centroid"
    elif latitude is not None and longitude is not None:
        lat, lon, how = float(latitude), float(longitude), "point"
    else:
        raise ValueError("provide latitude and longitude, or a GeoJSON geometry")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError(f"coordinates out of range: lat={lat}, lon={lon}")
    return lat, lon, how


# ─── Dates ───────────────────────────────────────────────────────────────────


def shift_years(d: date, years: int) -> date:
    """Move a date by whole years; 29 February becomes 28 February.

    Used to find "the same calendar window" in each baseline year; clamping
    the leap day keeps every baseline window valid instead of raising.
    """
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def last_complete_day(series: Series, var: str = "precipitation_sum") -> date:
    """Return the latest day that has a value for ``var``.

    Open-Meteo returns the requested range up to today, but ERA5 is published
    with ~5-7 days of delay, so the last days are ``None``. Precipitation is
    the default probe because every tool depends on it.

    Raises:
        ValueError: If the series has no value at all for ``var``.
    """
    days = [d for d, v in series.items() if v.get(var) is not None]
    if not days:
        raise ValueError("no data in series")
    return max(days)


def window_values(series: Series, var: str, start: date, end: date) -> list[float] | None:
    """Return the daily values of ``var`` from start to end inclusive.

    Returns None (instead of a shorter list) if any day is missing, so a
    partial window is never silently compared with complete baseline windows.
    """
    out: list[float] = []
    day = start
    while day <= end:
        value = series.get(day, {}).get(var)
        if value is None:
            return None
        out.append(value)
        day += timedelta(days=1)
    return out


# ─── Statistics ──────────────────────────────────────────────────────────────


def empirical_percentile(value: float, baseline: list[float]) -> float:
    """Return the percentile rank (0-100) of value within the baseline.

    Ties count half (the "mid-rank" convention), so a value equal to every
    baseline value sits at the 50th percentile rather than at 0 or 100.
    """
    below = sum(1 for b in baseline if b < value)
    equal = sum(1 for b in baseline if b == value)
    return 100.0 * (below + 0.5 * equal) / len(baseline)


def standardized_index(value: float, baseline: list[float]) -> float:
    """Non-parametric standardized index (empirical SPI/SPEI).

    Ranks the value within the baseline with the Gringorten plotting position
    and maps that probability through the inverse standard normal. With a
    30-year baseline the index is bounded to about ±2.3, which still spans
    every McKee class.

    Why non-parametric: the classic SPI fits a gamma distribution to
    precipitation (and SPEI a log-logistic to the water balance); those fits
    are fragile with zero-rain months and short baselines. Ranking avoids
    choosing a distribution at all.

    Why Gringorten: the plotting position (rank - 0.44) / (n + 0.12) is the
    standard unbiased choice for extremes and never yields p = 0 or p = 1,
    where the inverse normal would be infinite. The value is ranked as if it
    were added to the baseline (hence ``n = len(baseline) + 1``).

    Raises:
        ValueError: If the baseline has fewer than 10 values.
    """
    if len(baseline) < 10:
        raise ValueError("baseline needs at least 10 values")
    n = len(baseline) + 1  # the value itself joins the sample
    rank = 1 + sum(1 for b in baseline if b < value) + 0.5 * sum(1 for b in baseline if b == value)
    p = (rank - 0.44) / (n + 0.12)
    return NormalDist().inv_cdf(p)


def classify_index(index: float) -> tuple[str, str]:
    """Return (McKee class, risk level) for an SPI/SPEI value."""
    for lower, label, risk in _CLASSES:
        if index >= lower:
            return label, risk
    return "extremely dry", "high"


def worst_risk(levels: Iterable[str]) -> str:
    """Return the most severe risk level in ``levels`` ("low" if empty)."""
    return max(levels, key=_RISK_ORDER.__getitem__, default="low")


# ─── Monthly accumulations (SPI / SPEI) ──────────────────────────────────────


def monthly_totals(series: Series, var: str) -> dict[tuple[int, int], float]:
    """Return monthly sums of ``var``, only for months where every day has a value.

    Incomplete months (the current one, or one with a gap) are dropped rather
    than summed, because a partial month would look artificially dry.
    """
    sums: dict[tuple[int, int], float] = {}
    counts: dict[tuple[int, int], int] = {}
    bad: set[tuple[int, int]] = set()  # months with at least one missing day
    for day, values in series.items():
        key = (day.year, day.month)
        value = values.get(var)
        if value is None:
            bad.add(key)
            continue
        sums[key] = sums.get(key, 0.0) + value
        counts[key] = counts.get(key, 0) + 1
    complete = {}
    for (year, month), total in sums.items():
        # First day of next month minus first day of this month (handles December and leap years).
        days_in_month = ((date(year + month // 12, month % 12 + 1, 1)) - date(year, month, 1)).days
        if (year, month) not in bad and counts[(year, month)] == days_in_month:
            complete[(year, month)] = total
    return complete


def water_balance(series: Series) -> dict[tuple[int, int], float]:
    """Return monthly precipitation minus FAO-56 reference evapotranspiration (mm).

    This climatic water balance is the input of SPEI: unlike SPI it also drops
    when heat raises evaporative demand, even if rainfall is normal. Only
    months complete in both variables are kept.
    """
    precip = monthly_totals(series, "precipitation_sum")
    et0 = monthly_totals(series, "et0_fao_evapotranspiration")
    return {k: precip[k] - et0[k] for k in precip.keys() & et0.keys()}


def parse_month(text: str) -> tuple[int, int]:
    """Return (year, month) from "YYYY-MM".

    Agents often send a full date for a month field, so "YYYY-MM-DD" is read
    as its month instead of being refused.

    Raises:
        ValueError: Naming the expected format, so the agent can retry.
    """
    match = re.fullmatch(r"\s*(\d{4})-(\d{1,2})(?:-\d{1,2})?\s*", str(text))
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise ValueError(f"end_month must be YYYY-MM, for example 2026-09; got {text!r}")
    return int(match.group(1)), int(match.group(2))


def choose_end_month(
    requested: str | None, latest: tuple[int, int]
) -> tuple[int, int, str | None]:
    """Return (year, month, note) for the end of the accumulation window.

    Without a request this is the latest complete ERA5 month. A request later
    than that (agents often ask for the current, unfinished month) is moved
    back to it instead of failing, and the note says so: refusing it made
    small models retry with a date a whole year too early.
    """
    if not requested:
        return latest[0], latest[1], None
    year, month = parse_month(requested)
    if (year, month) > latest:
        note = (
            f"end_month {year}-{month:02d} has no complete ERA5 data yet; "
            f"used the latest complete month, {latest[0]}-{latest[1]:02d}"
        )
        return latest[0], latest[1], note
    return year, month, None


def _month_back(year: int, month: int, back: int) -> tuple[int, int]:
    """Return the (year, month) that lies ``back`` months before (year, month).

    Counting months on one linear axis (year * 12 + month index) makes
    crossing year boundaries trivial.
    """
    index = year * 12 + (month - 1) - back
    return index // 12, index % 12 + 1


def accumulate(monthly: dict[tuple[int, int], float], year: int, month: int, scale: int) -> float | None:
    """Return the sum of the ``scale`` months ending at (year, month).

    This is the "n-month accumulation" of SPI/SPEI (e.g. SPI-3 uses the last
    3 months). Returns None if any of those months is missing.
    """
    total = 0.0
    for back in range(scale):
        key = _month_back(year, month, back)
        if key not in monthly:
            return None
        total += monthly[key]
    return total


def last_complete_month(monthly: dict[tuple[int, int], float]) -> tuple[int, int]:
    """Return the latest (year, month) in a ``monthly_totals`` result.

    Raises:
        ValueError: If there is no complete month at all.
    """
    if not monthly:
        raise ValueError("no complete month in series")
    return max(monthly)


def standardized_for_month(
    monthly: dict[tuple[int, int], float],
    year: int,
    month: int,
    scale: int,
    baseline_years: range,
) -> dict[str, Any]:
    """Compute the SPI/SPEI-style index for the ``scale``-month window ending at (year, month).

    Pass precipitation totals to get SPI, or ``water_balance`` to get SPEI.
    The current accumulation is ranked against the accumulation ending in the
    same calendar month of each baseline year, which removes seasonality
    (a dry July is compared with other Julys, not with the rainy season).

    Returns:
        A JSON-ready dict: scale, period covered, accumulated value (mm),
        baseline median (mm), index, McKee class, risk level and the number
        of baseline years actually used.

    Raises:
        ValueError: If the current window is incomplete or fewer than 10
            baseline years have complete data.
    """
    current = accumulate(monthly, year, month, scale)
    if current is None:
        raise ValueError(f"no complete {scale}-month data ending {year}-{month:02d}")
    # Baseline years with a gap are skipped rather than failing the whole index.
    baseline = [
        v for y in baseline_years if (v := accumulate(monthly, y, month, scale)) is not None
    ]
    index = standardized_index(current, baseline)
    label, risk = classify_index(index)
    return {
        "scale_months": scale,
        "period": f"{_month_back(year, month, scale - 1)[0]}-{_month_back(year, month, scale - 1)[1]:02d}"
        f" to {year}-{month:02d}",
        "value_mm": round(current, 1),
        "baseline_median_mm": round(sorted(baseline)[len(baseline) // 2], 1),
        "index": round(index, 2),
        "class": label,
        "risk_level": risk,
        "baseline_samples": len(baseline),
    }


# ─── Period anomalies ────────────────────────────────────────────────────────


def period_anomaly(
    series: Series,
    var: str,
    start: date,
    end: date,
    baseline_years: range,
    aggregate: str,
) -> dict[str, Any]:
    """Compare one period with the same calendar window in each baseline year.

    Using the same calendar window (not a yearly average) removes the seasonal
    cycle, so "anomaly" means "unusual for this place at this time of year".
    The window is shifted relative to ``start.year`` so periods crossing New
    Year stay contiguous in every baseline year.

    Args:
        series: Daily ERA5 series covering the period and the baseline.
        var: Variable name from ``VARIABLES``.
        start: First day of the period (inclusive).
        end: Last day of the period (inclusive).
        baseline_years: Reference years, e.g. ``range(1991, 2021)``.
        aggregate: "mean" (temperatures) or "sum" (precipitation).

    Returns:
        A JSON-ready dict with value, baseline mean/min/max, anomaly,
        z-score (None if the baseline has no spread), empirical percentile,
        sample count and, for sums, ``percent_of_normal``.

    Raises:
        ValueError: If the period is incomplete or fewer than 10 baseline
            years have complete data.
    """
    agg = mean if aggregate == "mean" else sum
    values = window_values(series, var, start, end)
    if values is None:
        raise ValueError(f"no complete {var} data between {start} and {end}")
    current = agg(values)

    baseline = []
    for year in baseline_years:
        shift = year - start.year
        window = window_values(series, var, shift_years(start, shift), shift_years(end, shift))
        if window is not None:
            baseline.append(agg(window))
    if len(baseline) < 10:
        raise ValueError("not enough baseline years with complete data")

    normal = mean(baseline)
    spread = pstdev(baseline)
    result = {
        "value": round(current, 2),
        "baseline_mean": round(normal, 2),
        "anomaly": round(current - normal, 2),
        "z_score": round((current - normal) / spread, 2) if spread else None,
        "percentile": round(empirical_percentile(current, baseline), 1),
        "baseline_min": round(min(baseline), 2),
        "baseline_max": round(max(baseline), 2),
        "baseline_samples": len(baseline),
    }
    if aggregate == "sum":
        result["percent_of_normal"] = round(100.0 * current / normal, 1) if normal else None
    return result


def hot_days(
    series: Series,
    end: date,
    days: int,
    baseline_years: range,
    percentile: float = 90.0,
) -> dict[str, Any]:
    """Count days whose maximum temperature exceeds the local baseline percentile.

    The threshold is the given percentile of all daily maxima in the same
    calendar window across the baseline years, so "hot" is relative to what is
    normal at this place and season (35 °C is ordinary in Seville in July but
    exceptional in Oslo).

    Risk compares the count with the number expected by chance: with the
    90th percentile, 10 % of days (9 of 90) are "hot" in a normal season.
    Twice that is "medium", three times or more is "high".

    Args:
        series: Daily ERA5 series covering the window and the baseline.
        end: Last day of the window (usually the last day with data).
        days: Window length in days.
        baseline_years: Reference years.
        percentile: Threshold percentile of the baseline daily maxima.

    Raises:
        ValueError: If the window is incomplete or no baseline data exists.
    """
    start = end - timedelta(days=days - 1)
    current = window_values(series, "temperature_2m_max", start, end)
    if current is None:
        raise ValueError(f"no complete temperature data between {start} and {end}")
    pool: list[float] = []
    for year in baseline_years:
        shift = year - end.year
        window = window_values(
            series, "temperature_2m_max", shift_years(start, shift), shift_years(end, shift)
        )
        if window is not None:
            pool.extend(window)
    if not pool:
        raise ValueError("no baseline temperature data")
    pool.sort()
    # Nearest-rank percentile; min() guards the index when percentile is 100.
    threshold = pool[min(len(pool) - 1, int(len(pool) * percentile / 100.0))]
    count = sum(1 for v in current if v > threshold)
    expected = days * (100.0 - percentile) / 100.0
    ratio = count / expected if expected else 0.0
    risk = "high" if ratio >= 3 else "medium" if ratio >= 2 else "low"
    return {
        "period": f"{start} to {end}",
        "threshold_c": round(threshold, 1),
        "threshold_definition": f"p{percentile:g} of daily max temperature, same window, baseline years",
        "hot_days": count,
        "expected_hot_days": round(expected, 1),
        "ratio_to_expected": round(ratio, 2),
        "risk_level": risk,
    }


def index_series(
    monthly: dict[tuple[int, int], float],
    year: int,
    month: int,
    months: int,
    scale: int,
    baseline_years: range,
) -> list[dict[str, Any]]:
    """Return the monthly SPI or SPEI time series for the ``months`` months ending at (year, month).

    This is the data behind the drought chart. Months without enough data
    (e.g. at the start of the archive) are skipped instead of failing, so the
    chart simply starts later.

    Returns:
        Chronological list of ``{"month": "YYYY-MM", "index": float, "class": str}``.
    """
    series = []
    for back in range(months - 1, -1, -1):
        y, m = _month_back(year, month, back)
        try:
            entry = standardized_for_month(monthly, y, m, scale, baseline_years)
        except ValueError:
            continue
        series.append({"month": f"{y}-{m:02d}", "index": entry["index"], "class": entry["class"]})
    return series
