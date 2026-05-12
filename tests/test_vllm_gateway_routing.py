"""Routing of plain-completion and env helpers for vLLM gateway."""

from __future__ import annotations

from unittest import mock

import httpx
import pytest
from vllm_gateway_routing import (
    VllmRoute,
    _vllm_probe_url_candidates,
    diagnose_vllm_metal_server,
    probe_vllm_route,
    resolve_plain_completion_route,
    scheduler_inference_log_model,
    scheduler_vllm_openai_model_id,
    vllm_route_from_env,
)


@pytest.fixture
def sample_route() -> VllmRoute:
    return VllmRoute("http://127.0.0.1:8000/v1", "q14")


def test_vllm_probe_url_candidates_includes_v1_models() -> None:
    r = VllmRoute("http://127.0.0.1:8000/v1", "m14")
    urls = _vllm_probe_url_candidates(r)
    assert "http://127.0.0.1:8000/v1/models" in urls
    assert len(urls) == len(set(urls))


def test_diagnose_vllm_metal_server_exits_nonzero_when_down() -> None:
    r = VllmRoute("http://127.0.0.1:59997/v1", "m")
    with httpx.Client() as client:
        rc = diagnose_vllm_metal_server(client, r, timeout_sec=0.5)
    assert rc == 1


def test_probe_vllm_route_returns_false_on_connection_refused() -> None:
    """httpx.ConnectError must not escape; treat unreachable servers as down."""
    route = VllmRoute("http://127.0.0.1:59998/v1", "m")
    with httpx.Client() as client:
        assert probe_vllm_route(client, route, timeout_sec=0.5) is False


def test_resolve_plain_label_path_uses_same_route(
    sample_route: VllmRoute, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Financial label paths no longer hit a separate vLLM host."""
    p8 = tmp_path / "Qwen3-8B"
    p8.mkdir()
    monkeypatch.setenv("MLX_FINANCIAL_LABEL_MODEL", str(p8))
    sched = str(tmp_path / "Qwen3-14B")
    route, canon = resolve_plain_completion_route(
        model_arg=str(p8.resolve()),
        scheduler_default_path=sched,
        route=sample_route,
    )
    assert route.api_base == sample_route.api_base
    assert canon == str(p8.resolve())


def test_resolve_plain_hub_14b_id(
    sample_route: VllmRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MLX_FINANCIAL_LABEL_MODEL", raising=False)
    monkeypatch.delenv("MLX_FINANCIAL_INSIGHTS_MODEL", raising=False)
    monkeypatch.delenv("SCHEDULER_QUERY_PARSER_MODEL", raising=False)
    route, _ = resolve_plain_completion_route(
        model_arg="Qwen/Qwen3-14B",
        scheduler_default_path="/unused",
        route=sample_route,
    )
    assert route.api_base == sample_route.api_base


def test_vllm_route_from_env_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_14B_BASE_URL", raising=False)
    assert vllm_route_from_env() is None


def test_vllm_route_from_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_14B_BASE_URL", "http://h1/v1")
    monkeypatch.setenv("VLLM_14B_MODEL", "MyModel")
    r = vllm_route_from_env()
    assert r is not None
    assert r.api_base == "http://h1/v1"
    assert r.served_model_name == "MyModel"


def test_vllm_route_from_env_default_model_for_localhost_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VLLM_14B_MODEL", raising=False)
    monkeypatch.setenv("VLLM_14B_BASE_URL", "http://127.0.0.1:8000/v1")
    r = vllm_route_from_env()
    assert r is not None
    assert r.served_model_name == "Qwen3-14B"


def test_vllm_route_from_env_infers_path_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_14B_MODEL", raising=False)
    monkeypatch.setenv("VLLM_14B_BASE_URL", "http://host:9/my-model/v1")
    r = vllm_route_from_env()
    assert r is not None
    assert r.served_model_name == "my-model"


def test_vllm_route_from_env_ignores_legacy_8b_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_14B_BASE_URL", raising=False)
    monkeypatch.setenv("VLLM_8B_BASE_URL", "http://127.0.0.1:8001/v1")
    assert vllm_route_from_env() is None


def test_scheduler_inference_log_model_from_gateway_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VLLM_14B_BASE_URL", raising=False)

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args: object):
            return False

        def read(self) -> bytes:
            return b'{"backend":"vllm","vllm_model":"ServedName"}'

    with mock.patch("urllib.request.urlopen", return_value=Resp()):
        assert (
            scheduler_inference_log_model(
                gateway_origin="http://127.0.0.1:8766",
                client_model="/path/Qwen3-8B",
            )
            == "ServedName"
        )


def test_scheduler_inference_log_model_falls_back_when_health_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VLLM_14B_BASE_URL", raising=False)
    with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
        assert (
            scheduler_inference_log_model(
                gateway_origin="http://127.0.0.1:8766",
                client_model="client-id",
            )
            == "client-id"
        )


def test_scheduler_vllm_openai_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_14B_BASE_URL", raising=False)
    assert scheduler_vllm_openai_model_id() is None
    monkeypatch.setenv("VLLM_14B_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.delenv("VLLM_14B_MODEL", raising=False)
    assert scheduler_vllm_openai_model_id() == "Qwen3-14B"
    monkeypatch.setenv("VLLM_14B_MODEL", "custom-id")
    assert scheduler_vllm_openai_model_id() == "custom-id"
