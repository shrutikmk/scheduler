from __future__ import annotations

from schedule_parse import normalize_schedule_bullets_for_parser, validate_schedule_response

GOOD = """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* [10:00 AM] - Make breakfast - 0h30m
* [10:30 AM] - 10k steps - 1h00m
"""


def test_validate_schedule_response_accepts_markdown_heading_plan() -> None:
    md = (
        "# Today's plan\n\n"
        "- **[10:00 AM]** — Deep work — **2h05m**\n\n"
        "Wrap-up text acknowledges your goals and welcomes edits.\n"
    )
    out = validate_schedule_response(
        md,
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert out.ok
    assert len(out.parsed_tasks) == 1


def test_validate_schedule_response_accepts_strict_output() -> None:
    out = validate_schedule_response(
        GOOD,
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert out.ok
    assert len(out.parsed_tasks) == 2


def test_normalize_and_validate_bare_minutes_duration() -> None:
    raw = """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* [10:00 AM] - Make breakfast - 30m
* [10:30 AM] - Steps - 90m
"""
    norm = normalize_schedule_bullets_for_parser(raw)
    out = validate_schedule_response(
        norm,
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert out.ok
    assert len(out.parsed_tasks) == 2
    assert "0h30m" in norm
    assert "1h30m" in norm


def test_normalize_does_not_touch_valid_lines() -> None:
    assert normalize_schedule_bullets_for_parser(GOOD) == GOOD.strip()


def test_normalize_bare_hours_suffix() -> None:
    raw = """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* [2:00 PM] - Work block - 2h
"""
    norm = normalize_schedule_bullets_for_parser(raw)
    out = validate_schedule_response(
        norm,
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert out.ok
    assert "2h00m" in norm


def test_validate_schedule_response_rejects_overlapping_tasks() -> None:
    overlap = """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* [8:00 AM] - Make breakfast - 0h30m
* [8:15 AM] - Freshen up - 0h20m
"""
    out = validate_schedule_response(
        overlap,
        default_plan_date="2026-05-09",
        client_minute_of_day=7 * 60,
    )
    assert not out.ok
    assert any("overlap" in r for r in out.reasons)


def test_validate_schedule_response_rejects_bad_format_and_past_time() -> None:
    out = validate_schedule_response(
        "Here is a plan\n* [8:00 AM] - Thing - 0h30m",
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert not out.ok
    assert any("scheduler header" in r for r in out.reasons)
    assert any("before client NOW" in r for r in out.reasons)


def test_validate_schedule_response_allows_back_to_back_tasks() -> None:
    snug = """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* [8:00 AM] - Make breakfast - 0h30m
* [8:30 AM] - Freshen up - 0h20m
"""
    out = validate_schedule_response(
        snug,
        default_plan_date="2026-05-09",
        client_minute_of_day=7 * 60,
    )
    assert out.ok
    assert len(out.parsed_tasks) == 2


def test_validate_schedule_response_checks_required_habits() -> None:
    out = validate_schedule_response(
        """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* [10:00 AM] - Make breakfast - 0h30m
""",
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
        host_context=(
            "[Required habits — must schedule if absent]\n"
            "- 10k steps: 1h00m on 2026-05-09;"
        ),
    )
    assert not out.ok
    assert any("missing required habit" in r for r in out.reasons)


def test_validate_future_plan_allows_morning_when_anchor_is_today() -> None:
    tomorrow_plan = (
        "# Tomorrow plan\n\n"
        "* [7:00 AM] - Breakfast - 0h30m\n"
        "* [8:00 AM] - Deep work - 1h00m\n"
    )
    out = validate_schedule_response(
        tomorrow_plan,
        default_plan_date="2026-05-10",
        client_minute_of_day=17 * 60,
        client_anchor_date_iso="2026-05-09",
    )
    assert out.ok
    assert len(out.parsed_tasks) == 2


def test_validate_schedule_response_allows_empty_plan_bullet() -> None:
    out = validate_schedule_response(
        """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* (empty — nothing left on today's plan.)
""",
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert out.ok
    assert out.parsed_tasks == ()
