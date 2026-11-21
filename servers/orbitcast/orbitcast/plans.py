"""
ESA Sentinel-1/2 acquisition plans: which images ESA has scheduled, where and when.

ESA publishes, per satellite, KML files listing the planned data takes (footprint polygon, start/stop time,
mode, orbit) up to 2-3 weeks ahead. A satellite pass that matches a data take is labelled 'planned';
beyond the plan horizon only orbit prediction ('geometric') is available.

Pipeline:
    plan_index       list the plan files on ESA's page (validity window read from the file name)
    download_plan    download one KML into the cache (streamed, checked, locked against parallel downloads)
    plan_placemarks  parse a KML into data takes (parsed once, then read from a JSON cache)
    planned_takes    data takes over an AOI and time window, newest plan issue first; used by the forecast,
                     export and coverage tools

Everything is cached under PLAN_DIR, so ESA is only contacted for new plans or a list older than 6 h.
"""

import contextlib
import datetime as dt
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import shapely
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .catalog import ACTIVE
from .config import CACHE, UA
from .geo import UTC, _iso, area_km2, normalize_lon

# ESA site that hosts both the plan pages and the plan KML files.
PLAN_BASE = "https://sentinels.copernicus.eu"
PLAN_INDEX = {"S1": f"{PLAN_BASE}/copernicus/sentinel-1/acquisition-plans",
              "S2": f"{PLAN_BASE}/copernicus/sentinel-2/acquisition-plans"}

# Link to one plan file. Groups: file id, mission digit (1/2), unit letter (a-d), validity start, validity stop.
# href example: s1d_mp_user_20261020t182319_20261109t200700
PLAN_HREF = re.compile(r"/documents/d/sentinel/(s([12])([a-z])_mp_(?:user|acq__kml)_(\d{8}t\d{6})_(\d{8}t\d{6})[\w-]*)", re.IGNORECASE)

PLAN_INDEX_TTL_H = 6           # hours before the plan list page is downloaded again
MAX_PLAN_BYTES = 200 * 2**20   # larger downloads are aborted (plan KMLs are a few MB)
RETRY_AFTER_S = 60           # a plan URL that failed is not requested again for this long

# Cache folder: <file>.kml, <file>.meta.json (download info), <file>.parsed.v2.json (parsed placemarks),
# index_<mission>.html (cached plan list page).
PLAN_DIR = CACHE / "plans"
PLAN_DIR.mkdir(parents=True, exist_ok=True)

_bad_plan_urls: dict[str, float] = {}  # url -> time it failed (see RETRY_AFTER_S)


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """
    Exclusive file lock, so two processes don't download the same plan (no-op where fcntl is missing).

    Args:
        path: lock file to create and lock.
    """
    try:
        import fcntl
    except ImportError:  # Windows: no locking
        yield
        return
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # blocks until the other process releases it
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def plan_index(mission: str, refresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Plan files ESA currently publishes for a mission, read from its plan list page (cached 6 h).

    Returns ([{file, satellite, start, stop, url}], index info). RuntimeError if unreachable and nothing cached.

    Args:
        mission: 'S1' or 'S2'.
        refresh: download the list page again even if the cached copy is recent.
    """
    # Re-download the list page when asked, missing or older than the TTL; keep the old copy if ESA fails.
    p = PLAN_DIR / f"index_{mission}.html"
    age_h = (time.time() - p.stat().st_mtime) / 3600 if p.exists() else None
    status = "cached"
    if refresh or age_h is None or age_h > PLAN_INDEX_TTL_H:
        try: # Get plan from ESA's webpage for the desired mission
            r = httpx.get(PLAN_INDEX[mission], timeout=30, follow_redirects=True, headers=UA)
            r.raise_for_status()
            p.write_text(r.text, encoding="utf-8")
            status, age_h = "refreshed", 0.0
        except Exception as e:
            if not p.exists():
                raise RuntimeError(f"ESA {mission} plan index unreachable ({type(e).__name__}) and nothing cached.") from e
            status = f"stale ({type(e).__name__})"

    # One entry per plan link of this mission (the page repeats links); the validity window is in the file name.
    def to_utc(stamp: str) -> dt.datetime:
        """
        Plan file-name timestamp -> UTC datetime.

        Args:
            stamp: timestamp such as '20261020t182319'.
        """
        return dt.datetime.strptime(stamp.upper(), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)

    files, seen = [], set()
    for m in PLAN_HREF.finditer(p.read_text(encoding="utf-8")):
        fid, mis, unit, a, b = m.groups()
        if f"S{mis}" == mission and fid.lower() not in seen:
            seen.add(fid.lower())
            files.append({"file": fid, "satellite": f"SENTINEL-{mis}{unit.upper()}", "start": to_utc(a),
                          "stop": to_utc(b), "url": f"{PLAN_BASE}/documents/d/sentinel/{fid}"})
    return files, {"index": PLAN_INDEX[mission], "index_status": status, "index_age_h": round(age_h or 0, 1)}


def download_plan(plan_file: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """
    Download one plan KML unless cached; returns its download info (status, size, checksum...).

    Rejects files over MAX_PLAN_BYTES and ESA's HTML error pages.

    Args:
        plan_file: entry from plan_index (file, url...).
        force: download again even if cached.
    """

    kml_path = PLAN_DIR / f"{plan_file['file'].lower()}.kml"
    meta_path = kml_path.with_suffix(".meta.json")

    # Cached copy, or a recent failure: no request.
    if not force and kml_path.exists() and meta_path.exists():
        return {**json.loads(meta_path.read_text()), "status": "cached"}

    # Check when it was last retry
    if time.time() - _bad_plan_urls.get(plan_file["url"], 0) < RETRY_AFTER_S:
        raise RuntimeError(f"download failed less than {RETRY_AFTER_S/3600} h ago; not retried")

    with _file_lock(kml_path.with_suffix(".lock")): # Locked file to avoid multiple downloads of the same.
        # Another process may have downloaded it while we waited for the lock.
        if not force and kml_path.exists() and meta_path.exists():
            return {**json.loads(meta_path.read_text()), "status": "cached"}

        # Stream to a .part file and rename at the end, so a cut download never looks like a valid plan.
        part_path = kml_path.with_suffix(".part")
        checksum = hashlib.sha256()
        downloaded_bytes = 0
        download_start = time.time()

        try: # Download plan
            with httpx.stream("GET", plan_file["url"], timeout=httpx.Timeout(30, read=120), follow_redirects=True,
                              headers=UA) as response:
                response.raise_for_status()
                with open(part_path, "wb") as part_out:
                    for chunk in response.iter_bytes(1 << 16):
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > MAX_PLAN_BYTES:
                            raise RuntimeError("plan file larger than 200 MB")
                        checksum.update(chunk)
                        part_out.write(chunk)

            # Broken ESA links return an HTML page with status 200: check the start of the file is KML.
            with open(part_path, "rb") as part_in:
                if b"<kml" not in part_in.read(8192).lower():
                    raise RuntimeError("ESA returned a web page instead of KML (broken link on ESA's side)")
            part_path.replace(kml_path)

        except Exception:
            _bad_plan_urls[plan_file["url"]] = time.time()
            raise

        finally:
            part_path.unlink(missing_ok=True)

        # Download info (reported as provenance), and drop the parse cache of the previous copy.
        download_info = {"file": plan_file["file"], "url": plan_file["url"], "bytes": downloaded_bytes,
                         "sha256": checksum.hexdigest()[:16], "downloaded_utc": _iso(dt.datetime.now(UTC)),
                         "download_s": round(time.time() - download_start, 1)}
        meta_path.write_text(json.dumps(download_info))
        kml_path.with_suffix(".parsed.v2.json").unlink(missing_ok=True)

        return {**download_info, "status": "downloaded"}


def plan_placemarks(plan_file: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Data takes of a downloaded plan as [{geom, begin, end, fields (ExtendedData)}].

    The KML is parsed once and cached as JSON (XML parsing is slow). Handles both ESA layouts:
    S1 = bare LinearRing, S2 = Polygon/outerBoundaryIs/LinearRing.

    Args:
        plan_file: entry from plan_index; the KML must already be downloaded.
    """
    kml_path = PLAN_DIR / f"{plan_file['file'].lower()}.kml"
    parse_cache = kml_path.with_suffix(".parsed.v2.json")  # v2: antimeridian-safe footprints

    if not parse_cache.exists():
        def strip_namespace(tag: str) -> str:
            """
            XML tag without its namespace: '{http://www.opengis.net/kml/2.2}Placemark' -> 'Placemark'.

            Args:
                tag: XML tag, possibly with a namespace.
            """
            return tag.rsplit("}", 1)[-1]

        def kml_time(text: str) -> dt.datetime | None:
            """
            KML time -> UTC datetime, or None if unreadable.

            Args:
                text: time such as '2026-11-21T10:42:00Z'.
            """
            try:
                return dt.datetime.fromisoformat(text.strip().replace("Z", "")).replace(tzinfo=UTC)
            except ValueError:
                return None

        cached_rows = []
        for placemark in ET.fromstring(kml_path.read_bytes()).iter(): # Parse the XML file plan
            if strip_namespace(placemark.tag) != "Placemark":
                continue
            # Collect the data-take fields, its time span and its outer rings (holes are ignored).
            fields, rings, begin, end, hole_rings = {}, [], None, None, set()

            for element in placemark.iter():
                tag = strip_namespace(element.tag)
                if tag == "Data" and element.get("name"):
                    value = next((child.text for child in element if strip_namespace(child.tag) == "value"), None)
                    if value:
                        fields[element.get("name")] = value.strip()
                elif tag == "begin" and element.text:
                    begin = kml_time(element.text)
                elif tag == "end" and element.text:
                    end = kml_time(element.text)
                elif tag == "innerBoundaryIs":
                    hole_rings.update(id(ring) for ring in element.iter() if strip_namespace(ring.tag) == "LinearRing")
                elif tag == "LinearRing" and id(element) not in hole_rings:
                    for child in element:
                        if strip_namespace(child.tag) == "coordinates" and child.text:
                            # 'lon,lat[,alt] lon,lat[,alt] ...' -> [(lon, lat), ...]
                            points = [tuple(map(float, coord.split(",")[:2])) for coord in child.text.split()
                                      if "," in coord]
                            if len(points) >= 3:
                                rings.append(normalize_lon(Polygon(points)))

            # Some plans only give the time span in the ExtendedData fields.
            begin = begin or kml_time(fields.get("ObservationTimeStart", ""))
            end = end or kml_time(fields.get("ObservationTimeStop", ""))
            if rings and begin:
                cached_rows.append({"wkb": shapely.to_wkb(unary_union(rings), hex=True), "b": begin.isoformat(),
                                    "e": end.isoformat() if end else None, "f": fields})
        parse_cache.write_text(json.dumps(cached_rows))

    # Return area covered, the acquisition times, and extra fields like mode, polarization,
    # relative orbit, datatake id, etc.
    return [{"geom": shapely.from_wkb(row["wkb"]), "begin": dt.datetime.fromisoformat(row["b"]),
             "end": dt.datetime.fromisoformat(row["e"]) if row["e"] else None, "fields": row["f"]}
            for row in json.loads(parse_cache.read_text())]


def planned_takes(mission: str, aoi: BaseGeometry | None, start: dt.datetime,
                  end: dt.datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Images ESA has scheduled over the AOI in [start, end], for one mission.

    Per satellite, the newest plan file wins from its start date; older ones fill earlier times or a failed
    download. S2 keeps only nominal (NOBS) takes. Returns (takes sorted by start, provenance with the plan files
    used and each satellite's plan horizon).

    Args:
        mission: 'S1' or 'S2'.
        aoi: area of interest; None = whole world.
        start, end: time window.
    """
    # Plan files overlapping the window, grouped by satellite.
    plan_files, provenance = plan_index(mission)
    files_by_satellite = {}
    for plan_file in plan_files:
        if plan_file["stop"] >= start and plan_file["start"] <= end:
            files_by_satellite.setdefault(plan_file["satellite"], []).append(plan_file)

    takes = []           # data takes kept, returned
    used_files = []      # plan files actually read (provenance)
    warnings = []        # plan files that failed to download
    plan_horizon = {}    # satellite -> last time its plans cover

    for satellite, satellite_files in files_by_satellite.items():
        if satellite not in ACTIVE:
            continue

        # Newest file first
        covered_from = dt.datetime.max.replace(tzinfo=UTC)
        for plan_file in sorted(satellite_files, key=lambda candidate: candidate["start"], reverse=True):
            if covered_from <= start:
                break  # newer files already cover the whole window
            try:
                download_info = download_plan(plan_file)
            except Exception as error:
                reason = ("404 Not Found (broken link on ESA's page)" if "404" in str(error)
                          else str(error).splitlines()[0])
                warnings.append(f"{satellite} plan {plan_file['file'][:48]}: {reason}; used the previous plan issue")
                continue  # covered_from unchanged, so the older file fills this file's period
            used_files.append({"file": plan_file["file"], "status": download_info["status"],
                               "sha256": download_info["sha256"],
                               "covers": [_iso(plan_file["start"]), _iso(plan_file["stop"])]})
            plan_horizon[satellite] = max(plan_horizon.get(satellite, plan_file["stop"]), plan_file["stop"])

            for placemark in plan_placemarks(plan_file):
                # Keep takes in this file's share of the window that touch the AOI.
                take_start = placemark["begin"]
                if not (max(start, plan_file["start"]) <= take_start < min(end, covered_from)):
                    continue
                if aoi is not None and not placemark["geom"].intersects(aoi):
                    continue
                fields = placemark["fields"]
                mode = fields.get("Mode")
                if mission == "S2" and mode and mode.upper() != "NOBS":
                    continue  # only nominal (NOBS) S2 data takes are distributed to users
                aoi_coverage_pct = (100 * area_km2(placemark["geom"].intersection(aoi)) / area_km2(aoi)
                                    if aoi is not None else None)
                takes.append({"satellite": satellite, "start": take_start, "end": placemark["end"], "mode": mode,
                              "polarisation": fields.get("Polarisation"),
                              "relative_orbit": fields.get("OrbitRelative"),
                              "absolute_orbit": fields.get("OrbitAbsolute"),
                              "datatake": fields.get("DatatakeId") or fields.get("ID"),
                              "plan_coverage_pct": (round(min(aoi_coverage_pct, 100), 1)
                                                    if aoi_coverage_pct is not None else None),
                              "plan_file": plan_file["file"],
                              "geom": placemark["geom"]})
            covered_from = plan_file["start"]
    takes.sort(key=lambda take: take["start"])
    provenance = {**provenance, "plan_files": used_files,
                  "plan_horizon_utc": {satellite: _iso(horizon) for satellite, horizon in plan_horizon.items()}}
    if warnings:
        provenance["warnings"] = warnings
    return takes, provenance
