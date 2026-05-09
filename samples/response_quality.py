"""Response validation helpers for fast/fallback scheduler generation."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mlx_day_scheduler_pipeline import strip_reasoning_blocks

DEFAULT_EMBEDDING_MODEL_DIR = Path.home() / "models" / "Qwen3-Embedding-4B"
DEFAULT_SIMILARITY_THRESHOLD = 0.90

_embed_lock = threading.Lock()
_embed_pipe: Any | None = None
_embed_model_path: str | None = None


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


def _flatten_embedding(obj: Any) -> list[float]:
    """Mean-pool common feature-extraction pipeline outputs into one vector."""
    cur = obj
    while isinstance(cur, list) and len(cur) == 1 and isinstance(cur[0], list):
        cur = cur[0]
    if isinstance(cur, list) and cur and all(isinstance(x, (int, float)) for x in cur):
        return [float(x) for x in cur]
    if isinstance(cur, list) and cur and isinstance(cur[0], list):
        rows = [
            [float(x) for x in row]
            for row in cur
            if isinstance(row, list) and all(isinstance(x, (int, float)) for x in row)
        ]
        if not rows:
            return []
        dims = len(rows[0])
        pooled = [0.0] * dims
        used = 0
        for row in rows:
            if len(row) != dims:
                continue
            used += 1
            for i, val in enumerate(row):
                pooled[i] += val
        return [x / used for x in pooled] if used else []
    return []


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def embedding_similarity(
    first: str,
    second: str,
    *,
    model_path: str | None = None,
) -> float:
    """Compute semantic similarity with Qwen3-Embedding-4B, loaded lazily."""
    global _embed_pipe, _embed_model_path
    resolved = (
        model_path
        or os.environ.get("SCHEDULER_EMBEDDING_MODEL", "").strip()
        or str(DEFAULT_EMBEDDING_MODEL_DIR)
    )
    with _embed_lock:
        if _embed_pipe is None or _embed_model_path != resolved:
            from transformers import pipeline

            _embed_pipe = pipeline(
                "feature-extraction",
                model=resolved,
                trust_remote_code=True,
            )
            _embed_model_path = resolved
        pipe = _embed_pipe

    left = _flatten_embedding(pipe(normalize_schedule_for_similarity(first)))
    right = _flatten_embedding(pipe(normalize_schedule_for_similarity(second)))
    return cosine_similarity(left, right)


__all__ = [
    "DEFAULT_EMBEDDING_MODEL_DIR",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "SelfGrade",
    "cosine_similarity",
    "embedding_similarity",
    "normalize_schedule_for_similarity",
    "parse_self_grade",
]
