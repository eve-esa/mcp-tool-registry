"""MCP server for searching and subsetting CEDA CCI datasets."""

import argparse
import asyncio
import json
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ceda_client import (
    CedaOpenSearchClient,
    CedaOpenSearchError,
    geocode_local,
    inspect_dataset,
    run_pipeline,
)

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

mcp = FastMCP(
    "CEDA CCI OpenSearch",
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
)
MAX_RESULT_ROWS = 1000


@mcp.tool(
    title="CEDA CCI data subset",
    description=(
        "Find CEDA CCI datasets for an essential climate variable, then read "
        "point or area data remotely through OPeNDAP."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def ceda_run(
    ecv: str,
    start_date: str,
    end_date: str,
    place: str | None = None,
    bbox: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    dataset_uuid: str | None = None,
    dataset_index: int | None = None,
    max_files: int = 5,
) -> dict:
    """Find CEDA CCI datasets and return a bounded point or area subset.

    Args:
        ecv: Essential climate variable code, such as ``SOILMOISTURE``.
        start_date: ISO 8601 start date for the data search.
        end_date: ISO 8601 end date for the data search.
        place: Place name to geocode to a bounding box.
        bbox: Bounding box as ``west,south,east,north``.
        latitude: Point latitude; provide together with longitude.
        longitude: Point longitude; provide together with latitude.
        dataset_uuid: Optional dataset UUID to skip the dataset search.
        dataset_index: Optional zero-based index of a matching dataset.
        max_files: Maximum OPeNDAP files to read per dataset.

    Returns:
        A JSON-compatible mapping with dataset summaries, records, and truncation
        status. At most 1000 data records are returned.

    Raises:
        ValueError: If the area selection is invalid or an upstream query fails.
    """
    selected_areas = sum(value is not None for value in (place, bbox, latitude))
    if selected_areas != 1 or (latitude is None) != (longitude is None):
        raise ValueError("Use exactly one of place, bbox, or latitude with longitude.")
    if max_files < 1:
        raise ValueError("max_files must be at least 1.")

    if place is not None:
        location = json.loads(await geocode_local(place))
        if "error" in location:
            raise ValueError(location["error"])
        bbox = location["bbox"]

    area = tuple(float(value) for value in bbox.split(",")) if bbox else None
    if area is not None and len(area) != 4:
        raise ValueError("bbox must be west,south,east,north")

    def execute_pipeline() -> list[dict]:
        with CedaOpenSearchClient() as client:
            return run_pipeline(
                client,
                ecv,
                start_date,
                end_date,
                latitude,
                longitude,
                area,
                dataset_uuid,
                dataset_index,
                max_files,
            )

    try:
        results = await asyncio.to_thread(execute_pipeline)
    except (CedaOpenSearchError, OSError, KeyError) as exc:
        raise ValueError(str(exc)) from exc

    frames = []
    for result in results:
        frame = result["data"].to_dataframe().reset_index()
        frame.insert(0, "dataset_title", result["title"])
        frame.insert(0, "dataset_uuid", result["uuid"])
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    return {
        "bbox": bbox,
        "datasets": [
            {
                "uuid": result["uuid"],
                "title": result["title"],
                "inspect": inspect_dataset(result["data"]),
            }
            for result in results
        ],
        "truncated": len(frame) > MAX_RESULT_ROWS,
        "records": json.loads(frame.head(MAX_RESULT_ROWS).to_json(orient="records", date_format="iso")),
    }


def main() -> None:
    """Start the server over stdio or Streamable HTTP."""
    parser = argparse.ArgumentParser(description="CEDA CCI OpenSearch MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
        help="MCP transport (default: http)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
