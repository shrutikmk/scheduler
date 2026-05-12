#!/usr/bin/env python3
"""Primary **scheduler LLM gateway**: vLLM Metal (OpenAI API) only.

Default bind: ``http://127.0.0.1:8766``

Requires a running vLLM OpenAI-compatible server (e.g. vLLM Metal)::

    export VLLM_14B_BASE_URL=http://127.0.0.1:8000/v1
    uv run --group samples-vllm python app/scheduler_llm_gateway.py

Endpoints: ``GET /health``, ``POST /v1/day-scheduler/chat``, ``POST /v1/plain-completion``.
"""

from __future__ import annotations

import argparse
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent
SAMPLES_ROOT = REPO_ROOT / "samples"
for _p in (APP_ROOT, SAMPLES_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scheduler_llm_http_handler import (  # noqa: E402
    _resolve_scheduler_model,
    add_gateway_argparse,
    argparse_to_factory_kwargs,
    build_llm_gateway_handler,
)
from vllm_gateway_routing import (  # noqa: E402
    VllmGatewayContext,
    diagnose_vllm_metal_server,
    probe_vllm_route,
    vllm_route_from_env,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scheduler LLM gateway (vLLM OpenAI API only).",
    )
    add_gateway_argparse(parser)
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help=(
            "Exit after checks: detailed vLLM probe when VLLM_14B_BASE_URL is set; "
            "also loads tokenizer for --model / SCHEDULER_MODEL."
        ),
    )

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])
    vr = vllm_route_from_env()

    if ns.diagnose_only:
        if vr is None:
            print(
                "error: --diagnose-only requires VLLM_14B_BASE_URL",
                file=sys.stderr,
            )
            return 2
        import httpx

        with httpx.Client() as client:
            rc = diagnose_vllm_metal_server(client, vr)
        try:
            from scheduler_tokenizer import load_tokenizer_only

            tok_id = _resolve_scheduler_model(ns.model)
            load_tokenizer_only(tok_id)
            print(f"tokenizer ok: {tok_id!r}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"tokenizer check failed: {exc}", file=sys.stderr, flush=True)
            return 3 if rc == 0 else rc
        return rc

    if vr is None:
        print(
            "error: set VLLM_14B_BASE_URL to your vLLM OpenAI base (e.g. http://127.0.0.1:8000/v1)",
            file=sys.stderr,
        )
        print(
            "  Example: vllm serve ~/models/Qwen3-14B --port 8000 --served-model-name Qwen3-14B",
            file=sys.stderr,
        )
        return 2

    import httpx

    http_client = httpx.Client()
    if not probe_vllm_route(http_client, vr):
        print(
            f"error: vLLM unreachable for {vr.api_base!r}.",
            file=sys.stderr,
        )
        print(
            "  Start vLLM (example): vllm serve ~/models/Qwen3-14B --port 8000 "
            "--served-model-name Qwen3-14B",
            file=sys.stderr,
        )
        http_client.close()
        return 2

    tok_id = _resolve_scheduler_model(ns.model)
    vctx = VllmGatewayContext(
        client=http_client,
        route=vr,
        tokenizer_model_id=tok_id,
    )

    factory_kw = argparse_to_factory_kwargs(ns)
    factory_kw["vllm_context"] = vctx
    handler_cls = build_llm_gateway_handler(**factory_kw)
    httpd = ThreadingHTTPServer((ns.host, ns.port), handler_cls)

    api = f"http://{ns.host}:{ns.port}/"
    print(f"Scheduler LLM gateway (vLLM) → {api}", file=sys.stderr, flush=True)
    print(f"  GET  {api}health", file=sys.stderr, flush=True)
    print(f"  POST {api}v1/day-scheduler/chat", file=sys.stderr, flush=True)
    print(f"  POST {api}v1/plain-completion", file=sys.stderr, flush=True)
    print("tokenizer / bookkeeping model:", tok_id, file=sys.stderr, flush=True)
    print(
        f"  vLLM: {vr.api_base} model={vr.served_model_name}",
        file=sys.stderr,
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nLLM gateway stopped.", file=sys.stderr)
    finally:
        httpd.server_close()
        http_client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
