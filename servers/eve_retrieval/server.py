"""
EVE Retrieval MCP Server
========================
An MCP server that proxies document retrieval requests to the EVE
backend API (default: https://dev.eve-chat.chat/api).

Authenticates with an EVE API key (``eve_...``), sent as the Bearer
token, and forwards queries to the ``POST /retrieve`` endpoint,
returning the raw retrieval results (documents, latencies, rewritten
query). Email/password login is gone: the EVE backend moved human
sign-in to its identity provider and machine callers use API keys.

Tools:
    retrieve: search EVE collections and return retrieved documents

Usage:
    python server.py                              # stdio transport
    python server.py --transport http --port 8000  # HTTP transport

Requirements:
    pip install "mcp[cli]>=1.2.0" httpx python-dotenv

Environment (``eve_retrieval/.env``, loaded automatically):
    EVE_API_KEY          EVE API key (eve_...), created from a signed-in
                         session with POST /users/api-keys
    EVE_API_BASE_URL     API root (default: https://dev.eve-chat.chat/api,
                         same value as .env.template)

Per-request credential header (overrides the env var when present):
    X-EVE-Token          bearer for this request only: an eve_ API key or
                         a live OIDC access token
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

_SERVER_DIR = Path(__file__).resolve().parent
_ENV_PATH = _SERVER_DIR / ".env"
load_dotenv(_ENV_PATH, override=False)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

EVE_API_KEY = os.getenv("EVE_API_KEY", "")
EVE_API_BASE_URL = os.getenv("EVE_API_BASE_URL", "https://dev.eve-chat.chat/api")

_HTTP_TIMEOUT = 120.0

mcp = FastMCP("EVE Retrieval Server", host="0.0.0.0", port=8000, stateless_http=True)

# ---------------------------------------------------------------------------
# Per-request credential resolution (header first, env fallback)
# ---------------------------------------------------------------------------


def _get_request_headers() -> dict[str, str]:
    """Return HTTP headers from the current MCP request, or {} on stdio."""
    try:
        ctx = mcp.get_context()
        request = ctx.request_context.request
        if request is not None and hasattr(request, "headers"):
            return dict(request.headers)
    except Exception:
        pass
    return {}


def _resolve_eve_token() -> str | None:
    """Bearer for this request: X-EVE-Token header first, EVE_API_KEY env else.

    An eve_ API key never expires and never needs a login round-trip, so there
    is no token cache and no re-authentication path anymore. The header slot
    also accepts a live OIDC access token for callers that already hold one.
    """
    headers = _get_request_headers()
    return headers.get("x-eve-token") or EVE_API_KEY or None


async def _authed_post(path: str, body: dict[str, Any], token: str) -> httpx.Response:
    """POST with Bearer auth. A 401 means the credential is bad: no retry."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        return await client.post(
            f"{EVE_API_BASE_URL}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def retrieve(
    query: str,
    year: list[int] | None = None,
    filters: dict | None = None,
    llm_type: str | None = None,
    embeddings_model: str = "Qwen/Qwen3-Embedding-4B",
    k: Annotated[int, Field(ge=0, le=10)] = 10,
    temperature: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0,
    score_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.6,
    max_new_tokens: int = 100000,
    public_collections: list[str] | None = None,
) -> str:
    """Search the EVE document collections for a natural-language query.

    Pass only `query`: a self-contained search phrase describing what you
    need. Every other argument (collections, k, score_threshold, filters,
    model settings) is set by the EVE application from the user's UI
    selection and any value you provide is overridden. Do not fill them in.

    Args:
        query: A self-contained natural-language search phrase.
        year: Managed by the EVE application; do not set.
        filters: Managed by the EVE application; do not set.
        llm_type: Managed by the EVE application; do not set.
        embeddings_model: Managed by the EVE application; do not set.
        k: Managed by the EVE application; do not set.
        temperature: Managed by the EVE application; do not set.
        score_threshold: Managed by the EVE application; do not set.
        max_new_tokens: Managed by the EVE application; do not set.
        public_collections: Managed by the EVE application; do not set.

    Returns:
        JSON string containing retrieved_docs, latencies, original_query,
        and requery (the rewritten query used for retrieval).
    """
    token = _resolve_eve_token()

    if not token:
        return json.dumps(
            {
                "error": (
                    "EVE credential not set. Send an eve_ API key (or a live "
                    "OIDC access token) as the X-EVE-Token header, or set the "
                    "EVE_API_KEY env var on the server."
                )
            }
        )

    body: dict[str, Any] = {
        "query": query,
        "embeddings_model": embeddings_model,
        "k": k,
        "temperature": temperature,
        "score_threshold": score_threshold,
        "max_new_tokens": max_new_tokens,
        "public_collections": public_collections,
    }
    if year is not None:
        body["year"] = year
    if filters is not None:
        body["filters"] = filters
    if llm_type is not None:
        body["llm_type"] = llm_type

    resp = await _authed_post("/retrieve", body, token)

    if resp.status_code != 200:
        return json.dumps({"error": f"EVE /retrieve returned {resp.status_code}", "detail": resp.text})

    return json.dumps(resp.json())


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="EVE Retrieval MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
        help="Transport type (default: http for AgentCore, use stdio for local MCP clients)",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport")
    args = parser.parse_args()

    mcp.settings.port = args.port

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
