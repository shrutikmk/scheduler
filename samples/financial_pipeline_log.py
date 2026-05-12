"""Structured stderr lines for financial UI + ledger LLM jobs."""

from __future__ import annotations

import sys
import time
from pathlib import Path


def model_short(model_str: str | None) -> str:
    if not model_str:
        return "?"
    s = str(model_str).strip()
    if not s:
        return "?"
    try:
        p = Path(s).expanduser()
        if p.is_absolute() and p.parts:
            return p.name or s
    except OSError:
        pass
    return s


def _wall_ts() -> str:
    return time.strftime("%d/%b/%Y %H:%M:%S", time.localtime())


def financial_flow_log(
    flow_id: str,
    message: str,
    *,
    lane: str = "pipeline",
    role: str | None = None,
    model: str | None = None,
    mlx: str | None = None,
    gateway: str | None = None,
) -> None:
    ts = _wall_ts()
    bits: list[str] = []
    if lane.strip():
        bits.append(lane.strip().upper())
    if role:
        bits.append(f"role={role}")
    if mlx:
        bits.append(f"mlx={mlx}")
    if model:
        bits.append(f"model={model_short(model)}")
    if gateway:
        bits.append(f"gateway={gateway}")
    head = (" ".join(bits) + " │ ") if bits else ""
    print(
        f"[{ts}] [fin_pipeline] [{flow_id}] {head}{message}",
        file=sys.stderr,
        flush=True,
    )
