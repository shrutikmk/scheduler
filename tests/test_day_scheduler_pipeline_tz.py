"""Timezone context injected into day-scheduler prompts."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from day_scheduler_pipeline import prepare_day_scheduler_user_for_prompt


def test_prompt_includes_ui_and_host_iana_when_agree() -> None:
    with patch(
        "day_scheduler_pipeline.system_iana_timezone_name",
        return_value="America/Chicago",
    ):
        user_txt, _, _ = prepare_day_scheduler_user_for_prompt(
            "plan my day",
            clock_minutes=600,
            clock_date=date(2026, 5, 9),
            client_timezone_iana="America/Chicago",
        )
    assert "[IANA — local wall times]" in user_txt
    assert "America/Chicago" in user_txt
    assert "cross-check OK" in user_txt


def test_prompt_flags_mismatch_between_ui_and_host_iana() -> None:
    with patch(
        "day_scheduler_pipeline.system_iana_timezone_name",
        return_value="America/New_York",
    ):
        user_txt, _, _ = prepare_day_scheduler_user_for_prompt(
            "plan my day",
            clock_minutes=600,
            clock_date=date(2026, 5, 9),
            client_timezone_iana="Europe/London",
        )
    assert "Europe/London" in user_txt and "America/New_York" in user_txt
    assert "≠" in user_txt


def test_prepare_prompt_injects_asap_fact_sheet() -> None:
    user_txt, _, _ = prepare_day_scheduler_user_for_prompt(
        "Put away groceries (ASAP); lesson at 6 PM.",
        clock_minutes=17 * 60 + 5,
        clock_date=date(2026, 5, 9),
        client_timezone_iana=None,
    )
    assert "[Facts — parsed from the user's message" in user_txt
    assert "Urgent / ASAP" in user_txt
    assert "slack before" in user_txt.lower()
