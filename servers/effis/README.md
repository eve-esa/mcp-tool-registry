# EFFIS Fire Detection MCP Server

MCP server for European wildfire analysis. It retrieves burnt area polygons from the European Forest Fire Information System (EFFIS) and computes Sentinel-2 NDVI and BAIS2 time series around fire events through the Copernicus Data Space Ecosystem.

## Output files

The tools write GeoJSON, CSV, JSON, and plots under `EFFIS_SAVE_DIR`. When that variable is unset, they use a folder named `effis` in the system temp directory. The tool result names the absolute path of each file it means to return.

The image starts the server through `eve_mcp_runtime`. That launcher reads those paths, attaches the files, and replaces each absolute path with the file name. Images become image blocks. Other files become embedded resources. It attaches at most `MCP_MAX_ATTACHMENTS` files (default 20), each at most `MCP_MAX_ATTACHMENT_BYTES` bytes (default 50 MB). A file past a cap is named in a note and is not attached. `python server.py` without the launcher returns the paths and does not attach the bytes.

EVE stores image and resource blocks as artifacts. Its defaults keep 10 files of 25 MB, so raise `ARTIFACT_MAX_PER_TOOL_CALL` and `ARTIFACT_MAX_BYTES` to keep the rest.

<!-- BEGIN GENERATED CITATION: edit CITATION.cff, then run `python scripts/citation.py servers/<name>` -->

## Authors

- **Antonio Lopez**, [@Lopez-Antonio](https://github.com/Lopez-Antonio), Pi School

## Citation

Citation metadata lives in [`CITATION.cff`](CITATION.cff). Convert it to BibTeX or APA with [cffconvert](https://github.com/citation-file-format/cffconvert): `cffconvert -i CITATION.cff -f bibtex`.

<!-- END GENERATED CITATION -->
