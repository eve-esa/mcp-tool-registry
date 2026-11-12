#!/usr/bin/env python3
"""Offline tests: tool results carry file bytes, not server filesystem paths.

No network. Shapefile and GML fixtures are built in a temp directory.
compute_metrics downloads are replaced with tiny arrays.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import shapefile
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))

import server  # noqa: E402

_ABS_PATH = re.compile(
    r"/tmp(?:/|\b)|/var/|/Users/|/home/|/private/|[A-Za-z]:\\|"
    r"saved_geojson|saved_json|shapefile_dir|\bsave_dir\b"
)
_PATH_KEYS = {
    "path",
    "save_dir",
    "saved_json",
    "saved_geojson",
    "shapefile_dir",
    "mask_path",
    "composite_images",
}


def _walk_no_paths(value, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert not str(key).endswith("_path"), f"{where} key {key}"
            assert key not in _PATH_KEYS, f"{where} key {key}"
            _walk_no_paths(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _walk_no_paths(item, f"{where}[{i}]")
    elif isinstance(value, str):
        assert _ABS_PATH.search(value) is None, f"{where}: {value[:180]}"


def _assert_text_has_no_paths(blocks) -> None:
    for i, block in enumerate(blocks):
        if getattr(block, "type", None) != "text":
            continue
        assert _ABS_PATH.search(block.text) is None, block.text[:300]
        stripped = block.text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            _walk_no_paths(json.loads(block.text), f"text[{i}]")


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


def _file_blocks(blocks) -> list:
    return [block for block in blocks if block.type in {"image", "resource"}]


def _decode_blob(block) -> bytes:
    return base64.b64decode(block.resource.blob)


async def _burnt_areas(tmp: Path, **kwargs):
    return await server.get_effis_burnt_areas(
        bbox="23.0,37.5,24.0,38.5",
        max_features=5,
        date="2024-08-01",
        shapefile_dir=str(tmp),
        **kwargs,
    )


def test_shapefile_ldid_and_geojson_blob(tmp: Path) -> None:
    shp = _write_fire_shapefile(tmp / "ldid", "Avinyó", "latin-1")
    _set_ldid(shp, 0x57)
    blocks = asyncio.run(_burnt_areas(tmp / "ldid"))

    tool = server.mcp._tool_manager.get_tool("get_effis_burnt_areas")
    assert tool.fn_metadata.output_schema is None
    converted = tool.fn_metadata.convert_result(blocks)
    assert not isinstance(converted, tuple)
    assert [item.type for item in converted] == [item.type for item in blocks]
    assert converted[0].type == "text"
    assert converted[1].type == "resource"

    summary = json.loads(blocks[0].text)
    assert summary["fires"][0]["commune"] == "Avinyó"
    assert summary["total_features"] == 1
    _assert_text_has_no_paths(blocks)

    blob = blocks[1]
    assert blob.resource.mimeType == "application/geo+json"
    uri = str(blob.resource.uri)
    filename = uri.rsplit("/", 1)[-1]
    assert uri.startswith("file:///burnt_areas_2024-08-01_")
    assert filename.endswith(".geojson")
    geojson = json.loads(_decode_blob(blob))
    assert geojson["type"] == "FeatureCollection"
    assert geojson["features"][0]["properties"]["COMMUNE"] == "Avinyó"
    print("  [PASS] shapefile LDID 0x57 keeps Avinyó and returns a GeoJSON blob")


def test_cpg_greek_and_literal_question_marks(tmp: Path) -> None:
    greek_dir = tmp / "cpg"
    shp = _write_fire_shapefile(greek_dir, "Στύρα", "cp1253")
    shp.with_suffix(".cpg").write_text("windows-1253", encoding="ascii")
    blocks = asyncio.run(_burnt_areas(greek_dir))
    summary = json.loads(blocks[0].text)
    assert summary["fires"][0]["commune"] == "Στύρα"
    geojson = json.loads(_decode_blob(blocks[1]))
    assert geojson["features"][0]["properties"]["COMMUNE"] == "Στύρα"

    q_dir = tmp / "questions"
    q_shp = _write_fire_shapefile(q_dir, "??????", "ascii")
    _set_ldid(q_shp, 0x57)
    raw = q_shp.with_suffix(".dbf").read_bytes()
    assert b"?" in raw
    q_blocks = asyncio.run(_burnt_areas(q_dir))
    q_summary = json.loads(q_blocks[0].text)
    assert q_summary["fires"][0]["commune"].startswith("??????")
    print("  [PASS] .cpg windows-1253 restores Greek; literal '?' stays '?'")


def test_gml_utf8_greek(tmp: Path) -> None:
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
        with patch.object(server, "_http_get", new=AsyncMock(return_value=_Response())):
            return await server.get_effis_burnt_areas(
                bbox="23.0,37.5,24.0,38.5",
                max_features=1,
                date=None,
            )

    blocks = asyncio.run(_call())
    summary = json.loads(blocks[0].text)
    assert summary["fires"][0]["commune"] == "Τοπική Κοινότητα Στύρων"
    assert summary["fires"][0]["country"] == "EL"
    _assert_text_has_no_paths(blocks)
    print("  [PASS] WFS GML UTF-8 keeps Greek commune names")


def test_geojson_over_size_limit(tmp: Path) -> None:
    shp = _write_fire_shapefile(tmp / "big", "Avinyó", "latin-1")
    _set_ldid(shp, 0x57)
    original = server._MAX_ATTACHMENT_BYTES
    server._MAX_ATTACHMENT_BYTES = 40
    try:
        blocks = asyncio.run(_burnt_areas(tmp / "big"))
    finally:
        server._MAX_ATTACHMENT_BYTES = original

    assert blocks[0].type == "text"
    assert blocks[1].type == "text"
    assert "too large to attach" in blocks[1].text
    assert "25 MB" in blocks[1].text
    assert "bbox" in blocks[1].text
    assert "max_features" in blocks[1].text
    assert all(block.type != "resource" for block in blocks)
    summary = json.loads(blocks[0].text)
    assert summary["fires"]
    _assert_text_has_no_paths(blocks)
    print("  [PASS] GeoJSON over 25 MB is a note, not a blob")


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


def test_compute_metrics_file_cap_and_types() -> None:
    blocks = asyncio.run(_compute())
    tool = server.mcp._tool_manager.get_tool("compute_metrics")
    assert tool.fn_metadata.output_schema is None
    converted = tool.fn_metadata.convert_result(blocks)
    assert not isinstance(converted, tuple)
    assert [item.type for item in converted] == [item.type for item in blocks]

    assert blocks[0].type == "text"
    summary = json.loads(blocks[0].text)
    _assert_text_has_no_paths(blocks)
    note = summary["summary"]["omitted_files_note"]
    assert "10 files" in note
    assert "composite_ndvi_Tplus_24mo.png" in note

    files = _file_blocks(blocks)
    assert len(files) == 10
    assert len(files) <= server._MAX_ATTACHMENTS

    severity = Image.open(BytesIO(base64.b64decode(blocks[1].data)))
    burn = Image.open(BytesIO(base64.b64decode(blocks[2].data)))
    assert blocks[1].mimeType == "image/png"
    assert severity.getpixel((0, 0)) == (139, 0, 0)
    assert burn.getpixel((0, 0)) == 255
    assert blocks[3].type == "image"

    resources = [block for block in blocks if block.type == "resource"]
    assert resources[0].resource.mimeType == "text/csv"
    assert str(resources[0].resource.uri).endswith("recovery_2020-06-01.csv")
    csv_text = _decode_blob(resources[0]).decode("utf-8")
    assert "VRR (%)" in csv_text.splitlines()[0]

    assert resources[1].resource.mimeType == "application/json"
    assert str(resources[1].resource.uri).endswith("metrics_2020-06-01.json")
    metrics_doc = json.loads(_decode_blob(resources[1]))
    assert metrics_doc["fire_date"] == "2020-06-01"
    _walk_no_paths(metrics_doc, "metrics-json")

    for block in files:
        if block.type == "image":
            raw = base64.b64decode(block.data)
            assert raw.startswith(b"\x89PNG")
            assert block.mimeType == "image/png"
    print("  [PASS] compute_metrics attaches 10 files and drops the rest")


def test_compute_metrics_size_limit() -> None:
    original = server._MAX_ATTACHMENT_BYTES
    server._MAX_ATTACHMENT_BYTES = 8
    try:
        blocks = asyncio.run(_compute(recovery_months=[12]))
    finally:
        server._MAX_ATTACHMENT_BYTES = original

    assert blocks[0].type == "text"
    assert _file_blocks(blocks) == []
    summary = json.loads(blocks[0].text)
    note = summary["summary"]["omitted_files_note"]
    assert "25 MB" in note
    assert "severity_map.png" in note
    _assert_text_has_no_paths(blocks)
    print("  [PASS] compute_metrics drops files over the size limit")


def run_return_file_tests() -> None:
    import tempfile

    print("\n" + "=" * 60)
    print("  UNIT TEST: tool results return file bytes")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_shapefile_ldid_and_geojson_blob(tmp)
        test_cpg_greek_and_literal_question_marks(tmp)
        test_gml_utf8_greek(tmp)
        test_geojson_over_size_limit(tmp)
    test_compute_metrics_file_cap_and_types()
    test_compute_metrics_size_limit()


if __name__ == "__main__":
    run_return_file_tests()
    print("  [PASS] return-file tests")
