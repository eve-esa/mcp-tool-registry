"""
Post-fire debris-flow / flash-flood likelihood for the Wildfire Flood MCP server
(pure Python, no I/O).

Model: USGS M1 logistic regression of Staley et al. (2017), the model behind
the USGS emergency post-fire debris-flow assessments:

    x = b0 + b1·T·R + b2·F·R + b3·S·R,   likelihood = 1 / (1 + e^-x)

    T  share of the area burned at moderate/high severity on slopes >= 23°
    F  mean dNBR / 1000 of the burned area
    S  soil erodibility (KF factor); not mapped globally, so the agent may pass
       one and a mid-range default is used otherwise
    R  peak rainfall accumulation (mm) over the model's duration (15, 30 or 60 min)

Solving x = logit(p) for R gives the rainfall threshold for a likelihood p; the
USGS publishes the 50 % threshold, and so does this server.

Reference: Staley, D.M., et al. (2017). Prediction of spatially explicit
rainfall intensity-duration thresholds for post-fire debris-flow generation in
the western United States. Geomorphology 278, 149-162.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

DEFAULT_SOIL_KF = 0.25  # mid-range KF of burned mountain soils in the model's calibration data


@dataclass(frozen=True)
class Model:
    """Coefficients of Staley M1 for one rainfall duration."""

    duration_min: int
    b0: float
    b1: float
    b2: float
    b3: float


M1_MODELS = {
    15: Model(15, -3.63, 0.41, 0.67, 0.70),
    30: Model(30, -3.61, 0.26, 0.39, 0.50),
    60: Model(60, -3.21, 0.17, 0.20, 0.22),
}


def _slope_term(model: Model, t: float, f: float, s: float) -> float:
    """b1·T + b2·F + b3·S: how strongly each mm of rain raises the log-odds."""
    return model.b1 * t + model.b2 * f + model.b3 * s


def likelihood(model: Model, t: float, f: float, s: float, rain_mm: float) -> float:
    """Debris-flow likelihood (0-1) for a peak accumulation rain_mm."""
    x = model.b0 + _slope_term(model, t, f, s) * rain_mm
    return 1 / (1 + math.exp(-x))


def threshold_mm(model: Model, t: float, f: float, s: float, p: float = 0.5) -> float | None:
    """Peak accumulation (mm over the model duration) giving likelihood p, or
    None when the burn terms are zero (no rain is enough)."""
    term = _slope_term(model, t, f, s)
    if term <= 0:
        return None
    return (math.log(p / (1 - p)) - model.b0) / term


def risk_level(p: float) -> str:
    """Screening level: high from 50 % (the USGS threshold), medium from 20 %."""
    if p >= 0.5:
        return "high"
    if p >= 0.2:
        return "medium"
    return "low"


def rolling_sum(values: list[float | None], window: int) -> list[float | None]:
    """Sum over the last `window` steps (None if any step is missing)."""
    out: list[float | None] = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1) : i + 1]
        out.append(sum(chunk) if len(chunk) == window and None not in chunk else None)
    return out


def peak(times: list[str], values: list[float | None]) -> tuple[float, str | None]:
    """Largest value and the time step it ends at."""
    best, when = 0.0, None
    for time, value in zip(times, values, strict=True):
        if value is not None and value > best:
            best, when = value, time
    return best, when


def exceedances(times: list[str], values: list[float | None], threshold: float, step_min: int) -> list[dict]:
    """Rain bursts at or above a threshold, merged when less than an hour apart."""
    events: list[dict] = []
    for time, value in zip(times, values, strict=True):
        if value is None or value < threshold:
            continue
        stamp = datetime.fromisoformat(time)
        if events and (stamp - datetime.fromisoformat(events[-1]["last"])).total_seconds() <= 3600:
            events[-1]["last"] = time
            events[-1]["peak_mm"] = max(events[-1]["peak_mm"], value)
            events[-1]["steps"] += 1
        else:
            events.append({"first": time, "last": time, "peak_mm": value, "steps": 1})
    return [
        {
            "from_utc": e["first"],
            "to_utc": e["last"],
            "peak_mm": round(e["peak_mm"], 1),
            "minutes_above": e["steps"] * step_min,
        }
        for e in events
    ]


def assess(t: float, f: float, s: float, rain: dict) -> dict:
    """Likelihood and rainfall thresholds for the burn terms and a rain series.

    Args:
        t, f, s: Staley M1 terms (see the module docstring).
        rain: {"times_15": [...], "mm_15": [...] or None, "times_60": [...],
               "mm_60": [...]} with accumulations per 15-minute / hourly step,
               times in ISO UTC marking the end of each step.

    Returns:
        A dict with, per duration (15, 30, 60 min): the 50 % rainfall threshold
        (mm and mm/h), the peak accumulation in the window, its time, and the
        likelihood; then the controlling (highest) likelihood, its risk level
        and the bursts above the 15-minute (or hourly) threshold.
    """
    series: dict[int, tuple[list[str], list[float | None]]] = {60: (rain["times_60"], rain["mm_60"])}
    if rain.get("mm_15"):
        series[15] = (rain["times_15"], rain["mm_15"])
        series[30] = (rain["times_15"], rolling_sum(rain["mm_15"], 2))

    by_duration = {}
    for minutes in sorted(series):
        model = M1_MODELS[minutes]
        times, values = series[minutes]
        rain_peak, at = peak(times, values)
        threshold = threshold_mm(model, t, f, s)
        by_duration[f"{minutes}_min"] = {
            "threshold_50pct_mm": round(threshold, 1) if threshold is not None else None,
            "threshold_50pct_mm_per_h": round(threshold * 60 / minutes, 1) if threshold is not None else None,
            "peak_rain_mm": round(rain_peak, 1),
            "peak_at_utc": at,
            "likelihood": round(likelihood(model, t, f, s, rain_peak), 3),
        }

    controlling = max(by_duration, key=lambda k: by_duration[k]["likelihood"])
    p = by_duration[controlling]["likelihood"]
    burst_minutes = 15 if 15 in series else 60
    burst_threshold = by_duration[f"{burst_minutes}_min"]["threshold_50pct_mm"]
    times, values = series[burst_minutes]
    return {
        "likelihood": p,
        "risk_level": risk_level(p),
        "controlling_duration": controlling,
        "by_duration": by_duration,
        "bursts_above_threshold": (
            exceedances(times, values, burst_threshold, burst_minutes) if burst_threshold is not None else []
        ),
        "burst_duration_min": burst_minutes,
        "terms": {"T": round(t, 3), "F": round(f, 3), "S": round(s, 3)},
    }
