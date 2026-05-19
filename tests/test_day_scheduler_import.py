"""Tests for assistant import date filtering (day_scheduler_web helpers)."""

from __future__ import annotations

from day_scheduler_web import filter_assistant_import_dates


def test_filter_drops_clock_day_when_primary_is_future() -> None:
    touched = ["2026-05-18", "2026-05-19"]
    out = filter_assistant_import_dates(
        touched,
        client_clock_date="2026-05-18",
        client_local_date="2026-05-19",
    )
    assert out == ["2026-05-19"]


def test_filter_keeps_today_when_primary_is_today() -> None:
    touched = ["2026-05-18", "2026-05-19"]
    out = filter_assistant_import_dates(
        touched,
        client_clock_date="2026-05-18",
        client_local_date="2026-05-18",
    )
    assert out == ["2026-05-18", "2026-05-19"]
