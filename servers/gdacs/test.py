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
import tempfile
import time
import traceback
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

SERVER_DIR = Path(__file__).resolve().parent
SERVER_PY = SERVER_DIR / "server.py"
sys.path.insert(0, str(SERVER_DIR))
from regions import in_region, regions_of  # noqa: E402

DEFAULT_PORT = 9300
STARTUP_TIMEOUT = 15
TOOL_TIMEOUT = 180  # region and box searches page through GDACS, which can be slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _end(event: dict) -> str:
    """An event's end date, or its latest update if it is still going."""
    return event.get("to_date") or event.get("last_updated") or ""


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
    """Launch server.py as a subprocess on the given port.

    Its output goes to a temporary file, not a pipe: the server logs every
    GDACS request, and an unread pipe fills (about 64 KB) and blocks it.
    """
    log = tempfile.TemporaryFile()  # noqa: SIM115 (closed with the process)
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PY), "--transport", "http", "--port", str(port)],
        env=os.environ.copy(),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    proc.log = log  # type: ignore[attr-defined]
    return proc


def _server_log(proc: subprocess.Popen, limit: int = 2000) -> str:
    """The end of the server's output."""
    proc.log.seek(0)  # type: ignore[attr-defined]
    return proc.log.read().decode(errors="replace")[-limit:]  # type: ignore[attr-defined]


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
    # Every alert level by default (GDACS itself leaves Green out).
    every_level = await _call(session, {"alertlevel": ["Green", "Orange", "Red"]})
    assert parsed["total"] == every_level["total"], (parsed["total"], every_level["total"])
    assert "Green" in parsed["summary"]["by_alert_level"], parsed["summary"]
    assert not any("Green" in t and "not included" in t for t in parsed.get("tell_user", []))
    # Narrowing the levels is said in tell_user.
    serious = await _call(session, {"alertlevel": ["Orange", "Red"]})
    assert "Only Red and Orange alert events were searched; Green events are not included." in serious["tell_user"]
    red = await _call(session, {"alertlevel": "Red"})
    assert "Only Red alert events were searched; Orange and Green events are not included." in red["tell_user"]
    rank = {"Red": 2, "Orange": 1, "Green": 0}
    levels = [rank[e["alert_level"]] for e in events]
    assert levels == sorted(levels, reverse=True), "Key events should be highest alert first"
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
        assert _end(e) >= "2024-12-30", e
        assert e["from_date"] <= "2024-12-31", e["from_date"]
    assert any(e["alert_level"] == "Green" for e in events), "Expected Green events when Green is requested"

    # GDACS's search breaks when "TS" is in the list ("EQ;TS" returns a small
    # subset of earthquakes). Every type, TS included, must match no filter.
    window = {"alertlevel": ["Green", "Orange", "Red"], "from_date": "2024-12-30", "to_date": "2024-12-31"}
    every_type = await _call(session, {**window, "eventlist": ["EQ", "TC", "FL", "VO", "WF", "DR", "TS"]})
    no_filter = await _call(session, window)
    assert every_type["total"] == no_filter["total"], (every_type["total"], no_filter["total"])
    assert every_type["summary"]["by_type"] == no_filter["summary"]["by_type"]
    assert any("no tsunami data" in t for t in every_type["tell_user"]), every_type.get("tell_user")
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
    assert "lowered" in parsed["note"], parsed["note"]
    assert "events lists the 100 key events" in parsed["note"], parsed["note"]
    assert parsed["truncated"] is True
    assert parsed["total"] > 150, "total should count every match across pages, not only the listed events"
    assert sum(parsed["summary"]["by_alert_level"].values()) == parsed["total"]
    assert parsed["total"] == 449, f"December 2024 had 449 Green earthquakes, got {parsed['total']}"
    # Under the fetch limit, every detail is computed from all 449 events.
    assert not any("come from" in t for t in parsed.get("tell_user", [])), parsed.get("tell_user")
    assert parsed["summary"]["largest"]["earthquake"], parsed["summary"]

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
    # Over 2,000 matches: counts stay exact, and tell_user says the details are partial.
    assert any("The counts are exact" in t for t in mixed["tell_user"]), mixed.get("tell_user")
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
    assert complete["truncated"] is False, "Expected a complete result"
    assert "events lists" not in complete.get("note", "")
    assert not any(t.startswith("Counts are minimums") for t in complete.get("tell_user", []))
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

    # Brackets are dropped: "Myanmar (Burma)" searches Myanmar (Red M7.7
    # earthquake, 28 March 2025).
    bracketed = await _call(session, {"country": "Myanmar (Burma)", "eventlist": "EQ", "alertlevel": "Red",
                                      "from_date": "2025-03-01", "to_date": "2025-03-31"})
    assert bracketed.get("events"), bracketed
    assert bracketed["country_searched"] == ["Myanmar"], bracketed["country_searched"]
    # GDACS's country filter is a substring match; only events naming the
    # country as whole words are kept, and a longer country name wins.
    def names(parsed: dict) -> set[str]:
        return {c for e in parsed.get("events", []) for c in e["countries"]}

    guinea = await _call(session, {"country": "Guinea", "eventlist": "EQ", "from_date": "2024-01-01",
                                   "to_date": "2024-06-30"})
    assert not any("Papua New Guinea" in e["countries"] for e in guinea.get("events", [])), names(guinea)
    niger = await _call(session, {"country": "Niger", "from_date": "2019-01-01", "to_date": "2024-12-31",
                                  "max_results": 100})
    assert niger["events"] and all("Niger" in e["countries"] for e in niger["events"]), names(niger)
    assert niger["summary"]["countries"].get("Niger") == niger["total"], niger["summary"]["countries"]
    oman = await _call(session, {"country": "Oman", "from_date": "2023-01-01", "to_date": "2024-12-31"})
    assert oman["events"] and all("Oman" in e["countries"] for e in oman["events"]), names(oman)
    dominica = await _call(session, {"country": "Dominica", "eventlist": "EQ", "from_date": "2023-01-01",
                                     "to_date": "2024-12-31"})
    assert "Dominican Republic" not in names(dominica), names(dominica)
    for congo in ("Congo", "Republic of Congo"):
        parsed = await _call(session, {"country": congo, "eventlist": "EQ", "from_date": "2023-01-01",
                                       "to_date": "2024-12-31"})
        assert "DR Congo" not in names(parsed), (congo, names(parsed))
    # A real country with nothing in the period gets no spelling hint.
    assert "spelling" not in dominica.get("error", ""), dominica

    # "South Africa" is a country, not the "Africa" region.
    assert "error" not in (await _call(session, {"country": "South Africa", "alertlevel": ["Green", "Orange", "Red"]}))
    print("  [PASS] country names mapped")


async def test_no_results_hints(session: ClientSession) -> None:
    """An empty result explains why, so an agent can retry."""
    _section("TEST: search_gdacs_events() — no-results hints")

    # Only Green earthquakes on these days: found without asking for Green.
    green_only = await _call(session, {"eventlist": "EQ", "from_date": "2024-12-30", "to_date": "2024-12-31"})
    assert green_only.get("events"), green_only
    assert {e["alert_level"] for e in green_only["events"]} == {"Green"}

    spelling = await _call(session, {"country": "Atlantis", "alertlevel": "Red"})
    assert "spelling" in spelling.get("error", ""), spelling
    print("  [PASS] Green found by default; empty-result hints work")


async def test_since_days_and_active(session: ClientSession) -> None:
    """Relative periods are worked out on the server; active_only keeps ongoing events."""
    _section("TEST: search_gdacs_events() — since_days + active_only")

    recent = await _call(session, {"eventlist": "EQ", "alertlevel": ["Green", "Orange", "Red"], "since_days": 21})
    cutoff = recent["date_range"]["from"]
    assert recent["date_range"]["to"] == recent["retrieved_at"]
    assert all(_end(e) >= cutoff for e in recent["events"]), "Event older than since_days"
    assert recent["count"] <= 20, "Default max_results should be 20"

    active = await _call(session, {"alertlevel": ["Green", "Orange", "Red"], "active_only": True})
    if "error" not in active:
        for e in active["events"]:
            assert e["is_current"] or _end(e) >= cutoff, e

    # If any tropical cyclone is active at any alert level, a search without
    # alertlevel must find it: the Green fallback has to run after the
    # active_only filter, not before it.
    every_level = await _call(session, {"eventlist": "TC", "alertlevel": ["Green", "Orange", "Red"], "active_only": True})
    default = await _call(session, {"eventlist": "TC", "active_only": True})
    if "error" not in every_level:
        assert "error" not in default, f"Active cyclones exist but the default search found none: {default}"
    print("  [PASS] since_days and active_only work")


async def test_most_recent(session: ClientSession) -> None:
    """most_recent is the event with the latest to_date, not the first listed."""
    _section("TEST: search_gdacs_events() — most_recent")

    # Indonesian volcanoes: a Red eruption from 2025 heads the list, but
    # later single-day eruptions are more recent.
    volcanoes = await _call(session, {"eventlist": "VO", "country": "Indonesia",
                                      "alertlevel": ["Green", "Orange", "Red"]})
    assert "error" not in volcanoes, volcanoes
    latest = max(_end(e) for e in volcanoes["events"])
    assert _end(volcanoes["most_recent"]) == latest, volcanoes["most_recent"]
    print(f"  most_recent: {volcanoes['most_recent']['name']} ({latest}); "
          f"first listed: {volcanoes['events'][0]['name']} ({volcanoes['events'][0]['to_date']})")
    print("  [PASS] most_recent is the latest event")


def _inside(e: dict, box: list[float]) -> bool:
    west, south, east, north = box
    if not south <= e["lat"] <= north:
        return False
    return west <= e["lon"] <= east if west <= east else (e["lon"] >= west or e["lon"] <= east)


async def test_places(session: ClientSession) -> None:
    """Every place goes in the call: country lists, seas, any bbox, and active fallback."""
    _section("TEST: search_gdacs_events() — country lists, seas, bbox, active fallback")
    every_level = ["Green", "Orange", "Red"]

    # Several countries in one call: each event counted once.
    usa = await _call(session, {"eventlist": "DR", "alertlevel": every_level, "country": "USA"})
    mexico = await _call(session, {"eventlist": "DR", "alertlevel": every_level, "country": "Mexico"})
    both = await _call(session, {"eventlist": "DR", "alertlevel": every_level, "country": ["USA", "Mexico"]})
    assert "error" not in both, both
    assert max(usa["total"], mexico["total"]) <= both["total"] <= usa["total"] + mexico["total"], (
        usa["total"], mexico["total"], both["total"])
    assert both["country_searched"] == ["United States", "Mexico", "México"], both["country_searched"]

    # A sea is searched by coordinates.
    bay = await _call(session, {"eventlist": "TC", "alertlevel": every_level, "country": "Bay of Bengal",
                                "from_date": "2024-01-01", "to_date": "2024-12-31"})
    assert "error" not in bay, bay
    box = bay["area_searched"]["bbox"]
    assert all(_inside(e, box) for e in bay["events"]), [(e["lat"], e["lon"]) for e in bay["events"]]
    assert any("coordinates" in t for t in bay["tell_user"])
    # Cyclones worldwide in 2024 are under the fetch limit, so every one was checked.
    assert bay["total_is_lower_bound"] is False and bay["total"] >= 1, bay

    # Every hazard worldwide in 2024 is over 10,000 events: GDACS has no
    # location filter, so the server checks the first 2,000 and says the
    # area was only partly searched, never "no results".
    bay_all = await _call(session, {"country": "Bay of Bengal", "from_date": "2024-01-01", "to_date": "2024-12-31"})
    assert bay_all["total_is_lower_bound"] is True, bay_all
    assert any("only partly searched" in t for t in bay_all["tell_user"]), bay_all["tell_user"]
    open_sea = await _call(session, {"bbox": "-30,-40,-29,-39", "from_date": "2024-01-01", "to_date": "2024-12-31"})
    assert "error" not in open_sea and open_sea["total"] == 0, open_sea
    assert any("only partly searched" in t for t in open_sea["tell_user"]), open_sea

    # "South Atlantic" is its own box, not the whole Atlantic.
    south = await _call(session, {"eventlist": "TC", "country": "South Atlantic", "from_date": "2024-01-01",
                                  "to_date": "2024-12-31"})
    assert south.get("area_searched", {}).get("bbox") == [-70, -60, 20, 0] or "error" in south, south
    assert not any(e["lat"] > 0 for e in south.get("events", [])), "South Atlantic returned northern events"

    # Any area as a bbox (geocode_place's format), with the country it is in.
    california = "-124.4820,32.5295,-114.1308,42.0095"
    quakes = await _call(session, {"eventlist": "EQ", "alertlevel": every_level, "country": "United States",
                                   "bbox": california, "from_date": "2025-01-01", "to_date": "2025-12-31"})
    assert "error" not in quakes, quakes
    assert all(_inside(e, quakes["area_searched"]["bbox"]) for e in quakes["events"])
    # US earthquakes are under the fetch limit: all were checked, so the count is complete.
    assert quakes["total_is_lower_bound"] is False, quakes

    # Nothing active: the response says so and gives the most recent match.
    volcano = await _call(session, {"eventlist": "VO", "country": "Indonesia", "alertlevel": every_level,
                                    "active_only": True})
    if volcano.get("total") == 0:
        assert volcano["tell_user"][0].startswith("Nothing matching is active now"), volcano
        assert _end(volcano["most_recent"]), volcano
    # A drought GDACS updated in the last week is current, whatever its iscurrent
    # flag, and shows last_updated rather than an end date.
    droughts = await _call(session, {"eventlist": "DR", "since_days": 7})
    for e in droughts.get("events", []):
        assert e["is_current"] and "last_updated" in e and "to_date" not in e, e

    # Earthquakes are single moments: never ongoing, but recent ones are kept
    # by active_only, and tell_user says so.
    quakes = await _call(session, {"eventlist": "EQ", "active_only": True})
    if quakes.get("events"):
        assert all(not e["is_current"] for e in quakes["events"])
        assert quakes["summary"]["ongoing"] == 0, quakes["summary"]
        assert any("single moments" in t for t in quakes["tell_user"]), quakes["tell_user"]
    # The summary is computed from every match: the largest wildfire is the
    # largest of all of them, and the key events start with it.
    fires = await _call(session, {"eventlist": "WF", "country": "Africa", "since_days": 7, "max_results": 100})
    if "error" not in fires:
        def hectares(text: str) -> float:
            return float(text.split(" in ")[-1].split(" ")[0])

        largest = hectares(fires["summary"]["largest"]["wildfire"]["severity"])
        listed = [hectares(e["severity"]) for e in fires["events"]]
        assert largest >= max(listed), (largest, max(listed))
        rank = {"Red": 2, "Orange": 1, "Green": 0}
        order = [(rank[e["alert_level"]], _end(e)) for e in fires["events"]]
        assert order == sorted(order, reverse=True), "key events: highest alert, then most recent"
        assert fires["summary"]["ongoing"] <= fires["total"]
        assert "_size" not in fires["events"][0], "internal keys must not leak"
    # A name GDACS has never recorded is reported as unknown, not as "no events".
    for unknown in ("Queensland", "Atlantis"):
        parsed = await _call(session, {"country": unknown})
        assert "has ever recorded" in parsed.get("error", ""), parsed
    # A real country with nothing in the period is still just "no results".
    iceland = await _call(session, {"eventlist": "VO", "country": "Iceland", "from_date": "2026-11-20"})
    assert "error" not in iceland or "has ever recorded" not in iceland["error"], iceland

    # GDACS names only earthquake regions after some states ("Southern California");
    # other hazards are filed under the country, so the error points to geocode.
    california = await _call(session, {"eventlist": "WF", "country": "California", "from_date": "2025-01-01",
                                       "to_date": "2025-12-31"})
    assert "only in earthquake region names" in california.get("error", ""), california
    # A day past the month's end is read as the month's last day.
    february = await _call(session, {"eventlist": "EQ", "country": "Turkey", "from_date": "2023-02-01",
                                     "to_date": "2023-02-29"})
    assert february["date_range"]["to"] == "2023-02-28", february.get("date_range", february)

    # Named events, matched as whole words, over 5 years by default.
    melissa = await _call(session, {"eventlist": "TC", "country": "Jamaica", "name": "Melissa"})
    assert any("MELISSA" in e["name"] for e in melissa["events"]), melissa
    # An older named event without dates: the error says only 5 years were searched.
    haiyan = await _call(session, {"eventlist": "TC", "name": "Haiyan"})
    assert "covers only the last 5 years" in haiyan.get("error", ""), haiyan
    etna = await _call(session, {"name": "Etna"})
    assert etna["events"] and all("Etna" in e["name"] for e in etna["events"]), [e["name"] for e in etna["events"]]

    # "Near" is a point and radius: every event within radius_km.
    import math
    tokyo = (35.6812, 139.7671)
    near = await _call(session, {"eventlist": "EQ", "country": "Japan", "near": f"{tokyo[0]},{tokyo[1]}",
                                 "radius_km": 150, "since_days": 365})
    for e in near.get("events", []):
        p1, p2 = math.radians(tokyo[0]), math.radians(e["lat"])
        d = 2 * 6371 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2)
                                           * math.sin(math.radians(e["lon"] - tokyo[1]) / 2) ** 2))
        assert d <= 150.5, (e["name"], d)

    # The period searched is always reported, and a default period is said in tell_user.
    default = await _call(session, {"eventlist": "VO", "country": "Indonesia"})
    assert default["date_range"]["from"].startswith("about 12 months"), default["date_range"]
    assert "No dates were given, so this covers about the last 12 months." in default["tell_user"]
    print("  [PASS] country lists, seas, bbox and active fallback work")


async def test_regions(session: ClientSession) -> None:
    """A continent or region returns and counts only the events in it."""
    _section("TEST: search_gdacs_events() — continents and regions")

    every_level = ["Green", "Orange", "Red"]
    world = await _call(session, {"eventlist": "WF", "alertlevel": every_level, "since_days": 30})
    africa = await _call(session, {"eventlist": "WF", "alertlevel": every_level, "since_days": 30,
                                   "country": "Africa"})
    assert "error" not in africa, africa
    assert africa["region_searched"] == "Africa"
    assert africa["note"].startswith("total and summary cover only Africa"), africa["note"]
    assert africa["total"] < world["total"], "Africa's total should be below the worldwide total"
    for e in africa["events"]:
        assert in_region(e["countries"], "Africa"), e["countries"]
    outside = [c for c in africa["summary"]["countries"] if "Africa" not in regions_of(c)]
    assert not outside, f"top_countries lists countries outside Africa: {outside}"

    # Names that contain a region's country but lie elsewhere.
    assert regions_of("Papua New Guinea") == {"Oceania"}
    assert regions_of("New Ireland Region") == {"Oceania"}
    assert regions_of("South Georgia Island Region") == set()
    assert regions_of("British Indian Ocean Territory") == set()
    assert "Caribbean" in regions_of("Dominica") and "Caribbean" in regions_of("Dominican Republic")
    assert regions_of("Near Coast Of Peru") >= {"South America"}
    assert "Europe" not in regions_of("Kazakhstan") and "Asia" in regions_of("Kazakhstan")
    assert regions_of("Nigeria") == {"Africa", "West Africa"}
    assert "West Africa" not in regions_of("Chad"), "Chad is Central Africa"

    # African sub-regions are searchable.
    west = await _call(session, {"eventlist": "WF", "alertlevel": every_level, "since_days": 30,
                                 "country": "West Africa"})
    assert "error" not in west and west["region_searched"] == "West Africa", west
    assert west["total"] <= africa["total"]
    for e in west["events"]:
        assert in_region(e["countries"], "West Africa"), e["countries"]

    # Russia and Turkey count in Europe in full, and the note says so.
    europe = await _call(session, {"eventlist": "WF", "alertlevel": every_level, "country": "Europe",
                                   "from_date": "2026-01-01"})
    # Europe is small though wildfires worldwide are over the fetch limit: its
    # countries are fetched in full, so the details are complete too.
    assert not any("come from" in t for t in europe.get("tell_user", [])), europe.get("tell_user")
    if "Russia" in europe.get("summary", {}).get("countries", {}):
        assert any("both Europe and Asia" in t for t in europe["tell_user"]), europe["tell_user"]

    # "the Caribbean" and "worldwide" are accepted.
    caribbean = await _call(session, {"alertlevel": every_level, "country": "the Caribbean", "from_date": "2025-10-01",
                                      "to_date": "2025-11-30"})
    assert "error" not in caribbean, caribbean
    assert any("MELISSA" in (e["name"] or "") for e in caribbean["events"]), "Hurricane Melissa (2025) missing"
    worldwide = await _call(session, {"eventlist": "WF", "alertlevel": every_level, "since_days": 30,
                                      "country": "worldwide"})
    assert worldwide["total"] == world["total"] and "region_searched" not in worldwide
    print("  [PASS] continents and regions filtered")


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
        ({"country": "Scandinavia"}, "Pass its countries as a list"),
        ({"country": "America"}, "'United States'"),
        ({"eventlist": "EQ", "name": "Guinea"}, "Only tropical cyclones (TC) and volcanoes (VO) have names"),
        ({"country": ["Africa", "Kenya"]}, "on its own"),
        ({"country": "Bay of Bengal", "bbox": "80,5,95,23"}, "not both"),
        ({"bbox": "1,2,3"}, "bbox must be"),
        ({"bbox": "0,10,10,5"}, "out of range"),
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
                await test_most_recent(session)
                await test_places(session)
                await test_regions(session)
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
        output = _server_log(proc)
        if output:
            print(f"\n  Server output:\n{output}")
        sys.exit(1)

    except Exception as exc:
        # The MCP client wraps failures in an ExceptionGroup: show the real ones.
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        print(f"\n  [FAIL] {type(exc).__name__}: {exc}")
        traceback.print_exception(exc, limit=-3)
        sys.exit(1)

    finally:
        _stop_server(proc)
        print("  Server stopped.")


if __name__ == "__main__":
    main()
