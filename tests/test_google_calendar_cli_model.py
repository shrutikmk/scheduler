"""Calendar CLI default model resolution (Qwen3-14B vs env overrides)."""

from __future__ import annotations

import google_calendar_cli as cal


def test_explicit_model_wins() -> None:
    assert cal.resolve_calendar_cli_model("  Qwen/from-cli  ") == "Qwen/from-cli"


def test_mlx_model_env_overrides_defaults(monkeypatch) -> None:
    monkeypatch.delenv("MLX_CALENDAR_MODEL", raising=False)
    monkeypatch.setenv("MLX_MODEL", "Qwen/from-mlx-env")
    assert cal.resolve_calendar_cli_model(None) == "Qwen/from-mlx-env"


def test_mlx_calendar_model_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MLX_MODEL", raising=False)
    monkeypatch.setenv("MLX_CALENDAR_MODEL", "Qwen/calendar-only")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cal.resolve_calendar_cli_model(None) == "Qwen/calendar-only"


def test_default_uses_local_qwen14_when_present(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MLX_MODEL", raising=False)
    monkeypatch.delenv("MLX_CALENDAR_MODEL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    local = tmp_path / "models" / "Qwen3-14B"
    local.mkdir(parents=True)
    assert cal.resolve_calendar_cli_model(None) == str(local.resolve())


def test_default_falls_back_to_hub(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MLX_MODEL", raising=False)
    monkeypatch.delenv("MLX_CALENDAR_MODEL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cal.resolve_calendar_cli_model(None) == cal._CAL_DEFAULT_HUB
