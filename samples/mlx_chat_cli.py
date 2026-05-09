#!/usr/bin/env python3
"""MLX Metal chat defaulting to **Qwen3-8B** (``Qwen/Qwen3-8B``).

Model card: https://huggingface.co/Qwen/Qwen3-8B

Weights are expected under::

    ~/models/Qwen3-8B

Download with::

    bash ~/models/pull-hf-models.sh

(Optional ``SKIP_MODELS_CLEAN=1`` to avoid wiping ``~/models``.) Then::

    cd ~/projects/scheduler
    uv sync --group samples-mlx
    uv run --group samples-mlx python samples/mlx_chat_cli.py

Override path or stream from Hub::

    export MLX_MODEL="/path/to/snapshot"
    # or
    uv run --group samples-mlx python samples/mlx_chat_cli.py \\
        --model Qwen/Qwen3-8B

**Browser shell** — MLX runs as a separate internal API gateway; the UI only proxies HTTP::

    # Terminal 1 — LLM (Metal)
    uv run --group samples-mlx python samples/mlx_llm_gateway.py
    # Terminal 2 — static shell + habits (open http://127.0.0.1:8765/)
    uv run python samples/mlx_day_scheduler_ui.py

Upstream URL for the shell: env ``MLX_SCHEDULER_LLM_API`` (default ``http://127.0.0.1:8766``).

Other checkpoints work via ``--model`` / ``MLX_MODEL`` (must be mlx-lm compatible).

Defaults prioritize **decode throughput** (greedy sampling, aggressive prefill chunking,
optional quantized KV cache). Use ``--temperature`` > 0 for stochastic replies.

Diagnostics::

    uv run --group samples-mlx python samples/mlx_chat_cli.py --diagnose

Environment:

    MLX_MODEL          Default directory / Hub id
    MLX_CHAT_STATS=1   Print tokens/sec and peak RAM after each reply (stderr)

When the prompt nears the model context limit, older turns are **summarized in-band**
(via the same MLX model) so the thread can continue without manual ``/clear``.
Disable with ``--no-auto-summarize``.

Verbose compression logs: ``MLX_CHAT_CONTEXT=1`` (stderr).

Day planner mode injects your **local wall clock** on every message (see
``[Clock — local machine]`` in the prompt). It enables Qwen3 ``enable_thinking`` in the
chat template **by default** so the
model can verify durations; reasoning is **shown** unless you set ``MLX_STRIP_THINKING=1`` or pass
``--strip-thinking``. Disable thinking with ``--no-day-scheduler-thinking`` or
``MLX_DAY_SCHEDULER_NO_THINKING=1``.

With ``--day-scheduler``, assistant text **streams** by default. To buffer each full reply and
print once, use ``--buffer-schedule`` or ``MLX_DAY_SCHEDULER_BUFFER=1``, or
``MLX_DAY_SCHEDULER_STREAM=0``.
With ``MLX_STRIP_THINKING=1`` (or ``--strip-thinking``), streamed output skips the thinking block;
only the schedule streams live after the model closes it.
Explicit constraints from your message are also injected as ``[Facts — …]`` for reliability.

Day-scheduler raises ``--max-tokens`` to at least **4096**; raise further if needed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from mlx_day_scheduler_pipeline import (
    REPO_ROOT,
    _local_wall_clock_snapshot,
    _minutes_to_ampm,
    compress_history_for_budget,
    generate_day_scheduler_reply,
    load_day_scheduler_system_prompt,
)
from mlx_day_scheduler_pipeline import (
    ThinkBlockStreamSplitter as _ThinkBlockStreamSplitter,
)
from mlx_day_scheduler_pipeline import (
    build_prompt as _build_prompt,
)
from mlx_day_scheduler_pipeline import (
    normalize_scheduler_terminal_escapes as _normalize_scheduler_terminal_escapes,
)
from mlx_day_scheduler_pipeline import (
    strip_reasoning_blocks as _strip_reasoning_blocks,
)
from mlx_day_scheduler_pipeline import (
    trim_history_pairs as _trim_history,
)

Role = Literal["user", "assistant"]


class _SupportsEncode(Protocol):
    def encode(self, text: str, *args: Any, **kwargs: Any) -> Any: ...

DEFAULT_HUB_REPO = "Qwen/Qwen3-8B"
DEFAULT_LOCAL_MODEL_DIR = Path.home() / "models" / "Qwen3-8B"

TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
)


def default_model_path() -> str:
    return str(DEFAULT_LOCAL_MODEL_DIR.expanduser().resolve())


def resolve_model_arg(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = (os.environ.get("MLX_MODEL") or "").strip()
    if env:
        return env
    return default_model_path()


def is_local_dir(model: str) -> bool:
    return Path(model).expanduser().is_dir()


def _expects_existing_directory(model_str: str) -> bool:
    s = model_str.strip()
    if s.startswith("~/") or s.startswith("/"):
        return True
    return Path(s).expanduser().is_absolute()


def _context_limit_from_config_json(path: Path) -> int | None:
    cfg_path = path / "config.json"
    if not cfg_path.is_file():
        return None
    try:
        meta = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("max_position_embeddings", "model_max_length"):
        v = meta.get(key)
        if isinstance(v, int) and 512 <= v <= 2_000_000:
            return v
    sw = meta.get("sliding_window")
    if isinstance(sw, int) and 512 <= sw <= 2_000_000:
        return sw
    return None


def resolve_context_token_limit(
    *,
    model_str: str,
    tokenizer: Any,
    explicit: int | None,
) -> int:
    if explicit is not None and explicit > 0:
        return explicit
    if is_local_dir(model_str):
        cap = _context_limit_from_config_json(Path(model_str).expanduser().resolve())
        if cap is not None:
            return cap
    tok_cap = getattr(tokenizer, "model_max_length", None)
    if isinstance(tok_cap, int) and 512 <= tok_cap <= 2_000_000:
        return tok_cap
    return 32768


def _encoded_length(tokenizer: Any, text: str) -> int:
    enc = cast(_SupportsEncode, tokenizer).encode(text)
    if isinstance(enc, list):
        return len(enc)
    ids = getattr(enc, "input_ids", None)
    if ids is None:
        return len(enc)
    row = ids[0] if ids and isinstance(ids[0], list) else ids
    return len(row)


def _default_generic_system() -> str:
    return "You are a helpful assistant. Answer briefly unless the user needs detail."


def _should_strip_reasoning(*, day_scheduler: bool, force_strip: bool) -> bool:
    if force_strip:
        return True
    raw = os.environ.get("MLX_STRIP_THINKING")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() not in ("0", "false", "no", "off")
    if day_scheduler:
        return False
    return not day_scheduler


def diagnose_local_snapshot(path: Path) -> list[str]:
    issues: list[str] = []
    if not path.is_dir():
        issues.append(f"Not a directory: {path}")
        return issues
    cfg = path / "config.json"
    if not cfg.is_file():
        issues.append("Missing config.json")
        return issues
    try:
        meta = json.loads(cfg.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        issues.append(f"Invalid config.json: {e}")
        return issues
    if "model_type" not in meta:
        issues.append(
            "config.json has no 'model_type' — incomplete or non-HF layout for mlx-lm."
        )
    else:
        issues.append(f"model_type={meta['model_type']!r} (mlx-lm must implement it)")
    if not any((path / name).is_file() for name in TOKENIZER_FILES):
        issues.append(
            "No tokenizer assets found "
            f"(expected one of {list(TOKENIZER_FILES)} under {path})."
        )
    weights = list(path.glob("*.safetensors")) + list(path.glob("*.npz"))
    if not weights:
        issues.append("No *.safetensors weight shards in directory.")
    return issues


def run_diagnose(model: str) -> int:
    print("=== MLX / Metal diagnostics ===")
    print(f"platform: {sys.platform} ({platform.machine()})")
    print(f"python:   {sys.version.split()[0]} — {sys.executable}")

    try:
        import mlx.core as mx
    except ImportError:
        print("mlx:      NOT INSTALLED (uv sync --group samples-mlx)")
        return 2

    print(f"mlx:      {getattr(mx, '__version__', '?')}")
    try:
        ok = mx.metal.is_available()
    except RuntimeError as e:
        print(f"metal:    ERROR — {e}")
        print(
            "\nMetal failed (common in CI sandboxes / SSH without GPU access). "
            "Run on the MacBook desktop session.",
        )
        return 2

    print(f"metal:    {'available' if ok else 'NOT available'}")
    if not ok:
        return 2

    try:
        import mlx_lm
    except ImportError:
        print("mlx-lm:   NOT INSTALLED")
        return 2
    print(f"mlx-lm:   {getattr(mlx_lm, '__version__', '?')}")

    print(f"\nmodel arg: {model!r}")
    if is_local_dir(model):
        p = Path(model).expanduser().resolve()
        issues = diagnose_local_snapshot(p)
        if issues:
            print("\nLocal snapshot problems:")
            for line in issues:
                print(f"  - {line}")
            print(
                "\nFix: run  bash ~/models/pull-hf-models.sh\n"
                f"Or stream from Hub: --model {DEFAULT_HUB_REPO}",
            )
            return 3
        print("\nLocal snapshot: basic layout looks OK (still may fail if RAM exhausted).")
    else:
        print(
            f"\nDirectory not found locally — mlx_lm will treat this as a Hub ref "
            f"(e.g. {DEFAULT_HUB_REPO})."
        )

    print("\nTip: run without --diagnose to load and chat.")
    return 0


def _resolve_template_enable_thinking(
    *,
    day_scheduler: bool,
    enable_thinking_flag: bool,
    no_day_scheduler_thinking: bool,
) -> bool | None:
    if enable_thinking_flag or os.environ.get(
        "MLX_ENABLE_THINKING", ""
    ).strip().lower() in ("1", "true", "yes"):
        return True
    if day_scheduler:
        if no_day_scheduler_thinking or os.environ.get(
            "MLX_DAY_SCHEDULER_NO_THINKING", ""
        ).strip().lower() in ("1", "true", "yes"):
            return False
        return True
    return None


def _finalize_sampling_defaults(
    ns: argparse.Namespace,
    *,
    day_scheduler: bool,
    template_enable_thinking: bool | None,
) -> None:
    """Fill None sampler fields from argparse; mode-dependent defaults for Qwen3."""
    if ns.min_p is None:
        ns.min_p = 0.0

    thinking = template_enable_thinking is True
    nonthinking_ds = day_scheduler and template_enable_thinking is False

    if ns.temperature is None:
        if nonthinking_ds:
            ns.temperature = 0.7
        elif thinking:
            ns.temperature = 0.6
        else:
            ns.temperature = 0.0
    if ns.top_p is None:
        if nonthinking_ds:
            ns.top_p = 0.8
        elif thinking:
            ns.top_p = 0.95
        else:
            ns.top_p = 1.0
    if ns.top_k is None:
        if nonthinking_ds or thinking:
            ns.top_k = 20
        else:
            ns.top_k = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MLX Metal chat for Qwen3-8B (local ~/models or Hub)."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Local snapshot directory or HF repo id "
            f"(default: {DEFAULT_LOCAL_MODEL_DIR}; env MLX_MODEL). "
            f"Hub id (default): {DEFAULT_HUB_REPO}"
        ),
    )
    parser.add_argument(
        "--system",
        default=None,
        help=(
            "System prompt. Default: generic assistant; with --day-scheduler, loads "
            "prompts/day-scheduler-system.md unless this flag is set."
        ),
    )
    parser.add_argument(
        "--day-scheduler",
        action="store_true",
        help=(
            "Day-planner mode: load prompts/day-scheduler-system.md (unless --system), "
            "inject host **local wall clock** (refreshed on every message). "
            "Or set MLX_DAY_SCHEDULER=1."
        ),
    )
    parser.add_argument(
        "--enable-thinking",
        action="store_true",
        help=(
            "Force Qwen3 enable_thinking=True in chat template. "
            "Day-scheduler already defaults thinking on (hidden); this also affects generic chat. "
            "Or set MLX_ENABLE_THINKING=1."
        ),
    )
    parser.add_argument(
        "--no-day-scheduler-thinking",
        action="store_true",
        help=(
            "With --day-scheduler, force enable_thinking=False (faster, no internal think pass). "
            "Or set MLX_DAY_SCHEDULER_NO_THINKING=1."
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=384,
        help="Completion cap (lower = faster wall-clock). Default tuned for speed.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help=(
            "Sampling temperature (omit for mode defaults: greedy in generic chat, "
            "0.7 in day-scheduler without thinking per Qwen3 non-thinking guidance)."
        ),
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=None,
        help=(
            "Nucleus sampling (omit for defaults: 1.0 generic, 0.8 day-scheduler w/o thinking)."
        ),
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Top-k (omit for mode defaults: 0 generic, 20 day-scheduler / thinking-on).",
    )
    parser.add_argument(
        "--min-p",
        type=float,
        default=None,
        help="Min-p sampling; omit defaults to 0.",
    )
    parser.add_argument(
        "--prefill-step-size",
        type=int,
        default=4096,
        help="Prompt processing chunk size (larger often improves TTFT on Metal).",
    )
    parser.add_argument(
        "--kv-bits",
        type=int,
        default=8,
        help="KV-cache quantization bits (0 = off). 8 cuts cache bandwidth (often faster).",
    )
    parser.add_argument(
        "--kv-group-size",
        type=int,
        default=64,
        help="Group size when KV quantization is enabled.",
    )
    parser.add_argument(
        "--quantized-kv-start",
        type=int,
        default=0,
        help="Token offset at which KV quantization begins.",
    )
    parser.add_argument(
        "--max-kv-size",
        type=int,
        default=None,
        help="Optional KV cache cap (rotating); trims longest context, saves memory.",
    )
    parser.add_argument(
        "--max-history-messages",
        type=int,
        default=0,
        help=(
            "Hard cap on stored turns (0 = unlimited). Rolling summarization keeps token "
            "budget; use a small cap only with --no-auto-summarize."
        ),
    )
    parser.add_argument(
        "--context-limit",
        type=int,
        default=None,
        help="Model context size in tokens (default: config.json or tokenizer; else 32768).",
    )
    parser.add_argument(
        "--context-soft-fraction",
        type=float,
        default=0.72,
        help="When prompt tokens exceed this fraction of context (minus reserve), compress.",
    )
    parser.add_argument(
        "--context-reserve-tokens",
        type=int,
        default=None,
        help="Reserved headroom for the reply (default: max-tokens + 512).",
    )
    parser.add_argument(
        "--summarize-keep-recent",
        type=int,
        default=12,
        help="Messages to leave verbatim when compressing older context.",
    )
    parser.add_argument(
        "--summarize-max-tokens",
        type=int,
        default=384,
        help="Max new tokens for each rolling-summary generation.",
    )
    parser.add_argument(
        "--summarize-max-input-tokens",
        type=int,
        default=6144,
        help="Cap transcript size (tokens) fed into summarization prompts.",
    )
    parser.add_argument(
        "--no-auto-summarize",
        action="store_true",
        help="Disable rolling summarization (falls back to dropping oldest turns).",
    )
    parser.add_argument(
        "--strip-thinking",
        action="store_true",
        help=(
            "Remove Qwen3 reasoning XML from output (and from transcript). "
            "Day-scheduler keeps thinking visible by default; pass this to hide it."
        ),
    )
    parser.add_argument(
        "--buffer-schedule",
        action="store_true",
        help=(
            "With --day-scheduler, buffer the full assistant reply then print once (no streaming). "
            "Or set MLX_DAY_SCHEDULER_BUFFER=1 or MLX_DAY_SCHEDULER_STREAM=0."
        ),
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Print environment + model-path checks and exit (no load).",
    )
    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])

    day_scheduler = ns.day_scheduler or os.environ.get(
        "MLX_DAY_SCHEDULER", ""
    ).strip().lower() in ("1", "true", "yes")

    raw_sys = ns.system
    if raw_sys is None or (isinstance(raw_sys, str) and not raw_sys.strip()):
        ns.system = (
            load_day_scheduler_system_prompt(REPO_ROOT)
            if day_scheduler
            else _default_generic_system()
        )
    else:
        ns.system = raw_sys.strip()

    if day_scheduler:
        ns.max_tokens = max(ns.max_tokens, 4096)

    template_enable_thinking = _resolve_template_enable_thinking(
        day_scheduler=day_scheduler,
        enable_thinking_flag=ns.enable_thinking,
        no_day_scheduler_thinking=ns.no_day_scheduler_thinking,
    )
    _finalize_sampling_defaults(
        ns,
        day_scheduler=day_scheduler,
        template_enable_thinking=template_enable_thinking,
    )

    strip_reasoning = _should_strip_reasoning(
        day_scheduler=day_scheduler,
        force_strip=ns.strip_thinking,
    )
    _sched_buffer = (
        ns.buffer_schedule
        or os.environ.get("MLX_DAY_SCHEDULER_BUFFER", "").strip().lower()
        in ("1", "true", "yes")
        or os.environ.get("MLX_DAY_SCHEDULER_STREAM", "").strip().lower()
        in ("0", "false", "no", "off")
    )
    stream_schedule = day_scheduler and not _sched_buffer
    ds_stream_strip = stream_schedule and strip_reasoning
    ds_stream_raw = stream_schedule and not strip_reasoning

    model_str = resolve_model_arg(ns.model)
    if ns.diagnose:
        return run_diagnose(model_str)

    if _expects_existing_directory(model_str):
        p = Path(model_str).expanduser().resolve()
        if not p.is_dir():
            print(
                f"error: model directory does not exist:\n  {p}\n\n"
                "Download with:\n"
                "  bash ~/models/pull-hf-models.sh\n\n"
                f"Or pass the Hub repo explicitly:\n"
                f"  --model {DEFAULT_HUB_REPO}\n",
                file=sys.stderr,
            )
            return 2

    try:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.generate import stream_generate
        from mlx_lm.sample_utils import make_sampler
    except ImportError as e:
        print(f"error: {e}\nInstall: uv sync --group samples-mlx", file=sys.stderr)
        return 2

    if not mx.metal.is_available():
        print(
            "error: MLX Metal is not available (sandboxed session or unsupported host).\n"
            "Run this on your Apple Silicon Mac outside restricted environments.",
            file=sys.stderr,
        )
        return 2

    if is_local_dir(model_str):
        issues = diagnose_local_snapshot(Path(model_str).expanduser().resolve())
        hard_block = [
            i
            for i in issues
            if i.startswith("Missing") or "No tokenizer" in i or "No *.safetensors" in i
        ]
        if hard_block:
            print("Cannot load local model:\n", file=sys.stderr)
            for line in issues:
                print(f"  - {line}", file=sys.stderr)
            print("\nRun: bash ~/models/pull-hf-models.sh\n", file=sys.stderr)
            return 3

    print(f"Loading {model_str!r} (Metal) …", file=sys.stderr)
    try:
        model_m, tokenizer = load(
            model_str,
            tokenizer_config={"trust_remote_code": True},
        )
    except Exception as e:
        print(
            f"\nLoad failed: {e}\n\n"
            "If local: ensure pull finished (all shards) and you have enough RAM.\n"
            "Or try Hub:\n"
            f"  --model {DEFAULT_HUB_REPO}\n",
            file=sys.stderr,
        )
        return 4

    mx.clear_cache()

    sampler = make_sampler(
        temp=ns.temperature,
        top_p=ns.top_p,
        min_p=ns.min_p,
        top_k=ns.top_k,
    )
    gen_kw: dict[str, Any] = {
        "sampler": sampler,
        "prefill_step_size": ns.prefill_step_size,
    }
    if ns.kv_bits > 0:
        gen_kw["kv_bits"] = ns.kv_bits
        gen_kw["kv_group_size"] = ns.kv_group_size
        gen_kw["quantized_kv_start"] = ns.quantized_kv_start
    if ns.max_kv_size is not None:
        gen_kw["max_kv_size"] = ns.max_kv_size

    ctx_limit = resolve_context_token_limit(
        model_str=model_str,
        tokenizer=tokenizer,
        explicit=ns.context_limit,
    )
    reserve_tokens = (
        ns.context_reserve_tokens
        if ns.context_reserve_tokens is not None
        else ns.max_tokens + 512
    )
    soft_frac = min(0.92, max(0.25, float(ns.context_soft_fraction)))
    auto_summarize = not ns.no_auto_summarize
    keep_recent = max(2, int(ns.summarize_keep_recent))

    history: list[tuple[Role, str]] = []
    summarize_note = (
        "rolling summary on"
        if auto_summarize
        else "rolling summary off (drops oldest turns)"
    )
    boot_min, boot_date = (
        _local_wall_clock_snapshot() if day_scheduler else (0, date.today())
    )
    print(
        f"MLX chat ({DEFAULT_LOCAL_MODEL_DIR.name}). Commands: /clear /quit\n"
        f"model={model_str}\n"
        f"(speed defaults: temp={ns.temperature}, max_tokens={ns.max_tokens}, "
        f"prefill_step={ns.prefill_step_size}, kv_bits={ns.kv_bits or 'off'})\n"
        f"context ~{ctx_limit} tok, compress over ~{soft_frac:.0%} "
        f"(reserve {reserve_tokens} for reply); {summarize_note}\n",
        file=sys.stderr,
    )
    if not day_scheduler:
        print(
            "Tip: pass --day-scheduler (or MLX_DAY_SCHEDULER=1) to load "
            "prompts/day-scheduler-system.md for timed daily plans.\n",
            file=sys.stderr,
        )
    if day_scheduler:
        print(
            "Day scheduler: every message uses your **local laptop clock** (minute resolution), "
            f"e.g. right now about {_minutes_to_ampm(boot_min)} on {boot_date.isoformat()}.\n",
            file=sys.stderr,
        )
        if template_enable_thinking is False:
            print(
                "Qwen3: chat template enable_thinking=False (concise). "
                "Day-scheduler normally uses thinking on; disable with "
                "--no-day-scheduler-thinking or MLX_DAY_SCHEDULER_NO_THINKING=1.\n",
                file=sys.stderr,
            )
        elif template_enable_thinking is True:
            print(
                "Qwen3: enable_thinking=True — reasoning shows before the banner. "
                "Hide it with MLX_STRIP_THINKING=1 or --strip-thinking.\n",
                file=sys.stderr,
            )
        if stream_schedule:
            if strip_reasoning:
                print(
                    "Day-scheduler: replies stream as generated (thinking is buffered until the "
                    "closing think tag, then the rest streams).\n",
                    file=sys.stderr,
                )
            else:
                print(
                    "Day-scheduler: replies stream as generated (thinking + schedule).\n",
                    file=sys.stderr,
                )
        else:
            print(
                "Day-scheduler: buffering each full reply (--buffer-schedule or "
                "MLX_DAY_SCHEDULER_BUFFER=1 / MLX_DAY_SCHEDULER_STREAM=0). "
                "Thinking is included by default.\n",
                file=sys.stderr,
            )

    show_stats = os.environ.get("MLX_CHAT_STATS", "").strip() not in {"", "0", "false"}
    ctx_verbose = os.environ.get("MLX_CHAT_CONTEXT", "").strip() not in {"", "0", "false"}
    if day_scheduler and not strip_reasoning:
        print(
            "Reasoning included in output (hide with --strip-thinking or "
            f"MLX_STRIP_THINKING=1); max_tokens={ns.max_tokens}\n",
            file=sys.stderr,
            flush=True,
        )

    while True:
        try:
            user = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in {"/quit", "/exit", "/q"}:
            break
        if user == "/clear":
            history.clear()
            print("(cleared)\n")
            continue

        if day_scheduler:
            clock_minutes, clock_date = _local_wall_clock_snapshot()
            print(
                f"[scheduler] local time this prompt: {_minutes_to_ampm(clock_minutes)} "
                f"({clock_date.isoformat()})\n",
                file=sys.stderr,
                flush=True,
            )
            print("AI> ", end="", flush=True)

            def _on_compress(did: bool, p_tokens: int, budget: int) -> None:
                if did:
                    msg = (
                        f"[context] compressed older turns → ~{p_tokens} prompt tokens "
                        f"(budget ~{budget})."
                    )
                    if ctx_verbose:
                        msg += (
                            f" limit={ctx_limit} soft={soft_frac:.2f} reserve={reserve_tokens}"
                        )
                    print(msg, file=sys.stderr, flush=True)

            gen_ok, reply, last_resp = generate_day_scheduler_reply(
                user_raw=user,
                history=history,
                model_m=model_m,
                tokenizer=tokenizer,
                base_system_prompt=ns.system,
                template_enable_thinking=template_enable_thinking,
                context_limit=ctx_limit,
                soft_fraction=soft_frac,
                reserve_tokens=reserve_tokens,
                keep_recent_messages=keep_recent,
                summarize_max_tokens=ns.summarize_max_tokens,
                max_summarize_input_tokens=ns.summarize_max_input_tokens,
                gen_kw=gen_kw,
                auto_summarize=auto_summarize,
                max_tokens=ns.max_tokens,
                strip_reasoning=strip_reasoning,
                buffer_full_reply=_sched_buffer,
                max_history_messages=ns.max_history_messages,
                on_stream_chunk=lambda s: print(s, end="", flush=True),
                on_compress=_on_compress,
            )
            print()
            if show_stats and last_resp is not None:
                print(
                    f"[stats] gen_tokens={last_resp.generation_tokens} "
                    f"gen_tps={last_resp.generation_tps:.1f} "
                    f"peak_mem_gb={last_resp.peak_memory:.2f}\n",
                    file=sys.stderr,
                    flush=True,
                )
            if (
                last_resp is not None
                and getattr(last_resp, "generation_tokens", 0) >= ns.max_tokens - 1
            ):
                print(
                    "[hint] Hit --max-tokens ceiling (output may be truncated). "
                    f"Try --max-tokens {min(ns.max_tokens * 2, 32768)} or higher.\n",
                    file=sys.stderr,
                    flush=True,
                )
            if not gen_ok:
                continue
            continue

        user_for_prompt = user
        effective_system = ns.system

        did_compress, p_tokens, budget = compress_history_for_budget(
            model_m=model_m,
            tokenizer=tokenizer,
            system=effective_system,
            history=history,
            pending_user=user_for_prompt,
            context_limit=ctx_limit,
            soft_fraction=soft_frac,
            reserve_tokens=reserve_tokens,
            keep_recent_messages=keep_recent,
            summarize_max_tokens=ns.summarize_max_tokens,
            max_summarize_input_tokens=ns.summarize_max_input_tokens,
            gen_kw=gen_kw,
            auto_summarize=auto_summarize,
            enable_thinking=template_enable_thinking,
        )
        if did_compress:
            msg = (
                f"[context] compressed older turns → ~{p_tokens} prompt tokens "
                f"(budget ~{budget})."
            )
            if ctx_verbose:
                msg += (
                    f" limit={ctx_limit} soft={soft_frac:.2f} reserve={reserve_tokens}"
                )
            print(msg, file=sys.stderr, flush=True)

        prompt = _build_prompt(
            tokenizer,
            effective_system,
            history + [("user", user_for_prompt)],
            enable_thinking=template_enable_thinking,
        )

        print("AI> ", end="", flush=True)
        buf: list[str] = []
        last_resp = None
        gen_ok = False
        stream_tokens_live = (not strip_reasoning and not day_scheduler) or ds_stream_raw
        think_splitter = _ThinkBlockStreamSplitter() if ds_stream_strip else None
        try:
            for resp in stream_generate(
                model_m,
                tokenizer,
                prompt,
                max_tokens=ns.max_tokens,
                **gen_kw,
            ):
                last_resp = resp
                buf.append(resp.text)
                if think_splitter is not None:
                    piece = think_splitter.feed(resp.text)
                    if piece:
                        print(
                            _normalize_scheduler_terminal_escapes(piece),
                            end="",
                            flush=True,
                        )
                elif stream_tokens_live:
                    print(resp.text, end="", flush=True)
            gen_ok = True
        except Exception as e:
            print(f"\nerror: {e}", file=sys.stderr)
            continue
        reply = "".join(buf)
        if strip_reasoning:
            reply = _strip_reasoning_blocks(reply)
        if day_scheduler and gen_ok:
            reply = _normalize_scheduler_terminal_escapes(reply)
        if think_splitter is not None:
            tail = think_splitter.flush()
            if tail:
                print(_normalize_scheduler_terminal_escapes(tail), end="", flush=True)
        streamed_to_stdout = stream_tokens_live or think_splitter is not None
        if not streamed_to_stdout:
            print(reply, end="")
        print()
        if show_stats and last_resp is not None:
            print(
                f"[stats] gen_tokens={last_resp.generation_tokens} "
                f"gen_tps={last_resp.generation_tps:.1f} "
                f"peak_mem_gb={last_resp.peak_memory:.2f}\n",
                file=sys.stderr,
                flush=True,
            )
        if (
            last_resp is not None
            and getattr(last_resp, "generation_tokens", 0) >= ns.max_tokens - 1
        ):
            print(
                "[hint] Hit --max-tokens ceiling (output may be truncated). "
                f"Try --max-tokens {min(ns.max_tokens * 2, 32768)} or higher.\n",
                file=sys.stderr,
                flush=True,
            )
        history.append(("user", user))
        history.append(("assistant", reply))
        _trim_history(history, ns.max_history_messages)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
