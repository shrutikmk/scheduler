"""Filesystem + tokenizer helpers for scheduler model dirs (no inference runtime)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_HUB_REPO = "Qwen/Qwen3-8B"

TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
)


def is_local_dir(model: str) -> bool:
    return Path(model).expanduser().is_dir()


def _context_limit_from_config_json(path: Path) -> int | None:
    cfg_path = path / "config.json"
    if not cfg_path.is_file():
        return None
    try:
        meta = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("max_position_embeddings", "model_max_length"):
        v = meta.get(key)
        if isinstance(v, int) and 512 <= v <= 2_000_000:
            return v
    sw = meta.get("sliding_window")
    if isinstance(sw, int) and 512 <= sw <= 2_000_000:
        return sw
    return None


def resolve_context_token_limit(
    *,
    model_str: str,
    tokenizer: Any,
    explicit: int | None,
) -> int:
    if explicit is not None and explicit > 0:
        return explicit
    if is_local_dir(model_str):
        cap = _context_limit_from_config_json(Path(model_str).expanduser().resolve())
        if cap is not None:
            return cap
    tok_cap = getattr(tokenizer, "model_max_length", None)
    if isinstance(tok_cap, int) and 512 <= tok_cap <= 2_000_000:
        return tok_cap
    return 32768


def diagnose_local_snapshot(path: Path) -> list[str]:
    """Check a local HF-style snapshot layout (tokenizer config + shards)."""
    issues: list[str] = []
    if not path.is_dir():
        issues.append(f"Not a directory: {path}")
        return issues
    cfg = path / "config.json"
    if not cfg.is_file():
        issues.append("Missing config.json")
        return issues
    try:
        meta = json.loads(cfg.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        issues.append(f"Invalid config.json: {e}")
        return issues
    if "model_type" not in meta:
        issues.append("config.json has no 'model_type' — incomplete or non-HF layout.")
    if not any((path / name).is_file() for name in TOKENIZER_FILES):
        issues.append(
            "No tokenizer assets found "
            f"(expected one of {list(TOKENIZER_FILES)} under {path})."
        )
    weights = list(path.glob("*.safetensors")) + list(path.glob("*.npz"))
    if not weights:
        issues.append("No *.safetensors weight shards in directory.")
    return issues
