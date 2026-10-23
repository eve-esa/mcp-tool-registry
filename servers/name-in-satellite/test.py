#!/usr/bin/env python3
"""Regression checks for name-in-satellite. No network."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import date, timedelta

from server import (
    LETTERS,
    MAX_TEXT_CHARS,
    _candidate_orders,
    _font,
    _output_path,
    _period_plan,
    _place,
    spell_name,
    spell_name_timeseries,
)


def test_short_window_rejects_extra_frames() -> None:
    # round() built 2025-06-03/2025-06-02 for this input.
    result = json.loads(
        asyncio.run(
            spell_name_timeseries(
                "a",
                start_date="2025-06-01",
                end_date="2025-06-05",
                frames=6,
            )
        )
    )
    assert result == {"error": "frames cannot exceed the number of days in the window."}


def test_periods_cover_the_window_without_gaps() -> None:
    start = date(2025, 1, 1)
    for span in range(2, 40):
        end = start + timedelta(days=span - 1)
        for frames in range(2, min(24, span) + 1):
            periods = _period_plan(start, end, frames)
            assert isinstance(periods, list)
            assert periods[0][0] == start
            assert periods[-1][1] == end
            for index, (p0, p1) in enumerate(periods):
                assert p0 <= p1
                if index:
                    assert p0 == periods[index - 1][1] + timedelta(days=1)
                assert (p1 - p0).days + 1 in {span // frames, span // frames + 1}


def test_repeated_letters_use_distinct_places() -> None:
    for seed in range(30):
        orders = _candidate_orders(list("aaa"), random.Random(seed))
        places = [_place(order[0]) for order in orders]
        assert len(set(places)) == 3
        for order in orders:
            assert len({_place(variant_id) for variant_id in order}) == len(order)


def test_single_place_digit_can_repeat() -> None:
    orders = _candidate_orders(list("88"), random.Random(0))
    assert _place(orders[0][0]) == _place(orders[1][0])
    assert len({_place(variant_id) for variant_id in LETTERS if LETTERS[variant_id]["letter"] == "8"}) == 1


def test_font_covers_icelandic_eth() -> None:
    font, unicode_ok = _font(20)
    assert unicode_ok
    assert font.getmask("ð").getbbox() is not None
    assert font.getmask("Breiðamerkurjökull").getbbox() is not None


def test_output_paths_differ() -> None:
    first = _output_path("eva", "sentinel2", ".png")
    second = _output_path("eva", "sentinel2", ".png")
    assert first != second
    assert first.name.startswith("name_eva_sentinel2_")


def test_text_length_cap_does_not_fetch() -> None:
    result = json.loads(asyncio.run(spell_name("a" * (MAX_TEXT_CHARS + 1))))
    assert result == {"error": f"text must be at most {MAX_TEXT_CHARS} characters."}


if __name__ == "__main__":
    test_short_window_rejects_extra_frames()
    test_periods_cover_the_window_without_gaps()
    test_repeated_letters_use_distinct_places()
    test_single_place_digit_can_repeat()
    test_font_covers_icelandic_eth()
    test_output_paths_differ()
    test_text_length_cap_does_not_fetch()
    print("ok")
