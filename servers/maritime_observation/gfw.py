"""GFW 4Wings reports: sequential across local processes, cached, size bounded."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile

import httpx
from filelock import FileLock, Timeout

from common import MaritimeError, iso, mmsi, polygon, utcnow
from storage import cache_root, json_write

BASE = "https://gateway.api.globalfishingwatch.org/v3/4wings/report"
PRESENCE = "public-global-presence:v4.0"
SAR = "public-global-sar-presence:v4.0"
MAX_BYTES = 20 * 1024 * 1024


def decode_report(payload):
    if payload.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                members = [
                    i
                    for i in archive.infolist()
                    if i.filename.lower().endswith(".json")
                ]
                if sum(i.file_size for i in members) > MAX_BYTES:
                    raise MaritimeError(
                        "request_too_large", "GFW expanded report exceeds 20 MB."
                    )
                documents = [json.loads(archive.read(i)) for i in members]
                reports = [
                    d for d in documents if isinstance(d, dict) and "entries" in d
                ]
                if len(reports) != 1:
                    raise ValueError
                return reports[0]
        except (ValueError, zipfile.BadZipFile):
            raise MaritimeError(
                "source_schema_error", "Unrecognised GFW ZIP report."
            ) from None
    try:
        return json.loads(payload)
    except ValueError:
        raise MaritimeError(
            "source_schema_error", "GFW response is not a JSON report."
        ) from None


def rows_from_report(data, dataset):
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise MaritimeError("source_schema_error", "GFW report is missing entries.")
    if data.get("nextOffset") not in (None, 0):
        raise MaritimeError(
            "incomplete_report",
            "Report has another page; narrow the AOI/window before computing totals.",
        )
    rows = []
    for entry in data["entries"]:
        if (
            isinstance(entry, dict)
            and dataset in entry
            and isinstance(entry[dataset], list)
        ):
            rows.extend(entry[dataset])
        elif isinstance(entry, dict) and any(
            k in entry for k in ("vesselId", "hours", "detections")
        ):
            rows.append(entry)
        else:
            raise MaritimeError(
                "source_schema_error", "GFW returned an unexpected dataset/schema."
            )
    if not all(isinstance(r, dict) for r in rows):
        raise MaritimeError("source_schema_error", "Invalid GFW report rows.")
    return rows


class GFW:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=115, follow_redirects=False)

    def close(self):
        self.client.close()

    def _fetch_report(self, params, body, token):
        for attempt in range(1, 4):
            with self.client.stream(
                "POST",
                BASE,
                params=params,
                json=body,
                headers={
                    "Authorization": "Bearer " + token,
                    "User-Agent": "EVE-Maritime-MCP/0.1",
                },
            ) as response:
                status = response.status_code
                if status == 200:
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > MAX_BYTES:
                            raise MaritimeError(
                                "request_too_large",
                                "GFW report exceeds 20 MB; narrow AOI/window.",
                            )
                    return bytes(payload), response.headers.get("x-datasets"), attempt
                # Retry identical requests only for transient gateway/service errors.
                delay = 2 ** (attempt - 1)
                retry_after = response.headers.get("retry-after")
                if retry_after is not None:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = None
                if (
                    status not in (502, 503, 504)
                    or attempt == 3
                    or delay is None
                    or not 0 <= delay <= 10
                ):
                    code = (
                        "authentication_failed"
                        if status in (401, 403)
                        else "rate_limited"
                        if status == 429
                        else "source_unavailable"
                    )
                    raise MaritimeError(
                        code,
                        f"GFW returned HTTP {status} after {attempt} attempt(s); "
                        "request was not treated as zero vessels.",
                    )
            # Close the failed response before waiting; keep the per-token lock.
            time.sleep(delay)

    def report(self, bbox, start, end, dataset, matched=None):
        token = os.getenv("GFW_API_TOKEN", "").strip()
        if not token:
            raise MaritimeError(
                "credentials_required",
                "Set GFW_API_TOKEN in the server environment for GFW tools.",
            )
        params = {
            "datasets[0]": dataset,
            "date-range": f"{iso(start)},{iso(end)}",
            "format": "JSON",
            "temporal-resolution": "ENTIRE",
            "spatial-aggregation": "true",
            "spatial-resolution": "LOW",
            "group-by": "FLAG" if dataset == SAR else "VESSEL_ID",
        }
        if matched is not None:
            params["filters[0]"] = f"matched='{str(matched).lower()}'"
        body = {"geojson": polygon(bbox)}
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        key = hashlib.sha256(
            json.dumps([fingerprint, params, body], sort_keys=True).encode()
        ).hexdigest()
        root = cache_root() / "gfw"
        root.mkdir(exist_ok=True)
        path = root / f"{key}.json"
        try:
            with FileLock(str(root / f"{fingerprint}.lock"), timeout=120):
                if path.exists() and time.time() - path.stat().st_mtime < 3600:
                    try:
                        cached = json.loads(path.read_text(encoding="utf-8"))
                        rows = rows_from_report(cached["report"], dataset)
                        return rows, {**cached["provenance"], "cache_hit": True}
                    except (ValueError, KeyError, MaritimeError):
                        path.unlink(missing_ok=True)
                payload, dataset_header, attempts = self._fetch_report(
                    params, body, token
                )
                data = decode_report(payload)
                rows = rows_from_report(data, dataset)
                provenance = {
                    "provider": "Global Fishing Watch",
                    "dataset": dataset,
                    "api_version": "v3",
                    "retrieved_at": iso(utcnow()),
                    "request_parameters": params,
                    "response_sha256": hashlib.sha256(payload).hexdigest(),
                    "dataset_header": dataset_header,
                    "http_attempts": attempts,
                    "cache_hit": False,
                }
                # Credential-free response only; refuse accidental credential echoes.
                if token in json.dumps(data):
                    raise MaritimeError(
                        "source_schema_error",
                        "Unexpected credential echo; response was discarded.",
                    )
                json_write(path, {"report": data, "provenance": provenance})
                return rows, provenance
        except Timeout:
            raise MaritimeError(
                "provider_busy", "Another local GFW report is running; retry shortly."
            ) from None
        except httpx.HTTPError:
            raise MaritimeError(
                "source_unavailable",
                "GFW connection failed or timed out; retry with a smaller request.",
            ) from None


def presence_summary(rows):
    identities = [r.get("vesselId") for r in rows]
    if any(not v for v in identities) or len(set(identities)) != len(rows):
        raise MaritimeError(
            "source_schema_error", "Expected one ENTIRE row per GFW identity."
        )
    by_type = {}
    hours = 0.0
    for row in rows:
        value = row.get("hours")
        if (
            not isinstance(value, (int, float))
            or not __import__("math").isfinite(value)
            or value < 0
        ):
            raise MaritimeError("source_schema_error", "Invalid GFW presence hours.")
        hours += value
        entry = by_type.setdefault(
            row.get("vesselType") or "UNKNOWN",
            {"gfw_identity_count": 0, "presence_hours": 0.0},
        )
        entry["gfw_identity_count"] += 1
        entry["presence_hours"] += value
    return {
        "gfw_identity_count": len(identities),
        "distinct_valid_format_mmsi_count": len(
            {mmsi(r.get("mmsi")) for r in rows} - {None}
        ),
        "invalid_or_missing_mmsi_rows": sum(mmsi(r.get("mmsi")) is None for r in rows),
        "presence_hours": hours,
        "by_gfw_vessel_type": by_type,
        "observed_first_entry": min(
            (r["entryTimestamp"] for r in rows if r.get("entryTimestamp")), default=None
        ),
        "observed_last_exit": max(
            (r["exitTimestamp"] for r in rows if r.get("exitTimestamp")), default=None
        ),
    }


def detection_count(rows):
    counts = [r.get("detections") for r in rows]
    if any(
        not isinstance(v, (int, float))
        or not __import__("math").isfinite(v)
        or v < 0
        or int(v) != v
        for v in counts
    ):
        raise MaritimeError("source_schema_error", "Invalid SAR detection counts.")
    return int(sum(counts))
