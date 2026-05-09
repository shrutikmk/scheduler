"""Deterministic habit schedule requirements for the day scheduler.

This mirrors the Habit Builder progression rules closely enough for prompt
context: phase 1 weekly quotas, then phase 2 streak/rest-day progression.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

DEFAULT_HABIT_MINUTES = 20
PHASE1_WEEKS = 7


@dataclass(frozen=True)
class HabitRequirement:
    title: str
    target_date: str
    duration_minutes: int
    reason: str


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _add_days(iso: str, delta: int) -> str:
    d = date.fromisoformat(iso)
    return (d + timedelta(days=delta)).isoformat()


def _days_diff(a_iso: str, b_iso: str) -> int:
    return (date.fromisoformat(b_iso) - date.fromisoformat(a_iso)).days


def _sunday_of_week_containing(iso: str) -> str:
    d = date.fromisoformat(iso)
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def _week_index_for_date(start_iso: str, day_iso: str) -> int:
    if _days_diff(start_iso, day_iso) < 0:
        return -1
    start_sun = _sunday_of_week_containing(start_iso)
    day_sun = _sunday_of_week_containing(day_iso)
    w = _days_diff(start_sun, day_sun) // 7
    return w if 0 <= w < PHASE1_WEEKS else -1


def _phase1_week_range(start_iso: str, week_idx: int) -> tuple[str, str]:
    week_start = _add_days(_sunday_of_week_containing(start_iso), week_idx * 7)
    return week_start, _add_days(week_start, 6)


def _marked_dates(habit: dict[str, Any]) -> list[str]:
    raw_days = habit.get("days")
    if not isinstance(raw_days, dict):
        return []
    out: list[str] = []
    for key, val in raw_days.items():
        if val and isinstance(key, str) and _parse_date(key) is not None:
            out.append(key)
    return sorted(out)


def _phase1_stats(start_iso: str, marks: list[str]) -> tuple[list[int], bool]:
    actual = [0] * PHASE1_WEEKS
    for iso in marks:
        if _days_diff(start_iso, iso) < 0:
            continue
        w = _week_index_for_date(start_iso, iso)
        if 0 <= w < PHASE1_WEEKS:
            actual[w] += 1
    satisfied = all(actual[w] >= w + 1 for w in range(PHASE1_WEEKS))
    return actual, satisfied


def _phase2_boundary_date(start_iso: str, marks: list[str]) -> str | None:
    by_week: list[list[str]] = [[] for _ in range(PHASE1_WEEKS)]
    for iso in marks:
        if _days_diff(start_iso, iso) < 0:
            continue
        w = _week_index_for_date(start_iso, iso)
        if 0 <= w < PHASE1_WEEKS:
            by_week[w].append(iso)
    for week_marks in by_week:
        week_marks.sort()

    boundary: str | None = None
    for w, week_marks in enumerate(by_week):
        need = w + 1
        if len(week_marks) < need:
            return None
        d = week_marks[need - 1]
        if boundary is None or d > boundary:
            boundary = d
    return boundary


@dataclass(frozen=True)
class _Phase2State:
    target_len: int
    run: int
    need_rest: bool
    violation: bool
    complete: bool
    before_phase2: bool


def _phase2_state_at_start(boundary_iso: str, marks: set[str], target_iso: str) -> _Phase2State:
    first_phase2 = _add_days(boundary_iso, 1)
    if target_iso < first_phase2:
        return _Phase2State(8, 0, False, False, False, True)

    day = first_phase2
    target_len = 8
    run = 0
    need_rest = False
    complete = False
    violation = False
    guard = 0
    while day < target_iso and not complete and not violation and guard < 12000:
        guard += 1
        done = day in marks
        if need_rest:
            if done:
                violation = True
                break
            need_rest = False
            if target_len < 90:
                target_len += 1
        elif done:
            run += 1
            if run == target_len:
                run = 0
                if target_len == 90:
                    complete = True
                else:
                    need_rest = True
        else:
            run = 0
        day = _add_days(day, 1)
    return _Phase2State(target_len, run, need_rest, violation, complete, False)


def _phase1_requires_date(
    *,
    title: str,
    start_iso: str,
    target_iso: str,
    marks: set[str],
    actual: list[int],
    duration_minutes: int,
) -> HabitRequirement | None:
    week_idx = _week_index_for_date(start_iso, target_iso)
    if week_idx < 0:
        return None

    need = week_idx + 1
    done_count = actual[week_idx]
    if done_count >= need:
        return None

    week_start, week_end = _phase1_week_range(start_iso, week_idx)
    remaining_unlogged = 0
    d = max(target_iso, start_iso, week_start)
    while d <= week_end:
        if d not in marks:
            remaining_unlogged += 1
        d = _add_days(d, 1)

    remaining_needed = need - done_count
    if remaining_unlogged > remaining_needed:
        return None

    return HabitRequirement(
        title=title,
        target_date=target_iso,
        duration_minutes=duration_minutes,
        reason=(
            f"phase 1 week {week_idx + 1}/7 is {done_count}/{need}; "
            f"{remaining_needed} mark(s) still needed with "
            f"{remaining_unlogged} eligible day(s) left"
        ),
    )


def required_habits_for_date(
    snapshot: dict[str, Any],
    target_date_iso: str,
    *,
    default_minutes: int = DEFAULT_HABIT_MINUTES,
) -> list[HabitRequirement]:
    """Return only habits that must be scheduled on ``target_date_iso`` to stay on track."""
    if _parse_date(target_date_iso) is None:
        return []

    habits = snapshot.get("habits")
    if not isinstance(habits, list):
        return []

    out: list[HabitRequirement] = []
    for raw in habits:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "Habit").strip() or "Habit"
        start_raw = raw.get("start")
        if not isinstance(start_raw, str) or _parse_date(start_raw) is None:
            continue
        start_iso = start_raw
        if target_date_iso < start_iso:
            continue

        marks = set(_marked_dates(raw))
        if target_date_iso in marks:
            continue

        try:
            duration_minutes = max(1, int(raw.get("duration_minutes") or default_minutes))
        except (TypeError, ValueError):
            duration_minutes = default_minutes
        marks_list = sorted(marks)
        actual, phase1_satisfied = _phase1_stats(start_iso, marks_list)

        if not phase1_satisfied:
            req = _phase1_requires_date(
                title=title,
                start_iso=start_iso,
                target_iso=target_date_iso,
                marks=marks,
                actual=actual,
                duration_minutes=duration_minutes,
            )
            if req is not None:
                out.append(req)
            continue

        boundary = _phase2_boundary_date(start_iso, marks_list)
        if boundary is None:
            continue
        state = _phase2_state_at_start(boundary, marks, target_date_iso)
        if state.before_phase2 or state.violation or state.complete or state.need_rest:
            continue
        out.append(
            HabitRequirement(
                title=title,
                target_date=target_date_iso,
                duration_minutes=duration_minutes,
                reason=(
                    f"phase 2 streak day; current run {state.run}/{state.target_len} "
                    "would reset if skipped"
                ),
            )
        )

    out.sort(key=lambda h: h.title.lower())
    return out


def required_habits_context_block(snapshot: dict[str, Any], target_date_iso: str) -> str | None:
    required = required_habits_for_date(snapshot, target_date_iso)
    if not required:
        return None

    lines = ["[Required habits — must schedule if absent]"]
    for req in required:
        dur = f"{req.duration_minutes // 60}h{req.duration_minutes % 60:02d}m"
        lines.append(f"- {req.title}: {dur} on {req.target_date}; {req.reason}.")
    lines.append(
        "\nThese are hard planner requirements from Habit Builder. Include each as a "
        "timetable bullet unless an equivalent pending saved task already covers it. "
        "Do not add habits that are already logged, not started, completed, or marked as rest days."
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_HABIT_MINUTES",
    "HabitRequirement",
    "required_habits_context_block",
    "required_habits_for_date",
]
