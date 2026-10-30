# CEDA CCI OpenSearch MCP server

This directory is a self-contained contribution candidate for the MCP tool
registry. It exposes read-only tools over the CEDA OpenSearch catalogue and
OPeNDAP data service:

- `ceda_run` — finds CCI datasets for an essential climate variable and returns
  a point or bounding-box subset as JSON records (up to 1000 rows, with a
  `truncated` flag when more exist).
- `ceda_search` — searches the whole archive with free text, place names,
  natural-language dates ("June 2020", "3 days ago"), and OpenSearch facets
  such as `project`, `sensor`, or `data_type`; returns a numbered list of
  dataset UUIDs and titles.
- `ceda_describe` — inspects a dataset through OPeNDAP: dimensions, coordinate
  ranges, variables with units, and global attributes. It scans the dataset's
  full file listing for a readable file instead of stopping at unreadable
  entries that sort first (READMEs, GeoTIFF tiles, ...). Optional
  `start_date`/`end_date` narrow the scan for tile-dominated listings whose
  readable files sit too deep for pagination to reach.
- `ceda_files` — lists a dataset's files with dates, sizes, and OPeNDAP
  availability; `opendap_only` skips straight to the OPeNDAP-readable files.
- `ceda_extract` — extracts any dataset and returns per-variable statistics
  (count, mean, standard deviation, percentiles) plus a pipe-delimited sample
  of up to 100 rows.

`ceda_search` and `ceda_extract` accept natural-language inputs and can filter
the full catalogue (`parentIdentifier` is no longer pinned to `cci`).

Result sizes are capped by default and configurable through environment
variables (set them in `.env`; a value of `0` disables the limit):

| Variable | Default | Caps |
| --- | --- | --- |
| `CEDA_MAX_RESULT_ROWS` | 1000 | Records returned by `ceda_run` (sets `truncated`) |
| `CEDA_MAX_ELEMENTS` | 100 | `ceda_search` `max_results`, `ceda_files` `limit`, `ceda_extract` `max_datasets` and total sample rows |
| `CEDA_MAX_STATS_VARIABLES` | 10 | Variables listed in `ceda_extract` statistics |
| `CEDA_MAX_FILES` | 5 | Default `max_files` for `ceda_run` and `ceda_extract` |

Per-call arguments (`max_files`, `max_datasets`, `max_results`, `limit`)
override the environment defaults and must still be at least 1 when
provided.

## Run locally

```bash
python -m pip install -r requirements.txt
copy .env.template .env
python server.py --transport stdio
```

For Streamable HTTP, run `python server.py --transport http`; the endpoint is
available at `http://localhost:8000/mcp`.

## Run with Docker

The `Dockerfile` matches the registry's `shared/Dockerfile` (Python 3.13,
port 8000, `python server.py`). From this directory:

```bash
docker build -t ceda-opensearch-mcp .
docker run -d --name ceda -p 8000:8000 ceda-opensearch-mcp
docker logs ceda
```

To override settings, copy `.env.template` to `.env` and add
`--env-file .env` to `docker run`; `.env` is excluded from the image by
`.dockerignore`. The endpoint is `http://localhost:8000/mcp`; see the next
section for a sample call. Stop it with `docker rm -f ceda`.

## Call the tool over HTTP

The server uses stateless HTTP, so a tool call can be sent directly without a
session ID:

```bash
curl -i -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-03-26" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "ceda_run",
      "arguments": {
        "ecv": "SOILMOISTURE",
        "start_date": "2020-01-01T00:00:00Z",
        "end_date": "2020-01-05T23:59:59Z",
        "latitude": 41.9,
        "longitude": 12.5,
        "max_files": 1
      }
    }
  }'
```

Choose exactly one location input: `place`, `bbox` (`west,south,east,north`),
or the `latitude` and `longitude` pair. This example contacts CEDA and
OPeNDAP.

## Test

`python test.py` runs the offline client, pipeline, and MCP tool tests.
Network-dependent CEDA, Nominatim, and OPeNDAP calls are mocked and are not
made by the test suite.

<!-- BEGIN GENERATED CITATION: edit CITATION.cff, then run `python scripts/citation.py servers/<name>` -->

## Authors

- **Gianluca Squarcia**, [@gianlucasquarcia](https://github.com/gianlucasquarcia)
- **Davide Poveromo**, [@podak](https://github.com/podak)

## Citation

Citation metadata lives in [`CITATION.cff`](CITATION.cff). Convert it to BibTeX or APA with [cffconvert](https://github.com/citation-file-format/cffconvert): `cffconvert -i CITATION.cff -f bibtex`.

<!-- END GENERATED CITATION -->
