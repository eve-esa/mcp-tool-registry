"""Unit tests for aoi.py: input parsing, area, bbox and coverage (no network)."""

import json

import aoi
import pytest

BOX = [-118.17, 34.17, -118.02, 34.27]


@pytest.mark.parametrize("text", [json.dumps(BOX), ",".join(str(v) for v in BOX), " -118.17, 34.17,-118.02 ,34.27 "])
def test_bbox_strings_as_json_or_effis_format(text):
    assert aoi.bbox(aoi.to_area(bbox=text)[0]) == tuple(BOX)


def test_area_of_bbox_matches_planar_estimate():
    geom, _ = aoi.to_area(bbox=BOX)
    # 0.15° x 0.10° at 34.2°N: ~13.8 km x 11.1 km
    assert aoi.area_km2(geom) == pytest.approx(153.2, rel=0.01)


def test_circle_area_and_centre():
    geom, how = aoi.to_area(latitude=34.2, longitude=-118.1, radius_km=5)
    assert "5 km" in how
    assert aoi.area_km2(geom) == pytest.approx(3.14159 * 25, rel=0.01)
    lat, lon = aoi.centroid(geom)
    assert lat == pytest.approx(34.2, abs=1e-3) and lon == pytest.approx(-118.1, abs=1e-3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"bbox": BOX, "latitude": 1.0, "longitude": 1.0},
        {"bbox": [1, 2, 0, 3]},
        {"latitude": 34.0},
        {"geometry": {"type": "Point", "coordinates": [0, 0]}},
        {"latitude": 34.0, "longitude": -118.0, "radius_km": 40},
        {"bbox": [-120, 30, -119, 31]},  # ~10,600 km², over the limit
    ],
)
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        aoi.to_area(**kwargs)


def test_coverage_of_footprint():
    geom = aoi.to_area(bbox=[0, 0, 0.2, 0.2])[0]
    half = aoi.to_area(bbox=[0, 0, 0.1, 0.2])[0]
    assert aoi.coverage(geom, geom) == 1.0
    assert aoi.coverage(geom, half) == pytest.approx(0.5)


def test_area_limit_error_names_the_fix():
    with pytest.raises(ValueError, match="bbox of one fire"):
        aoi.to_area(bbox=[-0.9344, 41.3478, 0.7713, 42.9245])  # a whole province, ~24,700 km²
