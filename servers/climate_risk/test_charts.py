"""Offline test for the chart renderer (no network, no LLM).

Renders a chart covering every McKee class and checks that the output is a
valid PNG of the expected size and small enough to return inline.

Run from this directory: python -m pytest test_charts.py -q
"""

import struct

import charts


def test_drought_chart_png_is_small_valid_png():
    spei = [
        {"month": f"2025-{m:02d}", "index": i, "class": c}
        for m, (i, c) in enumerate(
            [(0.2, "near normal"), (-1.2, "moderately dry"), (-1.7, "severely dry"), (-2.3, "extremely dry"),
             (1.1, "moderately wet"), (2.1, "extremely wet")],
            start=1,
        )
    ]
    spi = [{**e, "index": e["index"] * 0.8} for e in spei]
    png = charts.drought_chart_png(spei, spi, 3, "Drought at test", "footer")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")  # PNG signature
    width, height = struct.unpack(">II", png[16:24])  # IHDR width/height (big-endian)
    assert (width, height) == (800, 360)
    assert len(png) < 40_000  # small enough to return inline (EVE stores it as an artifact)
