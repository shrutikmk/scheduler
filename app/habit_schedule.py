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
# sum(8..90) — Phase 2 point cap (mirrors Habit Builder / day_scheduler UI).
PHASE2_MAX_POINTS = 4067
# Phase 1 cap = 28; program total points cap = 28 + PHASE2_MAX_POINTS (4095) — see Habit Builder.


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


def _phase2_marks_sorted_after_boundary(boundary_iso: str, marks: set[str]) -> list[str]:
    first_p2 = _add_days(boundary_iso, 1)
    return sorted(iso for iso in marks if _days_diff(first_p2, iso) >= 0)


def _contiguous_runs_from_sorted(sorted_asc: list[str]) -> list[tuple[str, str, int]]:
    if not sorted_asc:
        return []
    runs: list[tuple[str, str, int]] = []
    run_start = sorted_asc[0]
    prev = sorted_asc[0]
    length = 1
    for iso in sorted_asc[1:]:
        if _days_diff(prev, iso) == 1:
            length += 1
        else:
            runs.append((run_start, prev, length))
            run_start = iso
            length = 1
        prev = iso
    runs.append((run_start, prev, length))
    return runs


def _max_phase2_run_length(boundary_iso: str, marks: set[str]) -> int:
    asc = _phase2_marks_sorted_after_boundary(boundary_iso, marks)
    runs = _contiguous_runs_from_sorted(asc)
    return max((r[2] for r in runs), default=0)


def _terminal_phase2_run_length(boundary_iso: str, marks: set[str]) -> int:
    asc = _phase2_marks_sorted_after_boundary(boundary_iso, marks)
    runs = _contiguous_runs_from_sorted(asc)
    return runs[-1][2] if runs else 0


def _forgiving_phase2_points_and_complete(boundary_iso: str, marks: set[str]) -> tuple[int, bool]:
    """Terminal-run greedy milestones + 90-day shortcut (matches Habit Builder)."""
    if _max_phase2_run_length(boundary_iso, marks) >= 90:
        return PHASE2_MAX_POINTS, True
    r_len = _terminal_phase2_run_length(boundary_iso, marks)
    if r_len <= 0:
        return 0, False
    pos = 0
    m = 8
    earned = 0
    while m <= 90:
        rem = r_len - pos
        if rem >= m:
            pos += m
            earned += m
            m += 1
        else:
            earned += rem
            break
    capped = min(PHASE2_MAX_POINTS, earned)
    return capped, capped >= PHASE2_MAX_POINTS


def _strict_phase2_simulate_complete(
    boundary_iso: str, marks: set[str], max_scan_iso: str
) -> tuple[bool, bool]:
    """Return (complete, violation) matching Habit Builder ``simulatePhase2``."""
    first_p2 = _add_days(boundary_iso, 1)
    day = first_p2
    target_len = 8
    run = 0
    need_rest = False
    complete = False
    violation = False
    guard = 0
    while _days_diff(day, max_scan_iso) <= 0 and not complete and guard < 12000:
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
    return complete, violation


def _habit_program_complete(boundary_iso: str, marks: set[str], scan_iso: str) -> bool:
    """True if forgiving path or strict ladder finished (for planner / off-quota)."""
    _fp, f_done = _forgiving_phase2_points_and_complete(boundary_iso, marks)
    if f_done:
        return True
    strict_done, _viol = _strict_phase2_simulate_complete(boundary_iso, marks, scan_iso)
    return strict_done


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
        scan_iso = target_date_iso
        if marks_list:
            scan_iso = max(scan_iso, marks_list[-1])
        if _habit_program_complete(boundary, marks, scan_iso):
            continue
        state = _phase2_state_at_start(boundary, marks, target_date_iso)
        if state.before_phase2 or state.complete or state.need_rest:
            continue
        if state.violation:
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


def latest_phase1_deadline_iso(habit: dict[str, Any], current_iso: str) -> str | None:
    """Latest ISO date this habit must be logged to keep the phase-1 weekly quota.

    Semantics: looking at the earliest week whose quota is not yet satisfied
    (skipping weeks whose calendar window is fully in the past, since those are
    permanently short and can no longer be rescued), return the **latest** day
    inside that week's eligible window where logging is still enough to meet
    ``need - already_done`` more marks. If too few eligible days remain in that
    week, ``current_iso`` is returned (must log today). Returns ``None`` if
    phase 1 is already satisfied for this habit, or the snapshot lacks a usable
    start/current date.
    """
    start_raw = habit.get("start")
    if not isinstance(start_raw, str) or _parse_date(start_raw) is None:
        return None
    start_iso = start_raw
    if _parse_date(current_iso) is None:
        return None

    cursor_iso = current_iso if current_iso > start_iso else start_iso
    marks_set = set(_marked_dates(habit))
    marks_list = sorted(marks_set)
    actual, satisfied = _phase1_stats(start_iso, marks_list)
    if satisfied:
        return None

    for w in range(PHASE1_WEEKS):
        need = w + 1
        done = actual[w]
        if done >= need:
            continue
        week_start, week_end = _phase1_week_range(start_iso, w)
        if week_end < cursor_iso:
            continue
        eligible_start = max(cursor_iso, week_start, start_iso)
        if eligible_start > week_end:
            continue
        avail: list[str] = []
        d = eligible_start
        while d <= week_end:
            if d not in marks_set:
                avail.append(d)
            d = _add_days(d, 1)
        remaining = need - done
        if len(avail) < remaining:
            return cursor_iso
        return avail[len(avail) - remaining]
    return None


def _phase2_status_for_target(
    *, start_iso: str, marks_set: set[str], target_iso: str
) -> dict[str, Any]:
    """Return a status dict for phase-2 awareness on ``target_iso``."""
    marks_list = sorted(marks_set)
    boundary = _phase2_boundary_date(start_iso, marks_list)
    if boundary is None:
        return {"kind": "phase2-pending"}
    scan_iso = target_iso
    if marks_list:
        scan_iso = max(scan_iso, marks_list[-1])
    if _habit_program_complete(boundary, marks_set, scan_iso):
        return {"kind": "phase2-complete"}
    state = _phase2_state_at_start(boundary, marks_set, target_iso)
    if state.before_phase2:
        return {"kind": "phase2-before", "first_iso": _add_days(boundary, 1)}
    if state.violation:
        return {"kind": "phase2-violation"}
    if state.complete:
        return {"kind": "phase2-complete"}
    if state.need_rest:
        return {
            "kind": "phase2-rest",
            "next_active_iso": _add_days(target_iso, 1),
        }
    return {
        "kind": "phase2-active",
        "run": state.run,
        "target_len": state.target_len,
    }


def non_required_habits_context_block(
    snapshot: dict[str, Any], target_date_iso: str
) -> str | None:
    """Block listing every habit that is *not* required on ``target_date_iso``.

    The day-scheduler model uses this to suppress habits whose program rules do
    not require them today. Each entry includes the next deadline (phase 1) or
    the upcoming non-rest day (phase 2 rest), so the model can confirm the day
    is genuinely off-quota.
    """
    if _parse_date(target_date_iso) is None:
        return None
    habits = snapshot.get("habits")
    if not isinstance(habits, list):
        return None

    required_titles = {
        req.title for req in required_habits_for_date(snapshot, target_date_iso)
    }

    lines: list[str] = []
    for raw in habits:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "Habit").strip() or "Habit"
        if title in required_titles:
            continue
        start_raw = raw.get("start")
        if not isinstance(start_raw, str) or _parse_date(start_raw) is None:
            continue
        start_iso = start_raw
        if target_date_iso < start_iso:
            lines.append(
                f"- {title}: not yet started (program start {start_iso}); "
                f"skip on {target_date_iso}."
            )
            continue
        marks_set = set(_marked_dates(raw))
        if target_date_iso in marks_set:
            lines.append(
                f"- {title}: already logged on {target_date_iso}; skip."
            )
            continue
        marks_list = sorted(marks_set)
        actual, phase1_satisfied = _phase1_stats(start_iso, marks_list)
        if not phase1_satisfied:
            deadline = latest_phase1_deadline_iso(raw, target_date_iso)
            week_idx = _week_index_for_date(start_iso, target_date_iso)
            if 0 <= week_idx < PHASE1_WEEKS:
                done = actual[week_idx]
                need = week_idx + 1
                progress = f"phase 1 week {week_idx + 1}/7 is {done}/{need}"
            else:
                progress = "phase 1 in progress"
            if deadline is None or deadline == target_date_iso:
                lines.append(
                    f"- {title}: {progress}; latest deadline {target_date_iso} (today)."
                )
            else:
                lines.append(
                    f"- {title}: {progress}; latest deadline {deadline}; skip on {target_date_iso}."
                )
            continue
        status = _phase2_status_for_target(
            start_iso=start_iso, marks_set=marks_set, target_iso=target_date_iso
        )
        kind = status.get("kind")
        if kind == "phase2-before":
            first = status.get("first_iso", "")
            lines.append(
                f"- {title}: phase 2 starts {first}; skip on {target_date_iso}."
            )
        elif kind == "phase2-rest":
            nxt = status.get("next_active_iso", "")
            lines.append(
                f"- {title}: phase 2 REST day on {target_date_iso}; do NOT schedule. "
                f"Resumes {nxt}."
            )
        elif kind == "phase2-complete":
            lines.append(f"- {title}: phase 2 complete; do NOT schedule.")
        elif kind == "phase2-violation":
            lines.append(
                f"- {title}: phase 2 rest-day violation pending; "
                f"skip on {target_date_iso} until corrected."
            )
        elif kind == "phase2-pending":
            lines.append(f"- {title}: phase 2 boundary unset; skip on {target_date_iso}.")
        # phase2-active should already be in required_titles

    if not lines:
        return None

    header = f"[Habit Builder — not required on {target_date_iso}]"
    footer = (
        "Habits in this list are off-quota for this calendar day. Do NOT add a "
        "timetable bullet for any of them on " + target_date_iso + "; they will "
        "appear under [Required habits — must schedule if absent] on the day they become due."
    )
    return "\n".join([header, *lines, "", footer])


def habits_snapshot_with_required_rows(
    snapshot: dict[str, Any],
    target_date_iso: str,
) -> dict[str, Any]:
    """Copy of the habits SQLite snapshot plus deterministic rows for ``target_date_iso``."""
    reqs = required_habits_for_date(snapshot, target_date_iso)
    out = dict(snapshot)
    out["required_for_planner_date"] = [
        {
            "title": r.title,
            "duration_minutes": r.duration_minutes,
            "reason": r.reason,
            "target_date": r.target_date,
        }
        for r in reqs
    ]
    return out


__all__ = [
    "DEFAULT_HABIT_MINUTES",
    "HabitRequirement",
    "habits_snapshot_with_required_rows",
    "latest_phase1_deadline_iso",
    "non_required_habits_context_block",
    "required_habits_context_block",
    "required_habits_for_date",
]
