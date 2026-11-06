"""Deterministic real MCP clients; only external HTTP responses are fixtures.

Run: python test_contract.py. No network, EVE or CDSE credentials required.
"""

import argparse
import asyncio
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import numpy as np
import tifffile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from PIL import Image

FEATURE = {
    "type": "Feature",
    "properties": {"AREA_HA": "100", "FIREDATE": "2021-08-03", "COUNTRY": "fixture"},
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]]},
}


def provider(request):
    case = os.getenv("EFFIS_CONTRACT_CASE", "normal")
    if request.url.host == "identity.dataspace.copernicus.eu":
        form = parse_qs(request.content.decode())
        pair = (form.get("client_id", [None])[0], form.get("client_secret", [None])[0])
        expected = ("hdr-id", "hdr") if case == "credentials-header" else ("env-id", "env")
        return httpx.Response(200, json={"access_token": "fixture"}) if pair == expected else httpx.Response(401)
    if request.url.host == "sh.dataspace.copernicus.eu":
        if case != "review-metrics":
            return httpx.Response(200, json={"features": []}) if request.url.path.endswith("/search") else httpx.Response(422)
        body = json.loads(request.content)
        if request.url.path.endswith("/search"):
            start = datetime.fromisoformat(body["datetime"].split("/")[0][:10]) + timedelta(days=1)
            return httpx.Response(200, json={"features": [{"id": "fixture-scene", "properties": {
                "datetime": start.strftime("%Y-%m-%dT10:00:00Z"), "eo:cloud_cover": 0}}]})
        output = body["output"]
        width, height = output.get("width", 16), output.get("height", 16)
        buffer = io.BytesIO()
        if output["responses"][0]["format"]["type"] == "image/png":
            Image.new("RGB", (width, height), color=(20, 40, 60)).save(buffer, format="PNG")
            return httpx.Response(200, content=buffer.getvalue(), headers={"Content-Type": "image/png"})
        script = body["evalscript"]
        bands = int(re.search(r"bands\s*:\s*(\d+)", script).group(1))
        start = body["input"]["data"][0]["dataFilter"]["timeRange"]["from"][:10]
        pre_or_recovered = start < "2021-08-03" or start >= "2022-01-01"
        if bands == 2:
            array = np.empty((2, height, width), dtype=np.float32)
            array[0] = 0.05 if pre_or_recovered else 0.20
            array[1] = 0.45 if pre_or_recovered else 0.30
        else:
            value = 1 if 'bands: ["SCL"' in script else (0.8 if pre_or_recovered else 0.2)
            array = np.full((height, width), value, dtype=np.float32)
        tifffile.imwrite(buffer, array, photometric="minisblack", metadata=None)
        return httpx.Response(200, content=buffer.getvalue(), headers={"Content-Type": "image/tiff"})
    if request.url.host == "nominatim.openstreetmap.org":
        return httpx.Response(200, json=[{"display_name": "Fixture", "lat": "38.8", "lon": "23.3",
                                        "boundingbox": ["38.7", "38.9", "23.2", "23.4"], "importance": 0.9}])
    if "effis" in request.url.host:
        if case == "lookup-error":
            return httpx.Response(503)
        if case == "wfs-exception":
            return httpx.Response(200, text='<ServiceExceptionReport><ServiceException code="InvalidParameterValue">Layer unavailable</ServiceException></ServiceExceptionReport>')
        if case == "wfs-ows-exception":
            return httpx.Response(200, text='<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows"><ows:Exception>Unavailable</ows:Exception></ows:ExceptionReport>')
        if case == "wfs-unexpected":
            return httpx.Response(200, text='<html><body>Provider maintenance</body></html>')
        if case == "wfs-empty":
            return httpx.Response(200, text='<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs" xmlns:gml="http://www.opengis.net/gml" numberOfFeatures="0"><gml:boundedBy><gml:null>missing</gml:null></gml:boundedBy></wfs:FeatureCollection>')
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
    if request.url.host == "archive-api.open-meteo.com":
        start = datetime.fromisoformat(request.url.params["start_date"])
        end = datetime.fromisoformat(request.url.params["end_date"])
        days = (end - start).days + 1
        hours = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(days * 24)]
        units = {"temperature_2m": "°C", "relative_humidity_2m": "%", "wind_speed_10m": "m/s",
                 "wind_direction_10m": "°", "wind_gusts_10m": "m/s", "precipitation": "mm"}
        values = {"temperature_2m": 25, "relative_humidity_2m": 40, "wind_speed_10m": 2,
                  "wind_direction_10m": 270, "wind_gusts_10m": 4, "precipitation": 0.2}
        if case == "weather-missing":
            values = dict.fromkeys(values)
        if case == "weather-units":
            units["wind_speed_10m"] = "km/h"
        return httpx.Response(200, json={
            "utc_offset_seconds": 0, "latitude": 0, "longitude": 0,
            "hourly_units": units, "daily_units": {"precipitation_sum": "mm"},
            "hourly": {"time": hours, **{k: [v] * len(hours) for k, v in values.items()}},
            "daily": {"time": [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)],
                      "precipitation_sum": [1] * days},
        })
    if request.url.host == "air-quality-api.open-meteo.com":
        start = datetime.fromisoformat(request.url.params["start_date"])
        end = datetime.fromisoformat(request.url.params["end_date"])
        hours = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
                 for i in range(((end - start).days + 1) * 24)]
        pollutants = request.url.params["hourly"].split(",")
        baseline = {"carbon_monoxide": 100, "pm2_5": 10, "pm10": 20}
        payload = {
            "utc_offset_seconds": 0, "latitude": 38.8, "longitude": 23.3,
            "hourly_units": {k: "μg/m³" for k in pollutants},
            "hourly": {"time": hours, **{k: [baseline[k] * (2 if h >= "2021-08-03" else 1) for h in hours]
                                        for k in pollutants}},
        }
        if case == "aq-missing":
            payload["hourly"]["pm2_5"][-1] = None
        if case == "aq-negative":
            payload["hourly"]["pm2_5"][-1] = -1
        if case == "aq-units":
            payload["hourly_units"]["pm2_5"] = "ppm"
        if case == "aq-shifted":
            payload["hourly"]["time"][0] = "1900-01-01T00:00"
        if case == "aq-zero":
            for key in pollutants:
                payload["hourly"][key] = [v if h >= "2021-08-03" else 0
                                           for v, h in zip(payload["hourly"][key], hours, strict=True)]
        return httpx.Response(200, json=payload)
    if request.url.host == "planetarycomputer.microsoft.com":
        if request.url.path.endswith("/search"):
            query = json.loads(request.content)
            collection, bounds = query["collections"][0], query["bbox"]
            ids = ["fixture_2020_tile", "fixture_2021_tile"] if collection == "esa-worldcover" else ["modis_2021_08"]
            if case == "stac-partial":
                bounds[2] = (bounds[0] + bounds[2]) / 2
            if case == "stac-overlap":
                ids.append("extra_2020_tile" if collection == "esa-worldcover" else "extra_modis_tile")
            features = [] if case == "stac-empty" else [{"id": name, "bbox": bounds} for name in ids]
            return httpx.Response(200, json={"features": features, "links":
                [{"rel": "next", "href": "https://example.invalid/untrusted"}] if case == "stac-next" else []})
        if request.url.path.endswith("/statistics"):
            if case == "stats-rejected":
                return httpx.Response(400, json={"error": {"message": "Asset ingestion source not registered"}})
            is_cover = request.url.params["collection"] == "esa-worldcover"
            histogram = [[50, 50], [30, 40]] if is_cover else [[80, 20], [0, 215]]
            if case == "modis-zero" and not is_cover:
                histogram = [[100], [0]]
            if case == "stats-empty":
                histogram = [[0, 0], [30, 40]] if is_cover else [[0, 0], [0, 215]]
            if case == "stats-negative":
                histogram[0][0] = -1
            return httpx.Response(200, json={"properties": {"statistics": {"map": {
                "histogram": histogram, "valid_percent": 50 if case == "stats-missing" else 100}}}})
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
async def session(transport="stdio", case="normal", headers=None):
    with tempfile.TemporaryDirectory() as base:
        directory = Path(base) / "artifacts"
        directory.mkdir()
        env = dict(os.environ, CDSE_CLIENT_ID="", CDSE_CLIENT_SECRET="", EFFIS_SHAPEFILE_DIR="",
                   DEFAULT_SAVE_DIR=str(directory), MPLCONFIGDIR=str(Path(base) / "mpl"), EFFIS_CONTRACT_CASE=case)
        if case.startswith("credentials-") or case == "review-metrics":
            env.update(CDSE_CLIENT_ID="env-id", CDSE_CLIENT_SECRET="env")
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
                    async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp", headers=headers) as (read, write, _):
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
        assert names == {"geocode_place", "get_effis_burnt_areas", "compute_metrics", "get_fire_exposure",
                         "get_fire_weather", "get_community_air_quality", "get_smoke_signal",
                         "get_burn_fuel_types", "estimate_fire_emissions", "detect_burned_area"}
        geocoded = await call(client, "geocode_place", place_name="Fixture", buffer_km=0)
        assert geocoded["top_result"]["bbox"] == "23.2000,38.7000,23.4000,38.9000", geocoded
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
        weather = await call(client, "get_fire_weather", latitude=0, longitude=0, start_date="2021-08-05")
        assert weather["ok"] and weather["antecedent_rain_mm"] == 30, weather
        assert len(weather["hourly"]["time"]) == 24 and abs(weather["event_rain_mm"] - 4.8) < 1e-8
        assert weather["event_ranges"]["temperature_2m"] == {"min": 25, "max": 25}
        community = await call(client, "get_community_air_quality", place_name="Fixture", latitude=38.8,
                               longitude=23.3, fire_latitude=38.8, fire_longitude=23.0, start_date="2021-08-03")
        assert community["ok"] and community["baseline_hours"] == 168 and community["event_hours"] == 24
        assert community["pollutants"]["pm2_5"]["mean_change"] == 10
        assert community["pollutants"]["carbon_monoxide"]["event_to_baseline_ratio"] == 2
        assert community["wind_context"]["aligned_fraction"] == 1
        smoke = await call(client, "get_smoke_signal", bbox="23.2,38.7,23.4,38.9", fire_date="2021-08-03",
                           days_after=1)
        assert smoke["ok"] and smoke["baseline_hours"] == 336 and smoke["event_hours"] == 48
        assert smoke["pollutants"]["pm10"]["peak_vs_baseline"] == 2
        assert smoke["pollutants"]["pm2_5"]["baseline_mean"] == 10
        default_smoke = await call(client, "get_smoke_signal", bbox="23.2,38.7,23.4,38.9", fire_date="2021-08-03")
        assert default_smoke["event_hours"] == 264 and default_smoke["baseline_hours"] == 336
        for overrides in [{"latitude": "nan"}, {"longitude": 181}, {"start_date": "2021-02-30"},
                          {"end_date": "2021-08-01"}, {"lookback_days": 0}, {"lookback_days": 91}]:
            invalid = await call(client, "get_fire_weather", **({"latitude": 38.8, "longitude": 23.3,
                                 "start_date": "2021-08-03"} | overrides))
            assert invalid.get("ok") is False and invalid["error_type"].startswith("invalid_"), invalid
        invalid = await call(client, "get_smoke_signal", bbox="nan,0,1,1", fire_date="2021-08-03")
        assert invalid.get("ok") is False
        estimate = await call(client, "estimate_fire_emissions", burned_area_ha=10, bbox="23.2,38.7,23.4,38.9",
                              fuel_shares_pct={"grassland": 100}, fire_date="2021-08-03")
        assert estimate["ok"], estimate
        # Independent dimensional reference: 41 t dry matter, 1613 g CO2/kg -> 66.133 t CO2.
        assert estimate["emissions_tonnes"]["CO2"]["central"] == 66.1
        assert estimate["co2_equivalent_tonnes"]["central"] == 71.0
        assert estimate["range_type"] == "fuel-consumption sensitivity, not confidence intervals"
        fuels = await call(client, "get_burn_fuel_types", bbox="23.2,38.7,23.4,38.9", fire_date="2021-08-03")
        assert fuels["ok"] and fuels["fuel_shares_pct"] == {"grassland": 50, "cropland": 50}, fuels
        assert fuels["worldcover_year"] == 2020 and fuels["pre_fire_map"] is True
        old = await call(client, "get_burn_fuel_types", bbox="23.2,38.7,23.4,38.9", fire_date="2019-08-03")
        assert old["ok"] and old["pre_fire_map"] is False and "not pre-fire" in old["map_choice"]
        mixed = await call(client, "estimate_fire_emissions", burned_area_ha=10, bbox="23.2,38.7,23.4,38.9",
                           fuel_shares_pct=fuels["fuel_shares_pct"], fire_date="2021-08-03")
        # Independently worked 5 ha grass + 5 ha rice-residue proxy: 74.729 / 79.70759 t.
        assert mixed["emissions_tonnes"]["CO2"]["central"] == 74.7
        assert mixed["co2_equivalent_tonnes"]["central"] == 79.7
        monthly = await call(client, "detect_burned_area", bbox="0,0,.01,.01", month="2021-08")
        assert monthly["ok"] and monthly["scope_kind"] == "monthly_bbox_aggregate", monthly
        assert monthly["burned_area_ha"] == 24.7 and monthly["first_burn_date"] == "2021-08-03"
        assert "perimeter" not in monthly and "feature_index" not in monthly
        automatic = await call(client, "estimate_fire_emissions", burned_area_ha=monthly["burned_area_ha"],
                               bbox=monthly["bbox"], fire_date=monthly["first_burn_date"])
        assert automatic["ok"] and "WorldCover 2020" in automatic["fuel_source"], automatic
        invalid = await call(client, "get_fire_exposure", perimeter=monthly)
        assert invalid.get("error_type") == "invalid_perimeter"
        normalized = await call(client, "estimate_fire_emissions", burned_area_ha=10, bbox="23.2,38.7,23.4,38.9",
                                fuel_shares_pct={"grassland": 5, "cropland": 5})
        assert normalized["emissions_tonnes"] == mixed["emissions_tonnes"]
        doubled = await call(client, "estimate_fire_emissions", burned_area_ha=20, bbox="23.2,38.7,23.4,38.9",
                             fuel_shares_pct={"grassland": 100})
        assert doubled["emissions_tonnes"]["CO2"]["central"] == 132
        forest = await call(client, "estimate_fire_emissions", burned_area_ha=1, bbox="23.2,38.7,23.4,38.9",
                            fuel_shares_pct={"tree_cover": 100})
        assert forest["emissions_tonnes"]["CO2"]["central"] == 31.1  # 19.8 * 1569 / 1000 = 31.0662.
        assert forest["by_fuel_type"]["tree_cover"]["fuel_burned_t_per_ha"] == 19.8
        for gas in estimate["emissions_tonnes"].values():
            assert gas["low"] <= gas["central"] <= gas["high"]
        for overrides in [{"burned_area_ha": 0}, {"burned_area_ha": -1}, {"burned_area_ha": "nan"},
                          {"burned_area_ha": "inf"}, {"burned_area_ha": 1e30}, {"bbox": "nan,0,1,1"},
                          {"fuel_shares_pct": {}}, {"fuel_shares_pct": {"grassland": -1}},
                          {"fuel_shares_pct": {"grassland": "inf"}}, {"fuel_shares_pct": {"unknown": 100}},
                          {"fuel_shares_pct": {"water": 100}}, {"fuel_shares_pct": {"grassland": 0}},
                          {"fuel_shares_pct": {"grassland": 1e308, "cropland": 1e308}},
                          {"fire_date": "2021-02-30"}]:
            invalid = await call(client, "estimate_fire_emissions", **({"burned_area_ha": 10,
                                 "bbox": "23.2,38.7,23.4,38.9", "fuel_shares_pct": {"grassland": 100}} | overrides))
            assert invalid.get("ok") is False, invalid
        unknown_date = await call(client, "get_burn_fuel_types", bbox="23.2,38.7,23.4,38.9")
        assert unknown_date["worldcover_year"] == 2021 and unknown_date["pre_fire_map"] is None
        tiny = await call(client, "get_burn_fuel_types", bbox="0,0,1e-300,1e-300")
        assert tiny.get("ok") is False
        for month in ["2021-13", "2021-08-99", "2999-01"]:
            invalid = await call(client, "detect_burned_area", bbox="0,0,.01,.01", month=month)
            assert invalid.get("ok") is False
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
    for case in ["aq-missing", "aq-units", "aq-shifted", "aq-negative", "aq-zero", "weather-missing", "weather-units"]:
        async with session(transport, case) as (client, _):
            community = await call(client, "get_community_air_quality", place_name="Fixture", latitude=38.8,
                                   longitude=23.3, fire_latitude=38.8, fire_longitude=23.0, start_date="2021-08-03")
            if case in {"aq-missing", "aq-units", "aq-shifted", "aq-negative"}:
                assert community.get("error_type") == "air_quality_unavailable", (case, community)
            elif case == "aq-zero":
                assert community["ok"] and community["pollutants"]["pm2_5"]["event_to_baseline_ratio"] is None
                assert community["pollutants"]["pm2_5"]["mean_change"] == 20
            else:
                assert community["ok"] and community["wind_context"]["aligned_fraction"] is None
                assert community["wind_context"]["available"] is False and community["warnings"]
                assert community["pollutants"]["pm2_5"]["mean_change"] == 10
    for case in ["stac-empty", "stac-next", "stac-partial", "stac-overlap", "stats-missing", "stats-empty", "stats-negative", "stats-rejected", "modis-zero"]:
        async with session(transport, case) as (client, _):
            fuel = await call(client, "get_burn_fuel_types", bbox="0,0,.01,.01", fire_date="2021-08-03")
            monthly = await call(client, "detect_burned_area", bbox="0,0,.01,.01", month="2021-08")
            if case == "modis-zero":
                assert fuel["ok"] and monthly["ok"] and monthly["burned_area_ha"] == 0
                assert "first_burn_date" not in monthly and "perimeter" not in monthly
            else:
                assert fuel.get("error_type") == "fuel_unavailable", (case, fuel)
                assert monthly.get("error_type") == "burned_area_unavailable", (case, monthly)
                assert "burned_area_ha" not in monthly
                if case == "stats-rejected":
                    assert monthly["provider_http_status"] == 400 and fuel["provider_http_status"] == 400
    async with session(transport, "credentials-env") as (client, _):
        authenticated = await call(client, "compute_metrics", bbox="0,0,.01,.01", fire_date="2021-08-03",
                                   months_before=1, months_after=1, metrics=["ndvi"], resolution_m=0, width=2, height=2)
        assert authenticated.get("error_type") == "burn_mask_unavailable", authenticated
    if transport == "http":
        for case, secret, expected in [("credentials-header", "hdr", "burn_mask_unavailable"),
                                        ("credentials-bad", "bad", "cdse_auth")]:
            headers = {"X-CDSE-Client-Id": "hdr-id", "X-CDSE-Client-Secret": secret}
            async with session(transport, case, headers) as (client, _):
                result = await call(client, "compute_metrics", bbox="0,0,.01,.01", fire_date="2021-08-03",
                                    months_before=1, months_after=1, metrics=["ndvi"], resolution_m=0, width=2, height=2)
                assert result.get("error_type") == expected, result
    print(f"PASS {transport}: all context/screening tools, arithmetic, provenance, data failures, security, credentials and false-green guards")


async def review_metrics(transport):
    parameters = {"bbox": "0,0,.01,.01", "fire_date": "2021-08-03", "months_before": 1, "months_after": 1,
                  "metrics": ["ndvi", "regrowth"], "recovery_months": [12], "resolution_m": 0, "width": 16, "height": 16}
    async with session(transport, "review-metrics") as (client, directory):
        directory = directory.resolve()
        baseline = await call(client, "compute_metrics", **parameters)
        assert baseline["ok"], baseline
        assert baseline.get("recovery", {}).get("csv_path"), "Fixture must reach recovery/CSV sinks"
        saved = Path(baseline["saved_json"])
        complete = json.loads(saved.read_text())

        def paths(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from paths(child)
            elif isinstance(value, list):
                for child in value:
                    yield from paths(child)
            elif isinstance(value, str) and value.startswith(str(directory)):
                path = Path(value)
                if path.is_file():
                    yield path

        outputs = sorted(set(paths(baseline)) | set(paths(complete)) | {
            saved.parent / "composite_nbr_pre.png", saved.parent / "composite_nbr_post.png"})
        assert all(p.is_file() for p in outputs), "Every attack target must be a real baseline output"
        assert any("timeseries" in p.name for p in outputs), baseline["plots"]
        assert any("composite" in p.name for p in outputs), outputs
        assert any(p.suffix == ".csv" for p in outputs), outputs
        assert any("/pre_fire/" in str(p) or "/post_fire/" in str(p) for p in outputs), complete.get("images")
        directories = {saved.parent}
        for output in outputs:
            parent = output.parent
            while parent != directory and parent.is_relative_to(directory):
                directories.add(parent)
                parent = parent.parent
        targets = [(p, "directory") for p in sorted(directories)] + [(p, "file") for p in outputs]
        failures = []
        for target, kind in targets:
            restored = await call(client, "compute_metrics", **parameters)
            assert restored["ok"], restored
            outside = directory.parent / ("outside-" + str(len(failures)) + "-" + str(targets.index((target, kind))))
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_bytes(b"must-not-change")
            if kind == "directory":
                shutil.rmtree(target)
                target.symlink_to(outside, target_is_directory=True)
            else:
                target.unlink()
                target.symlink_to(sentinel)
            try:
                await client.call_tool("compute_metrics", parameters)
                unchanged = sentinel.read_bytes() == b"must-not-change" and sorted(p.name for p in outside.iterdir()) == ["sentinel"]
                if not unchanged:
                    failures.append(kind + ":" + str(target.relative_to(directory)))
            finally:
                if target.is_symlink():
                    target.unlink()
        assert not failures, "Outside writes through derived symlinks: " + ", ".join(failures)
    print(f"PASS {transport}: derived metric directory and every published file/plot/composite sink confined")


async def review_wfs(transport):
    for case in ["wfs-exception", "wfs-ows-exception", "wfs-unexpected", "wfs-empty"]:
        async with session(transport, case) as (client, _):
            for date in ["today", "2021-08-03"]:
                value = await call(client, "get_effis_burnt_areas", bbox="-1,-1,1,1", date=date,
                                   max_features=1, include_perimeter=True)
                if case == "wfs-empty":
                    assert value.get("ok") is not False and value["total_features"] == 0 and value["fires"] == [], value
                else:
                    assert value.get("ok") is False and value.get("error_type") == "lookup_unavailable", (case, date, value)
                    assert "total_features" not in value, "Provider exception must not become a successful zero"
    print(f"PASS {transport}: HTTP200 WFS exceptions/unexpected roots reject, genuine empty remains empty")


async def main():
    failures = []
    review_checks = {"review-metrics": review_metrics, "review-wfs": review_wfs}
    for transport in ("stdio", "http"):
        try:
            if args.only:
                await review_checks[args.only](transport)
            else:
                await checks(transport)
                for check in review_checks.values():
                    await check(transport)
        except Exception as exc:
            pending, messages = [exc], []
            while pending:
                error = pending.pop()
                if getattr(error, "exceptions", None):
                    pending.extend(error.exceptions)
                else:
                    messages.append(str(error))
            detail = "; ".join(messages)
            failures.append((transport, detail))
            print(f"FAIL {transport}: {detail}", flush=True)
    assert not failures, failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--only", choices=["review-metrics", "review-wfs"])
    args = parser.parse_args()
    if args.serve:
        serve(args.transport, args.port)
    else:
        asyncio.run(main())
