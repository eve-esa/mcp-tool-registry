# OrbitCast MCP Server

OrbitCast predicts **future** Earth-observation images. For an area and time window, it says when Sentinel-1,
Sentinel-2, Landsat 8/9 (or any satellite you add) will image it, whether ESA's published plan confirms the image,
whether an optical image is likely to be cloud-free, and when the product can be downloaded. The other EO servers
(`cdse`, `effis`, ...) search images that already exist; OrbitCast answers *when will the image exist?*

**Questions it answers**

- "When will Sentinel-1 next see the Valencia floods?"
- "Will I get a clear Sentinel-2 image of this burnt area this week?"
- "When is the next acquisition with the same geometry as my reference scene?" (change detection, InSAR)
- "Which parts of Spain will Sentinel-1 image tomorrow?"

**Data sources:** open data only, no credentials needed.

| Source | Used for |
|---|---|
| [CelesTrak](https://celestrak.org) | Orbital elements (TLE), propagated with SGP4 |
| [ESA Sentinel Online](https://sentinels.copernicus.eu/copernicus/sentinel-1/acquisition-plans) | Sentinel-1/2 acquisition plans (KML) |
| [Open-Meteo](https://open-meteo.com) | Cloud forecast at the overpass time (optical sensors only) |

## Tools

| Tool | Returns |
|---|---|
| `forecast_observations` | **Main tool.** Timeline of upcoming images, with confidence, cloud forecast and download time |
| `observed_areas` | Which areas will be imaged in a window (GeoJSON file + % of a region covered per mission and day) |
| `predict_passes` | Orbit geometry only: when satellites fly over the area |
| `get_acquisition_plan` | ESA's scheduled data takes over the area (Sentinel-1 mode, polarisation) |
| `next_revisit_after` | Next acquisitions with the same geometry as a reference one |
| `export_forecast_geojson` | Footprints of the upcoming images as a styled GeoJSON file |
| `sync_acquisition_plans` | Downloads ESA's plans in advance and reports how far ahead each one goes |
| `list_eo_satellites` / `add_satellite` / `remove_satellite` | View or edit the satellite catalogue (NORAD id or TLE + sensor model) |

**Inputs:** `aoi` is `min_lon,min_lat,max_lon,max_lat`, a `lon,lat` point (10 km box) or a GeoJSON geometry. The
window is `start`/`end` (ISO UTC) or `days=N`. Past dates are rejected. Errors come back as `{"error": "..."}` so
the agent can correct its call.

## Example

Call `forecast_observations` over HTTP (server started with `python server.py`):

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"forecast_observations",
       "arguments":{"aoi":"-0.55,39.25,-0.25,39.55","days":5,"missions":["S1","S2","LANDSAT"]}}}'
```

Shortened response (illustrative values):

```json
{
  "window": ["2026-11-21T10:00:00Z", "2026-11-26T10:00:00Z"],
  "timeline": [
    {"time_utc": "2026-11-22T17:56:10Z", "satellite": "SENTINEL-1C", "confidence": "planned",
     "mode": "IW", "coverage_pct": 100.0, "expected_available_utc": "2026-11-23T17:56:10Z"},
    {"time_utc": "2026-11-23T10:51:30Z", "satellite": "SENTINEL-2B", "confidence": "likely_usable",
     "cloud_pct": 12, "coverage_pct": 100.0, "expected_available_utc": "2026-11-24T10:51:30Z"},
    {"time_utc": "2026-11-24T10:47:05Z", "satellite": "LANDSAT 9", "confidence": "cloudy",
     "cloud_pct": 78, "coverage_pct": 64.2, "expected_available_utc": "2026-11-25T10:47:05Z"},
    {"time_utc": "2026-11-25T06:02:41Z", "satellite": "SENTINEL-1D", "confidence": "geometric",
     "coverage_pct": 100.0, "expected_available_utc": "2026-11-26T06:02:41Z"}
  ],
  "next_by_mission": {"S1": {"...": "..."}, "S2": {"...": "..."}, "LANDSAT": {"none_usable_in_window": true}},
  "skipped": {"S2_no_data_take_in_esa_plan": 2, "night": 3},
  "warnings": []
}
```

| Confidence | Meaning |
|---|---|
| `planned` | ESA's plan has a data take over the area at that time (Sentinel-1/2) |
| `geometric` | The satellite can image the area, but there is no plan to confirm it: beyond ESA's plan horizon, or Landsat / added satellites |
| `likely_usable` | Optical pass with forecast cloud ≤ `max_cloud_pct` (default 30 %) |
| `cloudy` | Optical pass with forecast cloud > `max_cloud_pct` |

- Passes inside ESA's plan horizon with no data take will not produce an image: they are dropped and counted in
  `skipped`.
- Optical sensors only image in daylight on the descending pass, and only they get a cloud value; Sentinel-1 (SAR)
  is never cloud-labelled.
- `expected_available_utc` = acquisition time + mission latency (24 h by default).

## Claude configuration

Install with `pip install -r requirements.txt`, then add to the Claude Desktop (or Cursor) config:

```json
{
  "mcpServers": {
    "orbitcast": {
      "command": "python",
      "args": ["/path/to/orbitcast/server.py", "--transport", "stdio"]
    }
  }
}
```

Without `--transport stdio`, the server serves streamable HTTP on `http://localhost:8000/mcp` (`--port` to change).

## Optional environment variables

| Variable | Default | Effect |
|---|---|---|
| `ORBITCAST_CACHE` | `~/.cache/orbitcast` | Cache for TLEs, ESA plans and added satellites |
| `ORBITCAST_OUTPUT` | `./orbitcast_outputs` | Folder for GeoJSON files |
| `ORBITCAST_LATENCY_H` | 24 h per mission | Download delay, e.g. `S1=12,S2=24,LANDSAT=24` |
| `ORBITCAST_SATELLITES` | all | Restrict the catalogue, e.g. `SENTINEL-1C,SENTINEL-2B` |

Both folders fall back to the system temp dir if the default is not writable.

## Limitations

- ESA's plans reach a few days to ~2 weeks ahead; beyond that, results are `geometric` (possible, not confirmed).
- Landsat and added satellites have no published plan, so they are never `planned`.
- Cloud forecasts are only reliable a few days ahead (Open-Meteo goes up to 16 days).
- Swaths are modelled per sensor (S1 IW 343–617 km right of track, S2 ±145 km, Landsat ±92.5 km). Real footprints
  can differ by a few km.
- Needs outbound HTTPS to `celestrak.org`, `sentinels.copernicus.eu` and `api.open-meteo.com`. The first call
  downloads the plans (~1 min); later calls use the cache.

<!-- BEGIN GENERATED CITATION: edit CITATION.cff, then run `python scripts/citation.py servers/<name>` -->

## Authors

- **Cristopher Castro Traba**, [@CristopherCastro](https://github.com/CristopherCastro)

## Citation

Citation metadata lives in [`CITATION.cff`](CITATION.cff). Convert it to BibTeX or APA with [cffconvert](https://github.com/citation-file-format/cffconvert): `cffconvert -i CITATION.cff -f bibtex`.

<!-- END GENERATED CITATION -->

### BibTeX

```bibtex
@misc{castrotraba2026orbitcast,
  author       = {{Castro Traba}, Cristopher},
  title        = {{OrbitCast MCP Server}},
  year         = {2026},
  howpublished = {\url{https://github.com/eve-esa/mcp-tool-registry/tree/main/servers/orbitcast}},
  note         = {Software, Apache-2.0 license}
}
```
