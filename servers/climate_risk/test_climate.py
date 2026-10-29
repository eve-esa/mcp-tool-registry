"""Offline unit tests for climate.py (no network, no MCP, no LLM).

Small cases check each helper; the end-to-end cases build synthetic 1989-2026
climates with a planted drought, heatwave or hot week and check that the
indices detect it.

Run from this directory: python -m pytest test_climate.py -q
"""

from datetime import date, timedelta

import climate
import pytest


def make_series(start: date, end: date, fn) -> climate.Series:
    """Build a daily series where fn(day) returns (tmean, tmax, precip, et0)."""
    series: climate.Series = {}
    day = start
    while day <= end:
        tmean, tmax, precip, et0 = fn(day)
        series[day] = {
            "temperature_2m_mean": tmean,
            "temperature_2m_max": tmax,
            "precipitation_sum": precip,
            "et0_fao_evapotranspiration": et0,
        }
        day += timedelta(days=1)
    return series


BASELINE = range(1991, 2021)  # the default WMO 1991-2020 normal


# ─── Location ────────────────────────────────────────────────────────────────


def test_resolve_polygon_centroid():
    square = '{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,2],[0,2],[0,0]]]}'
    lat, lon, how = climate.resolve_location(None, None, square)
    assert how == "geometry centroid"
    assert lat == pytest.approx(0.8) and lon == pytest.approx(0.8)  # closing vertex counted twice


def test_resolve_bbox_string_centre():
    lat, lon, how = climate.resolve_location(None, None, "-6.7533,5.6250,-6.4333,5.9450")  # geocode_place output
    assert how == "bbox centre"
    assert lat == pytest.approx(5.785) and lon == pytest.approx(-6.5933)


def test_resolve_bad_geometry_names_the_formats():
    with pytest.raises(ValueError, match="west,south,east,north"):
        climate.resolve_location(None, None, "Soubre")


@pytest.mark.parametrize("args", [(None, None, None), (95.0, 0.0, None), (0.0, 200.0, None)])
def test_resolve_rejects_bad_input(args):
    with pytest.raises(ValueError):
        climate.resolve_location(*args)


# ─── Dates and windows ───────────────────────────────────────────────────────


def test_shift_years_leap_day():
    assert climate.shift_years(date(2024, 2, 29), -1) == date(2023, 2, 28)
    assert climate.shift_years(date(2024, 3, 1), -4) == date(2020, 3, 1)


def test_window_values_missing_day_returns_none():
    series = make_series(date(2020, 1, 1), date(2020, 1, 10), lambda d: (1, 2, 3, 4))
    series[date(2020, 1, 5)]["precipitation_sum"] = None
    assert climate.window_values(series, "precipitation_sum", date(2020, 1, 1), date(2020, 1, 4)) == [3] * 4
    assert climate.window_values(series, "precipitation_sum", date(2020, 1, 1), date(2020, 1, 10)) is None


def test_monthly_totals_only_complete_months():
    series = make_series(date(2020, 1, 1), date(2020, 3, 15), lambda d: (0, 0, 1.0, 0))
    totals = climate.monthly_totals(series, "precipitation_sum")
    assert totals == {(2020, 1): 31.0, (2020, 2): 29.0}  # March is incomplete; 2020 is a leap year


def test_accumulate_crosses_year_boundary():
    monthly = {(2019, 11): 1.0, (2019, 12): 2.0, (2020, 1): 4.0}
    assert climate.accumulate(monthly, 2020, 1, 3) == 7.0
    assert climate.accumulate(monthly, 2020, 1, 4) is None


# ─── Statistics ──────────────────────────────────────────────────────────────


def test_standardized_index_is_monotonic_and_centred():
    """Larger values give larger indices, the median maps to 0, extremes stay within ±2.6."""
    baseline = [float(v) for v in range(1, 31)]
    low, mid, high = (climate.standardized_index(v, baseline) for v in (0.0, 15.5, 31.0))
    assert low < mid < high
    assert mid == pytest.approx(0.0, abs=1e-9)
    assert -2.6 < low < -2.0 and 2.0 < high < 2.6


@pytest.mark.parametrize(
    ("index", "label", "risk"),
    [
        (2.1, "extremely wet", "low"),
        (0.0, "near normal", "low"),
        (-0.99, "near normal", "low"),
        (-1.2, "moderately dry", "medium"),
        (-1.7, "severely dry", "high"),
        (-2.3, "extremely dry", "high"),
    ],
)
def test_classify_index(index, label, risk):
    assert climate.classify_index(index) == (label, risk)


# ─── End to end on synthetic climates ────────────────────────────────────────


def wet_baseline_then_drought(day: date):
    """3 mm/day every year, except 2026 when it stops raining from April."""
    dry = day.year == 2026 and day.month >= 4
    wobble = (day.year % 7) * 0.1  # year-to-year variability so ranks are not all tied
    precip = 0.0 if dry else 3.0 + wobble
    return 25.0, 30.0, precip, 4.0


def test_drought_detected_by_spi_and_spei():
    """Three rainless months in 2026 are extremely dry for SPI-3 and high risk for SPEI-3."""
    series = make_series(date(1989, 1, 1), date(2026, 6, 30), wet_baseline_then_drought)
    precip = climate.monthly_totals(series, "precipitation_sum")
    balance = climate.water_balance(series)

    spi3 = climate.standardized_for_month(precip, 2026, 6, 3, BASELINE)
    spei3 = climate.standardized_for_month(balance, 2026, 6, 3, BASELINE)
    assert spi3["value_mm"] == 0.0
    assert spi3["class"] == "extremely dry" and spi3["risk_level"] == "high"
    assert spei3["risk_level"] == "high"
    assert spi3["period"] == "2026-04 to 2026-06"
    assert spi3["baseline_samples"] == 30

    # Before the drought started the same location looks normal.
    spi3_march = climate.standardized_for_month(precip, 2026, 3, 3, BASELINE)
    assert spi3_march["risk_level"] == "low"


def test_period_anomaly_precipitation_percent_of_normal():
    series = make_series(date(1989, 1, 1), date(2026, 12, 31), lambda d: (20, 25, 0.0 if d.year == 2026 else 2.0, 3))
    result = climate.period_anomaly(
        series, "precipitation_sum", date(2026, 3, 1), date(2026, 3, 31), BASELINE, "sum"
    )
    assert result["percent_of_normal"] == 0.0
    assert result["baseline_mean"] == 62.0


def test_period_anomaly_window_crossing_new_year():
    """A December-January window stays contiguous (22 days) in every baseline year."""
    series = make_series(date(1989, 1, 1), date(2027, 1, 31), lambda d: (10, 15, 1.0, 1))
    result = climate.period_anomaly(
        series, "precipitation_sum", date(2026, 12, 20), date(2027, 1, 10), BASELINE, "sum"
    )
    assert result["value"] == 22.0 and result["baseline_mean"] == 22.0


def test_hot_days_relative_to_local_normal():
    """A 40 °C August counts as hot against the local p90 and is >3x the 9 expected days."""

    def fn(day):
        heatwave = day.year == 2026 and day.month == 8
        tmax = 40.0 if heatwave else 30.0 + (day.toordinal() % 10) * 0.5  # spread for a real p90
        return 25.0, tmax, 1.0, 4.0

    series = make_series(date(1989, 1, 1), date(2026, 9, 30), fn)
    heat = climate.hot_days(series, date(2026, 9, 30), 90, BASELINE)
    assert heat["hot_days"] >= 31
    assert heat["risk_level"] == "high"
    assert heat["expected_hot_days"] == 9.0


def test_index_series_tracks_drought_onset():
    """The 12-month SPI-3 series is chronological and falls into extreme drought at the end."""
    series = make_series(date(1989, 1, 1), date(2026, 6, 30), wet_baseline_then_drought)
    precip = climate.monthly_totals(series, "precipitation_sum")
    points = climate.index_series(precip, 2026, 6, 12, 3, BASELINE)
    assert [p["month"] for p in points][:2] == ["2025-07", "2025-08"] and points[-1]["month"] == "2026-06"
    assert len(points) == 12
    assert points[-1]["class"] == "extremely dry"
    assert points[0]["index"] > points[-1]["index"]


def test_parse_month_accepts_month_or_date_and_names_the_format():
    assert climate.parse_month("2026-09") == (2026, 9)
    assert climate.parse_month("2026-09-01") == (2026, 9)
    with pytest.raises(ValueError, match="YYYY-MM"):
        climate.parse_month("September 2026")


def test_choose_end_month_moves_an_unfinished_month_back_with_a_note():
    assert climate.choose_end_month(None, (2026, 9)) == (2026, 9, None)
    assert climate.choose_end_month("2026-03", (2026, 9)) == (2026, 3, None)
    year, month, note = climate.choose_end_month("2026-10", (2026, 9))
    assert (year, month) == (2026, 9)
    assert "2026-09" in note
