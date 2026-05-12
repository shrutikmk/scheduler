#!/usr/bin/env python3
"""Primary **scheduler LLM gateway**: vLLM Metal (OpenAI API) when configured, else MLX.

Default bind: ``http://127.0.0.1:8766``

vLLM (recommended on Apple Silicon with `vllm-metal`)::

    export VLLM_14B_BASE_URL=http://127.0.0.1:8000/v1
    # Optional if ``--served-model-name`` differs from default:
    # export VLLM_14B_MODEL=Qwen3-14B

    uv run --group samples-vllm python app/scheduler_llm_gateway.py

MLX in-process (legacy)::

    uv run --group samples-mlx python app/scheduler_llm_gateway.py --llm-backend mlx

Or use ``app/mlx_llm_gateway.py`` directly (always MLX).

Same HTTP API as ``mlx_llm_gateway.py``: ``GET /health``, ``POST /v1/day-scheduler/chat``,
``POST /v1/plain-completion``.
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

from financial_llm_models import (  # noqa: E402
    resolve_financial_label_model,
    resolve_scheduler_query_parser_model,
)
from mlx_chat_cli import run_diagnose  # noqa: E402
from mlx_scheduler_llm_api import (  # noqa: E402
    _resolve_scheduler_model,
    add_gateway_argparse,
    argparse_to_factory_kwargs,
    build_llm_gateway_handler,
    ensure_model_bundle_loaded,
    ensure_model_loaded,
)
from vllm_gateway_routing import (  # noqa: E402
    VllmGatewayContext,
    diagnose_vllm_metal_server,
    probe_vllm_route,
    vllm_route_from_env,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scheduler LLM gateway (vLLM primary; MLX optional).",
    )
    add_gateway_argparse(parser)
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help=(
            "Exit after checks: detailed vLLM Metal probe when VLLM_* URLs are set "
            "(unless --llm-backend mlx); otherwise MLX snapshot diagnostics."
        ),
    )
    parser.add_argument(
        "--preload-financial-label-model",
        default=None,
        metavar="PATH_OR_HUB",
        help="(MLX only) Preload second weights for financial labels.",
    )
    parser.add_argument(
        "--no-preload-financial-label-model",
        action="store_true",
        help="(MLX only) Do not preload label bundle.",
    )
    parser.add_argument(
        "--preload-query-parser-model",
        default=None,
        metavar="PATH_OR_HUB",
        help="(MLX only) Preload query-parser weights.",
    )
    parser.add_argument(
        "--no-preload-query-parser-model",
        action="store_true",
        help="(MLX only) Do not preload query-parser bundle.",
    )

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])
    vr = vllm_route_from_env()

    if ns.diagnose_only:
        if ns.llm_backend != "mlx" and vr is not None:
            import httpx

            with httpx.Client() as client:
                return diagnose_vllm_metal_server(client, vr)
        resolved = _resolve_scheduler_model(ns.model)
        return run_diagnose(resolved)

    want_vllm: bool
    if ns.llm_backend == "mlx":
        want_vllm = False
    elif ns.llm_backend == "vllm":
        want_vllm = True
        if vr is None:
            print(
                "error: --llm-backend vllm requires VLLM_14B_BASE_URL",
                file=sys.stderr,
            )
            return 2
    else:
        want_vllm = vr is not None

    http_client = None
    vctx: VllmGatewayContext | None = None
    use_vllm = False

    if want_vllm and vr is not None:
        import httpx

        http_client = httpx.Client()
        ok = probe_vllm_route(http_client, vr)
        if ok:
            use_vllm = True
            tok_id = _resolve_scheduler_model(ns.model)
            vctx = VllmGatewayContext(
                client=http_client,
                route=vr,
                tokenizer_model_id=tok_id,
            )
        elif ns.mlx_fallback:
            use_vllm = False
            if http_client is not None:
                http_client.close()
                http_client = None
            print(
                "warning: vLLM probe failed; falling back to MLX",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"error: vLLM unreachable for {vr.api_base!r}.",
                file=sys.stderr,
            )
            print(
                "  Start vLLM (example): vllm serve ~/models/Qwen3-14B --port 8000 "
                "--served-model-name Qwen3-14B",
                file=sys.stderr,
            )
            print(
                "  Or use MLX: unset VLLM_14B_BASE_URL, pass --llm-backend mlx, "
                "or set SCHEDULER_LLM_FALLBACK_MLX=1 (run-llm-gateway-local-models.sh does this).",
                file=sys.stderr,
            )
            print(
                "  Strict mode when vLLM is required: pass --no-mlx-fallback or "
                "SCHEDULER_LLM_FALLBACK_MLX=0.",
                file=sys.stderr,
            )
            if http_client is not None:
                http_client.close()
            return 2

    factory_kw = argparse_to_factory_kwargs(ns)
    factory_kw["vllm_context"] = vctx if use_vllm else None

    label_to_preload: str | None = None
    if not use_vllm:
        model_o, _, err = ensure_model_loaded(model=ns.model)
        if model_o is None:
            print(err, file=sys.stderr)
            if http_client is not None:
                http_client.close()
            return 2

        sched_resolved = _resolve_scheduler_model(ns.model)
        if not ns.no_preload_financial_label_model:
            if ns.preload_financial_label_model and str(ns.preload_financial_label_model).strip():
                label_to_preload = resolve_financial_label_model(
                    str(ns.preload_financial_label_model).strip()
                )
            elif os.environ.get("MLX_FINANCIAL_LABEL_MODEL", "").strip():
                label_to_preload = resolve_financial_label_model(None)
            elif (Path.home() / "models" / "Qwen3-8B").is_dir():
                label_to_preload = resolve_financial_label_model(None)
        if label_to_preload and label_to_preload.strip() != sched_resolved.strip():
            bundle, err_lb = ensure_model_bundle_loaded(model=label_to_preload)
            if bundle is None:
                print(err_lb, file=sys.stderr)
                if http_client is not None:
                    http_client.close()
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
                if http_client is not None:
                    http_client.close()
                return 2
    else:
        sched_resolved = _resolve_scheduler_model(ns.model)

    handler_cls = build_llm_gateway_handler(**factory_kw)
    httpd = ThreadingHTTPServer((ns.host, ns.port), handler_cls)

    api = f"http://{ns.host}:{ns.port}/"
    mode = "vLLM" if use_vllm else "MLX"
    print(f"Scheduler LLM gateway ({mode}) → {api}", file=sys.stderr, flush=True)
    print(f"  GET  {api}health", file=sys.stderr, flush=True)
    print(f"  POST {api}v1/day-scheduler/chat", file=sys.stderr, flush=True)
    print(f"  POST {api}v1/plain-completion", file=sys.stderr, flush=True)
    print("day-scheduler model:", sched_resolved, file=sys.stderr, flush=True)
    if use_vllm:
        assert vctx is not None
        vr_out = vctx.route
        print(
            f"  vLLM: {vr_out.api_base} model={vr_out.served_model_name}",
            file=sys.stderr,
            flush=True,
        )
        print(f"  tokenizer: {vctx.tokenizer_model_id}", file=sys.stderr, flush=True)
    elif not ns.no_preload_financial_label_model and label_to_preload:
        if label_to_preload.strip() != sched_resolved.strip():
            print(
                "financial label model (preloaded):",
                label_to_preload,
                file=sys.stderr,
                flush=True,
            )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nLLM gateway stopped.", file=sys.stderr)
    finally:
        httpd.server_close()
        if http_client is not None:
            http_client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
