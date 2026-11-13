"""Attach files a tool names in its result.

A tool writes a deliverable anywhere it likes and puts that absolute path in
the tool result. This middleware attaches only those paths. A path is
attached when it is a regular file and it matches no denylist rule.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import site
import sys
from pathlib import Path

from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)

logger = logging.getLogger("eve_mcp_runtime.files")

# Absolute POSIX path. The lookbehind skips the path part of a URL such as
# https://host/a/b.png, because that slash is preceded by "/" or ":".
_PATH_RE = re.compile(r"(?<![\w:/.~])/(?:[^\s\"'<>|`/]+/)*[^\s\"'<>|`/]+")
_TRAILING_PUNCT = ".,;:)]}"
_KEY_RE = re.compile(
    r"^(?:id_rsa|id_ed25519|credentials)(?:.*)?$|.*\.(?:pem|key|p12|pfx)$",
    re.IGNORECASE,
)
_SYSTEM_DIRS = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/run",
    "/root",
    "/boot",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
)

mimetypes.add_type("application/geo+json", ".geojson")


def _size_limit_text(limit: int) -> str:
    mib, rest = divmod(limit, 1024 * 1024)
    return f"{mib} MB" if mib and not rest else f"{limit} bytes"


def _omitted_note(omitted: list[str], max_count: int, max_bytes: int) -> str:
    return (
        "Some files were not attached so this response stays within "
        f"{max_count} files and {_size_limit_text(max_bytes)} each: "
        + "; ".join(omitted)
        + "."
    )


def _file_uri_path(uri: str) -> str | None:
    if not uri.startswith("file://"):
        return None
    rest = uri.removeprefix("file://")
    if rest.startswith("localhost"):
        rest = rest[len("localhost") :]
    if not rest.startswith("/"):
        return None
    return rest.split("?", 1)[0]


def _mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _file_block(path: Path) -> ImageContent | EmbeddedResource:
    mime = _mime(path)
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    if mime.startswith("image/"):
        return ImageContent(type="image", data=data, mime_type=mime)
    return EmbeddedResource(
        type="resource",
        resource=BlobResourceContents(
            uri=f"file:///{path.name}",
            mime_type=mime,
            blob=data,
        ),
    )


def _collect(value, found: dict[str, str]) -> None:
    if isinstance(value, str):
        for match in _PATH_RE.findall(value):
            raw = match.rstrip(_TRAILING_PUNCT)
            if raw:
                found.setdefault(raw, raw)
    elif isinstance(value, dict):
        for item in value.values():
            _collect(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect(item, found)


def _strip(value, found: dict[str, Path]):
    if isinstance(value, str):
        for raw, path in sorted(found.items(), key=lambda item: len(item[0]), reverse=True):
            value = value.replace(raw, path.name)
        return value
    if isinstance(value, dict):
        return {key: _strip(item, found) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip(item, found) for item in value]
    return value


class OutputFilesMiddleware(Middleware):
    def __init__(self, server_dir: Path | None = None) -> None:
        self.server_dir = Path(server_dir).resolve() if server_dir is not None else None

    def _deny_roots(self) -> list[Path]:
        roots: list[Path] = []
        if self.server_dir is not None:
            roots.append(self.server_dir)
        roots.append(Path(__file__).resolve().parent)
        roots.append(Path(sys.prefix).resolve())
        roots.append(Path(sys.base_prefix).resolve())
        try:
            roots.extend(Path(entry).resolve() for entry in site.getsitepackages())
            roots.append(Path(site.getusersitepackages()).resolve())
        except Exception:
            logger.debug("site package paths unavailable", exc_info=True)
        roots.extend(Path(entry).resolve() for entry in _SYSTEM_DIRS)
        for part in os.environ.get("MCP_DENY_PATHS", "").split(os.pathsep):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser().resolve())
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def _deny_reason(self, path: Path) -> str | None:
        for root in self._deny_roots():
            if path == root or path.is_relative_to(root):
                return f"under {root}"
        if any(part.startswith(".") for part in path.parts if part not in ("/", "")):
            return "dot component"
        if _KEY_RE.match(path.name):
            return "key-like name"
        return None

    def _resolve(self, raw: str) -> Path | None:
        try:
            path = Path(raw).resolve()
        except (OSError, RuntimeError, ValueError):
            logger.debug("rejected %s: not a usable path", raw)
            return None
        if not path.is_file():
            logger.debug("rejected %s: not a regular file", raw)
            return None
        reason = self._deny_reason(path)
        if reason:
            logger.debug("rejected %s: %s", raw, reason)
            return None
        return path

    def _limits(self) -> tuple[int, int]:
        try:
            max_count = int(os.environ.get("MCP_MAX_ATTACHMENTS", "20"))
        except ValueError:
            max_count = 20
        try:
            max_bytes = int(os.environ.get("MCP_MAX_ATTACHMENT_BYTES", str(50 * 1024 * 1024)))
        except ValueError:
            max_bytes = 50 * 1024 * 1024
        return max_count, max_bytes

    def _select(self, paths: list[Path]) -> tuple[list[Path], list[str], int, int]:
        max_count, max_bytes = self._limits()
        kept: list[Path] = []
        omitted: list[str] = []
        for path in paths:
            size = path.stat().st_size
            if size > max_bytes:
                omitted.append(
                    f"{path.name} ({size} bytes, over the {_size_limit_text(max_bytes)} limit)"
                )
                continue
            if len(kept) >= max_count:
                omitted.append(path.name)
                continue
            kept.append(path)
        return kept, omitted, max_count, max_bytes

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        if result.is_error:
            return result
        mentioned: dict[str, str] = {}
        kept_blocks = []
        for block in result.content:
            if isinstance(block, ResourceLink) and str(block.uri).startswith("file://"):
                raw = _file_uri_path(str(block.uri))
                if raw and self._resolve(raw) is not None:
                    mentioned.setdefault(raw, raw)
                    continue
            if isinstance(block, TextContent):
                _collect(block.text, mentioned)
            kept_blocks.append(block)
        if result.structured_content is not None:
            _collect(result.structured_content, mentioned)

        resolved: dict[str, Path] = {}
        for raw in mentioned:
            path = self._resolve(raw)
            if path is not None:
                resolved[raw] = path
        if not resolved:
            return result

        ordered = list(dict.fromkeys(resolved.values()))
        kept, omitted, max_count, max_bytes = self._select(ordered)
        for block in kept_blocks:
            if isinstance(block, TextContent):
                block.text = _strip(block.text, resolved)
        if result.structured_content is not None:
            result.structured_content = _strip(result.structured_content, resolved)
        extra: list = [_file_block(path) for path in kept]
        if omitted:
            extra.append(TextContent(type="text", text=_omitted_note(omitted, max_count, max_bytes)))
        result.content = kept_blocks + extra
        result._raw_mcp_result = None
        return result
