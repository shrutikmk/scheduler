"""MLX model resolution for financial analytics (fast label model vs heavier insights)."""

from __future__ import annotations

import os
from pathlib import Path

_HOME_MODELS = Path.home() / "models"
_LOCAL_QWEN8 = _HOME_MODELS / "Qwen3-8B"
_LOCAL_QWEN14 = _HOME_MODELS / "Qwen3-14B"
_HUB_QWEN8 = "Qwen/Qwen3-8B"
_HUB_QWEN14 = "Qwen/Qwen3-14B"


def resolve_financial_label_model(cli_or_env: str | None = None) -> str:
    """Qwen3-8B for ledger titles, categories, and spending-mix bar labels.

    Precedence: non-empty ``cli_or_env``, ``MLX_FINANCIAL_LABEL_MODEL``, local
    ``~/models/Qwen3-8B``, else Hugging Face ``Qwen/Qwen3-8B``.
    """
    if cli_or_env and str(cli_or_env).strip():
        return str(cli_or_env).strip()
    v = os.environ.get("MLX_FINANCIAL_LABEL_MODEL", "").strip()
    if v:
        return v
    if _LOCAL_QWEN8.is_dir():
        return str(_LOCAL_QWEN8.resolve())
    return _HUB_QWEN8


def resolve_scheduler_query_parser_model(cli_or_env: str | None = None) -> str:
    """MLX snapshot for day-scheduler **query JSON** parsing (fast ~8B class).

    Precedence: non-empty ``cli_or_env``, ``SCHEDULER_QUERY_PARSER_MODEL``, then the same
    default chain as :func:`resolve_financial_label_model` (local ``~/models/Qwen3-8B``,
    Hub ``Qwen/Qwen3-8B``).
    """
    if cli_or_env and str(cli_or_env).strip():
        return str(cli_or_env).strip()
    v = os.environ.get("SCHEDULER_QUERY_PARSER_MODEL", "").strip()
    if v:
        return v
    return resolve_financial_label_model(None)


def resolve_financial_insights_model(cli_or_env: str | None = None) -> str:
    """Qwen3-14B for narrative insights only.

    Precedence: non-empty ``cli_or_env``, ``MLX_FINANCIAL_INSIGHTS_MODEL``, local
    ``~/models/Qwen3-14B``, else ``Qwen/Qwen3-14B``.

    Does **not** read ``MLX_MODEL`` so a global 8B default does not downgrade insights.
    """
    if cli_or_env and str(cli_or_env).strip():
        return str(cli_or_env).strip()
    v = os.environ.get("MLX_FINANCIAL_INSIGHTS_MODEL", "").strip()
    if v:
        return v
    if _LOCAL_QWEN14.is_dir():
        return str(_LOCAL_QWEN14.resolve())
    return _HUB_QWEN14
