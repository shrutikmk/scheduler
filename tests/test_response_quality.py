from __future__ import annotations

from response_quality import cosine_similarity, normalize_schedule_for_similarity, parse_self_grade


def test_parse_self_grade_valid_json() -> None:
    grade = parse_self_grade('{"pass": true, "score": 0.91, "reasons": []}', min_score=0.8)
    assert grade.passed
    assert grade.score == 0.91


def test_parse_self_grade_fails_closed() -> None:
    grade = parse_self_grade("looks good", min_score=0.8)
    assert not grade.passed
    assert "invalid JSON" in grade.reasons[0]


def test_cosine_similarity() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_normalize_schedule_for_similarity_drops_banner() -> None:
    out = normalize_schedule_for_similarity(
        """╭────╮
│TODO│
╰────╯
* [10:00 AM] - Task - 0h30m
"""
    )
    assert out == "* [10:00 AM] - Task - 0h30m"
