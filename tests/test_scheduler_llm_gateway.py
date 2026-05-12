"""Exit paths for ``scheduler_llm_gateway`` when vLLM is not configured."""

from __future__ import annotations


def test_main_exits_when_vllm_url_missing(monkeypatch) -> None:
    monkeypatch.delenv("VLLM_14B_BASE_URL", raising=False)
    from scheduler_llm_gateway import main

    assert main(["--host", "127.0.0.1", "--port", "58766"]) == 2


def test_diagnose_only_requires_vllm(monkeypatch) -> None:
    monkeypatch.delenv("VLLM_14B_BASE_URL", raising=False)
    from scheduler_llm_gateway import main

    assert main(["--diagnose-only"]) == 2
