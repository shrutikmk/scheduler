"""Shared MLX inference HTTP handler for ``mlx_llm_gateway.py`` day-scheduler API.

Loads the model in-process (Metal). Stateless per request besides the model singleton.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast

SAMPLES_ROOT = Path(__file__).resolve().parent
if str(SAMPLES_ROOT) not in sys.path:
    sys.path.insert(0, str(SAMPLES_ROOT))

# pylint: disable=wrong-import-position
from mlx_chat_cli import (  # noqa: E402
    DEFAULT_HUB_REPO,
    diagnose_local_snapshot,
    is_local_dir,
    resolve_context_token_limit,
    resolve_model_arg,
)
from mlx_day_scheduler_pipeline import (  # noqa: E402
    REPO_ROOT,
    chat_text_for_ui,
    generate_day_scheduler_reply,
    load_day_scheduler_system_prompt,
)

_generation_lock = threading.Lock()
_model_bundle: tuple[object, object] | None = None
_bundle_model_str: str | None = None


def _should_strip_reasoning_ui(*, force_strip: bool) -> bool:
    if force_strip:
        return True
    raw = os.environ.get("MLX_STRIP_THINKING")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() not in ("0", "false", "no", "off")
    return False


def _resolve_template_enable_thinking_ui(no_day_scheduler_thinking: bool) -> bool | None:
    if os.environ.get("MLX_ENABLE_THINKING", "").strip().lower() in ("1", "true", "yes"):
        return True
    if no_day_scheduler_thinking or os.environ.get(
        "MLX_DAY_SCHEDULER_NO_THINKING", ""
    ).strip().lower() in ("1", "true", "yes"):
        return False
    return True


def _finalize_sampling_defaults_ui(
    *,
    temperature: float | None,
    top_p: float | None,
    top_k: int | None,
    thinking: bool,
) -> tuple[float, float, int]:
    if temperature is None:
        temperature = 0.6 if thinking else 0.7
    if top_p is None:
        top_p = 0.95 if thinking else 0.8
    if top_k is None:
        top_k = 20
    return temperature, top_p, top_k


def ensure_model_loaded(
    *, model: str | None
) -> tuple[object, object, str] | tuple[None, None, str]:
    """Return ``(model_m, tokenizer, resolved_path)`` or ``(None, None, errmsg)``."""
    global _model_bundle, _bundle_model_str
    model_str = resolve_model_arg(model)
    with _generation_lock:
        if _model_bundle is not None and _bundle_model_str == model_str:
            return _model_bundle[0], _model_bundle[1], model_str

        try:
            import mlx.core as mx
            from mlx_lm import load
        except ImportError as e:
            return (
                None,
                None,
                f"mlx import failed ({e}); run: uv sync --group samples-mlx",
            )

        if not mx.metal.is_available():
            return (
                None,
                None,
                "MLX Metal unavailable (sandbox or unsupported host)",
            )

        if is_local_dir(model_str):
            issues = diagnose_local_snapshot(Path(model_str).expanduser().resolve())
            hard_block = [
                i
                for i in issues
                if i.startswith("Missing") or "No tokenizer" in i or "No *.safetensors" in i
            ]
            if hard_block:
                return (
                    None,
                    None,
                    "local model invalid: " + "; ".join(issues[:6]),
                )

        print(f"[mlx_llm_gateway] Loading {model_str!r} …", file=sys.stderr, flush=True)
        try:
            model_m, tokenizer = load(
                model_str,
                tokenizer_config={"trust_remote_code": True},
            )
        except Exception as e:
            return (None, None, f"load failed: {e}")

        mx.clear_cache()
        _model_bundle = (model_m, tokenizer)
        _bundle_model_str = model_str
        return model_m, tokenizer, model_str


def build_llm_gateway_handler(
    *,
    base_system: str,
    template_enable_thinking: bool | None,
    strip_arg: bool,
    no_summarize: bool,
    summarize_keep_recent: int,
    summarize_max_tokens: int,
    summarize_max_input_tokens: int,
    context_soft_fraction: float,
    context_limit_explicit: int | None,
    prefill_step_size: int,
    kv_bits: int,
    kv_group_size: int,
    quantized_kv_start: int,
    max_kv_size: int | None,
    temperature_override: float | None,
    top_p_override: float | None,
    top_k_override: int | None,
    min_p: float,
    max_tokens_override: int | None,
    max_history_override: int | None,
):
    summarize_keep_recent_eff = max(2, summarize_keep_recent)
    auto_summarize = not no_summarize
    max_tokens_eff = max(4096, int(max_tokens_override or 4096))
    env_hist = os.environ.get("MLX_DAY_SCHEDULER_UI_MAX_HISTORY", "0").strip()
    max_hist = max_history_override if max_history_override is not None else int(env_hist or "0")

    class SchedulerLlmGatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            line = fmt % args if args else fmt
            print(
                f"[{self.log_date_time_string()}] [llm] {line}",
                file=sys.stderr,
                flush=True,
            )

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json_error(self, code: int, message: str) -> None:
            self._send_json(code, {"error": message})

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/health":
                self.send_error(404, "Not found")
                return
            loaded = _model_bundle is not None and _bundle_model_str is not None
            model_resolved = resolve_model_arg(None)
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "mlx-scheduler-llm",
                    "model_loaded": loaded,
                    "model": model_resolved,
                    "default_hub_repo": DEFAULT_HUB_REPO,
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/chat", "/v1/day-scheduler/chat"):
                self._send_json_error(404, "Not found")
                return

            ln = self.headers.get("Content-Length")
            try:
                n = int(ln or "0")
            except ValueError:
                self._send_json_error(400, "Bad Content-Length")
                return
            body_raw = self.rfile.read(max(0, min(n, 4_000_000)))
            try:
                payload = json.loads(body_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json_error(400, "Invalid JSON body")
                return

            raw_hist = payload.get("history")
            if raw_hist is None:
                raw_hist = payload.get("messages")
            if raw_hist is None:
                raw_hist = []
            if not isinstance(raw_hist, list):
                self._send_json_error(
                    400,
                    "`history` must be an array (or omit for []).",
                )
                return
            history_j = raw_hist

            content = payload.get("content") or payload.get("text") or ""
            if not isinstance(content, str):
                self._send_json_error(400, "`content` must be a string.")
                return

            pairs: list[tuple[str, str]] = []
            for item in history_j:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    role, txt = item[0], item[1]
                elif isinstance(item, dict) and "role" in item and "content" in item:
                    role, txt = item["role"], item["content"]
                else:
                    continue
                if role not in ("user", "assistant") or not isinstance(txt, str):
                    continue
                pairs.append((str(role), txt))

            model_o, tok_o, model_str_or_err = ensure_model_loaded(model=payload.get("model"))
            if model_o is None:
                msg = cast(str, model_str_or_err)
                body_err = json.dumps({"type": "done", "ok": False, "error": msg}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Length", str(len(body_err)))
                self.end_headers()
                self.wfile.write(body_err)
                self.wfile.write(b"\n")
                return

            model_str = cast(str, model_str_or_err)
            model_m, tokenizer = cast(object, model_o), cast(object, tok_o)
            mt = max_tokens_eff
            reserve_tokens = int(payload.get("context_reserve_tokens") or (mt + 512))
            strip_reasoning = _should_strip_reasoning_ui(force_strip=strip_arg)

            temp, top_p, top_k = _finalize_sampling_defaults_ui(
                temperature=temperature_override if temperature_override is not None else None,
                top_p=top_p_override if top_p_override is not None else None,
                top_k=top_k_override if top_k_override is not None else None,
                thinking=template_enable_thinking is True,
            )

            sampler_mod = None
            try:
                from mlx_lm.sample_utils import make_sampler as _mk

                sampler_mod = _mk(
                    temp=temp,
                    top_p=top_p,
                    min_p=min_p,
                    top_k=top_k,
                )
            except Exception as e:
                self._send_json_error(500, f"sampler failed: {e}")
                return

            gen_kw: dict[str, Any] = {
                "sampler": sampler_mod,
                "prefill_step_size": prefill_step_size,
            }
            if kv_bits > 0:
                gen_kw["kv_bits"] = kv_bits
                gen_kw["kv_group_size"] = kv_group_size
                gen_kw["quantized_kv_start"] = quantized_kv_start
            if max_kv_size is not None:
                gen_kw["max_kv_size"] = max_kv_size

            ctx_limit = resolve_context_token_limit(
                model_str=model_str,
                tokenizer=tokenizer,
                explicit=context_limit_explicit,
            )

            hist = [(r, t) for r, t in pairs]

            habits_raw = payload.get("habits_block")
            host_ctx = (
                habits_raw.strip()
                if isinstance(habits_raw, str) and habits_raw.strip()
                else None
            )

            extra_ctx = payload.get("persisted_tasks_context")
            if isinstance(extra_ctx, str) and extra_ctx.strip():
                persist = extra_ctx.strip()
                host_ctx = f"{persist}\n\n{host_ctx}" if host_ctx else persist

            host_context = host_ctx

            client_clock_minutes: int | None = None
            client_clock_date: date | None = None
            cc = payload.get("client_calendar")
            if isinstance(cc, dict):
                d_raw = cc.get("date_iso")
                mod_raw = cc.get("minute_of_day")
                try:
                    if isinstance(d_raw, str) and isinstance(mod_raw, int):
                        client_clock_date = date.fromisoformat(d_raw)
                        client_clock_minutes = int(mod_raw) % (24 * 60)
                except ValueError:
                    client_clock_date = None
                    client_clock_minutes = None

            schedule_buffer = payload.get("buffer") is True or os.environ.get(
                "MLX_DAY_SCHEDULER_BUFFER", ""
            ).strip().lower() in ("1", "true", "yes") or os.environ.get(
                "MLX_DAY_SCHEDULER_STREAM", ""
            ).strip().lower() in ("0", "false", "no", "off")

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            def write_line(obj: dict) -> None:
                ln = json.dumps(obj, ensure_ascii=False) + "\n"
                self.wfile.write(ln.encode("utf-8"))
                try:
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def on_thinking_closed() -> None:
                write_line({"type": "thinking_end"})

            def on_thinking_chunk(t: str) -> None:
                write_line({"type": "delta", "phase": "thinking", "text": t})

            def on_chunk(chunk: str) -> None:
                write_line({"type": "delta", "text": chunk})

            stream_thinking_cb = None if strip_reasoning else on_thinking_chunk
            thinking_done_cb = on_thinking_closed if stream_thinking_cb else None

            with _generation_lock:
                ok, assistant_text, _last_resp = generate_day_scheduler_reply(
                    user_raw=content.strip(),
                    history=hist,
                    model_m=model_m,
                    tokenizer=tokenizer,
                    base_system_prompt=base_system,
                    template_enable_thinking=template_enable_thinking,
                    context_limit=ctx_limit,
                    soft_fraction=min(
                        0.92,
                        max(0.25, float(context_soft_fraction)),
                    ),
                    reserve_tokens=reserve_tokens,
                    keep_recent_messages=summarize_keep_recent_eff,
                    summarize_max_tokens=summarize_max_tokens,
                    max_summarize_input_tokens=summarize_max_input_tokens,
                    gen_kw=gen_kw,
                    auto_summarize=auto_summarize,
                    max_tokens=mt,
                    strip_reasoning=strip_reasoning,
                    buffer_full_reply=schedule_buffer,
                    max_history_messages=max_hist,
                    host_context=host_context,
                    client_clock_minutes=client_clock_minutes,
                    client_clock_date=client_clock_date,
                    on_stream_chunk=on_chunk,
                    on_stream_thinking=stream_thinking_cb,
                    on_thinking_closed=thinking_done_cb,
                    hide_schedule_deltas=True,
                    on_compress=None,
                )

            chat_line = (
                chat_text_for_ui(assistant_full=assistant_text, user_raw=content.strip())
                if ok
                else ""
            )

            serializable = [{"role": r, "content": c} for r, c in hist]
            write_line(
                {
                    "type": "done",
                    "ok": ok,
                    "assistant": assistant_text,
                    "chat": chat_line,
                    "history": serializable,
                    "error": None if ok else "MLX generation failed (see gateway terminal).",
                }
            )

    return SchedulerLlmGatewayHandler


def add_gateway_argparse(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        help="mlx-lm model path or Hub id (env MLX_MODEL or ~/models/Qwen3-14B)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--system",
        default=None,
        help="Override system prompt (default: prompts/day-scheduler-system.md)",
    )
    parser.add_argument("--strip-thinking", action="store_true")
    parser.add_argument("--no-day-scheduler-thinking", action="store_true")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", dest="top_p", type=float, default=None)
    parser.add_argument("--top-k", dest="top_k", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument(
        "--prefill-step-size",
        type=int,
        default=int(os.environ.get("MLX_PREFILL_STEP", "4096") or "4096"),
    )
    parser.add_argument("--kv-bits", type=int, default=8)
    parser.add_argument("--kv-group-size", type=int, default=64)
    parser.add_argument("--quantized-kv-start", type=int, default=0)
    parser.add_argument("--max-kv-size", type=int, default=None)
    parser.add_argument("--context-limit", type=int, default=None)
    parser.add_argument("--context-soft-fraction", type=float, default=0.72)
    parser.add_argument("--summarize-keep-recent", type=int, default=12)
    parser.add_argument("--summarize-max-tokens", type=int, default=384)
    parser.add_argument("--summarize-max-input-tokens", type=int, default=6144)
    parser.add_argument("--no-auto-summarize", action="store_true")
    parser.add_argument("--max-history-messages", type=int, default=0)


def argparse_to_factory_kwargs(ns: argparse.Namespace):
    raw_sys = ns.system
    if raw_sys is None or (isinstance(raw_sys, str) and not raw_sys.strip()):
        base_system = load_day_scheduler_system_prompt(REPO_ROOT)
    else:
        base_system = raw_sys.strip()
    template_et = _resolve_template_enable_thinking_ui(
        no_day_scheduler_thinking=ns.no_day_scheduler_thinking,
    )
    return dict(
        base_system=base_system,
        template_enable_thinking=template_et,
        strip_arg=ns.strip_thinking,
        no_summarize=ns.no_auto_summarize,
        summarize_keep_recent=ns.summarize_keep_recent,
        summarize_max_tokens=ns.summarize_max_tokens,
        summarize_max_input_tokens=ns.summarize_max_input_tokens,
        context_soft_fraction=ns.context_soft_fraction,
        context_limit_explicit=ns.context_limit,
        prefill_step_size=ns.prefill_step_size,
        kv_bits=ns.kv_bits,
        kv_group_size=ns.kv_group_size,
        quantized_kv_start=ns.quantized_kv_start,
        max_kv_size=ns.max_kv_size,
        temperature_override=ns.temperature,
        top_p_override=ns.top_p,
        top_k_override=ns.top_k,
        min_p=float(os.environ.get("MLX_MIN_P", "0.0") or "0.0"),
        max_tokens_override=ns.max_tokens,
        max_history_override=ns.max_history_messages,
    )


__all__ = [
    "REPO_ROOT",
    "build_llm_gateway_handler",
    "ensure_model_loaded",
    "add_gateway_argparse",
    "argparse_to_factory_kwargs",
    "load_day_scheduler_system_prompt",
]
