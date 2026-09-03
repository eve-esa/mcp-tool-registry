#!/usr/bin/env python3
"""
Integration test for the EVE Retrieval MCP Server.

Starts the server locally on HTTP, connects with an MCP client, and
exercises every tool through the protocol — exactly as a real caller would.

The EVE API key comes from .env / env vars by default. Override it from
the command line to test the per-request header proxy flow.

Usage:
    # Default (EVE_API_KEY from .env / env vars):
    python test.py

    # Override token:
    python test.py --eve-token eve_...

    # Custom query:
    python test.py --query "What is Copernicus?" --k 5

    # Specify port (default: 9100):
    python test.py --port 9200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

SERVER_DIR = Path(__file__).resolve().parent
SERVER_PY = SERVER_DIR / "server.py"
DEFAULT_PORT = 9100
STARTUP_TIMEOUT = 15
TOOL_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _print_result(result) -> dict | None:
    """Pretty-print a tool result and return the parsed JSON."""
    try:
        text = result.content[0].text
        parsed = json.loads(text)
        print(json.dumps(parsed, indent=2, default=str)[:3000])
        if len(json.dumps(parsed)) > 3000:
            print("  ... (truncated)")
        return parsed
    except (json.JSONDecodeError, IndexError, AttributeError):
        print(result.content[0].text[:3000])
        return None


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def _start_server(port: int, env_overrides: dict[str, str]) -> subprocess.Popen:
    """Launch server.py as a subprocess on the given port."""
    env = {**os.environ, **env_overrides}
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PY), "--transport", "http", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


async def _wait_for_server(url: str, timeout: float = STARTUP_TIMEOUT) -> None:
    """Poll the server until it accepts connections."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code < 500:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s")


def _stop_server(proc: subprocess.Popen) -> None:
    """Gracefully stop the server subprocess."""
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


async def test_list_tools(session: ClientSession) -> list[str]:
    """List available tools and return their names."""
    _section("LIST TOOLS")
    tools_result = await session.list_tools()
    names = []
    for t in tools_result.tools:
        print(f"  - {t.name}: {t.description[:80]}")
        names.append(t.name)
    print(f"\n  Total: {len(names)} tools")
    assert "retrieve" in names, "Expected 'retrieve' tool not found"
    print("  [PASS] retrieve tool registered")
    return names


async def test_retrieve(
    session: ClientSession,
    query: str,
    k: int,
    score_threshold: float,
    collections: list[str] | None,
    private_collection: str | None,
) -> dict:
    """Call the retrieve tool and validate the response shape."""
    _section(
        "TEST: retrieve("
        f"query={query!r}, k={k}, score_threshold={score_threshold}, "
        f"collections={collections}, private_collection={private_collection})"
    )

    args: dict = {"query": query, "k": k, "score_threshold": score_threshold}
    if collections:
        args["public_collections"] = collections
    if private_collection:
        args["private_collection"] = private_collection

    print(args)
    
    t0 = time.time()
    result = await session.call_tool("retrieve", args)
    elapsed = time.time() - t0

    parsed = _print_result(result)
    assert parsed is not None, "Failed to parse tool response as JSON"

    if "error" in parsed:
        print(f"\n  [FAIL] retrieve returned error: {parsed['error'][:200]}")
        return parsed

    print(f"\n  Elapsed: {elapsed:.1f}s")
    print(f"  original_query: {parsed.get('original_query', '?')}")
    print(f"  requery:        {parsed.get('requery', '?')}")
    docs = parsed.get("retrieved_docs", [])
    print(f"  documents:      {len(docs)}")

    if docs:
        for i, doc in enumerate(docs[:3]):
            score = doc.get("score")
            coll = doc.get("collection_name", "?")
            text = doc.get("text", "")[:120].replace("\n", " ")
            score_str = f"score={score:.4f}" if score is not None else ""
            print(f"    [{i+1}] {coll} | {score_str}")
            if text:
                print(f"         {text}...")

    print("  [PASS] retrieve returned valid response")
    return parsed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_tests(port: int, query: str, k: int, score_threshold: float,
                    collections: list[str] | None, private_collection: str | None,
                    headers: dict[str, str]) -> bool:
    """Connect to the local server and run all tests."""
    mcp_url = f"http://localhost:{port}/mcp"

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(TOOL_TIMEOUT)) as http:
        async with streamable_http_client(mcp_url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                await test_list_tools(session)
                await test_retrieve(session, query, k, score_threshold, collections, private_collection)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Integration test for the EVE Retrieval MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to run the test server on (default: {DEFAULT_PORT})")
    parser.add_argument("--query", "-q", default="How is TROPOMI used to support policy making?",
                        help="Search query (default: 'How is TROPOMI used to support policy making?')")
    parser.add_argument("--k", "-k", type=int, default=10,
                        help="Number of documents to retrieve (default: 3)")
    parser.add_argument("--score-threshold", "-s", type=float, default=0.2,
                        help="Minimum similarity score (default: 0.7)")
    parser.add_argument("--collections", "-c", nargs="+", default=["Wiley AI Gateway", "Wikipedia EO", "qwen-512-filtered", "wikipedia-512"],
                        help="Public collection names (default: server defaults)")
    parser.add_argument("--private-collection", default=None,
                        help="Private collection name")

    cred_group = parser.add_argument_group(
        "credentials",
        "Override EVE credentials (otherwise loaded from .env / env vars)",
    )
    cred_group.add_argument("--eve-token", default=None,
                            help="EVE API key or pre-obtained access token")

    args = parser.parse_args()

    # Build env overrides for the server subprocess
    env_overrides: dict[str, str] = {}

    # Build custom headers for the MCP client (per-request credential proxy)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if args.eve_token:
        headers["X-EVE-Token"] = args.eve_token

    # --- Start server, run tests, stop server ---
    _section("Starting EVE Retrieval MCP Server")
    print(f"  port:    {args.port}")
    print(f"  server:  {SERVER_PY}")
    if env_overrides:
        print(f"  env:     {', '.join(env_overrides.keys())}")
    if any(k.startswith("X-") for k in headers):
        print(f"  headers: {', '.join(k for k in headers if k.startswith('X-'))}")

    proc = _start_server(args.port, env_overrides)
    try:
        asyncio.run(_wait_for_server(f"http://localhost:{args.port}/mcp"))
        print("  Server is up.\n")

        ok = asyncio.run(run_tests(
            port=args.port,
            query=args.query,
            k=args.k,
            score_threshold=args.score_threshold,
            collections=args.collections,
            private_collection=args.private_collection,
            headers=headers,
        ))

        _section("DONE — all tests passed" if ok else "DONE — some tests failed")

    except TimeoutError:
        print(f"\n  [FAIL] Server did not start within {STARTUP_TIMEOUT}s")
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        if stderr:
            print(f"\n  Server stderr:\n{stderr[:2000]}")
        sys.exit(1)

    except Exception as exc:
        print(f"\n  [FAIL] {exc}")
        sys.exit(1)

    finally:
        _stop_server(proc)
        print("  Server stopped.")


if __name__ == "__main__":
    main()
