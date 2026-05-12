"""Tests for optional spend knowledge loader."""

from __future__ import annotations

from financial_spend_knowledge import load_spend_knowledge_for_prompt, spend_knowledge_path


def test_spend_knowledge_path_under_prompts() -> None:
    assert spend_knowledge_path().name == "financial-spend-knowledge.md"
    assert spend_knowledge_path().parent.name == "prompts"


def test_load_spend_knowledge_when_private_file_present() -> None:
    path = spend_knowledge_path()
    out = load_spend_knowledge_for_prompt()
    assert isinstance(out, str)
    if path.is_file():
        assert "User spend knowledge" in out
        assert len(out) > 40
    else:
        assert out == ""
