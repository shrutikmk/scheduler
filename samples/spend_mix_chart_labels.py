"""Background: LLM polish for spending-mix bar labels (Other · raw payee rows)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    import sqlite3

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPT_PATH = _REPO_ROOT / "prompts" / "spend-mix-chart-labels.md"

ProgressFn = Callable[[dict[str, Any]], None] | None


def _load_system_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8").strip()
    return (
        "You output only JSON: keys are row_key strings, values short 2–6 word "
        "labels for spending chart bars. Prefer merchant or purpose; omit Other · prefix."
    )


def _system_prompt_with_knowledge() -> str:
    base = _load_system_prompt()
    from financial_spend_knowledge import load_spend_knowledge_for_prompt

    kb = load_spend_knowledge_for_prompt()
    if not kb:
        return base
    return f"{base}\n\n{kb}"


def _plain_completion(
    origin: str,
    payload: dict[str, Any],
    *,
    model: str | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    url = origin.rstrip("/") + "/v1/plain-completion"
    body_dict = {**payload}
    m = (model or "").strip()
    if m:
        body_dict["model"] = m
    body = json.dumps(body_dict).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_label_map(raw_text: str, expected_keys: set[str]) -> dict[str, str]:
    s = (raw_text or "").strip()
    if not s:
        return {}
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```\s*$", "", s)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in obj.items():
        ks = str(k).strip()
        if ks not in expected_keys or not isinstance(v, str):
            continue
        t = v.strip()
        if t:
            out[ks] = t[:200]
    return out


def run_mix_chart_label_pass(
    conn: sqlite3.Connection,
    llm_origin: str,
    *,
    model: str | None = None,
    max_rows: int = 20,
    max_tokens: int = 900,
    timeout: float = 90.0,
    on_progress: ProgressFn = None,
    flow_id: str | None = None,
) -> dict[str, Any]:
    from bank_statement_csv import summarize_rows, transactions_matching_spend_mix_row
    from financial_ledger_store import (
        enrich_summary_with_ledger,
        fetch_statement_rows,
        init_schema,
        mix_row_needs_llm_bar_label,
        spend_mix_row_key,
        upsert_mix_chart_labels,
    )
    from financial_pipeline_log import financial_flow_log
    from ledger_llm_progress import short_model_label
    from vllm_gateway_routing import scheduler_inference_log_model

    vllm_mid = scheduler_inference_log_model(gateway_origin=llm_origin, client_model=model)
    mlabel = short_model_label(vllm_mid)
    log_model = vllm_mid

    init_schema(conn)
    if on_progress:
        on_progress(
            {
                "step": "mix_load",
                "detail": "Loading transactions from ledger for spending-mix summary…",
                "model": mlabel,
                "gateway": llm_origin,
            }
        )
    rows = fetch_statement_rows(conn, None, None)
    if not rows:
        if on_progress:
            on_progress({"step": "mix_skip", "detail": "Ledger empty; no mix-chart labels."})
        return {"ok": True, "updated": 0, "reason": "empty_ledger"}

    if on_progress:
        on_progress(
            {
                "step": "mix_summarize",
                "detail": "Summarizing rows + merging ledger titles/categories…",
                "model": mlabel,
            }
        )
    summary = summarize_rows(
        rows,
        filename="aggregate",
        preamble={},
        include_running_balance=False,
    )
    enrich_summary_with_ledger(conn, summary)
    if summary.get("display_titles_pending"):
        if on_progress:
            on_progress(
                {
                    "step": "mix_skipped",
                    "detail": "Skipping mix-bar labels until all debit titles are ready.",
                    "model": mlabel,
                }
            )
        return {"ok": True, "updated": 0, "skipped": "display_titles_pending"}

    mix = summary.get("spending_mix_chart") or []
    txs = summary.get("transactions") or []

    batch_raw: list[dict[str, Any]] = []
    for row in mix:
        if not mix_row_needs_llm_bar_label(row):
            continue
        key = spend_mix_row_key(row)
        if row.get("mix_label_loading") is not True:
            continue
        mt = transactions_matching_spend_mix_row(txs, row)
        samples = [
            str(t.get("display_title") or "").strip()
            for t in mt[:10]
            if t.get("display_title") and str(t.get("display_title")).strip()
        ]
        if not samples:
            continue
        batch_raw.append(
            {
                "row_key": key,
                "current_label": str(row.get("label") or "")[:180],
                "sample_titles": samples[:6],
                "total_spent": row.get("total"),
            }
        )

    if not batch_raw:
        if on_progress:
            on_progress(
                {
                    "step": "mix_none",
                    "detail": "No mix-chart rows need LLM bar labels right now.",
                    "model": mlabel,
                }
            )
        return {"ok": True, "updated": 0, "reason": "none_pending"}

    batch = batch_raw[: max(1, min(max_rows, 40))]
    expected = {str(b["row_key"]) for b in batch}
    user = (
        "For each row_key, output a concise bar label. Input JSON array:\n"
        + json.dumps(batch, ensure_ascii=False)
        + "\n\nReply with one JSON object only: keys = row_key, values = labels."
    )
    if on_progress:
        on_progress(
            {
                "step": "mix_llm_request",
                "detail": (
                    f"Sending mix-chart label batch to LLM ({mlabel}) via {llm_origin} — "
                    f"{len(batch)} row_key(s)."
                ),
                "model": mlabel,
                "gateway": llm_origin,
            }
        )
    fid = flow_id or "mix_labels"
    financial_flow_log(
        fid,
        f"gateway POST mix_chart_labels batch row_keys={len(batch)} "
        f"max_tokens={max_tokens} user_chars≈{len(user)}",
        lane="gateway",
        role="mix_chart",
        model=log_model or None,
        mlx="to_gateway",
        gateway=llm_origin,
    )
    try:
        upstream = _plain_completion(
            llm_origin,
            {
                "system": _system_prompt_with_knowledge(),
                "user": user,
                "max_tokens": max_tokens,
            },
            model=model,
            timeout=timeout,
        )
    except Exception as e:
        if on_progress:
            on_progress({"step": "mix_llm_error", "detail": f"Gateway error: {e}", "error": str(e)})
        financial_flow_log(
            fid,
            f"mix_chart_labels gateway error: {e!s}",
            lane="gateway",
            role="mix_chart",
            model=log_model,
            mlx="from_gateway_error",
            gateway=llm_origin,
        )
        return {"ok": False, "error": str(e)}

    mt = upstream.get("text")
    mtl = len(mt) if isinstance(mt, str) else 0
    financial_flow_log(
        fid,
        f"mix_chart_labels gateway OK response_chars≈{mtl}",
        lane="gateway",
        role="mix_chart",
        model=upstream.get("model") if isinstance(upstream.get("model"), str) else log_model,
        mlx="from_gateway",
        gateway=llm_origin,
    )

    if on_progress:
        on_progress(
            {
                "step": "mix_llm_response",
                "detail": f"Mix-chart response received from {mlabel}; parsing labels…",
                "model": mlabel,
            }
        )

    text = upstream.get("text")
    if not isinstance(text, str):
        if on_progress:
            on_progress(
                {
                    "step": "mix_parse_error",
                    "detail": "Invalid gateway response (no text).",
                }
            )
        return {"ok": False, "error": "bad_gateway_response", "detail": str(upstream)[:300]}

    labels = _parse_label_map(text, expected)
    if on_progress and labels:
        on_progress(
            {
                "step": "mix_ledger_write",
                "detail": f"Writing {len(labels)} mix_chart_labels row(s) to ledger.sqlite…",
                "model": mlabel,
            }
        )
    n = upsert_mix_chart_labels(conn, labels) if labels else 0
    if n and on_progress:
        on_progress(
            {
                "log_append": (
                    "DB write: SQLite committed mix-chart bar labels to ledger.sqlite "
                    f"(mix_chart_labels +{n} row(s))"
                ),
            }
        )
    if n:
        financial_flow_log(
            fid,
            f"SQLite ledger upsert_mix_chart_labels rows_written={n} parsed_labels={len(labels)}",
            lane="ledger",
            role="mix_chart",
            model=log_model or None,
            mlx="posted",
        )
    if on_progress:
        on_progress(
            {
                "step": "mix_posted",
                "detail": f"Stored {n} polished bar label(s) in mix_chart_labels table.",
                "model": mlabel,
            }
        )
    return {"ok": True, "updated": n, "batch_size": len(batch), "parsed": len(labels)}
