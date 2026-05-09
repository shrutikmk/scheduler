"""Shared MLX **day-scheduler** prompting, context compression, and generation.

CLI and HTTP UI import this module so inference behavior stays aligned.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

Role = Literal["user", "assistant"]

REPO_ROOT = Path(__file__).resolve().parent.parent


class _SupportsEncode(Protocol):
    def encode(self, text: str, *args: Any, **kwargs: Any) -> Any: ...


def _local_wall_clock_snapshot() -> tuple[int, date]:
    """Minute-of-day and calendar date in the machine's local timezone."""
    now = datetime.now().astimezone()
    return now.hour * 60 + now.minute, now.date()


def _minutes_to_ampm(total_minutes: int) -> str:
    total_minutes = total_minutes % (24 * 60)
    h24, m = divmod(total_minutes, 60)
    ampm = "AM" if h24 < 12 else "PM"
    h12 = h24 % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{m:02d} {ampm}"


def _day_scheduler_clock_suffix(*, clock_minutes: int, clock_date: date) -> str:
    floor = _minutes_to_ampm(clock_minutes)
    return (
        "\n\n---\n[Clock — local machine]\n"
        "Use **only** this timestamp as “right now” for scheduling "
        "(ignore model training-time priors about dates or times).\n"
        f"Local wall time for **this** prompt: **{floor}** on **{clock_date.isoformat()}**.\n"
        "Schedule forward from this instant through the rest of the day.\n"
        f"Hard rule: each `* [time] - …` line must use a start time **at or after {floor}**. "
        "Rebuild from this NOW; earlier times in past assistant messages are stale—"
        "do not copy them.\n"
        "**Outstanding work:** keep **every** obligation from your last plan that the user did "
        "**not** explicitly say they finished. Slide tasks forward—do **not** drop them just "
        "because time moved.\n"
    )


def _scheduler_completion_labels(user_line: str) -> list[str]:
    """Detect user lines reporting task(s) done; return short labels (possibly several)."""
    s = user_line.strip()
    if not s:
        return []

    tail: str | None = None
    anchored = (
        r"(?i)^i\s+just\s+finished\s+(.+)$",
        r"(?i)^(?:i\s+)?finished\s+up\s+(.+)$",
        r"(?i)^(?:i\s+)?finished\s+(.+)$",
        r"(?i)^i\s+ended\s+up\s+finish(?:ed|ing)\s+(?:up\s+)?(.+)$",
        r"(?i)^(?:done|finished)\s+with\s+(.+)$",
        r"(?i)^i\s+(?:have\s+)?completed\s+(.+)$",
        r"(?i)^i\s+(?:just\s+)?(?:got|am)\s+done\s+(?:with\s+)?(.+)$",
        r"(?i)^i\s+w(?:as|ere)\s+able\s+to\s+finish\s+(.+)$",
    )
    for pat in anchored:
        m = re.match(pat, s)
        if m:
            tail = m.group(1).strip().rstrip("!.?")
            break
    if tail is None:
        m = re.search(
            r"(?i)\bfinish(?:ed|ing)\s+up\s+(.+?)(?:\s+early)?\s*[!.?]?\s*$",
            s,
        )
        if m:
            tail = m.group(1).strip().rstrip("!.?")
    if not tail or len(tail) < 2:
        return []

    parts = re.split(r"(?i)\s*,\s*and\s+|\s+and\s+", tail)
    out = [p.strip().rstrip("!.?") for p in parts if p.strip()]
    return out if out else []


def _day_scheduler_user_fact_sheet(user_line: str) -> str | None:
    """Extract hard numeric constraints from the user's message (host guardrails)."""
    s = user_line.strip()
    if not s:
        return None
    chunks: list[str] = []

    work_h: int | None = None
    for pat in (
        r"(?i)(?:need\s+to|have\s+to|must|want\s+to|going\s+to)\s+work\s+(?:for\s+)?(\d+)\s*(?:hours?|hrs?\b|h\b)(?!\s*each)",
        r"(?i)\bwork\s+for\s+(\d+)\s*(?:hours?|hrs?\b|h\b)",
        r"(?i)(\d+)\s*(?:hours?|hrs?\b|h\b)\s+of\s+work\b",
        r"(?i)(\d+)\s*(?:hours?|hrs?\b|h\b)\s+(?:today\s+)?at\s+work\b",
    ):
        m = re.search(pat, s)
        if m:
            work_h = int(m.group(1))
            break

    if work_h is not None:
        chunks.append(
            f"- **Work:** The user specified **{work_h}h0m total** work time. Schedule **one** "
            f"continuous work block of **{work_h}h0m** (or split **only** if they explicitly "
            "asked for multiple shifts). The **sum** of durations on every line whose task is "
            f"clearly the same paid/focused work obligation must be **exactly {work_h}h0m**, "
            "not more—do **not** add an extra afternoon work block."
        )

    m = re.search(r"(?i)(?:get\s+)?(?:my\s+)?(\d+)\s*k\s*steps", s)
    if m:
        chunks.append(
            f"- **Steps:** Include a **{m.group(1)}k steps** block; infer a sensible duration."
        )
    elif re.search(r"(?i)\b10\s*k\s*steps\b", s):
        chunks.append(
            "- **Steps:** Include **10k steps**; infer a sensible duration."
        )

    if re.search(r"(?i)\b(cook|make)\s+(?:dinner|supper)\b", s) or re.search(
        r"(?i)\bcook\s+dinner\b", s
    ):
        chunks.append(
            "- **Dinner:** User mentioned cooking dinner—one meal-prep block unless they remove "
            "it in a later message."
        )
    elif re.search(r"(?i)\bdinner\b", s):
        chunks.append(
            "- **Meal:** User mentioned dinner—one meal-related block unless they say otherwise."
        )

    if not chunks:
        return None
    return (
        "[Facts — parsed from the user's message; treat as hard requirements]\n"
        + "\n".join(chunks)
    )


_REASONING_BLOCK_RE = re.compile(
    r"<\s*(?:(?:redacted_)?thinking|think)\b[^>]*>"
    r".*?"
    r"</\s*(?:(?:redacted_)?thinking|think)\s*>",
    re.IGNORECASE | re.DOTALL,
)

_REASONING_OPEN_RE = re.compile(
    r"<\s*((?:redacted_)?thinking|think)\b[^>]*>",
    re.IGNORECASE,
)
_REASONING_CLOSE_RE = re.compile(
    r"</\s*((?:redacted_)?thinking|think)\s*>",
    re.IGNORECASE,
)


def strip_reasoning_blocks(text: str) -> str:
    """Remove common model chain-of-thought XML blocks (e.g. Qwen3 thinking tags)."""
    t = _REASONING_BLOCK_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# Optional leading ``[YYYY-MM-DD]`` calendar tag for multi-day persistence / UI import.
_SCHED_LINE_RE = re.compile(
    r"^\*\s*(?:\[(\d{4}-\d{2}-\d{2})\]\s+)?\[([^\]]+)\]\s*-\s*(.+?)\s*-\s*(\d+h\d+m)\s*$",
)


def chat_text_for_ui(*, assistant_full: str, user_raw: str) -> str:
    """Short natural reply for the chat bubble; full ``assistant_full`` still drives the agenda."""
    body = strip_reasoning_blocks(assistant_full)
    tail = _extract_chat_tail_after_schedule(body)
    if tail and len(tail.strip()) >= 8:
        return tail.strip()
    return _synthetic_schedule_ack(user_raw)


def _extract_chat_tail_after_schedule(text: str) -> str:
    lines = text.splitlines()
    last_i = -1
    for i, line in enumerate(lines):
        if _SCHED_LINE_RE.match(line.strip()):
            last_i = i
    if last_i < 0:
        return ""
    return "\n".join(lines[last_i + 1 :]).strip()


def _synthetic_schedule_ack(user_raw: str) -> str:
    u = " ".join(user_raw.strip().split())
    if len(u) > 280:
        u = u[:277].rstrip() + "…"
    lead = (
        "I've generated a schedule for you—please review it in the planner and "
        "let me know if anything should move."
    )
    if not u:
        return lead
    first = u[0].upper() + u[1:] if u else u
    if first.endswith((".", "!", "?")):
        frag = first
    else:
        frag = first + "."
    return f"{frag} {lead}"


class ThinkBlockStreamSplitter:
    """Strip ``think`` / ``redacted_thinking`` blocks incrementally during streaming."""

    _HOLD = 96

    __slots__ = ("_pending", "_in_think")

    def __init__(self) -> None:
        self._pending = ""
        self._in_think = False

    def feed(self, chunk: str) -> str:
        self._pending += chunk
        out: list[str] = []
        while True:
            if not self._in_think:
                m = _REASONING_OPEN_RE.search(self._pending)
                if m:
                    out.append(self._pending[: m.start()])
                    self._pending = self._pending[m.end() :]
                    self._in_think = True
                    continue
                if len(self._pending) > self._HOLD:
                    take = len(self._pending) - self._HOLD
                    out.append(self._pending[:take])
                    self._pending = self._pending[take:]
                break
            m = _REASONING_CLOSE_RE.search(self._pending)
            if m:
                self._pending = self._pending[m.end() :]
                self._in_think = False
                continue
            if len(self._pending) > self._HOLD:
                self._pending = self._pending[-self._HOLD :]
            break
        return "".join(out)

    def flush(self) -> str:
        if self._in_think:
            self._pending = ""
            return ""
        t = self._pending
        self._pending = ""
        return t


class ThinkDualStreamSplitter:
    """Split model stream into visible reasoning vs assistant text (both incrementally)."""

    _HOLD = 96

    __slots__ = ("_pending", "_in_think")

    def __init__(self) -> None:
        self._pending = ""
        self._in_think = False

    def feed(self, chunk: str) -> tuple[str, str, bool]:
        """Return ``(thinking_piece, assistant_piece, thinking_just_closed)``."""
        self._pending += chunk
        think_out: list[str] = []
        asst_out: list[str] = []
        closed = False
        while True:
            if not self._in_think:
                m = _REASONING_OPEN_RE.search(self._pending)
                if m:
                    pre = self._pending[: m.start()]
                    if pre:
                        asst_out.append(pre)
                    self._pending = self._pending[m.end() :]
                    self._in_think = True
                    continue
                if len(self._pending) > self._HOLD:
                    take = len(self._pending) - self._HOLD
                    asst_out.append(self._pending[:take])
                    self._pending = self._pending[take:]
                break
            m = _REASONING_CLOSE_RE.search(self._pending)
            if m:
                think_body = self._pending[: m.start()]
                if think_body:
                    think_out.append(think_body)
                self._pending = self._pending[m.end() :]
                self._in_think = False
                closed = True
                continue
            if len(self._pending) > self._HOLD:
                take = len(self._pending) - self._HOLD
                think_out.append(self._pending[:take])
                self._pending = self._pending[take:]
            break
        return "".join(think_out), "".join(asst_out), closed

    def flush(self) -> tuple[str, str]:
        if self._in_think:
            tail = self._pending
            self._pending = ""
            return tail, ""
        rest = self._pending
        self._pending = ""
        return "", rest


class AssistantPublicStreamSplitter:
    """Drop banner + timetable lines from streamed assistant text (chat bubble only)."""

    __slots__ = ("_buf", "_phase")

    def __init__(self) -> None:
        self._buf = ""
        self._phase = "before"  # before | schedule | after

    def _emit_line(self, line: str) -> str:
        stripped = line.rstrip("\r\n").strip()

        if self._phase == "before":
            if stripped.startswith("╭"):
                self._phase = "schedule"
                return ""
            if stripped.startswith("*") and _SCHED_LINE_RE.match(stripped):
                self._phase = "schedule"
                return ""
            if stripped == "":
                return ""
            return line

        if self._phase == "schedule":
            if stripped.startswith(("╭", "│", "╰")):
                return ""
            if stripped.startswith("*") and (
                _SCHED_LINE_RE.match(stripped)
                or re.search(r"empty|nothing\s+left", stripped, re.I)
            ):
                return ""
            if stripped == "":
                return ""
            self._phase = "after"
            return line

        return line

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        parts: list[str] = []
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line = self._buf[: nl + 1]
            self._buf = self._buf[nl + 1 :]
            emitted = self._emit_line(line)
            if emitted:
                parts.append(emitted)
        return "".join(parts)

    def flush(self) -> str:
        if not self._buf:
            return ""
        if self._phase == "after":
            rest = self._buf
            self._buf = ""
            return rest
        # Still in schedule / before — do not leak partial banner into chat.
        self._buf = ""
        return ""


_SCHED_LITERAL_SGR_RE = (
    re.compile(r"\\x1b\[([\d;]+)m"),
    re.compile(r"\\033\[([\d;]+)m"),
)


def normalize_scheduler_terminal_escapes(text: str) -> str:
    """Turn literal ``\\x1b[…`` / ``\\033[…`` sequences into real CSI SGR bytes."""
    if not text:
        return text
    t = text
    for rx in _SCHED_LITERAL_SGR_RE:
        t = rx.sub(lambda m: f"\x1b[{m.group(1)}m", t)
    return t


def _encoded_length(tokenizer: Any, text: str) -> int:
    enc = cast(_SupportsEncode, tokenizer).encode(text)
    if isinstance(enc, list):
        return len(enc)
    ids = getattr(enc, "input_ids", None)
    if ids is None:
        return len(enc)
    row = ids[0] if ids and isinstance(ids[0], list) else ids
    return len(row)


def build_prompt(
    tokenizer: Any,
    system: str,
    pairs: list[tuple[Role, str]],
    *,
    enable_thinking: bool | None = None,
) -> str:
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for role, text in pairs:
        messages.append({"role": role, "content": text})
    apply = getattr(tokenizer, "apply_chat_template", None)
    if callable(apply):
        try:
            kwargs: dict[str, Any] = {
                "tokenize": False,
                "add_generation_prompt": True,
            }
            if enable_thinking is not None:
                kwargs["enable_thinking"] = enable_thinking
            return apply(messages, **kwargs)
        except TypeError:
            try:
                return apply(messages, tokenize=False, add_generation_prompt=True)
            except Exception:
                pass
        except Exception:
            pass
    sys_line = system
    if enable_thinking is False:
        sys_line = system.rstrip() + "\n/no_think"
    buf = [f"<|system|>\n{sys_line}"]
    for role, text in pairs:
        buf.append(f"<|{role}|>\n{text}")
    buf.append("<|assistant|>\n")
    return "\n".join(buf)


def prompt_token_count(
    tokenizer: Any,
    system: str,
    pairs: list[tuple[Role, str]],
    *,
    enable_thinking: bool | None = None,
) -> int:
    return _encoded_length(
        tokenizer,
        build_prompt(tokenizer, system, pairs, enable_thinking=enable_thinking),
    )


def trim_history_pairs(pairs: list[tuple[Role, str]], max_messages: int) -> None:
    if max_messages <= 0:
        return
    while len(pairs) > max_messages:
        pairs.pop(0)


def prepare_day_scheduler_user_for_prompt(
    user_raw: str,
    *,
    host_context: str | None = None,
    clock_minutes: int | None = None,
    clock_date: date | None = None,
) -> tuple[str, int, date]:
    """Apply completion meta + facts + hard-clock (+ optional habit context).

    Completion detection and `[Facts — …]` use **only** ``user_raw`` so habit titles
    cannot accidentally trigger scheduling fact injectors.

    When ``clock_minutes`` and ``clock_date`` are set (typically from the browser calendar),
    they override the server's local clock for scheduling semantics.
    """
    user_visible = user_raw.strip()
    user_for_prompt = user_visible

    completion_labels = _scheduler_completion_labels(user_visible)
    if clock_minutes is not None and clock_date is not None:
        wall_minutes, clock_d = clock_minutes % (24 * 60), clock_date
    else:
        wall_minutes, clock_d = _local_wall_clock_snapshot()

    if completion_labels:
        labels_fmt = ", ".join(repr(lab) for lab in completion_labels)
        floor = _minutes_to_ampm(wall_minutes)
        user_for_prompt = (
            f"{user_visible}\n\n"
            "[Meta — scheduler]\n"
            f"The user reports these are DONE (remove **every** matching task from the plan): "
            f"{labels_fmt}. "
            "They may have finished these **out of order** vs your last list—do **not** debate "
            "timestamps; trust them. "
            "**Keep every other item** from your last schedule unless the user **explicitly** "
            "said they finished that item too—including prep blocks like “get ready for work.” "
            "Those are **still owed**; **slide** their start times so the full day stays "
            "consistent with local NOW in the clock block (every line starts at or after "
            f"NOW). For **this** reply, local NOW is {floor} on "
            f"{clock_d.isoformat()}.\n"
            "Output only the full refreshed stylized TO DO banner plus bullets."
        )

    fact_sheet = _day_scheduler_user_fact_sheet(user_visible)
    if fact_sheet is not None:
        user_for_prompt = f"{fact_sheet}\n\n{user_for_prompt}"

    floor = _minutes_to_ampm(wall_minutes)
    prelude = ""
    if host_context and host_context.strip():
        prelude = (
            "[Context — active habits from Habit Builder]\n"
            + host_context.strip()
            + "\n\n"
        )

    user_for_prompt = (
        f"[Hard clock — this turn] Client local NOW = {floor} on {clock_d.isoformat()}.\n"
        "For bullets **without** an explicit `* [YYYY-MM-DD]` calendar prefix, times refer to "
        f"that same calendar day (**{clock_d.isoformat()}**) and `[time]` must be **≥ {floor}**.\n"
        "For obligations on another calendar day use `* [YYYY-MM-DD] [time] - …`, with times "
        "interpreted in the user's local timezone.\n\n"
        + prelude
        + user_for_prompt
    )
    return user_for_prompt, wall_minutes, clock_d


def _generate_plain_completion(
    *,
    model_m: Any,
    tokenizer: Any,
    prompt: str,
    max_tokens: int,
    gen_kw: dict[str, Any],
) -> str:
    from mlx_lm.generate import stream_generate

    parts: list[str] = []
    for resp in stream_generate(
        model_m,
        tokenizer,
        prompt,
        max_tokens=max_tokens,
        **gen_kw,
    ):
        parts.append(resp.text)
    return "".join(parts).strip()


def compress_history_for_budget(
    *,
    model_m: Any,
    tokenizer: Any,
    system: str,
    history: list[tuple[Role, str]],
    pending_user: str,
    context_limit: int,
    soft_fraction: float,
    reserve_tokens: int,
    keep_recent_messages: int,
    summarize_max_tokens: int,
    max_summarize_input_tokens: int,
    gen_kw: dict[str, Any],
    auto_summarize: bool,
    enable_thinking: bool | None = None,
) -> tuple[bool, int, int]:
    """Shrink ``history`` so the next prompt fits the soft token budget."""

    summarizer_system = (
        "You compress chat transcripts for ongoing context. Output a concise summary only: "
        "key facts, decisions, names, commitments, and open questions. "
        "No greeting or preamble."
    )

    budget = max(256, int(context_limit * soft_fraction) - max(0, reserve_tokens))

    if context_limit <= 0 or soft_fraction <= 0:
        return (
            False,
            prompt_token_count(
                tokenizer,
                system,
                history + [("user", pending_user)],
                enable_thinking=enable_thinking,
            ),
            budget,
        )

    target_pairs = history + [("user", pending_user)]
    ntok = prompt_token_count(tokenizer, system, target_pairs, enable_thinking=enable_thinking)
    if ntok <= budget:
        return False, ntok, budget

    mutated = False

    cap_in = max(512, min(max_summarize_input_tokens, budget))

    guard = 0
    while ntok > budget and guard < 24:
        guard += 1
        if len(history) <= keep_recent_messages:
            if not history:
                break
            if auto_summarize:
                drop_n = max(1, len(history) // 4) if len(history) >= 4 else 1
                del history[:drop_n]
                mutated = True
            else:
                history.pop(0)
                mutated = True
            ntok = prompt_token_count(
                tokenizer,
                system,
                history + [("user", pending_user)],
                enable_thinking=enable_thinking,
            )
            continue

        prefix_len = len(history) - keep_recent_messages
        prefix = history[:prefix_len]
        tail = history[prefix_len:]
        if auto_summarize and prefix:
            try:
                transcript_lines = [f"{role.upper()}: {text}" for role, text in prefix]
                while len(transcript_lines) > 2 and _encoded_length(
                    tokenizer,
                    "\n".join(transcript_lines),
                ) > cap_in:
                    transcript_lines = transcript_lines[max(1, len(transcript_lines) // 8) :]

                transcript = "\n".join(transcript_lines)
                summ_user = (
                    "Summarize this conversation excerpt for memory.\n\n"
                    + transcript
                )
                summ_prompt = build_prompt(
                    tokenizer,
                    summarizer_system,
                    [("user", summ_user)],
                    enable_thinking=enable_thinking,
                )
                summary = _generate_plain_completion(
                    model_m=model_m,
                    tokenizer=tokenizer,
                    prompt=summ_prompt,
                    max_tokens=summarize_max_tokens,
                    gen_kw=gen_kw,
                )
                if not summary:
                    summary = "(Earlier turns omitted — summary unavailable.)"
                marker = "[Earlier conversation — summarized]\n"
                history[:] = [
                    ("user", marker + summary),
                    (
                        "assistant",
                        "Understood; continuing with the recent messages below.",
                    ),
                ] + tail
                mutated = True
            except Exception:
                history[:] = tail
                mutated = True
        else:
            history[:] = tail
            mutated = True

        ntok = prompt_token_count(
            tokenizer,
            system,
            history + [("user", pending_user)],
            enable_thinking=enable_thinking,
        )

    return mutated, ntok, budget


def generate_day_scheduler_reply(
    *,
    user_raw: str,
    history: list[tuple[Role, str]],
    model_m: Any,
    tokenizer: Any,
    base_system_prompt: str,
    template_enable_thinking: bool | None,
    context_limit: int,
    soft_fraction: float,
    reserve_tokens: int,
    keep_recent_messages: int,
    summarize_max_tokens: int,
    max_summarize_input_tokens: int,
    gen_kw: dict[str, Any],
    auto_summarize: bool,
    max_tokens: int,
    strip_reasoning: bool,
    buffer_full_reply: bool,
    max_history_messages: int,
    host_context: str | None = None,
    client_clock_minutes: int | None = None,
    client_clock_date: date | None = None,
    on_stream_chunk: Callable[[str], None] | None = None,
    on_stream_thinking: Callable[[str], None] | None = None,
    on_thinking_closed: Callable[[], None] | None = None,
    hide_schedule_deltas: bool = False,
    on_compress: Callable[[bool, int, int], None] | None = None,
) -> tuple[bool, str, Any | None]:
    """Run one day-scheduler turn; mutates ``history``.

    Returns ``(ok, assistant_text, last_resp)``.
    """
    user_for_prompt, clock_minutes, clock_date = prepare_day_scheduler_user_for_prompt(
        user_raw,
        host_context=host_context,
        clock_minutes=client_clock_minutes,
        clock_date=client_clock_date,
    )
    clock_suffix = _day_scheduler_clock_suffix(
        clock_minutes=clock_minutes,
        clock_date=clock_date,
    )
    effective_system = base_system_prompt + clock_suffix

    did_compress, p_tokens, budget = compress_history_for_budget(
        model_m=model_m,
        tokenizer=tokenizer,
        system=effective_system,
        history=history,
        pending_user=user_for_prompt,
        context_limit=context_limit,
        soft_fraction=soft_fraction,
        reserve_tokens=reserve_tokens,
        keep_recent_messages=keep_recent_messages,
        summarize_max_tokens=summarize_max_tokens,
        max_summarize_input_tokens=max_summarize_input_tokens,
        gen_kw=gen_kw,
        auto_summarize=auto_summarize,
        enable_thinking=template_enable_thinking,
    )
    if on_compress is not None:
        on_compress(did_compress, p_tokens, budget)

    prompt = build_prompt(
        tokenizer,
        effective_system,
        history + [("user", user_for_prompt)],
        enable_thinking=template_enable_thinking,
    )

    from mlx_lm.generate import stream_generate

    stream_schedule = not buffer_full_reply
    ds_stream_strip = stream_schedule and strip_reasoning
    ds_stream_raw = stream_schedule and not strip_reasoning
    stream_tokens_live = ds_stream_raw
    stream_dual_reasoning = (
        stream_schedule
        and not strip_reasoning
        and on_stream_thinking is not None
    )

    buf: list[str] = []
    last_resp: Any | None = None
    gen_ok = False
    think_splitter = ThinkBlockStreamSplitter() if ds_stream_strip else None
    think_dual = ThinkDualStreamSplitter() if stream_dual_reasoning else None
    public_splitter = (
        AssistantPublicStreamSplitter()
        if stream_schedule and hide_schedule_deltas and on_stream_chunk is not None
        else None
    )

    def _route_public_assistant(stream_piece: str) -> None:
        if not stream_piece or on_stream_chunk is None:
            return
        if public_splitter is not None:
            chunk = public_splitter.feed(stream_piece)
            if not chunk:
                return
            on_stream_chunk(normalize_scheduler_terminal_escapes(chunk))
            return
        on_stream_chunk(normalize_scheduler_terminal_escapes(stream_piece))

    try:
        for resp in stream_generate(
            model_m,
            tokenizer,
            prompt,
            max_tokens=max_tokens,
            **gen_kw,
        ):
            last_resp = resp
            buf.append(resp.text)
            if think_dual is not None:
                t_piece, a_piece, think_closed_here = think_dual.feed(resp.text)
                if think_closed_here and on_thinking_closed is not None:
                    on_thinking_closed()
                if t_piece and on_stream_thinking is not None:
                    on_stream_thinking(normalize_scheduler_terminal_escapes(t_piece))
                _route_public_assistant(a_piece)
            elif think_splitter is not None:
                piece = think_splitter.feed(resp.text)
                _route_public_assistant(piece)
            elif stream_tokens_live and on_stream_chunk is not None:
                _route_public_assistant(resp.text)
        gen_ok = True
    except Exception as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return False, "", last_resp

    reply = "".join(buf)
    if strip_reasoning:
        reply = strip_reasoning_blocks(reply)
    if gen_ok:
        reply = normalize_scheduler_terminal_escapes(reply)
    if think_dual is not None:
        t_tail, a_tail = think_dual.flush()
        if t_tail and on_stream_thinking is not None:
            on_stream_thinking(normalize_scheduler_terminal_escapes(t_tail))
        _route_public_assistant(a_tail)
    elif think_splitter is not None:
        tail = think_splitter.flush()
        _route_public_assistant(tail)

    if public_splitter is not None and on_stream_chunk is not None:
        pub_tail = public_splitter.flush()
        if pub_tail:
            on_stream_chunk(normalize_scheduler_terminal_escapes(pub_tail))

    streamed = (
        stream_tokens_live
        or think_splitter is not None
        or think_dual is not None
    )
    if not streamed and on_stream_chunk is not None:
        full = normalize_scheduler_terminal_escapes(reply) if gen_ok else reply
        on_stream_chunk(full)

    history.append(("user", user_raw))
    history.append(("assistant", reply))
    trim_history_pairs(history, max_history_messages)
    return gen_ok, reply, last_resp


def load_day_scheduler_system_prompt(repo_root: Path | None = None) -> str:
    path = (repo_root or REPO_ROOT) / "prompts" / "day-scheduler-system.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return (
            "You are a helpful assistant. Answer briefly unless the user needs detail.\n\n"
            "Also act as a day scheduler; output a stylized TO DO banner then bullets as "
            "[time] - title - XhYm using the host local clock only."
        )
