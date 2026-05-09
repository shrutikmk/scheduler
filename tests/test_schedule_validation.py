from __future__ import annotations

from schedule_parse import validate_schedule_response

GOOD = """╭──────────────────────────────────╮
│           T O   D O              │
╰──────────────────────────────────╯
* [10:00 AM] - Make breakfast - 0h30m
* [10:30 AM] - 10k steps - 1h00m
"""


def test_validate_schedule_response_accepts_strict_output() -> None:
    out = validate_schedule_response(
        GOOD,
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert out.ok
    assert len(out.parsed_tasks) == 2


def test_validate_schedule_response_rejects_bad_format_and_past_time() -> None:
    out = validate_schedule_response(
        "Here is a plan\n* [8:00 AM] - Thing - 0h30m",
        default_plan_date="2026-05-09",
        client_minute_of_day=9 * 60,
    )
    assert not out.ok
    assert any("missing stylized" in r for r in out.reasons)
    assert any("before client NOW" in r for r in out.reasons)


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
