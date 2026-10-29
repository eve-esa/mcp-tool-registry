"""Deterministic real MCP clients; only external HTTP responses are fixtures.

Run: python test_contract.py. No network, EVE or CDSE credentials required.
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

FEATURE = {
    "type": "Feature",
    "properties": {"AREA_HA": "100", "FIREDATE": "2021-08-03", "COUNTRY": "fixture"},
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]]},
}


def provider(request):
    case = os.getenv("EFFIS_CONTRACT_CASE", "normal")
    if "effis" in request.url.host:
        if case == "lookup-error":
            return httpx.Response(503)
        # GML2/EPSG:4326 uses latitude/longitude axis order in this inherited parser.
        return httpx.Response(200, text='''<gml:FeatureCollection xmlns:gml="http://www.opengis.net/gml"
          xmlns:ms="http://mapserver.gis.umn.edu/mapserver"><gml:featureMember><ms:fire>
          <ms:AREA_HA>100</ms:AREA_HA><ms:FIREDATE>2021-08-03</ms:FIREDATE><ms:COUNTRY>fixture</ms:COUNTRY>
          <ms:msGeometry><gml:Polygon><gml:outerBoundaryIs><gml:LinearRing>
          <gml:coordinates>0,0 0,0.01 0.01,0.01 0.01,0 0,0</gml:coordinates>
          </gml:LinearRing></gml:outerBoundaryIs></gml:Polygon></ms:msGeometry>
          </ms:fire></gml:featureMember></gml:FeatureCollection>''')
    if request.url.host == "overpass-api.de":
        if case == "partial":
            return httpx.Response(200, json={"remark": "runtime error: partial result", "elements": []})
        return httpx.Response(200, json={
            "osm3s": {"timestamp_osm_base": "2026-01-01T00:00:00Z"},
            "elements": [
                {"type": "way", "id": 1, "tags": {"building": "yes"}, "geometry": [
                    {"lon": 0.002, "lat": 0.002}, {"lon": 0.004, "lat": 0.002},
                    {"lon": 0.004, "lat": 0.004}, {"lon": 0.002, "lat": 0.004},
                    {"lon": 0.002, "lat": 0.002}]},
                {"type": "way", "id": 2, "tags": {"highway": "track"}, "geometry": [
                    {"lon": -0.005, "lat": 0.005}, {"lon": 0.015, "lat": 0.005}]},
                {"type": "node", "id": 3, "lon": 0.005, "lat": 0.005,
                 "tags": {"place": "village", "name": "Fixture"}},
            ],
        })
    raise AssertionError(f"Unconfigured provider: {request.url.host}")


def serve(transport, port):
    import server

    real_client = httpx.AsyncClient

    def fixture_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(provider)
        return real_client(*args, **kwargs)

    httpx.AsyncClient = fixture_client
    server.mcp.settings.port = port
    server.mcp.run(transport="stdio" if transport == "stdio" else "streamable-http")


@asynccontextmanager
async def session(transport="stdio", case="normal"):
    with tempfile.TemporaryDirectory() as base:
        directory = Path(base) / "artifacts"
        directory.mkdir()
        env = dict(os.environ, CDSE_CLIENT_ID="", CDSE_CLIENT_SECRET="", EFFIS_SHAPEFILE_DIR="",
                   DEFAULT_SAVE_DIR=str(directory), MPLCONFIGDIR=str(Path(base) / "mpl"), EFFIS_CONTRACT_CASE=case)
        command = [str(Path(__file__).resolve()), "--serve", "--transport", transport]
        if transport == "stdio":
            params = StdioServerParameters(command=sys.executable, args=command, env=env)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                yield client, directory
        else:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            with tempfile.TemporaryFile(mode="w+") as log:
                proc = subprocess.Popen([sys.executable, *command, "--port", str(port)], env=env,
                                        stdout=subprocess.DEVNULL, stderr=log)
                try:
                    for _ in range(200):
                        if proc.poll() is not None:
                            log.seek(0)
                            raise AssertionError("Fixture server failed: " + log.read()[-1500:])
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                break
                        except OSError:
                            await asyncio.sleep(0.05)
                    else:
                        raise AssertionError("Fixture HTTP server startup timed out")
                    async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            yield client, directory
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    assert not result.isError, f"MCP error: {name}: {result.content}"
    return json.loads(result.content[0].text)


async def checks(transport):
    async with session(transport) as (client, directory):
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"geocode_place", "get_effis_burnt_areas", "compute_metrics"} <= names
        found = await call(client, "get_effis_burnt_areas", bbox="-1,-1,1,1", date="2021-08-03",
                           max_features=1, buffer_km=0, include_perimeter=True)
        assert "fires" in found, found
        assert found["fires"][0]["perimeter"] == FEATURE
        assert not list(directory.iterdir()), "Inline lookup must not require a file handoff"
        exposure = await call(client, "get_fire_exposure", perimeter=found["fires"][0]["perimeter"])
        assert exposure["ok"], exposure
        assert exposure["building_way_count"] == 1 and exposure["named_place_point_count"] == 1
        # 0.01 degrees at the equator is ~1113 m; UTM scale adds ~1 m here.
        assert abs(exposure["road_km"] - 1.114) < 0.003, exposure
        assert exposure["osm_timestamp"] == "2026-01-01T00:00:00Z"
        assert not list(directory.iterdir()), "Inline JSON must not require local exports"
        for overrides in [{"save_dir": str(directory.parent / "escape")}, {"shapefile_dir": str(directory)},
                          {"save_dir": "https://example.invalid/file"}]:
            invalid = await call(client, "get_effis_burnt_areas", bbox="-1,-1,1,1", date="2021-08-03",
                                 include_perimeter=True, max_features=1, **overrides)
            assert invalid.get("ok") is False and invalid.get("error_type") == "invalid_path", invalid
        invalid = await call(client, "compute_metrics", bbox="0,0,.01,.01", fire_date="2021-08-03",
                             save_dir=str(directory.parent / "escape"))
        assert invalid.get("error_type") == "invalid_path", invalid
        for bad_bbox in ["nan,0,1,1", "0,0,inf,1", "1,0,0,1", "-181,0,1,1"]:
            invalid = await call(client, "get_effis_burnt_areas", bbox=bad_bbox)
            assert invalid.get("error_type") == "invalid_input", invalid
        for mutation in ["open", "oversized", "vertices", "latitude", "type", "crs", "empty", "properties", "area"]:
            bad = deepcopy(FEATURE)
            if mutation == "open":
                bad["geometry"]["coordinates"][0].pop()
            elif mutation == "oversized":
                bad["properties"]["padding"] = "x" * 256_001
            elif mutation == "vertices":
                bad["geometry"]["coordinates"] = [[[0, 0]] * 10_001]
            elif mutation == "latitude":
                bad["geometry"]["coordinates"][0][1][1] = 90
            elif mutation == "type":
                bad["geometry"]["type"] = "LineString"
            elif mutation == "crs":
                bad["crs"] = {"name": "EPSG:3857"}
            elif mutation == "properties":
                bad["properties"] = []
            elif mutation == "area":
                bad["geometry"]["coordinates"] = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
            else:
                bad["geometry"]["coordinates"] = []
            invalid = await call(client, "get_fire_exposure", perimeter=bad)
            assert invalid.get("ok") is False, (mutation, invalid)
        legacy = await call(client, "get_effis_burnt_areas", bbox="-1,-1,1,1", date="2021-08-03",
                            max_features=1, buffer_km=0)
        path = Path(legacy["saved_geojson"])
        assert path.resolve().is_relative_to(directory.resolve())
        local = await call(client, "get_fire_exposure", geojson_path=str(path), feature_index=0)
        assert local["ok"] and local["building_way_count"] == 1, local
        image = Path(local["map_path"])
        assert image.stat().st_size > 0 and "server-local" in local["artifact_scope"]
        image.unlink()
        image.mkdir()  # A real filesystem failure: a directory cannot be a PNG.
        optional = await call(client, "get_fire_exposure", geojson_path=str(path), feature_index=0)
        assert optional["ok"] and optional["building_way_count"] == 1 and optional["warnings"], optional
        assert "map_path" not in optional
        outside = directory.parent / "outside.geojson"
        outside.write_text(json.dumps({"type": "FeatureCollection", "features": [FEATURE]}))
        link = directory / "escape.geojson"
        link.symlink_to(outside)
        for args in [{"geojson_path": str(link)}, {"geojson_path": str(directory / "expired.geojson")},
                     {"geojson_path": "https://example.invalid/file.geojson"},
                     {"geojson_path": str(path), "feature_index": -1}]:
            invalid = await call(client, "get_fire_exposure", **args)
            assert invalid.get("error_type") == "invalid_perimeter", invalid
        denied = await call(client, "compute_metrics", bbox="0,0,.01,.01", fire_date="2021-08-03")
        assert denied.get("error_type") == "cdse_auth", denied
    async with session(transport, "partial") as (client, _):
        partial = await call(client, "get_fire_exposure", perimeter=FEATURE)
        assert partial.get("error_type") == "overpass_unavailable" and "building_way_count" not in partial
    import test as live_checks

    async with session(transport, "lookup-error") as (client, _):
        for check in [live_checks.test_get_effis_burnt_areas, live_checks.test_compute_metrics]:
            try:
                await check(client)
            except AssertionError:
                pass
            else:
                raise AssertionError(f"{check.__name__} reported success for a failed tool")
    print(f"PASS {transport}: lookup/exposure, confinement, malformed inputs, optional exports, false-green guards")


async def main():
    for transport in ("stdio", "http"):
        await checks(transport)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.serve:
        serve(args.transport, args.port)
    else:
        asyncio.run(main())
