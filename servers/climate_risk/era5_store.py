"""
On-disk cache of daily ERA5 data, one file per ERA5 grid cell and year.

Responsibility: hand the tools a complete daily ``Series`` for a location
while downloading as little as possible.

How it fits:
    server.py     creates one ``ERA5Store`` and injects the HTTP download.
    climate.py    defines ``VARIABLES`` / ``Series`` and consumes the result.
    charts.py     is not involved.

Why a cache: the free Open-Meteo API weights a request by its time span (a
request longer than two weeks counts as several calls), so a 35-year baseline
costs about 1,000 calls of a 10,000/day budget. Past ERA5 years never change
once final, so each cell is downloaded once and afterwards only the recent
years are refreshed.

Layout: ``<cache_dir>/<lat>_<lon>/<year>.json`` where ``<lat>_<lon>`` is the
centre of the 0.25° ERA5 cell, so nearby points share files.

The download itself is injected (``fetch``), as are the clock and "today",
which keeps this module free of network code and testable offline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from climate import VARIABLES, Series

CELL_DEG = 0.25  # ERA5 grid spacing in degrees (~28 km at the equator)
# ERA5T (preliminary) data is replaced by final ERA5 within about 2-3 months,
# so a year is only cached for good once fetched on or after 1 April of the
# next year (month number of that cut-off). Earlier copies expire after the TTL.
FINAL_AFTER_MONTH = 4

# fetch(lat, lon, start, end) -> (daily, grid_cell); daily is Open-Meteo's
# {"time": [...], "<variable>": [...]} block and grid_cell describes the cell.
Fetcher = Callable[[float, float, date, date], Awaitable[tuple[dict[str, list], dict[str, Any]]]]


def snap_to_cell(lat: float, lon: float) -> tuple[float, float]:
    """Return the centre of the ERA5 0.25° cell containing the point.

    All points in the same cell get the same ERA5 values, so snapping before
    caching lets nearby farms reuse each other's downloads.
    """
    # "+ 0.0" turns -0.0 into 0.0 so both map to the same cache folder.
    return round(lat / CELL_DEG) * CELL_DEG + 0.0, round(lon / CELL_DEG) * CELL_DEG + 0.0


class ERA5Store:
    """Per-cell, per-year JSON cache in front of an ERA5 downloader.

    A cached year is reused if it is *final* (fetched after ERA5T was
    replaced, see ``FINAL_AFTER_MONTH``) or still *fresh* (younger than
    ``recent_ttl_s``). Anything else is downloaded again, grouping
    consecutive missing years into one request.
    """

    def __init__(
        self,
        cache_dir: Path,
        fetch: Fetcher,
        recent_ttl_s: float = 6 * 3600,
        today: Callable[[], date] = date.today,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a store; nothing is read or downloaded until ``series`` is called.

        Args:
            cache_dir: Root folder of the cache (created on first save).
            fetch: Async downloader, see ``Fetcher``.
            recent_ttl_s: How long a non-final year stays valid. ERA5T gains
                about one new day per day, so refreshing the recent years a
                few times a day (default 6 h) keeps them current without
                spending the API budget on every call.
            today: Returns the current date (the server passes UTC "today").
            clock: Returns the current epoch time in seconds, for the TTL.
        """
        self.cache_dir = cache_dir
        self.fetch = fetch
        self.recent_ttl_s = recent_ttl_s
        self.today = today
        self.clock = clock

    def _path(self, cell: tuple[float, float], year: int) -> Path:
        """Return the cache file of one cell and year, e.g. ``+40.50_-003.75/2024.json``.

        Signed, zero-padded coordinates keep folder names fixed-width and
        unambiguous.
        """
        return self.cache_dir / f"{cell[0]:+07.2f}_{cell[1]:+08.2f}" / f"{year}.json"

    def _load(self, cell: tuple[float, float], year: int) -> dict | None:
        """Return the cached entry for a cell and year, or None if it must be downloaded.

        None covers: no file, an unreadable/corrupt file (treated as a cache
        miss so it is simply overwritten), or a non-final year older than
        the TTL.
        """
        path = self._path(cell, year)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        # Final = downloaded after ERA5T for that year was replaced by ERA5.
        final = date.fromisoformat(entry["fetched_on"]) >= date(year + 1, FINAL_AFTER_MONTH, 1)
        if final or self.clock() - entry["fetched_at"] < self.recent_ttl_s:
            return entry
        return None

    def _save(self, cell: tuple[float, float], year: int, daily: dict[str, list], grid_cell: dict) -> dict:
        """Write one year to disk and return the stored entry.

        ``fetched_at`` (epoch seconds) drives the TTL; ``fetched_on`` (date)
        decides finality. The file is written to ``.tmp`` and renamed, which
        is atomic, so a crash or a concurrent reader never sees half a file.
        """
        entry = {
            "fetched_at": self.clock(),
            "fetched_on": self.today().isoformat(),
            "grid_cell": grid_cell,
            "daily": daily,
        }
        path = self._path(cell, year)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry))
        tmp.replace(path)
        return entry

    async def series(self, lat: float, lon: float, first_year: int) -> tuple[Series, dict[str, Any]]:
        """Return the daily series from 1 January of first_year to today for the point's cell.

        Returns:
            ``(series, meta)``: the daily ``Series`` (recent days may be None
            because of ERA5 latency) and provenance metadata: the grid cell
            reported by Open-Meteo, which years were downloaded now versus
            served from cache, and the UTC time range in which the data was
            retrieved.
        """
        cell = snap_to_cell(lat, lon)
        today = self.today()
        years = list(range(first_year, today.year + 1))
        entries: dict[int, dict] = {}
        missing: list[int] = []
        for year in years:
            entry = self._load(cell, year)
            if entry is None:
                missing.append(year)
            else:
                entries[year] = entry

        # One request per run of consecutive missing years.
        runs: list[list[int]] = []
        for year in missing:
            if runs and runs[-1][-1] == year - 1:
                runs[-1].append(year)
            else:
                runs.append([year])
        for run in runs:
            start = date(run[0], 1, 1)
            end = min(date(run[-1], 12, 31), today)
            daily, grid_cell = await self.fetch(cell[0], cell[1], start, end)
            # Split the multi-year response back into one bucket per cache file.
            per_year: dict[int, dict[str, list]] = {y: {"time": [], **{v: [] for v in VARIABLES}} for y in run}
            for i, day in enumerate(daily["time"]):
                bucket = per_year.get(int(day[:4]))
                if bucket is None:
                    continue
                bucket["time"].append(day)
                for var in VARIABLES:
                    bucket[var].append(daily[var][i])
            for year in run:
                entries[year] = self._save(cell, year, per_year[year], grid_cell)

        # Merge all years (cached and fresh) into one date-keyed series.
        series: Series = {}
        for year in years:
            daily = entries[year]["daily"]
            for i, day in enumerate(daily["time"]):
                series[date.fromisoformat(day)] = {var: daily[var][i] for var in VARIABLES}

        fetched = [entries[y]["fetched_at"] for y in years]
        meta = {
            # Same cell for every year; take it from the most recent entry.
            "grid_cell": entries[years[-1]]["grid_cell"],
            "cache": {
                "years_downloaded_now": missing,
                "years_from_cache": len(years) - len(missing),
            },
            "data_retrieved_between": [
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(fetched))),
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(fetched))),
            ],
        }
        return series, meta
