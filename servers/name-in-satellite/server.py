"""Name-in-satellite MCP server: spell text with letter-shaped places on Earth.

Letters come from NASA's "Your Name in Landsat"
(https://science.nasa.gov/specials/your-name-in-landsat/). Each letter is a real
place with known coordinates (see letters.json, built by build_letters.py).
The same places can be re-imaged with ESA Sentinel-2 L2A from Microsoft
Planetary Computer (no account needed).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import io
import json
import logging
import math
import os
import random
import re
import sys
import unicodedata
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as McpImage
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from shapely.geometry import box, shape
from shapely.ops import unary_union

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("name-in-satellite-mcp")

LETTERS: dict[str, dict] = json.loads(
    Path(__file__).with_name("letters.json").read_text()
)
# Hand-tuned rotation, centre shift and zoom for letters NASA cropped differently.
OVERRIDES: dict[str, dict] = json.loads(
    Path(__file__).with_name("letter_overrides.json").read_text()
)
PC_STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_DATA_BBOX = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox"
# Default: the folder that contains the hackathon repo, so generated PNGs stay out of git
# and out of a registry PR. Falls back to the home directory when deployed elsewhere.
_PARENTS = Path(__file__).resolve().parents
_DEFAULT_OUTPUT_DIR = (
    _PARENTS[3] if len(_PARENTS) > 3 else Path.home()
) / "name-in-satellite-outputs"
OUTPUT_DIR = Path(os.getenv("NAME_OUTPUT_DIR", _DEFAULT_OUTPUT_DIR)).resolve()
TILE_HEIGHT = 676  # Landsat letter tiles are ~282 x 676 px
# Second Sentinel-2 search when the requested window has no clear scene.
FALLBACK_WINDOW = ("2022-01-01", date.today().isoformat())
FALLBACK_MAX_CLOUD = 30.0
# Mean edge strength below which a Sentinel-2 tile shows no letter (e.g. open sea where
# NASA's letter was a passing sediment swirl). Measured on 15 variants, featureless vs
# letters: true colour <= 1.8 vs >= 2.6; false colour <= 1.0 vs >= 2.3.
MIN_STRUCTURE = {"true_color": 2.2, "false_color": 1.5}
TIMEOUT = 100

# Most NASA letters are natural colour; a few are false colour (NIR, red, green),
# where vegetation shows red.
S2_STYLES = {
    "true_color": "assets=visual",
    "false_color": "assets=B08&assets=B04&assets=B03&rescale=0,5000",
}

mcp = FastMCP("Name in satellite", host="0.0.0.0", port=8000, stateless_http=True)


def _variants(char: str) -> list[str]:
    return sorted(k for k, v in LETTERS.items() if v["letter"] == char)


def _bbox(lat: float, lon: float, width_km: float, height_km: float) -> list[float]:
    """West, south, east, north box centred on (lat, lon)."""
    # A degree of latitude is ~111 km. Longitude shrinks by cos(latitude).
    d_lat = height_km / 2 / 111.0
    d_lon = width_km / 2 / (111.0 * math.cos(math.radians(lat)))
    return [
        round(lon - d_lon, 5),
        round(lat - d_lat, 5),
        round(lon + d_lon, 5),
        round(lat + d_lat, 5),
    ]


async def _landsat_tile(
    client: httpx.AsyncClient, variant_id: str
) -> tuple[Image.Image, dict]:
    resp = await client.get(LETTERS[variant_id]["image_url"])
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB"), {}


async def _find_scene(
    client: httpx.AsyncClient,
    v: dict,
    bbox: list[float],
    start_date: str,
    end_date: str,
    max_cloud: float,
) -> list[dict]:
    """Items from the least cloudy single overpass that together cover the whole crop.

    A crop can straddle two Sentinel-2 tiles, or a swath edge; tiles from the
    same acquisition are then combined. Returns [] if no overpass covers it.
    """
    crop = box(*bbox)
    resp = await client.post(
        PC_STAC_SEARCH,
        json={
            "collections": ["sentinel-2-l2a"],
            "intersects": crop.__geo_interface__,
            "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": max_cloud}},
            "sortby": [{"field": "eo:cloud_cover", "direction": "asc"}],
            "limit": 50,
        },
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    # Features are sorted by cloud cover, so the first overpass that covers the crop is the clearest.
    for when in dict.fromkeys(f["properties"]["datetime"] for f in features):
        group = [f for f in features if f["properties"]["datetime"] == when]
        footprint = unary_union([shape(f["geometry"]) for f in group])
        if footprint.intersection(crop).area >= 0.995 * crop.area:
            return group
    return []


async def _sentinel2_tile(
    client: httpx.AsyncClient,
    variant_id: str,
    meters_per_pixel: float,
    start_date: str,
    end_date: str,
    max_cloud: float,
    style: str,
    allow_fallback: bool = True,
    tuning: dict | None = None,
) -> tuple[Image.Image, dict]:
    v = LETTERS[variant_id]
    # `tuning` replaces the letter_overrides.json entry (used to try out values).
    tuned = OVERRIDES.get(variant_id, {}) if tuning is None else tuning
    meters_per_pixel = tuned.get("meters_per_pixel", meters_per_pixel)
    rotate = tuned.get("rotate_deg", 0)
    east_km, north_km = tuned.get("shift_km", (0.0, 0.0))
    lat = v["lat"] + north_km / 111.0
    lon = v["lon"] + east_km / (111.0 * math.cos(math.radians(v["lat"])))
    width_km = v["width_px"] * meters_per_pixel / 1000
    height_km = v["height_px"] * meters_per_pixel / 1000
    size = (v["width_px"], v["height_px"])
    if rotate:
        # Fetch a square that still fills the tile after rotation, then crop its centre.
        width_km = height_km = math.hypot(width_km, height_km)
        side = round(width_km * 1000 / meters_per_pixel)
        fetch_size = (side, side)
    else:
        fetch_size = size
    bbox = _bbox(lat, lon, width_km, height_km)
    items = await _find_scene(client, v, bbox, start_date, end_date, max_cloud)
    if not items and not allow_fallback:
        raise LookupError(
            f"No clear Sentinel-2 scene between {start_date} and {end_date}."
        )
    fallback = not items
    if fallback:
        # Cloudy regions (e.g. the tropics) often have no clear scene in a short window.
        items = await _find_scene(
            client, v, bbox, *FALLBACK_WINDOW, max(max_cloud, FALLBACK_MAX_CLOUD)
        )
    if not items:
        raise LookupError(
            f"No Sentinel-2 overpass covering this place with cloud cover < "
            f"{max(max_cloud, FALLBACK_MAX_CLOUD)}% since {FALLBACK_WINDOW[0]}."
        )
    w, s, e, n = bbox
    # The PNGs are on a lat/lon grid; resizing to the metric pixel size restores the aspect
    # ratio. No-data is transparent, so tiles of one overpass are pasted on top of each other.
    img = Image.new("RGBA", fetch_size, "black")
    for item in items:
        resp = await client.get(
            f"{PC_DATA_BBOX}/{w},{s},{e},{n}.png?collection=sentinel-2-l2a&item={item['id']}"
            f"&{S2_STYLES[style]}&max_size={max(fetch_size)}"
        )
        resp.raise_for_status()
        img.alpha_composite(
            Image.open(io.BytesIO(resp.content)).convert("RGBA").resize(fetch_size)
        )
    if rotate:
        img = img.rotate(rotate, resample=Image.Resampling.BICUBIC)
        left, top = (fetch_size[0] - size[0]) // 2, (fetch_size[1] - size[1]) // 2
        img = img.crop((left, top, left + size[0], top + size[1]))
    meta = {
        "item_ids": [item["id"] for item in items],
        "datetime": items[0]["properties"]["datetime"],
        "cloud_cover": round(max(i["properties"]["eo:cloud_cover"] for i in items), 2),
        "bbox": bbox,
        "fallback_search": fallback,
        **({"hand_tuned": tuned} if tuned else {}),
    }
    return img.convert("RGB"), meta


def _font(size: int) -> tuple[ImageFont.FreeTypeFont, bool]:
    """Caption font, and whether it covers non-ASCII place names (e.g. "Breiðamerkurjökull").

    DejaVu Sans ships with matplotlib; Pillow's built-in font lacks such characters.
    """
    try:
        import matplotlib

        path = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"
        return ImageFont.truetype(str(path), size), True
    except (ImportError, OSError):
        return ImageFont.load_default(size=size), False


def _ascii(text: str) -> str:
    """Strip accents for fonts without them: "Breiðamerkurjökull" -> "Breidamerkurjokull"."""
    text = (
        text.replace("ð", "d").replace("Ð", "D").replace("þ", "th").replace("Þ", "Th")
    )
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _caption(letter: dict, src: str) -> list[str]:
    """Caption lines under a tile: place, variant id and coordinates, and the date.

    The variant id (e.g. "a_6") is the key to use in letter_overrides.json.
    """
    lat, lon = letter["lat"], letter["lon"]
    coords = f"{abs(lat):.2f}°{'N' if lat >= 0 else 'S'} {abs(lon):.2f}°{'E' if lon >= 0 else 'W'}"
    if src == "sentinel2":
        when = letter.get("datetime", "")[:10] or "no scene found"
    else:
        when = "Landsat (date not published)"
    return [letter["place"], f"{letter.get('variant_id', '')}  {coords}".strip(), when]


def _stitch(
    tiles: list[Image.Image | None], captions: list[list[str] | None]
) -> Image.Image:
    """Place tiles side by side at a common height, each with a caption below.

    None is a word gap.
    """
    gap, caption_height, font_size = 20, 130, 20
    font, unicode_ok = _font(font_size)
    scaled = [
        t.resize((round(t.width * TILE_HEIGHT / t.height), TILE_HEIGHT)) if t else None
        for t in tiles
    ]
    widths = [t.width if t else 140 for t in scaled]
    canvas = Image.new(
        "RGB",
        (sum(widths) + gap * (len(widths) + 1), TILE_HEIGHT + caption_height + 2 * gap),
        "black",
    )
    draw = ImageDraw.Draw(canvas)
    x = gap
    for t, w, caption in zip(scaled, widths, captions, strict=True):
        if t:
            canvas.paste(t, (x, gap))
        if caption:
            place, *rest = caption if unicode_ok else [_ascii(c) for c in caption]
            # Wrap the place name to the tile width, at most two lines.
            lines, line = [], ""
            for word in place.split():
                trial = f"{line} {word}".strip()
                if line and draw.textlength(trial, font=font) > w:
                    lines.append(line)
                    line = word
                else:
                    line = trial
            lines = ([*lines, line])[:2] + rest
            y = TILE_HEIGHT + gap + 8
            for text_line in lines:
                tx = x + (w - draw.textlength(text_line, font=font)) / 2
                draw.text((tx, y), text_line, fill="white", font=font)
                y += font_size + 6
        x += w + gap
    return canvas


def _render_strip(
    chars: list[str], picks: list[str], results: list, letters: list[dict], src: str
) -> Image.Image:
    """Captioned strip for one source. Adds each tile's metadata (or error) to `letters`."""
    tiles: list[Image.Image | None] = []
    captions: list[list[str] | None] = []
    it = iter(zip(picks, results, letters, strict=True))
    for c in chars:
        if c == " ":
            tiles.append(None)
            captions.append(None)
            continue
        variant_id, result, letter = next(it)
        if isinstance(result, BaseException):
            if not isinstance(result, (LookupError, httpx.HTTPError, OSError)):
                raise result
            letter[f"{src}_error"] = str(result)
            v = LETTERS[variant_id]
            tiles.append(Image.new("RGB", (v["width_px"], v["height_px"]), "black"))
        else:
            img, meta = result
            tiles.append(img)
            letter.update(meta)
        captions.append(_caption(letter, src))
    return _stitch(tiles, captions)


def _with_title(img: Image.Image, title: str) -> Image.Image:
    """Add a title band above an image."""
    font, unicode_ok = _font(32)
    title = title if unicode_ok else _ascii(title)
    out = Image.new("RGB", (img.width, img.height + 56), "black")
    out.paste(img, (0, 56))
    draw = ImageDraw.Draw(out)
    draw.text(
        ((img.width - draw.textlength(title, font=font)) / 2, 12),
        title,
        fill="white",
        font=font,
    )
    return out


def _structure(img: Image.Image) -> float:
    """Mean edge strength of a tile at 1/3 size; near 0 for featureless scenes."""
    gray = img.convert("L").resize((94, 225)).filter(ImageFilter.GaussianBlur(1))
    gy, gx = np.gradient(np.asarray(gray, dtype=float))
    return round(float(np.hypot(gx, gy).mean()), 2)


def _candidate_orders(chars: list[str], rng: random.Random) -> list[list[str]]:
    """Per character, the variants to try in order.

    Like the NASA site, repeated letters get different places ("aaa" gives three
    different a's) until a letter's variants run out.
    """
    shuffled = {
        c: rng.sample(_variants(c), len(_variants(c))) for c in sorted(set(chars))
    }
    seen: Counter[str] = Counter()
    orders = []
    for c in chars:
        variants = shuffled[c]
        k = seen[c] % len(variants)
        seen[c] += 1
        orders.append(variants[k:] + variants[:k])
    return orders


async def _first_visible_sentinel2(
    client: httpx.AsyncClient, order: list[str], *args: Any
) -> tuple[str, Image.Image, dict]:
    """Sentinel-2 tile of the first variant in `order` that shows structure.

    If none does, the variant with the most structure is used. Variants that
    were tried and rejected are listed under "skipped".
    """
    best: tuple[str, Image.Image, dict] | None = None
    skipped = []
    for variant_id in order:
        try:
            img, meta = await _sentinel2_tile(client, variant_id, *args)
        except LookupError as exc:
            skipped.append({"variant_id": variant_id, "reason": str(exc)})
            continue
        meta["structure"] = _structure(img)
        # Hand-tuned letters were checked by eye; args end with the style.
        if variant_id in OVERRIDES or meta["structure"] >= MIN_STRUCTURE[args[-1]]:
            return variant_id, img, {**meta, "skipped": skipped}
        skipped.append(
            {
                "variant_id": variant_id,
                "reason": f"featureless tile (structure {meta['structure']})",
            }
        )
        if best is None or meta["structure"] > best[2]["structure"]:
            best = (variant_id, img, meta)
    if best is None:
        raise LookupError(
            "; ".join(f"{s['variant_id']}: {s['reason']}" for s in skipped)
        )
    variant_id, img, meta = best
    skipped = [s for s in skipped if s["variant_id"] != variant_id]
    return variant_id, img, {**meta, "skipped": skipped, "low_structure": True}


def _image_content(path: Path) -> McpImage:
    """Compact image for an MCP response: still strips as JPEG (max 1600 px wide), GIFs as is."""
    if path.suffix == ".gif":
        return McpImage(data=path.read_bytes(), format="gif")
    img = Image.open(path).convert("RGB")
    if img.width > 1600:
        img = img.resize((1600, round(img.height * 1600 / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return McpImage(data=buf.getvalue(), format="jpeg")


def _tool_with_images(fn):
    """Register `fn` as an MCP tool with an extra `include_images` argument.

    `fn` returns JSON with file paths on the server's disk (how the notebook calls it
    directly). The MCP tool also returns the images themselves by default, so remote
    clients can show them. The EVE chat backend intercepts image content, stores it
    as an artifact, and gives the model a markdown link instead, so the images appear
    in the chat. Clients that pass images straight to a non-multimodal model (e.g.
    the hackathon notebook's agent with EVE-Instruct) must set include_images=false.
    """

    async def tool(*args: Any, include_images: bool = True, **kwargs: Any) -> Any:
        text = await fn(*args, **kwargs)
        result = json.loads(text)
        paths = [*result.get("images", {}).values(), result.get("gif_path")]
        if not include_images or "error" in result:
            return text
        return [text, *[_image_content(Path(p)) for p in paths if p]]

    signature = inspect.signature(fn)
    extra = inspect.Parameter(
        "include_images", inspect.Parameter.KEYWORD_ONLY, default=True, annotation=bool
    )
    tool.__signature__ = signature.replace(
        parameters=[*signature.parameters.values(), extra], return_annotation=Any
    )
    tool.__name__ = fn.__name__
    tool.__doc__ = (fn.__doc__ or "") + (
        "\n    include_images: also return the images themselves (default true), so the\n"
        "    chat can show them. When the result contains a markdown image line such as\n"
        "    ![name](/artifacts/...), put that line in your answer. Set false only if image\n"
        "    content causes an error (a client that sends images to a text-only model).\n"
    )
    mcp.tool(structured_output=False)(tool)
    return fn


@mcp.tool()
async def get_letter_locations(letter: str) -> str:
    """List the places on Earth that look like a given letter or digit.

    Each place comes from NASA's "Your Name in Landsat" collection: a real
    location whose shape, seen from a satellite, resembles the character.

    Args:
        letter: A single character, a-z or 0-9 (case-insensitive).

    Returns:
        JSON with one entry per variant: variant_id, place, lat, lon, and
        the URL of the NASA Landsat image.
    """
    char = letter.strip().lower()
    ids = _variants(char) if len(char) == 1 else []
    if not ids:
        return json.dumps(
            {"error": f"No satellite letter for '{letter}'. Supported: a-z and 0-9."}
        )
    return json.dumps(
        {
            "letter": char,
            "variants": [
                {
                    "variant_id": k,
                    **{f: LETTERS[k][f] for f in ("place", "lat", "lon", "image_url")},
                }
                for k in ids
            ],
        }
    )


@_tool_with_images
async def spell_name(
    text: str,
    source: str = "both",
    variant_ids: list[str] | None = None,
    start_date: str = "2025-05-01",
    end_date: str = "2025-09-30",
    max_cloud: float = 10.0,
    style: str = "true_color",
    meters_per_pixel: float = 40.0,
    seed: int | None = None,
) -> str:
    """Spell a word or name with satellite images of letter-shaped places on Earth.

    Every character is drawn as a real place (from NASA's "Your Name in
    Landsat") whose shape resembles that letter. One PNG per source is saved
    on disk. Call this once: source="both" (default) makes the Landsat and the
    Sentinel-2 image of the same places in a single call.

    Args:
        text:        Word or name to spell. Letters a-z, digits 0-9, and spaces.
        source:      "both" (default), "landsat" for the original NASA Landsat
                     images, or "sentinel2" to re-image the same places with
                     ESA Sentinel-2 L2A (Microsoft Planetary Computer).
        variant_ids: Optional list of variant ids, one per non-space character
                     (e.g. ["e_2", "s_1", "a_3"]), to reuse the same places
                     from an earlier call; these are used as given. Default:
                     a random variant per letter, without repeats, and for
                     Sentinel-2 the next variant is tried when a tile is
                     featureless (e.g. open sea).
        start_date:  Sentinel-2 only. Start of the search window, YYYY-MM-DD.
        end_date:    Sentinel-2 only. End of the search window, YYYY-MM-DD.
                     The least cloudy scene in the window is used. If none is
                     found, the tool searches 2022-today itself; no retry needed.
        max_cloud:   Sentinel-2 only. Maximum scene cloud cover in percent.
        style:       Sentinel-2 only. "true_color" (default, natural colours like
                     most Landsat letters) or "false_color" (NIR-red-green,
                     vegetation shows red).
        meters_per_pixel: Sentinel-2 only. Keep the default 40 (crop
                     ~11 x 27 km). Lower values zoom in, higher zoom out.
                     Letters in letter_overrides.json use their own hand-tuned
                     zoom, centre and rotation.
        seed:        Optional random seed to make the letter choice repeatable.

    Returns:
        JSON with images ({source: PNG path}) and, per letter, the variant_id,
        place, lat, lon, and for Sentinel-2 the scene id, acquisition datetime,
        cloud cover, bbox (west,south,east,north), fallback_search, structure
        (edge strength), and skipped (variants tried and rejected). A letter
        with no usable scene gets an error and a black tile; the other letters
        are still drawn.
    """
    chars = [c for c in text.lower().strip()]
    bad = sorted({c for c in chars if c != " " and not _variants(c)})
    if not chars or bad:
        return json.dumps(
            {
                "error": f"Cannot spell {bad or 'empty text'}. Supported: a-z, 0-9, and spaces."
            }
        )
    if source not in ("both", "landsat", "sentinel2"):
        return json.dumps({"error": "source must be 'both', 'landsat' or 'sentinel2'."})
    if style not in S2_STYLES:
        return json.dumps({"error": f"style must be one of {list(S2_STYLES)}."})
    if not 10 <= meters_per_pixel <= 100:
        return json.dumps(
            {"error": "meters_per_pixel must be between 10 and 100 (default 40)."}
        )

    letters_only = [c for c in chars if c != " "]
    if variant_ids:
        if len(variant_ids) != len(letters_only) or any(
            LETTERS.get(v, {}).get("letter") != c
            for v, c in zip(variant_ids, letters_only, strict=True)
        ):
            return json.dumps(
                {
                    "error": "variant_ids must give one valid id per non-space character, in order."
                }
            )
        orders = [[v] for v in variant_ids]
    else:
        orders = _candidate_orders(letters_only, random.Random(seed))

    sources = ["landsat", "sentinel2"] if source == "both" else [source]
    s2_args = (meters_per_pixel, start_date, end_date, max_cloud, style)
    results: dict[str, list] = {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            # Sentinel-2 first: it decides which variant each letter uses.
            picks = [order[0] for order in orders]
            if "sentinel2" in sources:
                # A letter without a usable scene must not sink the other letters.
                s2 = await asyncio.gather(
                    *[_first_visible_sentinel2(client, o, *s2_args) for o in orders],
                    return_exceptions=True,
                )
                picks = [
                    r[0] if isinstance(r, tuple) else p
                    for r, p in zip(s2, picks, strict=True)
                ]
                results["sentinel2"] = [
                    r[1:] if isinstance(r, tuple) else r for r in s2
                ]
            if "landsat" in sources:
                results["landsat"] = await asyncio.gather(
                    *[_landsat_tile(client, v) for v in picks], return_exceptions=True
                )
    except (httpx.HTTPError, OSError) as exc:
        return json.dumps({"error": f"Image retrieval failed: {exc}"})
    logger.info("Spelled %r from %s with %s", text, sources, picks)

    letters_out: list[dict[str, Any]] = [
        {
            "char": LETTERS[v]["letter"],
            "variant_id": v,
            "place": LETTERS[v]["place"],
            "lat": LETTERS[v]["lat"],
            "lon": LETTERS[v]["lon"],
        }
        for v in picks
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    images = {}
    for src in sources:
        path = OUTPUT_DIR / f"name_{slug}_{src}.png"
        _render_strip(chars, picks, results[src], letters_out, src).save(path)
        images[src] = str(path)

    return json.dumps({"text": text, "images": images, "letters": letters_out})


async def preview_rotations(
    variant_id: str,
    angles: tuple[float, ...] = (0, 45, 90, 135, 180, 225, 270, 315),
    meters_per_pixel: float | None = None,
    shift_km: tuple[float, float] | None = None,
    style: str = "true_color",
) -> str:
    """Helper for letter_overrides.json (not an MCP tool).

    Draws NASA's letter next to the Sentinel-2 crop at several rotations, so
    you can pick the values that match. meters_per_pixel and shift_km
    ([east, north] km) default to the letter's saved override, else 40 and no
    shift. Nothing is saved. Returns the PNG path.
    """
    trial = dict(OVERRIDES.get(variant_id, {}))
    if meters_per_pixel is not None:
        trial["meters_per_pixel"] = meters_per_pixel
    if shift_km is not None:
        trial["shift_km"] = list(shift_km)
    mpp = trial.setdefault("meters_per_pixel", 40.0)
    shift = trial.get("shift_km", [0, 0])
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        landsat, _ = await _landsat_tile(client, variant_id)
        tiles = await asyncio.gather(
            *[
                _sentinel2_tile(
                    client,
                    variant_id,
                    mpp,
                    "2025-05-01",
                    "2025-09-30",
                    10.0,
                    style,
                    tuning={**trial, "rotate_deg": angle},
                )
                for angle in angles
            ]
        )
    captions = [[f"{variant_id} NASA original"]] + [
        [f"rotate_deg {a:g}", f"mpp {mpp:g}  shift {shift[0]:g},{shift[1]:g} km"]
        for a in angles
    ]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"rotations_{variant_id}.png"
    _stitch([landsat, *[img for img, _ in tiles]], captions).save(path)
    return str(path)


@_tool_with_images
async def spell_name_timeseries(
    text: str,
    variant_ids: list[str] | None = None,
    start_date: str = "2025-01-01",
    end_date: str = "2025-12-31",
    frames: int = 6,
    max_cloud: float = 20.0,
    style: str = "true_color",
    meters_per_pixel: float = 40.0,
    frame_seconds: float = 1.0,
    seed: int | None = None,
) -> str:
    """Animated GIF of a spelled word through time, from Sentinel-2.

    Spells the text with letter-shaped places (as spell_name does), splits
    start_date..end_date into equal periods, and draws each period's least
    cloudy Sentinel-2 scene per letter. Each frame is one period, so the GIF
    shows seasons, floods, snow, or river changes at the same places.

    Args:
        text:        Word or name to spell. Letters a-z, digits 0-9, and spaces.
        variant_ids: Optional variant ids, one per non-space character, e.g.
                     from an earlier spell_name call, to reuse the same places.
        start_date:  Start of the series, YYYY-MM-DD.
        end_date:    End of the series, YYYY-MM-DD.
        frames:      Number of equal periods (2-24); e.g. 12 over a year is
                     monthly. A letter with no clear scene in a period shows
                     a black tile for that frame.
        max_cloud:   Maximum scene cloud cover in percent.
        style:       "true_color" (default) or "false_color".
        meters_per_pixel: Zoom, as in spell_name (default 40).
        frame_seconds: Display time of each frame in the GIF.
        seed:        Optional random seed to make the letter choice repeatable.

    Returns:
        JSON with gif_path, the places used, and per frame the period and
        each letter's scene date (or null if no clear scene was found).
    """
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        return json.dumps({"error": "start_date and end_date must be YYYY-MM-DD."})
    if end <= start:
        return json.dumps({"error": "end_date must be after start_date."})
    if not 2 <= frames <= 24:
        return json.dumps({"error": "frames must be between 2 and 24."})

    # Pick and check the places once over the whole window.
    first = json.loads(
        await spell_name(
            text,
            source="sentinel2",
            variant_ids=variant_ids,
            start_date=start_date,
            end_date=end_date,
            max_cloud=max_cloud,
            style=style,
            meters_per_pixel=meters_per_pixel,
            seed=seed,
        )
    )
    if "error" in first:
        return json.dumps(first)
    picks = [letter["variant_id"] for letter in first["letters"]]
    chars = list(text.lower().strip())

    span = (end - start).days + 1
    edges = [
        start + timedelta(days=round(k * span / frames)) for k in range(frames + 1)
    ]
    periods = [(edges[k], edges[k + 1] - timedelta(days=1)) for k in range(frames)]

    limit = asyncio.Semaphore(8)  # be polite to Planetary Computer

    async def tile(client: httpx.AsyncClient, variant_id: str, p0: date, p1: date):
        async with limit:
            return await _sentinel2_tile(
                client,
                variant_id,
                meters_per_pixel,
                p0.isoformat(),
                p1.isoformat(),
                max_cloud,
                style,
                allow_fallback=False,
            )

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            # All periods at once; the semaphore caps concurrent requests.
            per_period = await asyncio.gather(
                *[
                    asyncio.gather(
                        *[tile(client, v, p0, p1) for v in picks],
                        return_exceptions=True,
                    )
                    for p0, p1 in periods
                ]
            )
    except (httpx.HTTPError, OSError) as exc:
        return json.dumps({"error": f"Image retrieval failed: {exc}"})

    images, frames_out = [], []
    for (p0, p1), results in zip(periods, per_period, strict=True):
        letters = [
            {
                "char": LETTERS[v]["letter"],
                "variant_id": v,
                **{k: LETTERS[v][k] for k in ("place", "lat", "lon")},
            }
            for v in picks
        ]
        strip = _render_strip(chars, picks, results, letters, "sentinel2")
        images.append(
            _with_title(strip, f"Sentinel-2  {p0.isoformat()} to {p1.isoformat()}")
        )
        frames_out.append(
            {
                "period": [p0.isoformat(), p1.isoformat()],
                "dates": [
                    (letter.get("datetime") or "")[:10] or None for letter in letters
                ],
            }
        )

    # Half size keeps the GIF small enough to view and share.
    images = [im.resize((im.width // 2, im.height // 2)) for im in images]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    path = OUTPUT_DIR / f"name_{slug}_timeseries.gif"
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=round(frame_seconds * 1000),
        loop=0,
    )
    places = [{"variant_id": v, "place": LETTERS[v]["place"]} for v in picks]
    return json.dumps(
        {"text": text, "gif_path": str(path), "places": places, "frames": frames_out}
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Name-in-satellite MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
        help="http (default) serves streamable-http on port 8000, as AgentCore needs; stdio for local MCP clients",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP port (default 8000)",
    )
    args = parser.parse_args()
    if args.port != mcp.settings.port:
        mcp.settings.port = args.port
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
