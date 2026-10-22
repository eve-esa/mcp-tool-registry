# Climate Risk — MCP server

Climate anomalies and drought indices for any point on Earth, computed from the
**ERA5** reanalysis (ECMWF / Copernicus C3S) served by the **Open-Meteo**
Historical Weather API. No API key required.

Built for the "resilient supply chain" use case (climate risk of a farm) and for
hackathon thread B ("was last week unusually hot in Madrid?").

## Tools

| Tool | Question it answers | Input | Main output |
|---|---|---|---|
| `climate_anomaly` | Was this period warmer / wetter than normal? | `start_date`, `end_date` (YYYY-MM-DD, at most 366 days) + location | Mean temperature, mean daily maximum and total precipitation: value, 1991-2020 mean, anomaly, z-score, percentile, % of normal (precipitation) |
| `drought_index` | Is this farm / city in drought? | Location; optional `end_month` (YYYY-MM), `scales` (months, default `[3, 6]`) | SPI and SPEI per scale with McKee class, hot days in the last 90 days, and an overall `risk_level` (`low` / `medium` / `high`) |
| `drought_chart` | How did drought develop here? | Location; optional `label` (shown in the title), `scale` (default 3), `months` (12-120, default 36) | PNG chart (~15-20 KB): monthly SPEI bars coloured by drought class, SPI line, -1 / -1.5 / -2 thresholds; then a JSON summary (latest values, months in drought, driest month, provenance) |

**Location** (same for all tools): `latitude` + `longitude`, **or** `geometry`
(GeoJSON Point/Polygon/Feature/FeatureCollection, as an object or a string; its
centroid is used). The agent can therefore pass the same plot to this server and
to the forest server. `geometry` also takes a `"west,south,east,north"` bounding
box, the format `geocode_place` returns, so a place name can go straight from the
geocoder to these tools (the box centre is used).

Both accept `baseline_start_year` / `baseline_end_year` (default 1991-2020, the
WMO standard climate normal; at least 10 years).

Every response includes a **`provenance`** block: dataset, model (`era5`), the
ERA5 cell used, a reproducible public request URL, when the data was downloaded,
method, data latency and licence.

## Methods

- **Anomaly**: the period is compared with the same calendar window in each
  baseline year (mean for temperature, sum for precipitation). The percentile is
  the empirical rank among the 30 baseline years.
- **SPI** (*Standardized Precipitation Index*): precipitation accumulated over N
  months. **SPEI**: precipitation minus FAO-56 reference evapotranspiration, so it
  also captures heat-driven drying.
- Both are computed **empirically (non-parametric)**: the value is ranked against
  the same months in the baseline years (Gringorten plotting position) and mapped
  through the inverse standard normal. No scipy needed. With a 30-year baseline
  the index is bounded to about ±2.3, which still spans every class.
- **McKee (1993) classes**: ≥ −1 normal or wet (`low`), −1 to −1.5 moderately
  dry (`medium`), −1.5 to −2 severely dry (`high`), < −2 extremely dry (`high`).
- **Hot days**: days whose maximum temperature exceeds the local 90th percentile
  for that time of year. 9 in 90 days is normal; ≥2× is `medium`, ≥3× is `high`.
- Overall `risk_level` = the worst of SPI, SPEI and hot days.

## Files

| File | Contents |
|---|---|
| `server.py` | FastMCP, the two tools, download from Open-Meteo and provenance |
| `climate.py` | Pure calculations (no network): location, windows, SPI/SPEI, anomalies, heat |
| `era5_store.py` | On-disk cache per ERA5 cell and year |
| `charts.py` | Chart rendering (matplotlib, headless), re-encoded as a 64-colour palette PNG |
| `test_climate.py`, `test_era5_store.py`, `test_charts.py` | Offline tests (pytest), no network, no LLM |
| `test.py` | Live test over MCP/HTTP against real ERA5 data (no LLM) |
| `.env.template` | Optional `OPEN_METEO_API_KEY` |

## Images

`drought_chart` returns the PNG inline as MCP `ImageContent`, followed by a text
block with the JSON summary. The image is kept small (palette PNG, ~15-20 KB),
in line with EVE's MCP file convention (`backend/docs/mcp-file-return-convention.md`):
EVE stores it as a chat artifact in S3 and keeps the bytes out of the LLM context.

## Open-Meteo limits and caching

The free API is **for non-commercial use only** and is limited to 600
calls/minute and 10,000/day, but **requests are weighted by time span**: a
request covering more than two weeks counts as several calls. A series from 1989
to today costs about 1,000 calls, so without caching only about 10 locations fit
in a day, and one per minute.

That is why `era5_store.py` stores each **ERA5 cell (0.25°, ~28 km) and year**
on disk (`CLIMATE_RISK_CACHE_DIR`, default `/tmp/climate_risk_cache`, ~600 KB
per cell):

- Settled years (downloaded after 1 April of the following year, when ERA5T has
  been replaced by final ERA5) are never downloaded again.
- The current year (and the previous one until April) is refreshed every 6 h.
- Nearby farms share a cell, and therefore the cache.
- If the per-minute limit is hit, the server waits 61 s and retries once.
- With `OPEN_METEO_API_KEY` set it uses the paid endpoint (`customer-archive-api`).
  The key never appears in the provenance.

**Before a demo**, warm the cache by calling each location once (at least one
minute apart for new locations).

## Run and test

From `mcp-tool-registry/`:

```bash
uv venv -p 3.13 .venv
uv pip install -r servers/climate_risk/requirements.txt pytest ruff

cd servers/climate_risk
../../.venv/bin/python -m pytest -q                          # offline tests, no network
../../.venv/bin/python server.py --port 8101                 # terminal 1
../../.venv/bin/python test.py http://localhost:8101/mcp     # terminal 2, live
```

Expected output of `test.py` (values change from day to day):

```
Tools: ['climate_anomaly', 'drought_index']
Madrid 2026-09-28..2026-10-04: 19.46 °C vs normal 17.77 (anomaly +1.68, p80.0); rain 15.2 mm (203.2% of normal)
Soubré: Overall high risk. Driest signal: SPI-3 = -2.1 (extremely dry); 9 hot days in the last 90 (normal ≈ 9.0).
OK
```

Registry validation:

```bash
cd mcp-tool-registry
.venv/bin/python scripts/validate_pr.py --files servers/climate_risk/*.py servers/climate_risk/requirements.txt
.venv/bin/ruff check servers/climate_risk/
```

## Limitations

- **Resolution**: ERA5 cells are ~28 km wide; the result describes the climate of
  the area, not the microclimate of a plot.
- **Latency**: ERA5 arrives about 5-7 days late; SPI/SPEI use the last complete month.
- **Empirical SPI/SPEI**: with 30 years, values beyond about ±2.3 cannot be
  told apart; a gamma / log-logistic fit would give longer tails.
- **Absolute temperature is the cell's**: the ERA5 cell centre is requested (so it
  can be cached), so Open-Meteo does not apply its elevation correction for the
  exact point. Absolute values can differ by a few tenths (e.g. Madrid 25.0 vs
  25.2 °C); anomalies, percentiles and SPI/SPEI, being relative, barely change.
- **Simple centroid**: the mean of the vertices; enough because it is only used to
  pick the ERA5 cell.
