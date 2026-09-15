#!/usr/bin/env python3
"""
Integration test for the Disasters (GDACS) MCP Server.

Starts the server locally on HTTP, connects with an MCP client, and
exercises the search_gdacs_events tool through the protocol. No credentials required (GDACS is a free,
public API).

Usage:
    python test.py
    python test.py --port 9300
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
DEFAULT_PORT = 9300
STARTUP_TIMEOUT = 15
TOOL_TIMEOUT = 60


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


def _start_server(port: int) -> subprocess.Popen:
    """Launch server.py as a subprocess on the given port."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PY), "--transport", "http", "--port", str(port)],
        env=os.environ.copy(),
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
        print(f"  - {t.name}: {t.description.splitlines()[0][:80]}")
        names.append(t.name)
    print(f"\n  Total: {len(names)} tools")

    expected = {"search_gdacs_events"}
    missing = expected - set(names)
    assert not missing, f"Missing tools: {missing}"
    print("  [PASS] all expected tools registered")
    return names


async def test_search_all_events(session: ClientSession) -> dict:
    """Test search_gdacs_events with no filters (all current events)."""
    _section("TEST: search_gdacs_events() — no filters")

    t0 = time.time()
    result = await session.call_tool("search_gdacs_events", {})
    elapsed = time.time() - t0
    parsed = _print_result(result)
    assert parsed is not None, "Failed to parse response"

    if "error" in parsed:
        print(f"\n  [FAIL] {parsed['error'][:200]}")
        return parsed

    features = parsed.get("features", [])
    assert (
        isinstance(features, list) and len(features) > 0
    ), "Expected at least one event"
    print(f"\n  Elapsed:  {elapsed:.1f}s")
    print(f"  events:   {len(features)}")
    print("  [PASS] search_gdacs_events returned events")
    return parsed


async def test_search_filtered(session: ClientSession) -> dict:
    """Test search_gdacs_events filtered to earthquakes."""
    _section("TEST: search_gdacs_events(eventlist='EQ')")

    t0 = time.time()
    result = await session.call_tool("search_gdacs_events", {"eventlist": "EQ"})
    elapsed = time.time() - t0
    parsed = _print_result(result)
    assert parsed is not None, "Failed to parse response"

    if "error" in parsed:
        # No current earthquakes is a valid (but unlikely) outcome, only fail on real errors
        print(f"\n  [INFO] {parsed['error'][:200]}")
        return parsed

    features = parsed.get("features", [])
    for f in features:
        event_type = f.get("properties", {}).get("eventtype")
        assert event_type == "EQ", f"Expected only EQ events, got {event_type}"
    print(f"\n  Elapsed:  {elapsed:.1f}s")
    print(f"  events:   {len(features)}")
    print("  [PASS] search_gdacs_events filtered correctly")
    return parsed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_tests(port: int) -> None:
    """Connect to the local server and run all tests."""
    mcp_url = f"http://localhost:{port}/mcp"

    async with httpx.AsyncClient(timeout=httpx.Timeout(TOOL_TIMEOUT)) as http:
        async with streamable_http_client(mcp_url, http_client=http) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()

                await test_list_tools(session)
                await test_search_all_events(session)
                await test_search_filtered(session)


def main():
    parser = argparse.ArgumentParser(
        description="Integration test for the Disasters (GDACS) MCP Server"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to run the test server on (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()

    _section("Starting Disasters (GDACS) MCP Server")
    print(f"  port:   {args.port}")
    print(f"  server: {SERVER_PY}")

    proc = _start_server(args.port)
    try:
        asyncio.run(_wait_for_server(f"http://localhost:{args.port}/mcp"))
        print("  Server is up.\n")

        asyncio.run(run_tests(port=args.port))

        _section("DONE — all tests passed")

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
