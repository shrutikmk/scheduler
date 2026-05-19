"""Unit tests for day-scheduler query JSON parsing (no MLX)."""

from __future__ import annotations

import day_scheduler_query_parser as qp


def test_extract_json_object_plain() -> None:
    s = 'noise {"primary_plan_date_iso": null} trailing'
    out = qp.extract_json_object(s)
    assert out == '{"primary_plan_date_iso": null}'


def test_extract_json_object_markdown_fence() -> None:
    s = "```json\n{\"a\": 1}\n```"
    out = qp.extract_json_object(s)
    assert out == '{"a": 1}'


def test_parse_query_parser_completion_full() -> None:
    raw = """
Here:
{"primary_plan_date_iso": "2026-05-10", "time_intent_summary": "no explicit times",
 "estimated_event_count": 7, "count_disclaimer": "Guess."}
"""
    p = qp.parse_query_parser_completion_text(raw)
    assert p.primary_plan_date_iso == "2026-05-10"
    assert "explicit" in p.time_intent_summary.lower()
    assert p.estimated_event_count == 7
    assert p.count_disclaimer == "Guess."


def test_parse_invalid_primary_date_becomes_none() -> None:
    raw = (
        '{"primary_plan_date_iso": "not-a-date", '
        '"time_intent_summary": "x", "estimated_event_count": 1, '
        '"count_disclaimer": "d"}'
    )
    p = qp.parse_query_parser_completion_text(raw)
    assert p.primary_plan_date_iso is None


def test_resolve_import_default_plan_date_prefers_parser() -> None:
    p = qp.ParsedQuery(primary_plan_date_iso="2026-05-10")
    assert qp.resolve_import_default_plan_date(p, client_clock_date_iso="2026-05-09") == (
        "2026-05-10"
    )


def test_resolve_import_default_plan_date_fallback_anchor() -> None:
    p = qp.ParsedQuery(primary_plan_date_iso=None)
    assert qp.resolve_import_default_plan_date(p, client_clock_date_iso="2026-05-09") == (
        "2026-05-09"
    )


def test_resolve_import_default_plan_date_prefers_planner_focus() -> None:
    p = qp.ParsedQuery(primary_plan_date_iso=None)
    assert qp.resolve_import_default_plan_date(
        p,
        client_clock_date_iso="2026-05-09",
        planner_focus_date_iso="2026-05-12",
    ) == ("2026-05-12")


def test_format_query_parser_host_facts() -> None:
    p = qp.ParsedQuery(
        primary_plan_date_iso="2026-05-10",
        time_intent_summary="morning",
        estimated_event_count=3,
        count_disclaimer="Approx.",
    )
    blk = qp.format_query_parser_host_facts(p)
    assert "[Facts — query parser]" in blk
    assert "2026-05-10" in blk
    assert "morning" in blk


def test_strip_redacted_thinking() -> None:
    raw = "<think>x</think>{\"primary_plan_date_iso\": null}"
    assert qp.strip_redacted_thinking(raw).startswith("{")
