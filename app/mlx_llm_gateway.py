#!/usr/bin/env python3
"""Standalone **internal MLX LLM API** (day-scheduler chat + plain JSON completions).

Start this first; keep it running while you use the web UIs::

    uv run --group samples-mlx python app/mlx_llm_gateway.py

Default bind: ``http://127.0.0.1:8766``

Endpoints:

- ``GET /health`` — readiness / model snapshot (JSON).
- ``POST /v1/day-scheduler/chat`` or ``POST /chat`` — same NDJSON streaming body as the old
  monolithic UI.

Then start the thin static UI (proxies chat to this service)::

    uv run python app/mlx_day_scheduler_ui.py

Or set ``MLX_SCHEDULER_LLM_API=http://127.0.0.1:8766`` (default matches gateway port).

- ``POST /v1/plain-completion`` — generic non-streaming completion (JSON ``system`` + ``user``;
  optional ``model`` loads another checkpoint on demand). Financial analytics uses this for insights
  and label batches. Each model id has its own in-memory bundle and lock, so Qwen3-8B and Qwen3-14B
  can serve concurrent requests on this ``ThreadingHTTPServer``.

By default the gateway also **preloads** the financial label snapshot when
``MLX_FINANCIAL_LABEL_MODEL`` is set or ``~/models/Qwen3-8B`` exists (and it differs from the
day-scheduler model). Pass ``--no-preload-financial-label-model`` to skip, or
``--preload-financial-label-model PATH`` to force a path.

The day-scheduler **query parser** uses the same fast ~8B class by default (env
``SCHEDULER_QUERY_PARSER_MODEL``, else the financial-label resolution chain). Preload it with
``--preload-query-parser-model`` or disable with ``--no-preload-query-parser-model``.

Stop: press ``Ctrl+C`` in the gateway terminal (or kill the process).
"""

from __future__ import annotations

import argparse
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent
SAMPLES_ROOT = REPO_ROOT / "samples"
for _p in (APP_ROOT, SAMPLES_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from mlx_chat_cli import run_diagnose  # noqa: E402  (samples/ baseline; kept on sys.path above)
from financial_llm_models import (  # noqa: E402
    resolve_financial_label_model,
    resolve_scheduler_query_parser_model,
)
from mlx_scheduler_llm_api import (  # noqa: E402
    _resolve_scheduler_model,
    add_gateway_argparse,
    argparse_to_factory_kwargs,
    build_llm_gateway_handler,
    ensure_model_bundle_loaded,
    ensure_model_loaded,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MLX day-scheduler LLM gateway API.")
    add_gateway_argparse(parser)
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help="Model diagnostics then exit.",
    )
    parser.add_argument(
        "--preload-financial-label-model",
        default=None,
        metavar="PATH_OR_HUB",
        help=(
            "Optional MLX weights for financial UI labels (titles/categories/mix bars). "
            "When omitted, preloads if MLX_FINANCIAL_LABEL_MODEL is set or ~/models/Qwen3-8B "
            "exists. Disabled by --no-preload-financial-label-model."
        ),
    )
    parser.add_argument(
        "--no-preload-financial-label-model",
        action="store_true",
        help="Do not load a second bundle for financial labels (saves RAM).",
    )
    parser.add_argument(
        "--preload-query-parser-model",
        default=None,
        metavar="PATH_OR_HUB",
        help=(
            "Optional MLX weights for day-scheduler query JSON parsing (~8B). "
            "When omitted, preloads if SCHEDULER_QUERY_PARSER_MODEL is set or the default "
            "query-parser resolution differs from the scheduler model. "
            "Disabled by --no-preload-query-parser-model."
        ),
    )
    parser.add_argument(
        "--no-preload-query-parser-model",
        action="store_true",
        help="Do not preload the query-parser snapshot (saves RAM; first chat turn may load it).",
    )

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if ns.diagnose_only:
        resolved = _resolve_scheduler_model(ns.model)
        return run_diagnose(resolved)

    model_o, _, err = ensure_model_loaded(model=ns.model)
    if model_o is None:
        print(err, file=sys.stderr)
        return 2

    sched_resolved = _resolve_scheduler_model(ns.model)
    label_to_preload: str | None = None
    if not ns.no_preload_financial_label_model:
        if ns.preload_financial_label_model and str(ns.preload_financial_label_model).strip():
            label_to_preload = resolve_financial_label_model(str(ns.preload_financial_label_model).strip())
        elif os.environ.get("MLX_FINANCIAL_LABEL_MODEL", "").strip():
            label_to_preload = resolve_financial_label_model(None)
        elif (Path.home() / "models" / "Qwen3-8B").is_dir():
            label_to_preload = resolve_financial_label_model(None)
    if label_to_preload and label_to_preload.strip() != sched_resolved.strip():
        bundle, err_lb = ensure_model_bundle_loaded(model=label_to_preload)
        if bundle is None:
            print(err_lb, file=sys.stderr)
            return 2

    parser_to_preload: str | None = None
    if not ns.no_preload_query_parser_model:
        if ns.preload_query_parser_model and str(ns.preload_query_parser_model).strip():
            parser_to_preload = resolve_scheduler_query_parser_model(
                str(ns.preload_query_parser_model).strip()
            )
        elif os.environ.get("SCHEDULER_QUERY_PARSER_MODEL", "").strip():
            parser_to_preload = resolve_scheduler_query_parser_model(None)
        elif (Path.home() / "models" / "Qwen3-8B").is_dir():
            parser_to_preload = resolve_scheduler_query_parser_model(None)
    if parser_to_preload and parser_to_preload.strip() != sched_resolved.strip():
        bundle_qp, err_qp = ensure_model_bundle_loaded(model=parser_to_preload)
        if bundle_qp is None:
            print(err_qp, file=sys.stderr)
            return 2

    handler_cls = build_llm_gateway_handler(**argparse_to_factory_kwargs(ns))

    httpd = ThreadingHTTPServer((ns.host, ns.port), handler_cls)

    api = f"http://{ns.host}:{ns.port}/"
    print(f"MLX LLM gateway → {api}", file=sys.stderr, flush=True)
    print(f"  GET  {api}health", file=sys.stderr, flush=True)
    print(f"  POST {api}v1/day-scheduler/chat", file=sys.stderr, flush=True)
    print(f"  POST {api}v1/plain-completion", file=sys.stderr, flush=True)
    print(
        "day-scheduler model:",
        sched_resolved,
        "(hub default: Qwen/Qwen3-14B)",
        file=sys.stderr,
        flush=True,
    )
    if label_to_preload and label_to_preload.strip() != sched_resolved.strip():
        print(
            "financial label model (preloaded):",
            label_to_preload,
            file=sys.stderr,
            flush=True,
        )
    elif not ns.no_preload_financial_label_model:
        print(
            "financial label model: (not preloaded — set MLX_FINANCIAL_LABEL_MODEL, "
            "install ~/models/Qwen3-8B, or pass --preload-financial-label-model)",
            file=sys.stderr,
            flush=True,
        )

    if parser_to_preload and parser_to_preload.strip() != sched_resolved.strip():
        print(
            "query parser model (preloaded):",
            parser_to_preload,
            file=sys.stderr,
            flush=True,
        )
    elif not ns.no_preload_query_parser_model:
        print(
            "query parser model: (not preloaded — set SCHEDULER_QUERY_PARSER_MODEL, "
            "install ~/models/Qwen3-8B, or pass --preload-query-parser-model)",
            file=sys.stderr,
            flush=True,
        )

    thinking_off = os.environ.get(
        "MLX_DAY_SCHEDULER_NO_THINKING", ""
    ).strip().lower() in ("1", "true", "yes") or bool(
        getattr(ns, "no_day_scheduler_thinking", False)
    )
    self_grade_on = bool(getattr(ns, "self_grade", False))
    print(
        f"latency knobs: thinking={'off' if thinking_off else 'on'}  "
        f"self_grade={'on' if self_grade_on else 'off'}",
        file=sys.stderr,
        flush=True,
    )
    if not thinking_off:
        print(
            "  (set MLX_DAY_SCHEDULER_NO_THINKING=1 or pass --no-day-scheduler-thinking "
            "for faster, lower-variance replies)",
            file=sys.stderr,
            flush=True,
        )
    if self_grade_on:
        print(
            "  (self-grade adds a second full LLM call per reply; "
            "drop --self-grade / unset MLX_DAY_SCHEDULER_SELF_GRADE to disable)",
            file=sys.stderr,
            flush=True,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nLLM gateway stopped.", file=sys.stderr)
    finally:
        httpd.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
