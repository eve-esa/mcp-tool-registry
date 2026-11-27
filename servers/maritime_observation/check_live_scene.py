"""Optional public-data integration check. No EVE/GFW credentials required.

Run with --ais-file to reuse a previously downloaded archive after validating
its SHA-256 against current pinned HF metadata. Otherwise it downloads the file.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import shutil
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ResourceLink

from ais import AIS
from common import MaritimeError
from storage import cache_root, checksum, json_write

SCENE = "S2C_MSIL2A_20250825T105641_N0511_R094_T31UET_20250825T154713"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ais-file", type=Path)
    args = parser.parse_args()
    if args.ais_file:
        provider = AIS()
        try:
            item = provider.catalogue()["2025-08-25"]
            sha = item["lfs"]["oid"]
            if (
                args.ais_file.stat().st_size != item["size"]
                or checksum(args.ais_file) != sha
            ):
                raise MaritimeError(
                    "source_integrity_error",
                    "Provided daily file differs from published source.",
                )
            root = cache_root() / "ais"
            root.mkdir(exist_ok=True)
            from filelock import FileLock

            with FileLock(str(root / "archive.lock"), timeout=120):
                target = root / f"{sha}.parquet"
                if not target.exists() or checksum(target) != sha:
                    shutil.copyfile(args.ais_file, target)
                json_write(
                    target.with_suffix(".json"),
                    {"sha256": sha, "mtime_ns": target.stat().st_mtime_ns},
                )
        finally:
            provider.close()

    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).with_name("server.py")), "--transport", "stdio"],
        )
        async with stdio_client(parameters) as (read, write):  # noqa: SIM117 -- dependent transport and session.
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(minutes=10)
            ) as session:
                await session.initialize()
                response = await session.call_tool(
                    "prepare_scene_context",
                    {
                        "scene_id": SCENE,
                        "bbox": [3.96, 51.90, 4.10, 52.00],
                        "max_age_seconds": 300,
                        "highlight_seconds": 60,
                    },
                )
                result = response.structuredContent or json.loads(
                    response.content[0].text
                )
                if result["ok"]:
                    overlay = next(
                        a for a in result["artifacts"] if a["name"].endswith(".png")
                    )
                    link = next(
                        block
                        for block in response.content
                        if isinstance(block, ResourceLink)
                        and str(block.uri) == overlay["resource_uri"]
                        and block.mimeType == "image/png"
                    )
                    resource = await session.read_resource(link.uri)
                    if resource.contents[0].mimeType != "image/png":
                        raise ValueError("MCP resource returned the wrong media type")
                    payload = base64.b64decode(resource.contents[0].blob)
                    if hashlib.sha256(payload).hexdigest() != overlay["sha256"]:
                        raise ValueError("MCP resource differs from artifact manifest")
                return result

    result = asyncio.run(check())
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        raise SystemExit(1)
    if (
        result["source"]["distinct_valid_format_mmsi_count"] != 110
        or result["highlighted_returned_reports"] != 8
    ):
        raise SystemExit(
            "Results differ from verified preparation example; inspect provenance."
        )


if __name__ == "__main__":
    main()
