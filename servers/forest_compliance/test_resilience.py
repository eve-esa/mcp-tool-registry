"""Unit tests for resilience.py (no network).

Covers parsing of the drought argument, the supply-status rule and the
matrix rows, markdown table and summary.
"""

import json

import pytest
import resilience


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("High", "high"),
        ({"risk_level": "medium", "spi": []}, "medium"),
        (json.dumps({"risk_level": "low"}), "low"),
        ("not json", "unknown"),
        ({"risk_level": "extreme"}, "unknown"),
        (None, "unknown"),
    ],
)
def test_drought_level(value, expected):
    """Levels, drought_index results (dict or JSON) and junk all map to a level or "unknown"."""
    assert resilience.drought_level(value) == expected


def test_parse_drought_accepts_string_and_rejects_lists():
    assert resilience.parse_drought('{"farm-01": "high"}') == {"farm-01": "high"}
    assert resilience.parse_drought(None) == {}
    with pytest.raises(ValueError):
        resilience.parse_drought([1, 2])


@pytest.mark.parametrize(
    ("eudr", "drought", "status"),
    [
        ("high", "low", "blocked"),
        ("medium", "low", "review"),
        ("unknown", "high", "review"),
        ("low", "high", "at risk"),
        ("low", "medium", "at risk"),
        ("low", "low", "secure"),
        ("low", "unknown", "review"),
    ],
)
def test_supply_status(eudr, drought, status):
    """EUDR high blocks; EUDR medium/unknown or no drought data needs review; then drought decides."""
    assert resilience.supply_status(eudr, drought) == status


def test_rows_and_markdown():
    """End to end over one high, one low and one failed plot: statuses, table lines and summary."""
    results = [
        {"id": "farm-01", "risk_level": "high", "deforestation_after_cutoff_ha": 2.233},
        {"id": "farm-02", "risk_level": "low", "deforestation_after_cutoff_ha": 0.0},
        {"id": "farm-03", "error": "bad geometry"},
    ]
    rows = resilience.build_rows(results, {"farm-01": "high", "farm-02": "high"})
    assert [r["supply_status"] for r in rows] == ["blocked", "at risk", "review"]
    assert rows[2]["eudr_risk"] == "unknown" and rows[2]["drought_risk"] == "unknown"

    table = resilience.markdown_table(rows).splitlines()
    assert table[0].startswith("| Farm | EUDR risk")
    assert len(table) == 2 + len(rows)
    assert "🔴 high (2.233 ha lost after 2020)" in table[2]
    assert "| farm-02 | 🟢 low | 🔴 high | 🟠 at risk |" in table[3]

    summary = resilience.summarise(rows)
    assert summary["by_supply_status"] == {"secure": 0, "at risk": 1, "review": 1, "blocked": 1}
    assert summary["missing_drought"] == ["farm-03"]


def test_require_drought_names_the_tool_to_call():
    with pytest.raises(ValueError, match="drought_index"):
        resilience.require_drought(resilience.parse_drought(None))
    with pytest.raises(ValueError, match="drought_index"):
        resilience.require_drought(resilience.parse_drought({}))
    resilience.require_drought({"farm-01": "unknown"})  # some data passed: per-plot gaps show as "review"
