# Contributing a New MCP Server

This repository is a monorepo of [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) servers. Each subdirectory under `servers/` is an independent server that gets packaged into a Docker container and deployed to **AWS Bedrock AgentCore**.

---

## Table of Contents

1. [Repository Layout](#repository-layout)
2. [Quick Start](#quick-start)
3. [Step-by-Step Guide](#step-by-step-guide)
4. [Local Testing](#local-testing)
5. [Secrets and Environment Variables](#secrets-and-environment-variables)
6. [CI Rules and PR Checklist](#ci-rules-and-pr-checklist)

---

## Repository Layout

```
.github/workflows/
    deploy.yml          # Deploys servers to AgentCore on push to main
    pr-checks.yml       # Validates PRs before merge
scripts/
    deploy_server.py    # Build + deploy logic (used by CI)
    detect_changed.py   # Detects which servers changed in a push
    validate_pr.py      # PR validation checks
shared/
    Dockerfile          # Shared Dockerfile for all servers
servers/
    effis/              # Example: Fire Detection server
    eve_retrieval/      # Example: EVE Retrieval server
    your-server/        # <-- Your new server goes here
ruff.toml               # Linter configuration
CODEOWNERS              # Protects shared infrastructure paths
```

Each server directory contains:

| File | Required | Purpose |
|------|----------|---------|
| `server.py` | Yes | FastMCP entrypoint — the only file the container runs |
| `requirements.txt` | Yes | pip dependencies with version constraints |
| `.env.template` | No | Secret placeholders; CI substitutes them at deploy time |
| `test.py` | No (recommended) | Local test script |

You may include additional Python modules, data files, or subdirectories — everything in your server folder is copied into the Docker image.

---

## Quick Start

If you want to get going fast, copy this skeleton into `servers/my-server/server.py`:

```python
"""
My Server — MCP Server
======================
Brief description of what this server does.

Tools:
    my_tool — description of the tool

Usage:
    python server.py                              # stdio transport
    python server.py --transport http --port 8000  # HTTP transport

Requirements:
    pip install "mcp[cli]>=1.2.0" httpx python-dotenv
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

mcp = FastMCP("My Server", host="0.0.0.0", port=8000, stateless_http=True)


@mcp.tool()
async def my_tool(query: str, limit: int = 10) -> str:
    """Short description of what this tool does.

    Args:
        query: What to search for.
        limit: Maximum number of results to return.

    Returns:
        JSON string with the results.
    """
    results = {"query": query, "results": []}
    return json.dumps(results)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="My Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
```

And `servers/my-server/requirements.txt`:

```
mcp[cli]>=1.2.0
httpx>=0.27.0
python-dotenv>=1.0.0
```

Then test locally, build Docker, and open a PR. The rest of this guide explains each piece in detail.

---

## Step-by-Step Guide

### 1. Create the server directory

```bash
mkdir servers/my-server
```

### 2. Write `server.py`

Use the [Quick Start](#quick-start) skeleton or an existing server like `servers/eve_retrieval/server.py` as a starting point.

Your `server.py` must satisfy these rules (CI enforces them automatically):

- **Instantiate FastMCP** with the exact container settings:

  ```python
  mcp = FastMCP("My Server", host="0.0.0.0", port=8000, stateless_http=True)
  ```

  `host="0.0.0.0"` binds to all interfaces (required inside Docker), `port=8000` matches the shared Dockerfile's `EXPOSE 8000`, and `stateless_http=True` is required by AgentCore.

- **Define at least one tool** using the `@mcp.tool()` decorator. The tool docstring becomes the tool description that LLMs see, so write it clearly.

- **Include a `__main__` block** with both transports:

  ```python
  if __name__ == "__main__":
      import argparse
      parser = argparse.ArgumentParser()
      parser.add_argument("--transport", choices=["stdio", "http"], default="http")
      args = parser.parse_args()
      if args.transport == "stdio":
          mcp.run(transport="stdio")
      else:
          mcp.run(transport="streamable-http")
  ```

  `streamable-http` is used in production (AgentCore). `stdio` is for local testing with MCP clients like Cursor.

- **Read secrets from environment variables** (`os.environ`, `os.getenv`), never hardcode them. CI scans your code for inline passwords, API keys, AWS credentials, and tokens.

### 3. Create `requirements.txt`

List all your dependencies. Every package must have a version constraint:

```
# Good
mcp[cli]>=1.2.0
httpx>=0.27.0

# Bad — will be rejected by CI
httpx
requests
```

Local and editable installs (`-e .`, `file:///...`) are not allowed.

### 4. Create `.env.template` (if your server needs secrets)

Use `${PLACEHOLDER}` syntax for values that should come from GitHub Secrets. Literal defaults (like URLs) are fine:

```
MY_API_KEY=${MY_API_KEY}
MY_API_URL=https://api.example.com
```

Never put actual secret values in this file. See [Secrets and Environment Variables](#secrets-and-environment-variables) for how to get them added.

### 5. Write `test.py` (recommended)

Create a test script that imports your tools and exercises them locally. See `servers/eve_retrieval/test.py` for an example.

### 6. Test locally

```bash
# stdio mode (for Cursor, Claude Desktop, etc.)
cd servers/my-server
python server.py --transport stdio

# HTTP mode (for curl testing)
python server.py --transport http
# Then in another terminal:
curl http://localhost:8000/mcp
```

### 7. Test the Docker build

```bash
# From the repository root
docker build -f shared/Dockerfile servers/my-server/
```

This uses the same Dockerfile that CI uses. If it builds locally, it will build in CI.

### 8. Submit your PR

Create a branch, commit your server directory, and open a PR against `main`. The automated checks will run and report any issues.

---

## Local Testing

### stdio mode (MCP clients)

If you use Cursor or Claude Desktop, add the server to your MCP client config pointing at `server.py` with `--transport stdio`. The MCP client handles the protocol framing for you.

### HTTP mode (curl)

```bash
cd servers/my-server
python server.py --transport http

# List tools
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "tools/list", "id": 1}'

# Call a tool
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "my_tool", "arguments": {"query": "test"}}, "id": 2}'
```

### Running test.py

```bash
cd servers/my-server
python test.py
```

---

## Secrets and Environment Variables

The deploy pipeline automatically injects secrets at build time:

1. You declare what your server needs in `.env.template`
2. A maintainer adds the corresponding secrets in **GitHub repo Settings > Secrets**
3. At deploy time, CI reads `.env.template`, replaces every `${SECRET_NAME}` with the actual secret value, and writes a `.env` file into the Docker build context

**No workflow changes are needed** when adding new secrets. The pipeline uses `toJson(secrets)` to dynamically resolve all placeholders.

To request new secrets, open an issue or contact a maintainer with:
- The secret name (e.g. `MY_API_KEY`)
- Where to obtain the value
- Which server needs it

---

## CI Rules and PR Checklist

Every PR is validated automatically. Here is the full set of rules at a glance:

| Rule | What CI checks |
|------|---------------|
| One server per PR | Changed files must touch at most one `servers/<name>/` directory |
| Required files | `server.py` and `requirements.txt` must exist |
| Entrypoint contract | `FastMCP(host="0.0.0.0", port=8000, stateless_http=True)` and `mcp.run(transport="streamable-http")` |
| Version constraints | Every line in `requirements.txt` must have `>=`, `==`, etc. |
| No secrets in files | `.env` files, `*.pem`, `*.key`, `credentials.json`, `token.json` are blocked |
| No secrets in code | Source files are scanned for hardcoded passwords, API keys, AWS credentials, and tokens |
| Env template format | `.env.template` must use `${PLACEHOLDER}` syntax for secrets |
| File size limit | No file may exceed 5 MB |
| Lint | `ruff` runs on the changed server directory |
| Docker build | The image must build successfully with `shared/Dockerfile` |

Copy this checklist into your PR description:

```markdown
### Checklist

- [ ] PR modifies only one server directory under `servers/`
- [ ] `server.py` follows the entrypoint contract (FastMCP settings + streamable-http transport)
- [ ] `requirements.txt` exists and all dependencies have version constraints
- [ ] No `.env` files, credentials, or keys committed (only `.env.template`)
- [ ] No hardcoded secrets in source code (use `os.environ` / `os.getenv`)
- [ ] `.env.template` uses `${PLACEHOLDER}` syntax for secrets (if applicable)
- [ ] No files larger than 5 MB
- [ ] Local test passes (`python test.py` or manual curl test)
- [ ] Docker build succeeds (`docker build -f shared/Dockerfile servers/my-server/`)
```
