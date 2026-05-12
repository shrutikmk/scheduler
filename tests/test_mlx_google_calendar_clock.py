"""Clock context bundles for Calendar MLX CLI."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import mlx_google_calendar_cli as cal


def test_calendar_tz_label_prefers_zoneinfo_key() -> None:
    dt = datetime(2026, 5, 9, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert cal._calendar_tz_label(dt) == "America/Los_Angeles"


def test_calendar_clock_core_lines_includes_weekday_iso_utc() -> None:
    dt = datetime(2026, 5, 9, 14, 30, tzinfo=ZoneInfo("America/New_York"))
    lines = cal._calendar_clock_core_lines(dt)
    text = "\n".join(lines)
    assert "Saturday" in text
    assert "2026-05-09" in text
    assert "Z" in text
    assert "America/New_York" in text


def test_calendar_clock_system_suffix_starts_clock_header() -> None:
    sut = cal.calendar_clock_system_suffix()
    assert "[Clock — this request]" in sut
