"""
Overview map of many plots for the Forest Compliance MCP server (matplotlib, headless).

Used by the ``eudr_overview_map`` tool in ``server.py``: after every plot has
been assessed, this module draws them on one map, coloured by EUDR risk and
labelled by id, over a forest-fraction background read by
``sources.forest_background``. Geometry helpers (``bbox``, ``centroid``) come
from ``forest.py``.

The single-plot map (``eudr_plot_map``) is rendered in ``forest.render_map``
instead, pixel by pixel; this one needs axes, labels and a legend, hence
matplotlib. The Agg backend is used because the server has no display.

Each MCP server in the registry is deployed on its own, so this module does not
share code with other servers.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

import forest
import matplotlib
import numpy as np
from PIL import Image as PILImage  # Pillow ships with matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402 - the backend must be chosen first
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.patches import Polygon as PolygonPatch  # noqa: E402

# Marker/outline colour per EUDR risk level (ColorBrewer RdYlGn: green, orange, red).
RISK_COLOURS = {"low": "#1a9850", "medium": "#fdae61", "high": "#d73027"}
# Background ramp for forest fraction 0..1: beige (no forest) to dark green.
FOREST_CMAP = LinearSegmentedColormap.from_list("forest", ["#f2efe6", "#2e7d32"])
MIN_SPAN_DEG = 0.04  # ~4.5 km, so a single farm still shows its surroundings


@dataclass(frozen=True)
class MapClasses:
    """How plots are coloured on an overview map.

    Attributes:
        colours: Class key -> marker and outline colour.
        legend: (class key, legend text) in legend order; the plot count is appended.
    """

    colours: dict[str, str]
    legend: list[tuple[str, str]]


# The default map: plots coloured by EUDR risk level.
EUDR_CLASSES = MapClasses(
    colours=RISK_COLOURS,
    legend=[("high", "high EUDR risk"), ("medium", "medium EUDR risk"), ("low", "low EUDR risk")],
)


def map_bounds(geoms: list[dict], pad_fraction: float = 0.12) -> tuple[float, float, float, float]:
    """Bounding box of all plots, padded and at least MIN_SPAN_DEG wide and tall.

    Args:
        geoms: Plot Polygons/MultiPolygons.
        pad_fraction: Margin added on each side, as a fraction of the span.

    Returns:
        (west, south, east, north) in degrees, centred on the plots.
    """
    boxes = [forest.bbox(g) for g in geoms]
    west, south = min(b[0] for b in boxes), min(b[1] for b in boxes)
    east, north = max(b[2] for b in boxes), max(b[3] for b in boxes)
    cx, cy = (west + east) / 2, (south + north) / 2
    half_w = max(east - west, MIN_SPAN_DEG) * (0.5 + pad_fraction)
    half_h = max(north - south, MIN_SPAN_DEG) * (0.5 + pad_fraction)
    return cx - half_w, cy - half_h, cx + half_w, cy + half_h


def _rings(geom: dict) -> list[list[list[float]]]:
    """Outer ring of each polygon; holes are ignored, as they are invisible at overview scale."""
    polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [polygon[0] for polygon in polygons]


def overview_png(
    plots: list[tuple[str, dict, str]],
    forest_fraction: np.ndarray,
    bounds: tuple[float, float, float, float],
    title: str,
    footer: str,
    classes: MapClasses = EUDR_CLASSES,
) -> bytes:
    """Render the overview map: plots coloured by class over the GFC2020 forest fraction.

    Args:
        plots: (label, geometry, class key) per assessed plot; by default the
            class is the EUDR risk level.
        forest_fraction: Background array from ``sources.forest_background``.
        bounds: (west, south, east, north) extent of ``forest_fraction``.
        title: Map title (top left).
        footer: Small source/disclaimer line at the bottom.
        classes: Colours and legend of the plot classes (EUDR risk by default;
            the supply-status map passes its own).

    Returns:
        A compact palette PNG (see ``compact_png``).
    """
    west, south, east, north = bounds
    mid_lat = (south + north) / 2
    fig, ax = plt.subplots(figsize=figure_size(bounds), dpi=100)
    ax.imshow(forest_fraction, extent=(west, east, south, north), cmap=FOREST_CMAP, vmin=0, vmax=1,
              interpolation="nearest", zorder=0)
    # A degree of longitude is cos(lat) times shorter than a degree of latitude;
    # this aspect keeps shapes undistorted on the lon/lat axes.
    ax.set_aspect(1 / max(math.cos(math.radians(mid_lat)), 1e-6))

    markers = []
    for plot_ref, geom, key in plots:
        colour = classes.colours[key]
        for ring in _rings(geom):
            ax.add_patch(PolygonPatch(ring, closed=True, facecolor=colour, edgecolor="black",
                                      linewidth=0.8, alpha=0.75, zorder=2))
        lat, lon = forest.centroid(geom)
        ax.plot(lon, lat, marker="o", markersize=9, markerfacecolor=colour, markeredgecolor="black",
                markeredgewidth=0.8, zorder=3)
        markers.append((plot_ref, lon, lat))

    ax.set_xlim(west, east)
    ax.set_ylim(south, north)
    fig.canvas.draw()  # fixes the data -> pixel transform used to place labels
    _place_labels(ax, markers)
    ax.set_xlabel("longitude (°)", fontsize=8)
    ax.set_ylabel("latitude (°)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.set_title(title, fontsize=10, loc="left")

    counts = {key: sum(1 for _, _, k in plots if k == key) for key in classes.colours}
    handles = [
        Line2D([], [], marker="o", linestyle="", markersize=8, markerfacecolor=classes.colours[key],
               markeredgecolor="black", label=f"{text} ({counts[key]})")
        for key, text in classes.legend
    ]
    handles.append(Patch(facecolor="#2e7d32", label="forest on 31 Dec 2020 (JRC GFC2020)"))
    ax.legend(handles=handles, fontsize=7, loc="lower left", framealpha=0.9)
    fig.text(0.01, 0.01, footer, fontsize=6.5, color="#525252")
    fig.tight_layout(rect=(0, 0.03, 1, 1))

    rendered = io.BytesIO()
    fig.savefig(rendered, format="png")
    plt.close(fig)
    return compact_png(rendered.getvalue())


def figure_size(bounds: tuple[float, float, float, float], width_in: float = 8.0) -> tuple[float, float]:
    """Figure size (inches) following the map's ground aspect ratio.

    Wide or tall regions would otherwise leave large empty margins. The height
    adds ~1.3 in for title, axis labels and footer, and is clamped to 3.8..9 in.
    """
    west, south, east, north = bounds
    ground_aspect = (east - west) * math.cos(math.radians((south + north) / 2)) / (north - south)
    height = min(max(width_in / ground_aspect + 1.3, 3.8), 9.0)
    return width_in, round(height, 2)


# Label offsets (dx, dy) tried in order, in points from the marker: close
# positions around it first, then further out (with a leader line).
_LABEL_OFFSETS = [(7, 7), (7, -15), (-7, 7), (-7, -15), (7, 22), (-7, 22), (7, -30), (-7, -30),
                  (24, 0), (-24, 0), (7, 38), (-7, 38), (7, -46), (-7, -46)]


def _place_labels(ax, markers: list[tuple[str, float, float]]) -> None:
    """Place one label per marker without overlaps, greedily.

    Plots of a cooperative are often clustered, so default labels would pile on
    top of each other. Markers are handled top to bottom, left to right; for each
    one the candidate offsets are tried in order and the first whose label box
    overlaps neither placed labels nor markers is taken (else the least
    overlapping one). Greedy placement is not optimal, but it is fast,
    deterministic and good enough for the 25 plots a call allows. A leader line
    is drawn when the label ends up away from its marker.

    Label sizes are estimated from the character count rather than measured,
    to avoid a costly renderer round trip per candidate.

    Args:
        ax: Axes whose data -> pixel transform is final (the canvas was drawn).
        markers: (label, lon, lat) per plot.
    """
    to_px = ax.transData.transform
    pt = ax.figure.dpi / 72.0  # pixels per typographic point
    marker_px = [to_px((lon, lat)) for _, lon, lat in markers]
    placed: list[tuple[float, float, float, float]] = []

    def box(x: float, y: float, dx: float, dy: float, width: float, height: float):
        """Pixel box (left, bottom, right, top) of a label at offset (dx, dy) points.

        Mirrors the annotation alignment: labels to the right are left-aligned,
        labels to the left are right-aligned.
        """
        left = x + dx * pt if dx >= 0 else x + dx * pt - width
        bottom = y + dy * pt
        return left, bottom, left + width, bottom + height

    def overlap(a, b) -> float:
        """Overlap area of two pixel boxes (0 when disjoint)."""
        w = min(a[2], b[2]) - max(a[0], b[0])
        h = min(a[3], b[3]) - max(a[1], b[1])
        return w * h if w > 0 and h > 0 else 0.0

    order = sorted(range(len(markers)), key=lambda i: (-marker_px[i][1], marker_px[i][0]))
    for i in order:
        label, lon, lat = markers[i]
        x, y = marker_px[i]
        width, height = 6.4 * len(label) * pt + 6, 12 * pt
        best, best_cost = _LABEL_OFFSETS[0], float("inf")
        for dx, dy in _LABEL_OFFSETS:
            candidate = box(x, y, dx, dy, width, height)
            cost = sum(overlap(candidate, other) for other in placed)
            # Each marker occupies a 14 x 14 px square.
            cost += sum(overlap(candidate, (mx - 7, my - 7, mx + 7, my + 7)) for mx, my in marker_px)
            if cost < best_cost:
                best, best_cost = (dx, dy), cost
            if cost == 0:
                break
        placed.append(box(x, y, *best, width, height))
        far = abs(best[1]) > 15 or abs(best[0]) > 15
        ax.annotate(
            label, (lon, lat), xytext=best, textcoords="offset points", fontsize=8, fontweight="bold",
            ha="left" if best[0] >= 0 else "right", zorder=4,
            bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "edgecolor": "none", "alpha": 0.85},
            arrowprops={"arrowstyle": "-", "color": "#333333", "linewidth": 0.6} if far else None,
        )


def compact_png(png: bytes, colours: int = 128) -> bytes:
    """Re-encode as an optimized palette PNG so it stays small enough to return inline.

    MCP images travel base64-encoded inside the tool result, and EVE's
    convention for inline images is a few tens of KB. A flat-colour map
    quantised to 128 colours looks the same and is several times smaller than
    matplotlib's RGBA output.
    """
    image = PILImage.open(io.BytesIO(png)).convert("RGB").quantize(colors=colours)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
