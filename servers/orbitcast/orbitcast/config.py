"""
Shared settings used by every module: folders, network identity, size/time limits and confidence colours.

    CACHE / OUTPUT_DIR   cache (TLEs, ESA plans, user satellites) and GeoJSON output folders. Set with
                         ORBITCAST_CACHE / ORBITCAST_OUTPUT; each falls back to the temp dir if not writable
                         (containers often have a read-only home)
    UA                   User-Agent sent to CelesTrak, ESA and Open-Meteo
    MAX_*                limits that keep requests fast and meaningful (window length, AOI and region size)
    CONF_COLORS          one colour per confidence label, shared by GeoJSON, figures and slides
"""

import os
import tempfile
from pathlib import Path


def _writable(*candidates: str | Path | None) -> Path:
    """
    First folder that can be created and written (containers often have a read-only home).

    Args:
        candidates: folders to try, in order; empty values (an unset environment variable) are skipped.

    Raises RuntimeError if none of them is writable.
    """

    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)

        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_test"
            probe.touch()
            probe.unlink()
            return p
        except OSError:
            continue
    raise RuntimeError("No writable folder for OrbitCast; set ORBITCAST_CACHE / ORBITCAST_OUTPUT.")


CACHE = _writable(os.getenv("ORBITCAST_CACHE"), Path.home() / ".cache" / "orbitcast",
                  Path(tempfile.gettempdir()) / "orbitcast")
OUTPUT_DIR = _writable(os.getenv("ORBITCAST_OUTPUT"), Path("orbitcast_outputs"),
                       Path(tempfile.gettempdir()) / "orbitcast_outputs")
UA = {"User-Agent": "OrbitCast-MCP/0.4 (agentic-eo-hackathon)"}

MAX_HORIZON_DAYS = 30          # forecast window limit (TLE accuracy, ESA plan horizon)
MAX_AOI_KM2 = 500_000          # Maximum size for one defined area of interest
MAX_REGION_KM2 = 25_000_000    # observed_areas: a region can be a country or a continent
MAX_COVERAGE_DAYS = {True: 10, False: 3}  # observed_areas window: with a region / whole world

# Confidence colours, shared by the GeoJSON styling, the figures and the slides (Okabe-Ito, colour-blind safe).
CONF_COLORS = {"planned": "#0072B2", "likely_usable": "#009E73", "geometric": "#E69F00", "cloudy": "#8C8C8C"}
