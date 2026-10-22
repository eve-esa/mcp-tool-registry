"""Unit tests for burn.py: NBR, severity classes, slope, despeckling, statistics."""

import burn
import numpy as np
import pytest


def test_reflectance_applies_scale_offset_and_nodata():
    dn = np.array([[0, 1000, 3000]], dtype="uint16")
    out = burn.reflectance(dn, 0.0001, -0.1)
    assert np.isnan(out[0, 0])
    assert out[0, 1] == pytest.approx(0.0, abs=1e-6) and out[0, 2] == pytest.approx(0.2)


def test_nbr_drops_weak_signal_and_stays_in_range():
    nir = np.array([0.30, 0.005, 0.10], dtype="float32")
    swir = np.array([0.10, 0.004, -0.09], dtype="float32")
    ratio = burn.nbr(nir, swir)
    assert ratio[0] == pytest.approx(0.5)
    assert np.isnan(ratio[1])  # NIR + SWIR2 below MIN_NBR_SIGNAL
    assert np.isnan(ratio[2])  # sum 0.01 is also too weak
    assert np.nanmax(np.abs(ratio)) <= 1


def test_severity_thresholds():
    values = np.array([np.nan, 50, 100, 300, 500, 700])
    assert burn.severity(values).tolist() == [-1, 0, 1, 2, 3, 4]


def test_slope_of_a_45_degree_ramp():
    dem = np.tile(np.arange(10, dtype="float32") * 20, (10, 1))  # rises 20 m per 20 m pixel
    assert burn.slope_degrees(dem, 20)[5, 5] == pytest.approx(45, abs=0.01)


def test_footprint_keeps_scar_and_drops_speckle():
    """A solid burned block survives; isolated burned pixels (shadow noise) do not."""
    dnbr = np.zeros((30, 30))
    dnbr[5:15, 5:15] = 600
    dnbr[25, 25] = 600
    dnbr[22, 3] = 600
    footprint = burn.burn_footprint(dnbr)
    assert footprint[6:14, 6:14].all()  # the interior and edges stay
    assert footprint[5:15, 5:15].sum() == 100 - 4 * 3  # only the three pixels at each corner go
    assert not footprint[25, 25] and not footprint[22, 3]


def test_burn_stats_on_synthetic_scar():
    shape = (60, 60)
    dnbr = np.zeros(shape)
    dnbr[10:30, 10:30] = 700  # high severity block
    slope = np.zeros(shape, dtype="float32")
    slope[10:30, 10:20] = 30  # half of the block is steep
    land = np.full(shape, 10, dtype="uint8")
    land[40:45, 10:20] = burn.BUILT_UP  # 10 px south of the scar
    land[0:5, 50:60] = burn.BUILT_UP  # far away
    inside = np.ones(shape, dtype=bool)

    stats = burn.burn_stats(dnbr, slope, land, inside, pixel_m=20, near_km=0.4)  # 20 px radius
    pixel_ha = 0.04
    assert stats.burned_ha == pytest.approx(400 * pixel_ha, rel=0.05)
    assert stats.severity_ha["high"] == pytest.approx(stats.burned_ha)
    assert stats.steep_mod_high_share == pytest.approx(0.5, abs=0.03)
    assert stats.land_cover_burned_ha == {"tree cover": stats.burned_ha}
    assert stats.built_up_near_ha == pytest.approx(50 * pixel_ha)  # only the nearby block
    assert stats.clear_share == 1.0
    assert stats.cropland_change_ha == 0


def test_cropland_is_left_out_of_the_scar():
    """Harvested fields look burned in dNBR; they are reported apart, not as scar."""
    dnbr = np.full((20, 20), 500.0)
    land = np.full((20, 20), 10, dtype="uint8")
    land[:, 10:] = burn.CROPLAND
    stats = burn.burn_stats(dnbr, np.zeros((20, 20), "float32"), land, np.ones((20, 20), bool), pixel_m=20)
    # the despeckling window trims a few pixels at the grid corners
    assert stats.burned_ha == pytest.approx(200 * 0.04, rel=0.05)
    assert stats.cropland_change_ha == pytest.approx(200 * 0.04, rel=0.05)
    assert not burn.scar_mask(dnbr, land)[:, 10:].any()


def test_danger_masks_zone_and_exposed_built_up():
    """Steep, badly burned slopes define a zone; only built-up land inside it and off the scar is exposed."""
    shape = (60, 60)
    dnbr = np.zeros(shape)
    dnbr[10:30, 10:30] = 700
    slope = np.full(shape, 30, dtype="float32")
    land = np.full(shape, 10, dtype="uint8")
    land[35:40, 10:20] = burn.BUILT_UP  # 5-9 px below the scar: inside a 10 px zone
    land[55:60, 50:60] = burn.BUILT_UP  # far away
    masks = burn.danger_masks(dnbr, slope, land, np.ones(shape, bool), pixel_m=20, near_km=0.2)
    assert masks.steep_mod_high[20, 20] and not masks.steep_mod_high[50, 50]
    assert masks.near_zone[38, 15] and not masks.near_zone[57, 55]
    assert masks.exposed_built_up.sum() == 50


def test_settlement_cells_prefer_exposed_and_keep_apart():
    """Clusters in the danger zone come first; neighbouring blocks collapse into one label."""
    land = np.full((100, 100), 10, dtype="uint8")
    land[0:25, 0:25] = burn.BUILT_UP  # big town, outside the zone
    land[70:80, 70:80] = burn.BUILT_UP  # small village, in the zone
    near = np.zeros((100, 100), bool)
    near[60:100, 60:100] = True
    cells = burn.settlement_cells(land, near, pixel_m=100, cell_m=500, min_separation_m=3000)
    assert cells[0]["near_steep_burn"] and 70 <= cells[0]["row"] < 80
    assert len(cells) == 2  # the 25 blocks of the big town give a single label
    assert not cells[1]["near_steep_burn"]
