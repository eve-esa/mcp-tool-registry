"""
Sentinel Hub Processing API — per-collection fetch tools.

Each public function corresponds to one satellite collection and builds the
correct evalscript + dataFilter for that sensor.  A shared _fetch() helper
handles auth, the HTTP POST, and response packaging.

Collections / API type strings
--------------------------------
Sentinel-1 GRD      → "sentinel-1-grd"
Sentinel-2 L1C      → "sentinel-2-l1c"
Sentinel-2 L2A      → "sentinel-2-l2a"
Sentinel-3 OLCI     → "sentinel-3-olci"
Sentinel-3 SLSTR    → "sentinel-3-slstr"
Sentinel-3 SYN L2   → "sentinel-3-synergy-l2"
Sentinel-5P L2      → "sentinel-5p-l2"
Landsat OT L1       → "landsat-ot-l1"
DEM                 → "dem"
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
import time
from typing import Optional
from oauthlib.oauth2 import BackendApplicationClient
from requests_oauthlib import OAuth2Session

import requests

logger = logging.getLogger(__name__)

import os, requests


_PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"

def _get_token() -> str:

    client_id =  os.environ.get("CDSE_CLIENT_ID", "")
    client_secret =  os.environ.get("CDSE_CLIENT_SECRET", "")
    client = BackendApplicationClient(client_id=client_id)
    oauth = OAuth2Session(client=client)
    token = oauth.fetch_token(
            token_url='https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token',
            client_secret=client_secret,
            include_client_id=True
            )
    return token['access_token']

# ---------------------------------------------------------------------------
# Shared HTTP fetch helper
# ---------------------------------------------------------------------------

def _fetch(
    *,
    collection_type: str,
    evalscript: str,
    bbox: list[float],
    start_date: str,
    end_date: str,
    width: int = 512,
    height: int = 512,
    data_filter_extra: Optional[dict] = None,
    processing_options: Optional[dict] = None,
    save_path: Optional[str] = None
) -> dict:
    """
    Low-level POST to the Sentinel Hub Process API.

    Returns a dict with keys:
        image_b64, media_type, width, height, collection,
        evalscript, saved_path
    """
    data_filter: dict = {
        "timeRange": {
            "from": start_date + "T00:00:00Z",
            "to":   end_date   + "T23:59:59Z",
        }
    }
    if data_filter_extra:
        data_filter.update(data_filter_extra)

    data_entry: dict = {
        "type": collection_type,
        "dataFilter": data_filter,
    }
    if collection_type == "sentinel-3-slstr":
        data_filter["orbitDirection"] = "DESCENDING"
    if processing_options:
        data_entry["processing"] = processing_options

    body = {
        "input": {
            "bounds": {
                "bbox": bbox,
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84" },
            },
            "data": [data_entry],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [
                {"identifier": "default", "format": {"type": "image/jpeg"}}
            ],
        },
        "evalscript": evalscript,
    }

    token = _get_token()
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "image/jpeg",
    }

    logger.info(
        "Fetching collection=%s  bbox=%s  %dx%d px",
        collection_type, bbox, width, height,
    )
    resp = requests.post(_PROCESS_URL, headers=headers, json=body, timeout=90)

    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:600]
        raise RuntimeError(
            f"Sentinel Hub API error {resp.status_code}: {detail}"
        )

    image_bytes = resp.content

    # Actual dimensions via Pillow
    actual_w, actual_h = width, height
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image_bytes))
        actual_w, actual_h = img.size
    except Exception:
        pass

    # Optionally save to disk
    saved_path: Optional[str] = None
    if save_path:
        import pathlib
        p = pathlib.Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(image_bytes)
        saved_path = str(p.resolve())
        logger.info("Saved to %s", saved_path)

    return {
        "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        "media_type": "image/jpeg",
        "width": actual_w,
        "height": actual_h,
        "collection": collection_type,
        "evalscript": str(body),
        "saved_path": saved_path,
    }

def validate_bands(query_bands: list[str], true_bands: list[str]) -> bool:
    return set(query_bands).issubset(true_bands)

# ---------------------------------------------------------------------------
# Evalscript builders (internal)
# ---------------------------------------------------------------------------

def _es_rgb(r: str, g: str, b: str, gain: float = 2.5) -> str:
    """Generic 3-band RGB evalscript."""
    return (
        "//VERSION=3\n"
        f'function setup(){{return{{input:["{r}","{g}","{b}"],output:{{bands:3,sampleType:"AUTO"}}}}}}\n'
        f"function evaluatePixel(s){{return[{gain}*s.{r},{gain}*s.{g},{gain}*s.{b}]}}"
    )




# ---------------------------------------------------------------------------
# ── SENTINEL-1 GRD ──────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

# Available polarisations depend on the acquisition mode:
#   IW/EW dual-pol: VV+VH  or  HH+HV
#   Single-pol:     VV     or  HH
_S1_POLS = ["VV", "VH", "HH", "HV"]


def _es_s1_custom(bands: list[str]) -> str:
    """Single-polarisation dB greyscale (–20 dB → 0 dB range)."""
    bands_js = ", ".join(f'"{b}"' for b in bands)

    # Build one rectified sample per band, then average them
    rectified = " + ".join(f"Math.max(0, Math.log(s.{b}) * 0.21714724095 + 1)" for b in bands)
    if len(bands) > 1:
        pixel_expr = f"({rectified}) / {len(bands)}"
    else:
        pixel_expr = rectified

    return (
        "//VERSION=3\n"
        f'function setup(){{return{{input:[{bands_js}],output:{{bands:1,sampleType:"AUTO"}}}}}}\n'
        "function evaluatePixel(s){\n"
        f"  return[{pixel_expr}]\n"
        "}"
    )

def _es_s1_pseudo_rgb(pol_a: str, pol_b: str) -> str:
    """
    Pseudo-RGB from two polarisations.
    R = pol_a dB,  G = pol_b dB,  B = pol_a/pol_b ratio /10
    (mirrors the official S1GRD VV/VH composite example).
    """
    pa, pb = pol_a.upper(), pol_b.upper()
    return (
        "//VERSION=3\n"
        f'function setup(){{return{{input:["{pa}","{pb}"],output:{{bands:3,sampleType:"AUTO"}}}}}}\n'
        "function toDb(v){return Math.max(0,Math.log(v)*0.21714724095+1)}\n"
        "function evaluatePixel(s){\n"
        f"  var a=toDb(s.{pa}),b=toDb(s.{pb});\n"
        f"  return[a,b,a/b/10]\n"
        "}"
    )


def fetch_sentinel1_grd(
    bbox: list[float],
    start_date: str,
    end_date: str,
    *,
    # preset OR custom bands — one must be supplied
    preset: Optional[str] = "vv_vh_rgb",   
    custom_bands: Optional[list[str]] = None,  # e.g. ["VV"] or ["VV","VH"]
    evalscript: Optional[str] = None,
    # S1-specific processing options
    orthorectify: bool = True,
    back_coeff: str = "GAMMA0_ELLIPSOID",  # GAMMA0_ELLIPSOID | GAMMA0_TERRAIN | SIGMA0_ELLIPSOID
    dem_instance: Optional[str] = None,    # e.g. "COPERNICUS_30"
    acquisition_mode: Optional[str] = None,  # "IW" | "EW" | "SM" | "WV"
    polarization: Optional[str] = None,      # "DV" | "DH" | "SV" | "SH"
    orbit_direction: Optional[str] = None,   # "ASCENDING" | "DESCENDING"
    resolution: Optional[str] = None,        # "HIGH" | "MEDIUM"
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    """
    Fetch a Sentinel-1 GRD image from the Sentinel Hub Process API.

    Presets
    -------
    "vv_vh_rgb"  – Pseudo-RGB: R=VV, G=VH, B=VV/VH/10  (IW dual-pol)
    "hh_hv_rgb"  – Pseudo-RGB: R=HH, G=HV, B=HH/HV/10  (EW dual-pol)

    Custom bands
    ------------
    Pass custom_bands=["VV"] or custom_bands=["VV","VH"] to build an
    evalscript automatically, or supply your own via evalscript=.

    Processing options
    ------------------
    orthorectify  : apply geometric terrain correction (default True)
    back_coeff    : backscatter coefficient — GAMMA0_ELLIPSOID (default),
                    GAMMA0_TERRAIN (RTC), or SIGMA0_ELLIPSOID
    dem_instance  : DEM used for orthorectification, e.g. "COPERNICUS_30"
    acquisition_mode, polarization, orbit_direction, resolution :
                    filter the input scenes (see Sentinel Hub docs).
    """
    # ---- resolve evalscript ----
    if evalscript is None:
        if custom_bands:
            if validate_bands(custom_bands, _S1_POLS):
                evalscript = _es_s1_custom(custom_bands)
            else:
                raise ValueError(f"Unidentifed band in Sentinel-1, Choose from the followinfg bands : {_S1_POLS}")
        elif preset:
            p = preset.lower()
            if p == "vv_vh_rgb":
                evalscript = _es_s1_pseudo_rgb("VV", "VH")
            elif p == "hh_hv_rgb":
                evalscript = _es_s1_pseudo_rgb("HH", "HV")
            else:
                # Fallback to custom bands
                if validate_bands(custom_bands, _S1_POLS):
                    evalscript = _es_s1_custom(custom_bands)
                else:
                    raise ValueError(f"Unidentifed band in Sentinel-1, Choose from the followinfg bands : {_S1_POLS}")
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    # ---- build dataFilter extras ----
    df_extra: dict = {}
    if acquisition_mode:
        df_extra["acquisitionMode"] = acquisition_mode.upper()
    if polarization:
        df_extra["polarization"] = polarization.upper()
    if orbit_direction:
        df_extra["orbitDirection"] = orbit_direction.upper()
    if resolution:
        df_extra["resolution"] = resolution.upper()

    # ---- build processing options ----
    proc: dict = {"orthorectify": str(orthorectify).lower()}
    if back_coeff:
        proc["backCoeff"] = back_coeff.upper()
    if dem_instance:
        proc["demInstance"] = dem_instance.upper()

    return _fetch(
        collection_type="sentinel-1-grd",
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        data_filter_extra=df_extra or None,
        processing_options=proc,
        save_path=save_path
    )


# ---------------------------------------------------------------------------
# ── SENTINEL-2 L1C ──────────────────────────────────────────────────────────
# Available bands: B01–B12, B8A
# ---------------------------------------------------------------------------

_S2_BANDS = (
    "B01", "B02", "B03", "B04", "B05", "B06",
    "B07", "B08", "B8A", "B09", "B10", "B11", "B12",
)

def _es_s2_ndvi(nir: str = "B08", r: str = "B04") -> str:
    return (
        "//VERSION=3\n"
        "function setup(){return{"
        f'input:[{{bands:["{r}","{nir}"],units:"REFLECTANCE"}}],'
        'output:{id:"default",bands:1,sampleType:SampleType.AUTO}'
        "}}\n"
        f"function evaluatePixel(s){{return[(s.{nir}-s.{r})/(s.{nir}+s.{r})]}}"
    )
def _es_s2_custom_bands(bands: list[str]) -> str:
    n = len(bands)
    bands_js = ", ".join(f'"{b}"' for b in bands)
    returns = ",".join(f"s.{b}" for b in bands)
    return (
        "//VERSION=3\n"
        f'function setup(){{return{{input:[{bands_js}],output:{{bands:{n},sampleType:"AUTO"}}}}}}\n'
        f"function evaluatePixel(s){{return[{returns}]}}"
    )

def fectch_sentinel2(
    bbox: list[float],
    start_date: str,
    end_date: str,
    collection: Optional[str] = 'L2A',
    *,
    preset: Optional[str] = "true_color",       
    custom_bands: Optional[list[str]] = None,
    evalscript: Optional[str] = None,
    max_cloud_cover: Optional[float] = None,
    mosaicking_order: str = "leastCC",
    harmonize_values: bool = False,
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    
    """
    Fetch a Sentinel-2 L1C/L2A images.

    Presets
    -------
    "true_color"  – Natural colour RGB: R=B04, G=B03, B=B02,
    "ndvi" - vegetation index : (NIR - Red) / (NIR + Red)

    """
    if evalscript is None:
        if custom_bands:
            if validate_bands(custom_bands, _S2_BANDS):
                evalscript = _es_s2_custom_bands(custom_bands)
            else:
                raise ValueError(f"Unidentifed band in Sentinel-2, Choose from the followinfg bands : {_S2_BANDS}")
        elif preset:
            p = preset.lower()
            if p == "true_color":
                evalscript = _es_rgb("B04", "B03", "B02")
            elif p == "ndvi":
                 evalscript = _es_s2_ndvi("B08", "B04")

            else:
                if validate_bands(custom_bands, _S2_BANDS):
                    evalscript = _es_s2_custom_bands(custom_bands)
                else:
                    raise ValueError(f"Unidentifed band in Sentinel-2, Choose from the followinfg bands : {_S2_BANDS}")
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    df_extra: dict = {"mosaickingOrder": mosaicking_order}
    if max_cloud_cover is not None:
        df_extra["maxCloudCoverage"] = max_cloud_cover

    proc: dict = {"harmonizeValues": str(harmonize_values).lower()}
    collection_type = "sentinel-2-l2a" if collection == "L2A" else "sentinel-2-l1c"
    return _fetch(
        collection_type=collection_type,
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        data_filter_extra=df_extra,
        processing_options=proc,
        save_path=save_path
    )


_S3_OLCI_BANDS = ["B01","B02","B03","B04","B05","B06","B07","B08","B09","B10","B11",
          "B12","B13","B14","B15","B16","B17","B18","B19","B20","B21"]
def _es_s3_otci(h: str = "B10", m: str = "B11", l: str = "B12") -> str:
    return (
        "//VERSION=3\n"
        f'function setup(){{return{{input:[{{bands:["{h}","{m}","{l}"]}}],output:{{id:"default",bands:3,sampleType:"AUTO"}}}}}}\n'
        "var cm=new ColorMapVisualizer([[0,[0,0,0.5]],[1,[0,0.3,0.8]],[1.8,[1,0.2,0.2]],[2.5,[1,0.9,0]],[4,[0,0.8,0.1]],[4.5,[0,0.6,0.2]],[5,[1,1,1]]]);\n"
        f"function evaluatePixel(s){{let OTCI=(s.{l}-s.{m})/(s.{m}-s.{h});return cm.process(OTCI)}}"
    )
# def _es_s3_custom_bands(bands: list[str]) -> str:
#     n = len(bands)
#     bands_js = ", ".join(f'"{b}"' for b in bands)
#     returns = ",".join(f"s.{b}" for b in bands)
#     return (
#         "//VERSION=3\n"
#         f'function setup(){{return{{input:[{bands_js}], units:"REFLECTANCE,output:{{bands:{n},sampleType:"AUTO"}}}}}}\n'
#         f"function evaluatePixel(s){{return[{returns}]}}"
#     )
def _es_s3_custom_bands(bands: list[str]) -> str:
    n = len(bands)
    bands_js = ", ".join(f'"{b}"' for b in bands)
    returns = ", ".join(f"s.{b}" for b in bands)

    return (
        "//VERSION=3\n"
        "function setup(){return{"
        f'input:[{{bands:[{bands_js}],units:"REFLECTANCE"}}],'
        f'output:{{bands:{n},sampleType:SampleType.AUTO}}'
        "}}\n"
        f"function evaluatePixel(s){{return[{returns}]}}"
    )
# ---------------------------------------------------------------------------
# ── SENTINEL-3 OLCI ─────────────────────────────────────────────────────────
# 21 bands: B01–B21 (400–1020 nm)
# True-colour approximation: B08 (665 nm/red), B06 (560 nm/green), B04 (490 nm/blue)
# ---------------------------------------------------------------------------

def fetch_sentinel3_olci(
    bbox: list[float],
    start_date: str,
    end_date: str,
    *,
    preset: Optional[str] = "true_color",    # "otci"    
    custom_bands: Optional[list[str]] = None,
    evalscript: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    """
    Fetch a Sentinel-3 OLCI (Ocean and Land Colour Instrument) image.
    300 m resolution ocean/land colour sensor.

    Presets
    -------
    "true_color"  – Approximate RGB using B08 (red), B06 (green), B04 (blue)

    Custom bands
    ------------
    21 spectral bands: B01 (400 nm) … B21 (1020 nm), plus quality bands.
    custom_bands=["B17","B08","B04"]  → NIR false colour
    """
    if evalscript is None:
        if custom_bands:
            if validate_bands(custom_bands, _S3_OLCI_BANDS):
                evalscript = _es_s3_custom_bands(custom_bands)
            else:
                raise ValueError(f"Unidentifed band in Sentinel-3 OLCI, Choose from the followinfg bands : {_S3_OLCI_BANDS}")
        elif preset:
            p = preset.lower()
            if p == "true_color":
                evalscript = _es_rgb("B08", "B06", "B04")
            elif p == "otci":
                evalscript = _es_s3_otci("B10", "B11", "B12")
            else:
                raise ValueError(
                    f"Unknown S3 OLCI preset '{preset}'. Choose: true_color"
                )
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    return _fetch(
        collection_type="sentinel-3-olci",
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        save_path=save_path
    )


# ---------------------------------------------------------------------------
# ── SENTINEL-3 SLSTR ────────────────────────────────────────────────────────
# Dual-view (nadir + oblique) thermal and optical sensor.
# Nadir bands: S1–S6 (0.55–1.64 µm), S7–S9 + F1–F2 (thermal).
# True colour approximation: S3 (1.375 µm), S2 (0.659 µm), S1 (0.555 µm)
#   → adjusted gain since S-bands are not standard visible
# Better visible RGB: S3/S2/S1 with gain ~3.5 for approximate true colour.
# ---------------------------------------------------------------------------
_S3_SLSTR_BANDS = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "F1", "F2"]
def _es_s3_slstr_custom_bands(bands: list[str]) -> str:
    """SLSTR-safe custom bands — no units constraint (thermal bands aren't reflectance)."""
    n = len(bands)
    bands_js = ", ".join(f'"{b}"' for b in bands)
    returns = ", ".join(f"s.{b}" for b in bands)
    return (
        "//VERSION=3\n"
        "function setup(){return{"
        f'input:[{{bands:[{bands_js}]}}],'   # <-- no units:"REFLECTANCE"
        f'output:{{bands:{n},sampleType:SampleType.AUTO}}'
        "}}\n"
        f"function evaluatePixel(s){{return[{returns}]}}"
    )

def _es_s3_slstr_ndvi(nir: str = "S3", r: str = "S2") -> str:
    return (
        "//VERSION=3\n"
        "function setup(){return{"
        f'input:[{{bands:["{r}","{nir}"]}}],'
        'output:{bands:3,sampleType:"AUTO"}'
        "}}\n"
        f"function evaluatePixel(s){{"
        f"let NDVI=index(s.{nir},s.{r});"
        "const viz=ColorGradientVisualizer.createWhiteGreen(-0.1,1.0);"
        "return viz.process(NDVI)"
        "}"
    )
def fetch_sentinel3_slstr(
    bbox: list[float],
    start_date: str,
    end_date: str,
    *,
    preset: Optional[str] = "false_color", # "ndvi"       
    custom_bands: Optional[list[str]] = None,
    evalscript: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    """
    Fetch a Sentinel-3 SLSTR (Sea and Land Surface Temperature Radiometer) image.
    1 km resolution (500 m for S1–S3), dual-view instrument.

    Presets
    -------
    "false_color"  – Approximate visible RGB: S3 (SWIR-ish), S2 (red), S1 (green)
                    Note: SLSTR has no dedicated blue band; S1 stands in.


    """
    if evalscript is None:
        if custom_bands:
            if validate_bands(custom_bands, _S3_SLSTR_BANDS):
                evalscript = _es_s3_slstr_custom_bands(custom_bands)
            else:
                raise ValueError(f"Unidentifed band in Sentinel-3 SLSTR, Choose from the followinfg bands : {_S3_SLSTR_BANDS}")
        elif preset:
            p = preset.lower()
            if p == "false_color":
                evalscript = _es_rgb("S3", "S2", "S1", gain=2)
            elif p == "ndvi":
                evalscript = _es_s3_slstr_ndvi("S3", "S2")
            else:
                raise ValueError(
                    f"Unknown S3 SLSTR preset '{preset}'. Choose: false_color"
                )
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    return _fetch(
        collection_type="sentinel-3-slstr",
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        save_path=save_path
    )


# ---------------------------------------------------------------------------
# ── SENTINEL-3 SYN L2 ───────────────────────────────────────────────────────
# Synergy product combining OLCI + SLSTR.
# Bands Syn_Oa01 … Syn_Oa21 (OLCI channels) + Syn_S1 … Syn_S6 (SLSTR channels)
# True colour: Syn_Oa08 (R), Syn_Oa06 (G), Syn_Oa04 (B)
# ---------------------------------------------------------------------------
_S3_SLSYN_BANDS  = ["B01","B02","B03","B04","B05","B06","B07","B08","B09","B10","B11",
          "B12","B16","B17","B18", "B21","S1", "S2", "S3", "S4", "S5", "S6"]

def _es_s3_ndvi(nir: str = "S3", r: str = "S2") -> str:
    return (
        "//VERSION=3\n"
        "function setup(){return{"
        f'input:["{r}","{nir}"],'
        'output:{bands:1,sampleType:SampleType.AUTO}'
        "}}\n"
        f"function evaluatePixel(s){{return[(s.{nir}-s.{r})/(s.{nir}+s.{r})]}}"
    )



def fetch_sentinel3_syn_l2(
    bbox: list[float],
    start_date: str,
    end_date: str,
    *,
    preset: Optional[str] = "true_color",        # "true_color"
    custom_bands: Optional[list[str]] = None,
    evalscript: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    """
    Fetch a Sentinel-3 SYN L2 (Synergy OLCI+SLSTR combined) image.
    ~300 m resolution, surface reflectance.

    Presets
    -------
    "true_color"  – RGB using Syn_Oa08 (665 nm), Syn_Oa06 (560 nm), Syn_Oa04 (490 nm)

    Custom bands
    ------------
    OLCI synergy channels: Syn_Oa01 … Syn_Oa21
    SLSTR synergy channels: Syn_S1 … Syn_S6
    """
    if evalscript is None:
        if custom_bands:
            if validate_bands(custom_bands, _S3_SLSYN_BANDS):
                    evalscript = _es_s2_custom_bands(custom_bands)
            else:
                raise ValueError(f"Unidentifed band in Sentinel-3 SLSYN, Choose from the followinfg bands : {_S3_SLSYN_BANDS}")
        elif preset:
            p = preset.lower()
            if p == "true_color":
                evalscript = _es_rgb("B08", "B06", "B04")
            elif p == "ndvi":
                evalscript = _es_s3_ndvi("S3", "S2")
            else:
                raise ValueError(
                    f"Unknown S3 SYN L2 preset '{preset}'. Choose: true_color"
                )
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    return _fetch(
        collection_type="sentinel-3-synergy-l2",
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        save_path=save_path
    )

def _es_custom_bands(bands: list[str], gain: float = 2.5) -> str:
    """
    Build an evalscript for arbitrary band names.
    For 3 bands → RGB output (gain applied).
    For 1 band  → single-channel greyscale output (gain applied).
    For other counts → raw output (no gain), sampleType FLOAT32.
    """
    n = len(bands)
    bands_js = ", ".join(f'"{b}"' for b in bands)
    if n == 3:
        r, g, bl = bands
        return (
            "//VERSION=3\n"
            f'function setup(){{return{{input:[{bands_js}],output:{{bands:3,sampleType:"AUTO"}}}}}}\n'
            f"function evaluatePixel(s){{return[{gain}*s.{r},{gain}*s.{g},{gain}*s.{bl}]}}"
        )
    elif n == 1:
        b0 = bands[0]
        return (
            "//VERSION=3\n"
            f'function setup(){{return{{input:["{b0}"],output:{{bands:1,sampleType:"AUTO"}}}}}}\n'
            f"function evaluatePixel(s){{return[{gain}*s.{b0}]}}"
        )
    else:
        returns = ",".join(f"s.{b}" for b in bands)
        return (
            "//VERSION=3\n"
            f'function setup(){{return{{input:[{bands_js}],output:{{bands:{n},sampleType:"FLOAT32"}}}}}}\n'
            f"function evaluatePixel(s){{return[{returns}]}}"
        )

# ---------------------------------------------------------------------------
# ── SENTINEL-5P L2 ──────────────────────────────────────────────────────────
# Single-value trace-gas products. No optical RGB—each band is a geophysical
# quantity (mol/m², DU, etc.).
# ---------------------------------------------------------------------------

_S5P_BAND_MAP = {
    "no2":  "NO2",    # Nitrogen dioxide tropospheric column
    "co":   "CO",     # Carbon monoxide total column
    "o3":   "O3",     # Ozone total column
    "so2":  "SO2",    # Sulphur dioxide total column
    "ch4":  "CH4",    # Methane total column
    "hcho": "HCHO",   # Formaldehyde total column
    "aer_ai_340_380": "AER_AI_340_380",  # UV aerosol index
    "cloud_fraction": "CLOUD_FRACTION",
}


def _es_s5p_single(band_name: str, scale: float = 1e4) -> str:
    """
    Simple single-band S5P visualisation.
    Values are scaled to [0,1] for display; adjust scale per product.
    """
    return (
        "//VERSION=3\n"
        f'function setup(){{return{{input:["{band_name}","dataMask"],output:{{bands:1,sampleType:"AUTO"}}}}}}\n'
        "function evaluatePixel(s){\n"
        f"  if(!s.dataMask)return[0];\n"
        f"  return[Math.max(0,Math.min(1,s.{band_name}*{scale}))]\n"
        "}"
    )


def fetch_sentinel5p_l2(
    bbox: list[float],
    start_date: str,
    end_date: str,
    *,
    preset: Optional[str] = "no2",
    custom_bands: Optional[list[str]] = None,
    evalscript: Optional[str] = None,
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    """
    Fetch a Sentinel-5P L2 trace-gas product.
    Resolution ~3.5 × 5.5 km (TROPOMI footprint).

    Presets (greyscale single-band visualisations)
    -------
    "no2"             – NO₂ tropospheric column
    "co"              – CO total column
    "o3"              – O₃ total column
    "so2"             – SO₂ total column
    "ch4"             – CH₄ total column
    "hcho"            – HCHO total column
    "aer_ai_340_380"  – UV aerosol index
    "cloud_fraction"  – Cloud fraction

    Custom bands
    ------------
    Supply any valid S5P band name directly, e.g. custom_bands=["NO2"].
    For quantitative retrieval write your own evalscript= returning FLOAT32.
    """
    if evalscript is None:
        if custom_bands:
            evalscript = _es_custom_bands(custom_bands)
        elif preset:
            p = preset.lower()
            band = _S5P_BAND_MAP.get(p)
            if band is None:
                raise ValueError(
                    f"Unknown S5P preset '{preset}'. "
                    f"Choose: {', '.join(_S5P_BAND_MAP.keys())}"
                )
            evalscript = _es_s5p_single(band)
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    return _fetch(
        collection_type="sentinel-5p-l2",
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        save_path=save_path
    )


# ---------------------------------------------------------------------------
# ── LANDSAT OT L1 (Landsat 8 / 9) ──────────────────────────────────────────
# Bands: B01 (coastal), B02 (blue), B03 (green), B04 (red), B05 (NIR),
#        B06 (SWIR-1), B07 (SWIR-2), B08 (pan), B09 (cirrus), B10/B11 (thermal)
# True colour: B04 (red), B03 (green), B02 (blue)
# ---------------------------------------------------------------------------

def fetch_landsat_ot_l1(
    bbox: list[float],
    start_date: str,
    end_date: str,
    *,
    preset: Optional[str] = "true_color",        
    custom_bands: Optional[list[str]] = None,
    evalscript: Optional[str] = None,
    max_cloud_cover: Optional[float] = None,
    mosaicking_order: str = "leastCC",
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    """
    Fetch a Landsat 8/9 OLI-TIRS L1 image (~30 m resolution).

    Presets
    -------
    "true_color"  – Natural colour RGB: R=B04, G=B03, B=B02

    Custom bands
    ------------
    B01 (coastal/aerosol), B02 (blue), B03 (green), B04 (red),
    B05 (NIR), B06 (SWIR-1), B07 (SWIR-2), B08 (panchromatic),
    B09 (cirrus), B10 (TIRS-1 thermal), B11 (TIRS-2 thermal)
    custom_bands=["B07","B05","B04"]  → SWIR false colour
    custom_bands=["B05","B04","B03"]  → NIR false colour (vegetation)
    """
    if evalscript is None:
        if custom_bands:
            evalscript = _es_custom_bands(custom_bands)
        elif preset:
            p = preset.lower()
            if p == "true_color":
                evalscript = _es_rgb("B04", "B03", "B02")
            else:
                raise ValueError(
                    f"Unknown Landsat OT L1 preset '{preset}'. Choose: true_color"
                )
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    df_extra: dict = {"mosaickingOrder": mosaicking_order}
    if max_cloud_cover is not None:
        df_extra["maxCloudCoverage"] = max_cloud_cover

    return _fetch(
        collection_type="landsat-ot-l1",
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        data_filter_extra=df_extra,
        save_path=save_path
    )


# ---------------------------------------------------------------------------
# ── DEM ─────────────────────────────────────────────────────────────────────
# Band: DEM (elevation in metres)
# No time range required — pass any date or use sentinel hub defaults.
# ---------------------------------------------------------------------------

_DEM_HILLSHADE_ES = """//VERSION=3
function setup(){return{input:["DEM"],output:{bands:1,sampleType:"AUTO"}}}
function evaluatePixel(s){
  // Simple greyscale normalisation: 0 m → black, 8000 m → white
  return[Math.max(0,Math.min(1,s.DEM/8000))]
}"""

_DEM_COLORMAP_ES = """//VERSION=3
function setup(){return{input:["DEM"],output:{bands:3,sampleType:"AUTO"}}}
function evaluatePixel(s){
  var v=Math.max(0,Math.min(1,s.DEM/5000));
  // Blue (low) → green → yellow → red (high) ramp
  if(v<0.25)return[0,v*4,1];
  if(v<0.5)return[0,1,(0.5-v)*4];
  if(v<0.75)return[(v-0.5)*4,1,0];
  return[1,(1-v)*4,0];
}"""


def fetch_dem(
    bbox: list[float],
    start_date: str = "2020-01-01",
    end_date: str = "2020-12-31",
    *,
    preset: Optional[str] = "elevation_grey",       
    custom_bands: Optional[list[str]] = None,  # typically ["DEM"]
    evalscript: Optional[str] = None,
    dem_instance: str = "COPERNICUS_30", # COPERNICUS_30 | COPERNICUS_90 | MAPZEN
    width: int = 512,
    height: int = 512,
    save_path: Optional[str] = None
) -> dict:
    """
    Fetch a Digital Elevation Model image.
    DEM products are static (not time-dependent); date range is required by the
    API but doesn't filter data.

    Presets
    -------
    "elevation_grey"  – Greyscale 0–8000 m normalised
    "elevation_color" – Colour ramp (blue=low → red=high, 0–5000 m)

    Custom bands
    ------------
    The only band is "DEM" (elevation in metres, float).
    For raw FLOAT32 values use:
        custom_bands=["DEM"] with your own evalscript= for scientific use.

    dem_instance
    ------------
    "COPERNICUS_30"  – Copernicus DEM GLO-30 (default, 30 m)
    "COPERNICUS_90"  – Copernicus DEM GLO-90 (90 m)
    "MAPZEN"         – Mapzen Terrain Tiles
    """
    if evalscript is None:
        if custom_bands:
            evalscript = _es_custom_bands(custom_bands)
        elif preset:
            p = preset.lower()
            if p in ("elevation_grey", "elevation_gray"):
                evalscript = _DEM_HILLSHADE_ES
            elif p == "elevation_color":
                evalscript = _DEM_COLORMAP_ES
            else:
                raise ValueError(
                    f"Unknown DEM preset '{preset}'. "
                    "Choose: elevation_grey | elevation_color"
                )
        else:
            raise ValueError("Provide preset=, custom_bands=, or evalscript=.")

    proc: dict = {"demInstance": dem_instance}

    return _fetch(
        collection_type="dem",
        evalscript=evalscript,
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        width=width,
        height=height,
        processing_options=proc,
        save_path=save_path
    )