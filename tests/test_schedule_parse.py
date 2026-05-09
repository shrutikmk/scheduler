from __future__ import annotations

import schedule_parse as sp


def test_duration_to_minutes() -> None:
    assert sp.duration_to_minutes("2h05m") == 125
    assert sp.duration_to_minutes("0h20m") == 20
    assert sp.duration_to_minutes("not-a-duration") is None


def test_collect_tasks_mixed_dates_and_default() -> None:
    text = (
        "<think>ignore</think>\n"
        "* [2026-05-10] [8:00 AM] - Other day - 0h45m\n"
        "* [5:00 PM] - Same anchor day - 1h00m\n"
    )
    dates, rows = sp.collect_tasks_with_dates(text, default_plan_date="2026-05-08")
    assert set(dates) == {"2026-05-08", "2026-05-10"}
    assert len(rows) == 2
    assert rows[0].plan_date_iso == "2026-05-10"
    assert rows[0].start_label == "8:00 AM"
    assert rows[0].title == "Other day"
    assert rows[0].duration_minutes == 45
    assert rows[1].plan_date_iso == "2026-05-08"
    assert rows[1].start_label == "5:00 PM"


def test_infer_planner_date_hints_tomorrow_and_literal() -> None:
    out = sp.infer_planner_date_hints(
        "Prep for call tomorrow; also 2026-12-01 travel",
        anchor_date_iso="2026-05-08",
    )
    assert "2026-05-09" in out
    assert "2026-12-01" in out


def test_planner_facts_injection() -> None:
    block = sp.planner_facts_injection(
        "Meeting 2026-05-15 and tonight dinner",
        anchor_date_iso="2026-05-08",
    )
    assert block is not None
    assert "2026-05-08" in block
    assert "2026-05-15" in block
    assert "YYYY-MM-DD" in block
