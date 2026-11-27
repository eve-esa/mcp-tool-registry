"""Offline contract tests, generated Parquet fixtures and a real MCP stdio client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import ResourceLink
from shapely.geometry import box

import ais
import common
import service
from common import MaritimeError, bbox_check, runtime_root, timestamp, window
from gfw import (
    GFW,
    PRESENCE,
    SAR,
    decode_report,
    detection_count,
    presence_summary,
    rows_from_report,
)
from scene import load_scene
from storage import csv_write, resource_path

BOX = [3.96, 51.90, 4.10, 52.00]
WHEN = datetime(2025, 8, 25, 11, 6, 57, tzinfo=UTC)


class TemporaryEnvironment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(
            os.environ,
            {
                "MARITIME_CACHE_DIR": str(self.root / "cache"),
                "MARITIME_OUTPUT_DIR": str(self.root / "outputs"),
                "GFW_API_TOKEN": "",
                "AIS_CACHE_MAX_MB": "2",
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()


class DeploymentTests(TemporaryEnvironment):
    def test_runtime_root_supports_shallow_and_sagemaker_layouts(self):
        with patch.object(common, "__file__", "/app/common.py"):
            self.assertEqual(runtime_root(), Path("/app").resolve())
        nested = self.root / "project/examples/maritime_observation/common.py"
        with patch.object(common, "__file__", str(nested)):
            self.assertEqual(runtime_root(), self.root / "project")

    def test_flat_server_loads_its_env_and_uses_local_storage(self):
        app = self.root / "app"
        app.mkdir()
        for source in Path(__file__).resolve().parent.glob("*.py"):
            shutil.copy2(source, app / source.name)
        (app / ".env").write_text(
            "GFW_API_TOKEN=deployment-test-placeholder\n", encoding="utf-8"
        )
        environment = dict(os.environ)
        for name in ("GFW_API_TOKEN", "MARITIME_CACHE_DIR", "MARITIME_OUTPUT_DIR"):
            environment.pop(name, None)
        probe = (
            "import os; from pathlib import Path; import server, storage; "
            "assert os.getenv('GFW_API_TOKEN') == 'deployment-test-placeholder'; "
            "assert storage.cache_root().resolve() == Path('.cache/maritime').resolve(); "
            "assert storage.output_root() == Path('outputs/maritime').resolve(); "
            "print('READY')"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=app,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("READY", result.stdout)


class TemporalTests(unittest.TestCase):
    def test_dst_calendar_day(self):
        a, b, meta = window(
            period="yesterday",
            timezone="Europe/Rome",
            now=datetime(2026, 3, 30, 12, tzinfo=UTC),
        )
        self.assertEqual(b - a, timedelta(hours=23))
        self.assertIn("Europe/Rome", meta["time_assumption"])

    def test_explicit_and_relative_rules(self):
        a, b, _ = window("2025-08-25", "2025-09-01")
        self.assertEqual(b - a, timedelta(days=7))
        self.assertEqual(timestamp("2025-08-25T13:00:00+02:00").hour, 11)
        with self.assertRaises(MaritimeError):
            window("2025-08-25", "2025-09-01", "yesterday")
        with self.assertRaises(MaritimeError):
            timestamp("2025-08-25T13:00:00")

    def test_latest_disclosures(self):
        a, _, meta = window(latest_day="2025-08-25")
        self.assertEqual(a.day, 25)
        self.assertIn("Not live", meta["time_assumption"])
        self.assertIn("Not confirmed", window()[2]["time_assumption"])

    def test_invalid_area(self):
        for value in ([170, 0, -170, 1], [0, 0, float("nan"), 1], [0, 0, 20, 1]):
            with self.assertRaises(MaritimeError):
                bbox_check(value)


class GFWTests(TemporaryEnvironment):
    def test_transient_recovery_uses_quoted_sar_filter(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.params["filters[0]"], "matched='true'")
            if len(requests) < 3:
                return httpx.Response(503, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"entries": [{SAR: [{"detections": 2}]}]})

        provider = GFW(httpx.Client(transport=httpx.MockTransport(handler)))
        with (
            patch.dict(os.environ, {"GFW_API_TOKEN": "unit-test-placeholder"}),
            patch("gfw.time.sleep") as sleep,
        ):
            rows, provenance = provider.report(
                BOX, WHEN, WHEN + timedelta(hours=1), SAR, True
            )
        provider.close()
        self.assertEqual(detection_count(rows), 2)
        self.assertEqual(provenance["http_attempts"], 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len({str(r.url) for r in requests}), 1)

    def test_persistent_error_stays_unavailable_and_auth_is_not_retried(self):
        for status, attempts in [(503, 3), (401, 1), (429, 1)]:
            with self.subTest(status=status):
                requests = []

                def handler(request, requests=requests, status=status):
                    requests.append(request)
                    return httpx.Response(status)

                provider = GFW(httpx.Client(transport=httpx.MockTransport(handler)))
                with (
                    patch.dict(os.environ, {"GFW_API_TOKEN": "unit-test-placeholder"}),
                    patch("gfw.time.sleep"),
                    self.assertRaises(MaritimeError) as caught,
                ):
                    provider.report(BOX, WHEN, WHEN + timedelta(hours=1), SAR, False)
                provider.close()
                self.assertEqual(len(requests), attempts)
                self.assertIn("not treated as zero", str(caught.exception))

    def test_missing_credentials_and_http_errors(self):
        provider = GFW(
            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401)))
        )
        with self.assertRaisesRegex(MaritimeError, "GFW_API_TOKEN"):
            provider.report(BOX, WHEN, WHEN + timedelta(hours=1), PRESENCE)
        with patch.dict(os.environ, {"GFW_API_TOKEN": "unit-test-placeholder"}):
            with self.assertRaises(MaritimeError) as caught:
                provider.report(BOX, WHEN, WHEN + timedelta(hours=1), PRESENCE)
            self.assertEqual(caught.exception.code, "authentication_failed")
        provider.close()

    def test_report_cache_and_shape(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.params["group-by"], "VESSEL_ID")
            self.assertEqual(json.loads(request.content)["geojson"]["type"], "Polygon")
            return httpx.Response(
                200, json={"entries": [{PRESENCE: []}], "nextOffset": None}
            )

        provider = GFW(httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.dict(os.environ, {"GFW_API_TOKEN": "unit-test-placeholder"}):
            first = provider.report(BOX, WHEN, WHEN + timedelta(hours=1), PRESENCE)
            second = provider.report(BOX, WHEN, WHEN + timedelta(hours=1), PRESENCE)
        provider.close()
        self.assertEqual(len(calls), 1)
        self.assertFalse(first[1]["cache_hit"])
        self.assertTrue(second[1]["cache_hit"])
        self.assertEqual(first[0], [])

    def test_incomplete_wrong_schema_and_zip(self):
        with self.assertRaises(MaritimeError):
            rows_from_report({"entries": [], "nextOffset": 50}, PRESENCE)
        with self.assertRaises(MaritimeError):
            rows_from_report({"entries": [{SAR: []}]}, PRESENCE)
        document = {"entries": [{PRESENCE: []}], "nextOffset": None}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("report.json", json.dumps(document))
            archive.writestr("caveats.json", json.dumps({"note": "fixture"}))
        self.assertEqual(decode_report(stream.getvalue()), document)

    def test_identity_count_and_observation_time(self):
        rows = [
            {
                "vesselId": "a",
                "mmsi": "123456789",
                "hours": 2,
                "vesselType": "CARGO",
                "exitTimestamp": "2025-08-25T11:00:00Z",
                "lastTransmissionDate": "2026-10-01T00:00:00Z",
            },
            {"vesselId": "b", "mmsi": "123456789", "hours": 3},
            {"vesselId": "c", "mmsi": "0", "hours": 1},
        ]
        result = presence_summary(rows)
        self.assertEqual(result["gfw_identity_count"], 3)
        self.assertEqual(result["distinct_valid_format_mmsi_count"], 1)
        self.assertEqual(result["presence_hours"], 6)
        self.assertEqual(result["observed_last_exit"], "2025-08-25T11:00:00Z")
        with self.assertRaises(MaritimeError):
            presence_summary(rows + [rows[0]])

    def test_sar_consistency_and_zero_baseline(self):
        with patch("service.GFW") as provider:
            provider.return_value.report.side_effect = [
                ([{"detections": 3}], {}),
                ([{"detections": 1}], {}),
                ([{"detections": 1}], {}),
            ]
            with self.assertRaises(MaritimeError) as caught:
                service.summarize_sar(BOX, "2025-08-25", "2025-08-26")
            self.assertEqual(caught.exception.code, "inconsistent_report")
        with patch("service.GFW") as provider:
            provider.return_value.report.side_effect = [
                ([], {}),
                ([{"vesselId": "a", "mmsi": "123456789", "hours": 2}], {}),
            ]
            result = service.compare_presence(
                BOX, "2025-08-25", "2025-08-26", "2025-08-26", "2025-08-27"
            )
            self.assertIsNone(result["changes"]["presence_hours"]["percent_change"])
        with self.assertRaises(MaritimeError):
            service.compare_presence(
                BOX, "2025-08-25", "2025-08-26", "2025-08-26", "2025-08-28"
            )


class AISTests(TemporaryEnvironment):
    def provider(self, malformed=False):
        path = self.root / "fixture.parquet"
        rows = [
            {
                "MMSI": 123456789,
                "TSTAMP": WHEN.replace(tzinfo=None) - timedelta(seconds=40),
                "LONGITUDE": 4.0,
                "LATITUDE": 51.95,
                "SOG": 102.3,
            },
            {
                "MMSI": 123456789,
                "TSTAMP": WHEN.replace(tzinfo=None) + timedelta(seconds=20),
                "LONGITUDE": 4.01,
                "LATITUDE": 51.95,
                "SOG": 1.0,
            },
            {
                "MMSI": 987654321,
                "TSTAMP": WHEN.replace(tzinfo=None),
                "LONGITUDE": 4.09,
                "LATITUDE": 51.95,
                "SOG": 0.0,
            },
            {
                "MMSI": 0,
                "TSTAMP": WHEN.replace(tzinfo=None),
                "LONGITUDE": 4.0,
                "LATITUDE": 51.95,
                "SOG": 0.0,
            },
            {
                "MMSI": 111222333,
                "TSTAMP": WHEN.replace(tzinfo=None) + timedelta(seconds=300),
                "LONGITUDE": 4.0,
                "LATITUDE": 51.95,
                "SOG": 0.0,
            },
        ]
        pq.write_table(pa.Table.from_pylist(rows), path)
        payload = b"broken parquet object" if malformed else path.read_bytes()
        sha = hashlib.sha256(payload).hexdigest()
        tree = [
            {
                "type": "file",
                "path": "2025-08-25_ais.parquet",
                "size": len(payload),
                "lfs": {"oid": sha},
            }
        ]

        def handler(request):
            if request.url.path.endswith("/DB"):
                return httpx.Response(200, json={"sha": "a" * 40})
            if "/tree/" in request.url.path:
                return httpx.Response(200, json=tree)
            return httpx.Response(200, content=payload)

        return ais.AIS(httpx.Client(transport=httpx.MockTransport(handler)))

    def test_nearest_latest_half_open_footprint_truncation(self):
        provider = self.provider()
        start, end = WHEN - timedelta(seconds=300), WHEN + timedelta(seconds=300)
        records, info = provider.positions(BOX, start, end, limit=1, reference=WHEN)
        self.assertEqual(info["distinct_valid_format_mmsi_count"], 2)
        self.assertTrue(info["truncated"])
        self.assertEqual(info["invalid_mmsi_reports"], 1)
        self.assertEqual(info["matching_report_count"], 4)  # end boundary excluded
        self.assertEqual(records[0]["mmsi"], "987654321")
        records, _ = provider.positions(
            BOX, start, end, reference=WHEN, footprint=box(3.96, 51.9, 4.05, 52)
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["delta_seconds"], 20)
        latest, _ = provider.positions(BOX, start, end)
        self.assertEqual(latest[0]["longitude"], 4.01)
        provider.close()

    def test_missing_malformed_budget(self):
        provider = self.provider(malformed=True)
        with self.assertRaises(MaritimeError) as caught:
            provider.positions(
                BOX, WHEN - timedelta(seconds=1), WHEN + timedelta(seconds=1)
            )
        self.assertEqual(caught.exception.code, "source_file_unreadable")
        with self.assertRaises(MaritimeError) as caught:
            provider.positions(
                BOX, WHEN + timedelta(days=1), WHEN + timedelta(days=1, seconds=1)
            )
        self.assertEqual(caught.exception.code, "source_date_unavailable")
        provider.close()
        provider = self.provider()
        with patch.dict(os.environ, {"AIS_CACHE_MAX_MB": "0"}):
            with self.assertRaises(MaritimeError) as caught:
                provider.positions(BOX, WHEN, WHEN + timedelta(seconds=1))
            self.assertEqual(caught.exception.code, "cache_budget_exceeded")
        provider.close()

    def test_checksum_mismatch_is_not_empty(self):
        provider = self.provider()
        provider.catalogue()["2025-08-25"]["lfs"]["oid"] = "0" * 64
        with self.assertRaises(MaritimeError) as caught:
            provider.positions(BOX, WHEN, WHEN + timedelta(seconds=1))
        self.assertEqual(caught.exception.code, "source_integrity_error")
        provider.close()

    def test_success_and_empty_exports(self):
        provider = self.provider()
        with patch("service.AIS", return_value=provider):
            result = service.get_ais_positions(
                BOX, "2025-08-25T11:05:00Z", "2025-08-25T11:10:00Z"
            )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["record_preview"]), 2)
        self.assertEqual(result["record_preview"][0]["speed_knots"], 1)
        self.assertTrue(
            any(
                Path(a["path"]).read_bytes().startswith(b"\x89PNG")
                for a in result["artifacts"]
            )
        )
        provider = self.provider()
        with patch("service.AIS", return_value=provider):
            empty = service.get_ais_positions(
                [3.0, 51.9, 3.1, 52.0], "2025-08-25T11:05:00Z", "2025-08-25T11:10:00Z"
            )
        self.assertEqual(empty["status"], "no_data")
        self.assertEqual(empty["record_preview"], [])


class SceneTests(unittest.TestCase):
    def test_exact_product_and_tile_time(self):
        product = "S2C_MSIL2A_20250825T105641_N0511_R094_T31UET_20250825T154713"

        def handler(request):
            if "copernicus" in request.url.host:
                return httpx.Response(
                    200,
                    json={
                        "id": product,
                        "geometry": box(*BOX).__geo_interface__,
                        "properties": {"datetime": "2025-08-25T10:56:41Z"},
                    },
                )
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "features": [
                            {
                                "id": "exact-copy",
                                "properties": {"s2:product_uri": product + ".SAFE"},
                                "assets": {
                                    "visual": {
                                        "href": "https://sentinel-cogs.s3.us-west-2.amazonaws.com/test/TCI.tif"
                                    }
                                },
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                text="<root><SENSING_TIME>2025-08-25T11:06:57Z</SENSING_TIME></root>",
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            scene, reference, _ = load_scene(product, BOX, client)
            self.assertEqual(reference, WHEN)
            self.assertNotEqual(scene["reference_time"], scene["catalogue_datetime"])
        with self.assertRaises(MaritimeError):
            load_scene("https://arbitrary-host/image.tif", BOX)


class ArtifactAndMCPTests(TemporaryEnvironment):
    def test_streamable_http(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("server.py")),
                "--transport",
                "http",
                "--port",
                str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ),
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    self.fail("HTTP server exited before startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("HTTP server startup timed out")

            async def run():
                async with (
                    streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (
                        read,
                        write,
                        _,
                    ),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    self.assertEqual(len((await session.list_tools()).tools), 5)
                    result = await session.call_tool(
                        "get_ais_positions", {"bbox": [10, 0, 0, 10]}
                    )
                    self.assertEqual(
                        result.structuredContent["status"], "invalid_request"
                    )

            asyncio.run(run())
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_resource_paths_and_csv(self):
        directory = self.root / "outputs" / ("a" * 32)
        directory.mkdir(parents=True)
        path = directory / "test.csv"
        csv_write(path, [{"name": "=DANGEROUS()"}])
        self.assertIn("'=DANGEROUS", path.read_text())
        self.assertEqual(resource_path(directory.name, path.name), path.resolve())
        with self.assertRaises(MaritimeError):
            resource_path(directory.name, "../../test.csv")

    def test_real_stdio_tools_error_and_resource(self):
        directory = self.root / "outputs" / ("b" * 32)
        directory.mkdir(parents=True)
        fixture = directory / "fixture.csv"
        fixture.write_bytes(b"mmsi\n123456789\n")
        server_dir = Path(__file__).resolve().parent
        command = (
            f"import sys; sys.path.insert(0, {str(server_dir)!r}); "
            "import server; from storage import manifest, output_root; "
            "server.service.get_ais_positions = lambda **kw: "
            f"{{'ok': True, 'artifacts': manifest(output_root() / {directory.name!r})}}; "
            "server.mcp.run(transport='stdio')"
        )

        async def run():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", command],
                env=dict(os.environ),
            )
            async with stdio_client(parameters) as (read, write):  # noqa: SIM117 -- separate transport/session lifetime is intentional.
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    catalogue = await session.list_tools()
                    self.assertEqual(
                        {t.name for t in catalogue.tools},
                        {
                            "summarize_presence",
                            "compare_presence",
                            "summarize_sar",
                            "get_ais_positions",
                            "prepare_scene_context",
                        },
                    )
                    result = await session.call_tool(
                        "summarize_presence",
                        {"bbox": BOX, "start": "2025-08-25", "end": "2025-08-26"},
                    )
                    self.assertEqual(
                        result.structuredContent["status"], "credentials_required"
                    )
                    result = await session.call_tool("get_ais_positions", {"bbox": BOX})
                    artifact = result.structuredContent["artifacts"][0]
                    self.assertNotIn("path", artifact)
                    self.assertNotIn(str(directory), result.content[0].text)
                    link = next(
                        block
                        for block in result.content
                        if isinstance(block, ResourceLink)
                    )
                    self.assertEqual(str(link.uri), artifact["resource_uri"])
                    self.assertEqual(link.mimeType, "text/csv")
                    resource = (await session.read_resource(link.uri)).contents[0]
                    self.assertEqual(resource.mimeType, "text/csv")
                    self.assertEqual(
                        base64.b64decode(resource.blob), fixture.read_bytes()
                    )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
