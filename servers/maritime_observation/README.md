# Maritime observation MCP

Historical vessel activity from [Global Fishing Watch](https://globalfishingwatch.org/) and the [AISDataPortal archive](https://huggingface.co/datasets/AISDataPortal/DB), with Sentinel-2 imagery for context.

| Tool | What it does |
| --- | --- |
| `summarize_presence` | Summarizes recorded AIS activity and vessel types. |
| `compare_presence` | Compares two equal-duration periods in the same area. |
| `summarize_sar` | Returns GFW radar detections matched or unmatched with AIS. |
| `get_ais_positions` | Returns the latest recorded position per MMSI in a time window. |
| `prepare_scene_context` | Overlays nearby AIS reports on an exact Sentinel-2 scene. |

## Run

Python 3.12–3.13. Set `GFW_API_TOKEN` in the environment or a local `.env`; the public AIS and scene tools do not need it.

```bash
pip install -r requirements.txt
python server.py                 # HTTP: http://localhost:8000/mcp
python server.py --transport stdio
```

The server-local `Dockerfile` installs `libexpat1` for satellite imagery support. The registry deployment script selects this Dockerfile automatically; no shared Dockerfile change is needed. Its PR build check currently still uses the shared Dockerfile, so also test the custom image at runtime and on `linux/arm64`, the deployment architecture.

```bash
docker build --platform linux/arm64 -t maritime-local -f Dockerfile .
docker run --rm --platform linux/arm64 maritime-local python test.py
```

Coordinate the deployed `GFW_API_TOKEN` account with the EVE maintainers before enabling the GFW tools. Do not hand over a personal token: GFW documents noncommercial use, attribution requirements and restrictions on token sharing. See [data sources and deployment notes](DATA_SOURCES.md).

Optional settings: `MARITIME_CACHE_DIR`, `MARITIME_OUTPUT_DIR`, and `AIS_CACHE_MAX_MB` (default 1024 MB). The container must have writable cache/export directories. Outputs are local and have no automatic retention cleanup; MCP resource reads must reach the instance that generated them. Validate this behavior through EVE/AgentCore before wider deployment.

## Inputs and results

Areas use `[west, south, east, north]`. Dates use ISO format; end boundaries are exclusive. GFW windows are limited to 31 days, detailed AIS to 24 hours. Availability depends on provider coverage.

Results include sources, observation dates, and MCP resource links for generated charts and exports. The AIS archive provides positions for dates with readable files. Its historical coverage varies and a cold query can download around 260 MB per day. AIS positions are reported observations. Unmatched radar detections indicate that Global Fishing Watch found no AIS match for those detections.

<!-- BEGIN GENERATED CITATION: edit CITATION.cff, then run `python scripts/citation.py servers/<name>` -->

## Authors

- **Francesco Piccolo**, [@francescoopiccolo](https://github.com/francescoopiccolo)
- **Bashudev Neupane**, [@Vashudeva](https://github.com/Vashudeva)

## Citation

Citation metadata lives in [`CITATION.cff`](CITATION.cff). Convert it to BibTeX or APA with [cffconvert](https://github.com/citation-file-format/cffconvert): `cffconvert -i CITATION.cff -f bibtex`.

<!-- END GENERATED CITATION -->
