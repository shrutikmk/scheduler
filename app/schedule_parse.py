"""Parse timetable bullets and planner-relative dates (host + import path)."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from day_scheduler_pipeline import strip_reasoning_blocks  # noqa: E402

_LINE_ONLY_RE = re.compile(
    r"^\*\s*(?:\[(\d{4}-\d{2}-\d{2})\]\s+)?\[([^\]]+)\]\s*-\s*(.+?)\s*-\s*(\d+h\d+m)\s*$",
)

_DOW = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class ParsedTaskLine:
    plan_date_iso: str
    start_label: str
    title: str
    duration_label: str
    duration_minutes: int


@dataclass(frozen=True)
class ScheduleValidationResult:
    ok: bool
    reasons: tuple[str, ...]
    parsed_tasks: tuple[ParsedTaskLine, ...]


_TIME_LABEL_RE = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*(AM|PM)\s*$", re.IGNORECASE)

# Strict importer contract (after normalization everything uses a leading asterisk).
_EMPTY_PLAN_RE = re.compile(
    r"^\*\s+\(empty\s+—\s+nothing left on today's plan\.\)\s*$", re.I
)
_EMPTY_ANY_RE = re.compile(
    r"^\s*(?:\*\s+|[-+*]\s+|(?:\d+[.)])\s+)?\(empty\s+—\s+nothing left on today's plan\.\)\s*$",
    re.I,
)

_LIST_MARKER_PREFIX_RE = re.compile(r"^(?:[-+*]\s+|(?:\d+)[.)]\s+)")
_MD_SEP = r"(?:-|—|\u2013)\s*"  # hyphen, em dash, or en dash
_MD_PROBE_CORE_RE = re.compile(
    rf"^(?:\[(\d{{4}}-\d{{2}}-\d{{2}})\]\s+)?\[([^\]]+)\]\s*{_MD_SEP}(.+?)\s*{_MD_SEP}(\d+h\d+m)\s*$",
)


def _strip_leading_md_list_tokens(s: str) -> str:
    t = s.lstrip()
    while True:
        m = _LIST_MARKER_PREFIX_RE.match(t)
        if not m:
            break
        t = t[m.end() :].lstrip()
        if t.startswith("*") and not t.startswith("**"):
            t = t[1:].lstrip()
    return t.strip()


def _consume_line_start_md_stars(ls: str) -> str:
    """Strip Markdown bold wrappers (`` `` or `` *[``…) after list-marker removal."""
    t = ls.strip()
    while True:
        if t.startswith("**"):
            t = t[2:].lstrip()
        elif len(t) > 1 and t.startswith("*") and t[1] in "[(":
            t = t[1:].lstrip()
        else:
            break
    return t


def _markdownish_line_to_strict_star(ls: str) -> str | None:
    """Map markdown-style timetable bullets onto the importer's strict ``* [...] ...`` shape."""
    s = ls.strip()
    if not s or s.startswith(("#", ">", "|", "```")):
        return None
    if _EMPTY_ANY_RE.match(s):
        return "* (empty — nothing left on today's plan.)"

    stripped = _consume_line_start_md_stars(_strip_leading_md_list_tokens(s))
    core = stripped.replace("**", "")
    core = re.sub(r"\s+", " ", core).strip()

    md_m = _MD_PROBE_CORE_RE.match(core)
    if not md_m:
        return None
    d_iso, time_lab, title, dur = (
        md_m.group(1),
        md_m.group(2).strip(),
        md_m.group(3).strip(),
        md_m.group(4).strip(),
    )
    if d_iso:
        return f"* [{d_iso}] [{time_lab}] - {title} - {dur}"
    return f"* [{time_lab}] - {title} - {dur}"


def _scheduler_header_ok(lines: list[str]) -> bool:
    """Legacy Unicode banner OR leading Markdown heading / schedule fence opener."""
    if not lines:
        return False
    first = lines[0].strip()
    if first.startswith("╭"):
        return True
    if re.match(r"^#{1,6}\s+\S", first):
        return True
    mf = re.match(r"^\s*`{3,}\s*(\w+)", first)
    if mf and mf.group(1).strip().lower() in {"schedule", "plan"}:
        return True
    return False


def _normalize_task_line_duration(line: str) -> str:
    """Rewrite trailing shorthand durations so strict `_LINE_ONLY_RE` can match."""
    pad = line[: len(line) - len(line.lstrip())]
    ls = line.strip()
    if not ls.startswith("*"):
        canon = _markdownish_line_to_strict_star(ls)
        if canon is None:
            return line
        ls = canon

    canon_empty = "* (empty — nothing left on today's plan.)"
    if _EMPTY_PLAN_RE.match(ls):
        return pad + canon_empty
    if _LINE_ONLY_RE.match(ls):
        return pad + ls
    m = re.search(r"-\s*(\d{1,4})\s*m\s*$", ls)
    if m:
        total = int(m.group(1))
        if total >= 24 * 60:
            return line
        h, mi = divmod(total, 60)
        repl = f"{h}h{mi:02d}m"
        new_ls = ls[: m.start()] + f"- {repl}"
        return pad + new_ls
    m2 = re.search(r"-\s*(\d{1,2})\s*h\s*$", ls)
    if m2:
        hrs = int(m2.group(1))
        if 1 <= hrs <= 24:
            repl = f"{hrs}h00m"
            new_ls = ls[: m2.start()] + f"- {repl}"
            return pad + new_ls
    if ls != line.strip():
        return pad + ls
    return line


def normalize_schedule_line_for_parser(line: str) -> str:
    """Normalize a single timetable-related line before strict matching."""
    return _normalize_task_line_duration(line)


def normalize_schedule_bullets_for_parser(assistant_text: str) -> str:
    """Strip reasoning, then fix common duration shorthand on task bullets (parser lint)."""
    body = strip_reasoning_blocks(assistant_text)
    out_lines: list[str] = []
    for line in body.splitlines():
        out_lines.append(_normalize_task_line_duration(line))
    return "\n".join(out_lines).strip()


def duration_to_minutes(label: str) -> int | None:
    s = label.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)h(\d+)m", s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def start_label_to_minutes(label: str) -> int | None:
    m = _TIME_LABEL_RE.match(label.strip())
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    ap = m.group(3).upper()
    if ap == "PM":
        hour24 = 12 if hour == 12 else hour + 12
    else:
        hour24 = 0 if hour == 12 else hour
    return hour24 * 60 + minute


def infer_planner_date_hints(user_text: str, *, anchor_date_iso: str) -> list[str]:
    """Resolve obvious relative phrases to concrete YYYY-MM-DD (client-local anchor)."""
    try:
        y, mo, d = map(int, anchor_date_iso.split("-"))
        anchor = date(y, mo, d)
    except ValueError:
        return []

    s = user_text.strip()
    if not s:
        return []
    low = s.lower()

    hinted: list[date] = []
    tomorrow = anchor + timedelta(days=1)

    for mm in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", s):
        try:
            hinted.append(date.fromisoformat(mm.group(1)))
        except ValueError:
            pass

    if "tomorrow" in low or re.search(r"\btmw\b", low):
        hinted.append(tomorrow)

    if "next week" in low:
        hinted.append(anchor + timedelta(days=7))

    for name, dow in _DOW.items():
        pat = rf"(?i)\bthis\s+(?:coming\s+)?{name}\b"
        if re.search(pat, low):
            delta = (dow - anchor.weekday()) % 7
            target = anchor + timedelta(days=(delta if delta != 0 else 7))
            hinted.append(target)

    if ("today" in low or "tonight" in low or "this evening" in low) and "tomorrow" not in low:
        hinted.append(anchor)

    return sorted({d.isoformat() for d in hinted})


def planner_facts_injection(user_text: str, *, anchor_date_iso: str) -> str | None:
    hints = infer_planner_date_hints(user_text, anchor_date_iso=anchor_date_iso)
    if not hints:
        return None
    lines = [
        f"- **Planner targets** (resolved for client local anchor **{anchor_date_iso}**):",
        f"  - Calendar day(s): {', '.join(hints)}.",
        (
            "  - For obligations on a **future calendar day**, each bullet MUST include the "
            "date prefix `* [YYYY-MM-DD] [time] - …`."
        ),
    ]
    try:
        anchor_d = date.fromisoformat(anchor_date_iso)
    except ValueError:
        anchor_d = None
    if anchor_d is not None:
        future = False
        for h in hints:
            try:
                if date.fromisoformat(h) > anchor_d:
                    future = True
                    break
            except ValueError:
                continue
        if future:
            lines.append(
                "  - **Machine format:** end every task line with `NhMm` only (e.g. `0h30m`); "
                "never bare `30m`, `90m`, or `2h`."
            )
    return "[Facts — planner targets]\n" + "\n".join(lines)


def iter_parsed_schedule_lines(
    assistant_text: str,
    *,
    default_plan_date: str,
) -> Iterator[ParsedTaskLine]:
    body = normalize_schedule_bullets_for_parser(assistant_text)
    for line in body.splitlines():
        ls = line.strip()
        if not ls.startswith("*"):
            continue
        m = _LINE_ONLY_RE.match(ls)
        if not m:
            continue
        d_iso = m.group(1) or default_plan_date
        start_l = m.group(2).strip()
        title = m.group(3).strip()
        dur_l = m.group(4).strip()
        mins = duration_to_minutes(dur_l)
        if mins is None:
            continue
        yield ParsedTaskLine(d_iso, start_l, title, dur_l, mins)


def collect_tasks_with_dates(
    text: str,
    *,
    default_plan_date: str,
) -> tuple[list[str], list[ParsedTaskLine]]:
    seen_dates: list[str] = []
    pairs: list[ParsedTaskLine] = []
    for row in iter_parsed_schedule_lines(text, default_plan_date=default_plan_date):
        pairs.append(row)
        if row.plan_date_iso not in seen_dates:
            seen_dates.append(row.plan_date_iso)
    return seen_dates, pairs


def minutes_duration_label(total_minutes: int) -> str:
    return f"{total_minutes // 60}h{total_minutes % 60:02d}m"


def _required_habit_titles(host_context: str | None) -> list[str]:
    if not host_context or "[Required habits" not in host_context:
        return []
    out: list[str] = []
    in_block = False
    for line in host_context.splitlines():
        ls = line.strip()
        if ls.startswith("[Required habits"):
            in_block = True
            continue
        if in_block and ls.startswith("[") and not ls.startswith("[Required habits"):
            break
        if not in_block or not ls.startswith("- "):
            continue
        title = ls[2:].split(":", 1)[0].strip()
        if title:
            out.append(title)
    return out


def _norm_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def validate_schedule_response(
    assistant_text: str,
    *,
    default_plan_date: str,
    client_minute_of_day: int | None = None,
    host_context: str | None = None,
    client_anchor_date_iso: str | None = None,
) -> ScheduleValidationResult:
    """Validate strict scheduler output before trusting a fast model candidate.

    ``client_anchor_date_iso`` is the user's **today** (from UI clock). The
    "starts before client NOW" rule applies only to tasks on that calendar day.
    ``default_plan_date`` may be a future day for undated bullets while the NOW gate
    still uses the anchor day.
    """
    normalized = normalize_schedule_bullets_for_parser(assistant_text)
    reasons: list[str] = []
    lines = [ln.strip() for ln in normalized.splitlines() if ln.strip()]

    if not lines:
        reasons.append("empty assistant timetable")
    elif not _scheduler_header_ok(lines):
        reasons.append(
            "missing scheduler header — begin with Markdown heading (#/## …) "
            "or optional ```schedule fenced block opener, "
            "or legacy Unicode TO DO frame (╭…╮)."
        )

    bullet_lines = [ln for ln in lines if ln.startswith("*")]
    if not bullet_lines:
        reasons.append("missing timetable bullet lines")
        return ScheduleValidationResult(False, tuple(reasons), ())

    if len(bullet_lines) == 1 and _EMPTY_PLAN_RE.match(bullet_lines[0]):
        return ScheduleValidationResult(not reasons, tuple(reasons), ())

    parsed: list[ParsedTaskLine] = []
    last_start_by_day: dict[str, int] = {}
    last_end_by_day: dict[str, int] = {}
    for line in bullet_lines:
        m = _LINE_ONLY_RE.match(line)
        if not m:
            reasons.append(f"invalid task bullet format: {line[:120]}")
            continue
        plan_date = m.group(1) or default_plan_date
        start_label = m.group(2).strip()
        title = m.group(3).strip()
        duration_label = m.group(4).strip()
        duration_minutes = duration_to_minutes(duration_label)
        start_minutes = start_label_to_minutes(start_label)
        if duration_minutes is None:
            reasons.append(f"invalid duration: {duration_label}")
            continue
        if start_minutes is None:
            reasons.append(f"invalid time label: {start_label}")
            continue
        now_anchor_day = (client_anchor_date_iso or default_plan_date).strip()
        if (
            plan_date == now_anchor_day
            and client_minute_of_day is not None
        ):
            if start_minutes < client_minute_of_day:
                reasons.append(f"task starts before client NOW: {title}")
        prev_st = last_start_by_day.get(plan_date)
        if prev_st is not None and start_minutes < prev_st:
            reasons.append(f"tasks are not chronological for {plan_date}")
        prev_end = last_end_by_day.get(plan_date)
        if prev_end is not None and start_minutes < prev_end:
            reasons.append(
                f"tasks overlap on {plan_date}: {title!r} starts before the prior task ends"
            )
        last_start_by_day[plan_date] = start_minutes
        last_end_by_day[plan_date] = start_minutes + duration_minutes
        parsed.append(
            ParsedTaskLine(plan_date, start_label, title, duration_label, duration_minutes)
        )

    if not parsed:
        reasons.append("no valid parsed task bullets")

    parsed_titles = [_norm_text(p.title) for p in parsed]
    for habit_title in _required_habit_titles(host_context):
        habit_norm = _norm_text(habit_title)
        if habit_norm and not any(habit_norm in title for title in parsed_titles):
            reasons.append(f"missing required habit: {habit_title}")

    return ScheduleValidationResult(not reasons, tuple(reasons), tuple(parsed))
