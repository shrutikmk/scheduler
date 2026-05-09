"""Parse timetable bullets and planner-relative dates (host + import path)."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

_SAMPLES = Path(__file__).resolve().parent
if str(_SAMPLES) not in sys.path:
    sys.path.insert(0, str(_SAMPLES))

from mlx_day_scheduler_pipeline import strip_reasoning_blocks  # noqa: E402

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
_EMPTY_PLAN_RE = re.compile(r"^\*\s+\(empty\s+—\s+nothing left on today's plan\.\)\s*$", re.I)


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
    return "[Facts — planner targets]\n" + "\n".join(lines)


def iter_parsed_schedule_lines(
    assistant_text: str,
    *,
    default_plan_date: str,
) -> Iterator[ParsedTaskLine]:
    body = strip_reasoning_blocks(assistant_text)
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
) -> ScheduleValidationResult:
    """Validate strict scheduler output before trusting a fast model candidate."""
    body = strip_reasoning_blocks(assistant_text)
    reasons: list[str] = []
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]

    if not lines or not lines[0].startswith("╭"):
        reasons.append("missing stylized TO DO banner as first visible line")

    bullet_lines = [ln for ln in lines if ln.startswith("*")]
    if not bullet_lines:
        reasons.append("missing timetable bullet lines")
        return ScheduleValidationResult(False, tuple(reasons), ())

    if len(bullet_lines) == 1 and _EMPTY_PLAN_RE.match(bullet_lines[0]):
        return ScheduleValidationResult(not reasons, tuple(reasons), ())

    parsed: list[ParsedTaskLine] = []
    last_by_day: dict[str, int] = {}
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
        if plan_date == default_plan_date and client_minute_of_day is not None:
            if start_minutes < client_minute_of_day:
                reasons.append(f"task starts before client NOW: {title}")
        prev = last_by_day.get(plan_date)
        if prev is not None and start_minutes < prev:
            reasons.append(f"tasks are not chronological for {plan_date}")
        last_by_day[plan_date] = start_minutes
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
