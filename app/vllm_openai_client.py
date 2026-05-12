"""Minimal OpenAI-compatible client for vLLM servers (vLLM Metal, etc.)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

_CHAT_COMPLETIONS_PATH = "/chat/completions"


def _chat_url(api_base: str) -> str:
    return api_base.rstrip("/") + _CHAT_COMPLETIONS_PATH


def _merge_chat_template_kwargs(
    body: dict[str, Any], enable_thinking: bool | None
) -> None:
    if enable_thinking is None:
        return
    # vLLM accepts chat template kwargs on the completions request for Qwen3.
    body.setdefault("chat_template_kwargs", {})["enable_thinking"] = bool(enable_thinking)


def chat_completion_text(
    client: httpx.Client,
    *,
    api_base: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    top_p: float,
    max_tokens: int,
    enable_thinking: bool | None = None,
    timeout_sec: float = 600.0,
) -> str:
    """Non-streaming chat completion; returns assistant message content only."""
    url = _chat_url(api_base)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "top_p": float(top_p),
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    _merge_chat_template_kwargs(body, enable_thinking)
    r = client.post(url, json=body, timeout=timeout_sec)
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices")
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if content is None:
        return ""
    return str(content).strip()


def _iter_sse_delta_texts(line: str) -> Iterator[str]:
    line = line.strip()
    if not line.startswith("data:"):
        return
    payload = line[5:].strip()
    if payload == "[DONE]":
        return
    try:
        evt = json.loads(payload)
    except json.JSONDecodeError:
        return
    choices = evt.get("choices") or []
    if not choices:
        return
    delta = choices[0].get("delta") or {}
    piece = delta.get("content")
    if piece:
        yield str(piece)


def chat_completion_stream(
    client: httpx.Client,
    *,
    api_base: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    top_p: float,
    max_tokens: int,
    enable_thinking: bool | None = None,
    timeout_sec: float = 600.0,
) -> Iterator[str]:
    """Streaming chat completion; yields text fragments (tokens merged by server)."""
    url = _chat_url(api_base)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "top_p": float(top_p),
        "max_tokens": int(max_tokens),
        "stream": True,
    }
    _merge_chat_template_kwargs(body, enable_thinking)
    with client.stream(
        "POST",
        url,
        json=body,
        timeout=httpx.Timeout(timeout_sec, read=timeout_sec),
    ) as r:
        r.raise_for_status()
        for raw in r.iter_lines():
            if not raw:
                continue
            yield from _iter_sse_delta_texts(raw)
