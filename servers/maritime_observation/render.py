"""PNG maps/charts and portable CSV/GeoJSON/provenance artifacts."""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from scene import read_crop
from storage import csv_write, geojson, json_write, manifest, run_dir


def positions_artifacts(records, bbox, result, scene=None, highlight=60):
    directory = run_dir()
    csv_write(directory / "ais-reports.csv", records)
    json_write(directory / "ais-reports.geojson", geojson(records))
    fig, ax = plt.subplots(figsize=(10, 7))
    try:
        if scene:
            rgb, extent, project, crs = read_crop(scene["visual_url"], bbox)
            ax.imshow(rgb, extent=[extent[0], extent[2], extent[1], extent[3]])
            xs, ys = project.transform(
                [r["longitude"] for r in records], [r["latitude"] for r in records]
            )
            colours = [
                "#facc15" if abs(r["delta_seconds"]) <= highlight else "#22d3ee"
                for r in records
            ]
            ax.scatter(
                xs, ys, edgecolors=colours, s=35, facecolors="none", linewidths=1
            )
            ax.set(
                xlim=(extent[0], extent[2]),
                ylim=(extent[1], extent[3]),
                xlabel=f"Easting ({crs})",
                ylabel="Northing (m)",
            )
            ax.set_title(
                f"AIS reports over Sentinel-2 • {scene['reference_time']}\nYellow ≤ {highlight}s; cyan = remaining selected reports"
            )
        else:
            ax.scatter(
                [r["longitude"] for r in records],
                [r["latitude"] for r in records],
                s=14,
                c="#0891b2",
            )
            ax.set(
                xlim=(bbox[0], bbox[2]),
                ylim=(bbox[1], bbox[3]),
                xlabel="Longitude",
                ylabel="Latitude",
            )
            ax.set_title(
                "AIS reported positions • latest selected report per MMSI\nCoordinate plot; no satellite background"
            )
            ax.grid(alpha=0.3)
        fig.text(
            0.5,
            0.01,
            "AIS reports are not independently confirmed image detections. Coverage and timestamps limit interpretation.",
            ha="center",
            fontsize=8,
        )
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        fig.savefig(
            directory / ("scene-overlay.png" if scene else "ais-map.png"), dpi=140
        )
    finally:
        plt.close(fig)
    json_write(directory / "provenance.json", result)
    return manifest(directory)


def summary_artifacts(result, labels, values, title):
    directory = run_dir()
    csv_write(
        directory / "summary.csv",
        [
            {"label": label, "value": value}
            for label, value in zip(labels, values, strict=True)
        ],
        ["label", "value"],
    )
    json_write(directory / "provenance.json", result)
    fig, ax = plt.subplots(figsize=(8, 5))
    try:
        ax.bar(labels, values, color="#0891b2")
        ax.set_title(title)
        ax.tick_params(axis="x", labelrotation=20)
        fig.text(
            0.5,
            0.015,
            "Powered by Global Fishing Watch. https://globalfishingwatch.org/",
            ha="center",
            fontsize=8,
            url="https://globalfishingwatch.org/",
        )
        fig.tight_layout(rect=(0, 0.05, 1, 1))
        fig.savefig(directory / "summary.png", dpi=140)
    finally:
        plt.close(fig)
    return manifest(directory)
