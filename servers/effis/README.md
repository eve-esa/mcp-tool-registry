# EFFIS Fire Detection MCP Server

MCP server for European wildfire analysis. It retrieves burnt area polygons from the European Forest Fire Information System (EFFIS) and computes Sentinel-2 NDVI and BAIS2 time series around fire events through the Copernicus Data Space Ecosystem.

## Output files

By default the tools return their files inside the tool result: GeoJSON, CSV and JSON as embedded resources, plots as images. This lets a remote client store them, because it cannot read the server's disk. Each call carries at most 20 files of up to 50 MB each. EVE's backend stores at most 10 files of 25 MB by default, so raise its `ARTIFACT_MAX_PER_TOOL_CALL` and `ARTIFACT_MAX_BYTES` to keep the rest.

When the server runs on the same machine as the client, set `EFFIS_SAVE_DIR` to a folder. The tools then write every file there and return only the paths, which keeps raw bytes out of the model context:

```bash
EFFIS_SAVE_DIR=~/fire_maps python server.py --transport stdio
```

Do not set `EFFIS_SAVE_DIR` on a remote deployment: the client cannot open those paths.

<!-- BEGIN GENERATED CITATION: edit CITATION.cff, then run `python scripts/citation.py servers/<name>` -->

## Authors

- **Antonio Lopez**, [@Lopez-Antonio](https://github.com/Lopez-Antonio), Pi School

## Citation

Citation metadata lives in [`CITATION.cff`](CITATION.cff). Convert it to BibTeX or APA with [cffconvert](https://github.com/citation-file-format/cffconvert): `cffconvert -i CITATION.cff -f bibtex`.

<!-- END GENERATED CITATION -->
