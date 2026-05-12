"""Shared helpers for live ledger LLM progress (financial UI + background jobs)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol


class LedgerProgressTarget(Protocol):
    ledger_llm_progress: dict[str, Any]
    ledger_llm_progress_lock: Any


def short_model_label(model: str | None) -> str:
    """Short label for UI (e.g. ``Qwen3-8B``)."""
    if not model or not str(model).strip():
        return "gateway default"
    s = str(model).strip()
    if s.startswith("http://") or s.startswith("https://"):
        return Path(s).name or s
    low = s.lower()
    if "qwen3-8b" in low or s.endswith("Qwen3-8B"):
        return "Qwen3-8B"
    if "qwen3-14b" in low or s.endswith("Qwen3-14B"):
        return "Qwen3-14B"
    name = Path(s).name
    if name and name != s and "/" not in name:
        return name
    if "/" in s and not s.startswith("/"):
        tail = s.rsplit("/", 1)[-1]
        return tail or s
    return name if name else s


PROGRESS_LOG_MAX = 96


def _normalize_log_fragments(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    out: list[str] = []
    for x in raw:
        if not x:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _append_tail_list(
    lst: list[str],
    snippets: Any,
    *,
    dedupe_consecutive: bool,
) -> None:
    for ln in _normalize_log_fragments(snippets):
        if dedupe_consecutive and lst and lst[-1] == ln:
            continue
        lst.append(ln)
    while len(lst) > PROGRESS_LOG_MAX:
        lst.pop(0)


def merge_ledger_llm_progress(target: LedgerProgressTarget, **kwargs: Any) -> None:
    detail_incoming = kwargs.get("detail") if kwargs else None
    step_incoming = kwargs.get("step") if kwargs else None
    extra_logs = kwargs.pop("log_append", None)

    details_str = ""
    if isinstance(detail_incoming, str):
        details_str = detail_incoming.strip()

    auto_detail_line: str | None = None
    if details_str:
        if step_incoming is not None and str(step_incoming).strip():
            auto_detail_line = f"{step_incoming}: {details_str}"
        else:
            auto_detail_line = details_str

    with target.ledger_llm_progress_lock:
        prog = target.ledger_llm_progress
        prog.update(kwargs)
        pl = prog.setdefault("progress_log", [])
        if isinstance(pl, list):
            pass
        else:
            prog["progress_log"] = []
            pl = prog["progress_log"]

        assert isinstance(pl, list)
        if auto_detail_line:
            _append_tail_list(pl, [auto_detail_line], dedupe_consecutive=True)

        if extra_logs is not None:
            _append_tail_list(pl, extra_logs, dedupe_consecutive=False)

        prog["ts"] = time.time()


def initial_ledger_llm_progress() -> dict[str, Any]:
    return {
        "active": False,
        "phase": "idle",
        "step": "",
        "detail": "",
        "model": "",
        "gateway": "",
        "percent": None,
        "title_round": None,
        "mix_round": None,
        "error": None,
        "progress_log": [],
        "ts": 0.0,
    }
