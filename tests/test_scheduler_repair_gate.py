from __future__ import annotations

from mlx_scheduler_llm_api import _only_format_repairable_validation_reasons


def test_format_repair_gate_allows_parser_only_failures() -> None:
    assert _only_format_repairable_validation_reasons(
        ("invalid task bullet format: * x", "invalid duration: 30m")
    )
    assert _only_format_repairable_validation_reasons(("no valid parsed task bullets",))


def test_format_repair_gate_allows_overlap_reasons() -> None:
    assert _only_format_repairable_validation_reasons(
        ("tasks overlap on 2026-05-09: 'Freshen up' starts before the prior task ends",)
    )


def test_format_repair_gate_rejects_mixed_failures() -> None:
    assert not _only_format_repairable_validation_reasons(
        ("invalid task bullet format: x", "missing stylized TO DO banner as first visible line")
    )
