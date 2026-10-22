# satellite_passes

When will a free Earth-observation satellite next image an area, how much of the area will each image cover, and how cloudy will it be?

ESA publishes the exact schedule of every Sentinel acquisition weeks ahead, and USGS publishes the Landsat calendar, but only as Google Earth (KML) files and a web tool. An agent cannot read them. This server turns them into one tool call.

## Tool

`find_next_satellite_passes(bbox, days_ahead=None, satellites="sentinel-2,sentinel-1,landsat", min_coverage_pct=30)`

| Argument | Meaning |
|---|---|
| `bbox` | Area as `west,south,east,north` (the bbox returned by `geocode_place`) |
| `days_ahead` | 1–21. Empty: everything the published schedules contain |
| `satellites` | Any of `sentinel-2`, `sentinel-1`, `landsat` |
| `min_coverage_pct` | Hide passes covering less of the area than this (default 30) |

Each pass has the time over the area (UTC), satellite, `optical` or `radar`, the share of the area covered, and, for optical passes in the next 7 days, the cloud forecast. `schedules` says how far each satellite's published plan reaches, and `warnings` explains gaps (for example a plan file ESA has not published correctly). `sources` lists the link of every service used, so the agent cites real pages.

## Sources (free, no keys)

| Satellites | Source |
|---|---|
| Sentinel-2A/2B/2C (optical) | [ESA acquisition plans](https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-2/acquisition-plans) |
| Sentinel-1C/1D (radar) | [ESA acquisition plans](https://sentinels.copernicus.eu/web/sentinel/copernicus/sentinel-1/acquisition-plans) |
| Landsat 8/9 (optical) | [USGS acquisition calendar](https://landsat.usgs.gov/landsat_acq) (16-day cycle, carried past its last published year) and WRS-2 footprints |
| Cloud forecast | [Open-Meteo](https://open-meteo.com/) |

## How it works

1. Reads the newest plan file per satellite from ESA's pages. If ESA's newest file is broken, it falls back to the previous one and says so.
2. Keeps the datatakes whose footprint overlaps the area and estimates the covered share on a 20 × 20 grid.
3. Interpolates the time over the area along each datatake (a datatake can last 20 minutes from the Arctic to Africa).
4. Steps the USGS Landsat calendar forward in 16-day cycles for the WRS-2 paths that cover the area.
5. Adds the Open-Meteo cloud cover at that hour, averaged over five points of the area.

Downloads are cached in memory for three hours. Nothing is fetched at start-up.

## Limits

- Schedules are plans and can change. ESA publishes about one to three weeks ahead.
- Landsat times of day are not published; Landsat passes have a date only (and no cloud forecast).
- Coverage is the share of the bounding box, not of the exact outline of a region.
- Cloud forecasts cover the next 7 days; beyond about 3 days they are uncertain.
- Areas larger than 20 × 20 degrees, or crossing the antimeridian, are not supported.

## Run and test

```bash
pip install -r requirements.txt
python server.py                    # HTTP on port 8000
python server.py --transport stdio  # for MCP clients
python test.py                      # unit, error and live tests
```
