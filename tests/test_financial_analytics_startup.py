"""Financial standalone UI hooks (ledger titling bootstrap)."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest


@patch("financial_analytics_ui.FinancialAnalyticsServer")
@patch("financial_analytics_ui._schedule_ledger_titling")
def test_financial_ui_kicks_titling_when_retitles_pending(
    mock_schedule: MagicMock,
    mock_server_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MLX_FINANCIAL_LABEL_MODEL", raising=False)
    monkeypatch.delenv("MLX_FINANCIAL_INSIGHTS_MODEL", raising=False)
    monkeypatch.setattr("financial_llm_models._LOCAL_QWEN8", Path("/no/local/8b"))
    monkeypatch.setattr("financial_llm_models._LOCAL_QWEN14", Path("/no/local/14b"))

    inst = MagicMock()
    inst.llm_origin = "http://127.0.0.1:8766"
    inst.ledger_llm_progress_lock = threading.Lock()
    inst.ledger_llm_progress = {"active": False}
    inst.serve_forever.side_effect = KeyboardInterrupt
    mock_server_cls.return_value = inst

    with patch("financial_ledger_store.ledger_meta", return_value={"retitle_pending_count": 3}):
        with patch("financial_analytics_ui._ledger_connection", return_value=MagicMock()):
            from financial_analytics_ui import main

            main([])

    mock_schedule.assert_called_once_with("http://127.0.0.1:8766", ANY, inst)


@patch("financial_analytics_ui.FinancialAnalyticsServer")
@patch("financial_analytics_ui._schedule_ledger_titling")
def test_financial_ui_no_kick_when_job_active(
    mock_schedule: MagicMock,
    mock_server_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MLX_FINANCIAL_LABEL_MODEL", raising=False)
    monkeypatch.delenv("MLX_FINANCIAL_INSIGHTS_MODEL", raising=False)
    monkeypatch.setattr("financial_llm_models._LOCAL_QWEN8", Path("/no/local/8b"))
    monkeypatch.setattr("financial_llm_models._LOCAL_QWEN14", Path("/no/local/14b"))

    inst = MagicMock()
    inst.llm_origin = "http://127.0.0.1:8766"
    inst.ledger_llm_progress_lock = threading.Lock()
    inst.ledger_llm_progress = {"active": True}
    inst.serve_forever.side_effect = KeyboardInterrupt
    mock_server_cls.return_value = inst

    with patch("financial_ledger_store.ledger_meta", return_value={"retitle_pending_count": 3}):
        with patch("financial_analytics_ui._ledger_connection", return_value=MagicMock()):
            from financial_analytics_ui import main

            main([])

    mock_schedule.assert_not_called()
