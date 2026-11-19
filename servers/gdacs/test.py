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
    assert "error" not in parsed, parsed.get("error")

    events = parsed.get("events", [])
    assert isinstance(events, list) and events, "Expected at least one event"
    assert parsed["count"] == len(events), "count does not match events"
    assert parsed["retrieved_at"], "Missing retrieved_at"
    assert {e["alert_level"] for e in events} <= {"Orange", "Red"}, "Green should be left out by default"
    assert "Green (low-impact) events are not included" in parsed["note"], "Expected the Green note"
    assert next(iter(parsed)) == "note", "note should be the first key"
    rank = {"Red": 2, "Orange": 1, "Green": 0}
    order = [(rank[e["alert_level"]], e["from_date"]) for e in events]
    assert order == sorted(order, reverse=True), "Events should be highest alert first, then newest"
    print(f"\n  Elapsed:  {elapsed:.1f}s")
    print(f"  events:   {len(events)}")
    print("  [PASS] search_gdacs_events returned events")
    return parsed


async def test_search_filtered(session: ClientSession) -> dict:
    """Test search_gdacs_events filtered to earthquakes (EQ)."""
    _section("TEST: search_gdacs_events(eventlist='EQ')")

    t0 = time.time()
    result = await session.call_tool("search_gdacs_events", {"eventlist": "EQ"})
    elapsed = time.time() - t0
    parsed = _print_result(result)
    assert parsed is not None, "Failed to parse response"

    if "error" in parsed:
        # No current earthquakes is a valid outcome, will only fail on real errors
        print(f"\n  [INFO] {parsed['error'][:200]}")
        return parsed

    events = parsed.get("events", [])
    for e in events:
        assert e["type"] == "EQ", f"Expected only EQ events, got {e['type']}"
    print(f"\n  Elapsed:  {elapsed:.1f}s")
    print(f"  events:   {len(events)}")
    print("  [PASS] search_gdacs_events filtered correctly")
    return parsed


async def _call(session: ClientSession, args: dict) -> dict:
    result = await session.call_tool("search_gdacs_events", args)
    text = result.content[0].text
    parsed = json.loads(text)
    if "error" in parsed:
        print(f"  {args} -> {parsed}")
    else:
        n = len(parsed.get("events") or parsed.get("features") or [])
        print(f"  {args} -> {n} events, truncated={parsed.get('truncated')}, {len(text) / 1000:.0f} KB")
    return parsed


async def test_search_lists_and_dates(session: ClientSession) -> None:
    """Lists of hazard types and alert levels, inside a past date range."""
    _section("TEST: search_gdacs_events() — lists + date range")

    # A fixed past window, so the expected events do not change over time.
    args = {
        "eventlist": ["EQ", "TC"],
        "alertlevel": ["Green", "Orange", "Red"],
        "from_date": "2024-12-30",
        "to_date": "2024-12-31",
    }
    parsed = await _call(session, args)
    events = parsed.get("events", [])
    assert events, f"Expected events for {args}, got {parsed}"
    for e in events:
        assert e["type"] in {"EQ", "TC"}, e["type"]
        assert e["to_date"] >= "2024-12-30", e["to_date"]
        assert e["from_date"] <= "2024-12-31", e["from_date"]
    assert any(e["alert_level"] == "Green" for e in events), "Expected Green events when Green is requested"

    # GDACS's search breaks when "TS" is in the list ("EQ;TS" returns a small
    # subset of earthquakes). Every type, TS included, must match no filter.
    window = {"alertlevel": ["Green", "Orange", "Red"], "from_date": "2024-12-30", "to_date": "2024-12-31"}
    every_type = await _call(session, {**window, "eventlist": ["EQ", "TC", "FL", "VO", "WF", "DR", "TS"]})
    no_filter = await _call(session, window)
    assert every_type["total"] == no_filter["total"], (every_type["total"], no_filter["total"])
    assert every_type["summary"]["by_type"] == no_filter["summary"]["by_type"]
    assert "Tsunamis (TS) cannot be searched" in every_type["note"], every_type.get("note")
    eq_only = await _call(session, {**window, "eventlist": "EQ"})
    eq_ts = await _call(session, {**window, "eventlist": ["EQ", "TS"]})
    assert eq_ts["total"] == eq_only["total"], (eq_ts["total"], eq_only["total"])
    print("  [PASS] lists and date range applied")


async def test_paging(session: ClientSession) -> None:
    """Paging past GDACS's 100-event pages, the 100-event list cap and the summary."""
    _section("TEST: search_gdacs_events() — paging past 100")

    args = {
        "eventlist": "EQ",
        "alertlevel": "Green",
        "from_date": "2024-12-01",
        "to_date": "2024-12-31",
        "max_results": 150,
    }
    parsed = await _call(session, args)
    events = parsed.get("events", [])
    assert len(events) == 100 == parsed["count"], f"Expected the list capped at 100, got {len(events)}"
    assert "lowered" in parsed["note"] and "partial" in parsed["note"], parsed["note"]
    assert parsed["truncated"] is True
    assert parsed["total"] > 150, "total should count every match across pages, not only the listed events"
    assert sum(parsed["summary"]["by_alert_level"].values()) == parsed["total"]
    assert parsed["total"] == 449, f"December 2024 had 449 Green earthquakes, got {parsed['total']}"
    assert parsed["summary"]["covers"].startswith(f"all {parsed['total']} matches"), parsed["summary"]["covers"]

    # A year of Green earthquakes is far more than 500: the statistic
    # endpoint must still count all of them exactly.
    year = await _call(session, {**args, "from_date": "2024-01-01", "max_results": 5})
    assert year["total_is_lower_bound"] is False, "Counts should be exact"
    assert year["total"] > 500, f"Expected more than 500 Green earthquakes in 2024, got {year['total']}"
    assert sum(year["summary"]["by_alert_level"].values()) == year["total"]

    # Mixed levels over a busy year: Green fills the page, so the server must
    # fetch Orange/Red separately to list them first, and count all of them
    # (2023 includes the two Red Turkey earthquakes of 6 February).
    all_levels = {"eventlist": "EQ", "alertlevel": ["Green", "Orange", "Red"],
                  "from_date": "2023-01-01", "to_date": "2023-12-31", "max_results": 10}
    mixed = await _call(session, all_levels)
    serious_only = await _call(session, {**all_levels, "alertlevel": ["Orange", "Red"], "max_results": 100})
    assert mixed["total_is_lower_bound"] is False and mixed["total"] > 500, mixed["total"]
    for level in ("Red", "Orange"):
        assert mixed["summary"]["by_alert_level"].get(level) == serious_only["summary"]["by_alert_level"].get(level), (
            f"{level} count should be complete: {mixed['summary']['by_alert_level']} vs {serious_only['summary']['by_alert_level']}"
        )
    assert mixed["summary"]["covers"].startswith("all "), mixed["summary"]["covers"]
    assert mixed["events"][0]["alert_level"] == "Red", "Red events should be listed first"

    small = await _call(session, {**args, "max_results": 5})
    assert len(small["events"]) == 5 and small["truncated"] is True

    # min_severity: earthquakes of magnitude 7 or more only.
    strong = await _call(session, {"eventlist": "EQ", "alertlevel": ["Green", "Orange", "Red"],
                                   "from_date": "2024-01-01", "to_date": "2024-12-31", "min_severity": 7})
    assert strong["events"], strong
    for e in strong["events"]:
        magnitude = float(e["severity"].split("Magnitude ")[1].split("M")[0])
        assert magnitude >= 7, e["severity"]
    assert strong["total"] == strong["count"] and strong["total"] < 50, "Expected a short, complete list"

    complete = await _call(session, {**args, "from_date": "2024-12-30", "max_results": 100})
    assert complete["truncated"] is False and "note" not in complete, "Expected a complete result"
    print("  [PASS] paging, max_results, count and truncated work")


async def test_geojson_format(session: ClientSession) -> None:
    """format='geojson' still returns the full GDACS FeatureCollection."""
    _section("TEST: search_gdacs_events(format='geojson')")

    args = {"eventlist": "EQ", "from_date": "2020-01-01", "to_date": "2020-12-31", "format": "geojson"}
    parsed = await _call(session, args)
    assert parsed.get("type") == "FeatureCollection", parsed.keys()
    features = parsed["features"]
    assert features and parsed["count"] == len(features)
    assert all(f["properties"]["eventtype"] == "EQ" for f in features)

    compact = await _call(session, {**args, "format": "compact"})
    assert compact["count"] == parsed["count"], "Both formats should return the same events"
    print("  [PASS] geojson format unchanged")


async def test_country_spellings(session: ClientSession) -> None:
    """Common country names are mapped to GDACS's own spellings."""
    _section("TEST: search_gdacs_events() — country spellings")

    # Kahramanmaras earthquake, 6 February 2023: GDACS lists it under "Türkiye".
    args = {"eventlist": "EQ", "country": "Turkey", "from_date": "2023-02-01", "to_date": "2023-02-28"}
    parsed = await _call(session, args)
    events = parsed.get("events", [])
    assert any(e["alert_level"] == "Red" and e["from_date"] == "2023-02-06" for e in events), (
        f"Expected the Red 6 Feb 2023 earthquake, got {parsed}"
    )
    assert parsed["country_searched"] == ["Türkiye"]
    assert all("Turkey" in e["countries"] for e in events), "GDACS spellings should show as Turkey"

    # South Korea must not pull in North Korea ("Democratic Republic of Korea").
    korea = await _call(
        session,
        {"country": "South Korea", "alertlevel": ["Green", "Orange", "Red"], "from_date": "2019-01-01", "max_results": 100},
    )
    for e in korea.get("events", []):
        assert "South Korea" in e["countries"] and "Republic of Korea" not in e["countries"], e["countries"]
    print("  [PASS] country names mapped")


async def test_no_results_hints(session: ClientSession) -> None:
    """An empty result explains why, so an agent can retry."""
    _section("TEST: search_gdacs_events() — no-results hints")

    # Only Green earthquakes on these days, and GDACS leaves Green out by
    # default, so the server falls back to Green and says so.
    fallback = await _call(session, {"eventlist": "EQ", "from_date": "2024-12-30", "to_date": "2024-12-31"})
    assert fallback.get("events"), fallback
    assert {e["alert_level"] for e in fallback["events"]} == {"Green"}
    assert "these are Green" in fallback["note"] and "not included" not in fallback["note"], fallback["note"]

    spelling = await _call(session, {"country": "Atlantis", "alertlevel": "Red"})
    assert "spelling" in spelling.get("error", ""), spelling
    print("  [PASS] Green fallback and empty-result hints work")


async def test_since_days_and_active(session: ClientSession) -> None:
    """Relative periods are worked out on the server; active_only keeps ongoing events."""
    _section("TEST: search_gdacs_events() — since_days + active_only")

    recent = await _call(session, {"eventlist": "EQ", "alertlevel": ["Green", "Orange", "Red"], "since_days": 21})
    cutoff = recent["date_range"]["from"]
    assert recent["date_range"]["to"] == recent["retrieved_at"]
    assert all(e["to_date"] >= cutoff for e in recent["events"]), "Event older than since_days"
    assert recent["count"] <= 50, "Default max_results should be 50"

    active = await _call(session, {"alertlevel": ["Green", "Orange", "Red"], "active_only": True})
    if "error" not in active:
        for e in active["events"]:
            assert e["is_current"] or e["to_date"] >= cutoff, e

    # If any tropical cyclone is active at any alert level, a search without
    # alertlevel must find it: the Green fallback has to run after the
    # active_only filter, not before it.
    every_level = await _call(session, {"eventlist": "TC", "alertlevel": ["Green", "Orange", "Red"], "active_only": True})
    default = await _call(session, {"eventlist": "TC", "active_only": True})
    if "error" not in every_level:
        assert "error" not in default, f"Active cyclones exist but the default search found none: {default}"
    print("  [PASS] since_days and active_only work")


async def test_invalid_arguments(session: ClientSession) -> None:
    """Bad arguments return a clear error instead of calling GDACS."""
    _section("TEST: search_gdacs_events() — invalid arguments")

    cases = [
        ({"from_date": "2024-13-45"}, "YYYY-MM-DD"),
        ({"to_date": "2024-12-31"}, "2000-01-01"),
        ({"from_date": "2025-01-01", "to_date": "2024-01-01"}, "on or before"),
        ({"max_results": 0}, "at least 1"),
        ({"from_date": "2099-01-01"}, "no forecasts"),
        ({"since_days": 7, "from_date": "2024-01-01"}, "not both"),
        ({"since_days": 0}, "since_days must be"),
        ({"eventlist": "TS"}, "no tsunami"),
        ({"min_severity": -1}, "min_severity"),
    ]
    for args, expected in cases:
        parsed = await _call(session, args)
        assert expected in parsed.get("error", ""), f"{args}: {parsed}"
    print("  [PASS] invalid arguments rejected")


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
                await test_search_lists_and_dates(session)
                await test_paging(session)
                await test_geojson_format(session)
                await test_country_spellings(session)
                await test_no_results_hints(session)
                await test_since_days_and_active(session)
                await test_invalid_arguments(session)


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
