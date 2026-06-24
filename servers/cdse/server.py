"""
MCP server exposing each Sentinel Hub collection as a separate tool.

Run with:
    python server.py

Or via stdio transport (for use in MCP clients / Claude Desktop):
    {
        "command": "python",
        "args": ["/path/to/server.py"],
        "env": {
            "CDSE_CLIENT_ID": "...",
            "CDSE_CLIENT_SECRET": "..."
        }
    }

Each tool returns:
  - An ImageContent block containing the JPEG as base64 (for MCP clients
    that can render images)
  - A TextContent block with a JSON metadata summary (saved_path, dims,
    collection, evalscript used)
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from dotenv import load_dotenv


from helper import (
    fetch_sentinel1_grd,
    fectch_sentinel2,
    fetch_sentinel3_olci,
    fetch_sentinel3_slstr,
    fetch_sentinel3_syn_l2,
    fetch_sentinel5p_l2,
    fetch_landsat_ot_l1,
    fetch_dem,
)

mcp = FastMCP(
    "sentinel-hub",
    instructions=(
        "Sentinel Hub satellite imagery tools. "
        "Each tool fetches a clipped AOI image for a specific satellite collection. "
        "Always pass bbox as [west, south, east, north] in WGS-84 decimal degrees. "
        "Dates are ISO-8601 strings: YYYY-MM-DD."
    ),
    host="0.0.0.0", port=8000, stateless_http=True
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_content(result: dict) -> list[ImageContent | TextContent]:
    """
    Convert a sentinel_hub fetch result dict into MCP content blocks:
    1. ImageContent  – the JPEG image as base64
    2. TextContent   – JSON metadata (dimensions, collection, saved_path, evalscript)
    """
    image_block = ImageContent(
        type="image",
        data=result["image_b64"],
        mimeType=result["media_type"],
    )
    meta = {
        "collection":  result["collection"],
        "width_px":    result["width"],
        "height_px":   result["height"],
        "saved_path":  result["saved_path"],
        "evalscript":  result["evalscript"],
    }
    text_block = TextContent(
        type="text",
        text=json.dumps(meta, indent=2),
    )
    return [image_block, text_block]


# ---------------------------------------------------------------------------
# Tool: Sentinel-1 GRD
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_sentinel1_grd",
    description=(
        "Fetch a Sentinel-1 GRD SAR image. "
        "Presets: 'vv_vh_rgb' (pseudo-RGB from VV+VH, dual-pol IW mode), "
        "'hh_hv_rgb' (pseudo-RGB from HH+HV, dual-pol EW mode), "
        "Use custom_bands to select any polarisation(s) manually. "
        "back_coeff options: GAMMA0_ELLIPSOID (default), GAMMA0_TERRAIN (RTC), SIGMA0_ELLIPSOID."
    ),
)
def tool_sentinel1_grd(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    start_date: Annotated[str, "Start of date range, YYYY-MM-DD"],
    end_date: Annotated[str, "End of date range, YYYY-MM-DD"],
    preset: Annotated[
        Optional[str],
        "One of: vv_vh_rgb | hh_hv_rgb "
        "Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Explicit polarisation list, e.g. ['VV'] or ['VV','VH']. "
        "Overrides preset.",
    ] = None,
    evalscript: Annotated[
        Optional[str],
        "Raw Sentinel Hub evalscript (VERSION=3 JS). Overrides preset and custom_bands.",
    ] = None,
    orthorectify: Annotated[bool, "Apply geometric terrain correction (default True)"] = True,
    back_coeff: Annotated[
        str,
        "Backscatter coefficient: GAMMA0_ELLIPSOID | GAMMA0_TERRAIN | SIGMA0_ELLIPSOID",
    ] = "GAMMA0_ELLIPSOID",
    dem_instance: Annotated[
        Optional[str],
        "DEM for orthorectification, e.g. 'COPERNICUS_30'",
    ] = None,
    acquisition_mode: Annotated[
        Optional[str],
        "Filter by mode: IW | EW | SM | WV",
    ] = None,
    polarization: Annotated[
        Optional[str],
        "Filter by polarization scheme: DV | DH | SV | SH",
    ] = None,
    orbit_direction: Annotated[
        Optional[str],
        "Filter by orbit: ASCENDING | DESCENDING",
    ] = None,
    resolution: Annotated[Optional[str], "HIGH | MEDIUM"] = None,
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fetch_sentinel1_grd(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        orthorectify=orthorectify,
        back_coeff=back_coeff,
        dem_instance=dem_instance,
        acquisition_mode=acquisition_mode,
        polarization=polarization,
        orbit_direction=orbit_direction,
        resolution=resolution,
        width=width,
        height=height,
        save_path=save_path
    )
    return _to_content(result)


# ---------------------------------------------------------------------------
# Tool: Sentinel-2
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_sentinel2",
    description=(
        "Fetch a Sentinel-2 image at ~10–60 m resolution. "
        "Collection : choose form L2A or L1c, defaults to L2A"
        "Preset: 'true_color' (R=B04, G=B03, B=B02). 'ndvi' (NIR=B08, R=B04) "
        "Use custom_bands for any combination of B01–B12, B8A. "
        "Note: no atmospheric correction — use L2A for surface reflectance."
    ),
)
def tool_sentinel2(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    start_date: Annotated[str, "Start of date range, YYYY-MM-DD"],
    end_date: Annotated[str, "End of date range, YYYY-MM-DD"],
    collection: Annotated[str, "The collection of Sentinel-2 L2A or L1C"],
    preset: Annotated[
        Optional[str],
        "true_color. Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Band list, e.g. ['B08','B04','B03'] for NIR false colour. "
        "Available: B01–B12, B8A.",
    ] = None,
    evalscript: Annotated[Optional[str], "Raw evalscript. Overrides everything."] = None,
    max_cloud_cover: Annotated[
        Optional[float], "Maximum cloud coverage 0–100 (%)"
    ] = None,
    mosaicking_order: Annotated[
        str, "leastCC | mostRecent | leastRecent"
    ] = "leastCC",
    harmonize_values: Annotated[
        bool, "Apply SAFE/BASELINE processing baseline harmonisation"
    ] = False,
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fectch_sentinel2(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        collection=collection,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        max_cloud_cover=max_cloud_cover,
        mosaicking_order=mosaicking_order,
        harmonize_values=harmonize_values,
        width=width,
        height=height,
        save_path=save_path,
    )
    return _to_content(result)




# ---------------------------------------------------------------------------
# Tool: Sentinel-3 OLCI
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_sentinel3_olci",
    description=(
        "Fetch a Sentinel-3 OLCI ocean/land colour image at ~300 m resolution. "
        "21 spectral bands from 400 nm (B01) to 1020 nm (B21). "
        "Preset: 'true_color' (B08 red / B06 green / B04 blue). "
        "Good for large-area overviews, ocean colour, vegetation at continental scale."
    ),
)
def tool_sentinel3_olci(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    start_date: Annotated[str, "Start of date range, YYYY-MM-DD"],
    end_date: Annotated[str, "End of date range, YYYY-MM-DD"],
    preset: Annotated[
        Optional[str],
        "true_color or otci. Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Band list from B01–B21, e.g. ['B17','B08','B04'] for NIR false colour.",
    ] = None,
    evalscript: Annotated[Optional[str], "Raw evalscript. Overrides everything."] = None,
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fetch_sentinel3_olci(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        width=width,
        height=height,
        save_path=save_path,
    )
    return _to_content(result)


# ---------------------------------------------------------------------------
# Tool: Sentinel-3 SLSTR
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_sentinel3_slstr",
    description=(
        "Fetch a Sentinel-3 SLSTR image. "
        "Shortwave bands S1–S6 (0.55–2.25 µm) at 500 m–1 km; "
        "thermal bands S7–S9, F1–F2 (3.7–12 µm). "
        "Preset: 'false_color' (S3n/S2n/S1n approximate visible composite) or ndvi."
        "Append the view suffix to custom band names (e.g. S2n, S2o), "
        "or omit suffix and set view= to apply it automatically."
    ),
)
def tool_sentinel3_slstr(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    start_date: Annotated[str, "Start of date range, YYYY-MM-DD"],
    end_date: Annotated[str, "End of date range, YYYY-MM-DD"],
    preset: Annotated[
        Optional[str],
        "true_color. Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Bands without view suffix, e.g. ['S5','S3','S1']. "
        "The view= parameter appends 'n' or 'o' automatically.",
    ] = None,
    evalscript: Annotated[Optional[str], "Raw evalscript. Overrides everything."] = None,
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fetch_sentinel3_slstr(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        width=width,
        height=height,
        save_path=save_path,
    )
    return _to_content(result)


# ---------------------------------------------------------------------------
# Tool: Sentinel-3 SYN L2
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_sentinel3_syn_l2",
    description=(
        "Fetch a Sentinel-3 SYN L2 (Synergy OLCI+SLSTR combined) image at ~300 m. "
        "Surface reflectance product. "
        "OLCI channels: B01–B12, B16-B18, B21. SLSTR channels: S1–S6. "
        "Preset: 'true_color' (B08 / B06 / B04), NDVI "
    ),
)
def tool_sentinel3_syn_l2(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    start_date: Annotated[str, "Start of date range, YYYY-MM-DD"],
    end_date: Annotated[str, "End of date range, YYYY-MM-DD"],
    preset: Annotated[
        Optional[str],
        "true_color. Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Band list, e.g. ['B17','B08','B04'] for NIR false colour.",
    ] = None,
    evalscript: Annotated[Optional[str], "Raw evalscript. Overrides everything."] = None,
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fetch_sentinel3_syn_l2(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        width=width,
        height=height,
        save_path=save_path,
    )
    return _to_content(result)


# ---------------------------------------------------------------------------
# Tool: Sentinel-5P L2
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_sentinel5p_l2",
    description=(
        "Fetch a Sentinel-5P TROPOMI L2 trace-gas product at ~3.5 × 5.5 km resolution. "
        "Each band is a geophysical quantity (mol/m², DU, etc.), visualised as greyscale. "
        "Presets: no2 | co | o3 | so2 | ch4 | hcho | aer_ai_340_380 | cloud_fraction. "
        "For quantitative retrieval (FLOAT32 values) supply a custom evalscript."
    ),
)
def tool_sentinel5p_l2(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    start_date: Annotated[str, "Start of date range, YYYY-MM-DD"],
    end_date: Annotated[str, "End of date range, YYYY-MM-DD"],
    preset: Annotated[
        Optional[str],
        "no2 | co | o3 | so2 | ch4 | hcho | aer_ai_340_380 | cloud_fraction. "
        "Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Single S5P band name list, e.g. ['NO2'].",
    ] = None,
    evalscript: Annotated[Optional[str], "Raw evalscript. Overrides everything."] = None,
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fetch_sentinel5p_l2(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        width=width,
        height=height,
        save_path=save_path,
    )
    return _to_content(result)


# ---------------------------------------------------------------------------
# Tool: Landsat OT L1
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_landsat_ot_l1",
    description=(
        "Fetch a Landsat 8/9 OLI-TIRS L1 image at ~30 m (15 m panchromatic). "
        "Preset: 'true_color' (R=B04, G=B03, B=B02). "
        "custom_bands: B01 (coastal), B02 (blue), B03 (green), B04 (red), "
        "B05 (NIR), B06 (SWIR-1), B07 (SWIR-2), B08 (pan), "
        "B09 (cirrus), B10/B11 (thermal). "
        "e.g. ['B07','B05','B04'] for SWIR false colour."
    ),
)
def tool_landsat_ot_l1(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    start_date: Annotated[str, "Start of date range, YYYY-MM-DD"],
    end_date: Annotated[str, "End of date range, YYYY-MM-DD"],
    preset: Annotated[
        Optional[str],
        "true_color. Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Band list, e.g. ['B05','B04','B03'] for NIR false colour.",
    ] = None,
    evalscript: Annotated[Optional[str], "Raw evalscript. Overrides everything."] = None,
    max_cloud_cover: Annotated[
        Optional[float], "Maximum cloud coverage 0–100 (%)"
    ] = None,
    mosaicking_order: Annotated[
        str, "leastCC | mostRecent | leastRecent"
    ] = "leastCC",
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fetch_landsat_ot_l1(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        max_cloud_cover=max_cloud_cover,
        mosaicking_order=mosaicking_order,
        width=width,
        height=height,
        save_path=save_path,
    )
    return _to_content(result)


# ---------------------------------------------------------------------------
# Tool: DEM
# ---------------------------------------------------------------------------

@mcp.tool(
    name="fetch_dem",
    description=(
        "Fetch a Digital Elevation Model image. Static product — date range is required "
        "by the API but does not filter scenes. "
        "Presets: 'elevation_grey' (0–8000 m greyscale), "
        "'elevation_color' (blue→green→yellow→red ramp, 0–5000 m). "
        "dem_instance: COPERNICUS_30 (30 m, default) | COPERNICUS_90 (90 m) | MAPZEN."
    ),
)
def tool_dem(
    bbox: Annotated[list[float], "Bounding box [west, south, east, north] in WGS-84"],
    preset: Annotated[
        Optional[str],
        "elevation_grey | elevation_color. Omit if supplying custom_bands or evalscript.",
    ] = None,
    custom_bands: Annotated[
        Optional[list[str]],
        "Typically ['DEM'] for raw elevation values.",
    ] = None,
    evalscript: Annotated[Optional[str], "Raw evalscript. Overrides everything."] = None,
    dem_instance: Annotated[
        str, "COPERNICUS_30 | COPERNICUS_90 | MAPZEN"
    ] = "COPERNICUS_30",
    start_date: Annotated[str, "Ignored by DEM but required by API (YYYY-MM-DD)"] = "2020-01-01",
    end_date: Annotated[str, "Ignored by DEM but required by API (YYYY-MM-DD)"] = "2020-12-31",
    width: Annotated[int, "Output image width in pixels"] = 512,
    height: Annotated[int, "Output image height in pixels"] = 512,
    save_path: Annotated[Optional[str], "Local file path to save the JPEG"] = None,
) -> list[ImageContent | TextContent]:
    result = fetch_dem(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        preset=preset,
        custom_bands=custom_bands,
        evalscript=evalscript,
        dem_instance=dem_instance,
        width=width,
        height=height,
        save_path=save_path,
    )
    return _to_content(result)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # mcp.run()
    import argparse

    parser = argparse.ArgumentParser(description="CDSE MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="Transport type (default: streamable-http)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")