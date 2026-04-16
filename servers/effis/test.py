#!/usr/bin/env python3
"""
Local test script for the Fire Detection MCP Server.

Tests the shapefile reading path, the live WFS path, and the
compute_metrics tool (burn mask from dNBR + per-scene masked statistics).

Setup:
    1. Download the EFFIS shapefile:
       wget -O effis.zip "https://maps.effis.emergency.copernicus.eu/effis?service=WFS&request=getfeature&typename=ms:modis.ba.poly&version=1.1.0&outputformat=SHAPEZIP"
       unzip effis.zip -d effis_layer/

    2. pip install pyshp httpx "mcp[cli]>=1.2.0" matplotlib Pillow numpy

    3. python test.py /path/to/effis_layer

Usage:
    python test.py /Users/antoniolopez/Downloads/effis_layer
    python test.py /Users/antoniolopez/Downloads/effis_layer --date 2023-08-22
    python test.py /Users/antoniolopez/Downloads/effis_layer --date 2025-07-29 --bbox "-9.3,36.0,3.3,43.8"
    python test.py --wfs-only --date today          # test live WFS (no shapefile)
    python test.py --wfs-only --date week            # test live WFS
    python test.py --metrics --date 2021-08-05 --bbox "22.8,37.9,24.2,39.1"
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path


def test_shapefile_direct(shapefile_dir: str, date: str, bbox: str, max_features: int):
    """Test the shapefile reader directly (no MCP, no async)."""
    print("=" * 60)
    print("TEST: Direct shapefile read")
    print(f"  shapefile_dir: {shapefile_dir}")
    print(f"  date:          {date}")
    print(f"  bbox:          {bbox}")
    print(f"  max_features:  {max_features}")
    print("=" * 60)

    # Import the reader from server.py
    sys.path.insert(0, str(Path(__file__).parent))
    from server import _read_burnt_areas_from_shapefile

    t0 = time.time()
    geojson = _read_burnt_areas_from_shapefile(
        shapefile_dir=shapefile_dir,
        target_date=date,
        bbox=bbox,
        max_features=max_features,
    )
    elapsed = time.time() - t0

    features = geojson.get("features", [])
    print(f"\n✅ Done in {elapsed:.2f}s")
    print(f"   Features found: {len(features)}")

    if features:
        print("\n   First 5 features:")
        for i, f in enumerate(features[:5]):
            props = f.get("properties", {})
            print(f"   [{i}] {props.get('FIREDATE', '?')} | "
                  f"{props.get('COUNTRY', '?')} | "
                  f"{props.get('COMMUNE', '?')} | "
                  f"{props.get('AREA_HA', '?')} ha")

        # Summary stats
        areas = []
        for f in features:
            val = f["properties"].get("AREA_HA", 0)
            try:
                areas.append(float(val))
            except (ValueError, TypeError):
                pass
        if areas:
            print("\n   Area stats:")
            print(f"     Total:   {sum(areas):.0f} ha")
            print(f"     Largest: {max(areas):.0f} ha")
            print(f"     Count:   {len(areas)} fires")

        countries = {}
        for f in features:
            c = f["properties"].get("COUNTRY", "?")
            countries[c] = countries.get(c, 0) + 1
        print(f"\n   By country: {dict(sorted(countries.items(), key=lambda x: -x[1]))}")

        # Check geometry
        has_geom = sum(1 for f in features if f.get("geometry"))
        print(f"\n   Features with geometry: {has_geom}/{len(features)}")
    else:
        print("   ⚠ No features found for this date/bbox combination")

    return geojson


async def test_mcp_tool(shapefile_dir: str | None, date: str | None,
                        bbox: str, max_features: int):
    """Test the full MCP tool (async, includes WMS image download)."""
    print("=" * 60)
    print("TEST: Full MCP tool (get_effis_burnt_areas)")
    print(f"  shapefile_dir: {shapefile_dir or 'None (WFS mode)'}")
    print(f"  date:          {date}")
    print(f"  bbox:          {bbox}")
    print(f"  max_features:  {max_features}")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent))
    from server import get_effis_burnt_areas

    t0 = time.time()
    result_json = await get_effis_burnt_areas(
        bbox=bbox,
        max_features=max_features,
        date=date,
        shapefile_dir=shapefile_dir,
    )
    elapsed = time.time() - t0

    result = json.loads(result_json)

    print(f"\n✅ Done in {elapsed:.2f}s")
    print(f"   Data source:    {result.get('data_source', 'wfs')}")
    print(f"   Date filter:    {result.get('date_filter', '?')}")
    print(f"   Total features: {result.get('total_features', '?')}")

    if "scan_info" in result:
        si = result["scan_info"]
        print(f"   Scan info:      {si['batches']} batches of {si['batch_size']} "
              f"({si['total_scanned']} scanned, {si['days_ago']}d ago)")

    if "saved_geojson" in result:
        print(f"   Saved GeoJSON:  {result['saved_geojson']}")

    if "map_image" in result:
        img = result["map_image"]
        if "saved_path" in img:
            print(f"   Saved map PNG:  {img['saved_path']}")
        elif "error" in img:
            print(f"   Map image:      ⚠ {img['error']}")

    if "wms_url" in result:
        print(f"   WMS URL:        {result['wms_url'][:100]}...")

    if "geojson_error" in result:
        print(f"   ❌ GeoJSON error: {result['geojson_error']}")

    # Print feature summary
    geojson = result.get("geojson", {})
    features = geojson.get("features", [])
    if features:
        print("\n   First 5 features:")
        for i, f in enumerate(features[:5]):
            props = f.get("properties", {})
            print(f"   [{i}] {props.get('FIREDATE', '?')} | "
                  f"{props.get('COUNTRY', '?')} | "
                  f"{props.get('COMMUNE', '?')} | "
                  f"{props.get('AREA_HA', '?')} ha")

    return result


async def test_compute_metrics(
    bbox: str,
    fire_date: str,
    months_before: int,
    months_after: int,
    max_cloud_cover: float,
    metrics: list[str],
    burn_threshold: float,
    width: int,
    height: int,
    min_mask_coverage: float,
    save_dir: str | None,
):
    """Test the compute_metrics tool (burn mask + per-scene masked stats)."""
    print("=" * 60)
    print("TEST: compute_metrics (burn mask pipeline: dNBR)")
    print(f"  fire_date:       {fire_date}")
    print(f"  bbox:            {bbox}")
    print(f"  months_before:   {months_before}")
    print(f"  months_after:    {months_after}")
    print(f"  max_cloud_cover: {max_cloud_cover}%")
    print(f"  metrics:         {metrics}")
    print(f"  burn_threshold:  {burn_threshold}")
    print(f"  image_size:      {width}x{height}")
    print(f"  min_mask_cov:    {min_mask_coverage:.0%}")
    if save_dir:
        print(f"  save_dir:        {save_dir}")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent))
    from server import compute_metrics

    t0 = time.time()
    result_json = await compute_metrics(
        bbox=bbox,
        fire_date=fire_date,
        months_before=months_before,
        months_after=months_after,
        metrics=metrics,
        max_cloud_cover=max_cloud_cover,
        width=width,
        height=height,
        min_mask_coverage=min_mask_coverage,
        burn_threshold=burn_threshold,
        save_dir=save_dir,
    )
    elapsed = time.time() - t0
    result = json.loads(result_json)

    if "error" in result:
        print(f"\n❌ Error: {result['error']}")
        return result

    print(f"\n✅ Done in {elapsed:.1f}s")

    # Burn mask info
    bm = result.get("burn_mask", {})
    if "error" in bm:
        print(f"   Burn mask:      ❌ {bm['error']}")
    else:
        print(f"   Burn mask:      {bm.get('burned_pixels', '?')}"
              f"/{bm.get('total_pixels', '?')} pixels"
              f"  ({100 * bm.get('burn_fraction', 0):.1f}% burned)")
        if bm.get("mask_path"):
            print(f"     mask PNG:     {bm['mask_path']}")
        if bm.get("dnbr_path"):
            print(f"     dNBR PNG:     {bm['dnbr_path']}")
        if bm.get("warning"):
            print(f"     ⚠ {bm['warning']}")

    # Summary
    summary = result.get("summary", {})
    print("\n   Observations:")
    print(f"     pre-fire:     {summary.get('pre_fire_observations', '?')}")
    print(f"     post-fire:    {summary.get('post_fire_observations', '?')}")

    for m in metrics:
        pre_key = f"pre_fire_mean_{m}"
        post_key = f"post_fire_mean_{m}"
        change_key = f"{m}_change"
        pre_val = summary.get(pre_key)
        post_val = summary.get(post_key)
        change_val = summary.get(change_key)
        print(f"\n   {m.upper()}:")
        print(f"     pre-fire mean:  {pre_val}")
        print(f"     post-fire mean: {post_val}")
        print(f"     change:         {change_val}")

    print(f"\n   Images downloaded: {summary.get('images_downloaded', '?')}")

    # Coverage filter
    filt = summary.get("mask_coverage_filter")
    if filt:
        print(f"   Coverage filter:  {filt.get('observations_discarded', 0)} discarded"
              f" / {filt.get('total_observations_before_filter', 0)} total"
              f"  (min {filt.get('min_mask_coverage', 0):.0%})")

    # Burn mask summary
    bm_summary = summary.get("burn_mask")
    if bm_summary:
        print(f"   Burn mask:        threshold={bm_summary.get('threshold')}"
              f"  fraction={bm_summary.get('burn_fraction')}")

    # Plots
    plots = result.get("plots", {})
    if isinstance(plots, dict) and "error" not in plots:
        for m, path in plots.items():
            print(f"   Plot {m}: {path}")
    elif isinstance(plots, dict) and "error" in plots:
        print(f"   Plots: ⚠ {plots['error']}")

    # Recovery / VRR
    recovery = result.get("recovery")
    if recovery:
        if "error" in recovery:
            print(f"\n   Recovery:     ⚠ {recovery['error']}")
        else:
            table = recovery.get("table", [])
            steps = recovery.get("time_steps", [])
            print("\n   Recovery (VRR):")
            print(f"     Time steps: {steps}")
            for row in table:
                ts = row.get("Time step", "?")
                ndvi = row.get("NDVI", "?")
                vrr = row.get("VRR (%)", "?")
                print(f"     {ts}: NDVI={ndvi}  VRR={vrr}%")
            if recovery.get("plot_path"):
                print(f"     Plot: {recovery['plot_path']}")
            if recovery.get("csv_path"):
                print(f"     CSV:  {recovery['csv_path']}")

    print(f"\n   Saved JSON: {result.get('saved_json', '?')}")
    print(f"   Save dir:   {result.get('save_dir', '?')}")

    return result


def test_shapefile_fields(shapefile_dir: str):
    """Print shapefile metadata: fields, record count, date range."""
    print("=" * 60)
    print("TEST: Shapefile metadata")
    print(f"  shapefile_dir: {shapefile_dir}")
    print("=" * 60)

    import shapefile
    shp_path = Path(shapefile_dir)
    shp_files = list(shp_path.glob("*.shp"))
    if not shp_files:
        print("  ❌ No .shp file found!")
        return

    sf = shapefile.Reader(str(shp_files[0]))
    fields = sf.fields[1:]  # skip DeletionFlag

    print(f"\n   Shapefile: {shp_files[0].name}")
    print(f"   Records:   {len(sf)}")
    print(f"\n   Fields ({len(fields)}):")
    for f in fields:
        print(f"     {f[0]:20s} type={f[1]} size={f[2]} decimal={f[3]}")

    # Sample first and last records for date range
    first_rec = sf.record(0)
    last_rec = sf.record(len(sf) - 1)
    field_names = [f[0] for f in fields]
    if "FIREDATE" in field_names:
        fd_idx = field_names.index("FIREDATE")
        print(f"\n   First record FIREDATE: {first_rec[fd_idx]}")
        print(f"   Last record FIREDATE:  {last_rec[fd_idx]}")

    # Sample 5 random records
    import random
    print("\n   5 random samples:")
    indices = random.sample(range(len(sf)), min(5, len(sf)))
    for idx in sorted(indices):
        rec = sf.record(idx)
        if "FIREDATE" in field_names:
            fd = rec[fd_idx]
            country_idx = field_names.index("COUNTRY") if "COUNTRY" in field_names else None
            area_idx = field_names.index("AREA_HA") if "AREA_HA" in field_names else None
            country = rec[country_idx] if country_idx is not None else "?"
            area = rec[area_idx] if area_idx is not None else "?"
            print(f"     [{idx:6d}] {fd} | {country} | {area} ha")


def test_build_recovery_table():
    """Unit test for build_recovery_table (VRR formula, no CDSE needed)."""
    import numpy as np

    print("=" * 60)
    print("TEST: build_recovery_table (VRR — Lin et al. 2005)")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent))
    from server import _compute_severity_map, build_recovery_table

    H, W = 20, 20
    bands = {"red": 0, "nir": 1}

    def _make_img(red_val: float, nir_val: float) -> np.ndarray:
        img = np.zeros((2, H, W), dtype=np.float32)
        img[0] = red_val
        img[1] = nir_val
        return img

    img_pre = _make_img(0.05, 0.45)   # NDVI = 0.8
    img_dist = _make_img(0.20, 0.30)  # NDVI = 0.2
    img_half = _make_img(0.10, 0.40)  # NDVI = 0.6 → VRR = (0.6-0.2)/(0.8-0.2)*100 = 66.67%
    img_full = _make_img(0.05, 0.45)  # NDVI = 0.8 → VRR = 100%
    img_over = _make_img(0.03, 0.47)  # NDVI = 0.88 → VRR > 100%

    burn_mask = np.ones((H, W), dtype=bool)

    pre_nbr = np.full((H, W), 0.5, dtype=np.float32)
    post_nbr = np.full((H, W), 0.1, dtype=np.float32)
    severity_map = _compute_severity_map(pre_nbr, post_nbr)

    imgs_post = {
        "T+12mo": img_half,
        "T+24mo": img_full,
        "T+36mo": img_over,
    }

    df = build_recovery_table(img_pre, img_dist, imgs_post, burn_mask, bands, severity_map)

    print(f"\n  Recovery table ({len(df)} rows):")
    print(df.to_string(index=False))

    # Validate VRR values
    vrr_12 = df.loc[df["Time step"] == "T+12mo", "VRR (%)"].values[0]
    vrr_24 = df.loc[df["Time step"] == "T+24mo", "VRR (%)"].values[0]
    vrr_36 = df.loc[df["Time step"] == "T+36mo", "VRR (%)"].values[0]

    assert 60 < vrr_12 < 70, f"Expected VRR ~66.67%, got {vrr_12}"
    assert 99 < vrr_24 < 101, f"Expected VRR ~100%, got {vrr_24}"
    assert vrr_36 > 100, f"Expected VRR >100%, got {vrr_36}"

    # Check classification columns exist
    assert "% Very poor" in df.columns, "Missing VRR class column"
    assert "% Excellent" in df.columns, "Missing VRR class column"

    # T+12mo should be 100% Good (VRR ~66.67% falls in 50-75% = Good)
    pct_good_12 = df.loc[df["Time step"] == "T+12mo", "% Good"].values[0]
    assert pct_good_12 == 100.0, f"Expected 100% Good at T+12mo, got {pct_good_12}"

    # T+24mo: VRR=100% falls into Excellent (bins are [lo, hi), so 100.0 >= 100.0)
    pct_exc_24 = df.loc[df["Time step"] == "T+24mo", "% Excellent"].values[0]
    assert pct_exc_24 == 100.0, f"Expected 100% Excellent at T+24mo (VRR=100), got {pct_exc_24}"

    # T+36mo should be 100% Excellent (VRR > 100%)
    pct_exc_36 = df.loc[df["Time step"] == "T+36mo", "% Excellent"].values[0]
    assert pct_exc_36 == 100.0, f"Expected 100% Excellent at T+36mo, got {pct_exc_36}"

    print("\n  VRR formula validation:")
    print(f"    T+12mo: VRR={vrr_12:.2f}% (expected ~66.67%) ✅")
    print(f"    T+24mo: VRR={vrr_24:.2f}% (expected 100%)    ✅")
    print(f"    T+36mo: VRR={vrr_36:.2f}% (expected >100%)   ✅")
    print("    Classification columns present               ✅")
    print("    Class assignment correct                     ✅")

    # Test guard condition: when NDVI_pre ≈ NDVI_dist (denom → 0)
    img_same = _make_img(0.20, 0.30)  # same as disturbance
    df_guard = build_recovery_table(
        img_same, img_dist, {"T+12mo": img_half},
        burn_mask, bands, severity_map,
    )
    vrr_guard = df_guard.loc[0, "VRR (%)"]
    print(f"\n  Guard test (NDVI_pre ≈ NDVI_dist): VRR={vrr_guard}")
    # Should be NaN (denom → 0) — nanmean of all-NaN = NaN
    assert np.isnan(vrr_guard) or vrr_guard != vrr_guard, "Expected NaN when denom ≈ 0"
    print("    Denominator guard (NaN) works                ✅")

    print("\n✅ All build_recovery_table tests passed!")


def test_severity_map():
    """Unit test for _compute_severity_map."""
    import numpy as np

    print("=" * 60)
    print("TEST: _compute_severity_map (Key & Benson 2006)")
    print("=" * 60)

    sys.path.insert(0, str(Path(__file__).parent))
    from server import _compute_severity_map

    pre = np.array([0.05, 0.20, 0.40, 0.60, 0.80], dtype=np.float32)
    post = np.array([0.05, 0.05, 0.05, 0.05, 0.05], dtype=np.float32)
    # dNBR:           0.00  0.15  0.35  0.55  0.75
    # Expected:        0     1     2     3     4

    sev = _compute_severity_map(pre, post)
    expected = [0, 1, 2, 3, 4]

    print(f"\n  dNBR values:    {(pre - post).tolist()}")
    print(f"  Severity map:   {sev.tolist()}")
    print(f"  Expected:       {expected}")

    assert sev.tolist() == expected, f"Severity mismatch: {sev.tolist()} != {expected}"
    print("\n✅ _compute_severity_map test passed!")


def main():
    parser = argparse.ArgumentParser(
        description="Test Fire Detection MCP Server locally",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Inspect shapefile metadata
  python test.py /path/to/effis_layer --info

  # Test shapefile reader for a specific date
  python test.py /path/to/effis_layer --date 2023-08-22

  # Test with bbox filter (Greece)
  python test.py /path/to/effis_layer --date 2023-08-22 --bbox "19.4,34.8,29.6,41.8"

  # Test full MCP tool with shapefile
  python test.py /path/to/effis_layer --date 2023-08-22 --full

  # Test live WFS (no shapefile needed)
  python test.py --wfs-only --date today --full
  python test.py --wfs-only --date week --full

  # Test compute_metrics (burn mask + masked stats, requires CDSE creds)
  python test.py --metrics --date 2021-08-05 --bbox "22.8,37.9,24.2,39.1"
  python test.py --metrics --date 2021-08-05 --bbox "22.8,37.9,24.2,39.1" --months-before 2 --months-after 2
  python test.py --metrics --date 2021-08-05 --bbox "22.8,37.9,24.2,39.1" --burn-threshold 0.2 --metric-names ndvi

  # Test VRR recovery logic (offline, no CDSE creds needed)
  python test.py --unit-tests

  # Test compute_metrics WITH regrowth (live, requires CDSE creds)
  python test.py --metrics --date 2018-07-23 --bbox "23.6,37.9,24.1,38.15" --metric-names ndvi regrowth
        """,
    )
    parser.add_argument("shapefile_dir", nargs="?", default=None,
                        help="Path to directory with modis.ba.poly.shp")
    parser.add_argument("--date", "-d", default="2023-08-22",
                        help="Date to filter (default: 2023-08-22)")
    parser.add_argument("--bbox", "-b", default="-18,27,42,72",
                        help='Bounding box "west,south,east,north" (default: Europe)')
    parser.add_argument("--max-features", "-n", type=int, default=50,
                        help="Max features to return (default: 50)")
    parser.add_argument("--info", action="store_true",
                        help="Print shapefile metadata and exit")
    parser.add_argument("--full", action="store_true",
                        help="Test the full MCP tool (async, includes WMS download)")
    parser.add_argument("--wfs-only", action="store_true",
                        help="Skip shapefile, test WFS path only")

    # unit tests (offline)
    parser.add_argument("--unit-tests", action="store_true",
                        help="Run offline unit tests (VRR, severity map). "
                             "No CDSE credentials needed.")

    # compute_metrics options
    parser.add_argument("--metrics", action="store_true",
                        help="Test compute_metrics (burn mask + masked stats, "
                             "requires CDSE credentials)")
    parser.add_argument("--months-before", type=int, default=3,
                        help="Months before fire for --metrics (default: 3)")
    parser.add_argument("--months-after", type=int, default=3,
                        help="Months after fire for --metrics (default: 3)")
    parser.add_argument("--max-cloud-cover", type=float, default=20.0,
                        help="Max cloud cover %% for --metrics (default: 20)")
    parser.add_argument("--metric-names", nargs="+",
                        default=["ndvi", "nbr", "bais2", "regrowth"],
                        help="Indices to compute (default: ndvi nbr bais2 regrowth)")
    parser.add_argument("--burn-threshold", type=float, default=0.15,
                        help="dNBR threshold for burn mask (default: 0.15)")
    parser.add_argument("--width", type=int, default=512,
                        help="Image width in pixels (default: 512)")
    parser.add_argument("--height", type=int, default=512,
                        help="Image height in pixels (default: 512)")
    parser.add_argument("--min-mask-coverage", type=float, default=0.8,
                        help="Min fraction of mask pixels with valid data (0-1, default: 0.8)")
    parser.add_argument("--save-dir", default=None,
                        help="Output directory (default: ~/fire_maps/)")

    args = parser.parse_args()

    if args.unit_tests:
        test_severity_map()
        print()
        test_build_recovery_table()
        return

    if args.metrics:
        asyncio.run(test_compute_metrics(
            bbox=args.bbox,
            fire_date=args.date,
            months_before=args.months_before,
            months_after=args.months_after,
            max_cloud_cover=args.max_cloud_cover,
            metrics=args.metric_names,
            burn_threshold=args.burn_threshold,
            width=args.width,
            height=args.height,
            min_mask_coverage=args.min_mask_coverage,
            save_dir=args.save_dir,
        ))
        return

    if not args.shapefile_dir and not args.wfs_only:
        parser.print_help()
        print("\n❌ Provide a shapefile_dir, use --wfs-only, or use --metrics")
        sys.exit(1)

    if args.info:
        if not args.shapefile_dir:
            print("❌ --info requires a shapefile_dir")
            sys.exit(1)
        test_shapefile_fields(args.shapefile_dir)
        return

    if args.full:
        shp = None if args.wfs_only else args.shapefile_dir
        asyncio.run(test_mcp_tool(
            shapefile_dir=shp,
            date=args.date,
            bbox=args.bbox,
            max_features=args.max_features,
        ))
    else:
        if args.wfs_only:
            print("❌ --wfs-only requires --full (WFS is only in the async tool)")
            sys.exit(1)
        test_shapefile_direct(
            shapefile_dir=args.shapefile_dir,
            date=args.date,
            bbox=args.bbox,
            max_features=args.max_features,
        )


if __name__ == "__main__":
    main()
