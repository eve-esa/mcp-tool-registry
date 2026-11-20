"""Atomic local cache and bounded, exportable per-run artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from pathlib import Path

from common import MaritimeError, runtime_root

ARTIFACT_MIME_TYPES = {
    ".csv": "text/csv",
    ".geojson": "application/geo+json",
    ".json": "application/json",
    ".png": "image/png",
}


def cache_root():
    root = Path(os.getenv("MARITIME_CACHE_DIR") or runtime_root() / ".cache/maritime")
    root.mkdir(parents=True, exist_ok=True)
    return root


def output_root():
    root = Path(os.getenv("MARITIME_OUTPUT_DIR") or runtime_root() / "outputs/maritime")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def json_write(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, allow_nan=False), encoding="utf-8"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run_dir():
    path = output_root() / uuid.uuid4().hex
    path.mkdir()
    return path


def manifest(directory):
    return [
        {
            "name": p.name,
            "path": str(p),
            "mime_type": ARTIFACT_MIME_TYPES[p.suffix],
            "size_bytes": p.stat().st_size,
            "sha256": checksum(p),
            "resource_uri": f"maritime://artifacts/{directory.name}/{p.name}",
        }
        for p in sorted(directory.iterdir())
        if p.is_file() and p.suffix in ARTIFACT_MIME_TYPES
    ]


def resource_path(run_id, filename):
    if not __import__("re").fullmatch(r"[a-f0-9]{32}", run_id):
        raise MaritimeError("invalid_request", "Invalid artifact run id.")
    path = (output_root() / run_id / filename).resolve()
    if path.parent != output_root() / run_id or not path.is_file():
        raise MaritimeError("invalid_request", "Unknown artifact.")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise MaritimeError("request_too_large", "Resource export exceeds 8 MB.")
    return path


def csv_write(path, rows, fields=None):
    fields = fields or (
        list(rows[0]) if rows else ["mmsi", "timestamp", "longitude", "latitude"]
    )
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        # Neutralise spreadsheet formulas in untrusted ship names/callsigns.
        for row in rows:
            writer.writerow(
                {
                    k: (
                        "'" + v
                        if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                        else v
                    )
                    for k, v in row.items()
                }
            )


def geojson(rows):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["longitude"], r["latitude"]],
                },
                "properties": {
                    k: v for k, v in r.items() if k not in ("longitude", "latitude")
                },
            }
            for r in rows
        ],
    }
