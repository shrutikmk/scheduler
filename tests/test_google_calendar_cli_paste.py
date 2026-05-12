"""Paste helpers for Google Calendar MLX CLI."""

from google_calendar_cli import _paste_block_closer, _pasted_multi_line_hint


def test_paste_closer_exact() -> None:
    assert _paste_block_closer("  /end  ")
    assert _paste_block_closer("/paste-end")
    assert not _paste_block_closer("Departs Thu")


def test_pasted_hint_empty_for_short() -> None:
    assert _pasted_multi_line_hint("one liner") == ""


def test_pasted_hint_for_itinerary_shape() -> None:
    blob = """Southwest Airlines
Departs Thu, May 14
(AUS-airport line)
Layover:

Arrives Thu, May 14
"""
    hint = _pasted_multi_line_hint(blob)
    assert "[Host — pasted / multi-line block]" in hint
