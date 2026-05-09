"""Shared MLX inference HTTP handler for ``mlx_llm_gateway.py`` day-scheduler API.

Loads the model in-process (Metal). Stateless per request besides the model singleton.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import threading
from dataclasses import dataclass
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
    build_prompt,
    chat_text_for_ui,
    generate_day_scheduler_reply,
    load_day_scheduler_system_prompt,
)
from response_quality import (  # noqa: E402
    DEFAULT_EMBEDDING_MODEL_DIR,
    DEFAULT_SIMILARITY_THRESHOLD,
    embedding_similarity,
    parse_self_grade,
)
from schedule_parse import validate_schedule_response  # noqa: E402

DEFAULT_CHEAP_MODEL = str(Path.home() / "models" / "Qwen3-8B")
DEFAULT_EXPENSIVE_MODEL = str(Path.home() / "models" / "Qwen3-14B")
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="scheduler-llm")


@dataclass
class ModelBundle:
    model: object
    tokenizer: object
    model_str: str
    lock: threading.Lock


_registry_lock = threading.Lock()
_model_registry: dict[str, ModelBundle] = {}


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


def _resolve_role_model(model: str | None, *, role: str = "cheap") -> str:
    if model and model.strip():
        return model.strip()
    if role == "expensive":
        env = os.environ.get("SCHEDULER_EXPENSIVE_MODEL", "").strip()
        return env or DEFAULT_EXPENSIVE_MODEL
    env = os.environ.get("SCHEDULER_CHEAP_MODEL", "").strip()
    return env or resolve_model_arg(None)


def ensure_model_bundle_loaded(
    *, model: str | None, role: str = "cheap"
) -> tuple[ModelBundle, str] | tuple[None, str]:
    """Return a named model bundle or ``(None, errmsg)``."""
    model_str = _resolve_role_model(model, role=role)
    key = f"{role}:{model_str}"
    with _registry_lock:
        existing = _model_registry.get(key)
        if existing is not None:
            return existing, model_str

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
        bundle = ModelBundle(model_m, tokenizer, model_str, threading.Lock())
        _model_registry[key] = bundle
        return bundle, model_str


def ensure_model_loaded(
    *, model: str | None
) -> tuple[object, object, str] | tuple[None, None, str]:
    """Backward-compatible cheap/default loader."""
    bundle, model_str_or_err = ensure_model_bundle_loaded(model=model, role="cheap")
    if bundle is None:
        return None, None, model_str_or_err
    return bundle.model, bundle.tokenizer, model_str_or_err


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
    cheap_model: str | None,
    expensive_model: str | None,
    embedding_model: str | None,
    similarity_threshold: float,
    background_reference: bool,
    self_grade_threshold: float,
):
    summarize_keep_recent_eff = max(2, summarize_keep_recent)
    auto_summarize = not no_summarize
    max_tokens_eff = max(4096, int(max_tokens_override or 4096))
    env_hist = os.environ.get("MLX_DAY_SCHEDULER_UI_MAX_HISTORY", "0").strip()
    max_hist = max_history_override if max_history_override is not None else int(env_hist or "0")

    def _make_sampler():
        temp, top_p, top_k = _finalize_sampling_defaults_ui(
            temperature=temperature_override if temperature_override is not None else None,
            top_p=top_p_override if top_p_override is not None else None,
            top_k=top_k_override if top_k_override is not None else None,
            thinking=template_enable_thinking is True,
        )
        from mlx_lm.sample_utils import make_sampler as _mk

        return _mk(temp=temp, top_p=top_p, min_p=min_p, top_k=top_k)

    def _gen_kw(sampler_mod: object) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sampler": sampler_mod,
            "prefill_step_size": prefill_step_size,
        }
        if kv_bits > 0:
            out["kv_bits"] = kv_bits
            out["kv_group_size"] = kv_group_size
            out["quantized_kv_start"] = quantized_kv_start
        if max_kv_size is not None:
            out["max_kv_size"] = max_kv_size
        return out

    def _run_scheduler_generation(
        *,
        role: str,
        model_arg: str | None,
        content: str,
        pairs: list[tuple[str, str]],
        host_context: str | None,
        client_clock_minutes: int | None,
        client_clock_date: date | None,
        strip_reasoning: bool,
        schedule_buffer: bool,
        sampler_mod: object,
        stream_callbacks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        bundle, model_str_or_err = ensure_model_bundle_loaded(model=model_arg, role=role)
        if bundle is None:
            return {
                "ok": False,
                "assistant": "",
                "history": pairs,
                "error": model_str_or_err,
                "model": model_arg or "",
                "role": role,
            }
        ctx_limit = resolve_context_token_limit(
            model_str=bundle.model_str,
            tokenizer=bundle.tokenizer,
            explicit=context_limit_explicit,
        )
        hist = [(r, t) for r, t in pairs]
        callbacks = stream_callbacks or {}
        with bundle.lock:
            ok, assistant_text, _last_resp = generate_day_scheduler_reply(
                user_raw=content.strip(),
                history=hist,
                model_m=bundle.model,
                tokenizer=bundle.tokenizer,
                base_system_prompt=base_system,
                template_enable_thinking=template_enable_thinking,
                context_limit=ctx_limit,
                soft_fraction=min(0.92, max(0.25, float(context_soft_fraction))),
                reserve_tokens=max_tokens_eff + 512,
                keep_recent_messages=summarize_keep_recent_eff,
                summarize_max_tokens=summarize_max_tokens,
                max_summarize_input_tokens=summarize_max_input_tokens,
                gen_kw=_gen_kw(sampler_mod),
                auto_summarize=auto_summarize,
                max_tokens=max_tokens_eff,
                strip_reasoning=strip_reasoning,
                buffer_full_reply=schedule_buffer,
                max_history_messages=max_hist,
                host_context=host_context,
                client_clock_minutes=client_clock_minutes,
                client_clock_date=client_clock_date,
                on_stream_chunk=callbacks.get("chunk"),
                on_stream_thinking=callbacks.get("thinking"),
                on_thinking_closed=callbacks.get("thinking_closed"),
                hide_schedule_deltas=True,
                on_compress=None,
            )
        return {
            "ok": ok,
            "assistant": assistant_text,
            "history": hist,
            "error": None if ok else "MLX generation failed (see gateway terminal).",
            "model": bundle.model_str,
            "role": role,
        }

    def _self_grade_candidate(
        *,
        bundle: ModelBundle,
        user_content: str,
        host_context: str | None,
        candidate: str,
        sampler_mod: object,
    ) -> tuple[bool, list[str]]:
        prompt_user = (
            "Grade this day-scheduler response for correctness. Check only format, chronology, "
            "whether it follows hard facts/context, and whether the plan is sensible.\n\n"
            f"USER:\n{user_content}\n\n"
            f"HOST CONTEXT:\n{host_context or '(none)'}\n\n"
            f"CANDIDATE:\n{candidate}\n\n"
            'Return strict JSON only: {"pass": true, "score": 0.0, "reasons": []}'
        )
        grader_system = "You are a strict JSON-only validator for day-scheduler responses."
        try:
            from mlx_day_scheduler_pipeline import _generate_plain_completion

            prompt = build_prompt(
                bundle.tokenizer,
                grader_system,
                [("user", prompt_user)],
                enable_thinking=False,
            )
            with bundle.lock:
                raw = _generate_plain_completion(
                    model_m=bundle.model,
                    tokenizer=bundle.tokenizer,
                    prompt=prompt,
                    max_tokens=256,
                    gen_kw=_gen_kw(sampler_mod),
                )
        except Exception as exc:
            return False, [f"self-grade failed: {exc}"]
        grade = parse_self_grade(raw, min_score=self_grade_threshold)
        return grade.passed, list(grade.reasons)

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
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "mlx-scheduler-llm",
                    "model_loaded": bool(_model_registry),
                    "model": _resolve_role_model(cheap_model, role="cheap"),
                    "cheap_model": _resolve_role_model(cheap_model, role="cheap"),
                    "expensive_model": _resolve_role_model(expensive_model, role="expensive"),
                    "loaded_roles": sorted(_model_registry.keys()),
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

            strip_reasoning = _should_strip_reasoning_ui(force_strip=strip_arg)
            try:
                sampler_mod = _make_sampler()
            except Exception as e:
                self._send_json_error(500, f"sampler failed: {e}")
                return

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

            cheap_model_arg = (
                payload.get("model")
                if isinstance(payload.get("model"), str)
                else cheap_model
            )
            expensive_model_arg = (
                payload.get("expensive_model")
                if isinstance(payload.get("expensive_model"), str)
                else expensive_model
            )

            cheap_bundle, cheap_model_or_err = ensure_model_bundle_loaded(
                model=cheap_model_arg,
                role="cheap",
            )
            if cheap_bundle is None:
                body_err = json.dumps(
                    {"type": "done", "ok": False, "error": cheap_model_or_err}
                ).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Length", str(len(body_err)))
                self.end_headers()
                self.wfile.write(body_err)
                self.wfile.write(b"\n")
                return

            expensive_future = None
            if background_reference:
                expensive_future = _executor.submit(
                    _run_scheduler_generation,
                    role="expensive",
                    model_arg=expensive_model_arg,
                    content=content,
                    pairs=hist,
                    host_context=host_context,
                    client_clock_minutes=client_clock_minutes,
                    client_clock_date=client_clock_date,
                    strip_reasoning=strip_reasoning,
                    schedule_buffer=True,
                    sampler_mod=sampler_mod,
                    stream_callbacks=None,
                )

            cheap_result = _run_scheduler_generation(
                role="cheap",
                model_arg=cheap_model_arg,
                content=content,
                pairs=hist,
                host_context=host_context,
                client_clock_minutes=client_clock_minutes,
                client_clock_date=client_clock_date,
                strip_reasoning=strip_reasoning,
                schedule_buffer=schedule_buffer,
                sampler_mod=sampler_mod,
                stream_callbacks={
                    "chunk": on_chunk,
                    "thinking": stream_thinking_cb,
                    "thinking_closed": thinking_done_cb,
                },
            )

            validation = validate_schedule_response(
                str(cheap_result.get("assistant") or ""),
                default_plan_date=client_clock_date.isoformat()
                if client_clock_date is not None
                else date.today().isoformat(),
                client_minute_of_day=client_clock_minutes,
                host_context=host_context,
            )
            cheap_ok = bool(cheap_result.get("ok")) and validation.ok
            cheap_reasons = list(validation.reasons)
            if cheap_ok:
                graded_ok, grade_reasons = _self_grade_candidate(
                    bundle=cheap_bundle,
                    user_content=content.strip(),
                    host_context=host_context,
                    candidate=str(cheap_result.get("assistant") or ""),
                    sampler_mod=sampler_mod,
                )
                cheap_ok = graded_ok
                cheap_reasons.extend(grade_reasons)

            chosen = cheap_result
            if not cheap_ok:
                if expensive_future is not None:
                    chosen = expensive_future.result()
                else:
                    chosen = _run_scheduler_generation(
                        role="expensive",
                        model_arg=expensive_model_arg,
                        content=content,
                        pairs=hist,
                        host_context=host_context,
                        client_clock_minutes=client_clock_minutes,
                        client_clock_date=client_clock_date,
                        strip_reasoning=strip_reasoning,
                        schedule_buffer=True,
                        sampler_mod=sampler_mod,
                        stream_callbacks=None,
                    )

            assistant_text = str(chosen.get("assistant") or "")
            ok = bool(chosen.get("ok"))
            chat_line = (
                chat_text_for_ui(assistant_full=assistant_text, user_raw=content.strip())
                if ok
                else ""
            )

            serializable = [
                {"role": r, "content": c} for r, c in cast(list[tuple[str, str]], chosen["history"])
            ]
            write_line(
                {
                    "type": "done",
                    "ok": ok,
                    "assistant": assistant_text,
                    "chat": chat_line,
                    "history": serializable,
                    "model_role": chosen.get("role"),
                    "model": chosen.get("model"),
                    "fast_validation_reasons": cheap_reasons,
                    "error": None if ok else chosen.get("error"),
                }
            )

            if (
                ok
                and cheap_ok
                and expensive_future is not None
                and chosen.get("role") == "cheap"
            ):
                try:
                    expensive_result = expensive_future.result()
                    expensive_text = str(expensive_result.get("assistant") or "")
                    if expensive_result.get("ok") and expensive_text.strip():
                        sim = embedding_similarity(
                            assistant_text,
                            expensive_text,
                            model_path=embedding_model,
                        )
                        if sim < similarity_threshold:
                            replacement_chat = chat_text_for_ui(
                                assistant_full=expensive_text,
                                user_raw=content.strip(),
                            )
                            replacement_hist = [
                                {"role": r, "content": c}
                                for r, c in cast(
                                    list[tuple[str, str]],
                                    expensive_result["history"],
                                )
                            ]
                            write_line(
                                {
                                    "type": "replacement",
                                    "ok": True,
                                    "assistant": expensive_text,
                                    "chat": replacement_chat,
                                    "history": replacement_hist,
                                    "model_role": "expensive",
                                    "model": expensive_result.get("model"),
                                    "similarity": sim,
                                    "reason": (
                                        f"semantic similarity {sim:.3f} below "
                                        f"{similarity_threshold:.3f}"
                                    ),
                                }
                            )
                        else:
                            write_line(
                                {
                                    "type": "comparison",
                                    "ok": True,
                                    "similarity": sim,
                                    "kept": "cheap",
                                }
                            )
                except Exception as exc:
                    write_line(
                        {
                            "type": "comparison",
                            "ok": False,
                            "error": str(exc),
                        }
                    )

    return SchedulerLlmGatewayHandler


def add_gateway_argparse(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        help="cheap/default mlx-lm model path or Hub id (env MLX_MODEL or ~/models/Qwen3-8B)",
    )
    parser.add_argument(
        "--cheap-model",
        default=None,
        help="Fast model path/Hub id (default: ~/models/Qwen3-8B).",
    )
    parser.add_argument(
        "--expensive-model",
        default=None,
        help="Fallback/reference model path/Hub id (default: ~/models/Qwen3-14B).",
    )
    parser.add_argument(
        "--embedding-model",
        default=None,
        help="Embedding model path/Hub id (default: ~/models/Qwen3-Embedding-4B).",
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=float(
            os.environ.get("SCHEDULER_SIMILARITY_THRESHOLD", DEFAULT_SIMILARITY_THRESHOLD)
        ),
    )
    parser.add_argument(
        "--self-grade-threshold",
        type=float,
        default=float(os.environ.get("SCHEDULER_SELF_GRADE_THRESHOLD", "0.80")),
    )
    parser.add_argument(
        "--no-background-reference",
        action="store_true",
        help="Disable Qwen3-14B background comparison/replacement flow.",
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
        cheap_model=ns.cheap_model or ns.model,
        expensive_model=ns.expensive_model,
        embedding_model=ns.embedding_model or str(DEFAULT_EMBEDDING_MODEL_DIR),
        similarity_threshold=ns.similarity_threshold,
        background_reference=not ns.no_background_reference,
        self_grade_threshold=ns.self_grade_threshold,
    )


__all__ = [
    "REPO_ROOT",
    "build_llm_gateway_handler",
    "ensure_model_loaded",
    "add_gateway_argparse",
    "argparse_to_factory_kwargs",
    "load_day_scheduler_system_prompt",
]
