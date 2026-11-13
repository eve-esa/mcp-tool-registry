"""
Figures of the Wildfire Flood MCP server (matplotlib, headless).

cascade_png — left: a danger-zone map (burn severity over a hillshade, the
    2 km zone around steep, badly burned slopes, built-up land inside and
    outside it, named places); right: rainfall per 15 minutes (or per hour)
    against the post-fire debris-flow threshold. Together they tell the cascade
    at a glance: where the scar is, who lives below it, and whether the rain
    crosses the line.
before_after_jpeg — natural-colour Sentinel-2 photos before and after the
    fire, side by side, so anyone can see the scar without reading a legend.

Maps are drawn in the grid's UTM metres but labelled in longitude/latitude.
"""

from __future__ import annotations

import io
from datetime import datetime

import burn
import matplotlib
import numpy as np
from PIL import Image as PILImage  # Pillow ships with matplotlib
from rasterio.warp import transform

matplotlib.use("Agg")  # no display in a server
import matplotlib.dates as mdates  # noqa: E402 - the backend must be chosen first
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

# Colours for severity classes 1..4 (low, moderate-low, moderate-high, high).
SEVERITY_COLOURS = ["#fee08b", "#fdae61", "#f46d43", "#a50026"]
EXPOSED_COLOUR = "#6a1b9a"  # built-up land in the danger zone
BUILT_UP_COLOUR = "#546e7a"  # other built-up land
DANGER_COLOUR = "#4a148c"  # outline of the danger zone
MAX_MAP_PX = 450  # longest side of the map raster, to keep the PNG small
SHADE_LEVELS = 4
MAX_PLACE_LABELS = 8
JPEG_QUALITY = 80  # photos compress badly as palette PNGs; JPEG keeps them small


def _downsample(array: np.ndarray, step: int) -> np.ndarray:
    """Every step-th pixel: enough for a ~450 px map and keeps the PNG small."""
    return array[::step, ::step] if step > 1 else array


def _extent(grid) -> tuple[float, float, float, float]:
    """(left, right, bottom, top) of a grid in its CRS, for imshow."""
    left, top = grid.transform * (0, 0)
    right, bottom = grid.transform * (grid.width, grid.height)
    return left, right, bottom, top


def _nice_step(span_deg: float) -> float:
    """A round tick spacing giving about 3-6 ticks over span_deg degrees."""
    for step in (0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1.0):
        if span_deg / step <= 6:
            return step
    return 2.0


def _lonlat_ticks(ax, grid) -> None:
    """Label UTM axes in degrees: ticks at round longitudes and latitudes.

    The grid is in metres (UTM) so areas and distances stay true; readers know
    places by longitude/latitude, so the ticks are placed where round degree
    values fall along the middle of the map.
    """
    left, right, bottom, top = _extent(grid)
    mid_x, mid_y = (left + right) / 2, (bottom + top) / 2
    (lon_l, lon_r), _ = transform(grid.crs, "EPSG:4326", [left, right], [mid_y, mid_y])
    _, (lat_b, lat_t) = transform(grid.crs, "EPSG:4326", [mid_x, mid_x], [bottom, top])
    lon_step, lat_step = _nice_step(lon_r - lon_l), _nice_step(lat_t - lat_b)
    lons = list(np.arange(np.ceil(lon_l / lon_step) * lon_step, lon_r, lon_step))
    lats = list(np.arange(np.ceil(lat_b / lat_step) * lat_step, lat_t, lat_step))
    mid_lat, mid_lon = (lat_b + lat_t) / 2, (lon_l + lon_r) / 2
    xs, _ = transform("EPSG:4326", grid.crs, lons, [mid_lat] * len(lons))
    _, ys = transform("EPSG:4326", grid.crs, [mid_lon] * len(lats), lats)
    ax.set_xticks(xs, [f"{v:.2f}°" for v in lons])
    ax.set_yticks(ys, [f"{v:.2f}°" for v in lats])
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    ax.tick_params(labelsize=7)


def _draw_outline(ax, outline_xy, colour: str = "black") -> None:
    """The analysed area's outline (rings in the grid CRS)."""
    for ring in outline_xy:
        ax.plot([x for x, _ in ring], [y for _, y in ring], color=colour, linewidth=1.2)


# Label positions tried in order, as (dx, dy) in points from the dot: right,
# below-right, left, below-left. A negative dx means the label ends at the dot.
_LABEL_OFFSETS = ((4, 3), (4, -11), (-4, 3), (-4, -11))


def _draw_places(ax, places: list[dict], on_photo: bool = False) -> None:
    """Dots and names of the main settlements; those in the danger zone in dark red.

    Places come most relevant first. Each label takes the first position that
    does not cover an earlier label; a place with no free position is left
    unlabelled, so the map never shows a pile of names. Label boxes are
    estimated from the character count (about 0.6 em per character).
    """
    ax.figure.canvas.draw()  # fixes the data -> pixel transform
    to_px = ax.transData.transform
    pt = ax.figure.dpi / 72.0
    taken: list[tuple[float, float, float, float]] = []
    labelled = 0
    for place in places:
        if labelled == MAX_PLACE_LABELS:
            break
        x, y = to_px((place["x"], place["y"]))
        width, height = 0.62 * 7 * len(place["name"]) * pt + 4, 10 * pt
        for dx, dy in _LABEL_OFFSETS:
            left = x + dx * pt if dx > 0 else x + dx * pt - width
            box = (left, y + dy * pt, left + width, y + dy * pt + height)
            if all(box[2] < o[0] or box[0] > o[2] or box[3] < o[1] or box[1] > o[3] for o in taken):
                break
        else:
            continue
        taken.append(box)
        labelled += 1
        danger = place.get("near_steep_burn")
        ax.plot(place["x"], place["y"], marker="o", markersize=4, color="#b71c1c" if danger else "#263238",
                markeredgecolor="white", markeredgewidth=0.6, zorder=5)
        ax.annotate(place["name"], (place["x"], place["y"]), xytext=(dx, dy), textcoords="offset points",
                    ha="left" if dx > 0 else "right", fontsize=7, fontweight="bold" if danger else "normal",
                    color="#b71c1c" if danger and not on_photo else "#111111", zorder=6,
                    bbox={"boxstyle": "round,pad=0.15", "facecolor": "white", "edgecolor": "none", "alpha": 0.8})


def cascade_png(
    scene,
    outline_xy: list[list[tuple[float, float]]],
    rain: dict,
    hazard_result: dict,
    title: str,
    footer: str,
) -> bytes:
    """Render the two-panel figure and return it as a compact PNG.

    The map shows severity inside the AOI only, while built-up land is drawn
    over the whole grid (the AOI plus the exposure margin).

    Args:
        scene: sources.BurnScene with grid, dnbr, shade, land_cover.
        outline_xy: AOI rings in the grid CRS (metres).
        rain: series from sources.fetch_rain.
        hazard_result: output of hazard.assess.
        title: figure title.
        footer: source line printed at the bottom.
    """
    grid = scene.grid
    step = max(1, int(np.ceil(max(grid.shape) / MAX_MAP_PX)))
    extent = _extent(grid)

    fig, (ax_map, ax_rain) = plt.subplots(
        1, 2, figsize=(11, 5.6), dpi=90, gridspec_kw={"width_ratios": [1.05, 1]}
    )

    # Map: hillshade, severity classes 1-4, built-up land, danger zone, places, AOI outline.
    # Posterized hillshade: a few grey levels keep the terrain readable and the PNG small.
    shade = np.round(_downsample(scene.shade, step) * SHADE_LEVELS) / SHADE_LEVELS
    ax_map.imshow(shade, cmap="gray", vmin=-0.3, vmax=1, extent=extent, interpolation="nearest")
    masks = burn.danger_masks(scene.dnbr, scene.slope, scene.land_cover, scene.inside, grid.pixel_m,
                              scene.stats.near_km)
    # Severity only inside the AOI: the margin is read for exposure, and on shaded
    # slopes far from the fire it shows illumination speckle, not burn.
    classes = burn.severity(_downsample(np.where(masks.burned, scene.dnbr, np.nan), step)).astype("float32")
    classes[classes < 1] = np.nan
    ax_map.imshow(classes, cmap=ListedColormap(SEVERITY_COLOURS), vmin=0.5, vmax=4.5, extent=extent,
                  alpha=0.8, interpolation="nearest")
    built = scene.land_cover == burn.BUILT_UP
    for mask, colour, alpha in ((built & ~masks.exposed_built_up, BUILT_UP_COLOUR, 0.45),
                                (masks.exposed_built_up, EXPOSED_COLOUR, 0.85)):
        layer = _downsample(mask, step).astype("float32")
        layer[layer == 0] = np.nan
        ax_map.imshow(layer, cmap=ListedColormap([colour]), extent=extent, alpha=alpha, interpolation="nearest")
    near = _downsample(masks.near_zone, step).astype("float32")
    if near.any() and not near.all():
        ax_map.contour(near, levels=[0.5], extent=extent, origin="upper", colors=DANGER_COLOUR,
                       linewidths=1.0, linestyles="--")
    _draw_outline(ax_map, outline_xy)
    _lonlat_ticks(ax_map, grid)
    _draw_places(ax_map, scene.places)
    stats = scene.stats
    ax_map.set_title(
        f"Danger zone: {stats.burned_ha:,.0f} ha burned, {stats.steep_mod_high_share:.0%} badly on steep slopes;\n"
        f"{stats.built_up_near_ha:,.0f} ha of built-up land within {stats.near_km:g} km of those slopes",
        fontsize=8.5, loc="left",
    )
    handles = [Patch(color=c, label=f"burn: {n}") for c, n in zip(SEVERITY_COLOURS, ["low", "moderate-low",
               "moderate-high", "high"], strict=True)]
    handles += [
        Line2D([], [], color=DANGER_COLOUR, linestyle="--", label=f"{stats.near_km:g} km around steep, badly burned slopes"),
        Patch(color=EXPOSED_COLOUR, alpha=0.85, label="built-up in that zone"),
        Patch(color=BUILT_UP_COLOUR, alpha=0.6, label="other built-up"),
    ]
    # Below the map, so it never hides the scar.
    ax_map.legend(handles=handles, fontsize=6.5, loc="upper center", bbox_to_anchor=(0.5, -0.07), ncol=2,
                  frameon=False)

    # Rain: bars per step, 50 % threshold line, peak annotation.
    minutes = hazard_result["burst_duration_min"]
    times = rain["times_15"] if minutes == 15 else rain["times_60"]
    values = rain["mm_15"] if minutes == 15 else rain["mm_60"]
    stamps = [datetime.fromisoformat(t) for t in times]
    heights = [v or 0.0 for v in values]
    threshold = hazard_result["by_duration"][f"{minutes}_min"]["threshold_50pct_mm"]
    colours = ["#c62828" if threshold is not None and h >= threshold else "#1e88e5" for h in heights]
    # The edge line gives every bar at least ~1 px: over a long window a
    # 15-minute bar is narrower than a pixel and could vanish, peak included.
    ax_rain.bar(stamps, heights, width=minutes / 1440, color=colours, edgecolor=colours, linewidth=0.8,
                align="edge")
    if threshold is not None:
        ax_rain.axhline(threshold, color="#c62828", linestyle="--", linewidth=1.2,
                        label=f"50 % debris-flow threshold: {threshold:g} mm / {minutes} min")
        ax_rain.legend(fontsize=7.5, loc="upper left")
    ax_rain.set_ylim(0, max([*heights, threshold or 0, 1.0]) * 1.25)
    ax_rain.set_ylabel(f"rain per {minutes} min (mm)", fontsize=8)
    ax_rain.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
    ax_rain.tick_params(labelsize=7)
    ax_rain.grid(axis="y", color="#dddddd", linewidth=0.6)
    level = hazard_result["risk_level"]
    ax_rain.set_title(
        f"Rain vs post-fire debris-flow threshold\nlikelihood {hazard_result['likelihood']:.0%} ({level} risk)",
        fontsize=8.5, loc="left", color={"high": "#c62828", "medium": "#ef6c00"}.get(level, "#2e7d32"),
    )

    fig.suptitle(title, fontsize=11, x=0.01, ha="left")
    fig.text(0.01, 0.01, footer, fontsize=6.5, color="#525252")
    fig.tight_layout(rect=(0, 0.13, 1, 0.95))  # the bottom band holds the map legend and the footer
    rendered = io.BytesIO()
    fig.savefig(rendered, format="png")
    plt.close(fig)  # free the figure; the server renders many over its lifetime
    return compact_png(rendered.getvalue())


def before_after_jpeg(scene, outline_xy: list, title: str, footer: str) -> bytes:
    """Natural-colour Sentinel-2 photos before and after the fire, side by side.

    Args:
        scene: sources.BurnScene with display_grid, photo_pre, photo_post, places, scenes.
        outline_xy: analysed-area rings in the grid CRS (metres).
        title: figure title.
        footer: source line printed at the bottom.

    Returns:
        JPEG bytes (photos do not compress well as palette PNGs).
    """
    grid = scene.display_grid
    extent = _extent(grid)
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.0), dpi=90)
    for ax, photo, key, label in ((axes[0], scene.photo_pre, "pre_fire", "Before the fire"),
                                  (axes[1], scene.photo_post, "post_fire", "After the fire")):
        ax.imshow(photo, extent=extent, interpolation="bilinear")
        _draw_outline(ax, outline_xy, colour="#ffd600")
        _lonlat_ticks(ax, grid)
        _draw_places(ax, scene.places, on_photo=True)
        ax.set_title(f"{label} · Sentinel-2, {scene.scenes[key]['datetime'][:10]}", fontsize=9, loc="left")
    fig.suptitle(title, fontsize=11, x=0.01, ha="left")
    fig.text(0.01, 0.01, footer, fontsize=6.5, color="#525252")
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    rendered = io.BytesIO()
    fig.savefig(rendered, format="png")
    plt.close(fig)
    out = io.BytesIO()
    PILImage.open(rendered).convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def compact_png(png: bytes, colours: int = 48) -> bytes:
    """Re-encode as an optimized palette PNG so it stays small enough to return
    inline (EVE's MCP file convention: a few tens of KB).

    Fast-octree quantisation keeps colours that cover only a few pixels, such as
    the purple danger-zone line and exposed built-up land, which median-cut
    would merge into grey.
    """
    image = PILImage.open(io.BytesIO(png)).convert("RGB").quantize(colors=colours, method=PILImage.Quantize.FASTOCTREE)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
