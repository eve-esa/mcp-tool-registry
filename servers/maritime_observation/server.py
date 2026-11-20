"""EVE maritime MCP: GFW processed AIS/SAR and detailed Hugging Face AIS."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink, TextContent

import service
from common import error_result, runtime_root
from storage import resource_path

load_dotenv(runtime_root() / ".env", override=False)
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("maritime_observation")
mcp = FastMCP("maritime_observation", host="0.0.0.0", port=8000, stateless_http=True)


def invoke(function, **kwargs) -> CallToolResult:
    try:
        result = function(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- MCP boundary must not expose provider payloads or secrets.
        logger.warning("%s failed (%s)", function.__name__, type(exc).__name__)
        result = error_result(exc)

    artifacts = result.get("artifacts", [])
    if artifacts:
        result["artifacts"] = [
            {key: value for key, value in item.items() if key != "path"}
            for item in artifacts
        ]
    return CallToolResult(
        structuredContent=result,
        content=[
            TextContent(type="text", text=json.dumps(result, allow_nan=False)),
            *[
                ResourceLink(
                    type="resource_link",
                    uri=item["resource_uri"],
                    name=item["name"],
                    mimeType=item["mime_type"],
                )
                for item in artifacts
            ],
        ],
    )


@mcp.tool()
def summarize_presence(
    bbox: list[float],
    start: str | None = None,
    end: str | None = None,
    period: str | None = None,
    timezone: str = "Europe/Rome",
) -> CallToolResult:
    """Summarize GFW historical hourly AIS presence, identities and vessel types.

    bbox is [west,south,east,north] WGS84. Explicit ISO start/end are half-open UTC
    (<=31 days). Or period: yesterday/last_week/today/latest/now with IANA timezone.
    Omitted/latest is an estimated recent day with 5-day lag, not confirmed latest
    availability. Never silently replace explicit dates. Requires GFW_API_TOKEN.
    Counts are GFW identities, not verified physical ships; hours are not tracks.
    Returned PNG/CSV/provenance references are downloadable MCP resources.
    """
    return invoke(
        service.summarize_presence,
        bbox=bbox,
        start=start,
        end=end,
        period=period,
        timezone=timezone,
    )


@mcp.tool()
def compare_presence(
    bbox: list[float],
    baseline_start: str,
    baseline_end: str,
    comparison_start: str,
    comparison_end: str,
) -> CallToolResult:
    """Compare two equal-duration <=31-day GFW AIS windows on the same version.

    ISO dates mean UTC midnight, end exclusive. Return identities/MMSIs/hours,
    absolute and percentage differences; zero baseline has null percentage.
    Changes can reflect coverage, not necessarily changes in traffic. Requires GFW token.
    """
    return invoke(
        service.compare_presence,
        bbox=bbox,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        comparison_start=comparison_start,
        comparison_end=comparison_end,
    )


@mcp.tool()
def summarize_sar(
    bbox: list[float],
    start: str | None = None,
    end: str | None = None,
    period: str | None = None,
    timezone: str = "Europe/Rome",
) -> CallToolResult:
    """Summarize GFW processed Sentinel-1 SAR detections and AIS matched/unmatched counts.

    Same bbox/window rules as summarize_presence. Omitted/latest uses an estimated
    recent day with 6-day lag, not confirmed latest. Requires GFW_API_TOKEN.
    Counts are detections across acquisitions, not unique ships. Unmatched does
    not establish illegal activity. This tool does not download raw SAR imagery.
    """
    return invoke(
        service.summarize_sar,
        bbox=bbox,
        start=start,
        end=end,
        period=period,
        timezone=timezone,
    )


@mcp.tool()
def get_ais_positions(
    bbox: list[float],
    start: str | None = None,
    end: str | None = None,
    period: str | None = None,
    timezone: str = "Europe/Rome",
    limit: int = 1000,
) -> CallToolResult:
    """Get latest report per valid-format MMSI from pinned Hugging Face AIS daily files.

    WGS84 bbox each side <=2 degrees, window <=24 hours, limit 1–5000.
    Omitted/latest/now uses newest published daily file, not live positions.
    Explicit dates never fall back. Initial download can be ~260 MB/day.
    A malformed source is an error, not zero ships. Return 25-record preview and
    complete bounded CSV/GeoJSON/PNG artifacts, with truncation/coverage metadata.
    """
    return invoke(
        service.get_ais_positions,
        bbox=bbox,
        start=start,
        end=end,
        period=period,
        timezone=timezone,
        limit=limit,
    )


@mcp.tool()
def prepare_scene_context(
    scene_id: str,
    bbox: list[float],
    max_age_seconds: int = 300,
    highlight_seconds: int = 60,
    limit: int = 1000,
) -> CallToolResult:
    """Overlay nearest observed AIS report per MMSI on an exact Sentinel-2 L2A product.

    scene_id is ESA product ID without .SAFE, bbox WGS84 each side <=1 degree.
    Tile SENSING_TIME is the reference; max_age_seconds 1–900, highlight 0–max_age.
    Downloads a bounded public COG crop of that exact product and pinned AIS files.
    Original coordinates/timestamps retained, no interpolation or ship detector.
    This is optical scene context; use summarize_sar separately for SAR detections.
    Return PNG overlay, CSV/GeoJSON/provenance, signed deltas and coverage caveats.
    """
    return invoke(
        service.prepare_scene_context,
        scene_id=scene_id,
        bbox=bbox,
        max_age_seconds=max_age_seconds,
        highlight_seconds=highlight_seconds,
        limit=limit,
    )


@mcp.resource("maritime://artifacts/{run_id}/{name}.png", mime_type="image/png")
def png_artifact(run_id: str, name: str) -> bytes:
    return resource_path(run_id, f"{name}.png").read_bytes()


@mcp.resource("maritime://artifacts/{run_id}/{name}.csv", mime_type="text/csv")
def csv_artifact(run_id: str, name: str) -> bytes:
    return resource_path(run_id, f"{name}.csv").read_bytes()


@mcp.resource(
    "maritime://artifacts/{run_id}/{name}.geojson", mime_type="application/geo+json"
)
def geojson_artifact(run_id: str, name: str) -> bytes:
    return resource_path(run_id, f"{name}.geojson").read_bytes()


@mcp.resource("maritime://artifacts/{run_id}/{name}.json", mime_type="application/json")
def json_artifact(run_id: str, name: str) -> bytes:
    return resource_path(run_id, f"{name}.json").read_bytes()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "http"], default="http")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    mcp.settings.port = args.port
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
