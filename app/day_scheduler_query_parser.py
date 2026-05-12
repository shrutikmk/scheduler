"""Structured query extraction for day-scheduler (JSON-only LLM parse).

Pure helpers plus prompts for ``mlx_scheduler_llm_api``: no MLX imports here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

QUERY_PARSER_SYSTEM = (
    "You extract scheduling intent from ONE user turn (may include host lines such as "
    "'[Facts — planner targets]'). Output **JSON only**, no markdown fences or prose. "
    "Use exactly these keys:\n"
    '- "primary_plan_date_iso": string YYYY-MM-DD for the single calendar day the user '
    "mainly wants planned/updated, or JSON null if ambiguous, multi-day, not scheduling, "
    'or unclear.\n'
    '- "time_intent_summary": short English phrase (e.g. "no explicit times", "morning only", '
    '"after 3pm", user-mentioned clocks).\n'
    '- "estimated_event_count": non-negative integer estimate of distinct activities/events '
    "the user implied.\n"
    '- "count_disclaimer": one short sentence that this count is approximate and the final '
    "timetable may merge/split lines.\n"
    "Resolve relative dates using the anchor date provided in the user block."
)

DEFAULT_COUNT_DISCLAIMER = (
    "Approximate; the final timetable may merge or split activities."
)


@dataclass
class ParsedQuery:
    primary_plan_date_iso: str | None = None
    time_intent_summary: str = ""
    estimated_event_count: int | None = None
    count_disclaimer: str = field(default_factory=lambda: DEFAULT_COUNT_DISCLAIMER)


def is_valid_iso_date(s: str) -> bool:
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


def extract_json_object(text: str) -> str | None:
    """Best-effort slice of the first top-level JSON object in ``text``."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    low = s.lower()
    if "```json" in low:
        start_fence = low.find("```json")
        inner = s[start_fence + 7 :]
        end_fence = inner.find("```")
        if end_fence != -1:
            inner = inner[:end_fence]
        s = inner.strip()
    elif s.startswith("```"):
        inner = s.split("\n", 1)[-1] if "\n" in s else ""
        end_fence = inner.rfind("```")
        if end_fence != -1:
            inner = inner[:end_fence]
        s = inner.strip()

    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def parsed_query_from_dict(d: dict[str, Any]) -> ParsedQuery:
    raw_date = d.get("primary_plan_date_iso")
    primary: str | None
    if raw_date is None or raw_date is False:
        primary = None
    elif isinstance(raw_date, str) and is_valid_iso_date(raw_date.strip()):
        primary = raw_date.strip()
    else:
        primary = None

    tsum = d.get("time_intent_summary")
    time_summary = tsum.strip() if isinstance(tsum, str) else ""

    n_raw = d.get("estimated_event_count")
    est: int | None
    if isinstance(n_raw, bool):
        est = None
    elif isinstance(n_raw, int):
        est = max(0, n_raw)
    elif isinstance(n_raw, float):
        est = max(0, int(n_raw))
    elif isinstance(n_raw, str) and n_raw.strip().isdigit():
        est = max(0, int(n_raw.strip()))
    else:
        est = None

    disc = d.get("count_disclaimer")
    disclaimer = disc.strip() if isinstance(disc, str) and disc.strip() else DEFAULT_COUNT_DISCLAIMER

    return ParsedQuery(
        primary_plan_date_iso=primary,
        time_intent_summary=time_summary,
        estimated_event_count=est,
        count_disclaimer=disclaimer,
    )


def parse_query_parser_completion_text(raw: str) -> ParsedQuery:
    """Parse model output into ``ParsedQuery``; fall back to empty parse on failure."""
    blob = extract_json_object(raw)
    if not blob:
        return ParsedQuery()
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ParsedQuery()
    if not isinstance(data, dict):
        return ParsedQuery()
    return parsed_query_from_dict(data)


def build_query_parser_user_block(
    *,
    content: str,
    client_clock_date_iso: str,
    client_clock_minutes: int | None,
    client_timezone_iana: str | None,
) -> str:
    wall = ""
    if client_clock_minutes is not None:
        h24, mi = divmod(int(client_clock_minutes) % (24 * 60), 60)
        ampm = "AM" if h24 < 12 else "PM"
        h12 = h24 % 12
        if h12 == 0:
            h12 = 12
        wall = f"{h12}:{mi:02d} {ampm}"
    tz_line = ""
    if isinstance(client_timezone_iana, str) and client_timezone_iana.strip():
        tz_line = f"\nIANA timezone (UI): {client_timezone_iana.strip()}"
    return (
        f"Anchor calendar date (interpret **today** / **tomorrow** against this): "
        f"**{client_clock_date_iso}**.\n"
        f"Local wall clock now: **{wall or 'unknown'}**.{tz_line}\n\n"
        "USER MESSAGE:\n"
        f"{content.strip()}"
    )


def format_query_parser_host_facts(parsed: ParsedQuery) -> str:
    """Host-only block injected before the main scheduler model."""
    day_bits = (
        f"**{parsed.primary_plan_date_iso}**"
        if parsed.primary_plan_date_iso
        else "**unspecified** (use anchor calendar / explicit `[YYYY-MM-DD]` on bullets)"
    )
    time_bits = parsed.time_intent_summary.strip() or "unspecified"
    count_line = (
        f"~**{parsed.estimated_event_count}**. {parsed.count_disclaimer}"
        if parsed.estimated_event_count is not None
        else f"unspecified. {parsed.count_disclaimer}"
    )
    return (
        "[Facts — query parser]\n"
        "Derived from the latest user message (approximate):\n"
        f"- Primary plan calendar day: {day_bits}.\n"
        f"- Time intent: {time_bits}.\n"
        f"- Estimated distinct activities: {count_line}\n"
        "- When primary plan day is a concrete **YYYY-MM-DD** above, put that day's tasks on "
        "that date (prefix each bullet with `[YYYY-MM-DD]` before the time bracket for that "
        "day). If bullets omit the date bracket, the importer uses the host default plan date.\n"
        "- Obey this block together with `[Facts — planner targets]` when both appear."
    )


def resolve_import_default_plan_date(parsed: ParsedQuery, *, client_clock_date_iso: str) -> str:
    """ISO date string for ``collect_tasks_with_dates(..., default_plan_date=…)``."""
    if parsed.primary_plan_date_iso and is_valid_iso_date(parsed.primary_plan_date_iso):
        return parsed.primary_plan_date_iso
    anchor = client_clock_date_iso.strip()
    return anchor if is_valid_iso_date(anchor) else date.today().isoformat()


def parsed_query_to_meta(parsed: ParsedQuery) -> dict[str, Any]:
    """Echo-safe subset for NDJSON ``done`` payloads."""
    return {
        "primary_plan_date_iso": parsed.primary_plan_date_iso,
        "time_intent_summary": parsed.time_intent_summary,
        "estimated_event_count": parsed.estimated_event_count,
        "count_disclaimer": parsed.count_disclaimer,
    }


def strip_redacted_thinking(text: str) -> str:
    """Drop ``<think>...</think>`` wrappers Qwen may emit before JSON."""
    if "<think>" not in text and "</think>" not in text:
        return text
    return re.sub(
        r"<think>[\s\S]*?</think>",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

