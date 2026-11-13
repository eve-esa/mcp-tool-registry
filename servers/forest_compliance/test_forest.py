"""Offline unit tests for forest.py (no network, no rasters, no LLM).

Covers geometry normalisation and area, Hansen tile lookup, pixel tallies and
the risk assessment on small synthetic grids, and the PNG map encoder.

Run from this directory: python -m pytest test_forest.py -q
"""

import struct
import zlib

import forest
import numpy as np
import pytest

SQUARE_1KM = {  # ~1 km x 1 km at the equator, ~100 ha
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.008983, 0.0], [0.008983, 0.009043], [0.0, 0.009043], [0.0, 0.0]]],
}


# ─── Geometry ────────────────────────────────────────────────────────────────


def test_area_of_one_km_square():
    assert forest.area_ha(SQUARE_1KM) == pytest.approx(100.0, rel=0.01)


def test_point_becomes_four_hectare_circle():
    geom, how = forest.to_plot_geometry(latitude=5.79, longitude=-6.61)
    assert geom["type"] == "Polygon" and "4 ha circle" in how
    assert forest.area_ha(geom) == pytest.approx(4.0, rel=0.01)


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        {"type": "FeatureCollection", "features": []},
        {"type": "Point", "coordinates": [200.0, 0.0]},
    ],
)
def test_bad_geometries_raise(bad):
    """Unsupported type, empty FeatureCollection and out-of-range longitude are rejected."""
    with pytest.raises(ValueError):
        forest.to_plot_geometry(bad)


def test_plot_area_limits():
    huge = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
    with pytest.raises(ValueError, match="at most"):
        forest.plot_area_check(huge)


def test_plot_id_is_stable_and_order_insensitive_for_keys():
    """The audit fingerprint must not depend on the key order of the GeoJSON object."""
    a = {"type": "Polygon", "coordinates": SQUARE_1KM["coordinates"]}
    b = {"coordinates": SQUARE_1KM["coordinates"], "type": "Polygon"}
    assert forest.plot_id(a) == forest.plot_id(b)
    assert forest.plot_id(a).startswith("sha256:")


# ─── Hansen tiles ────────────────────────────────────────────────────────────


def test_tiles_for_plot_crossing_equator_and_meridian():
    """A bbox around (0, 0) touches the four tiles that meet there."""
    names = sorted(t for t, _ in forest.hansen_tiles((-0.1, -0.1, 0.1, 0.1)))
    assert names == ["00N_000E", "00N_010W", "10N_000E", "10N_010W"]


def test_plot_ending_on_tile_edge_keeps_its_tile():
    """A bbox whose north edge is exactly 10N must not pull in the empty 20N tile above it."""
    assert [t for t, _ in forest.hansen_tiles((5.0, 9.5, 5.5, 10.0))] == ["10N_000E"]


# ─── Tally and assessment ────────────────────────────────────────────────────


def grids():
    """10x10 plot, all inside, 1 ha per pixel. Left half forest in 2020."""
    forest_2020 = np.zeros((10, 10), dtype=np.uint8)
    forest_2020[:, :5] = 1
    loss = np.zeros((10, 10), dtype=np.uint8)
    inside = np.ones((10, 10), dtype=bool)
    return forest_2020, loss, inside


def test_tally_separates_before_after_and_non_forest():
    """Loss is split into before the cut-off, after it on forest (deforestation) and after it on
    non-forest; pixels outside the plot are ignored."""
    forest_2020, loss, inside = grids()
    loss[0, 0] = 15  # 2015, forest -> before cut-off
    loss[1, 0:3] = 23  # 2023, forest -> deforestation
    loss[2, 0] = 21  # 2021, forest -> deforestation
    loss[5, 7] = 24  # 2024, non-forest -> loss but not deforestation
    inside[9, :] = False
    loss[9, 0] = 22  # outside the plot -> ignored
    stats = forest.tally(forest_2020, loss, inside, pixel_ha=1.0)
    assert stats["loss_before_cutoff_ha"] == 1
    assert stats["loss_after_cutoff_ha"] == 5
    assert stats["deforestation_after_cutoff_ha"] == 4
    assert stats["deforestation_by_year_ha"] == {2021: 1.0, 2023: 3.0}
    assert stats["plot_ha"] == 90


def test_assess_high_medium_low():
    """Same 3 lost pixels: high at 1 ha/pixel, medium when tiny pixels put them below the review threshold."""
    forest_2020, loss, inside = grids()
    clean = forest.tally(forest_2020, loss, inside, 1.0)
    assert forest.assess(clean, 100)["risk_level"] == "low"
    assert "forest at the cut-off" in forest.assess(clean, 100)["notes"][0]

    loss[0, 0:3] = 22
    high = forest.assess(forest.tally(forest_2020, loss, inside, 1.0), 100)
    assert high["risk_level"] == "high" and "2022" in high["verdict"]

    tiny = forest.tally(forest_2020, loss, inside, 0.0001)  # 3 px of 1 m2 -> below threshold
    assert forest.assess(tiny, 100)["risk_level"] == "medium"


def test_assess_no_forest():
    """Loss on land that was not forest in 2020 is low risk but flagged in the notes."""
    forest_2020, loss, inside = grids()
    forest_2020[:] = 0
    loss[3, 3] = 23
    result = forest.assess(forest.tally(forest_2020, loss, inside, 1.0), 100)
    assert result["risk_level"] == "low"
    assert any("not mapped as forest" in note for note in result["notes"])


# ─── Map ─────────────────────────────────────────────────────────────────────


def test_render_map_is_valid_png():
    """Checks the PNG signature, the upscaled size and the uncompressed scanline length (filter byte + RGB)."""
    forest_2020, loss, inside = grids()
    inside[:2, :] = False
    png = forest.render_map(forest_2020, loss, inside, min_size=50)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (50, 50)
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + idat_len])
    assert len(raw) == height * (1 + width * 3)
