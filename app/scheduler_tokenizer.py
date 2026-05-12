"""Hugging Face-loaded tokenizer only (no weights) for context measurement on vLLM path."""

from __future__ import annotations

import threading
from typing import Any

_tok_lock = threading.Lock()
_tok_cache: dict[str, Any] = {}


def load_tokenizer_only(model_path_or_id: str) -> Any:
    """Load ``AutoTokenizer``; same id semantics as ``transformers`` / local snapshots."""
    key = str(model_path_or_id).strip()
    if not key:
        raise ValueError("empty model_path_or_id")
    with _tok_lock:
        hit = _tok_cache.get(key)
        if hit is not None:
            return hit
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(key, trust_remote_code=True)
        _tok_cache[key] = tok
        return tok
