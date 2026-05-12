"""Tests for MLX gateway dual-model preload and health bundle listing."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest


@pytest.fixture
def _clear_model_registry():
    import mlx_scheduler_llm_api as api

    prev = dict(api._model_registry)
    api._model_registry.clear()
    yield
    api._model_registry.clear()
    api._model_registry.update(prev)


def test_loaded_model_bundle_paths_sorted_unique(_clear_model_registry) -> None:
    from mlx_scheduler_llm_api import ModelBundle, loaded_model_bundle_paths
    import mlx_scheduler_llm_api as api

    lk = threading.Lock()
    api._model_registry["scheduler:/z"] = ModelBundle(None, None, "/z", lk)
    api._model_registry["scheduler:/a"] = ModelBundle(None, None, "/a", lk)
    assert loaded_model_bundle_paths() == ["/a", "/z"]


@patch("mlx_llm_gateway.ThreadingHTTPServer")
@patch("mlx_llm_gateway.build_llm_gateway_handler", return_value=object())
@patch("mlx_llm_gateway.ensure_model_bundle_loaded")
@patch("mlx_llm_gateway.ensure_model_loaded")
def test_gateway_main_preloads_label_when_models_differ(
    mock_ensure_main: MagicMock,
    mock_bundle: MagicMock,
    _mock_build: object,
    _mock_httpd_cls: MagicMock,
) -> None:
    mock_ensure_main.return_value = (object(), object(), "")
    mock_bundle.return_value = (MagicMock(), "/labels/Qwen3-8B")
    inst = _mock_httpd_cls.return_value
    inst.serve_forever.side_effect = KeyboardInterrupt

    from mlx_llm_gateway import main

    rc = main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8766",
            "--model",
            "/scheduler/Qwen3-14B",
            "--preload-financial-label-model",
            "/labels/Qwen3-8B",
            "--no-preload-query-parser-model",
        ]
    )
    assert rc == 0
    mock_bundle.assert_called_once()
    assert mock_bundle.call_args.kwargs["model"] == "/labels/Qwen3-8B"


@patch("mlx_llm_gateway.ThreadingHTTPServer")
@patch("mlx_llm_gateway.build_llm_gateway_handler", return_value=object())
@patch("mlx_llm_gateway.ensure_model_bundle_loaded")
@patch("mlx_llm_gateway.ensure_model_loaded")
def test_gateway_main_skips_preload_with_no_preload_flag(
    mock_ensure_main: MagicMock,
    mock_bundle: MagicMock,
    _mock_build: object,
    _mock_httpd_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MLX_FINANCIAL_LABEL_MODEL", raising=False)
    mock_ensure_main.return_value = (object(), object(), "")
    _mock_httpd_cls.return_value.serve_forever.side_effect = KeyboardInterrupt

    from mlx_llm_gateway import main

    rc = main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8766",
            "--model",
            "/scheduler/Qwen3-14B",
            "--no-preload-financial-label-model",
            "--no-preload-query-parser-model",
        ]
    )
    assert rc == 0
    mock_bundle.assert_not_called()


@patch("mlx_llm_gateway.ThreadingHTTPServer")
@patch("mlx_llm_gateway.build_llm_gateway_handler", return_value=object())
@patch("mlx_llm_gateway.ensure_model_bundle_loaded")
@patch("mlx_llm_gateway.ensure_model_loaded")
def test_gateway_main_no_second_load_when_scheduler_equals_label(
    mock_ensure_main: MagicMock,
    mock_bundle: MagicMock,
    _mock_build: object,
    _mock_httpd_cls: MagicMock,
) -> None:
    mock_ensure_main.return_value = (object(), object(), "")
    _mock_httpd_cls.return_value.serve_forever.side_effect = KeyboardInterrupt
    same = "/models/same-weights"

    from mlx_llm_gateway import main

    rc = main(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8766",
            "--model",
            same,
            "--preload-financial-label-model",
            same,
            "--no-preload-query-parser-model",
        ]
    )
    assert rc == 0
    mock_bundle.assert_not_called()


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

    mock_schedule.assert_called_once_with(
        "http://127.0.0.1:8766", ANY, inst,
    )


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
