"""Offline tests for the overview map (no network, no LLM).

Covers map bounds, figure sizing and that the rendered PNG stays small enough
to return inline; forest.centroid is tested here because it places the markers.

Run from this directory: python -m pytest test_overview.py -q
"""

import struct

import forest
import numpy as np
import overview
import pytest

SQUARE = {"type": "Polygon", "coordinates": [[[0.0, 0.0], [0.01, 0.0], [0.01, 0.01], [0.0, 0.01], [0.0, 0.0]]]}


def test_map_bounds_pads_and_enforces_minimum_span():
    """A 1 km plot is widened to MIN_SPAN_DEG, then padded by 12 % on each side (x 1.24)."""
    west, south, east, north = overview.map_bounds([SQUARE])
    assert east - west == pytest.approx(overview.MIN_SPAN_DEG * 1.24)
    assert (west + east) / 2 == pytest.approx(0.005)


def test_overview_png_is_small_valid_png():
    """An 8 in figure at 100 dpi is 800 px wide; the palette PNG must stay under 60 KB."""
    plots = [
        ("farm-01", SQUARE, "high"),
        ("farm-02", forest.to_plot_geometry(latitude=0.02, longitude=0.02)[0], "low"),
        ("farm-03", forest.to_plot_geometry(latitude=0.03, longitude=0.0)[0], "medium"),
    ]
    bounds = overview.map_bounds([g for _, g, _ in plots])
    fraction = np.linspace(0, 1, 100 * 100, dtype=np.float32).reshape(100, 100)
    png = overview.overview_png(plots, fraction, bounds, "Test cooperative", "footer")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert width == 800 and 380 <= height <= 900
    assert len(png) < 60_000


def test_overview_png_with_custom_classes():
    """The decision map colours plots by supply status instead of EUDR risk."""
    classes = overview.MapClasses(
        colours={"blocked": "#d73027", "secure": "#1a9850"},
        legend=[("blocked", "blocked"), ("secure", "secure")],
    )
    plots = [("farm-01", SQUARE, "blocked"), ("farm-02", forest.to_plot_geometry(latitude=0.02, longitude=0.02)[0], "secure")]
    bounds = overview.map_bounds([g for _, g, _ in plots])
    fraction = np.zeros((50, 50), dtype=np.float32)
    png = overview.overview_png(plots, fraction, bounds, "Supply status", "footer", classes)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
