"""Pinned daily Hugging Face AIS files, validated before bounded batch scanning."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, timedelta

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from filelock import FileLock, Timeout
from shapely.geometry import Point

from common import MaritimeError, finite, iso, mmsi, utcnow
from storage import cache_root, checksum, json_write

REPO = "AISDataPortal/DB"
HF = "https://huggingface.co"
MAX_FILE = 512 * 1024 * 1024
MAX_MATCHING_REPORTS = 250_000


class AIS:
    def __init__(self, client=None):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(60, read=120), follow_redirects=True
        )
        self.revision = None
        self.files = None

    def close(self):
        self.client.close()

    def catalogue(self):
        if self.files is not None:
            return self.files
        try:
            info = self.client.get(f"{HF}/api/datasets/{REPO}")
            info.raise_for_status()
            self.revision = info.json()["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", self.revision):
                raise ValueError
            url = f"{HF}/api/datasets/{REPO}/tree/{self.revision}?limit=1000"
            files = {}
            for _ in range(20):
                response = self.client.get(url)
                response.raise_for_status()
                for item in response.json():
                    if item.get("type") == "file" and re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}_ais\.parquet", item.get("path", "")
                    ):
                        files[item["path"][:10]] = item
                next_url = response.links.get("next", {}).get("url")
                if not next_url:
                    break
                if not next_url.startswith(
                    f"{HF}/api/datasets/{REPO}/tree/{self.revision}"
                ):
                    raise ValueError
                url = next_url
            else:
                raise MaritimeError(
                    "source_schema_error", "Archive catalogue exceeded page limit."
                )
            if not files:
                raise ValueError
            self.files = files
            return files
        except httpx.HTTPError:
            raise MaritimeError(
                "source_unavailable", "Cannot retrieve the Hugging Face AIS catalogue."
            ) from None
        except (KeyError, ValueError, TypeError):
            raise MaritimeError(
                "source_schema_error", "Unexpected AIS repository catalogue."
            ) from None

    def latest_day(self):
        return max(self.catalogue())

    def _prune(self, root, required, budget, keep):
        existing = list(root.glob("*.parquet"))
        used = sum(p.stat().st_size for p in existing)
        for path in sorted(existing, key=lambda p: p.stat().st_mtime):
            if used + required <= budget:
                break
            if path == keep:
                continue
            used -= path.stat().st_size
            path.unlink()
            path.with_suffix(".json").unlink(missing_ok=True)
        if used + required > budget:
            raise MaritimeError(
                "cache_budget_exceeded",
                "AIS cache budget is too small for the required daily file.",
            )

    def _file(self, day, root):
        """Caller holds archive.lock during download AND scan to prevent eviction races."""
        item = self.catalogue().get(day)
        if item is None:
            raise MaritimeError(
                "source_date_unavailable",
                f"No published AIS daily file for {day}; no date substitution.",
            )
        expected = item.get("lfs", {}).get("oid")
        size = item.get("size")
        if (
            not isinstance(size, int)
            or not 8 < size <= MAX_FILE
            or not re.fullmatch(r"[a-f0-9]{64}", expected or "")
        ):
            raise MaritimeError(
                "source_schema_error",
                "AIS file lacks a bounded size or published SHA-256.",
            )
        path = root / f"{expected}.parquet"
        metadata_path = path.with_suffix(".json")
        try:
            metadata = (
                json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
            )
        except ValueError:
            metadata = {}
        if not (
            path.exists()
            and path.stat().st_size == size
            and metadata.get("sha256") == expected
            and metadata.get("mtime_ns") == path.stat().st_mtime_ns
        ):
            path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            try:
                budget = int(os.getenv("AIS_CACHE_MAX_MB", "1024")) * 1024 * 1024
            except ValueError:
                raise MaritimeError(
                    "invalid_configuration", "AIS_CACHE_MAX_MB must be an integer."
                ) from None
            self._prune(root, size, budget, path)
            temporary = path.with_suffix(".part")
            url = f"{HF}/datasets/{REPO}/resolve/{self.revision}/{item['path']}"
            try:
                with self.client.stream("GET", url) as response:
                    response.raise_for_status()
                    count = 0
                    with temporary.open("wb") as target:
                        for chunk in response.iter_bytes():
                            count += len(chunk)
                            if count > size or count > MAX_FILE:
                                raise MaritimeError(
                                    "source_integrity_error",
                                    "AIS download exceeded published size.",
                                )
                            target.write(chunk)
                if count != size or checksum(temporary) != expected:
                    raise MaritimeError(
                        "source_integrity_error",
                        "AIS download size/checksum differs from published file.",
                    )
                temporary.replace(path)
            except httpx.HTTPError:
                raise MaritimeError(
                    "source_unavailable", f"AIS download failed for {day}."
                ) from None
            finally:
                temporary.unlink(missing_ok=True)
            json_write(
                metadata_path, {"sha256": expected, "mtime_ns": path.stat().st_mtime_ns}
            )
        try:
            parquet = pq.ParquetFile(path)
            if not {"MMSI", "TSTAMP", "LONGITUDE", "LATITUDE"} <= set(
                parquet.schema_arrow.names
            ):
                raise ValueError
        except (pa.ArrowException, ValueError, OSError):
            raise MaritimeError(
                "source_file_unreadable",
                f"Published AIS file {day} is not readable Parquet; no substitute date.",
            ) from None
        return parquet, {
            "repository": REPO,
            "revision": self.revision,
            "file": item["path"],
            "sha256": expected,
            "size_bytes": size,
            "url": f"{HF}/datasets/{REPO}/resolve/{self.revision}/{item['path']}",
        }

    def positions(self, bbox, start, end, limit=1000, reference=None, footprint=None):
        if not 1 <= limit <= 5000:
            raise MaritimeError("invalid_request", "limit must be between 1 and 5000.")
        if end <= start or end - start > timedelta(days=1):
            raise MaritimeError(
                "request_too_large", "Detailed AIS window must be <= 24 hours."
            )
        days = []
        current = start.date()
        while current <= (end - timedelta(microseconds=1)).date():
            days.append(current.isoformat())
            current += timedelta(days=1)
        root = cache_root() / "ais"
        root.mkdir(exist_ok=True)
        selected = {}
        matching = invalid = outside = 0
        sources = []
        newest = None
        try:
            with FileLock(str(root / "archive.lock"), timeout=120):
                for day in days:
                    parquet, source = self._file(day, root)
                    sources.append(source)
                    names = parquet.schema_arrow.names
                    columns = [
                        c
                        for c in [
                            "MMSI",
                            "TSTAMP",
                            "LONGITUDE",
                            "LATITUDE",
                            "SOG",
                            "COG",
                            "HEADING",
                            "NAME",
                            "TYPE",
                            "A",
                            "B",
                            "C",
                            "D",
                        ]
                        if c in names
                    ]
                    for batch in parquet.iter_batches(
                        batch_size=131072, columns=columns
                    ):
                        ts_type = batch.column("TSTAMP").type
                        if not pa.types.is_timestamp(ts_type):
                            raise MaritimeError(
                                "source_schema_error",
                                "AIS TSTAMP must be a timestamp column.",
                            )
                        # Archive naive timestamp fields are documented here as UTC.
                        a = start if ts_type.tz else start.replace(tzinfo=None)
                        b = end if ts_type.tz else end.replace(tzinfo=None)
                        mask = pc.and_(
                            pc.greater_equal(batch.column("LONGITUDE"), bbox[0]),
                            pc.less_equal(batch.column("LONGITUDE"), bbox[2]),
                        )
                        mask = pc.and_(
                            mask,
                            pc.and_(
                                pc.greater_equal(batch.column("LATITUDE"), bbox[1]),
                                pc.less_equal(batch.column("LATITUDE"), bbox[3]),
                            ),
                        )
                        mask = pc.and_(
                            mask,
                            pc.and_(
                                pc.greater_equal(
                                    batch.column("TSTAMP"), pa.scalar(a, type=ts_type)
                                ),
                                pc.less(
                                    batch.column("TSTAMP"), pa.scalar(b, type=ts_type)
                                ),
                            ),
                        )
                        filtered = batch.filter(mask)
                        matching += filtered.num_rows
                        if matching > MAX_MATCHING_REPORTS:
                            raise MaritimeError(
                                "request_too_large",
                                "More than 250,000 matching reports; narrow AOI/window.",
                            )
                        for row in filtered.to_pylist():
                            identifier = mmsi(row["MMSI"])
                            if identifier is None:
                                invalid += 1
                                continue
                            lon, lat = row["LONGITUDE"], row["LATITUDE"]
                            if footprint is not None and not footprint.covers(
                                Point(lon, lat)
                            ):
                                outside += 1
                                continue
                            when = row["TSTAMP"]
                            when = (
                                when.replace(tzinfo=UTC)
                                if when.tzinfo is None
                                else when.astimezone(UTC)
                            )
                            newest = max(newest, when) if newest else when
                            score = (
                                abs((when - reference).total_seconds())
                                if reference
                                else -when.timestamp()
                            )
                            previous = selected.get(identifier)
                            if previous and (score, when) >= previous[0]:
                                continue
                            speed = finite(row.get("SOG"))
                            speed = (
                                speed
                                if speed is not None and 0 <= speed < 102.3
                                else None
                            )
                            course, heading = (
                                finite(row.get("COG")),
                                finite(row.get("HEADING")),
                            )
                            record = {
                                "mmsi": identifier,
                                "timestamp": iso(when),
                                "longitude": lon,
                                "latitude": lat,
                                "speed_knots": speed,
                                "course_degrees": course
                                if course is not None and 0 <= course < 360
                                else None,
                                "heading_degrees": heading
                                if heading is not None and 0 <= heading < 360
                                else None,
                                "name": str(row.get("NAME") or "")[:160],
                                "reported_type": finite(row.get("TYPE")),
                            }
                            for dimension, fields in [
                                ("length_m_reported", ("A", "B")),
                                ("width_m_reported", ("C", "D")),
                            ]:
                                values = [finite(row.get(k)) for k in fields]
                                record[dimension] = (
                                    sum(values)
                                    if all(v is not None and v >= 0 for v in values)
                                    and sum(values) > 0
                                    else None
                                )
                            if reference:
                                record["delta_seconds"] = (
                                    when - reference
                                ).total_seconds()
                                record["travel_distance_at_reported_speed_m"] = (
                                    round(score * speed * 0.514444, 2)
                                    if speed is not None
                                    else None
                                )
                            selected[identifier] = ((score, when), record)
                # Touch only after integrity validation and scan; sidecar tracks the new mtime.
                for source in sources:
                    path = root / f"{source['sha256']}.parquet"
                    if path.exists():
                        path.touch()
                        json_write(
                            path.with_suffix(".json"),
                            {
                                "sha256": source["sha256"],
                                "mtime_ns": path.stat().st_mtime_ns,
                            },
                        )
        except Timeout:
            raise MaritimeError(
                "provider_busy", "Another AIS download/scan is running; retry shortly."
            ) from None
        except pa.ArrowException:
            raise MaritimeError(
                "source_file_unreadable",
                "AIS batch scan failed; partial data was discarded.",
            ) from None
        ordered = sorted(selected.values(), key=lambda pair: (pair[0], pair[1]["mmsi"]))
        records = [pair[1] for pair in ordered[:limit]]
        return records, {
            "provider": "Hugging Face AISDataPortal/DB (upstream AIS Hub per dataset card)",
            "retrieved_at": iso(utcnow()),
            "files": sources,
            "matching_report_count": matching,
            "invalid_mmsi_reports": invalid,
            "outside_scene_footprint_reports": outside,
            "distinct_valid_format_mmsi_count": len(selected),
            "returned_record_count": len(records),
            "truncated": len(selected) > limit,
            "newest_observation": iso(newest) if newest else None,
            "selection": "nearest_per_mmsi" if reference else "latest_per_mmsi",
            "timestamp_semantics": "Archive TSTAMP; naive timestamps interpreted as UTC.",
        }
