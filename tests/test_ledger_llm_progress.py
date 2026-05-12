"""Tests for ledger LLM live progress helpers."""

from __future__ import annotations

import threading

from ledger_llm_progress import (
    PROGRESS_LOG_MAX,
    initial_ledger_llm_progress,
    merge_ledger_llm_progress,
    short_model_label,
)


class _FakeTarget:
    """Minimal LedgerProgressTarget for merge_* tests."""

    def __init__(self) -> None:
        self.ledger_llm_progress_lock = threading.Lock()
        self.ledger_llm_progress = initial_ledger_llm_progress()


def test_short_model_label_qwen8() -> None:
    assert "8" in short_model_label("Qwen/Qwen3-8B") or "Qwen" in short_model_label("Qwen/Qwen3-8B")


def test_short_model_label_path_tail() -> None:
    s = short_model_label("/Users/x/models/Qwen3-8B")
    assert "Qwen3-8B" in s


def test_merge_progress_log_records_detail_updates() -> None:
    tgt = _FakeTarget()
    merge_ledger_llm_progress(tgt, step="warm", detail="hello")
    lines = tgt.ledger_llm_progress["progress_log"]
    assert lines == ["warm: hello"]

    merge_ledger_llm_progress(tgt, step="warm", detail="hello")
    assert lines == ["warm: hello"]

    merge_ledger_llm_progress(tgt, step="next", detail="again")
    assert lines[-1] == "next: again"


def test_merge_progress_log_explicit_appends_even_after_duplicate_detail() -> None:
    tgt = _FakeTarget()
    merge_ledger_llm_progress(tgt, step="a", detail="same")
    merge_ledger_llm_progress(tgt, log_append="DB write: x")
    merge_ledger_llm_progress(tgt, log_append="DB write: x")
    lines = tgt.ledger_llm_progress["progress_log"]
    assert lines == ["a: same", "DB write: x", "DB write: x"]


def test_merge_progress_log_truncates_to_max() -> None:
    tgt = _FakeTarget()
    for i in range(PROGRESS_LOG_MAX + 7):
        merge_ledger_llm_progress(tgt, step=str(i), detail="row")
    assert len(tgt.ledger_llm_progress["progress_log"]) == PROGRESS_LOG_MAX
