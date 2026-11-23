"""PNG charts and CSV tables written to OUTPUT_DIR. Tools return their paths."""

from __future__ import annotations

import csv
import os
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

OUTPUT_DIR = Path(os.getenv("CROP_OUTPUT_DIR", Path(__file__).resolve().parent / "outputs"))
COLORS = {"ndvi": "#2e7d32", "ndmi": "#1565c0", "ndwi": "#00838f"}


def _out(name: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / name


def _period_mid(row: dict) -> date:
    a, b = date.fromisoformat(row["first_scene"]), date.fromisoformat(row["last_scene"])
    return a + (b - a) / 2


def vegetation_chart(tag: str, scenes: list[dict], periods: list[dict], indices: list[str], title: str) -> dict:
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    ax, ax_anom = axes
    for name in indices:
        pts = [(date.fromisoformat(s["date"]), s[name]) for s in scenes if s.get(name) is not None]
        if pts:
            ax.scatter(*zip(*pts, strict=True), s=10, alpha=0.35, color=COLORS.get(name), label=f"{name.upper()} scenes")
        rows = [r for r in periods if r.get(name)]
        if rows:
            xs = [_period_mid(r) for r in rows]
            ax.plot(xs, [r[name]["median"] for r in rows], "-o", ms=4, color=COLORS.get(name), label=f"{name.upper()} period median")
            ax.fill_between(xs, [r[name]["p10"] for r in rows], [r[name]["p90"] for r in rows], color=COLORS.get(name), alpha=0.12)
            anom = [(x, r[name].get("anomaly")) for x, r in zip(xs, rows, strict=True) if r[name].get("anomaly") is not None]
            if anom:
                ax_anom.bar([a[0] for a in anom], [a[1] for a in anom], width=25, alpha=0.6, color=COLORS.get(name), label=name.upper())
    ax.set_ylabel("index value")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, ncol=2)
    ax_anom.axhline(0, color="black", lw=0.8)
    ax_anom.set_ylabel("anomaly vs other years")
    ax_anom.grid(alpha=0.3)
    ax_anom.legend(fontsize=8)
    fig.tight_layout()
    png = _out(f"{tag}_vegetation.png")
    fig.savefig(png, dpi=120)
    plt.close(fig)

    csv_path = _out(f"{tag}_vegetation_scenes.csv")
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "valid_fraction", "cloud_cover", *indices])
        writer.writeheader()
        for s in scenes:
            writer.writerow({k: s.get(k) for k in writer.fieldnames})
    return {"chart_png": str(png), "scenes_csv": str(csv_path)}


def weather_chart(tag: str, rows: list[dict], title: str) -> dict:
    labels = [r["period"] + ("" if r.get("complete", True) else "*") for r in rows]
    fig, ax = plt.subplots(figsize=(11, 5))
    x = range(len(rows))
    ax.bar([i - 0.2 for i in x], [r["precip_mm"] for r in rows], width=0.4, color="#1e88e5", label="precipitation (mm)")
    ax.bar([i + 0.2 for i in x], [r["et0_mm"] for r in rows], width=0.4, color="#fb8c00", label="reference ET0 (mm)")
    ax.set_xticks(list(x), labels, rotation=60, fontsize=7)
    ax.set_ylabel("mm per period")
    ax.grid(alpha=0.3, axis="y")
    ax2 = ax.twinx()
    ax2.plot(list(x), [r.get("soil_moisture_0_7cm") for r in rows], "-o", ms=3, color="#6d4c41", label="soil moisture 0-7 cm")
    ax2.set_ylabel("soil moisture (m³/m³)")
    handles = ax.get_legend_handles_labels()
    handles2 = ax2.get_legend_handles_labels()
    ax.legend(handles[0] + handles2[0], handles[1] + handles2[1], fontsize=8, loc="upper left")
    ax.set_title(title + ("  (* = period only partly inside the date range)" if "*" in "".join(labels) else ""))
    fig.tight_layout()
    png = _out(f"{tag}_weather.png")
    fig.savefig(png, dpi=120)
    plt.close(fig)
    return {"chart_png": str(png)}


def comparison_chart(tag: str, areas: list[dict], title: str) -> dict:
    """Scores and drought impact per area, plus growing-season peak NDVI and water balance per year."""
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    names = [a["name"] for a in areas]
    x = range(len(areas))
    ax = axes[0]
    ax.bar([i - 0.2 for i in x], [a["score"] or 0 for a in areas], width=0.4, color="#2e7d32", label="suitability score")
    ax.bar(
        [i + 0.2 for i in x],
        [a["drought"]["drought_impact_index"] or 0 for a in areas],
        width=0.4,
        color="#c62828",
        label="drought impact index",
    )
    ax.set_xticks(list(x), names, rotation=20, fontsize=8)
    ax.set_ylim(0, 1.3)
    ax.set_title("Suitability vs drought impact (0-1)")
    ax.legend(fontsize=8, loc="upper center", ncol=2)
    ax.grid(alpha=0.3, axis="y")
    for a in areas:
        seasons = a["drought"]["growing_seasons"]
        ndvi = [(r["year"], r["peak_ndvi"]) for r in seasons if r["peak_ndvi"] is not None]
        wb = [(r["year"], r["water_balance_mm"]) for r in seasons if r["water_balance_mm"] is not None]
        if ndvi:
            axes[1].plot(*zip(*ndvi, strict=True), "-o", label=a["name"])
        if wb:
            axes[2].plot(*zip(*wb, strict=True), "-o", label=a["name"])
    axes[1].set_title("Growing-season peak NDVI (Sentinel-2)")
    axes[2].set_title("Growing-season water balance P-ET0 (mm, ERA5)")
    for ax in axes[1:]:
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        ax.xaxis.get_major_locator().set_params(integer=True)
    fig.suptitle(title)
    fig.tight_layout()
    png = _out(f"{tag}_comparison.png")
    fig.savefig(png, dpi=120)
    plt.close(fig)
    return {"chart_png": str(png)}


# ---------------------------------------------------------------------------
# Sentinel-2 maps
# ---------------------------------------------------------------------------

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LAYERS = {
    "ndvi": {"cmap": "RdYlGn", "vmin": -0.1, "vmax": 0.9, "label": "NDVI (greenness)"},
    "ndmi": {"cmap": "BrBG", "vmin": -0.4, "vmax": 0.5, "label": "NDMI (vegetation water)"},
    "dndvi": {"cmap": "RdBu", "vmin": -0.3, "vmax": 0.3, "label": "NDVI change vs mean of other years"},
}


def _rgb_stretch(composites: list[dict]) -> tuple[float, float]:
    """One 2-98% stretch for every true-colour panel, so years and areas look comparable."""
    import numpy as np

    vals = np.concatenate([c["rgb"][np.isfinite(c["rgb"])].ravel() for c in composites if np.isfinite(c["rgb"]).any()])
    if not vals.size:
        return 0.0, 0.3
    return float(np.percentile(vals, 2)), float(np.percentile(vals, 98))


def _show_rgb(ax, rgb, lo: float, hi: float) -> None:
    import numpy as np

    img = np.clip((rgb - lo) / (hi - lo), 0, 1) ** (1 / 1.2)
    img = np.where(np.isfinite(img), img, 0.85)  # masked pixels in light grey
    ax.imshow(img)


def _ndvi_change(per_year: dict, year: int):
    import numpy as np

    others = [c["ndvi"] for y, c in per_year.items() if y != year]
    if not others:
        return None
    with np.errstate(all="ignore"):
        return per_year[year]["ndvi"] - np.nanmean(np.stack(others), axis=0)


def _cmap(name: str):
    """Colormap with masked (cloudy / no data) pixels in light grey."""
    cmap = matplotlib.colormaps[name].copy()
    cmap.set_bad("#d9d9d9")
    return cmap


def _colorbar(fig, im, axes_column, key: str) -> None:
    fig.colorbar(im, ax=list(axes_column), orientation="horizontal", fraction=0.03, pad=0.02, label=LAYERS[key]["label"])


def area_maps(tag: str, name: str, composites: dict, resolution_m: int) -> dict:
    """Rows = years; columns = true colour, NDVI, NDMI and NDVI change vs the other years."""
    per_year = dict(sorted(composites["years"].items()))
    years = list(per_year)
    cols = ["rgb", "ndvi", "ndmi"] + (["dndvi"] if len(years) > 1 else [])
    fig, axes = plt.subplots(
        len(years), len(cols), figsize=(3.6 * len(cols), 3.4 * len(years) + 1.4), squeeze=False, layout="constrained"
    )
    lo, hi = _rgb_stretch(list(per_year.values()))
    last_im = {}
    for r, year in enumerate(years):
        c = per_year[year]
        for k, col in enumerate(cols):
            ax = axes[r][k]
            ax.set_xticks([])
            ax.set_yticks([])
            if col == "rgb":
                _show_rgb(ax, c["rgb"], lo, hi)
                ax.set_ylabel(f"{year}\n{len(c['dates'])} scene(s), {c['clear_fraction']:.0%} clear", fontsize=9)
            else:
                data = _ndvi_change(per_year, year) if col == "dndvi" else c[col]
                spec = LAYERS[col]
                last_im[col] = ax.imshow(data, cmap=_cmap(spec["cmap"]), vmin=spec["vmin"], vmax=spec["vmax"])
                if col != "dndvi" and c.get(f"{col}_mean") is not None:
                    ax.text(0.02, 0.03, f"mean {c[f'{col}_mean']:.2f}", transform=ax.transAxes, fontsize=8,
                            bbox={"facecolor": "white", "alpha": 0.7, "pad": 2})
            if r == 0:
                ax.set_title({"rgb": "True colour (B04-B03-B02)", "ndvi": "NDVI", "ndmi": "NDMI", "dndvi": "NDVI change"}[col])
    for col, im in last_im.items():
        _colorbar(fig, im, axes[:, cols.index(col)], col)
    fig.suptitle(
        f"{name} — Sentinel-2 L2A, {MONTHS[composites['month'] - 1]} median composites, {resolution_m} m "
        "(clouds masked, grey = no clear pixel)",
        fontsize=11,
    )
    if "dndvi" in cols:
        fig.text(
            0.5, -0.01,
            "NDVI change per field mostly shows crop rotation (e.g. harvested wheat vs green maize); "
            "a drought shows as a shift of the whole area (see the means).",
            ha="center", fontsize=8, style="italic",
        )
    png = _out(f"{tag}_maps.png")
    fig.savefig(png, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return {"maps_png": str(png)}


def compare_maps(tag: str, areas: list[tuple[str, dict]], layer: str) -> dict:
    """Rows = areas, columns = years, for one layer ("rgb", "ndvi", "ndmi" or "dndvi")."""
    years = sorted({y for _, comp in areas for y in comp["years"]})
    fig, axes = plt.subplots(
        len(areas), len(years), figsize=(3.4 * len(years), 3.3 * len(areas) + 1.2), squeeze=False, layout="constrained"
    )
    lo, hi = _rgb_stretch([c for _, comp in areas for c in comp["years"].values()])
    im = None
    for r, (name, comp) in enumerate(areas):
        per_year = dict(sorted(comp["years"].items()))
        for k, year in enumerate(years):
            ax = axes[r][k]
            ax.set_xticks([])
            ax.set_yticks([])
            if k == 0:
                ax.set_ylabel(name, fontsize=10)
            if r == 0:
                ax.set_title(str(year))
            c = per_year.get(year)
            if c is None:
                ax.text(0.5, 0.5, "no clear scene", ha="center", va="center", transform=ax.transAxes)
                continue
            if layer == "rgb":
                _show_rgb(ax, c["rgb"], lo, hi)
            else:
                data = _ndvi_change(per_year, year) if layer == "dndvi" else c[layer]
                spec = LAYERS[layer]
                im = ax.imshow(data, cmap=_cmap(spec["cmap"]), vmin=spec["vmin"], vmax=spec["vmax"])
                if layer != "dndvi" and c.get(f"{layer}_mean") is not None:
                    ax.text(0.02, 0.03, f"mean {c[f'{layer}_mean']:.2f}", transform=ax.transAxes, fontsize=8,
                            bbox={"facecolor": "white", "alpha": 0.7, "pad": 2})
    if im is not None:
        fig.colorbar(im, ax=axes.ravel().tolist(), orientation="horizontal", fraction=0.03, pad=0.02, label=LAYERS[layer]["label"])
    month = MONTHS[areas[0][1]["month"] - 1]
    title = {"rgb": "True colour", "ndvi": "NDVI", "ndmi": "NDMI", "dndvi": "NDVI change vs other years"}[layer]
    fig.suptitle(f"{title} — Sentinel-2 {month} median composites per year", fontsize=11)
    png = _out(f"{tag}_compare_{layer}.png")
    fig.savefig(png, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return {f"{layer}_png": str(png)}
