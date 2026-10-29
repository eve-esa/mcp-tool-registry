"""Unit tests for the offline parts of sources.py: grid coarsening and the BOA offset rule."""

import rasterio
import sources


def test_coarsened_grid_keeps_the_extent():
    grid = sources.Grid("EPSG:32630", rasterio.Affine(40, 0, 600000, 0, -40, 4800000), 871, 835, 40.0)
    display = grid.coarsened(520)
    assert display.pixel_m == 80.0 and display.shape == (418, 436)
    assert display.transform * (0, 0) == grid.transform * (0, 0)
    assert grid.coarsened(5000).shape == grid.shape  # never finer than the source


def test_offset_skipped_when_catalogue_says_it_is_applied():
    """Scenes flagged boa_offset_applied must not get the -0.1 offset a second time."""
    asset = {"href": "unused", "raster:bands": [{"scale": 0.0001, "offset": -0.1}]}
    item = {"id": "x", "properties": {"earthsearch:boa_offset_applied": True}, "assets": {"red": asset}}
    assert sources._scale_offset(item, "red") == (0.0001, 0.0)


def test_offset_kept_for_old_products_without_it():
    """Products before 2022 have offset 0 in the catalogue: nothing to decide, no read needed."""
    asset = {"href": "unused", "raster:bands": [{"scale": 0.0001, "offset": 0}]}
    item = {"id": "y", "properties": {}, "assets": {"red": asset}}
    assert sources._scale_offset(item, "red") == (0.0001, 0)
