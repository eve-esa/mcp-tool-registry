"""Build letters.json from NASA's "Your Name in Landsat" page.

The site has no data file: each letter variant (e.g. "a_1") is hard-coded in
assets/main.min.js with a place name, a source link, and DMS coordinates.
Run once: python build_letters.py
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import httpx
from PIL import Image

SITE = "https://science.nasa.gov/specials/your-name-in-landsat"
OUT = Path(__file__).with_name("letters.json")

# Each variant starts with: "a_1"==x.alt&&(locationTitle.innerHTML="...",...
MARKER_RE = re.compile(r'"([a-z0-9]_\d+)"==x\.alt&&\(')
# JS string literal, allowing escaped quotes inside.
STR = r'"((?:[^"\\]|\\.)*)"'
TITLE_RE = re.compile(r"locationTitle\.innerHTML=" + STR)
TITLE_HREF_RE = re.compile(r"locationTitle\.href=" + STR)
COORD_RE = re.compile(r"locationCoordinates\.innerHTML=" + STR)
# 36°35'20.8 N  /  62°38'51.6\"N  /  39°19'00.0\" N
DMS_RE = re.compile(r"(\d+)°(\d+)'([\d.]+)\\?\"?\s*([NSEW])")


def dms_to_decimal(text: str) -> tuple[float, float] | None:
    parts = DMS_RE.findall(text)
    if len(parts) != 2:
        return None
    values = []
    for deg, minutes, sec, hemi in parts:
        v = int(deg) + int(minutes) / 60 + float(sec) / 3600
        values.append(-v if hemi in "SW" else v)
    return round(values[0], 5), round(values[1], 5)


def main() -> None:
    js = httpx.get(f"{SITE}/assets/main.min.js", timeout=60, follow_redirects=True).text
    letters: dict[str, dict] = {}
    markers = list(MARKER_RE.finditer(js))
    for i, m in enumerate(markers):
        variant_id = m.group(1)
        if variant_id in letters:
            continue  # the same id can appear again elsewhere in the script
        end = markers[i + 1].start() if i + 1 < len(markers) else len(js)
        body = js[m.end() : end]
        title = TITLE_RE.search(body)
        coords = COORD_RE.search(body)
        latlon = dms_to_decimal(coords.group(1)) if coords else None
        if not title or not latlon:
            print(f"skip {variant_id}: could not parse")
            continue
        href = TITLE_HREF_RE.search(body)
        # Pixel size of the Landsat crop: at 30 m/px it gives the ground footprint.
        img = Image.open(
            io.BytesIO(httpx.get(f"{SITE}/images/{variant_id}.jpg", timeout=60).content)
        )
        letters[variant_id] = {
            "letter": variant_id.split("_")[0],
            "place": title.group(1),
            "lat": latlon[0],
            "lon": latlon[1],
            "image_url": f"{SITE}/images/{variant_id}.jpg",
            "source_url": href.group(1) if href else "",
            "width_px": img.width,
            "height_px": img.height,
        }
    OUT.write_text(json.dumps(letters, indent=1, ensure_ascii=False))
    print(f"wrote {len(letters)} variants to {OUT}")


if __name__ == "__main__":
    main()
