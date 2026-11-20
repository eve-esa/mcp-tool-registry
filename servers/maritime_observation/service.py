"""Five tool implementations, shared by the MCP server and deterministic tests."""

from datetime import timedelta

from ais import AIS
from common import MaritimeError, area, bbox_check, iso, utcnow, window
from gfw import GFW, PRESENCE, SAR, detection_count, presence_summary
from render import positions_artifacts, summary_artifacts
from scene import load_scene
from storage import geojson

GFW_ATTRIBUTION = {
    "text": "Powered by Global Fishing Watch.",
    "url": "https://globalfishingwatch.org/",
    "terms_url": "https://globalfishingwatch.org/our-apis/documentation/docs/license-rate-limits",
    "usage": "Noncommercial use; preserve attribution in downstream displays and exports.",
}

PRESENCE_LIMITS = [
    "GFW identities and valid-format MMSIs do not establish unique physical ships.",
    "Presence hours represent hourly AIS sampling, not tracks, arrivals or continuous occupancy.",
    "Differences can reflect reception, coverage and processing as well as vessel activity.",
]
AIS_LIMITS = [
    "AIS positions and identities are reported observations, not independently verified physical ships.",
    "Archive coverage is uneven and delayed; absence of reports does not prove absence of vessels.",
    "Nearest/latest reports are selected without interpolation; original coordinates and timestamps are retained.",
]


def envelope(bbox, temporal):
    return {
        "ok": True,
        "status": "ok",
        "area": area(bbox),
        **temporal,
        "retrieved_at": iso(utcnow()),
    }


def summarize_presence(bbox, start=None, end=None, period=None, timezone="Europe/Rome"):
    bbox = bbox_check(bbox)
    a, b, temporal = window(start, end, period, timezone)
    provider = GFW()
    try:
        rows, source = provider.report(bbox, a, b, PRESENCE)
    finally:
        provider.close()
    metrics = presence_summary(rows)
    result = {
        **envelope(bbox, temporal),
        "status": "ok" if rows else "no_data",
        "metrics": metrics,
        "attribution": GFW_ATTRIBUTION,
        "source": source,
        "source_temporal_resolution": "hourly presence, ENTIRE aggregation",
        "newest_observation": metrics["observed_last_exit"],
        "limitations": PRESENCE_LIMITS,
    }
    types = metrics["by_gfw_vessel_type"]
    result["artifacts"] = summary_artifacts(
        result,
        list(types),
        [v["presence_hours"] for v in types.values()],
        "GFW AIS presence hours by vessel type",
    )
    return result


def compare_presence(
    bbox, baseline_start, baseline_end, comparison_start, comparison_end
):
    bbox = bbox_check(bbox)
    a, b, first_time = window(baseline_start, baseline_end)
    c, d, second_time = window(comparison_start, comparison_end)
    if b - a != d - c:
        raise MaritimeError(
            "invalid_request", "Comparison windows must have equal durations."
        )
    provider = GFW()
    try:
        first_rows, first_source = provider.report(bbox, a, b, PRESENCE)
        second_rows, second_source = provider.report(bbox, c, d, PRESENCE)
    finally:
        provider.close()
    if first_source.get("dataset_header") != second_source.get("dataset_header"):
        raise MaritimeError(
            "source_version_mismatch",
            "GFW dataset headers differ; comparison was discarded.",
        )
    first, second = presence_summary(first_rows), presence_summary(second_rows)
    changes = {}
    for metric in (
        "gfw_identity_count",
        "distinct_valid_format_mmsi_count",
        "presence_hours",
    ):
        changes[metric] = {
            "absolute_change": second[metric] - first[metric],
            "percent_change": (second[metric] - first[metric]) * 100 / first[metric]
            if first[metric]
            else None,
        }
    result = {
        **envelope(bbox, {}),
        "baseline": {**first_time, "metrics": first, "source": first_source},
        "comparison": {**second_time, "metrics": second, "source": second_source},
        "changes": changes,
        "attribution": GFW_ATTRIBUTION,
        "limitations": PRESENCE_LIMITS,
        "status": "ok"
        if first_rows and second_rows
        else "no_data_in_one_or_both_periods",
    }
    result["artifacts"] = summary_artifacts(
        result,
        ["Baseline", "Comparison"],
        [first["presence_hours"], second["presence_hours"]],
        "GFW AIS presence hours • equal-duration windows",
    )
    return result


def summarize_sar(bbox, start=None, end=None, period=None, timezone="Europe/Rome"):
    bbox = bbox_check(bbox)
    a, b, temporal = window(start, end, period, timezone, lag_days=6)
    counts, sources = {}, {}
    provider = GFW()
    try:
        for label, matched in [
            ("total", None),
            ("matched", True),
            ("unmatched", False),
        ]:
            rows, sources[label] = provider.report(bbox, a, b, SAR, matched)
            counts[label] = detection_count(rows)
    finally:
        provider.close()
    if counts["matched"] + counts["unmatched"] != counts["total"]:
        raise MaritimeError(
            "inconsistent_report",
            "SAR matched + unmatched do not equal total; no summary was published.",
        )
    if len({s.get("dataset_header") for s in sources.values()}) != 1:
        raise MaritimeError(
            "source_version_mismatch", "GFW SAR dataset headers differ."
        )
    counts["unmatched_percentage"] = (
        counts["unmatched"] * 100 / counts["total"] if counts["total"] else None
    )
    result = {
        **envelope(bbox, temporal),
        "status": "ok" if counts["total"] else "no_data",
        "metrics": counts,
        "attribution": GFW_ATTRIBUTION,
        "sources": sources,
        "source_temporal_resolution": "sampled SAR acquisitions, ENTIRE aggregation",
        "newest_observation": None,
        "limitations": [
            "Counts are processed Sentinel-1 detections, not unique vessels or new detections produced by this server.",
            "Unmatched is GFW's AIS matching result; it does not establish illegal activity or intentional AIS disabling.",
            "No detections may reflect acquisition coverage, vessel size or detection performance.",
            "FLAG aggregation does not establish exact latest acquisition timestamps.",
        ],
    }
    result["artifacts"] = summary_artifacts(
        result,
        ["Matched", "Unmatched"],
        [counts["matched"], counts["unmatched"]],
        "GFW processed SAR detections",
    )
    return result


def get_ais_positions(
    bbox, start=None, end=None, period=None, timezone="Europe/Rome", limit=1000
):
    bbox = bbox_check(bbox, max_degrees=2)
    provider = AIS()
    try:
        latest_day = (
            provider.latest_day()
            if not start and not end and period in (None, "latest", "now")
            else None
        )
        a, b, temporal = window(
            start, end, period, timezone, latest_day=latest_day, max_days=1
        )
        records, source = provider.positions(bbox, a, b, limit)
    finally:
        provider.close()
    result = {
        **envelope(bbox, temporal),
        "status": "ok" if records else "no_data",
        "source": source,
        "newest_observation": source["newest_observation"],
        "limitations": AIS_LIMITS,
        "record_preview": records[:25],
        "geojson_preview": geojson(records[:25]),
        "preview_truncated": len(records) > 25,
    }
    result["artifacts"] = positions_artifacts(records, bbox, result)
    return result


def prepare_scene_context(
    scene_id, bbox, max_age_seconds=300, highlight_seconds=60, limit=1000
):
    bbox = bbox_check(bbox, max_degrees=1)
    if not 1 <= max_age_seconds <= 900 or not 0 <= highlight_seconds <= max_age_seconds:
        raise MaritimeError(
            "invalid_request",
            "max_age_seconds must be 1–900; highlight_seconds must be 0–max_age_seconds.",
        )
    scene, reference, footprint = load_scene(scene_id, bbox)
    a, b = (
        reference - timedelta(seconds=max_age_seconds),
        reference + timedelta(seconds=max_age_seconds, microseconds=1),
    )
    provider = AIS()
    try:
        records, source = provider.positions(bbox, a, b, limit, reference, footprint)
    finally:
        provider.close()
    result = {
        **envelope(
            bbox,
            {
                "effective_window": {"start": iso(a), "end": iso(b)},
                "interval": "[start,end)",
                "time_assumption": "Exact tile SENSING_TIME controls AIS selection; original coordinates retained.",
            },
        ),
        "status": "ok" if records else "no_data",
        "scene": scene,
        "source": source,
        "max_age_seconds": max_age_seconds,
        "highlight_seconds": highlight_seconds,
        "highlighted_returned_reports": sum(
            abs(r["delta_seconds"]) <= highlight_seconds for r in records
        ),
        "newest_observation": source["newest_observation"],
        "record_preview": records[:25],
        "geojson_preview": geojson(records[:25]),
        "preview_truncated": len(records) > 25,
        "limitations": AIS_LIMITS
        + [
            "Sentinel-2 is optical context; processed SAR counts are supplied separately by summarize_sar.",
            "This overlay is not a trained image detector or verified SAR–AIS match.",
            "Clouds and pixel validity are not classified by this renderer; inspect the image before interpreting alignment.",
            "Tile time differs from catalogue time; per-pixel timing and movement remain uncertain.",
        ],
    }
    result["artifacts"] = positions_artifacts(
        records, bbox, result, scene, highlight_seconds
    )
    return result
