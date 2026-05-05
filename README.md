# EO MCP Tools

A monorepo of MCP servers for Earth Observation (EO) workflows.

Current server examples:

- `servers/effis` - wildfire and remote sensing analysis
- `servers/eve_retrieval` - document retrieval
- `servers/serpapi` - web search 
- `servers/traille` - structured data extraction

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install only what you need:

```bash
pip install -r servers/<server-name>/requirements.txt
```

Example:

```bash
pip install -r servers/effis/requirements.txt
```

## Run any server locally

Most servers support MCP stdio and HTTP transport.

```bash
# stdio (for MCP clients)
python servers/<server-name>/server.py --transport stdio
```

## Add a new EO server

1. Create `servers/<new-server>/`
2. Add `server.py` (FastMCP entrypoint)
3. Add `requirements.txt`
4. Optionally add `.env.template` and `test.py`
5. Validate locally, then open a PR

For full contribution and deployment rules, see [CONTRIBUTING GUIDE](CONTRIBUTING.md).

