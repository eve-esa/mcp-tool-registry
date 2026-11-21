"""MCP server for searching and extracting data from the CEDA archive.

Tools:
    ceda_run      — legacy CCI ECV extraction returning raw OPeNDAP records
    ceda_search   — whole-archive dataset search with natural-language inputs
    ceda_describe — OPeNDAP dataset structure inspection
    ceda_files    — file listing for a dataset
    ceda_extract  — statistics plus a size-capped sample for any archive dataset
Result-size limits are configurable through the environment: CEDA_MAX_RESULT_ROWS,
CEDA_MAX_ELEMENTS, CEDA_MAX_STATS_VARIABLES, and CEDA_MAX_FILES. A value of 0
(or any negative number) disables the corresponding limit.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd
from ceda_client import (
    CedaOpenSearchClient,
    CedaOpenSearchError,
    compute_stats_text,
    describe_text,
    geocode_local,
    inspect_dataset,
    open_subset,
    parse_when,
    render_sample,
    run_extraction,
    run_pipeline,
)
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

_SERVER_DIR = Path(__file__).resolve().parent
load_dotenv(_SERVER_DIR / ".env", override=False)

mcp = FastMCP(
    "CEDA OpenSearch",
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
)
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _limit_from_env(name: str, default: int | None) -> int | None:
    """Read a configurable limit from the environment.

    Unset or non-integer values fall back to ``default``; values <= 0 disable
    the limit entirely, leaving the corresponding output uncapped.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None


# Maximum output dimensions. Defaults restore the historical caps; override or
# disable them with the CEDA_MAX_* environment variables (see README).
MAX_RESULT_ROWS = _limit_from_env("CEDA_MAX_RESULT_ROWS", 1000)
MAX_ELEMENTS = _limit_from_env("CEDA_MAX_ELEMENTS", 100)
MAX_STATS_VARIABLES = _limit_from_env("CEDA_MAX_STATS_VARIABLES", 10)
MAX_FILES = _limit_from_env("CEDA_MAX_FILES", 5)


async def _resolve_area(
    place: str | None,
    bbox: str | None,
    latitude: float | None,
    longitude: float | None,
    *,
    required: bool,
) -> tuple[tuple[float, ...] | None, str | None]:
    """Validate the area selectors and geocode place names.

    Returns the numeric ``(west, south, east, north)`` tuple for OPeNDAP
    subsetting and the comma-separated text for OpenSearch ``bbox`` filtering.
    """
    if (latitude is None) != (longitude is None):
        raise ValueError("Provide latitude and longitude together.")
    selected = sum(value is not None for value in (place, bbox, latitude))
    if selected > 1:
        raise ValueError("Use at most one of place, bbox, or latitude with longitude.")
    if required and selected == 0:
        raise ValueError("Use exactly one of place, bbox, or latitude with longitude.")
    if place is not None:
        location = json.loads(await geocode_local(place))
        if "error" in location:
            raise ValueError(location["error"])
        bbox = location["bbox"]
    area = tuple(float(value) for value in bbox.split(",")) if bbox else None
    if area is not None and len(area) != 4:
        raise ValueError("bbox must be west,south,east,north")
    return area, bbox


def _search_filters(**facets: str | None) -> dict[str, str]:
    """Drop empty facet values so optional tool arguments stay optional."""
    return {key: value.strip() for key, value in facets.items() if value is not None and value.strip()}


def _human_size(num_bytes: Any) -> str:
    """Render a byte count as a human-readable size."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


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
    max_files: int | None = None,
) -> dict:
    """Find CEDA CCI datasets and return a point or area subset.

    Args:
        ecv: Essential climate variable code, such as ``SOILMOISTURE``.
        start_date: ISO 8601 start date for the data search.
        end_date: ISO 8601 end date for the data search.
        place: Place name to geocode to a bounding box.
        bbox: Bounding box as ``west,south,east,north``.
        latitude: Point latitude; provide together with longitude.
        longitude: Point longitude; provide together with latitude.
        dataset_uuid: Optional dataset UUID to skip the dataset search.
        dataset_index: Optional zero-based index of a matching dataset;
            out-of-range values fall back to a sole single match.
        max_files: Maximum OPeNDAP files to read per dataset; omit for the
            CEDA_MAX_FILES limit (5 by default, every file when disabled).

    Returns:
        A JSON-compatible mapping with dataset summaries and up to
        CEDA_MAX_RESULT_ROWS (1000) data records, flagged via ``truncated``.

    Raises:
        ValueError: If the area selection is invalid or an upstream query fails.
    """
    if max_files is not None and max_files < 1:
        raise ValueError("max_files must be at least 1.")
    file_limit = max_files if max_files is not None else MAX_FILES
    area, bbox_text = await _resolve_area(place, bbox, latitude, longitude, required=True)

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
                file_limit,
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
    truncated = MAX_RESULT_ROWS is not None and len(frame) > MAX_RESULT_ROWS
    if MAX_RESULT_ROWS is not None:
        frame = frame.head(MAX_RESULT_ROWS)
    return {
        "bbox": bbox_text,
        "datasets": [
            {
                "uuid": result["uuid"],
                "title": result["title"],
                "inspect": inspect_dataset(result["data"]),
            }
            for result in results
        ],
        "truncated": truncated,
        "records": json.loads(frame.to_json(orient="records", date_format="iso")),
    }


@mcp.tool(
    title="CEDA archive search",
    description=(
        "Search the whole CEDA archive for datasets using free text, a place name, "
        "natural-language dates, and OpenSearch facets such as project, sensor, or "
        "data type. Returns a numbered list of dataset UUIDs and titles."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def ceda_search(
    query: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    place: str | None = None,
    bbox: str | None = None,
    ecv: str | None = None,
    project: str | None = None,
    data_type: str | None = None,
    sensor: str | None = None,
    platform: str | None = None,
    file_format: str | None = None,
    frequency: str | None = None,
    institute: str | None = None,
    processing_level: str | None = None,
    product_string: str | None = None,
    product_version: str | None = None,
    drs_id: str | None = None,
    parent_identifier: str | None = None,
    max_results: int = 20,
) -> str:
    """Find CEDA datasets with free text, a place name, or archive facets.

    Natural language is accepted: ``query`` may be plain text such as "soil
    moisture over Africa", ``start_date``/``end_date`` accept "June 2020",
    "2020-06", "3 days ago" or "last month" (ISO 8601 also works), and
    ``place`` accepts any place name, geocoded to a bounding box. Combine at
    most one of place or bbox.

    Args:
        query: Free-text search terms, e.g. "sea surface temperature".
        start_date: Window start (ISO 8601 or natural language).
        end_date: Window end (ISO 8601 or natural language).
        place: Place name to geocode into a bounding-box filter.
        bbox: Bounding box as ``west,south,east,north``.
        ecv: Essential climate variable code, e.g. SOILMOISTURE.
        project: Project or collection, e.g. cci, cmip5.
        data_type: Dataset type code, e.g. AOD, SST.
        sensor: Sensor name, e.g. AVHRR.
        platform: Satellite platform, e.g. ENVISAT.
        file_format: File format, e.g. NetCDF.
        frequency: Sampling frequency code, e.g. MON.
        institute: Producing institute.
        processing_level: Processing level, e.g. L3.
        product_string: Product string filter.
        product_version: Product version filter.
        drs_id: Data Reference Syntax identifier.
        parent_identifier: Collection UUID to search within.
        max_results: Maximum number of datasets to list; capped at the
            CEDA_MAX_ELEMENTS limit (100) unless it is disabled.

    Returns:
        Numbered dataset lines with UUID and title; use the index with
        ``ceda_extract`` or the UUID with ``ceda_describe``/``ceda_files``.
    """
    limit = max(1, int(max_results))
    if MAX_ELEMENTS is not None:
        limit = min(limit, MAX_ELEMENTS)
    filters: dict[str, Any] = _search_filters(
        query=query,
        ecv=ecv,
        project=project,
        data_type=data_type,
        sensor=sensor,
        platform=platform,
        file_format=file_format,
        frequency=frequency,
        institute=institute,
        processing_level=processing_level,
        product_string=product_string,
        product_version=product_version,
        drs_id=drs_id,
        parent_identifier=parent_identifier,
    )
    if start_date:
        filters["start_date"] = parse_when(start_date)
    if end_date:
        filters["end_date"] = parse_when(end_date)
    _, bbox_text = await _resolve_area(place, bbox, None, None, required=False)
    if bbox_text:
        filters["bbox"] = bbox_text
    filters["max_records"] = limit

    def execute() -> list[dict[str, Any]]:
        with CedaOpenSearchClient() as client:
            return client.search_datasets(**filters)

    try:
        entries = await asyncio.to_thread(execute)
    except (CedaOpenSearchError, OSError, KeyError) as exc:
        raise ValueError(str(exc)) from exc
    if not entries:
        return "No CEDA datasets matched the search."
    lines = [f"Found {len(entries)} dataset(s); use each index with ceda_extract or each UUID with ceda_describe."]
    for index, entry in enumerate(entries):
        line = f"[{index}] {entry['uuid']} | {entry['title']}"
        if entry.get("date"):
            line += f" | {entry['date']}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool(
    title="CEDA dataset structure",
    description=(
        "Inspect the structure of a CEDA dataset through OPeNDAP: dimensions, "
        "coordinate ranges, variables with units, and global attributes. Use it "
        "before ceda_extract to pick variable names. Optional date windows help "
        "when the file listing starts with thousands of non-OPeNDAP tiles."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def ceda_describe(
    dataset_uuid: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Describe the structure of a CEDA dataset (dimensions, variables, units).

    Args:
        dataset_uuid: Dataset UUID from ``ceda_search``.
        start_date: Optional window start (ISO 8601 or natural language); the
            readable-file scan only sees files dated within the window.
        end_date: Optional window end (ISO 8601 or natural language).

    Returns:
        Compact text listing dimensions, coordinate ranges, variables with
        units, and truncated global attributes.
    """
    uuid = (dataset_uuid or "").strip()
    if not uuid:
        raise ValueError("dataset_uuid is required.")
    parsed_start = parse_when(start_date) if start_date else None
    parsed_end = parse_when(end_date) if end_date else None

    def execute() -> tuple[str, Any]:
        with CedaOpenSearchClient() as client:
            files = client.list_files(uuid, parsed_start, parsed_end, 1, True)
        if not files:
            raise CedaOpenSearchError(f"No OPeNDAP files found for dataset {uuid} after scanning its file listing.")
        url = files[0]["opendap"]
        return url, open_subset(url).load()

    try:
        url, dataset = await asyncio.to_thread(execute)
    except (CedaOpenSearchError, OSError, KeyError) as exc:
        raise ValueError(str(exc)) from exc
    return f"Dataset {uuid}\nOPeNDAP source: {url}\n{describe_text(dataset)}"


@mcp.tool(
    title="CEDA dataset files",
    description=(
        "List the files inside a CEDA dataset with dates, sizes, and OPeNDAP "
        "availability; opendap_only skips straight to the OPeNDAP-readable "
        "files. Dates accept natural language such as 'June 2020'."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def ceda_files(
    dataset_uuid: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
    opendap_only: bool = False,
) -> str:
    """List the files of a CEDA dataset with dates, sizes, and OPeNDAP status.

    Args:
        dataset_uuid: Dataset UUID from ``ceda_search``.
        start_date: Optional window start (ISO 8601 or natural language).
        end_date: Optional window end (ISO 8601 or natural language).
        limit: Maximum number of files to list; omit for the CEDA_MAX_ELEMENTS
            cap (100 by default, every file when disabled).
        opendap_only: List only OPeNDAP-readable files; the scan continues
            past earlier non-OPeNDAP entries instead of stopping at them.

    Returns:
        Numbered file lines with title, date, human-readable size, and
        ``opendap=yes/no`` availability.
    """
    uuid = (dataset_uuid or "").strip()
    if not uuid:
        raise ValueError("dataset_uuid is required.")
    listed = limit if limit is not None else MAX_ELEMENTS
    if listed is not None:
        listed = max(1, int(listed))
    parsed_start = parse_when(start_date) if start_date else None
    parsed_end = parse_when(end_date) if end_date else None

    def execute() -> list[dict[str, Any]]:
        with CedaOpenSearchClient() as client:
            return client.list_files(uuid, parsed_start, parsed_end, listed, opendap_only)

    try:
        files = await asyncio.to_thread(execute)
    except (CedaOpenSearchError, OSError, KeyError) as exc:
        raise ValueError(str(exc)) from exc
    if not files:
        return f"No files found for dataset {uuid} in the given window."
    lines = [f"Found {len(files)} file(s); only files with opendap=yes can be read by ceda_extract."]
    for index, item in enumerate(files):
        availability = "opendap=yes" if item["opendap"] else "opendap=no"
        lines.append(f"[{index}] {item['title']} | {item['date']} | {_human_size(item['filesize'])} | {availability}")
    return "\n".join(lines)


@mcp.tool(
    title="CEDA data extraction",
    description=(
        "Extract data from any CEDA dataset through OPeNDAP and summarise it for "
        "quick reading: per-variable statistics (count, mean, standard deviation, "
        "min/max, percentiles) plus a pipe-delimited sample capped at "
        "CEDA_MAX_ELEMENTS rows. Select a dataset by UUID or by search filters; "
        "the area can be a place name, a bounding box, or a point."
    ),
    annotations=READ_ONLY_ANNOTATIONS,
)
async def ceda_extract(
    dataset_uuid: str | None = None,
    query: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    place: str | None = None,
    bbox: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    variables: str | None = None,
    ecv: str | None = None,
    project: str | None = None,
    data_type: str | None = None,
    sensor: str | None = None,
    platform: str | None = None,
    file_format: str | None = None,
    frequency: str | None = None,
    institute: str | None = None,
    processing_level: str | None = None,
    product_string: str | None = None,
    product_version: str | None = None,
    drs_id: str | None = None,
    parent_identifier: str | None = None,
    dataset_index: int | None = None,
    max_files: int | None = None,
    max_datasets: int = 1,
) -> str:
    """Extract CEDA data and return statistics plus a size-capped sample.

    Natural language is accepted: dates such as "June 2020" or "3 days ago",
    place names, and free-text ``query``. ``variables`` is a comma-separated
    list matched case-insensitively against dataset variables (substrings
    allowed, e.g. "sm, moisture"). Combine at most one of place, bbox, or
    latitude with longitude; omitting the area extracts full coverage.

    Args:
        dataset_uuid: Dataset UUID to extract from, skipping the search.
        query: Free-text search terms when no UUID is given.
        start_date: Window start (ISO 8601 or natural language).
        end_date: Window end (ISO 8601 or natural language).
        place: Place name to geocode into a bounding-box subset.
        bbox: Bounding box subset as ``west,south,east,north``.
        latitude: Point latitude; provide together with longitude.
        longitude: Point longitude; provide together with latitude.
        variables: Comma-separated variable names or substrings to keep.
        ecv: Essential climate variable code, e.g. SOILMOISTURE.
        project: Project or collection, e.g. cci, cmip5.
        data_type: Dataset type code, e.g. AOD, SST.
        sensor: Sensor name, e.g. AVHRR.
        platform: Satellite platform, e.g. ENVISAT.
        file_format: File format, e.g. NetCDF.
        frequency: Sampling frequency code, e.g. MON.
        institute: Producing institute.
        processing_level: Processing level, e.g. L3.
        product_string: Product string filter.
        product_version: Product version filter.
        drs_id: Data Reference Syntax identifier.
        parent_identifier: Collection UUID to search within.
        dataset_index: Zero-based index of the search result to extract;
            out-of-range values fall back to a sole single match. Omit to
            use the first result(s) up to max_datasets.
        max_files: Maximum OPeNDAP-readable files per dataset to read; omit
            for the CEDA_MAX_FILES limit (5 by default, every file when
            disabled).
        max_datasets: Maximum datasets to extract; capped at the
            CEDA_MAX_ELEMENTS limit (100) unless it is disabled.

    Returns:
        One section per dataset with statistics lines and a pipe-delimited
        sample of up to CEDA_MAX_ELEMENTS rows in total.
    """
    if max_files is not None and max_files < 1:
        raise ValueError("max_files must be at least 1.")
    file_limit = max_files if max_files is not None else MAX_FILES
    selected = max(1, int(max_datasets))
    if MAX_ELEMENTS is not None:
        selected = min(selected, MAX_ELEMENTS)
    wanted = [token.strip() for token in (variables or "").split(",") if token.strip()]
    filters: dict[str, Any] = _search_filters(
        query=query,
        ecv=ecv,
        project=project,
        data_type=data_type,
        sensor=sensor,
        platform=platform,
        file_format=file_format,
        frequency=frequency,
        institute=institute,
        processing_level=processing_level,
        product_string=product_string,
        product_version=product_version,
        drs_id=drs_id,
        parent_identifier=parent_identifier,
    )
    if start_date:
        filters["start_date"] = parse_when(start_date)
    if end_date:
        filters["end_date"] = parse_when(end_date)
    area, bbox_text = await _resolve_area(place, bbox, latitude, longitude, required=False)
    if bbox_text:
        filters["bbox"] = bbox_text

    def execute() -> list[dict[str, Any]]:
        with CedaOpenSearchClient() as client:
            return run_extraction(
                client,
                dataset_uuid=dataset_uuid.strip() if dataset_uuid else None,
                dataset_index=dataset_index,
                max_files=file_limit,
                max_datasets=selected,
                variables=wanted or None,
                latitude=latitude,
                longitude=longitude,
                area=area,
                **filters,
            )

    try:
        results = await asyncio.to_thread(execute)
    except (CedaOpenSearchError, OSError, KeyError) as exc:
        raise ValueError(str(exc)) from exc

    sections: list[str] = []
    budget = MAX_ELEMENTS
    for result in results:
        data = result["data"]
        lines = [f"Dataset: {result['title']} ({result['uuid']})"]
        stats = compute_stats_text(data, MAX_STATS_VARIABLES)
        if stats:
            lines.append(stats)
        if budget is None or budget > 0:
            sample, rendered = render_sample(data, budget)
            if budget is None:
                lines.append(f"Sample ({rendered} rows, pipe-delimited):")
            else:
                budget -= rendered
                lines.append(f"Sample (first {rendered} rows, pipe-delimited):")
            lines.append(sample)
        sections.append("\n".join(lines))
    if budget is not None and len(results) > 1 and budget <= 0:
        sections.append(f"(row budget of {MAX_ELEMENTS} exhausted; later datasets show statistics only)")
    return "\n\n".join(sections)


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
