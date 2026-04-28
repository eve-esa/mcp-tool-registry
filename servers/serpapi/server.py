"""
SerpAPI MCP Server
=================
An MCP server that proxies search requests to Google via SerpAPI.

Tools:
    search Google — search Google and return organic results with titles, URLs, and snippets

Auth:
    Requires X-API-Key header with SerpAPI API key.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("serpapi-mcp")

SERPAPI_KEY = os.getenv("SERPAPI_API_KEY", "")
SERPAPI_URL = "https://serpapi.com/search"
SERP_HTTP_TIMEOUT = 60.0


def get_api_key() -> str:
    try:
        request = get_http_request()
        headers = dict(request.headers)
        api_key = headers.get("X-API-Key") or headers.get("authorization", "")
        if api_key.startswith("Bearer "):
            api_key = api_key[7:]
        return api_key if api_key else None
    except Exception:
        return None


mcp = FastMCP("SerpAPI")


@mcp.tool()
async def search_google(
    query: str,
    num: int = 10,
) -> str:
    """Search Google via SerpAPI and return organic results.

    Args:
        query: The search query string.
        num: Maximum number of results to return (default 10).

    Returns:
        JSON string containing a list of results with title, url, and snippet.
    """
    api_key = get_api_key()
    if not api_key:
        api_key = SERPAPI_KEY

    if not api_key:
        return json.dumps({
            "error": (
                "No API key available. Provide X-API-Key header or "
                "set SERPAPI_API_KEY in server .env file."
            )
        })

    params: dict[str, Any] = {
        "q": query,
        "api_key": api_key,
        "engine": "google",
        "num": min(num, 20),
    }

    try:
        async with httpx.AsyncClient(timeout=SERP_HTTP_TIMEOUT) as client:
            resp = await client.get(SERPAPI_URL, params=params)
            resp.raise_for_status()
    except Exception as e:
        logger.warning("SerpAPI request failed: %s", e)
        return json.dumps({"error": f"Request failed: {e}"})

    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in resp.json().get("organic_results", [])
        if item.get("link")
    ]

    return json.dumps({"results": results[:num]})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SerpAPI MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="Transport type (default: streamable-http)",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host for HTTP transport")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio", host=args.host, port=args.port)
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
