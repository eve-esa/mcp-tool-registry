#!/usr/bin/env python3
"""Offline tests: tools write files and name their absolute paths.

No network. Shapefile and GML fixtures are built in a temp directory.
compute_metrics downloads are replaced with tiny arrays. A second set of
checks runs the same tools through OutputFilesMiddleware.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import shapefile
from fastmcp import Client
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVER_DIR.parents[1]
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(REPO_ROOT / "shared"))

from eve_mcp_runtime.files import OutputFilesMiddleware  # noqa: E402

import server  # noqa: E402


def _write_fire_shapefile(directory: Path, commune: str, encoding: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shp_path = directory / "modis.ba.poly.shp"
    with shapefile.Writer(str(shp_path), encoding=encoding) as writer:
        writer.field("FIREDATE", "C", 20)
        writer.field("COMMUNE", "C", 80)
        writer.field("PROVINCE", "C", 40)
        writer.field("COUNTRY", "C", 8)
        writer.field("AREA_HA", "C", 20)
        writer.poly([[
            [23.5, 37.9],
            [23.6, 37.9],
            [23.6, 38.0],
            [23.5, 38.0],
            [23.5, 37.9],
        ]])
        writer.record("2024-08-01", commune, "Attica", "EL", "12")
    return shp_path


def _set_ldid(shp_path: Path, ldid: int) -> None:
    dbf_path = shp_path.with_suffix(".dbf")
    raw = bytearray(dbf_path.read_bytes())
    raw[29] = ldid
    dbf_path.write_bytes(raw)


def _save_env(tmp: Path) -> dict[str, str]:
    return {"EFFIS_SAVE_DIR": str(tmp / "out")}


async def _burnt_areas(tmp: Path, **kwargs):
    return await server.get_effis_burnt_areas(
        bbox="23.0,37.5,24.0,38.5",
        max_features=5,
        date="2024-08-01",
        shapefile_dir=str(tmp),
        **kwargs,
    )


def _summary(blocks) -> dict:
    assert blocks[0].type == "text"
    assert len(blocks) == 1
    return json.loads(blocks[0].text)


def _geojson(summary: dict) -> dict:
    path = Path(summary["geojson_path"])
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


def test_shapefile_ldid_writes_geojson(tmp_path: Path) -> None:
    shp = _write_fire_shapefile(tmp_path / "ldid", "Avinyó", "latin-1")
    _set_ldid(shp, 0x57)
    with patch.dict(os.environ, _save_env(tmp_path)):
        blocks = asyncio.run(_burnt_areas(tmp_path / "ldid"))

    summary = _summary(blocks)
    assert summary["fires"][0]["commune"] == "Avinyó"
    assert summary["total_features"] == 1
    geojson = _geojson(summary)
    assert geojson["type"] == "FeatureCollection"
    assert geojson["features"][0]["properties"]["COMMUNE"] == "Avinyó"
    assert Path(summary["geojson_path"]).name.startswith("burnt_areas_2024-08-01_")
    print("  [PASS] shapefile LDID 0x57 keeps Avinyó and writes GeoJSON")


def test_cpg_greek_and_literal_question_marks(tmp_path: Path) -> None:
    greek_dir = tmp_path / "cpg"
    shp = _write_fire_shapefile(greek_dir, "Στύρα", "cp1253")
    shp.with_suffix(".cpg").write_text("windows-1253", encoding="ascii")
    with patch.dict(os.environ, _save_env(tmp_path)):
        blocks = asyncio.run(_burnt_areas(greek_dir))
    summary = _summary(blocks)
    assert summary["fires"][0]["commune"] == "Στύρα"
    geojson = _geojson(summary)
    assert geojson["features"][0]["properties"]["COMMUNE"] == "Στύρα"

    q_dir = tmp_path / "questions"
    q_shp = _write_fire_shapefile(q_dir, "??????", "ascii")
    _set_ldid(q_shp, 0x57)
    raw = q_shp.with_suffix(".dbf").read_bytes()
    assert b"?" in raw
    with patch.dict(os.environ, _save_env(tmp_path)):
        q_blocks = asyncio.run(_burnt_areas(q_dir))
    q_summary = _summary(q_blocks)
    assert q_summary["fires"][0]["commune"].startswith("??????")
    print("  [PASS] .cpg windows-1253 restores Greek; literal '?' stays '?'")


def test_gml_utf8_greek(tmp_path: Path) -> None:
    gml = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs"
    xmlns:gml="http://www.opengis.net/gml"
    xmlns:ms="http://mapserver.gis.umn.edu/mapserver">
  <gml:featureMember>
    <ms:modis.ba.poly>
      <ms:FIREDATE>2024-08-01</ms:FIREDATE>
      <ms:COMMUNE>Τοπική Κοινότητα Στύρων</ms:COMMUNE>
      <ms:PROVINCE>Εύβοια</ms:PROVINCE>
      <ms:COUNTRY>EL</ms:COUNTRY>
      <ms:AREA_HA>10</ms:AREA_HA>
      <ms:msGeometry>
      <gml:Polygon srsName="EPSG:4326">
        <gml:outerBoundaryIs><gml:LinearRing>
          <gml:coordinates>37.9,23.5 37.9,23.6 38.0,23.6 38.0,23.5 37.9,23.5</gml:coordinates>
        </gml:LinearRing></gml:outerBoundaryIs>
      </gml:Polygon>
      </ms:msGeometry>
    </ms:modis.ba.poly>
  </gml:featureMember>
</wfs:FeatureCollection>
""".encode()

    class _Response:
        content = gml

    async def _call():
        with (
            patch.dict(os.environ, _save_env(tmp_path)),
            patch.object(server, "_http_get", new=AsyncMock(return_value=_Response())),
        ):
            return await server.get_effis_burnt_areas(
                bbox="23.0,37.5,24.0,38.5",
                max_features=1,
                date=None,
            )

    blocks = asyncio.run(_call())
    summary = _summary(blocks)
    assert summary["fires"][0]["commune"] == "Τοπική Κοινότητα Στύρων"
    assert summary["fires"][0]["country"] == "EL"
    geojson = _geojson(summary)
    assert geojson["features"][0]["properties"]["COMMUNE"] == "Τοπική Κοινότητα Στύρων"
    print("  [PASS] WFS GML UTF-8 keeps Greek commune names")


def _fake_composite(token, bounds, size, time_from, time_to, max_cloud, metric):
    value = 0.8 if str(time_from) < "2020-06-01" else 0.1
    return np.full((8, 8), value, np.float32)


def _fake_multiband(*_args, **_kwargs):
    image = np.zeros((2, 8, 8), np.float32)
    image[0] = 0.05
    image[1] = 0.45
    return image


def _fake_ndvi(*_args, **_kwargs):
    return np.full((8, 8), 0.2, np.float32)


async def _compute(**overrides):
    args = {
        "bbox": "23.5,37.9,23.6,38.0",
        "fire_date": "2020-06-01",
        "months_before": 1,
        "months_after": 1,
        "metrics": ["ndvi", "regrowth"],
        "resolution_m": 0,
        "width": 8,
        "height": 8,
        "recovery_months": [12, 24, 36, 48],
        "max_cloud_cover": 30.0,
    }
    args.update(overrides)
    with (
        patch.object(server, "_resolve_cdse_creds", return_value=("id", "secret")),
        patch.object(server, "_cdse_get_token", new=AsyncMock(return_value="token")),
        patch.object(server, "_cdse_catalog_search", new=AsyncMock(return_value=[])),
        patch.object(server, "_download_composite", new=AsyncMock(side_effect=_fake_composite)),
        patch.object(server, "_download_multiband_composite", new=AsyncMock(side_effect=_fake_multiband)),
        patch.object(server, "_download_ndvi_median_composite", new=AsyncMock(side_effect=_fake_ndvi)),
    ):
        return await server.compute_metrics(**args)


def test_compute_metrics_writes_named_files(tmp_path: Path) -> None:
    with patch.dict(os.environ, _save_env(tmp_path)):
        blocks = asyncio.run(_compute())
    compact = _summary(blocks)
    names = [entry["name"] for entry in compact["files"]]
    assert names[:4] == [
        "severity_map.png",
        "burn_mask.png",
        "recovery_2020-06-01.png",
        "recovery_2020-06-01.csv",
    ]
    assert "metrics_2020-06-01.json" in names
    assert "composite_ndvi_Tplus_48mo.png" in names
    for entry in compact["files"]:
        path = Path(entry["path"])
        assert path.is_file() and path.stat().st_size == entry["bytes"]
        assert str(path) in blocks[0].text
    severity = Image.open(BytesIO(Path(compact["files"][0]["path"]).read_bytes()))
    burn = Image.open(BytesIO(Path(compact["files"][1]["path"]).read_bytes()))
    assert severity.getpixel((0, 0)) == (139, 0, 0)
    assert burn.getpixel((0, 0)) == 255
    print(f"  [PASS] compute_metrics writes {len(names)} named files")


def _ensure_middleware() -> None:
    stack = getattr(server.mcp, "middleware", None)
    if stack is None:
        server.mcp.add_middleware(OutputFilesMiddleware(server_dir=SERVER_DIR))
        return
    if any(isinstance(item, OutputFilesMiddleware) for item in stack):
        return
    server.mcp.add_middleware(OutputFilesMiddleware(server_dir=SERVER_DIR))


async def _call_through_middleware(name: str, arguments: dict):
    _ensure_middleware()
    async with Client(server.mcp) as client:
        return await client.call_tool_mcp(name, arguments)


def test_middleware_attaches_geojson_and_strips_the_path(tmp_path: Path) -> None:
    shp = _write_fire_shapefile(tmp_path / "mw", "Avinyó", "latin-1")
    _set_ldid(shp, 0x57)
    with patch.dict(os.environ, _save_env(tmp_path)):
        result = asyncio.run(
            _call_through_middleware(
                "get_effis_burnt_areas",
                {
                    "bbox": "23.0,37.5,24.0,38.5",
                    "max_features": 5,
                    "date": "2024-08-01",
                    "shapefile_dir": str(tmp_path / "mw"),
                },
            )
        )
    blobs = [block for block in result.content if block.type == "resource"]
    assert len(blobs) == 1
    filename = str(blobs[0].resource.uri).rsplit("/", 1)[-1]
    assert filename.startswith("burnt_areas_2024-08-01_")
    assert filename.endswith(".geojson")
    text = result.content[0].text
    assert filename in text
    assert str(tmp_path) not in text
    print("  [PASS] middleware attaches the named GeoJSON and strips its path")


def test_middleware_skips_an_oversize_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MCP_MAX_ATTACHMENT_BYTES", "40")
    shp = _write_fire_shapefile(tmp_path / "big", "Avinyó", "latin-1")
    _set_ldid(shp, 0x57)
    monkeypatch.setenv("EFFIS_SAVE_DIR", str(tmp_path / "out"))
    result = asyncio.run(
        _call_through_middleware(
            "get_effis_burnt_areas",
            {
                "bbox": "23.0,37.5,24.0,38.5",
                "max_features": 5,
                "date": "2024-08-01",
                "shapefile_dir": str(tmp_path / "big"),
            },
        )
    )
    assert all(block.type != "resource" for block in result.content)
    note = "\n".join(block.text for block in result.content if block.type == "text")
    assert "over the 40 bytes limit" in note
    assert ".geojson" in note
    assert str(tmp_path) not in note
    print("  [PASS] middleware names an oversize GeoJSON and does not attach it")


def run_return_file_tests() -> None:
    import tempfile

    print("\n" + "=" * 60)
    print("  UNIT TEST: tools name output files")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_shapefile_ldid_writes_geojson(tmp)
        test_cpg_greek_and_literal_question_marks(tmp)
        test_gml_utf8_greek(tmp)
        test_compute_metrics_writes_named_files(tmp)
        test_middleware_attaches_geojson_and_strips_the_path(tmp)


if __name__ == "__main__":
    run_return_file_tests()
    print("  [PASS] return-file tests")
