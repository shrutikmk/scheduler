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


def duration_to_minutes(label: str) -> int | None:
    s = label.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)h(\d+)m", s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


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
