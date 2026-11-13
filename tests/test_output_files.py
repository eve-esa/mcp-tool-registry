"""Path-matching output files. Runs against the in-memory fastmcp client."""

from __future__ import annotations

import asyncio
import site
import sys
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from mcp.types import EmbeddedResource, ImageContent, ResourceLink, TextContent

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "shared"))

from eve_mcp_runtime.files import OutputFilesMiddleware  # noqa: E402


def _texts(result) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _server(tmp_path: Path) -> FastMCP:
    server_dir = tmp_path / "app"
    server_dir.mkdir(exist_ok=True)
    (server_dir / ".env").write_text("CDSE_CLIENT_SECRET=nope\n")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    mcp = FastMCP("files-test")
    mcp.add_middleware(OutputFilesMiddleware(server_dir=server_dir))

    @mcp.tool(output_schema=None)
    def make_report() -> str:
        for index in range(50):
            (out / f"tile_{index}.tif").write_bytes(b"x")
        report = out / "report.csv"
        report.write_text("a,b\n1,2\n")
        return f"Saved the report to {report}."

    @mcp.tool(output_schema=None)
    def make_dict() -> dict:
        image = out / "map.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        return {"ok": True, "map": str(image)}

    @mcp.tool(output_schema=None)
    def make_link() -> list:
        data = out / "data.json"
        data.write_text("{}")
        return [
            "Exported data.json",
            ResourceLink(type="resource_link", uri=data.as_uri(), name="data.json"),
        ]

    @mcp.tool(output_schema=None)
    def show(text: str) -> str:
        return text

    return mcp


def _call(mcp: FastMCP, name: str, arguments: dict):
    async def _run():
        async with Client(mcp) as client:
            return await client.call_tool_mcp(name, arguments)

    return asyncio.run(_run())


def test_only_named_file_is_attached(tmp_path: Path):
    result = _call(_server(tmp_path), "make_report", {})
    blobs = [block for block in result.content if isinstance(block, EmbeddedResource)]
    assert len(blobs) == 1
    assert str(blobs[0].resource.uri) == "file:///report.csv"
    assert blobs[0].resource.blob
    text = _texts(result)
    assert text == "Saved the report to report.csv."
    assert "tile_" not in text


def test_dict_path_becomes_image_and_name(tmp_path: Path):
    result = _call(_server(tmp_path), "make_dict", {})
    images = [block for block in result.content if isinstance(block, ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert result.structured_content == {"ok": True, "map": "map.png"}
    assert "map.png" in _texts(result)
    assert str(tmp_path) not in _texts(result)


def test_resource_link_becomes_file_block(tmp_path: Path):
    result = _call(_server(tmp_path), "make_link", {})
    blobs = [block for block in result.content if isinstance(block, EmbeddedResource)]
    assert len(blobs) == 1
    assert str(blobs[0].resource.uri) == "file:///data.json"
    assert not any(isinstance(block, ResourceLink) for block in result.content)


def test_url_is_not_a_path(tmp_path: Path):
    text = "See https://host/tmp/x.png for the tile."
    result = _call(_server(tmp_path), "show", {"text": text})
    assert _texts(result) == text
    assert len(result.content) == 1


def test_denylist_leaves_text_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mcp = _server(tmp_path)
    secret = tmp_path / "app" / ".env"
    link = tmp_path / "out" / "link"
    link.symlink_to(secret)
    dotfile = tmp_path / "out" / ".secret"
    dotfile.write_text("hidden")
    key_file = tmp_path / "out" / "id_rsa"
    key_file.write_text("key")
    denied_dir = tmp_path / "denied"
    denied_dir.mkdir()
    denied_file = denied_dir / "notes.txt"
    denied_file.write_text("nope")
    monkeypatch.setenv("MCP_DENY_PATHS", str(denied_dir))
    package_root = Path(site.getsitepackages()[0])
    package_file = next(package_root.rglob("*.py"))
    samples = [
        str(secret),
        "/etc/passwd",
        "/proc/self/environ",
        "/tmp/../etc/passwd",
        str(link),
        str(dotfile),
        str(key_file),
        str(package_file),
        str(denied_file),
    ]
    for sample in samples:
        result = _call(mcp, "show", {"text": f"look at {sample}"})
        assert _texts(result) == f"look at {sample}", sample
        assert len(result.content) == 1, sample


def test_caps_add_a_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_MAX_ATTACHMENTS", "1")
    monkeypatch.setenv("MCP_MAX_ATTACHMENT_BYTES", "8")
    mcp = _server(tmp_path)
    small = tmp_path / "out" / "small.txt"
    other = tmp_path / "out" / "other.txt"
    big = tmp_path / "out" / "big.bin"
    small.write_text("ok")
    other.write_text("ok")
    big.write_bytes(b"0123456789abcdef")
    result = _call(mcp, "show", {"text": f"{small} {other} {big}"})
    blobs = [block for block in result.content if isinstance(block, EmbeddedResource)]
    assert len(blobs) == 1
    assert str(blobs[0].resource.uri) == "file:///small.txt"
    note = _texts(result)
    assert "other.txt" in note
    assert "big.bin" in note
    assert "over the 8 bytes limit" in note
    assert str(small) not in note
