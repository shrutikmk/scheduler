#!/usr/bin/env python3
"""Local financial analytics UI: CSV upload to ``financial-data/``, charts, LLM-backed insights.

**Two terminals**

**HTTP access logging:** Identical consecutive lines (for example rapid
``GET /api/ledger_llm_progress``) roll up into ``×N`` bundles. Set
``MLX_FINANCIAL_UI_ACCESS_LOG_STACK_SEC`` (default ``45`` seconds; use ``0`` or ``off`` for
per-line logs). Ledger and LLM hops also log as ``[fin_pipeline] …``.

1. **LLM gateway (Metal)** — must expose ``POST /v1/plain-completion``::

       uv run --group samples-vllm python app/scheduler_llm_gateway.py

   One gateway process is enough: each completion may set JSON ``model``; the server loads
   **Qwen3-8B** and **Qwen3-14B** in separate bundles (per-model locks) so label batches and
   insights can run **in parallel**.

2. **Standalone finances-only UI** (optional, default port ``8770``)::

       uv run python app/financial_analytics_ui.py

   Prefer the unified shell: ``uv run python app/day_scheduler_web.py`` (Finances at ``/finances``).

Set ``MLX_SCHEDULER_LLM_API`` if the gateway is not on
``http://127.0.0.1:8766``. On startup, if the ledger has debits still flagged for LLM titles
(``retitle_pending_count > 0``) and no background job is already running, a label pass is
scheduled automatically (same as after upload/reindex).

**Models**

- **Labels** (ledger title + category, mix-chart bar labels): **Qwen3-8B** — env
  ``MLX_FINANCIAL_LABEL_MODEL``, or flag ``--financial-label-model``, else ``~/models/Qwen3-8B``
  or Hub ``Qwen/Qwen3-8B``.
- **Insights** (markdown narrative): **Qwen3-14B** — env ``MLX_FINANCIAL_INSIGHTS_MODEL``, or
  ``--financial-insights-model``, else ``~/models/Qwen3-14B`` or Hub ``Qwen/Qwen3-14B``.
  Insights resolution intentionally ignores ``MLX_MODEL`` so a global 8B default does not
  downgrade this pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

_APP_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _APP_ROOT.parent
_SAMPLES_DIR = _REPO_ROOT / "samples"
for _p in (_APP_ROOT, _SAMPLES_DIR):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from financial_pipeline_log import financial_flow_log
from financial_spend_knowledge import load_spend_knowledge_for_prompt
from http_access_rollup import AccessLogRollup

_FIN_UI_HTTP_ACCESS = AccessLogRollup(
    stderr_tag="financial-ui",
    env_seconds_key="MLX_FINANCIAL_UI_ACCESS_LOG_STACK_SEC",
)

_FINANCIAL_DIR = _REPO_ROOT / "financial-data"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "financial-insights-system.md"
_HTML_PATH = _APP_ROOT / "financial_analytics.html"

DEFAULT_LLM_ORIGIN = os.environ.get("MLX_SCHEDULER_LLM_API", "http://127.0.0.1:8766").strip().rstrip(
    "/"
)

_system_prompt_cache: str | None = None


def _load_financial_system_prompt() -> str:
    global _system_prompt_cache
    if _system_prompt_cache is not None:
        return _system_prompt_cache
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    _system_prompt_cache = raw
    return raw


def _insights_system_prompt_text() -> str:
    """Base insights instructions plus optional spend knowledge (interpretive; amounts stay in user digest)."""
    base = _load_financial_system_prompt().rstrip()
    kb = load_spend_knowledge_for_prompt()
    if not kb:
        return base
    return f"{base}\n\n{kb}".rstrip()


def _safe_csv_filename(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    base = Path(name).name
    if not base.lower().endswith(".csv"):
        return None
    if base in (".", "..") or "/" in base or "\\" in base:
        return None
    return base


def _financial_csv_paths() -> list[Path]:
    if not _FINANCIAL_DIR.is_dir():
        return []
    return sorted(_FINANCIAL_DIR.glob("*.csv"), key=lambda p: p.name.lower())


def _json_bool(val: object, *, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return default


def _query_bool(qs: dict[str, list[str]], key: str, *, default: bool = False) -> bool:
    vals = qs.get(key) or []
    if not vals:
        return default
    return _json_bool(vals[0], default=default)


def _parse_iso_date(val: str | None) -> date | None:
    if not val or not str(val).strip():
        return None
    return date.fromisoformat(str(val).strip())


def _schedule_ledger_titling(
    llm_origin: str,
    label_model: str,
    httpd: ThreadingHTTPServer,
) -> None:
    """Fire-and-forget LLM pass: titles + categories (small model), then mix-bar labels."""

    def _job() -> None:
        from expense_display_titles import run_ledger_titling_pass
        from financial_ledger_store import connect, init_schema
        from ledger_llm_progress import merge_ledger_llm_progress, short_model_label
        from spend_mix_chart_labels import run_mix_chart_label_pass
        from vllm_gateway_routing import scheduler_inference_log_model

        job_id = uuid.uuid4().hex[:10]

        def on_step(fragment: dict) -> None:
            merge_ledger_llm_progress(httpd, **fragment)

        _vm = scheduler_inference_log_model(gateway_origin=llm_origin, client_model=label_model)
        mlabel = short_model_label(_vm)
        progress_model_id = _vm
        try:
            financial_flow_log(
                job_id,
                "background ledger LLM job started "
                "(title/category rounds → mix-chart bar labels)",
                lane="job",
                role="ledger_llm_bg",
                model=progress_model_id,
                mlx="enqueue",
                gateway=llm_origin,
            )
            merge_ledger_llm_progress(
                httpd,
                active=True,
                phase="title_category",
                step="start",
                detail=(
                    f"Ledger LLM job started — label model {mlabel} "
                    f"(titles, categories, mix bars); gateway {llm_origin}."
                ),
                model=mlabel,
                gateway=llm_origin,
                error=None,
                percent=2,
                progress_log=[],
            )
            conn = connect()
            try:
                init_schema(conn)
                for i in range(32):
                    merge_ledger_llm_progress(
                        httpd,
                        phase="title_category",
                        title_round=i + 1,
                        percent=min(85, 2 + int(83 * (i + 1) / 32)),
                        step="title_round",
                        detail=(
                            f"Round {i + 1} (≤32): fetching pending debits, "
                            f"then LLM + ledger write…"
                        ),
                        model=mlabel,
                        gateway=llm_origin,
                    )
                    r = run_ledger_titling_pass(
                        conn,
                        llm_origin,
                        model=label_model or None,
                        max_items=40,
                        chunk_size=1,
                        on_progress=on_step,
                        flow_id=job_id,
                    )
                    if r.get("reason") == "none_pending":
                        merge_ledger_llm_progress(
                            httpd,
                            phase="title_category",
                            step="titles_done",
                            detail="All debits have title + category (or queue empty).",
                            percent=86,
                            model=mlabel,
                        )
                        break
                    if not r.get("ok"):
                        saved = int(r.get("updated") or 0)
                        if saved > 0:
                            merge_ledger_llm_progress(
                                httpd,
                                phase="title_category",
                                step="title_round_recover",
                                title_round=i + 1,
                                detail=(
                                    f"Round {i + 1}: error after {saved} debit(s) "
                                    "were committed to the ledger; retrying next round."
                                ),
                                error=str(r.get("error") or r.get("detail") or ""),
                                model=mlabel,
                                gateway=llm_origin,
                            )
                            continue
                        merge_ledger_llm_progress(
                            httpd,
                            active=False,
                            phase="error",
                            step="failed",
                            detail=str(r.get("error", "title pass failed")),
                            error=str(r.get("error", "")),
                            percent=None,
                        )
                        return
                merge_ledger_llm_progress(
                    httpd,
                    phase="mix_chart",
                    step="mix_start",
                    detail="Starting spending-mix Other · bar-label polish (same model).",
                    percent=87,
                    model=mlabel,
                )
                for j in range(8):
                    merge_ledger_llm_progress(
                        httpd,
                        mix_round=j + 1,
                        percent=min(99, 87 + int(12 * (j + 1) / 8)),
                        step="mix_round",
                        detail=(
                            f"Mix-chart round {j + 1} (≤8): LLM bar labels "
                            f"→ mix_chart_labels table…"
                        ),
                        model=mlabel,
                        gateway=llm_origin,
                    )
                    mr = run_mix_chart_label_pass(
                        conn,
                        llm_origin,
                        model=label_model or None,
                        max_rows=28,
                        on_progress=on_step,
                        flow_id=job_id,
                    )
                    if not mr.get("ok"):
                        print(
                            f"[financial-ui] mix chart labels: {mr.get('error', mr)}",
                            file=sys.stderr,
                            flush=True,
                        )
                        merge_ledger_llm_progress(
                            httpd,
                            active=False,
                            phase="error",
                            step="mix_failed",
                            detail=str(mr.get("error", "mix pass failed")),
                            error=str(mr.get("error", "")),
                        )
                        return
                    if mr.get("reason") in ("none_pending", "empty_ledger"):
                        merge_ledger_llm_progress(
                            httpd,
                            step="mix_done",
                            detail="No further mix-chart rows to label.",
                            percent=99,
                        )
                        break
                    if mr.get("skipped") == "display_titles_pending":
                        merge_ledger_llm_progress(
                            httpd,
                            step="mix_skipped",
                            detail="Mix labels skipped (titles still pending).",
                            percent=99,
                        )
                        break
                    if int(mr.get("updated") or 0) == 0:
                        break
            finally:
                conn.close()
            merge_ledger_llm_progress(
                httpd,
                active=False,
                phase="idle",
                step="done",
                detail="Ledger LLM background job finished. Refresh charts to see updates.",
                percent=100,
                error=None,
            )
            financial_flow_log(
                job_id,
                "background ledger LLM job finished cleanly",
                lane="job",
                role="ledger_llm_bg",
                model=progress_model_id,
                mlx="done",
                gateway=llm_origin,
            )
        except Exception as e:
            merge_ledger_llm_progress(
                httpd,
                active=False,
                phase="error",
                step="exception",
                detail=str(e),
                error=str(e),
            )
            financial_flow_log(
                job_id,
                f"background ledger LLM job raised: {e!s}",
                lane="job",
                role="ledger_llm_bg",
                mlx="error",
                gateway=llm_origin,
            )
            print(f"[financial-ui] ledger titling: {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_job, daemon=True).start()


def _ledger_connection():
    from financial_ledger_store import connect, init_schema

    conn = connect()
    init_schema(conn)
    return conn


def _extract_multipart_file(body: bytes, content_type: str) -> tuple[str, bytes] | None:
    if "multipart/form-data" not in content_type.lower():
        return None
    m = re.search(r"boundary=([^;\s]+)", content_type, re.I)
    if not m:
        return None
    boundary = m.group(1).strip().strip('"').encode("ascii", "ignore")
    if not boundary:
        return None
    segments = body.split(b"--" + boundary)
    for seg in segments:
        if b"Content-Disposition:" not in seg:
            continue
        if b'name="file"' not in seg and b"name='file'" not in seg:
            continue
        header_blob, sep, rest = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        fn_match = re.search(rb'filename="([^"]*)"', header_blob)
        fn_match = fn_match or re.search(rb"filename=\s*([^;\r\n]+)", header_blob)
        raw_name = (fn_match.group(1).decode("utf-8", "replace").strip() if fn_match else "")
        content = rest
        if content.endswith(b"\r\n"):
            content = content[:-2]
        if content.endswith(b"\n"):
            content = content[:-1]
        name = raw_name or "upload.csv"
        return name, content
    return None


def _plain_completion(
    origin: str,
    payload: dict,
    *,
    timeout: float = 120.0,
    model: str | None = None,
    flow_id: str | None = None,
    purpose: str = "insights",
) -> tuple[int, dict]:
    url = origin.rstrip("/") + "/v1/plain-completion"
    pl = {**payload}
    m = (model or "").strip()
    if m:
        pl["model"] = m
    body = json.dumps(pl).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    u_len = len((payload.get("user") or "") if isinstance(payload.get("user"), str) else "")
    s_len = len(
        (payload.get("system") or "") if isinstance(payload.get("system"), str) else ""
    )
    if flow_id:
        financial_flow_log(
            flow_id,
            f"POST {url} ({purpose}) "
            f"system_chars≈{s_len} user_chars≈{u_len} payload_bytes={len(body)}",
            lane="gateway",
            role=purpose,
            model=model or origin,
            mlx="to_gateway",
            gateway=origin,
        )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        code = getattr(resp, "status", 200) or 200
        decoded = json.loads(raw.decode("utf-8"))
        if flow_id:
            tx = decoded.get("text")
            tl = len(tx) if isinstance(tx, str) else 0
            financial_flow_log(
                flow_id,
                f"gateway HTTP {code} response_chars={tl} resolved_model="
                f"{decoded.get('model')!r}",
                lane="gateway",
                role=purpose,
                model=decoded.get("model") if isinstance(decoded.get("model"), str) else model,
                mlx="from_gateway",
                gateway=origin,
            )
        return code, decoded


def _serve_financial_html(handler: BaseHTTPRequestHandler) -> None:
    if not _HTML_PATH.is_file():
        handler._send_binary(
            500,
            b"Missing financial_analytics.html\n",
            "text/plain; charset=utf-8",
        )
        return
    handler._send_binary(200, _HTML_PATH.read_bytes(), "text/html; charset=utf-8")


def financial_dispatch_get(
    handler: BaseHTTPRequestHandler, path: str, qs: dict[str, list[str]]
) -> bool:
    """Return True when the request is fully handled."""
    if path in ("/finances", "/financial_analytics.html"):
        _serve_financial_html(handler)
        return True

    if path == "/api/files":
        names = [p.name for p in _financial_csv_paths()]
        handler._send_json(200, {"files": names})
        return True

    if path == "/api/ledger_meta":
        from financial_ledger_store import ledger_meta

        conn = _ledger_connection()
        try:
            meta = ledger_meta(conn)
        finally:
            conn.close()
        handler._send_json(200, meta)
        return True

    if path == "/api/ledger_llm_progress":
        with handler.server.ledger_llm_progress_lock:
            payload = dict(handler.server.ledger_llm_progress)
        handler._send_json(200, payload)
        return True

    if path == "/api/summary":
        from bank_statement_csv import (
            clip_statement_rows,
            load_statement_rows_from_path,
            summarize_rows,
            summarize_rows_for_insights,
        )
        from financial_ledger_store import (
            contributing_source_files,
            enrich_summary_with_ledger,
            fetch_statement_rows,
            ledger_meta,
        )

        mode = (qs.get("mode", [""])[0] or "").strip().lower()
        fn = (qs.get("file", [None])[0] or "").strip()
        start = _parse_iso_date(qs.get("start", [None])[0])
        end = _parse_iso_date(qs.get("end", [None])[0])
        ex_sav_brk = _query_bool(qs, "exclude_sav_brk_transfers", default=False)

        if mode == "aggregate":
            conn = _ledger_connection()
            try:
                meta = ledger_meta(conn)
                if meta.get("transaction_count", 0) == 0:
                    handler._send_json(
                        400,
                        {
                            "error": "Ledger is empty.",
                            "hint": (
                                "Upload CSVs or POST /api/reindex to rebuild the ledger "
                                "from financial-data/*.csv."
                            ),
                        },
                    )
                    return True
                rows = fetch_statement_rows(conn, start, end)
                contrib = contributing_source_files(conn, start, end)
                if ex_sav_brk:
                    summary = summarize_rows_for_insights(
                        rows,
                        filename="aggregate",
                        preamble={},
                        include_running_balance=False,
                        exclude_sav_brk_transfer_debits=True,
                    )
                else:
                    summary = summarize_rows(
                        rows,
                        filename="aggregate",
                        preamble={},
                        include_running_balance=False,
                    )
                summary["view"] = {
                    "mode": "aggregate",
                    "date_range_start": start.isoformat() if start else None,
                    "date_range_end": end.isoformat() if end else None,
                    "contributing_files": contrib,
                    "exclude_sav_brk_transfers": ex_sav_brk,
                }
                enrich_summary_with_ledger(conn, summary)
            finally:
                conn.close()
            handler._send_json(200, summary)
            return True

        if mode and mode != "snapshot":
            handler._send_json(400, {"error": "Unknown mode (use aggregate or snapshot)."})
            return True
        safe = _safe_csv_filename(fn)
        if not safe:
            handler._send_json(
                400,
                {"error": "Invalid or missing `file` for snapshot (CSV name only)."},
            )
            return True
        target = _FINANCIAL_DIR / safe
        if not target.is_file():
            handler._send_json(404, {"error": f"File not found: {safe}"})
            return True
        try:
            preamble, rows = load_statement_rows_from_path(target)
            rows = clip_statement_rows(rows, start, end)
            if ex_sav_brk:
                summary = summarize_rows_for_insights(
                    rows,
                    filename=safe,
                    preamble=preamble,
                    include_running_balance=True,
                    exclude_sav_brk_transfer_debits=True,
                )
            else:
                summary = summarize_rows(
                    rows,
                    filename=safe,
                    preamble=preamble,
                    include_running_balance=True,
                )
            summary["view"] = {
                "mode": "snapshot",
                "date_range_start": start.isoformat() if start else None,
                "date_range_end": end.isoformat() if end else None,
                "contributing_files": [safe],
                "exclude_sav_brk_transfers": ex_sav_brk,
            }
        except (OSError, ValueError) as e:
            handler._send_json(400, {"error": str(e)})
            return True
        lconn = _ledger_connection()
        try:
            from financial_ledger_store import init_schema

            init_schema(lconn)
            enrich_summary_with_ledger(lconn, summary)
        finally:
            lconn.close()
        handler._send_json(200, summary)
        return True

    return False


def financial_dispatch_delete(
    handler: BaseHTTPRequestHandler, path: str, qs: dict[str, list[str]]
) -> bool:
    """DELETE /api/files?file=name.csv — remove CSV from disk and rebuild ledger."""
    if path != "/api/files":
        return False
    origin = getattr(handler.server, "llm_origin", DEFAULT_LLM_ORIGIN)
    raw = qs.get("file", [None])[0]
    safe = _safe_csv_filename(raw if isinstance(raw, str) else None)
    if not safe:
        handler._send_json(400, {"error": "Missing or invalid `file` query (CSV name only)."})
        return True
    target = _FINANCIAL_DIR / safe
    if not target.is_file():
        handler._send_json(404, {"error": f"File not found: {safe}"})
        return True
    try:
        target.unlink()
    except OSError as e:
        handler._send_json(400, {"error": str(e)})
        return True

    from financial_ledger_store import reindex_all_csvs

    conn = _ledger_connection()
    try:
        stats = reindex_all_csvs(conn, _FINANCIAL_DIR)
    except (OSError, ValueError) as e:
        handler._send_json(400, {"error": str(e)})
        return True
    finally:
        conn.close()
    _schedule_ledger_titling(
        origin,
        getattr(handler.server, "financial_label_model", ""),
        handler.server,
    )
    handler._send_json(200, {"ok": True, "deleted": safe, "reindex": stats})
    return True


def financial_dispatch_post(handler: BaseHTTPRequestHandler, path: str) -> bool:
    """Return True when the request is fully handled."""
    origin = getattr(handler.server, "llm_origin", DEFAULT_LLM_ORIGIN)

    if path == "/api/upload":
        flow_id = uuid.uuid4().hex[:10]
        ctype = handler.headers.get("Content-Type") or ""
        body = handler._read_body()
        financial_flow_log(
            flow_id,
            "HTTP POST /api/upload (body received)",
            lane="http",
            role="upload",
            mlx="from_client",
        )
        filename: str | None = None
        data: bytes | None = None
        extracted = _extract_multipart_file(body, ctype)
        if extracted is not None:
            filename, data = extracted
        elif ctype.lower().split(";")[0].strip() in (
            "text/csv",
            "application/csv",
            "application/octet-stream",
        ):
            filename = handler.headers.get("X-Filename") or handler.headers.get("X-File-Name")
            data = body
        if not data:
            handler._send_json(
                400,
                {"error": "No file body (use multipart field 'file' or raw CSV)."},
            )
            return True
        safe = _safe_csv_filename(filename)
        if not safe:
            handler._send_json(400, {"error": "Invalid filename; use a .csv name."})
            return True
        _FINANCIAL_DIR.mkdir(parents=True, exist_ok=True)
        dest = _FINANCIAL_DIR / safe
        dest.write_bytes(data)
        ingest: dict | None = None
        conn = _ledger_connection()
        try:
            from financial_ledger_store import ingest_bank_csv

            ingest = ingest_bank_csv(conn, dest)
        except (OSError, ValueError) as e:
            ingest = {"error": str(e)}
        finally:
            conn.close()
        handler._send_json(
            200,
            {"ok": True, "file": safe, "bytes": len(data), "ledger_ingest": ingest},
        )
        ok_ingest = isinstance(ingest, dict) and "error" not in ingest
        financial_flow_log(
            flow_id,
            f"SQLite ledger ingest_bank_csv committed for {safe!r} snapshot={ingest!r}",
            lane="ledger",
            role="upload",
            mlx="posted" if ok_ingest else "error",
        )
        if ok_ingest:
            _schedule_ledger_titling(
                origin,
                getattr(handler.server, "financial_label_model", ""),
                handler.server,
            )
        return True

    if path == "/api/reindex":
        rfx_id = uuid.uuid4().hex[:10]
        financial_flow_log(
            rfx_id,
            "HTTP POST /api/reindex (rebuild SQLite ledger from financial-data/*.csv)",
            lane="http",
            role="reindex",
            mlx="from_client",
        )
        from financial_ledger_store import reindex_all_csvs

        conn = _ledger_connection()
        try:
            stats = reindex_all_csvs(conn, _FINANCIAL_DIR)
        except (OSError, ValueError) as e:
            financial_flow_log(
                rfx_id,
                f"reindex_all_csvs failed: {e!s}",
                lane="ledger",
                role="reindex",
                mlx="error",
            )
            handler._send_json(400, {"error": str(e)})
            return True
        finally:
            conn.close()
        handler._send_json(200, stats)
        financial_flow_log(
            rfx_id,
            f"reindex_all_csvs committed stats={stats!r}",
            lane="ledger",
            role="reindex",
            mlx="posted",
        )
        _schedule_ledger_titling(
            origin,
            getattr(handler.server, "financial_label_model", ""),
            handler.server,
        )
        return True

    if path == "/api/insights":
        insights_fid = uuid.uuid4().hex[:10]
        body_raw = handler._read_body(cap=2_000_000)
        financial_flow_log(
            insights_fid,
            f"HTTP POST /api/insights bytes={len(body_raw)}",
            lane="http",
            role="insights",
            mlx="from_client",
        )
        try:
            payload = json.loads(body_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            handler._send_json(400, {"error": "Invalid JSON body"})
            return True
        if not isinstance(payload, dict):
            handler._send_json(400, {"error": "JSON body must be an object"})
            return True

        mode = str(payload.get("mode") or "snapshot").strip().lower()
        raw_start = payload.get("start")
        raw_end = payload.get("end")
        start = _parse_iso_date(raw_start if isinstance(raw_start, str) else None)
        end = _parse_iso_date(raw_end if isinstance(raw_end, str) else None)

        from bank_statement_csv import (
            clip_statement_rows,
            llm_digest_payload,
            load_statement_rows_from_path,
            summarize_rows_for_insights,
        )
        from financial_ledger_store import (
            build_insights_cache_key,
            contributing_source_files,
            enrich_summary_with_ledger,
            fetch_statement_rows,
            insights_cache_get,
            insights_cache_store,
            ledger_meta,
            transaction_fingerprint,
        )

        exclude_sav_brk = _json_bool(payload.get("exclude_sav_brk_transfers"), default=False)
        force = _json_bool(payload.get("force"), default=False)

        summary: dict
        digest_ctx: dict[str, str | None]
        response_extra: dict[str, str | bool | None] = {
            "mode": mode,
            "date_range_start": start.isoformat() if start else None,
            "date_range_end": end.isoformat() if end else None,
            "exclude_sav_brk_transfers": exclude_sav_brk,
        }

        row_fp_list: list[str] = []
        snapshot_safe: str | None = None

        if mode == "aggregate":
            conn = _ledger_connection()
            try:
                meta = ledger_meta(conn)
                if meta.get("transaction_count", 0) == 0:
                    handler._send_json(
                        400,
                        {
                            "error": "Ledger is empty.",
                            "hint": (
                                "Upload CSVs or POST /api/reindex to rebuild from "
                                "financial-data/*.csv."
                            ),
                        },
                    )
                    return True
                rows = fetch_statement_rows(conn, start, end)
                row_fp_list = sorted({transaction_fingerprint(r) for r in rows})
                contrib = contributing_source_files(conn, start, end)
                summary = summarize_rows_for_insights(
                    rows,
                    filename="aggregate",
                    preamble={},
                    include_running_balance=False,
                    exclude_sav_brk_transfer_debits=exclude_sav_brk,
                )
                digest_ctx = {
                    "mode": "aggregate",
                    "date_range_start": start.isoformat() if start else None,
                    "date_range_end": end.isoformat() if end else None,
                    "contributing_files": ", ".join(contrib) if contrib else None,
                }
                response_extra["contributing_files"] = ", ".join(contrib) if contrib else None
                enrich_summary_with_ledger(conn, summary)
            finally:
                conn.close()
        elif mode == "snapshot":
            fn = payload.get("file")
            safe = _safe_csv_filename(fn if isinstance(fn, str) else None)
            if not safe:
                handler._send_json(
                    400,
                    {"error": "Snapshot mode requires {\"file\": \"name.csv\"}."},
                )
                return True
            target = _FINANCIAL_DIR / safe
            if not target.is_file():
                handler._send_json(404, {"error": f"File not found: {safe}"})
                return True
            try:
                preamble, rows = load_statement_rows_from_path(target)
                rows = clip_statement_rows(rows, start, end)
                snapshot_safe = safe
                row_fp_list = sorted({transaction_fingerprint(r) for r in rows})
                summary = summarize_rows_for_insights(
                    rows,
                    filename=safe,
                    preamble=preamble,
                    include_running_balance=True,
                    exclude_sav_brk_transfer_debits=exclude_sav_brk,
                )
                digest_ctx = {
                    "mode": "snapshot",
                    "date_range_start": start.isoformat() if start else None,
                    "date_range_end": end.isoformat() if end else None,
                    "contributing_files": safe,
                }
                response_extra["file"] = safe
            except (OSError, ValueError) as e:
                handler._send_json(400, {"error": str(e)})
                return True
            lconn = _ledger_connection()
            try:
                from financial_ledger_store import init_schema

                init_schema(lconn)
                enrich_summary_with_ledger(lconn, summary)
            finally:
                lconn.close()
        else:
            handler._send_json(400, {"error": "Unknown mode (use aggregate or snapshot)."})
            return True

        try:
            system = _insights_system_prompt_text()
        except OSError as e:
            handler._send_json(500, {"error": f"Missing system prompt: {e}"})
            return True

        insights_context_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()
        ckey = build_insights_cache_key(
            view_mode=mode,
            snapshot_file=snapshot_safe,
            date_start=start.isoformat() if start else None,
            date_end=end.isoformat() if end else None,
            exclude_sav_brk=exclude_sav_brk,
            transaction_fingerprints=row_fp_list,
            insights_context_hash=insights_context_hash,
        )

        if not force:
            ck_conn = _ledger_connection()
            try:
                cached = insights_cache_get(ck_conn, ckey)
                if cached:
                    financial_flow_log(
                        insights_fid,
                        f"insights cache hit id={cached['id']}",
                        lane="http",
                        role="insights",
                        mlx="cache_hit",
                    )
                    handler._send_json(
                        200,
                        {
                            "insights": cached["markdown"],
                            "cached": True,
                            "insights_id": cached["id"],
                            "cache_key": ckey,
                            "insights_model": cached["insights_model"],
                            "updated_at": cached["updated_at"],
                            **response_extra,
                        },
                    )
                    return True
            finally:
                ck_conn.close()

        user_block = llm_digest_payload(summary, digest_context=digest_ctx)
        upstream_payload = {
            "system": system,
            "user": user_block,
            "max_tokens": 3072,
        }
        insights_model = getattr(handler.server, "financial_insights_model", "")
        financial_flow_log(
            insights_fid,
            f"insights digest ready mode={digest_ctx.get('mode')!r} "
            f"user_block_chars={len(user_block)} — calling insights model",
            lane="pipeline",
            role="insights",
            model=insights_model or None,
            mlx="to_gateway",
            gateway=origin,
        )
        try:
            _, upstream_json = _plain_completion(
                origin,
                upstream_payload,
                timeout=180.0,
                model=insights_model or None,
                flow_id=insights_fid,
                purpose="insights",
            )
        except HTTPError as e:
            detail = e.read().decode("utf-8", "replace") if e.fp else str(e)
            financial_flow_log(
                insights_fid,
                f"insights gateway HTTP failure code={getattr(e, 'code', '?')!r}",
                lane="gateway",
                role="insights",
                mlx="from_gateway_error",
                gateway=origin,
            )
            handler._send_json(
                502,
                {
                    "error": "LLM gateway returned an error.",
                    "detail": detail[:800],
                    "upstream": origin,
                    "hint": (
                        "Start: export VLLM_14B_BASE_URL=… then uv run --group samples-vllm "
                        "python app/scheduler_llm_gateway.py"
                    ),
                },
            )
            return True
        except URLError as e:
            financial_flow_log(
                insights_fid,
                f"insights gateway unreachable ({e!s})",
                lane="gateway",
                role="insights",
                mlx="from_gateway_error",
                gateway=origin,
            )
            handler._send_json(
                502,
                {
                    "error": "Cannot reach LLM gateway.",
                    "detail": str(e),
                    "upstream": origin,
                    "hint": (
                        "Start: export VLLM_14B_BASE_URL=… then uv run --group samples-vllm "
                        "python app/scheduler_llm_gateway.py"
                    ),
                },
            )
            return True
        text = upstream_json.get("text")
        if not isinstance(text, str):
            err = upstream_json.get("error")
            financial_flow_log(
                insights_fid,
                f"unexpected gateway JSON shape (no text): {err!r}",
                lane="gateway",
                role="insights",
                mlx="from_gateway_error",
                gateway=origin,
            )
            handler._send_json(
                502,
                {
                    "error": "Unexpected gateway response.",
                    "detail": str(err or upstream_json)[:800],
                    "upstream": origin,
                },
            )
            return True
        st_conn = _ledger_connection()
        try:
            iid = insights_cache_store(
                st_conn,
                cache_key=ckey,
                markdown=text,
                insights_model=(
                    str(upstream_json.get("model") or insights_model or "").strip() or None
                ),
                view_mode=mode,
                snapshot_file=snapshot_safe,
                date_start=start.isoformat() if start else None,
                date_end=end.isoformat() if end else None,
                exclude_sav_brk=exclude_sav_brk,
                transaction_count=len(row_fp_list),
            )
        finally:
            st_conn.close()
        financial_flow_log(
            insights_fid,
            f"sending markdown insights HTTP 200 chars={len(text)} "
            f"model={upstream_json.get('model')!r} stored_id={iid}",
            lane="http",
            role="insights",
            mlx="to_client",
        )
        handler._send_json(
            200,
            {
                "insights": text,
                "model": upstream_json.get("model"),
                "cached": False,
                "insights_id": iid,
                "cache_key": ckey,
                **response_extra,
            },
        )
        return True

    return False


class FinancialAnalyticsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        line = fmt % args if args else fmt
        _FIN_UI_HTTP_ACCESS.note(self, line)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_binary(self, code: int, data: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self, cap: int = 20_000_000) -> bytes:
        ln = self.headers.get("Content-Length")
        try:
            n = int(ln or "0")
        except ValueError:
            return b""
        return self.rfile.read(max(0, min(n, cap)))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            _serve_financial_html(self)
            return

        if financial_dispatch_get(self, path, qs):
            return

        self._send_binary(404, b"Not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if financial_dispatch_post(self, path):
            return

        self._send_binary(404, b"Not found\n", "text/plain; charset=utf-8")

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if financial_dispatch_delete(self, path, qs):
            return
        self._send_binary(404, b"Not found\n", "text/plain; charset=utf-8")


class FinancialAnalyticsServer(ThreadingHTTPServer):
    def __init__(
        self,
        host: str,
        port: int,
        llm_origin: str,
        *,
        financial_label_model: str,
        financial_insights_model: str,
    ):
        from ledger_llm_progress import initial_ledger_llm_progress

        self.llm_origin = llm_origin.rstrip("/")
        self.financial_label_model = financial_label_model
        self.financial_insights_model = financial_insights_model
        self.ledger_llm_progress_lock = threading.Lock()
        self.ledger_llm_progress = initial_ledger_llm_progress()
        super().__init__((host, port), FinancialAnalyticsHandler)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Financial analytics sample UI.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument(
        "--llm-origin",
        default=DEFAULT_LLM_ORIGIN,
        help="Scheduler LLM gateway base URL (default env MLX_SCHEDULER_LLM_API or http://127.0.0.1:8766).",
    )
    p.add_argument(
        "--financial-label-model",
        default=None,
        help=(
            "Model id for ledger titles/categories/mix labels "
            "(env MLX_FINANCIAL_LABEL_MODEL; default Qwen3-8B local or Hub)."
        ),
    )
    p.add_argument(
        "--financial-insights-model",
        default=None,
        help=(
            "Model id for narrative insights "
            "(env MLX_FINANCIAL_INSIGHTS_MODEL; default Qwen3-14B local or Hub)."
        ),
    )
    ns = p.parse_args(argv if argv is not None else sys.argv[1:])
    from financial_llm_models import (
        resolve_financial_insights_model,
        resolve_financial_label_model,
    )

    label_m = resolve_financial_label_model(ns.financial_label_model)
    insight_m = resolve_financial_insights_model(ns.financial_insights_model)
    _FINANCIAL_DIR.mkdir(parents=True, exist_ok=True)
    _lc = _ledger_connection()
    _lc.close()
    httpd = FinancialAnalyticsServer(
        ns.host,
        ns.port,
        ns.llm_origin,
        financial_label_model=label_m,
        financial_insights_model=insight_m,
    )
    conn_kick = _ledger_connection()
    try:
        from financial_ledger_store import ledger_meta

        meta_kick = ledger_meta(conn_kick)
        if int(meta_kick.get("retitle_pending_count") or 0) > 0:
            with httpd.ledger_llm_progress_lock:
                already = bool(httpd.ledger_llm_progress.get("active"))
            if not already:
                _schedule_ledger_titling(ns.llm_origin.rstrip("/"), label_m, httpd)
    finally:
        conn_kick.close()
    url = f"http://{ns.host}:{ns.port}/"
    print(f"Financial analytics UI → {url}", file=sys.stderr, flush=True)
    print(f"  Data directory: {_FINANCIAL_DIR}", file=sys.stderr, flush=True)
    print(f"  LLM gateway: {ns.llm_origin}", file=sys.stderr, flush=True)
    print(f"  Label model (titles/categories): {label_m}", file=sys.stderr, flush=True)
    print(f"  Insights model: {insight_m}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nFinancial UI stopped.", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
