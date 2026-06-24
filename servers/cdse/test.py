import asyncio
import argparse
import base64
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

load_dotenv()

OUT_DIR = Path(__file__).parent / "test_outputs"
OUT_DIR.mkdir(exist_ok=True)

SERVER = "server.py"

# bbox: [west, south, east, north]
ROME_BBOX      = [12.40, 41.85, 12.55, 41.95]
ALPS_BBOX      = [10.50, 46.00, 11.50, 46.80]
NORTH_SEA_BBOX = [3.00,  53.00,  5.00, 54.50]
PO_VALLEY_BBOX = [9.00,  44.50, 12.00, 45.50]
NEW_BOX = [8.3333, 41.3149,9.7009, 43.0568]
NEW_ALPS = [5.361328, 43.98491, 8.250732, 46.498392]

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg: str)   -> str: return f"{GREEN}✔  {msg}{RESET}"
def err(msg: str)  -> str: return f"{RED}✘  {msg}{RESET}"
def info(msg: str) -> str: return f"{CYAN}▸  {msg}{RESET}"
def head(msg: str) -> str: return f"\n{BOLD}{YELLOW}{msg}{RESET}"



def _save_result(result, name: str) -> bool:
    """Print text blocks and save any image block; print coloured status.
    Returns True if an image was saved, False otherwise."""
    saved = False
    for block in result.content:
        if block.type == "text":
            text = block.text.strip()
            if text:
                print(f"    {text}")
        elif block.type == "image":
            img_bytes = base64.b64decode(block.data)
            out_path = OUT_DIR / f"{name}.jpg"
            out_path.write_bytes(img_bytes)
            print(ok(f"image saved → {out_path}  ({len(img_bytes):,} bytes)"))
            saved = True

    if not saved:
        print(err("no image returned"))

    return saved  # ← ADD THIS

# ── Test registry ─────────────────────────────────────────────────────────────
# Each entry: (key, label, tool_name, kwargs)
TESTS: list[tuple[str, str, str, dict]] = [
    # Sentinel-1 GRD
    ("s1_vv_vh_rgb", "S1 pseudo-RGB VV+VH (Alps, IW ascending)", "fetch_sentinel1_grd", {
        "bbox": ALPS_BBOX, "start_date": "2024-03-01", "end_date": "2024-03-31",
        "preset": "vv_vh_rgb", "acquisition_mode": "IW", "orbit_direction": "ASCENDING",
        "width": 512, "height": 512,
    }),
    ("s1_vv_single", "S1 custom bands VV greyscale dB (Alps)", "fetch_sentinel1_grd", {
        "bbox": ALPS_BBOX, "start_date": "2024-03-01", "end_date": "2024-03-31",
        "custom_bands": ["VV"], "width": 512, "height": 512,
    }),
    ("s1_custom_bands", "S1 custom bands VV+VH (Rome)", "fetch_sentinel1_grd", {
        "bbox": ROME_BBOX, "start_date": "2024-02-01", "end_date": "2024-02-28",
        "custom_bands": ["VV", "VH"], "width": 512, "height": 512,
    }),

    # Sentinel-2 L1C
    ("s2_l1c_true_color", "S2 L1C true colour (Rome, cloud ≤20%)", "fetch_sentinel2", {
        "bbox": ROME_BBOX, "start_date": "2024-04-01", "end_date": "2024-06-30", "collection":"L1C",
        "preset": "true_color", "max_cloud_cover": 20.0, "width": 512, "height": 512,
    }),
    ("s2_l1c_custom_nir", "S2 L1C NIR false colour B08/B04/B03 (Rome)", "fetch_sentinel2", {
        "bbox": ROME_BBOX, "start_date": "2024-04-01", "end_date": "2024-06-30", "collection":"L1C",
        "custom_bands": ["B08", "B04", "B03"], "max_cloud_cover": 30.0,
        "width": 512, "height": 512,
    }),
    ("s2_l1c_ndvi", "S2 L1C true colour (Rome, cloud ≤20%)", "fetch_sentinel2", {
        "bbox": ROME_BBOX, "start_date": "2024-04-01", "end_date": "2024-06-30", "collection":"L1C",
        "preset": "ndvi", "max_cloud_cover": 20.0, "width": 512, "height": 512,
    }),

    # Sentinel-2 L2A
    ("s2_l2a_true_color", "S2 L2A true colour (Rome, cloud ≤20%)", "fetch_sentinel2", {
        "bbox": ROME_BBOX, "start_date": "2024-04-01", "end_date": "2024-06-30", "collection":"L2A",
        "preset": "true_color", "max_cloud_cover": 20.0, "width": 512, "height": 512,
    }),
    ("s2_l2a_swir", "S2 L2A SWIR B12/B8A/B04 (Alps)", "fetch_sentinel2", {
        "bbox": ALPS_BBOX, "start_date": "2024-03-01", "end_date": "2024-05-31", "collection":"L2A",
        "custom_bands": ["B12", "B8A", "B04"], "max_cloud_cover": 30.0,
        "width": 512, "height": 512,
    }),
    ("s2_l2a_ndvi", "S2 L2A true colour (Rome, cloud ≤20%)", "fetch_sentinel2", {
        "bbox": ROME_BBOX, "start_date": "2024-04-01", "end_date": "2024-06-30", "collection":"L2A",
        "preset": "ndvi", "max_cloud_cover": 20.0, "width": 512, "height": 512,
    }),

    # Sentinel-3 OLCI
    ("s3_olci_true_color", "S3 OLCI true colour (Corsica)", "fetch_sentinel3_olci", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "preset": "true_color", "width": 512, "height": 512,
    }),
    ("s3_olci_custom_nir", "S3 OLCI NIR false colour B17/B08/B04 (Corsica)", "fetch_sentinel3_olci", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "custom_bands": ["B17", "B08", "B04"], "width": 512, "height": 512,
    }),
    ("s3_olci_otci", "S3 OLCI OTCI (Corsica)", "fetch_sentinel3_olci", {
        "bbox": NEW_BOX, "start_date": "2020-04-04", "end_date": "2020-04-05",
        "preset": "otci", "width": 512, "height": 512,
    }),
    # Sentinel-3 SLSTR
    ("s3_slstr_false_color", "S3 SLSTR approx false colour", "fetch_sentinel3_slstr", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "preset": "false_color", "width": 512, "height": 512,
    }),
    ("s3_slstr_ndvi", "S3 SLSTR approx NDVI", "fetch_sentinel3_slstr", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "preset": "ndvi", "width": 512, "height": 512,
    }),
    ("s3_slstr_custom_swir", "S3 SLSTR SWIR S5/S3/S1", "fetch_sentinel3_slstr", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "custom_bands": ["S5", "S3", "S1"], "width": 512, "height": 512,
    }),

    # Sentinel-3 SYN L2
    ("s3_syn_true_color", "S3 SYN L2 true colour (North Sea)", "fetch_sentinel3_syn_l2", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "preset": "true_color", "width": 512, "height": 512,
    }),
    ("s3_syn_ndvi", "S3 SYN L2 ndvi (North Sea)", "fetch_sentinel3_syn_l2", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "preset": "ndvi", "width": 512, "height": 512,
    }),
    ("s3_syn_custom", "S3 SYN L2 custom (Corsica)", "fetch_sentinel3_syn_l2", {
        "bbox": NEW_BOX, "start_date": "2024-05-01", "end_date": "2024-05-31",
        "custom_bands": ["B17", "B08", "B04"], "width": 512, "height": 512,
    }),

    # Sentinel-5P L2
    ("s5p_no2", "S5P NO₂ tropospheric column (Po Valley)", "fetch_sentinel5p_l2", {
        "bbox": PO_VALLEY_BBOX, "start_date": "2024-01-01", "end_date": "2024-03-31",
        "preset": "no2", "width": 512, "height": 512,
    }),
    ("s5p_co", "S5P CO total column (Po Valley)", "fetch_sentinel5p_l2", {
        "bbox": PO_VALLEY_BBOX, "start_date": "2024-01-01", "end_date": "2024-03-31",
        "preset": "co", "width": 512, "height": 512,
    }),

    # Landsat OT L1
    ("landsat_true_color", "Landsat true colour (Rome, cloud ≤30%)", "fetch_landsat_ot_l1", {
        "bbox": ROME_BBOX, "start_date": "2024-03-01", "end_date": "2024-06-30",
        "preset": "true_color", "max_cloud_cover": 30.0, "width": 512, "height": 512,
    }),
    ("landsat_custom_swir", "Landsat SWIR B07/B05/B04 (Alps)", "fetch_landsat_ot_l1", {
        "bbox": ALPS_BBOX, "start_date": "2024-03-01", "end_date": "2024-06-30",
        "custom_bands": ["B07", "B05", "B04"], "max_cloud_cover": 40.0,
        "width": 512, "height": 512,
    }),

    # DEM
    ("dem_grey", "DEM greyscale elevation (Alps, Copernicus 30 m)", "fetch_dem", {
        "bbox": ALPS_BBOX, "preset": "elevation_grey",
        "dem_instance": "COPERNICUS_30", "width": 512, "height": 512,
    }),
    ("dem_color", "DEM colour ramp elevation (Alps)", "fetch_dem", {
        "bbox": ALPS_BBOX, "preset": "elevation_color",
        "dem_instance": "COPERNICUS_30", "width": 512, "height": 512,
    }),
]

ALL_KEYS = [t[0] for t in TESTS]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run MCP satellite-image tests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python test_mcp.py                          # run all tests\n"
            "  python test_mcp.py --list                   # list available tests\n"
            "  python test_mcp.py s1_vv_vh_rgb dem_grey    # run specific tests\n"
            "  python test_mcp.py --group s1               # run all s1_* tests\n"
            "  python test_mcp.py --group s2 dem           # run s2_* and dem_* tests\n"
        ),
    )
    p.add_argument(
        "tests",
        nargs="*",
        metavar="TEST_KEY",
        help="Specific test keys to run (default: all). See --list.",
    )
    p.add_argument(
        "--group", "-g",
        nargs="+",
        metavar="PREFIX",
        help="Run all tests whose key starts with PREFIX (e.g. s1, s2, dem).",
    )
    p.add_argument(
        "--list", "-l",
        action="store_true",
        help="Print all available test keys and exit.",
    )
    return p


def resolve_tests(args: argparse.Namespace) -> list[tuple]:
    if args.list:
        print(head("Available tests"))
        for key, label, tool, _ in TESTS:
            print(f"  {CYAN}{key:<30}{RESET}  {label}")
        sys.exit(0)

    selected_keys: set[str] = set()

    if args.tests:
        for k in args.tests:
            if k not in ALL_KEYS:
                print(err(f"Unknown test key: '{k}'  (run --list to see valid keys)"))
                sys.exit(1)
            selected_keys.add(k)

    if args.group:
        for prefix in args.group:
            matched = [k for k in ALL_KEYS if k.startswith(prefix)]
            if not matched:
                print(err(f"No tests match prefix '{prefix}'"))
                sys.exit(1)
            selected_keys.update(matched)

    # Default: all tests
    if not selected_keys:
        return TESTS

    # Preserve original order
    return [t for t in TESTS if t[0] in selected_keys]


async def main():
    parser = build_parser()
    args = parser.parse_args()
    selected = resolve_tests(args)

    print(head(f"Running {len(selected)} test(s)  →  output: {OUT_DIR}/"))

    params = StdioServerParameters(
        command="python",
        args=[SERVER, "--transport", "stdio"],
        env={
            **os.environ,
            "CDSE_CLIENT_ID":     os.environ["CDSE_CLIENT_ID"],
            "CDSE_CLIENT_SECRET": os.environ["CDSE_CLIENT_SECRET"],
        },
    )

    passed = failed = 0

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()

            tools = await session.list_tools()
            available_tools = {t.name for t in tools.tools}
            print(info(f"Available tools: {', '.join(sorted(available_tools))}"))

            for key, label, tool_name, kwargs in selected:
                print(head(f"[{key}]  {label}"))

                if tool_name not in available_tools:
                    print(err(f"Tool '{tool_name}' not available — skipping"))
                    failed += 1
                    continue

                t0 = time.perf_counter()
                try:
                    result = await session.call_tool(tool_name, kwargs)
                    # print(result)
                    elapsed = time.perf_counter() - t0
                    print(info(f"completed in {elapsed:.1f}s"))
                    # _save_result(result, key)
                    if _save_result(result, key):  # ← was: _save_result(...); passed += 1
                        passed += 1
                    else:
                        failed += 1

                except Exception as exc:
                    elapsed = time.perf_counter() - t0
                    print(err(f"FAILED after {elapsed:.1f}s: {exc}"))
                    failed += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    print(head("Summary"))
    print(ok(f"{passed}/{total} passed") if failed == 0 else f"  {ok(f'{passed}/{total} passed')}   {err(f'{failed}/{total} failed')}")
    print()


asyncio.run(main())