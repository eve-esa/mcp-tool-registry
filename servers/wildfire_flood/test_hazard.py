"""Unit tests for hazard.py: the Staley M1 model, thresholds and rain series."""

import hazard
import pytest

M15 = hazard.M1_MODELS[15]


def test_threshold_gives_fifty_percent():
    t, f, s = 0.4, 0.7, 0.25
    rain = hazard.threshold_mm(M15, t, f, s)
    assert hazard.likelihood(M15, t, f, s, rain) == pytest.approx(0.5)


def test_threshold_none_without_burn_terms():
    assert hazard.threshold_mm(M15, 0, 0, 0) is None
    assert hazard.likelihood(M15, 0, 0, 0, 50) == pytest.approx(1 / (1 + 2.718281828**3.63), rel=1e-3)


@pytest.mark.parametrize(("p", "level"), [(0.05, "low"), (0.2, "medium"), (0.49, "medium"), (0.5, "high")])
def test_risk_level(p, level):
    assert hazard.risk_level(p) == level


def test_exceedances_merge_close_bursts():
    times = ["2025-02-14T00:00", "2025-02-14T00:15", "2025-02-14T00:30", "2025-02-14T03:00"]
    events = hazard.exceedances(times, [5.0, 1.0, 6.0, 7.0], threshold=4.0, step_min=15)
    assert len(events) == 2
    assert events[0]["from_utc"] == "2025-02-14T00:00" and events[0]["to_utc"] == "2025-02-14T00:30"
    assert events[0]["peak_mm"] == 6.0 and events[0]["minutes_above"] == 30


def _rain(mm_15):
    """Two hours of rain in the fetch_rain format; mm_15=None means no 15-minute data."""
    times_15 = [f"2025-02-14T{h:02d}:{m:02d}" for h in range(2) for m in (0, 15, 30, 45)]
    mm_60 = [sum(mm_15[0:4]), sum(mm_15[4:8])] if mm_15 else [8.0, 2.0]
    return {"times_15": times_15, "mm_15": mm_15, "times_60": ["2025-02-14T01:00", "2025-02-14T02:00"],
            "mm_60": mm_60}


def test_assess_uses_15_minute_data_when_available():
    result = hazard.assess(0.45, 0.8, 0.25, _rain([0, 1, 12, 1, 0, 0, 0, 0]))
    assert set(result["by_duration"]) == {"15_min", "30_min", "60_min"}
    assert result["risk_level"] == "high" and result["burst_duration_min"] == 15
    assert result["bursts_above_threshold"][0]["peak_mm"] == 12


def test_assess_falls_back_to_hourly():
    result = hazard.assess(0.45, 0.8, 0.25, _rain(None))
    assert set(result["by_duration"]) == {"60_min"} and result["burst_duration_min"] == 60


def test_assess_dry_window_is_low():
    result = hazard.assess(0.45, 0.8, 0.25, _rain([0.0] * 8))
    assert result["risk_level"] == "low" and result["bursts_above_threshold"] == []
