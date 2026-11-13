"""
Chart rendering for the Climate Risk MCP server (matplotlib, headless).

Responsibility: turn the monthly SPEI/SPI lists from ``climate.index_series``
into a compact PNG for the ``drought_chart`` tool.

How it fits:
    server.py     computes the lists, builds title/footer, calls this module
                  in a worker thread and wraps the bytes in an MCP Image.
    climate.py    produces the input lists (month, index, McKee class).
    era5_store.py is not involved.

Kept apart from the statistics so the numbers can be tested without a
plotting backend, and the charts without network access.
"""

from __future__ import annotations

import io

import matplotlib
from PIL import Image as PILImage  # Pillow ships with matplotlib

# "Agg" renders to memory only: the server has no display.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402 - the backend must be chosen first
from matplotlib.patches import Patch  # noqa: E402

# McKee class -> bar colour, dry (brown/orange) to wet (green), after the usual
# drought-monitor palette. Keys must match the labels in climate._CLASSES.
CLASS_COLOURS = {
    "extremely dry": "#7f2704",
    "severely dry": "#d94801",
    "moderately dry": "#fd8d3c",
    "near normal": "#bdbdbd",
    "moderately wet": "#74c476",
    "very wet": "#31a354",
    "extremely wet": "#006d2c",
}


def drought_chart_png(
    spei: list[dict],
    spi: list[dict],
    scale: int,
    title: str,
    footer: str,
) -> bytes:
    """Render monthly SPEI as bars coloured by McKee class, with SPI as a line.

    SPEI gets the bars because it is the more complete drought signal (it
    includes evaporative demand); SPI is overlaid so a reader can tell
    rain-driven drought from heat-driven drying.

    Args:
        spei: Chronological ``{"month", "index", "class"}`` dicts; one bar each.
        spi: Same shape; matched to the bars by month (gaps become breaks).
        scale: Accumulation window in months, used in labels.
        title: Chart title (drawn left-aligned).
        footer: Small attribution line at the bottom (data source, baseline).

    Returns:
        PNG bytes, 800x360 px, palette-compressed by ``_compact_png``.
    """
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=100)
    months = [e["month"] for e in spei]
    x = range(len(months))
    ax.bar(x, [e["index"] for e in spei], color=[CLASS_COLOURS[e["class"]] for e in spei], width=0.85, zorder=2)
    spi_by_month = {e["month"]: e["index"] for e in spi}
    ax.plot(x, [spi_by_month.get(m) for m in months], color="#08306b", linewidth=1.4, label=f"SPI-{scale}", zorder=3)

    # Moderately / severely / extremely dry thresholds.
    for level, style in ((-1.0, ":"), (-1.5, "--"), (-2.0, "-")):
        ax.axhline(level, color="#7f2704", linestyle=style, linewidth=0.8, zorder=1)
    ax.axhline(0, color="#525252", linewidth=0.8, zorder=1)
    ax.set_ylim(-2.6, 2.6)  # the empirical index is bounded to about ±2.3
    ax.set_ylabel(f"SPEI-{scale}")
    step = max(1, len(months) // 12)  # at most ~12 x-axis labels
    ax.set_xticks(list(x)[::step], months[::step], rotation=45, ha="right", fontsize=8)
    ax.grid(axis="y", linewidth=0.3, zorder=0)
    ax.set_title(title, fontsize=10, loc="left")

    # Legend lists only the classes present, in dry-to-wet order, plus the SPI line.
    used = [c for c in CLASS_COLOURS if any(e["class"] == c for e in spei)]
    handles = [Patch(color=CLASS_COLOURS[c], label=c) for c in used]
    handles.append(ax.get_lines()[0])
    ax.legend(handles=handles, fontsize=7, ncol=min(4, len(handles)), loc="upper left", frameon=False)
    fig.text(0.01, 0.01, footer, fontsize=6.5, color="#525252")
    fig.tight_layout(rect=(0, 0.04, 1, 1))  # leave the bottom 4 % for the footer

    rendered = io.BytesIO()
    fig.savefig(rendered, format="png")
    plt.close(fig)  # pyplot keeps figures alive otherwise; a long-running server would leak memory
    return _compact_png(rendered.getvalue())


def _compact_png(png: bytes, colours: int = 64) -> bytes:
    """Re-encode a PNG as an optimized palette (indexed-colour) PNG.

    Why: matplotlib writes full RGBA, but a chart has only a handful of flat
    colours plus anti-aliasing. Quantising to a 64-colour palette is visually
    lossless for such images and shrinks the file several-fold, which keeps
    it small enough to return inline (EVE's MCP file convention: a few tens
    of KB).

    Args:
        png: PNG bytes as produced by matplotlib.
        colours: Palette size; 64 keeps anti-aliased text edges smooth.
    """
    image = PILImage.open(io.BytesIO(png)).convert("RGB").quantize(colors=colours)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
