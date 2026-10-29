# EFFIS Fire Detection MCP Server

MCP server for European wildfire analysis. It retrieves burnt area polygons from the European Forest Fire Information System (EFFIS) and computes Sentinel-2 NDVI and BAIS2 time series around fire events through the Copernicus Data Space Ecosystem.

## Perimeter handoff and mapped exposure

The existing geocode, EFFIS lookup and spectral/recovery tools remain available.
For a stateless handoff, request one lookup feature with `include_perimeter=True`
and `max_features=1`, then pass its `perimeter` Feature to `get_fire_exposure`.
Use `buffer_km=0` when a tight bbox is needed; even that bbox is not the polygon.
Inline lookup/exposure needs no prior server-local file or exported map.

Inputs are bounded WGS84 Polygon/MultiPolygon Features (256000 bytes and 10000
vertices). Exposure supports bboxes below 6 degrees width, latitude -80..84,
and at most 2500 km². Results count current OSM building ways, clipped road km
and named place points; they do not measure damage, population or historical
asset completeness. Relation-only/unmapped and enclosing OSM ways may be absent.
OSM timestamps and attribution are returned; supplied perimeter provenance is
not independently authenticated.

Legacy GeoJSON path/index inputs and optional diagnostic exports are confined
to `DEFAULT_SAVE_DIR` (default `/tmp/fire_maps`), including producer overrides.
Cache overrides must stay under operator-configured `EFFIS_SHAPEFILE_DIR`.
Paths are not download URLs or persistence promises. Export failure is a warning,
not zero exposure. No asset hosting infrastructure or hosted validation is implied.

Run `python test_contract.py` for deterministic real stdio/HTTP client checks
with provider fixtures; `python test.py --unit-tests` retains inherited checks.
Live checks are separate and must not report unavailable/gated cases as passed.

## Weather and concentration context

`get_fire_weather` returns UTC ERA5 event-hour weather (1–31 past event days)
and antecedent rain (1–90 days), with units, counts and unavailable values.
This is regional reanalysis, not local fuel moisture or a spread forecast.

`get_community_air_quality` retains the seven-day concentration baseline and
optional fire-to-community wind context. `get_smoke_signal` is a compatible
bbox-centre adapter over the same CAMS Europe calculation: CO, PM2.5 and PM10,
a fourteen-day default baseline, configurable 3–60 baseline days and 1–30 days
after the event (inclusive endpoints). It retains peak ratios; the community
interface retains mean ratios. Missing optional wind does not erase valid AQ.
Requested concentrations require complete expected hours/units; missing is not
zero, and a zero baseline yields an undefined (null) ratio.

CAMS Europe historical data begins in 2013 and availability is provider-dependent;
no worldwide support is promised. These are modeled concentrations, not sensors,
estimated emissions, health thresholds or proof that a particular fire caused
smoke. Recent periods can be unavailable. Responses preserve model attribution.
Open-Meteo's [free API terms](https://open-meteo.com/en/terms) restrict service
use; open data licensing does not establish free hosted/commercial eligibility.
Deployment maintainers must resolve the appropriate service plan/access.

<!-- BEGIN GENERATED CITATION: edit CITATION.cff, then run `python scripts/citation.py servers/<name>` -->

## Authors

- **Antonio Lopez**, [@Lopez-Antonio](https://github.com/Lopez-Antonio), Pi School

## Citation

Citation metadata lives in [`CITATION.cff`](CITATION.cff). Convert it to BibTeX or APA with [cffconvert](https://github.com/citation-file-format/cffconvert): `cffconvert -i CITATION.cff -f bibtex`.

<!-- END GENERATED CITATION -->
