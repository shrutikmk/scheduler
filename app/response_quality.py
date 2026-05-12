"""Response validation helpers for day-scheduler generation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from mlx_day_scheduler_pipeline import strip_reasoning_blocks


@dataclass(frozen=True)
class SelfGrade:
    passed: bool
    score: float
    reasons: tuple[str, ...]


def parse_self_grade(text: str, *, min_score: float = 0.80) -> SelfGrade:
    """Parse strict-ish grader JSON. Malformed output fails closed."""
    body = strip_reasoning_blocks(text).strip()
    m = re.search(r"\{[\s\S]*\}", body)
    if m:
        body = m.group(0)
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return SelfGrade(False, 0.0, ("grader returned invalid JSON",))
    if not isinstance(raw, dict):
        return SelfGrade(False, 0.0, ("grader JSON was not an object",))

    passed_raw = raw.get("pass")
    score_raw = raw.get("score")
    reasons_raw = raw.get("reasons")
    passed = bool(passed_raw) if isinstance(passed_raw, bool) else False
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = 0.0
    reasons = (
        tuple(str(x) for x in reasons_raw if isinstance(x, (str, int, float)))
        if isinstance(reasons_raw, list)
        else ()
    )
    if not passed:
        reasons = reasons or ("grader marked response as failing",)
    if score < min_score:
        passed = False
        reasons = reasons + (f"grader score {score:.2f} below {min_score:.2f}",)
    return SelfGrade(passed, max(0.0, min(1.0, score)), reasons)


def normalize_schedule_for_similarity(text: str) -> str:
    """Keep semantic schedule content, dropping banner chrome and spacing noise."""
    body = strip_reasoning_blocks(text)
    lines: list[str] = []
    for line in body.splitlines():
        ls = line.strip()
        if not ls or ls.startswith(("╭", "│", "╰")):
            continue
        lines.append(ls)
    return "\n".join(lines)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


__all__ = [
    "SelfGrade",
    "cosine_similarity",
    "normalize_schedule_for_similarity",
    "parse_self_grade",
]
