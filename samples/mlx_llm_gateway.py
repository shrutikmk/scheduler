#!/usr/bin/env python3
"""Standalone **internal MLX LLM API** for the day scheduler (day-scheduler mode only).

Start this first; keep it running while you use the web UI::

    uv run --group samples-mlx python samples/mlx_llm_gateway.py

Default bind: ``http://127.0.0.1:8766``

Endpoints:

- ``GET /health`` — readiness / model snapshot (JSON).
- ``POST /v1/day-scheduler/chat`` or ``POST /chat`` — same NDJSON streaming body as the old
  monolithic UI.

Then start the thin static UI (proxies chat to this service)::

    uv run python samples/mlx_day_scheduler_ui.py

Or set ``MLX_SCHEDULER_LLM_API=http://127.0.0.1:8766`` (default matches gateway port).

Stop: press ``Ctrl+C`` in the gateway terminal (or kill the process).
"""

from __future__ import annotations

import argparse
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

SAMPLES_ROOT = Path(__file__).resolve().parent
if str(SAMPLES_ROOT) not in sys.path:
    sys.path.insert(0, str(SAMPLES_ROOT))

from mlx_chat_cli import DEFAULT_HUB_REPO, resolve_model_arg, run_diagnose
from mlx_scheduler_llm_api import (
    add_gateway_argparse,
    argparse_to_factory_kwargs,
    build_llm_gateway_handler,
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

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if ns.diagnose_only:
        resolved = resolve_model_arg(ns.model)
        return run_diagnose(resolved)

    model_o, _, err = ensure_model_loaded(model=ns.model)
    if model_o is None:
        print(err, file=sys.stderr)
        return 2

    handler_cls = build_llm_gateway_handler(**argparse_to_factory_kwargs(ns))

    httpd = ThreadingHTTPServer((ns.host, ns.port), handler_cls)

    api = f"http://{ns.host}:{ns.port}/"
    print(f"MLX LLM gateway → {api}", file=sys.stderr, flush=True)
    print(f"  GET  {api}health", file=sys.stderr, flush=True)
    print(f"  POST {api}v1/day-scheduler/chat", file=sys.stderr, flush=True)
    resolved = resolve_model_arg(ns.model)
    print(
        "model:",
        resolved,
        f"(hub default: {DEFAULT_HUB_REPO})",
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
