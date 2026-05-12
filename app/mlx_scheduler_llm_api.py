"""HTTP handler for the scheduler LLM gateway: **vLLM** (OpenAI API) or **MLX** in-process.

Stateless per request besides loaded model bundles (MLX) or shared ``httpx`` client (vLLM).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, cast

from http_access_rollup import AccessLogRollup

APP_ROOT = Path(__file__).resolve().parent
SAMPLES_ROOT = APP_ROOT.parent / "samples"
for _p in (APP_ROOT, SAMPLES_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# pylint: disable=wrong-import-position
from day_scheduler_query_parser import (  # noqa: E402
    QUERY_PARSER_SYSTEM,
    ParsedQuery,
    build_query_parser_user_block,
    format_query_parser_host_facts,
    parse_query_parser_completion_text,
    parsed_query_to_meta,
    resolve_import_default_plan_date,
    strip_redacted_thinking,
)
from financial_llm_models import resolve_scheduler_query_parser_model  # noqa: E402
from mlx_chat_cli import (  # noqa: E402  (samples/ baseline; kept on sys.path above)
    DEFAULT_HUB_REPO,
    diagnose_local_snapshot,
    is_local_dir,
    resolve_context_token_limit,
)
from mlx_day_scheduler_pipeline import (  # noqa: E402
    REPO_ROOT,
    _generate_plain_completion,
    build_prompt,
    chat_text_for_ui,
    generate_day_scheduler_reply,
    generate_day_scheduler_reply_vllm,
    load_day_scheduler_system_prompt,
)
from response_quality import parse_self_grade  # noqa: E402
from schedule_parse import (  # noqa: E402
    normalize_schedule_bullets_for_parser,
    validate_schedule_response,
)
from vllm_gateway_routing import (  # noqa: E402
    VllmGatewayContext,
    probe_vllm_route,
    resolve_plain_completion_route,
)
from vllm_openai_client import chat_completion_text  # noqa: E402

DEFAULT_SCHEDULER_MODEL = str(Path.home() / "models" / "Qwen3-14B")

_FORMAT_REPAIR_SYSTEM = (
    "You repair day-scheduler assistant replies. Output ONLY the final user-visible reply: "
    "a Unicode TO DO banner, then timetable task lines, then exactly two short sentences. "
    "No scratchpad or thinking tags. Keep the user's intent; fix formatting to match all rules."
)


def _only_format_repairable_validation_reasons(reasons: tuple[str, ...]) -> bool:
    """True if every failure is something a formatting-only repair pass might fix."""
    if not reasons:
        return False
    allowed_prefixes = (
        "invalid task bullet format:",
        "invalid duration:",
        "no valid parsed task bullets",
        "tasks overlap on",
    )
    return all(any(r.startswith(p) for p in allowed_prefixes) for r in reasons)


@dataclass
class ModelBundle:
    model: object
    tokenizer: object
    model_str: str
    lock: threading.Lock


_registry_lock = threading.Lock()
_model_registry: dict[str, ModelBundle] = {}

_LLM_HTTP_ACCESS = AccessLogRollup(
    stderr_tag="llm",
    env_seconds_key="MLX_LLM_GATEWAY_ACCESS_LOG_STACK_SEC",
)


def _model_short(model_str: str | None) -> str:
    """Readable model label for logs (basename for local dirs, else full id)."""
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


def _flow_wall_timestamp() -> str:
    """Match BaseHTTPRequestHandler.log_date_time_string() style."""
    return time.strftime("%d/%b/%Y %H:%M:%S", time.localtime())


def _flow_log(
    flow_id: str,
    message: str,
    *,
    lane: str = "pipeline",
    role: str | None = None,
    model: str | None = None,
    mlx: str | None = None,
) -> None:
    """Human-oriented scheduler flow diagnostics (stderr)."""
    ts = _flow_wall_timestamp()
    bits: list[str] = []
    if lane.strip():
        bits.append(lane.strip().upper())
    if role:
        bits.append(f"role={role}")
    if mlx:
        bits.append(f"mlx={mlx}")
    if model:
        bits.append(f"model={_model_short(model)}")
    head = (" ".join(bits) + " │ ") if bits else ""
    print(
        f"[{ts}] [mlx_scheduler_flow] [{flow_id}] {head}{message}",
        file=sys.stderr,
        flush=True,
    )


def _clip(s: str, max_len: int = 140) -> str:
    one = " ".join(s.split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1] + "…"


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


def _resolve_scheduler_model(model: str | None) -> str:
    """Default day-scheduler weights: Qwen3-14B under ``~/models`` (override with env or CLI)."""
    if model and model.strip():
        return model.strip()
    for env in ("SCHEDULER_MODEL", "SCHEDULER_EXPENSIVE_MODEL", "MLX_MODEL"):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    return DEFAULT_SCHEDULER_MODEL


def loaded_model_bundle_paths() -> list[str]:
    """Snapshot of ``model_str`` for each bundle in the gateway registry (sorted, de-duplicated)."""

    return sorted({b.model_str for b in _model_registry.values()})


def ensure_model_bundle_loaded(
    *, model: str | None
) -> tuple[ModelBundle, str] | tuple[None, str]:
    """Return the scheduler MLX bundle or ``(None, errmsg)``."""
    model_str = _resolve_scheduler_model(model)
    key = f"scheduler:{model_str}"
    with _registry_lock:
        existing = _model_registry.get(key)
        if existing is not None:
            return existing, model_str

        try:
            import mlx.core as mx
            from mlx_lm import load
        except ImportError as e:
            return (None, f"mlx import failed ({e}); run: uv sync --group samples-mlx")

        if not mx.metal.is_available():
            return (None, "MLX Metal unavailable (sandbox or unsupported host)")

        if is_local_dir(model_str):
            issues = diagnose_local_snapshot(Path(model_str).expanduser().resolve())
            hard_block = [
                i
                for i in issues
                if i.startswith("Missing") or "No tokenizer" in i or "No *.safetensors" in i
            ]
            if hard_block:
                return (None, "local model invalid: " + "; ".join(issues[:6]))

        print(f"[mlx_llm_gateway] Loading {model_str!r} …", file=sys.stderr, flush=True)
        try:
            model_m, tokenizer = load(
                model_str,
                tokenizer_config={"trust_remote_code": True},
            )
        except Exception as e:
            return (None, f"load failed: {e}")

        mx.clear_cache()
        bundle = ModelBundle(model_m, tokenizer, model_str, threading.Lock())
        _model_registry[key] = bundle
        return bundle, model_str


def ensure_model_loaded(
    *, model: str | None
) -> tuple[object, object, str] | tuple[None, None, str]:
    """Load the single scheduler model (default Qwen3-14B)."""
    bundle, model_str_or_err = ensure_model_bundle_loaded(model=model)
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
    scheduler_model: str | None,
    self_grade_threshold: float,
    enable_self_grade: bool,
    vllm_context: VllmGatewayContext | None = None,
):
    summarize_keep_recent_eff = max(2, summarize_keep_recent)
    auto_summarize = not no_summarize
    max_tokens_eff = max(4096, int(max_tokens_override or 4096))
    env_hist = os.environ.get("MLX_DAY_SCHEDULER_UI_MAX_HISTORY", "0").strip()
    max_hist = max_history_override if max_history_override is not None else int(env_hist or "0")

    vllm_tok: Any | None = None
    if vllm_context is not None:
        from scheduler_tokenizer import load_tokenizer_only

        vllm_tok = load_tokenizer_only(vllm_context.tokenizer_model_id)

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
        client_timezone_iana: str | None,
        strip_reasoning: bool,
        schedule_buffer: bool,
        sampler_mod: object,
        stream_callbacks: dict[str, Any] | None = None,
        flow_id: str,
    ) -> dict[str, Any]:
        streaming = stream_callbacks is not None and (
            stream_callbacks.get("chunk") is not None
            or stream_callbacks.get("thinking") is not None
        )
        _flow_log(
            flow_id,
            f"[{role}] generate start requested — model_arg={model_arg!r} buffer={schedule_buffer} "
            f"stream_to_client={streaming} strip_reasoning={strip_reasoning}",
            lane="mlx",
            role=role,
            model=_resolve_scheduler_model(model_arg),
            mlx="to_model",
        )
        t0 = time.perf_counter()
        if vllm_context is not None and vllm_tok is not None:
            model_str_resolved = _resolve_scheduler_model(model_arg)
            ctx_limit = resolve_context_token_limit(
                model_str=vllm_context.tokenizer_model_id,
                tokenizer=vllm_tok,
                explicit=context_limit_explicit,
            )
            hist = [(r, t) for r, t in pairs]
            callbacks = stream_callbacks or {}
            temp, top_p, _top_k = _finalize_sampling_defaults_ui(
                temperature=temperature_override if temperature_override is not None else None,
                top_p=top_p_override if top_p_override is not None else None,
                top_k=top_k_override if top_k_override is not None else None,
                thinking=template_enable_thinking is True,
            )
            sum_temp, sum_tp, _ = _finalize_sampling_defaults_ui(
                temperature=None,
                top_p=None,
                top_k=None,
                thinking=False,
            )
            ok, assistant_text, _last = generate_day_scheduler_reply_vllm(
                user_raw=content.strip(),
                history=hist,
                client=vllm_context.client,
                route=vllm_context.route,
                tokenizer=vllm_tok,
                base_system_prompt=base_system,
                template_enable_thinking=template_enable_thinking,
                context_limit=ctx_limit,
                soft_fraction=min(0.92, max(0.25, float(context_soft_fraction))),
                reserve_tokens=max_tokens_eff + 512,
                keep_recent_messages=summarize_keep_recent_eff,
                summarize_max_tokens=summarize_max_tokens,
                max_summarize_input_tokens=summarize_max_input_tokens,
                auto_summarize=auto_summarize,
                max_tokens=max_tokens_eff,
                strip_reasoning=strip_reasoning,
                buffer_full_reply=schedule_buffer,
                max_history_messages=max_hist,
                temperature=temp,
                top_p=top_p,
                summarize_temperature=sum_temp,
                summarize_top_p=sum_tp,
                host_context=host_context,
                client_clock_minutes=client_clock_minutes,
                client_clock_date=client_clock_date,
                client_timezone_iana=client_timezone_iana,
                on_stream_chunk=callbacks.get("chunk"),
                on_stream_thinking=callbacks.get("thinking"),
                on_thinking_closed=callbacks.get("thinking_closed"),
                hide_schedule_deltas=False,
                on_compress=None,
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            _flow_log(
                flow_id,
                f"[{role}] generate finished — resolved_model={model_str_resolved!r} ok={ok} "
                f"assistant_chars={len(assistant_text)} {elapsed_ms}ms",
                lane="vllm",
                role=role,
                model=model_str_resolved,
                mlx="from_model",
            )
            return {
                "ok": ok,
                "assistant": assistant_text,
                "history": hist,
                "error": None if ok else "vLLM generation failed (see gateway terminal).",
                "model": model_str_resolved,
                "role": role,
            }

        bundle, model_str_or_err = ensure_model_bundle_loaded(model=model_arg)
        if bundle is None:
            _flow_log(
                flow_id,
                f"model bundle failed ({model_str_or_err})",
                lane="mlx",
                role=role,
                mlx="from_model",
            )
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
                client_timezone_iana=client_timezone_iana,
                on_stream_chunk=callbacks.get("chunk"),
                on_stream_thinking=callbacks.get("thinking"),
                on_thinking_closed=callbacks.get("thinking_closed"),
                hide_schedule_deltas=False,
                on_compress=None,
            )

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        _flow_log(
            flow_id,
            f"[{role}] generate finished — resolved_model={bundle.model_str!r} ok={ok} "
            f"assistant_chars={len(assistant_text)} {elapsed_ms}ms",
            lane="mlx",
            role=role,
            model=bundle.model_str,
            mlx="from_model",
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
        flow_id: str,
    ) -> tuple[bool, list[str]]:
        prompt_user = (
            "Grade this day-scheduler response for correctness. Check:\n"
            "1) Visible layout: either a Markdown heading (#/##…) as the first substantive line "
            "OR the legacy Unicode TO DO frame banner (╭ first line); then timetable bullets "
            "(Markdown list bullets are OK if they normalize); then conversational wrap-up prose.\n"
            "2) Each task line ends with duration **NhMm** only (e.g. 0h30m, 1h00m, 2h15m). "
            "Fail if you see bare 30m, 90m, **2h** without minutes, or \"45 min\" after the dash.\n"
            "3) Tasks on a **future calendar day** (per HOST planner targets / facts) must use "
            "`* [YYYY-MM-DD] [h:mm AM/PM] - …` including the date bracket before the time.\n"
            "4) Chronology and **single-track timeline**: on each day, each task starts "
            "at or after the **end** of the previous task (no overlapping blocks unless "
            "the USER asked for parallel work).\n"
            "5) Hard facts/context and overall planning sense—including **priority**: "
            "if the USER marks "
            "flexible work **ASAP** / **urgent** / **right away** / **first** while also naming "
            "**fixed-time** commitments later, the timetable should usually put that urgent "
            "flexible "
            "work in **slack before the next anchor** when it fits (Host clock = NOW), not after "
            "discretionary blocks that leave that gap empty; respect obvious dependencies "
            "(e.g. put-away groceries before cooking).\n\n"
            f"USER:\n{user_content}\n\n"
            f"HOST CONTEXT:\n{host_context or '(none)'}\n\n"
            f"CANDIDATE:\n{candidate}\n\n"
            'Return strict JSON only: {"pass": true, "score": 0.0, "reasons": []}'
        )
        grader_system = (
            "You are a strict JSON-only validator for day-scheduler replies; output JSON only."
        )
        if vllm_context is not None:
            _flow_log(
                flow_id,
                "self-grade vLLM call begins (validator JSON)",
                lane="vllm",
                role="self_grade",
                model=bundle.model_str,
                mlx="to_model",
            )
            try:
                g_temp, g_top_p, _ = _finalize_sampling_defaults_ui(
                    temperature=None,
                    top_p=None,
                    top_k=None,
                    thinking=False,
                )
                raw = chat_completion_text(
                    vllm_context.client,
                    api_base=vllm_context.route.api_base,
                    model=vllm_context.route.served_model_name,
                    messages=[
                        {"role": "system", "content": grader_system},
                        {"role": "user", "content": prompt_user},
                    ],
                    temperature=float(g_temp),
                    top_p=float(g_top_p),
                    max_tokens=256,
                    enable_thinking=False,
                )
            except Exception as exc:
                return False, [f"self-grade failed: {exc}"]
            grade = parse_self_grade(raw, min_score=self_grade_threshold)
            _flow_log(
                flow_id,
                "self-grade vLLM call finished "
                f"pass={grade.passed} score={grade.score:.2f} n_reasons={len(grade.reasons)}",
                lane="vllm",
                role="self_grade",
                model=bundle.model_str,
                mlx="from_model",
            )
            return grade.passed, list(grade.reasons)

        _flow_log(
            flow_id,
            "self-grade MLX call begins (validator JSON)",
            lane="mlx",
            role="self_grade",
            model=bundle.model_str,
            mlx="to_model",
        )
        try:
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
        _flow_log(
            flow_id,
            "self-grade MLX call finished "
            f"pass={grade.passed} score={grade.score:.2f} n_reasons={len(grade.reasons)}",
            lane="mlx",
            role="self_grade",
            model=bundle.model_str,
            mlx="from_model",
        )
        return grade.passed, list(grade.reasons)

    def _run_format_repair_completion(
        *,
        bundle: ModelBundle,
        user_content: str,
        host_context: str | None,
        broken_assistant: str,
        sampler_mod: object,
        flow_id: str,
    ) -> str:
        user_block = (
            "Rewrite the assistant timetable below so it passes strict automated checks. "
            "Keep the same tasks and intent; change only formatting.\n\n"
            "Required:\n"
            "- First line = top border of the Unicode TO DO banner (╭…).\n"
            "- Every task: * [optional YYYY-MM-DD for non-anchor days] "
            "[h:mm AM/PM] - Title - NhMm\n"
            "- Duration must be NhMm only (0h30m, 1h00m, not 30m or 2h alone).\n"
            "- Tasks on each day **must not overlap**: next start ≥ previous task end "
            "(start + duration); back-to-back is OK.\n"
            "- If HOST lists future planner-target dates, prefix those bullets with "
            "the date before the time bracket.\n\n"
            f"HOST CONTEXT:\n{host_context or '(none)'}\n\n"
            f"USER:\n{user_content}\n\n"
            f"BROKEN ASSISTANT OUTPUT:\n{broken_assistant}\n"
        )
        if vllm_context is not None:
            _flow_log(
                flow_id,
                "[scheduler] format-repair vLLM call begins (repair timetable formatting)",
                lane="vllm",
                role="format_repair",
                model=bundle.model_str,
                mlx="to_model",
            )
            try:
                r_temp, r_top_p, _ = _finalize_sampling_defaults_ui(
                    temperature=temperature_override if temperature_override is not None else None,
                    top_p=top_p_override if top_p_override is not None else None,
                    top_k=top_k_override if top_k_override is not None else None,
                    thinking=False,
                )
                raw = chat_completion_text(
                    vllm_context.client,
                    api_base=vllm_context.route.api_base,
                    model=vllm_context.route.served_model_name,
                    messages=[
                        {"role": "system", "content": _FORMAT_REPAIR_SYSTEM},
                        {"role": "user", "content": user_block},
                    ],
                    temperature=float(r_temp),
                    top_p=float(r_top_p),
                    max_tokens=min(8192, max_tokens_eff),
                    enable_thinking=False,
                )
            except Exception as exc:
                _flow_log(
                    flow_id,
                    f"[scheduler] format repair FAILED inside vLLM layer: {exc!r}",
                    lane="vllm",
                    role="format_repair",
                    model=bundle.model_str,
                    mlx="from_model",
                )
                return ""
            out = raw if isinstance(raw, str) else str(raw)
            _flow_log(
                flow_id,
                "[scheduler] format-repair vLLM call finished",
                lane="vllm",
                role="format_repair",
                model=bundle.model_str,
                mlx="from_model",
            )
            return out

        _flow_log(
            flow_id,
            "[scheduler] format-repair MLX call begins (repair timetable formatting)",
            lane="mlx",
            role="format_repair",
            model=bundle.model_str,
            mlx="to_model",
        )
        try:
            prompt = build_prompt(
                bundle.tokenizer,
                _FORMAT_REPAIR_SYSTEM,
                [("user", user_block)],
                enable_thinking=False,
            )
            with bundle.lock:
                raw = _generate_plain_completion(
                    model_m=bundle.model,
                    tokenizer=bundle.tokenizer,
                    prompt=prompt,
                    max_tokens=min(8192, max_tokens_eff),
                    gen_kw=_gen_kw(sampler_mod),
                )
        except Exception as exc:
            _flow_log(
                flow_id,
                f"[scheduler] format repair FAILED inside MLX layer: {exc!r}",
                lane="mlx",
                role="format_repair",
                model=bundle.model_str,
                mlx="from_model",
            )
            return ""
        out = raw if isinstance(raw, str) else str(raw)
        _flow_log(
            flow_id,
            "[scheduler] format-repair MLX call finished",
            lane="mlx",
            role="format_repair",
            model=bundle.model_str,
            mlx="from_model",
        )
        return out

    def _parser_sampler_mod():
        pt, pp, pk = _finalize_sampling_defaults_ui(
            temperature=0.25,
            top_p=0.85,
            top_k=20,
            thinking=False,
        )
        from mlx_lm.sample_utils import make_sampler as _mk

        return _mk(temp=pt, top_p=pp, min_p=min_p, top_k=pk)

    def _run_query_parser_completion(
        *,
        parser_model_path: str,
        content: str,
        client_clock_date_iso: str,
        client_clock_minutes: int | None,
        client_timezone_iana: str | None,
        flow_id: str,
    ) -> ParsedQuery:
        user_block = build_query_parser_user_block(
            content=content,
            client_clock_date_iso=client_clock_date_iso,
            client_clock_minutes=client_clock_minutes,
            client_timezone_iana=client_timezone_iana,
        )
        if vllm_context is not None:
            try:
                pt, pp, _pk = _finalize_sampling_defaults_ui(
                    temperature=0.25,
                    top_p=0.85,
                    top_k=20,
                    thinking=False,
                )
                raw = chat_completion_text(
                    vllm_context.client,
                    api_base=vllm_context.route.api_base,
                    model=vllm_context.route.served_model_name,
                    messages=[
                        {"role": "system", "content": QUERY_PARSER_SYSTEM},
                        {"role": "user", "content": user_block},
                    ],
                    temperature=float(pt),
                    top_p=float(pp),
                    max_tokens=384,
                    enable_thinking=False,
                )
            except Exception as exc:
                _flow_log(
                    flow_id,
                    f"[query_parser] vLLM call failed: {exc!r}",
                    lane="vllm",
                    role="query_parser",
                    model=vllm_context.route.served_model_name,
                )
                return ParsedQuery()
            cleaned = strip_redacted_thinking(raw if isinstance(raw, str) else str(raw))
            pq = parse_query_parser_completion_text(cleaned)
            _flow_log(
                flow_id,
                f"[query_parser] parsed primary_day={pq.primary_plan_date_iso!r} "
                f"est_events={pq.estimated_event_count}",
                lane="vllm",
                role="query_parser",
                model=vllm_context.route.served_model_name,
                mlx="from_model",
            )
            return pq

        bundle_p, err_txt = ensure_model_bundle_loaded(model=parser_model_path)
        if bundle_p is None:
            _flow_log(
                flow_id,
                f"[query_parser] bundle unavailable ({err_txt})",
                lane="mlx",
                role="query_parser",
            )
            return ParsedQuery()
        try:
            sampler_parser = _parser_sampler_mod()
            prompt = build_prompt(
                bundle_p.tokenizer,
                QUERY_PARSER_SYSTEM,
                [("user", user_block)],
                enable_thinking=False,
            )
            with bundle_p.lock:
                raw = _generate_plain_completion(
                    model_m=bundle_p.model,
                    tokenizer=bundle_p.tokenizer,
                    prompt=prompt,
                    max_tokens=384,
                    gen_kw=_gen_kw(sampler_parser),
                )
        except Exception as exc:
            _flow_log(
                flow_id,
                f"[query_parser] MLX call failed: {exc!r}",
                lane="mlx",
                role="query_parser",
                model=bundle_p.model_str,
            )
            return ParsedQuery()
        cleaned = strip_redacted_thinking(raw if isinstance(raw, str) else str(raw))
        pq = parse_query_parser_completion_text(cleaned)
        _flow_log(
            flow_id,
            f"[query_parser] parsed primary_day={pq.primary_plan_date_iso!r} "
            f"est_events={pq.estimated_event_count}",
            lane="mlx",
            role="query_parser",
            model=bundle_p.model_str,
            mlx="from_model",
        )
        return pq

    class SchedulerLlmGatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            line = fmt % args if args else fmt
            _LLM_HTTP_ACCESS.note(self, line)

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
            _qp_raw = os.environ.get("MLX_DAY_SCHEDULER_NO_QUERY_PARSER", "").strip().lower()
            qp_disabled = _qp_raw in (
                "1",
                "true",
                "yes",
                "on",
            )
            if vllm_context is not None:
                v_ok = probe_vllm_route(
                    vllm_context.client,
                    vllm_context.route,
                )
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "scheduler-llm",
                        "backend": "vllm",
                        "vllm_ok": v_ok,
                        "vllm_api_base": vllm_context.route.api_base,
                        "vllm_model": vllm_context.route.served_model_name,
                        "model": vllm_context.route.served_model_name,
                        "scheduler_model": _resolve_scheduler_model(scheduler_model),
                        "tokenizer_model_id": vllm_context.tokenizer_model_id,
                        "query_parser_model": resolve_scheduler_query_parser_model(None),
                        "query_parser_disabled": qp_disabled,
                        "loaded_roles": [],
                        "loaded_model_paths": [],
                        "default_hub_repo": DEFAULT_HUB_REPO,
                    },
                )
                return
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "mlx-scheduler-llm",
                    "backend": "mlx",
                    "model_loaded": bool(_model_registry),
                    "model": _resolve_scheduler_model(scheduler_model),
                    "scheduler_model": _resolve_scheduler_model(scheduler_model),
                    "query_parser_model": resolve_scheduler_query_parser_model(None),
                    "query_parser_disabled": qp_disabled,
                    "loaded_roles": sorted(_model_registry.keys()),
                    "loaded_model_paths": loaded_model_bundle_paths(),
                    "default_hub_repo": DEFAULT_HUB_REPO,
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
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

            if path == "/v1/plain-completion":
                flow_id = uuid.uuid4().hex[:10]
                system = payload.get("system")
                user = payload.get("user")
                if not isinstance(system, str) or not system.strip():
                    self._send_json_error(400, "`system` must be a non-empty string.")
                    return
                if not isinstance(user, str) or not user.strip():
                    self._send_json_error(400, "`user` must be a non-empty string.")
                    return
                mt_raw = payload.get("max_tokens")
                try:
                    mt = int(mt_raw) if mt_raw is not None else 2048
                except (TypeError, ValueError):
                    self._send_json_error(400, "`max_tokens` must be an integer when provided.")
                    return
                max_plain = max(32, min(8192, mt))

                t_override = payload.get("temperature")
                p_override = payload.get("top_p")
                k_override = payload.get("top_k")
                if t_override is not None and not isinstance(t_override, int | float):
                    self._send_json_error(400, "`temperature` must be a number when provided.")
                    return
                if p_override is not None and not isinstance(p_override, int | float):
                    self._send_json_error(400, "`top_p` must be a number when provided.")
                    return
                if k_override is not None and not isinstance(k_override, int):
                    self._send_json_error(400, "`top_k` must be an integer when provided.")
                    return

                pm = payload.get("model")
                model_arg = pm if isinstance(pm, str) else scheduler_model

                temp_pc, top_pc, _tk_pc = _finalize_sampling_defaults_ui(
                    temperature=float(t_override) if t_override is not None else None,
                    top_p=float(p_override) if p_override is not None else None,
                    top_k=int(k_override) if k_override is not None else None,
                    thinking=False,
                )

                if vllm_context is not None:
                    sched_d = _resolve_scheduler_model(scheduler_model)
                    route_pc, _canon_model = resolve_plain_completion_route(
                        model_arg=str(model_arg).strip() if model_arg else None,
                        scheduler_default_path=sched_d,
                        route=vllm_context.route,
                    )
                    infer_model = route_pc.served_model_name
                    max_u = len(user.strip()) if isinstance(user, str) else 0
                    _flow_log(
                        flow_id,
                        f"[plain-completion] vLLM call begins system_chars={len(system.strip())} "
                        f"user_chars={max_u} max_tokens={max_plain}",
                        lane="vllm",
                        role="plain_completion",
                        model=infer_model,
                        mlx="to_model",
                    )
                    try:
                        text_out = chat_completion_text(
                            vllm_context.client,
                            api_base=route_pc.api_base,
                            model=route_pc.served_model_name,
                            messages=[
                                {"role": "system", "content": system.strip()},
                                {"role": "user", "content": user.strip()},
                            ],
                            temperature=float(temp_pc),
                            top_p=float(top_pc),
                            max_tokens=max_plain,
                            enable_thinking=False,
                        )
                    except Exception as exc:
                        _flow_log(
                            flow_id,
                            f"plain-completion vLLM call failed ({exc!r})",
                            lane="vllm",
                            role="plain_completion",
                            model=infer_model,
                            mlx="from_model",
                        )
                        self._send_json_error(500, f"generation failed: {exc}")
                        return
                    text_final = text_out if isinstance(text_out, str) else str(text_out)
                    self._send_json(
                        200,
                        {
                            "text": text_final.strip(),
                            "model": infer_model,
                            "flow_id": flow_id,
                        },
                    )
                    text_n = len(text_final.strip())
                    _flow_log(
                        flow_id,
                        f"[plain-completion] vLLM call finished text_chars={text_n}",
                        lane="vllm",
                        role="plain_completion",
                        model=infer_model,
                        mlx="from_model",
                    )
                    return

                def _plain_sampler():
                    from mlx_lm.sample_utils import make_sampler as _mk

                    return _mk(temp=temp_pc, top_p=top_pc, min_p=min_p, top_k=_tk_pc)

                try:
                    sampler_plain = _plain_sampler()
                except Exception as e:
                    self._send_json_error(500, f"sampler failed: {e}")
                    return

                bundle_p, err_p = ensure_model_bundle_loaded(model=model_arg)
                if bundle_p is None:
                    self._send_json_error(500, err_p or "model load failed")
                    return
                max_u = len(user.strip()) if isinstance(user, str) else 0
                _flow_log(
                    flow_id,
                    f"[plain-completion] MLX call begins system_chars={len(system.strip())} "
                    f"user_chars={max_u} max_tokens={max_plain}",
                    lane="mlx",
                    role="plain_completion",
                    model=bundle_p.model_str,
                    mlx="to_model",
                )
                try:
                    prompt_pc = build_prompt(
                        bundle_p.tokenizer,
                        system.strip(),
                        [("user", user.strip())],
                        enable_thinking=False,
                    )
                    with bundle_p.lock:
                        text_out = _generate_plain_completion(
                            model_m=bundle_p.model,
                            tokenizer=bundle_p.tokenizer,
                            prompt=prompt_pc,
                            max_tokens=max_plain,
                            gen_kw=_gen_kw(sampler_plain),
                        )
                except Exception as exc:
                    _flow_log(
                        flow_id,
                        f"plain-completion MLX call failed ({exc!r})",
                        lane="mlx",
                        role="plain_completion",
                        model=bundle_p.model_str,
                        mlx="from_model",
                    )
                    self._send_json_error(500, f"generation failed: {exc}")
                    return
                text_final = text_out if isinstance(text_out, str) else str(text_out)
                self._send_json(
                    200,
                    {
                        "text": text_final.strip(),
                        "model": bundle_p.model_str,
                        "flow_id": flow_id,
                    },
                )
                _flow_log(
                    flow_id,
                    f"[plain-completion] MLX call finished text_chars={len(text_final.strip())}",
                    lane="mlx",
                    role="plain_completion",
                    model=bundle_p.model_str,
                    mlx="from_model",
                )
                return

            if path not in ("/chat", "/v1/day-scheduler/chat"):
                self._send_json_error(404, "Not found")
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
            sampler_mod: object | None = None
            if vllm_context is None:
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

            tz_label_raw = cc.get("timezone") if isinstance(cc, dict) else None
            client_timezone_iana = (
                str(tz_label_raw).strip()
                if isinstance(tz_label_raw, str) and tz_label_raw.strip()
                else None
            )

            client_clock_date_iso_fallback = (
                client_clock_date.isoformat()
                if client_clock_date is not None
                else date.today().isoformat()
            )
            parsed_query_result = ParsedQuery()
            import_default_plan_date = client_clock_date_iso_fallback

            schedule_buffer = payload.get("buffer") is True or os.environ.get(
                "MLX_DAY_SCHEDULER_BUFFER", ""
            ).strip().lower() in ("1", "true", "yes") or os.environ.get(
                "MLX_DAY_SCHEDULER_STREAM", ""
            ).strip().lower() in ("0", "false", "no", "off")

            flow_id = uuid.uuid4().hex[:10]

            model_arg = (
                payload.get("model")
                if isinstance(payload.get("model"), str)
                else scheduler_model
            )
            _flow_log(
                flow_id,
                f"HTTP body received — POST {path} content_chars={len(content)} "
                f"preview={_clip(content)!r} hist_turns={len(pairs)}",
                lane="http",
                mlx="from_client",
            )
            _flow_log(
                flow_id,
                f"pipeline config self_grade={'on' if enable_self_grade else 'off'} "
                f"threshold={self_grade_threshold:.3f}",
                lane="pipeline",
            )
            m_res = _resolve_scheduler_model(model_arg)
            _flow_log(
                flow_id,
                f"model routing model_arg={model_arg!r} → weights={m_res!r}",
                lane="pipeline",
                model=m_res,
            )

            _qp_raw2 = os.environ.get("MLX_DAY_SCHEDULER_NO_QUERY_PARSER", "").strip().lower()
            qp_disabled = _qp_raw2 in (
                "1",
                "true",
                "yes",
                "on",
            )
            if not qp_disabled:
                parser_path = resolve_scheduler_query_parser_model(None)
                parsed_query_result = _run_query_parser_completion(
                    parser_model_path=parser_path,
                    content=content,
                    client_clock_date_iso=client_clock_date_iso_fallback,
                    client_clock_minutes=client_clock_minutes,
                    client_timezone_iana=client_timezone_iana,
                    flow_id=flow_id,
                )
                qpf = format_query_parser_host_facts(parsed_query_result)
                host_context = f"{qpf}\n\n{host_context}" if host_context else qpf
                import_default_plan_date = resolve_import_default_plan_date(
                    parsed_query_result,
                    client_clock_date_iso=client_clock_date_iso_fallback,
                )

            persist_flag = isinstance(extra_ctx, str) and bool(extra_ctx.strip())
            habits_flag = isinstance(habits_raw, str) and bool(habits_raw.strip())
            merged_chars = len(host_context) if host_context else 0
            ck = ", ".join(
                filter(
                    None,
                    (
                        (
                            client_clock_date.isoformat()
                            if client_clock_date is not None
                            else None
                        ),
                        (
                            str(client_clock_minutes)
                            if client_clock_minutes is not None
                            else None
                        ),
                        client_timezone_iana,
                    ),
                )
            )
            _flow_log(
                flow_id,
                f"host_ctx habits_block={habits_flag} persisted_block={persist_flag} "
                f"merged_chars={merged_chars} client_clock[{ck}]",
                lane="pipeline",
            )
            _flow_log(
                flow_id,
                f"streaming schedule_buffer={schedule_buffer} → "
                f"tokens_streamed_to_HTTP_client={not schedule_buffer}",
                lane="pipeline",
            )

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Scheduler-Flow-ID", flow_id)
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

            if vllm_context is not None:
                sched_bundle = ModelBundle(
                    None,
                    None,
                    _resolve_scheduler_model(model_arg),
                    threading.Lock(),
                )
                sched_err = None
            else:
                sched_bundle, sched_err = ensure_model_bundle_loaded(model=model_arg)
            if sched_bundle is None:
                _flow_log(
                    flow_id,
                    f"scheduler model unavailable ({sched_err})",
                    lane="mlx",
                    mlx="from_model",
                    model=_resolve_scheduler_model(model_arg),
                )
                body_err = json.dumps({"type": "done", "ok": False, "error": sched_err}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Length", str(len(body_err)))
                self.end_headers()
                self.wfile.write(body_err)
                self.wfile.write(b"\n")
                return

            primary_result = _run_scheduler_generation(
                role="scheduler",
                model_arg=model_arg,
                content=content,
                pairs=hist,
                host_context=host_context,
                client_clock_minutes=client_clock_minutes,
                client_clock_date=client_clock_date,
                client_timezone_iana=client_timezone_iana,
                strip_reasoning=strip_reasoning,
                schedule_buffer=schedule_buffer,
                sampler_mod=sampler_mod,
                stream_callbacks={
                    "chunk": on_chunk,
                    "thinking": stream_thinking_cb,
                    "thinking_closed": thinking_done_cb,
                },
                flow_id=flow_id,
            )

            primary_result = dict(primary_result)
            assistant_norm = normalize_schedule_bullets_for_parser(
                str(primary_result.get("assistant") or "")
            )
            primary_result["assistant"] = assistant_norm

            default_pd = import_default_plan_date
            client_anchor_iso = (
                client_clock_date.isoformat()
                if client_clock_date is not None
                else None
            )
            repair_on = os.environ.get("SCHEDULER_FORMAT_REPAIR", "1").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            validation = validate_schedule_response(
                assistant_norm,
                default_plan_date=default_pd,
                client_minute_of_day=client_clock_minutes,
                host_context=host_context,
                client_anchor_date_iso=client_anchor_iso,
            )
            if (
                repair_on
                and not validation.ok
                and _only_format_repairable_validation_reasons(validation.reasons)
            ):
                _flow_log(
                    flow_id,
                    "[scheduler] validation failed — format-only; trying format-repair pass",
                    lane="pipeline",
                    model=sched_bundle.model_str,
                )
                fixed = _run_format_repair_completion(
                    bundle=sched_bundle,
                    user_content=content.strip(),
                    host_context=host_context,
                    broken_assistant=assistant_norm,
                    sampler_mod=sampler_mod,
                    flow_id=flow_id,
                )
                if fixed.strip():
                    assistant_norm = normalize_schedule_bullets_for_parser(fixed)
                    primary_result["assistant"] = assistant_norm
                    validation = validate_schedule_response(
                        assistant_norm,
                        default_plan_date=default_pd,
                        client_minute_of_day=client_clock_minutes,
                        host_context=host_context,
                        client_anchor_date_iso=client_anchor_iso,
                    )
            quality_ok = bool(primary_result.get("ok")) and validation.ok
            val_reasons = list(validation.reasons)
            _flow_log(
                flow_id,
                f"[scheduler] quality gate — mlx_ok={primary_result.get('ok')} "
                f"structural_ok={validation.ok} n_reasons={len(val_reasons)}",
                lane="pipeline",
                model=primary_result.get("model"),
            )
            sample = validation.reasons[:3]
            if sample:
                _flow_log(
                    flow_id,
                    f"[scheduler] validation_reasons(sample)={sample!r}",
                    lane="pipeline",
                )

            if quality_ok and enable_self_grade:
                graded_ok, grade_reasons = _self_grade_candidate(
                    bundle=sched_bundle,
                    user_content=content.strip(),
                    host_context=host_context,
                    candidate=str(primary_result.get("assistant") or ""),
                    sampler_mod=sampler_mod,
                    flow_id=flow_id,
                )
                quality_ok = graded_ok
                val_reasons.extend(grade_reasons)
            elif quality_ok:
                _flow_log(
                    flow_id,
                    "[scheduler] self-grade skipped (feature disabled)",
                    lane="pipeline",
                )

            assistant_text = str(primary_result.get("assistant") or "")
            mlx_ok = bool(primary_result.get("ok"))
            response_ok = mlx_ok and quality_ok
            chat_line = (
                chat_text_for_ui(assistant_full=assistant_text, user_raw=content.strip())
                if response_ok
                else ""
            )

            serializable = [
                {"role": r, "content": c}
                for r, c in cast(list[tuple[str, str]], primary_result["history"])
            ]
            _flow_log(
                flow_id,
                f"PRIMARY reply selected → role={primary_result.get('role')!r} "
                f"mlx_ok={mlx_ok} quality_ok={response_ok} assistant_chars={len(assistant_text)}",
                lane="pipeline",
                model=primary_result.get("model"),
                mlx="to_client",
            )
            write_line(
                {
                    "type": "done",
                    "flow_id": flow_id,
                    "ok": response_ok,
                    "assistant": assistant_text,
                    "chat": chat_line,
                    "history": serializable,
                    "model_role": primary_result.get("role"),
                    "model": primary_result.get("model"),
                    "fast_validation_reasons": val_reasons,
                    "error": None if response_ok else primary_result.get("error"),
                    "import_default_plan_date": import_default_plan_date,
                    "query_parser": parsed_query_to_meta(parsed_query_result),
                }
            )

            _flow_log(
                flow_id,
                "HTTP response stream closed (REQUEST end)",
                lane="http",
                mlx="to_client",
                model=primary_result.get("model"),
            )

    return SchedulerLlmGatewayHandler


def add_gateway_argparse(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Day-scheduler mlx-lm snapshot path or Hub id (env SCHEDULER_MODEL / MLX_MODEL; "
            f"default: {DEFAULT_SCHEDULER_MODEL})"
        ),
    )
    parser.add_argument(
        "--self-grade-threshold",
        type=float,
        default=float(os.environ.get("SCHEDULER_SELF_GRADE_THRESHOLD", "0.80")),
    )
    parser.add_argument(
        "--self-grade",
        dest="self_grade",
        action="store_true",
        default=os.environ.get("MLX_DAY_SCHEDULER_SELF_GRADE", "0").strip().lower()
        in ("1", "true", "yes", "on"),
        help=(
            "Run a second LLM grader after each successful reply (adds 5–15s/turn on Qwen3-14B). "
            "Off by default; opt in here or via MLX_DAY_SCHEDULER_SELF_GRADE=1. Structural "
            "validation + format-repair gate still run unconditionally."
        ),
    )
    parser.add_argument(
        "--no-self-grade",
        dest="self_grade",
        action="store_false",
        help="Force-disable self-grade even if MLX_DAY_SCHEDULER_SELF_GRADE is set.",
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
    parser.add_argument(
        "--llm-backend",
        choices=["auto", "vllm", "mlx"],
        default=(os.environ.get("SCHEDULER_LLM_BACKEND") or "auto").strip().lower() or "auto",
        help=(
            "Inference backend: vLLM OpenAI server (set VLLM_14B_BASE_URL), "
            "in-process MLX, or auto (vLLM if URLs set and healthy)."
        ),
    )
    _fb_raw = (os.environ.get("SCHEDULER_LLM_FALLBACK_MLX") or "").strip().lower()
    if _fb_raw in ("0", "false", "no", "off"):
        _mlx_fb_default = False
    elif _fb_raw in ("1", "true", "yes", "on"):
        _mlx_fb_default = True
    else:
        _mlx_fb_default = False
    parser.add_argument(
        "--mlx-fallback",
        action=argparse.BooleanOptionalAction,
        default=_mlx_fb_default,
        help=(
            "If vLLM is selected but probe fails, fall back to in-process MLX (requires mlx-lm). "
            "Default follows env SCHEDULER_LLM_FALLBACK_MLX (1/0); off if env unset. "
            "Use --no-mlx-fallback to force exit when vLLM is down."
        ),
    )


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
        scheduler_model=ns.model,
        self_grade_threshold=ns.self_grade_threshold,
        enable_self_grade=bool(getattr(ns, "self_grade", False)),
    )


__all__ = [
    "REPO_ROOT",
    "DEFAULT_SCHEDULER_MODEL",
    "_resolve_scheduler_model",
    "build_llm_gateway_handler",
    "ensure_model_bundle_loaded",
    "ensure_model_loaded",
    "loaded_model_bundle_paths",
    "add_gateway_argparse",
    "argparse_to_factory_kwargs",
    "load_day_scheduler_system_prompt",
]
