"""Background batch: LLM short titles + spend categories for ledger debit rows."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    import sqlite3

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPT_PATH = _REPO_ROOT / "prompts" / "ledger-title-category.md"

ProgressFn = Callable[[dict[str, Any]], None] | None


def _load_system_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8").strip()
    return (
        "You output only JSON: each key is an fp string; each value is an object with "
        "title (2–6 words) and category (from Allowed categories or a new short label)."
    )


def _system_prompt_with_knowledge_and_categories() -> str:
    base = _load_system_prompt()
    from financial_spend_knowledge import load_spend_knowledge_for_prompt
    from spend_categories import format_categories_for_prompt

    parts = [base, "", "Allowed categories:", format_categories_for_prompt()]
    kb = load_spend_knowledge_for_prompt()
    if kb:
        parts.extend(["", "User spend knowledge:", kb])
    return "\n".join(parts)


def _plain_completion(
    origin: str,
    payload: dict[str, Any],
    *,
    model: str | None = None,
    timeout: float = 75.0,
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


def _balanced_slice(s: str, start: int, open_c: str, close_c: str) -> str | None:
    depth = 0
    for j in range(start, len(s)):
        c = s[j]
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return s[start : j + 1]
    return None


def _json_candidate_strings(raw: str) -> list[str]:
    s = (raw or "").strip()
    if not s:
        return []
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```\s*$", "", s)
    candidates: list[str] = [s]
    for opener, closer in (("{", "}"), ("[", "]")):
        i = s.find(opener)
        if i < 0:
            continue
        frag = _balanced_slice(s, i, opener, closer)
        if frag and frag not in candidates:
            candidates.append(frag)
    return candidates


def _parse_json_loose(raw_text: str) -> Any | None:
    for cand in _json_candidate_strings(raw_text):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


_WRAPPER_KEYS = frozenset(
    {
        "items",
        "results",
        "data",
        "mapping",
        "titles",
        "rows",
        "entries",
        "transactions",
        "debits",
        "mappings",
        "output",
        "response",
    }
)


def _norm_key_map(d: dict[Any, Any]) -> dict[str, Any]:
    return {str(k).strip().lower(): v for k, v in d.items()}


def _title_cat_from_obj(d: dict[str, Any]) -> tuple[str, str, bool] | None:
    nk = _norm_key_map(d)
    title_raw = (
        nk.get("title")
        or nk.get("display_title")
        or nk.get("short_title")
        or nk.get("label")
    )
    cat_raw = (
        nk.get("category")
        or nk.get("spend_category")
        or nk.get("cat")
        or nk.get("bucket")
    )
    if title_raw is None or cat_raw is None:
        return None
    title = str(title_raw).strip()
    cat = str(cat_raw).strip()
    if not title or not cat:
        return None
    inc = nk.get("is_new_category")
    if inc is None:
        inc = nk.get("new_category")
    if isinstance(inc, bool):
        is_new = inc
    elif isinstance(inc, int):
        is_new = inc != 0
    elif isinstance(inc, str):
        is_new = inc.strip().lower() in ("true", "1", "yes", "y")
    else:
        is_new = False
    return title, cat, is_new


def _collect_title_category_mappings(obj: Any, expected_fps: set[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add_from_item(fp: str, item: dict[str, Any]) -> None:
        got = _title_cat_from_obj(item)
        if not got or fp not in expected_fps:
            return
        title, cat, is_new = got
        found[fp] = {
            "title": title[:200],
            "category": cat[:120],
            "is_new_category": is_new,
        }

    def walk(x: Any, depth: int) -> None:
        if depth > 10:
            return
        if isinstance(x, list):
            for el in x:
                if isinstance(el, dict):
                    nk = _norm_key_map(el)
                    fp_val: str | None = None
                    for key in ("fp", "fingerprint", "id", "key"):
                        raw = nk.get(key)
                        if raw is not None and str(raw).strip():
                            fp_val = str(raw).strip()
                            break
                    if fp_val and fp_val in expected_fps:
                        add_from_item(fp_val, el)
                    else:
                        walk(el, depth + 1)
                else:
                    walk(el, depth + 1)
            return
        if not isinstance(x, dict):
            return
        for k, v in x.items():
            ks = str(k).strip()
            if ks in expected_fps and isinstance(v, dict):
                add_from_item(ks, v)
        lower_map = _norm_key_map(x)
        for wk in _WRAPPER_KEYS:
            inner = lower_map.get(wk)
            if inner is not None:
                walk(inner, depth + 1)
        for _k, v in x.items():
            if isinstance(v, list) and v:
                walk(v, depth + 1)
        for k, v in x.items():
            ks = str(k).strip()
            if ks in expected_fps:
                continue
            if isinstance(v, dict):
                walk(v, depth + 1)

    walk(obj, 0)
    return found


def _parse_title_category_map(
    raw_text: str, expected_fps: set[str]
) -> dict[str, dict[str, Any]]:
    obj = _parse_json_loose(raw_text)
    if obj is None:
        return {}
    return _collect_title_category_mappings(obj, expected_fps)


def _max_tokens_for_chunk(chunk_len: int, cap: int) -> int:
    n = max(1, chunk_len)
    return min(max(32, cap), 280 + 130 * n)


def run_ledger_titling_pass(
    conn: sqlite3.Connection,
    llm_origin: str,
    *,
    model: str | None = None,
    max_items: int = 36,
    chunk_size: int = 1,
    max_tokens: int = 1400,
    timeout: float = 90.0,
    on_progress: ProgressFn = None,
    flow_id: str | None = None,
) -> dict[str, Any]:
    from financial_ledger_store import (
        init_schema,
        list_debits_pending_retitle,
        upsert_ledger_title_category,
    )
    from financial_pipeline_log import financial_flow_log
    from ledger_llm_progress import short_model_label
    from spend_categories import append_category_if_new
    from vllm_gateway_routing import scheduler_inference_log_model

    init_schema(conn)
    rows = list_debits_pending_retitle(conn, max_items)
    if not rows:
        return {"ok": True, "updated": 0, "reason": "none_pending"}

    log_model = scheduler_inference_log_model(gateway_origin=llm_origin, client_model=model)
    mlabel = short_model_label(log_model)
    chunk_size = max(1, min(int(chunk_size), 64))
    n_chunks = (len(rows) + chunk_size - 1) // chunk_size
    if on_progress:
        on_progress(
            {
                "step": "prepare",
                "detail": (
                    f"Prepared {len(rows)} debit(s) in {n_chunks} chunk(s) "
                    f"(≤{chunk_size} per LLM call); {mlabel} — each parsed row "
                    "commits to ledger immediately."
                ),
                "model": mlabel,
                "gateway": llm_origin,
            }
        )

    total_updated = 0
    total_parsed = 0
    chunk_idx = 0
    for chunk_start in range(0, len(rows), chunk_size):
        chunk = rows[chunk_start : chunk_start + chunk_size]
        chunk_idx += 1
        expected = {r[0] for r in chunk}
        batch = [{"fp": fp, "d": desc[:420]} for fp, desc in chunk]
        chunk_cap = _max_tokens_for_chunk(len(chunk), max_tokens)
        user = (
            "For each fp, output title and category per the system rules. Input JSON array:\n"
            + json.dumps(batch, ensure_ascii=False)
            + "\n\nReply with one JSON object only: keys = fp strings, values = objects with "
            '"title", "category", and optionally "is_new_category" (boolean).'
        )
        if on_progress:
            on_progress(
                {
                    "step": "llm_request",
                    "detail": (
                        f"Chunk {chunk_idx}/{n_chunks}: LLM request "
                        f"({len(chunk)} fp) → {llm_origin}/v1/plain-completion"
                    ),
                    "model": mlabel,
                    "gateway": llm_origin,
                }
            )
        fid = flow_id or "ledger_titling"
        short_u = len(user)
        financial_flow_log(
            fid,
            f"gateway POST titles chunk {chunk_idx}/{n_chunks} fps={len(chunk)} "
            f"user_chars≈{short_u} max_tokens={chunk_cap}",
            lane="gateway",
            role="title_category",
            model=log_model or None,
            mlx="to_gateway",
            gateway=llm_origin,
        )
        try:
            upstream = _plain_completion(
                llm_origin,
                {
                    "system": _system_prompt_with_knowledge_and_categories(),
                    "user": user,
                    "max_tokens": chunk_cap,
                },
                model=model,
                timeout=timeout,
            )
        except Exception as e:
            if on_progress:
                on_progress({"step": "llm_error", "detail": f"Gateway error: {e}", "error": str(e)})
            financial_flow_log(
                fid,
                f"titles chunk {chunk_idx}/{n_chunks} gateway error: {e!s}",
                lane="gateway",
                role="title_category",
                model=log_model,
                mlx="from_gateway_error",
                gateway=llm_origin,
            )
            return {
                "ok": False,
                "error": str(e),
                "updated": total_updated,
                "batch_size": len(rows),
                "parsed": total_parsed,
                "chunks_done": chunk_idx - 1,
            }

        tx = upstream.get("text")
        tl = len(tx) if isinstance(tx, str) else 0
        financial_flow_log(
            fid,
            f"titles chunk {chunk_idx}/{n_chunks} gateway OK response_chars≈{tl}",
            lane="gateway",
            role="title_category",
            model=upstream.get("model") if isinstance(upstream.get("model"), str) else log_model,
            mlx="from_gateway",
            gateway=llm_origin,
        )

        if on_progress:
            on_progress(
                {
                    "step": "llm_response",
                    "detail": f"Chunk {chunk_idx}/{n_chunks}: parsing JSON…",
                    "model": mlabel,
                }
            )

        text = upstream.get("text")
        if not isinstance(text, str):
            alt = upstream.get("content")
            text = alt if isinstance(alt, str) else None
        if not isinstance(text, str):
            if on_progress:
                on_progress(
                    {
                        "step": "parse_error",
                        "detail": "Invalid gateway response (no text).",
                    }
                )
            return {
                "ok": False,
                "error": "bad_gateway_response",
                "detail": str(upstream)[:300],
                "updated": total_updated,
                "batch_size": len(rows),
                "parsed": total_parsed,
                "chunks_done": chunk_idx - 1,
            }

        parsed = _parse_title_category_map(text, expected)
        total_parsed += len(parsed)
        if not parsed and expected:
            snippet = (text or "").replace("\n", " ").strip()
            if len(snippet) > 420:
                snippet = snippet[:420] + "…"
            print(
                "[ledger_titling] parsed 0 title/category mappings; "
                f"batch had {len(expected)} fp(s); output snippet: {snippet!r}",
                file=sys.stderr,
            )
        if on_progress:
            on_progress(
                {
                    "step": "parsed",
                    "detail": (
                        f"Chunk {chunk_idx}/{n_chunks}: parsed {len(parsed)}/{len(chunk)} "
                        "mapping(s); committing to ledger row-by-row…"
                    ),
                    "model": mlabel,
                }
            )

        chunk_written = 0
        for fp, _desc in chunk:
            if fp not in parsed:
                continue
            item = parsed[fp]
            cat = item["category"]
            if item.get("is_new_category"):
                if on_progress:
                    on_progress(
                        {
                            "step": "new_category",
                            "detail": f"Appending new category to categories.md: {cat[:80]}…",
                            "model": mlabel,
                        }
                    )
                append_category_if_new(cat)
            n_row = upsert_ledger_title_category(conn, {fp: (item["title"], cat)})
            total_updated += n_row
            chunk_written += n_row
            if n_row and on_progress:
                preview = fp if len(fp) <= 26 else fp[:24] + "…"
                on_progress(
                    {
                        "log_append": (
                            "DB write: SQLite committed debit title/category to ledger.sqlite "
                            f"(+{n_row} row) fp={preview}"
                        ),
                    }
                )
        if chunk_written:
            financial_flow_log(
                fid,
                f"SQLite ledger upsert title/category rows={chunk_written} "
                f"(chunk {chunk_idx}/{n_chunks}, running_total={total_updated})",
                lane="ledger",
                role="title_category",
                model=log_model or None,
                mlx="posted",
            )
        if on_progress:
            on_progress(
                {
                    "step": "ledger_posted",
                    "detail": (
                        f"Chunk {chunk_idx}/{n_chunks}: committed {chunk_written} row(s) "
                        f"({total_updated} total this pass)."
                    ),
                    "model": mlabel,
                }
            )

    return {
        "ok": True,
        "updated": total_updated,
        "batch_size": len(rows),
        "parsed": total_parsed,
        "chunks": n_chunks,
    }

