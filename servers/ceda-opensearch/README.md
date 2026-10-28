# CEDA CCI OpenSearch MCP server

This directory is a self-contained contribution candidate for the MCP tool
registry. It exposes `ceda_run`, which finds CCI datasets, selects OPeNDAP
files by date, and returns a point or bounding-box subset as JSON records.

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

## Author
- Gianluca Squarcia ([@github](https://github.com/gianlucasquarcia))
- Davide Poveromo