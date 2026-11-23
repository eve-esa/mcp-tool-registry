#!/usr/bin/env python3
"""
Integration test for the Crop Suitability MCP Server.

Starts the server locally on HTTP, connects with an MCP client, and calls every
tool on a small (~30 km²) maize and wheat area near Hódmezővásárhely in the
Hungarian Great Plain (Alföld), 2021-2023, which includes the 2022 drought, and
compares it with an area near Győr in north-west Hungary. No credentials required (all sources are public).

Usage:
    python test.py
    python test.py --port 9310
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
DEFAULT_PORT = 9310
STARTUP_TIMEOUT = 30
TOOL_TIMEOUT = 180

AREA = {"bbox": "20.38,46.40,20.45,46.45", "start_date": "2021-01-01", "end_date": "2023-12-31"}
# A wetter maize area in north-west Hungary, to compare with.
COMPARE_BBOX = "17.45,47.55,17.52,47.60"


def _section(title: str) -> None:
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


async def _call(session: ClientSession, tool: str, args: dict) -> dict:
    _section(f"TEST: {tool}")
    t0 = time.time()
    result = await session.call_tool(tool, args)
    elapsed = time.time() - t0
    parsed = json.loads(result.content[0].text)
    text = json.dumps(parsed, indent=2, default=str)
    print(text[:2500] + ("\n  ... (truncated)" if len(text) > 2500 else ""))
    assert parsed.get("ok"), f"{tool} failed: {parsed.get('error')}"
    print(f"\n  Elapsed: {elapsed:.1f}s")
    assert elapsed < TOOL_TIMEOUT, f"{tool} took {elapsed:.0f}s"
    print(f"  [PASS] {tool}")
    return parsed


def _start_server(port: int, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w")
    return subprocess.Popen(
        [sys.executable, str(SERVER_PY), "--transport", "http", "--port", str(port)],
        cwd=SERVER_DIR,
        env=os.environ.copy(),
        stdout=log,
        stderr=subprocess.STDOUT,
    )


async def _wait_for_server(url: str, timeout: float = STARTUP_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.3)
    raise TimeoutError(f"Server did not start within {timeout}s")


def _stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


async def run_tests(port: int) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(TOOL_TIMEOUT)) as http:
        async with streamable_http_client(f"http://localhost:{port}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                _section("LIST TOOLS")
                names = {t.name for t in (await session.list_tools()).tools}
                expected = {
                    "plan_crop_analysis",
                    "get_vegetation_indices",
                    "get_weather_drought",
                    "get_crop_landcover",
                    "get_terrain",
                    "assess_crop_suitability",
                    "compare_areas",
                    "get_sentinel2_maps",
                }
                assert expected <= names, f"Missing tools: {expected - names}"
                print(f"  [PASS] {len(names)} tools registered")

                plan = await _call(session, "plan_crop_analysis", AREA)
                assert plan["crop_detection"]["crop_status"] in {"detected", "ambiguous", "not_found"}
                assert plan["questions_for_user"]

                bad = json.loads((await session.call_tool("get_terrain", {"bbox": "8.4,45.3,8.3,45.2"})).content[0].text)
                assert bad["ok"] is False and bad["error_type"] == "invalid_bbox", bad
                print("  [PASS] invalid bbox returns a JSON error")

                veg = await _call(session, "get_vegetation_indices", {**AREA, "indices": ["ndvi", "ndmi", "ndwi"]})
                summer22 = next(p for p in veg["periods"] if p["period"] == "2022-JJA")
                print(f"  2022 summer NDMI: {summer22['ndmi']}")

                wx = await _call(session, "get_weather_drought", AREA)
                jja22 = next(p for p in wx["periods"] if p["period"] == "2022-JJA")
                print(f"  2022 summer weather anomaly: {jja22.get('anomaly_vs_previous_years')}")

                await _call(session, "get_crop_landcover", {"bbox": AREA["bbox"], "crop": "maize"})
                await _call(session, "get_terrain", {"bbox": AREA["bbox"]})

                verdict = await _call(session, "assess_crop_suitability", {**AREA, "crop": "maize"})
                assert verdict["verdict"] in {"likely_suitable", "marginal_at_risk", "unlikely_suitable"}
                print(f"\n  Verdict for maize: {verdict['verdict']} (score {verdict['score']}, {verdict['confidence']})")

                comparison = await _call(
                    session,
                    "compare_areas",
                    {
                        "bboxes": [AREA["bbox"], COMPARE_BBOX],
                        "names": ["Hódmezővásárhely", "Győr (Kisalföld)"],
                        "crop": "maize",
                        "start_date": AREA["start_date"],
                        "end_date": AREA["end_date"],
                    },
                )
                assert len(comparison["ranking_by_suitability"]) == 2 and comparison["most_drought_affected"]
                print(f"\n{comparison['comparison_table_markdown']}")

                maps = await _call(
                    session,
                    "get_sentinel2_maps",
                    {"bboxes": [AREA["bbox"]], "names": ["Hódmezővásárhely"], "crop": "maize",
                     "start_date": AREA["start_date"], "end_date": AREA["end_date"]},
                )
                assert maps["files"]["maps_png"] and maps["areas"][0]["years"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Integration test for the Crop Suitability MCP Server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    log_path = SERVER_DIR / "outputs" / "test_server.log"
    log_path.parent.mkdir(exist_ok=True)
    proc = _start_server(args.port, log_path)
    try:
        asyncio.run(_wait_for_server(f"http://localhost:{args.port}/mcp"))
        asyncio.run(run_tests(args.port))
        print("\nAll tests passed.")
    finally:
        _stop_server(proc)
        print(f"Server log: {log_path}")


if __name__ == "__main__":
    main()
