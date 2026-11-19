"""Offline tests for the ERA5 disk cache (fake downloader, no network).

A fake ``fetch`` records every requested range, and "today" and the clock are
injected, so the tests can check exactly which years are downloaded, reused or
refreshed as time passes.

Run from this directory: python -m pytest test_era5_store.py -q
"""

import asyncio
from datetime import date, timedelta

import pytest
from era5_store import ERA5Store, snap_to_cell


class FakeFetch:
    """Records each requested range and returns 1 mm/day for it."""

    def __init__(self):
        self.calls: list[tuple[date, date]] = []

    async def __call__(self, lat, lon, start, end):
        self.calls.append((start, end))
        days, day = [], start
        while day <= end:
            days.append(day.isoformat())
            day += timedelta(days=1)
        n = len(days)
        daily = {
            "time": days,
            "temperature_2m_mean": [20.0] * n,
            "temperature_2m_max": [25.0] * n,
            "precipitation_sum": [1.0] * n,
            "et0_fao_evapotranspiration": [3.0] * n,
        }
        return daily, {"latitude": lat, "longitude": lon, "elevation_m": 100}


def run(coro):
    """Run a coroutine to completion (the tests themselves are synchronous)."""
    return asyncio.run(coro)


def make_store(tmp_path, fetch, today, clock):
    """Build a store with a fixed "today", a 1 h TTL and a mutable clock (``clock[0]``)."""
    return ERA5Store(tmp_path, fetch, recent_ttl_s=3600, today=lambda: today, clock=lambda: clock[0])


def test_snap_to_cell():
    assert snap_to_cell(5.79, -6.61) == (5.75, -6.5)
    assert snap_to_cell(-0.1, 0.1) == (0.0, 0.0)  # no "-0.0" folder


def test_first_call_downloads_once_then_serves_from_disk(tmp_path):
    fetch, clock = FakeFetch(), [1_000_000.0]
    store = make_store(tmp_path, fetch, date(2026, 10, 12), clock)

    series, meta = run(store.series(5.79, -6.61, 2020))
    assert fetch.calls == [(date(2020, 1, 1), date(2026, 10, 12))]  # one request for all years
    assert min(series) == date(2020, 1, 1) and max(series) == date(2026, 10, 12)
    assert meta["cache"]["years_downloaded_now"] == list(range(2020, 2027))

    # Same cell from a nearby point and a fresh process: nothing is downloaded.
    store2 = make_store(tmp_path, fetch, date(2026, 10, 12), clock)
    series2, meta2 = run(store2.series(5.80, -6.55, 2020))
    assert len(fetch.calls) == 1
    assert series2 == series
    assert meta2["cache"] == {"years_downloaded_now": [], "years_from_cache": 7}


def test_recent_years_expire_final_years_do_not(tmp_path):
    """After the TTL only the current (non-final) year is downloaded again."""
    fetch, clock = FakeFetch(), [1_000_000.0]
    store = make_store(tmp_path, fetch, date(2026, 10, 12), clock)
    run(store.series(40.4, -3.7, 2023))

    clock[0] += 7200  # past the 1 h TTL
    run(store.series(40.4, -3.7, 2023))
    # 2023-2025 were fetched after 1 April of the following year, so they are final.
    # Only the current year is refetched.
    assert fetch.calls[-1] == (date(2026, 1, 1), date(2026, 10, 12))


def test_previous_year_not_final_before_april(tmp_path):
    """Before 1 April the previous year may still be ERA5T, so it expires like the current one."""
    fetch, clock = FakeFetch(), [1_000_000.0]
    store = make_store(tmp_path, fetch, date(2026, 2, 10), clock)
    run(store.series(40.4, -3.7, 2024))
    clock[0] += 7200
    run(store.series(40.4, -3.7, 2024))
    # In February 2026, 2025 may still be preliminary (ERA5T): 2025 and 2026 are refetched together.
    assert fetch.calls[-1] == (date(2025, 1, 1), date(2026, 2, 10))


def test_gaps_are_fetched_as_separate_runs(tmp_path):
    """Only the missing years are requested, as one contiguous range, not the cached ones."""
    fetch, clock = FakeFetch(), [1_000_000.0]
    store = make_store(tmp_path, fetch, date(2026, 10, 12), clock)
    run(store.series(40.4, -3.7, 2024))  # caches 2024-2026
    run(store.series(40.4, -3.7, 2020))  # needs 2020-2023 only
    assert fetch.calls[-1] == (date(2020, 1, 1), date(2023, 12, 31))


def test_corrupt_cache_file_is_refetched(tmp_path):
    fetch, clock = FakeFetch(), [1_000_000.0]
    store = make_store(tmp_path, fetch, date(2026, 10, 12), clock)
    run(store.series(40.4, -3.7, 2025))
    next(tmp_path.rglob("2025.json")).write_text("{not json")
    run(store.series(40.4, -3.7, 2025))
    assert fetch.calls[-1] == (date(2025, 1, 1), date(2025, 12, 31))


@pytest.mark.parametrize("year", [2019, 2020])
def test_leap_years_have_all_days(tmp_path, year):
    """Splitting a multi-year response per year keeps 29 February in leap years."""
    fetch, clock = FakeFetch(), [1_000_000.0]
    store = make_store(tmp_path, fetch, date(2026, 10, 12), clock)
    series, _ = run(store.series(40.4, -3.7, year))
    days = sum(1 for d in series if d.year == year)
    assert days == (366 if year == 2020 else 365)
