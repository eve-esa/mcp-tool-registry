"""Shared validation, temporal contracts and credential-free result envelopes."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class MaritimeError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def runtime_root():
    """Use the lab project root in examples/, or the standalone server directory."""
    server_dir = Path(__file__).resolve().parent
    if server_dir.parent.name == "examples":
        return server_dir.parent.parent
    return server_dir


def utcnow():
    return datetime.now(UTC)


def iso(value: datetime):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def timestamp(value: str):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if len(value) == 10:
            result = result.replace(tzinfo=UTC)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (TypeError, ValueError):
        raise MaritimeError(
            "invalid_request", "Use ISO dates or timestamps with a UTC offset."
        ) from None


def bbox_check(bbox: list[float], max_degrees: float = 10):
    if len(bbox) != 4 or not all(
        isinstance(x, (int, float)) and math.isfinite(x) for x in bbox
    ):
        raise MaritimeError(
            "invalid_request",
            "bbox needs four finite WGS84 numbers: west,south,east,north.",
        )
    w, s, e, n = bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise MaritimeError(
            "invalid_request",
            "Invalid bbox; split antimeridian-crossing areas into separate requests.",
        )
    if e - w > max_degrees or n - s > max_degrees:
        raise MaritimeError(
            "request_too_large", f"Each bbox side must be <= {max_degrees} degrees."
        )
    return list(map(float, bbox))


def area(bbox):
    return {
        "bbox_array": bbox,
        "bbox_string": ",".join(map(str, bbox)),
        "crs": "EPSG:4326",
    }


def polygon(bbox):
    w, s, e, n = bbox
    return {
        "type": "Polygon",
        "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]],
    }


def window(
    start=None,
    end=None,
    period=None,
    timezone="Europe/Rome",
    *,
    latest_day=None,
    lag_days=5,
    max_days=31,
    now=None,
):
    """Half-open UTC intervals. Defaults disclose estimates rather than inventing availability."""
    now = now or utcnow()
    requested = {"start": start, "end": end, "period": period, "timezone": timezone}
    if start or end:
        if not start or not end or period:
            raise MaritimeError(
                "invalid_request", "Supply both start/end OR a period, not both."
            )
        a, b = timestamp(start), timestamp(end)
        assumption = (
            "Explicit window; date-only boundaries mean UTC midnight. End is exclusive."
        )
    elif period in ("yesterday", "last_week", "today"):
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            raise MaritimeError("invalid_request", "Unknown IANA timezone.") from None
        day = now.astimezone(zone).date()
        if period == "last_week":
            end_day = day - timedelta(days=day.weekday())
            start_day = end_day - timedelta(days=7)
        else:
            start_day = day - timedelta(days=period == "yesterday")
            end_day = start_day + timedelta(days=1)
        a = datetime.combine(start_day, time.min, zone).astimezone(UTC)
        b = datetime.combine(end_day, time.min, zone).astimezone(UTC)
        assumption = f"{period}: calendar boundaries in {timezone}; last_week means previous Monday–Monday."
    elif period in (None, "latest", "now"):
        if latest_day:
            a = timestamp(latest_day)
            b = a + timedelta(days=1)
            assumption = "Latest published daily file in the pinned repository; readability checked separately. Not live."
        else:
            b = datetime.combine(now.date() - timedelta(days=lag_days), time.min, UTC)
            a = b - timedelta(days=1)
            assumption = (
                f"Estimated recent complete day with a conservative {lag_days}-day lag. "
                "Not confirmed latest availability; no automatic historical substitution."
            )
    else:
        raise MaritimeError(
            "invalid_request",
            "period must be yesterday, last_week, today, latest or now.",
        )
    if b <= a or b - a > timedelta(days=max_days):
        raise MaritimeError(
            "request_too_large", f"Window must be positive and <= {max_days} days."
        )
    return (
        a,
        b,
        {
            "requested_window": requested,
            "effective_window": {"start": iso(a), "end": iso(b)},
            "time_assumption": assumption,
            "interval": "[start,end)",
        },
    )


def mmsi(value):
    if isinstance(value, (int, float)) and math.isfinite(value) and int(value) == value:
        value = str(int(value))
    return (
        value if isinstance(value, str) and re.fullmatch(r"[1-9]\d{8}", value) else None
    )


def finite(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def error_result(exc: Exception):
    if isinstance(exc, MaritimeError):
        return {
            "ok": False,
            "status": exc.code,
            "message": str(exc),
            "retrieved_at": iso(utcnow()),
        }
    # Never include raw HTTP exceptions, payloads or environment values in model context.
    return {
        "ok": False,
        "status": "processing_error",
        "message": "Processing failed; see local server diagnostics.",
        "retrieved_at": iso(utcnow()),
    }
