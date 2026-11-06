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

## Fuel, monthly fallback and emissions screening

`get_burn_fuel_types` uses WorldCover 2020 v100 for events through 2021 and 2021
v200 thereafter (2021 when the date is unknown). A 2020 map is **not** pre-fire
for a 2020-or-earlier event. Its proportions describe the whole bbox, not burnt
pixels; use an EFFIS bbox with zero buffer and keep this proxy limitation visible.
WorldCover attribution and dataset citation are returned with the year/tile IDs.

`detect_burned_area` is an optional MCD64A1 v061 monthly **bbox aggregate** of
recorded pixels, possibly multiple fires. Its earliest/latest/peak days are not
individual event dates; it returns no polygon and cannot feed polygon exposure.
No detected pixels differs from unavailable data and is not proof no fire occurred.
Both raster tools reject paginated/truncated searches, missing/overlapping tile
envelopes, more than eight selected tiles and less than 99.99% valid statistics.
Numeric validity alone does not prove full-month mapping. MODIS also checks the
supported `QA`, `First_Day` and `Last_Day` assets: observed land/valid QA, no
shortened-period flag, and reliable intervals spanning the requested month.
Known incomplete mapping returns `temporal_coverage_incomplete`, never a complete
zero-month result. Coverage metadata describes sampled categorical statistics,
not full-resolution pixelwise QA or an uncertainty model. Zero-count categories
do not create burn dates. Narrow the bbox when coverage is unsupported.
Categorical downsampling and spherical clipped-bbox area weighting are approximate,
not validated accuracy.
No worldwide availability or complete small-fire/QA guarantee is established.

**Live provider checks:** the documented PC GeoJSON statistics route previously
returned HTTP 400 for the published MODIS asset, but recovered on recheck. Real
MCP requests for inland Evia samples in August 2021 returned monthly aggregates
with one tile and recorded burn days; WorldCover also succeeded. These checks
establish supported access for those samples, not universal coverage or accuracy.
A broader coastal bbox remained unavailable under the conservative valid-data
and categorical guards. Missing/water-category or incomplete statistics may be
rejected rather than converted to zero. Provider failures return structured
unavailable results and safe HTTP status metadata. No alternate stack, access
bypass, new credentials or individual perimeter is introduced.

`estimate_fire_emissions` accepts caller area/bbox/date and explicit fuel shares,
or the WorldCover proxy. Area is not authenticated as an EFFIS event. All numeric
inputs must be finite/nonnegative (area positive); area may not exceed spherical
bbox area by more than the documented 5% envelope tolerance. Unsupported/empty
burnable shares are unavailable, not zero emissions. The retained gas, CO2e,
car-year and by-fuel outputs carry units and explicit assumptions.

**screening-v1 factor basis:**

- [IPCC 2006 Volume 4, Eq. 2.27 and Tables 2.4/2.5](https://www.ipcc-nggip.iges.or.jp/public/2006gl/pdf/4_Volume4/V4_02_Ch2_Generic.pdf):
  consumed dry matter t/ha already includes biomass times combustion completeness.
  Central forest scenarios use primary tropical 83.9 (slash/burn transferred to
  wildfire), other-temperate wildfire 19.8 and boreal-general wildfire 52.8.
  Other representative centres are all-shrubland 14.3, grassland 4.1 and rice
  residue 5.5. Low/high use alternative named table rows, not table SE/SD or a
  statistical interval. Every used row/transfer and coefficient is returned.
- Wetland/moss-lichen use an explicit grassland proxy; mangroves use tropical
  forest. These are not measured class-specific fuel loads; peat/soil combustion
  is excluded. Absolute latitude selects forest class heuristically, not a mapped
  ecozone. These choices replace several unsourced prototype numeric defaults.
- Gas means: IPCC Table 2.5, g/kg dry matter. PM2.5 means:
  [Andreae & Merlet (2001), Table 1](https://doi.org/10.1029/2000GB001382).
- GWP100 is explicitly versioned to [AR6 WGI 2021 Table 7.15](https://www.ipcc.ch/report/ar6/wg1/downloads/report/IPCC_AR6_WGI_Chapter07.pdf):
  non-fossil CH4 27 and N2O 273. The vehicle comparison uses
  [EPA's typical 4.6 t tailpipe CO2/year](https://www.epa.gov/greenvehicles/greenhouse-gas-emissions-typical-passenger-vehicle).

Low/central/high vary fuel-consumption assumptions only: **sensitivity, not
confidence intervals, measured emissions or a claimed accuracy percentage**.
They do not propagate all land-cover, area, emission-factor or model uncertainty.
Gross CO2/CO2e release is not net climate impact, an inventory total, a carbon
offset or guaranteed regrowth. Car-years are illustrative tailpipe-CO2 comparisons,
not life-cycle equivalence. No causal smoke, damage or population inference follows.

## Attribution and readiness limits

Existing Antonio Lopez credit is retained. Additional verified contributors are
Alessandro D'Angelo (`a-dangelo`) and Sameer Ahmed (`Sameer-Ahmed7`), both with
Apache-2.0 consent. Added metadata entries are alphabetically serialized for
identification only; citation priority and fine-grained roles have not been
agreed and are not inferred from notebook names or commit counts. No ORCID or
additional affiliation is invented. The human review before publication must
resolve any required final citation-order preference.

Local checks and a shared Linux image build are not AgentCore certification or
permission to publish. EVE deployment operators remain responsible for service
plan/commercial eligibility and credentials; upstream maintainers determine
acceptance of expanded remit, request limits and hosted behavior. No such
acceptance or free-API entitlement is asserted by the local contribution.

<!-- BEGIN GENERATED CITATION: edit CITATION.cff, then run `python scripts/citation.py servers/<name>` -->

## Authors

- **Antonio Lopez**, [@Lopez-Antonio](https://github.com/Lopez-Antonio), Pi School
- **Alessandro D'Angelo**, [@a-dangelo](https://github.com/a-dangelo)
- **Sameer Ahmed**, [@Sameer-Ahmed7](https://github.com/Sameer-Ahmed7)

## Citation

Citation metadata lives in [`CITATION.cff`](CITATION.cff). Convert it to BibTeX or APA with [cffconvert](https://github.com/citation-file-format/cffconvert): `cffconvert -i CITATION.cff -f bibtex`.

<!-- END GENERATED CITATION -->
