# Forest Compliance — MCP server

EU Deforestation Regulation (**EUDR**, Regulation (EU) 2023/1115) screening for
farm plots: **was the land forest on 31 December 2020** (the EUDR cut-off date),
**and has that forest been lost since?** Two public datasets are read in place
with HTTP range requests, so only the pixels around the plot are downloaded and
no API key is needed.

| Dataset | Role | Resolution | Source |
|---|---|---|---|
| **JRC Global Forest Cover 2020 (GFC2020) V4** | Forest / non-forest on 31 Dec 2020. The reference map of the EU Observatory on deforestation. | 10 m | Single global COG, [forobs.jrc.ec.europa.eu/GFC](https://forobs.jrc.ec.europa.eu/GFC) |
| **Hansen Global Forest Change v1.12** (`lossyear`) | Year of tree cover loss, 2001-2024. Also used by Global Forest Watch. | 30 m | 10° GeoTIFF tiles on Google Cloud Storage |

Results are a **risk screening for due diligence, not a legal determination**.

## Key dates (as of October 2026)

- Cut-off date: **31 December 2020** (unchanged by the postponements).
- Application: **30 December 2026** for large and medium operators, **30 June 2027**
  for micro and small enterprises (Regulation (EU) 2025/2650).
- Geolocation: a **point** is enough for plots up to **4 ha**; larger plots need
  a **polygon**. This server buffers points into a 4 ha circle.

Check the consolidated text on EUR-Lex before relying on these dates.

## Tools

| Tool | Input | Output |
|---|---|---|
| `eudr_check_plot` | `geometry` (GeoJSON Polygon / MultiPolygon / Point / Feature, object or string) or `latitude` + `longitude` | `risk_level` (`low` / `medium` / `high`), `verdict`, review `notes`, areas in ha, `provenance` |
| `eudr_check_plots` | `plots`: GeoJSON FeatureCollection, up to 25 features; `properties.id` or `name` is echoed back | One entry per plot (or its error) plus a `summary`: count per risk level, high-risk share, total area, total deforestation after the cut-off |
| `eudr_plot_map` | Same as `eudr_check_plot` (plots up to 2,500 ha, inside one 10° tile) | PNG map + JSON legend |
| `eudr_overview_map` | `plots`: FeatureCollection (up to 25, within 5° × 5°); optional `title` | PNG map (~15-20 KB) of all plots, each outlined and labelled in its risk colour over the 2020 forest fraction; then the batch summary, one short entry per plot and provenance |
| `supply_chain_resilience` | `plots`: FeatureCollection (up to 25); `drought`: object mapping each plot id to its drought risk (`low` / `medium` / `high`, or the full `drought_index` result of the climate_risk server) | A **decision map** (PNG, ~20 KB) with every plot coloured by supply status; then `matrix_markdown` (ready-to-show table: Farm, EUDR risk, Drought risk, Supply status, Recommendation), one row per plot, a summary per supply status and provenance |

Areas returned (`areas_ha`): `plot_ha`, `forest_2020_ha`,
`loss_before_cutoff_ha` (2001-2020, allowed), `loss_after_cutoff_ha` (any land),
`deforestation_after_cutoff_ha` (2020 forest lost in 2021+), and
`deforestation_by_year_ha`.

Images (`eudr_plot_map`, `eudr_overview_map`, `supply_chain_resilience`) are returned inline as MCP
`ImageContent` and kept small, in line with EVE's MCP file convention
(`backend/docs/mcp-file-return-convention.md`): EVE stores them as chat
artifacts and keeps the bytes out of the LLM context.

`eudr_plot_map` colours: dark green = forest in 2020; dark red = 2020 forest lost after the
cut-off; light red = tree cover lost after the cut-off on non-forest land; yellow
= loss before the cut-off; beige = not forest in 2020; black line = plot
boundary (surroundings are faded).

### Resilience matrix

`supply_chain_resilience` combines this server's EUDR screening with a drought
risk computed elsewhere (normally `drought_index` of the `climate_risk` server,
passed in by the agent). The table is built in code, not by the LLM, so small
models cannot misreport a risk level. Supply status per plot:

| EUDR risk | Drought risk | Supply status |
|---|---|---|
| high | any | 🔴 blocked |
| medium (or not assessed) | any | 🟠 review |
| low | unknown (no drought result for the plot) | 🟠 review |
| low | medium or high | 🟠 at risk |
| low | low | 🟢 secure |

`drought` is required: called without any drought data, the tool returns an error
that tells the agent to run `drought_index` for each plot first. Otherwise every
plot with a clean EUDR record would look "secure".

The recommendation joins a fixed EUDR action and a fixed drought action for the
two levels.

The decision map draws every plot over the 2020 forest background, coloured by
its supply status: red = blocked, yellow = review, orange = at risk,
blue = secure (blue rather than green, so it stands out from the forest). It
answers "which farms can we buy from?" at a glance, also for 25 plots where a
table gets long.

## Method

1. The input becomes a Polygon/MultiPolygon (points → 4 ha circle) and its
   geodesic area is computed; plots are limited to 10,000 ha.
2. For each Hansen tile the plot overlaps, the GFC2020 window is read at 10 m and
   the Hansen loss-year raster is resampled onto it (nearest neighbour; one 30 m
   Hansen pixel is exactly 3×3 GFC pixels, the grids are aligned).
3. Pixels whose centre lies inside the plot are counted.
   **Deforestation after the cut-off = GFC2020 forest AND Hansen loss year ≥ 2021.**
4. Risk level:
   - `high`: deforestation after the cut-off ≥ max(0.1 ha, 1 % of the plot).
   - `medium`: some, but below that threshold (may be 30 m edge misregistration).
   - `low`: none. A note is added when the plot still holds 2020 forest (converting
     it later would breach the EUDR), when tree cover was lost after 2020 on land
     that was not forest (e.g. plantation or agroforestry rotation), and when loss
     happened before the cut-off.

`provenance` holds a SHA-256 fingerprint of the plot geometry, dataset names,
versions, URLs and licences, the Hansen tiles read, the method, the data gap and
a disclaimer.

## Files

| File | Contents |
|---|---|
| `server.py` | FastMCP and the five tools |
| `resilience.py` | Pure logic for the EUDR × drought resilience matrix |
| `forest.py` | Pure logic (no I/O): geometry, area, Hansen tiles, tallies, risk assessment, PNG rendering |
| `sources.py` | Remote raster reads with rasterio/GDAL (`/vsicurl/`), including a low-resolution forest background from the COG overviews |
| `overview.py` | Overview map rendering (matplotlib, headless): aspect-aware figure, collision-free labels, palette PNG |
| `test_forest.py`, `test_overview.py`, `test_resilience.py` | Offline tests (pytest), no network, no LLM |
| `test.py` | Live test over MCP/HTTP against the real datasets (no LLM); writes `eudr_map.png` |
| `examples/cocoa_cooperative.geojson` | Five illustrative cocoa plots for a demo batch (one high risk) |

## Run and test

From `mcp-tool-registry/`:

```bash
uv venv -p 3.13 .venv
uv pip install -r servers/forest_compliance/requirements.txt pytest ruff

cd servers/forest_compliance
../../.venv/bin/python -m pytest -q                          # offline tests, no network
../../.venv/bin/python server.py --port 8102                 # terminal 1
../../.venv/bin/python test.py http://localhost:8102/mcp     # terminal 2, live (~10 s)
```

Expected output of `test.py`:

```
Tools: ['eudr_check_plot', 'eudr_check_plots', 'eudr_plot_map']
Soubré 493.152 ha: low — forest 2020 7.783 ha, deforested after cut-off 0.0 ha {}
Cleared plot: high — Possible deforestation after the EUDR cut-off: 2.23 ha of land mapped as forest on 31 Dec 2020 lost tree cover in 2021, 2022, 2023, 2024. ...
   tai-forest low 3.869
   madrid low 0.0
   broken None None unsupported geometry type: 'LineString' (use Point, Polygon or MultiPolygon)
Map: eudr_map.png (image/png, 1.0s)
OK
```

Each plot takes about 1-3 s. Registry validation:

```bash
cd mcp-tool-registry
.venv/bin/python scripts/validate_pr.py --files servers/forest_compliance/*.py servers/forest_compliance/requirements.txt
.venv/bin/ruff check servers/forest_compliance/
```

## Limitations

- **Hansen ends in 2024**: clearing in 2025-2026 is not detected yet. A recent
  Sentinel-2 check would close that gap.
- **Tree cover loss is not legal deforestation**: Hansen also records plantation
  harvests and natural disturbances. Combining it with the GFC2020 forest mask
  removes most of them, but a `high` result still needs human review.
- **Resolution mismatch**: 30 m loss over a 10 m forest map; field edges can
  produce small false positives, which the `medium` level absorbs.
- **No protected areas or land-tenure check** (WDPA, cadastres): legality under
  the EUDR is not assessed.
- **Coverage**: latitudes 50°S to 80°N (the Hansen extent).
