"""Financial model path resolution helpers."""

from __future__ import annotations

from pathlib import Path

import financial_llm_models as flm


def test_label_model_explicit_wins() -> None:
    assert flm.resolve_financial_label_model("  /tmp/Qwen8  ") == "/tmp/Qwen8"


def test_insights_model_explicit_wins() -> None:
    assert flm.resolve_financial_insights_model("Qwen/hub-14") == "Qwen/hub-14"


def test_insights_ignores_mlx_model_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MLX_MODEL", "/should/not/use/for/insights")
    monkeypatch.delenv("MLX_FINANCIAL_INSIGHTS_MODEL", raising=False)
    monkeypatch.setattr(flm, "_LOCAL_QWEN14", tmp_path / "not-a-real-dir")
    assert flm.resolve_financial_insights_model(None) == flm._HUB_QWEN14


def test_label_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MLX_FINANCIAL_LABEL_MODEL", "/env/eight")
    assert flm.resolve_financial_label_model(None) == "/env/eight"


def test_scheduler_query_parser_explicit(monkeypatch) -> None:
    monkeypatch.delenv("SCHEDULER_QUERY_PARSER_MODEL", raising=False)
    assert flm.resolve_scheduler_query_parser_model(" /parse/model ") == "/parse/model"


def test_scheduler_query_parser_env(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_QUERY_PARSER_MODEL", "/qp/custom")
    assert flm.resolve_scheduler_query_parser_model(None) == "/qp/custom"


def test_scheduler_query_parser_falls_through_to_label_chain(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SCHEDULER_QUERY_PARSER_MODEL", raising=False)
    monkeypatch.delenv("MLX_FINANCIAL_LABEL_MODEL", raising=False)
    monkeypatch.setattr(flm, "_LOCAL_QWEN8", tmp_path / "missing-eight")
    assert flm.resolve_scheduler_query_parser_model(None) == flm._HUB_QWEN8
