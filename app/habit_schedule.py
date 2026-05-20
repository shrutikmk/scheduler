"""Deterministic habit schedule requirements for the day scheduler.

This mirrors the Habit Builder progression rules closely enough for prompt
context: phase 1 weekly quotas, then phase 2 streak/rest-day progression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

DEFAULT_HABIT_MINUTES = 20

# "10k steps", "12 k steps" — used only when ``duration_minutes`` is omitted on the habit row.
_STEPS_K_RE = re.compile(r"(?i)(\d+)\s*k\s*steps?\b")


def _habit_duration_minutes(raw: dict[str, Any], title: str, default_minutes: int) -> int:
    """Planner block length for a habit snapshot row (explicit wins; else infer from title)."""
    raw_dm = raw.get("duration_minutes")
    if raw_dm is not None and raw_dm != "":
        try:
            explicit = int(raw_dm)
        except (TypeError, ValueError):
            explicit = 0
        if explicit > 0:
            return max(1, explicit)
    m = _STEPS_K_RE.search(title)
    if m:
        thousands = int(m.group(1))
        # ~6 min per 1k walking steps (≈1h for 10k); sane bounds for planning.
        return max(30, min(240, thousands * 6))
    return default_minutes


PHASE1_WEEKS = 7
PHASE1_WEEKS_WORKNIGHT = 5
PHASE1_MAX_POINTS_WORKNIGHT = 15  # 1+2+3+4+5
# sum(8..90) — Phase 2 point cap (mirrors Habit Builder / day_scheduler UI).
PHASE2_MAX_POINTS = 4067
# Phase 1 cap = 28; program total points cap = 28 + PHASE2_MAX_POINTS (4095) — see Habit Builder.
# Calendar Phase 2: Leg 8 … Leg 90 each ends with one mandatory rest → 83 nominal rest days on-time path.
PHASE2_NOMINAL_REST_DAY_COUNT = 83


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


def nominal_phase2_rest_dates(phase2_start_iso: str) -> list[str]:
    """Ideal mandatory-rest calendar dates when every leg completes on schedule.

    Phase 2 day 1 is ``phase2_start_iso``. Leg ``n`` occupies ``n`` consecutive completed
    days; the mandatory rest is the following calendar day; the next leg begins the day after.

    Rest after Leg 8 is ``phase2_start + 8 days``; subsequent rests follow the cumulative rule.
    Returns ``PHASE2_NOMINAL_REST_DAY_COUNT`` (= 83) ISO dates when ``phase2_start_iso`` is valid.

    Worknight habits use a different discrete timeline; omit nominal rests for that mode.
    """
    if _parse_date(phase2_start_iso) is None:
        return []
    cursor = phase2_start_iso
    out: list[str] = []
    for n in range(8, 91):
        rest = _add_days(cursor, n)
        out.append(rest)
        cursor = _add_days(rest, 1)
    return out


def _phase2_leg_progress_from_nominal_rests(
    phase2_start_iso: str, marks: set[str], today_iso: str
) -> tuple[int, int] | None:
    """Calendar Phase 2 leg UI: filled marks in current nominal leg vs gap to next nominal rest.

    Uses ``max(today_iso, latest_phase2_mark)`` for segment selection so pre-logged next-leg days roll the
    displayed leg forward (e.g. L9 1/9 while calendar today is still the L8 rest).

    Returns ``(filled, gap)`` or ``None`` when nominal rests cannot be derived.
    """
    rests = nominal_phase2_rest_dates(phase2_start_iso)
    if not rests:
        return None
    anchor_iso = today_iso
    for iso in marks:
        if _days_diff(phase2_start_iso, iso) >= 0 and iso > anchor_iso:
            anchor_iso = iso
    leg_start = phase2_start_iso
    for rest in rests:
        if _days_diff(anchor_iso, rest) >= 0:
            gap = _days_diff(leg_start, rest)
            if gap < 8 or gap > 90:
                return None
            filled = sum(
                1
                for iso in marks
                if _days_diff(leg_start, iso) >= 0 and _days_diff(iso, rest) > 0
            )
            return (filled, gap)
        leg_start = _add_days(rest, 1)
    gap = 90
    rest_after = _add_days(leg_start, gap)
    filled = sum(
        1
        for iso in marks
        if _days_diff(leg_start, iso) >= 0 and _days_diff(iso, rest_after) > 0
    )
    return (filled, gap)


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


def _cheat_dates(habit: dict[str, Any]) -> list[str]:
    raw = habit.get("cheat_days")
    if not isinstance(raw, dict):
        return []
    out: list[str] = []
    for key, val in raw.items():
        if val and isinstance(key, str) and _parse_date(key) is not None:
            out.append(key)
    return sorted(out)


DEFAULT_TIME_TARGET_MINUTES = 20
MAX_TIME_TARGET_MINUTES = 240


def _normalize_habit_type(habit: dict[str, Any]) -> str:
    raw = str(habit.get("habit_type") or "").strip().lower()
    if raw == "worknight":
        return "default"
    if raw == "time":
        return "time"
    return "default"


def _is_worknight(habit: dict[str, Any]) -> bool:
    if habit.get("worknight_mode"):
        return True
    return str(habit.get("habit_type") or "").strip().lower() == "worknight"


def _is_time(habit: dict[str, Any]) -> bool:
    return _normalize_habit_type(habit) == "time"


def _time_target_minutes(habit: dict[str, Any]) -> int:
    raw = habit.get("time_target_minutes")
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = DEFAULT_TIME_TARGET_MINUTES
    return max(1, min(MAX_TIME_TARGET_MINUTES, n))


def _time_program_days(start_iso: str, n: int, worknight: bool) -> list[str]:
    """First *n* program days from ``start_iso`` (Sun–Thu only when *worknight*)."""
    if _parse_date(start_iso) is None or n < 1:
        return []
    out: list[str] = []
    cursor = _first_active_on_or_after(start_iso) if worknight else start_iso
    guard = 0
    while len(out) < n and guard < 10000:
        guard += 1
        if not worknight or _sun_thru_thu(cursor):
            out.append(cursor)
        if len(out) >= n:
            break
        cursor = _next_active_after(cursor) if worknight else _add_days(cursor, 1)
    return out


def _phase1_stats_time(
    start_iso: str, marks: list[str], n: int, worknight: bool
) -> tuple[list[str], int, bool]:
    program_days = _time_program_days(start_iso, n, worknight)
    if not program_days:
        return [], 0, False
    mark_set = set(marks)
    earned = sum(i + 1 for i, pd in enumerate(program_days) if pd in mark_set)
    satisfied = all(pd in mark_set for pd in program_days)
    return program_days, earned, satisfied


def _phase2_boundary_date_time(
    start_iso: str, marks: list[str], n: int, worknight: bool
) -> str | None:
    program_days, _, satisfied = _phase1_stats_time(start_iso, marks, n, worknight)
    if not satisfied or not program_days:
        return None
    return program_days[-1]


def _time_phase1_day_index(program_days: list[str], target_iso: str) -> int:
    try:
        return program_days.index(target_iso)
    except ValueError:
        return -1


def _time_duration_for_phase1_day(program_days: list[str], target_iso: str) -> int:
    idx = _time_phase1_day_index(program_days, target_iso)
    return idx + 1 if idx >= 0 else DEFAULT_TIME_TARGET_MINUTES


def _phase1_requires_date_time(
    *,
    title: str,
    start_iso: str,
    target_iso: str,
    marks: set[str],
    program_days: list[str],
) -> HabitRequirement | None:
    idx = _time_phase1_day_index(program_days, target_iso)
    if idx < 0:
        return None
    for i in range(idx):
        if program_days[i] not in marks:
            return None
    if target_iso in marks:
        return None
    mins = _time_duration_for_phase1_day(program_days, target_iso)
    need = idx + 1
    return HabitRequirement(
        title=title,
        target_date=target_iso,
        duration_minutes=mins,
        reason=f"time phase 1 day {need}/{len(program_days)} · {mins} min",
    )


def _latest_phase1_deadline_iso_time(habit: dict[str, Any], current_iso: str) -> str | None:
    start_raw = habit.get("start")
    if not isinstance(start_raw, str) or _parse_date(start_raw) is None:
        return None
    if _parse_date(current_iso) is None:
        return None
    start_iso = start_raw
    n = _time_target_minutes(habit)
    worknight = _is_worknight(habit)
    marks_set = set(_marked_dates(habit))
    program_days, _, satisfied = _phase1_stats_time(start_iso, sorted(marks_set), n, worknight)
    if satisfied or not program_days:
        return None
    cursor_iso = current_iso if current_iso > start_iso else start_iso
    for pd in program_days:
        if pd in marks_set:
            continue
        if pd >= cursor_iso:
            return pd
        return cursor_iso
    return None


def _week_index_uncapped(start_iso: str, day_iso: str) -> int:
    if _days_diff(start_iso, day_iso) < 0:
        return -1
    start_sun = _sunday_of_week_containing(start_iso)
    day_sun = _sunday_of_week_containing(day_iso)
    return _days_diff(start_sun, day_sun) // 7


def _week_has_cheat(start_iso: str, week_idx: int, cheats: set[str]) -> bool:
    week_start, week_end = _phase1_week_range(start_iso, week_idx)
    for c in cheats:
        if week_start <= c <= week_end:
            return True
    return False


def _sun_thru_thu(iso: str) -> bool:
    wd = date.fromisoformat(iso).weekday()
    return wd == 6 or wd <= 3


def _first_active_on_or_after(iso: str) -> str:
    d = date.fromisoformat(iso)
    for _ in range(14):
        s = d.isoformat()
        if _sun_thru_thu(s):
            return s
        d += timedelta(days=1)
    return iso


def _next_active_after(iso: str) -> str:
    return _first_active_on_or_after(_add_days(iso, 1))


def _cheat_week_sundays(cheats: set[str]) -> set[str]:
    return {_sunday_of_week_containing(c) for c in cheats}


def _phase1_stats_worknight(
    start_iso: str, marks: list[str], cheats: set[str]
) -> tuple[list[int], bool]:
    actual = [0] * PHASE1_WEEKS_WORKNIGHT
    for iso in marks:
        if _days_diff(start_iso, iso) < 0:
            continue
        w = _week_index_uncapped(start_iso, iso)
        if 0 <= w < PHASE1_WEEKS_WORKNIGHT:
            actual[w] += 1
    satisfied = True
    for w in range(PHASE1_WEEKS_WORKNIGHT):
        need = w + 1
        if actual[w] >= need or _week_has_cheat(start_iso, w, cheats):
            continue
        satisfied = False
    return actual, satisfied


def _phase2_boundary_date_worknight(start_iso: str, marks: list[str]) -> str | None:
    by_week: list[list[str]] = [[] for _ in range(PHASE1_WEEKS_WORKNIGHT)]
    for iso in marks:
        if _days_diff(start_iso, iso) < 0:
            continue
        w = _week_index_uncapped(start_iso, iso)
        if 0 <= w < PHASE1_WEEKS_WORKNIGHT:
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


def _phase2_marks_sorted_after_boundary_worknight(
    boundary_iso: str, marks: set[str]
) -> list[str]:
    first_p2 = _add_days(boundary_iso, 1)
    out = sorted(iso for iso in marks if _days_diff(first_p2, iso) >= 0 and _sun_thru_thu(iso))
    return out


def _contiguous_runs_worknight(sorted_asc: list[str]) -> list[tuple[str, str, int]]:
    if not sorted_asc:
        return []
    runs: list[tuple[str, str, int]] = []
    run_start = sorted_asc[0]
    prev = sorted_asc[0]
    length = 1
    for iso in sorted_asc[1:]:
        if _next_active_after(prev) == iso:
            length += 1
        else:
            runs.append((run_start, prev, length))
            run_start = iso
            length = 1
        prev = iso
    runs.append((run_start, prev, length))
    return runs


def _max_phase2_run_length_worknight(boundary_iso: str, marks: set[str]) -> int:
    asc = _phase2_marks_sorted_after_boundary_worknight(boundary_iso, marks)
    runs = _contiguous_runs_worknight(asc)
    return max((r[2] for r in runs), default=0)


def _terminal_phase2_run_length_worknight(boundary_iso: str, marks: set[str]) -> int:
    asc = _phase2_marks_sorted_after_boundary_worknight(boundary_iso, marks)
    runs = _contiguous_runs_worknight(asc)
    return runs[-1][2] if runs else 0


def _forgiving_phase2_points_and_complete_worknight(
    boundary_iso: str, marks: set[str]
) -> tuple[int, bool]:
    if _max_phase2_run_length_worknight(boundary_iso, marks) >= 90:
        return PHASE2_MAX_POINTS, True
    r_len = _terminal_phase2_run_length_worknight(boundary_iso, marks)
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


def _strict_phase2_simulate_complete_worknight(
    boundary_iso: str,
    marks: set[str],
    max_scan_iso: str,
    cheat_suns: set[str],
) -> tuple[bool, bool]:
    """Return (complete, violation)."""
    night = _first_active_on_or_after(_add_days(boundary_iso, 1))
    target_len = 8
    run = 0
    need_rest = False
    complete = False
    violation = False
    guard = 0
    while _days_diff(night, max_scan_iso) >= 0 and not complete and guard < 12000:
        guard += 1
        sun_night = _sunday_of_week_containing(night)
        frozen = sun_night in cheat_suns
        done = night in marks
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
        elif not frozen:
            run = 0
        night = _next_active_after(night)
    return complete, violation


def _habit_program_complete_worknight(
    boundary_iso: str, marks: set[str], scan_iso: str, cheat_suns: set[str]
) -> bool:
    _fp, fc = _forgiving_phase2_points_and_complete_worknight(boundary_iso, marks)
    if fc:
        return True
    strict_done, _viol = _strict_phase2_simulate_complete_worknight(
        boundary_iso, marks, scan_iso, cheat_suns
    )
    return strict_done


def _phase2_state_at_start_worknight(
    boundary_iso: str, marks: set[str], target_iso: str, cheat_suns: set[str]
) -> _Phase2State:
    first_p2 = _first_active_on_or_after(_add_days(boundary_iso, 1))
    if target_iso < first_p2:
        return _Phase2State(8, 0, False, False, False, True)

    night = first_p2
    target_len = 8
    run = 0
    need_rest = False
    violation = False
    complete = False
    guard = 0
    while _days_diff(night, target_iso) < 0 and not violation and guard < 12000:
        guard += 1
        sun_night = _sunday_of_week_containing(night)
        frozen = sun_night in cheat_suns
        done = night in marks
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
        elif not frozen:
            run = 0
        night = _next_active_after(night)
    return _Phase2State(target_len, run, need_rest, violation, complete, False)


def _phase1_requires_date_worknight(
    *,
    title: str,
    start_iso: str,
    target_iso: str,
    marks: set[str],
    actual: list[int],
    duration_minutes: int,
    cheats: set[str],
) -> HabitRequirement | None:
    if not _sun_thru_thu(target_iso):
        return None
    week_idx = _week_index_uncapped(start_iso, target_iso)
    if week_idx < 0 or week_idx >= PHASE1_WEEKS_WORKNIGHT:
        return None
    if _week_has_cheat(start_iso, week_idx, cheats):
        return None

    need = week_idx + 1
    done_count = actual[week_idx]
    if done_count >= need:
        return None

    week_start, week_end = _phase1_week_range(start_iso, week_idx)
    remaining_unlogged = 0
    d = max(target_iso, start_iso, week_start)
    while d <= week_end:
        if _sun_thru_thu(d) and d not in marks:
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
            f"worknight phase 1 week {week_idx + 1}/{PHASE1_WEEKS_WORKNIGHT} is {done_count}/{need}; "
            f"{remaining_needed} mark(s) still needed with "
            f"{remaining_unlogged} Sun–Thu eligible day(s) left"
        ),
    )


def _latest_phase1_deadline_iso_worknight(
    habit: dict[str, Any], current_iso: str
) -> str | None:
    start_raw = habit.get("start")
    if not isinstance(start_raw, str) or _parse_date(start_raw) is None:
        return None
    start_iso = start_raw
    if _parse_date(current_iso) is None:
        return None

    cursor_iso = current_iso if current_iso > start_iso else start_iso
    marks_set = set(_marked_dates(habit))
    cheats = set(_cheat_dates(habit))
    marks_list = sorted(marks_set)
    actual, satisfied = _phase1_stats_worknight(start_iso, marks_list, cheats)
    if satisfied:
        return None

    for w in range(PHASE1_WEEKS_WORKNIGHT):
        if _week_has_cheat(start_iso, w, cheats):
            continue
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
            if _sun_thru_thu(d) and d not in marks_set:
                avail.append(d)
            d = _add_days(d, 1)
        remaining = need - done
        if len(avail) < remaining:
            return cursor_iso
        return avail[len(avail) - remaining]
    return None


def _phase2_status_for_target_worknight(
    *,
    start_iso: str,
    marks_set: set[str],
    target_iso: str,
    cheat_suns: set[str],
) -> dict[str, Any]:
    marks_list = sorted(marks_set)
    boundary = _phase2_boundary_date_worknight(start_iso, marks_list)
    if boundary is None:
        return {"kind": "phase2-pending"}
    scan_iso = target_iso
    if marks_list:
        scan_iso = max(scan_iso, marks_list[-1])
    if _habit_program_complete_worknight(boundary, marks_set, scan_iso, cheat_suns):
        return {"kind": "phase2-complete"}
    state = _phase2_state_at_start_worknight(boundary, marks_set, target_iso, cheat_suns)
    if state.before_phase2:
        return {"kind": "phase2-before", "first_iso": _first_active_on_or_after(_add_days(boundary, 1))}
    if state.violation:
        return {"kind": "phase2-violation"}
    if state.complete:
        return {"kind": "phase2-complete"}
    if state.need_rest:
        nxt = target_iso
        for _ in range(14):
            nxt = _add_days(nxt, 1)
            if _sun_thru_thu(nxt):
                return {"kind": "phase2-rest", "next_active_iso": nxt}
        return {"kind": "phase2-rest", "next_active_iso": _add_days(target_iso, 1)}
    return {"kind": "phase2-active", "run": state.run, "target_len": state.target_len}


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
    while _days_diff(day, max_scan_iso) >= 0 and not complete and guard < 12000:
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


def _phase2_state_at_start_forgiving(
    boundary_iso: str, marks_set: set[str], day_iso: str
) -> _Phase2State:
    """Forgiving replay through ``day_iso`` (matches Habit Builder ``stateAtStartOfDayForgiving``)."""
    first_p2 = _add_days(boundary_iso, 1)
    if day_iso < first_p2:
        return _Phase2State(8, 0, False, False, False, True)

    day = first_p2
    target_len = 8
    run = 0
    need_rest = False
    violation = False
    guard = 0
    while day < day_iso and not violation and guard < 12000:
        guard += 1
        done = day in marks_set
        if need_rest:
            if done:
                if day < day_iso:
                    done = False
                else:
                    violation = True
                    break
            need_rest = False
            if target_len < 90:
                target_len += 1
        else:
            if done:
                run += 1
                if run == target_len:
                    run = 0
                    if target_len == 90:
                        pass
                    else:
                        need_rest = True
            else:
                run = 0
        day = _add_days(day, 1)
    return _Phase2State(target_len, run, need_rest, violation, False, False)


def _derive_strict_rest_days_calendar(
    boundary_iso: str, marks_set: set[str], horizon_anchor_iso: str
) -> set[str]:
    """Rest calendar days from strict ladder replay with actual marks (``deriveRestDaySet``)."""
    rest_days: set[str] = set()
    first_p2 = _add_days(boundary_iso, 1)
    day = first_p2
    target_len = 8
    run = 0
    need_rest = False
    complete = False
    max_scan = _add_days(horizon_anchor_iso, 365 * 5)
    guard = 0
    while _days_diff(day, max_scan) >= 0 and not complete and guard < 12000:
        guard += 1
        done = day in marks_set
        if need_rest:
            rest_days.add(day)
            if done:
                break
            need_rest = False
            if target_len < 90:
                target_len += 1
        else:
            if done:
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
    return rest_days


def _next_need_rest_start_iso_forgiving(
    boundary_iso: str, marks_set: set[str], start_from_iso: str
) -> str | None:
    d = start_from_iso
    for _ in range(800):
        st = _phase2_state_at_start_forgiving(boundary_iso, marks_set, d)
        if st.violation or st.before_phase2:
            return None
        if st.need_rest:
            return d
        d = _add_days(d, 1)
    return None


def _next_projected_rest_forward_calendar(
    _boundary_iso: str,
    marks_set: set[str],
    anchor_iso: str,
    st: _Phase2State,
) -> str | None:
    if st.before_phase2 or st.violation:
        return None
    target_len = st.target_len
    run = st.run
    need_rest = st.need_rest
    day = anchor_iso
    for _ in range(500):
        if need_rest:
            if day in marks_set:
                return None
            return day
        if _days_diff(day, anchor_iso) >= 0:
            done = day in marks_set
        else:
            done = True
        if done:
            run += 1
            if run == target_len:
                run = 0
                if target_len == 90:
                    return None
                need_rest = True
        else:
            run = 0
        day = _add_days(day, 1)
    return None


def _next_projected_rest_iso_calendar(
    boundary_iso: str, marks_set: set[str], anchor_iso: str
) -> str | None:
    first_p2 = _add_days(boundary_iso, 1)
    if _days_diff(first_p2, anchor_iso) >= 0:
        st = _phase2_state_at_start_forgiving(boundary_iso, marks_set, anchor_iso)
        fwd = _next_projected_rest_forward_calendar(boundary_iso, marks_set, anchor_iso, st)
        if fwd:
            return fwd

    day = first_p2
    target_len = 8
    run = 0
    need_rest = False
    complete = False
    max_scan = _add_days(anchor_iso, 500)
    guard = 0
    while _days_diff(day, max_scan) >= 0 and not complete and guard < 12000:
        guard += 1
        if need_rest:
            done = day in marks_set
            if done and _days_diff(day, anchor_iso) > 0:
                done = False
        elif _days_diff(day, anchor_iso) >= 0:
            done = day in marks_set
        else:
            done = True

        if need_rest:
            if done:
                return None
            if _days_diff(day, anchor_iso) >= 0:
                return day
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
    return None


def _effective_next_rest_iso_calendar(
    boundary_iso: str, marks_set: set[str], anchor_iso: str
) -> str | None:
    strict_rest_days = _derive_strict_rest_days_calendar(boundary_iso, marks_set, anchor_iso)
    nr: str | None = None
    for r in sorted(strict_rest_days):
        if r >= anchor_iso:
            nr = r
            break
    if nr is None:
        nr = _next_need_rest_start_iso_forgiving(boundary_iso, marks_set, anchor_iso)
    if nr is None:
        nr = _next_projected_rest_iso_calendar(boundary_iso, marks_set, anchor_iso)
    return nr


def _strict_phase2_scan_metrics(
    boundary_iso: str, marks_set: set[str], max_scan_iso: str
) -> tuple[int, bool, bool]:
    """Strict Phase 2 points earned (per logged streak day), complete, violation."""
    first_p2 = _add_days(boundary_iso, 1)
    day = first_p2
    target_len = 8
    run = 0
    need_rest = False
    complete = False
    violation = False
    earned = 0
    guard = 0
    while _days_diff(day, max_scan_iso) >= 0 and not complete and guard < 12000:
        guard += 1
        done = day in marks_set
        if need_rest:
            if done:
                violation = True
                break
            need_rest = False
            if target_len < 90:
                target_len += 1
        elif done:
            run += 1
            earned += 1
            if run == target_len:
                run = 0
                if target_len == 90:
                    complete = True
                else:
                    need_rest = True
        else:
            run = 0
        day = _add_days(day, 1)
    return earned, complete, violation


def _strict_phase2_scan_metrics_worknight(
    boundary_iso: str,
    marks_set: set[str],
    max_scan_iso: str,
    cheat_suns: set[str],
) -> tuple[int, bool, bool]:
    night = _first_active_on_or_after(_add_days(boundary_iso, 1))
    target_len = 8
    run = 0
    need_rest = False
    complete = False
    violation = False
    earned = 0
    guard = 0
    while _days_diff(night, max_scan_iso) >= 0 and not complete and guard < 12000:
        guard += 1
        sun_night = _sunday_of_week_containing(night)
        frozen = sun_night in cheat_suns
        done = night in marks_set
        if need_rest:
            if done:
                violation = True
                break
            need_rest = False
            if target_len < 90:
                target_len += 1
        elif done:
            run += 1
            earned += 1
            if run == target_len:
                run = 0
                if target_len == 90:
                    complete = True
                else:
                    need_rest = True
        elif not frozen:
            run = 0
        night = _next_active_after(night)
    return earned, complete, violation


def _phase1_current_week_ui_index(start_iso: str, anchor_iso: str, actual: list[int]) -> int:
    raw_w = _week_index_for_date(start_iso, anchor_iso)
    w = raw_w
    if w < 0:
        w = 0
    if w > 6:
        first_short = -1
        for i in range(PHASE1_WEEKS):
            if actual[i] < i + 1:
                first_short = i
                break
        w = first_short if first_short >= 0 else PHASE1_WEEKS - 1
    return w


def derive_habit_program_state(habit: dict[str, Any], anchor_iso: str | None = None) -> dict[str, Any]:
    """Structured derived program state for API/UI (calendar habits + worknight subset)."""
    if anchor_iso is None:
        anchor_iso = date.today().isoformat()
    if _parse_date(anchor_iso) is None:
        anchor_iso = date.today().isoformat()

    title = str(habit.get("title") or "Habit").strip() or "Habit"
    start_raw = habit.get("start")
    if not isinstance(start_raw, str) or _parse_date(start_raw) is None:
        return {
            "phase": "phase1",
            "anchor_iso": anchor_iso,
            "title": title,
            "error": "invalid_start",
            "phase1": None,
            "phase2": None,
        }

    start_iso = start_raw
    marks_list = _marked_dates(habit)
    marks_set = set(marks_list)

    if _is_time(habit):
        n = _time_target_minutes(habit)
        worknight = _is_worknight(habit)
        program_days, phase1_earned, phase1_satisfied = _phase1_stats_time(
            start_iso, marks_list, n, worknight
        )
        phase1_cap = n * (n + 1) // 2
        day_ui = 0
        for i, pd in enumerate(program_days):
            if pd <= anchor_iso:
                day_ui = i
            else:
                break
        if phase1_satisfied:
            day_ui = n - 1
        else:
            for i, pd in enumerate(program_days):
                if pd not in marks_set:
                    day_ui = i
                    break

        boundary = (
            _phase2_boundary_date_time(start_iso, marks_list, n, worknight)
            if phase1_satisfied
            else None
        )
        phase2_start = None
        if boundary is not None:
            phase2_start = (
                _first_active_on_or_after(_add_days(boundary, 1))
                if worknight
                else _add_days(boundary, 1)
            )

        scan_iso = anchor_iso
        if marks_list:
            scan_iso = max(scan_iso, marks_list[-1])
        if boundary is not None and phase2_start is not None:
            scan_iso = max(scan_iso, phase2_start)

        if worknight and boundary is not None:
            cheats = set(_cheat_dates(habit))
            cheat_suns = _cheat_week_sundays(cheats)
            forgiving_pts, forgiving_done = _forgiving_phase2_points_and_complete_worknight(
                boundary, marks_set
            )
            strict_earned, strict_complete, strict_violation = _strict_phase2_scan_metrics_worknight(
                boundary, marks_set, scan_iso, cheat_suns
            )
            program_done = bool(
                _habit_program_complete_worknight(boundary, marks_set, scan_iso, cheat_suns)
            )
        elif boundary is not None:
            cheat_suns = set()
            forgiving_pts, forgiving_done = _forgiving_phase2_points_and_complete(
                boundary, marks_set
            )
            strict_earned, strict_complete, strict_violation = _strict_phase2_scan_metrics(
                boundary, marks_set, scan_iso
            )
            program_done = bool(_habit_program_complete(boundary, marks_set, scan_iso))
        else:
            cheat_suns = set()
            forgiving_pts, forgiving_done = (0, False)
            strict_earned, strict_complete, strict_violation = (0, False, False)
            program_done = False

        if boundary is not None:
            forgiving_pts = max(forgiving_pts, strict_earned)

        if program_done:
            phase = "done"
        elif phase1_satisfied and boundary is not None:
            phase = "phase2"
        else:
            phase = "phase1"

        phase2_block = None
        if boundary is not None and phase2_start is not None:
            phase2_leg_ui_day = _add_days(anchor_iso, 1) if anchor_iso in marks_set else anchor_iso
            nominal: list[str] = []
            if not worknight:
                nominal = nominal_phase2_rest_dates(phase2_start)
            if worknight:
                st_tomorrow = _phase2_state_at_start_worknight(
                    boundary, marks_set, phase2_leg_ui_day, cheat_suns
                )
                cur_target = st_tomorrow.target_len if not st_tomorrow.before_phase2 else 8
                cur_run = st_tomorrow.run if not st_tomorrow.before_phase2 else 0
                if st_tomorrow.violation:
                    cur_target = None
                    cur_run = None
                eff_rest = None
            elif strict_violation:
                cur_target = None
                cur_run = None
                eff_rest = _effective_next_rest_iso_calendar(boundary, marks_set, anchor_iso)
            else:
                leg_prog = _phase2_leg_progress_from_nominal_rests(
                    phase2_start, marks_set, anchor_iso
                )
                if leg_prog is None:
                    cur_target = None
                    cur_run = None
                else:
                    cur_run, cur_target = leg_prog
                eff_rest = _effective_next_rest_iso_calendar(boundary, marks_set, anchor_iso)
            phase2_block = {
                "boundary_iso": boundary,
                "phase2_start_iso": phase2_start,
                "nominal_rest_dates": nominal,
                "strict": {
                    "violation": strict_violation,
                    "complete": strict_complete,
                    "earned_points": strict_earned,
                },
                "forgiving": {"points": forgiving_pts, "complete": forgiving_done},
                "effective_next_rest_iso": eff_rest if not worknight else None,
                "current_leg_target_len": cur_target,
                "current_leg_run_start_of_tomorrow": cur_run,
                "time_target_minutes": n,
            }

        return {
            "phase": phase,
            "anchor_iso": anchor_iso,
            "title": title,
            "habit_type": "time",
            "time_target_minutes": n,
            "worknight_mode": worknight,
            "phase1": {
                "program_days": program_days,
                "satisfied": phase1_satisfied,
                "current_day_ui_index": day_ui,
                "points_earned": phase1_earned,
                "points_cap": phase1_cap,
            },
            "phase2": phase2_block,
        }

    if _is_worknight(habit):
        cheats = set(_cheat_dates(habit))
        cheat_suns = _cheat_week_sundays(cheats)
        actual, phase1_satisfied = _phase1_stats_worknight(start_iso, marks_list, cheats)
        phase1_earned = 0
        for w in range(PHASE1_WEEKS_WORKNIGHT):
            need = w + 1
            cheat_w = _week_has_cheat(start_iso, w, cheats)
            eff = max(actual[w], need) if cheat_w else actual[w]
            phase1_earned += min(eff, need)

        raw_w = _week_index_uncapped(start_iso, anchor_iso)
        w_ui = raw_w
        if w_ui < 0:
            w_ui = 0
        if w_ui > PHASE1_WEEKS_WORKNIGHT - 1:
            first_short = -1
            for i in range(PHASE1_WEEKS_WORKNIGHT):
                ok = actual[i] >= i + 1 or _week_has_cheat(start_iso, i, cheats)
                if not ok:
                    first_short = i
                    break
            w_ui = first_short if first_short >= 0 else PHASE1_WEEKS_WORKNIGHT - 1

        boundary = (
            _phase2_boundary_date_worknight(start_iso, marks_list) if phase1_satisfied else None
        )
        phase2_start = (
            _first_active_on_or_after(_add_days(boundary, 1)) if boundary is not None else None
        )
        scan_iso = anchor_iso
        if marks_list:
            scan_iso = max(scan_iso, marks_list[-1])
        if boundary is not None and phase2_start is not None:
            scan_iso = max(scan_iso, phase2_start)
        forgiving_pts, forgiving_done = (
            _forgiving_phase2_points_and_complete_worknight(boundary, marks_set)
            if boundary is not None
            else (0, False)
        )
        strict_earned, strict_complete, strict_violation = (
            _strict_phase2_scan_metrics_worknight(boundary, marks_set, scan_iso, cheat_suns)
            if boundary is not None
            else (0, False, False)
        )
        if boundary is not None:
            forgiving_pts = max(forgiving_pts, strict_earned)
        program_done = False
        if boundary is not None:
            program_done = bool(
                _habit_program_complete_worknight(boundary, marks_set, scan_iso, cheat_suns)
            )

        if program_done:
            phase = "done"
        elif phase1_satisfied and boundary is not None:
            phase = "phase2"
        else:
            phase = "phase1"

        phase2_block = None
        if boundary is not None and phase2_start is not None:
            phase2_leg_ui_day = _add_days(anchor_iso, 1) if anchor_iso in marks_set else anchor_iso
            st_tomorrow = _phase2_state_at_start_worknight(
                boundary, marks_set, phase2_leg_ui_day, cheat_suns
            )
            cur_target = st_tomorrow.target_len if not st_tomorrow.before_phase2 else 8
            cur_run = st_tomorrow.run if not st_tomorrow.before_phase2 else 0
            if st_tomorrow.violation:
                cur_target = None
                cur_run = None
            phase2_block = {
                "boundary_iso": boundary,
                "phase2_start_iso": phase2_start,
                "nominal_rest_dates": [],
                "strict": {
                    "violation": strict_violation,
                    "complete": strict_complete,
                    "earned_points": strict_earned,
                },
                "forgiving": {"points": forgiving_pts, "complete": forgiving_done},
                "effective_next_rest_iso": None,
                "current_leg_target_len": cur_target,
                "current_leg_run_start_of_tomorrow": cur_run,
            }

        return {
            "phase": phase,
            "anchor_iso": anchor_iso,
            "title": title,
            "phase1": {
                "actual_per_week": actual,
                "satisfied": phase1_satisfied,
                "current_week_ui_index": w_ui,
                "points_earned": phase1_earned,
                "points_cap": PHASE1_MAX_POINTS_WORKNIGHT,
            },
            "phase2": phase2_block,
        }

    actual, phase1_satisfied = _phase1_stats(start_iso, marks_list)
    phase1_earned = sum(min(actual[w], w + 1) for w in range(PHASE1_WEEKS))
    current_week_ui = _phase1_current_week_ui_index(start_iso, anchor_iso, actual)

    boundary = _phase2_boundary_date(start_iso, marks_list) if phase1_satisfied else None
    phase2_start = _add_days(boundary, 1) if boundary is not None else None

    scan_iso = anchor_iso
    if marks_list:
        scan_iso = max(scan_iso, marks_list[-1])
    if boundary is not None and phase2_start is not None:
        scan_iso = max(scan_iso, phase2_start)

    forgiving_pts, forgiving_done = (
        _forgiving_phase2_points_and_complete(boundary, marks_set)
        if boundary is not None
        else (0, False)
    )
    strict_earned, strict_complete, strict_violation = (
        _strict_phase2_scan_metrics(boundary, marks_set, scan_iso)
        if boundary is not None
        else (0, False, False)
    )
    if boundary is not None:
        forgiving_pts = max(forgiving_pts, strict_earned)

    program_done = False
    if boundary is not None:
        program_done = bool(_habit_program_complete(boundary, marks_set, scan_iso))

    if program_done:
        phase = "done"
    elif phase1_satisfied and boundary is not None:
        phase = "phase2"
    else:
        phase = "phase1"

    phase2_block = None
    if boundary is not None and phase2_start is not None:
        nominal = nominal_phase2_rest_dates(phase2_start)
        if strict_violation:
            cur_target = None
            cur_run = None
        else:
            leg_prog = _phase2_leg_progress_from_nominal_rests(phase2_start, marks_set, anchor_iso)
            if leg_prog is None:
                cur_target = None
                cur_run = None
            else:
                cur_run, cur_target = leg_prog
        eff_rest = _effective_next_rest_iso_calendar(boundary, marks_set, anchor_iso)
        phase2_block = {
            "boundary_iso": boundary,
            "phase2_start_iso": phase2_start,
            "nominal_rest_dates": nominal,
            "strict": {
                "violation": strict_violation,
                "complete": strict_complete,
                "earned_points": strict_earned,
            },
            "forgiving": {"points": forgiving_pts, "complete": forgiving_done},
            "effective_next_rest_iso": eff_rest,
            "current_leg_target_len": cur_target,
            "current_leg_run_start_of_tomorrow": cur_run,
        }

    return {
        "phase": phase,
        "anchor_iso": anchor_iso,
        "title": title,
        "phase1": {
            "actual_per_week": actual,
            "satisfied": phase1_satisfied,
            "current_week_ui_index": current_week_ui,
            "points_earned": phase1_earned,
            "points_cap": 28,
        },
        "phase2": phase2_block,
    }


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

        duration_minutes = _habit_duration_minutes(raw, title, default_minutes)
        marks_list = sorted(marks)

        if _is_time(raw):
            n = _time_target_minutes(raw)
            worknight = _is_worknight(raw)
            program_days, _, phase1_satisfied = _phase1_stats_time(
                start_iso, marks_list, n, worknight
            )
            if not phase1_satisfied:
                req = _phase1_requires_date_time(
                    title=title,
                    start_iso=start_iso,
                    target_iso=target_date_iso,
                    marks=marks,
                    program_days=program_days,
                )
                if req is not None:
                    out.append(req)
                continue

            boundary = _phase2_boundary_date_time(start_iso, marks_list, n, worknight)
            if boundary is None:
                continue
            scan_iso = target_date_iso
            if marks_list:
                scan_iso = max(scan_iso, marks_list[-1])

            if worknight:
                cheats = set(_cheat_dates(raw))
                cheat_suns = _cheat_week_sundays(cheats)
                if _habit_program_complete_worknight(boundary, marks, scan_iso, cheat_suns):
                    continue
                if not _sun_thru_thu(target_date_iso):
                    continue
                state = _phase2_state_at_start_worknight(
                    boundary, marks, target_date_iso, cheat_suns
                )
                if state.before_phase2 or state.complete or state.need_rest or state.violation:
                    continue
                out.append(
                    HabitRequirement(
                        title=title,
                        target_date=target_date_iso,
                        duration_minutes=n,
                        reason=(
                            f"time phase 2 streak night · {n} min; "
                            f"current run {state.run}/{state.target_len} would reset if skipped"
                        ),
                    )
                )
            else:
                if _habit_program_complete(boundary, marks, scan_iso):
                    continue
                state = _phase2_state_at_start(boundary, marks, target_date_iso)
                if state.before_phase2 or state.complete or state.need_rest or state.violation:
                    continue
                out.append(
                    HabitRequirement(
                        title=title,
                        target_date=target_date_iso,
                        duration_minutes=n,
                        reason=(
                            f"time phase 2 streak day · {n} min; "
                            f"current run {state.run}/{state.target_len} would reset if skipped"
                        ),
                    )
                )
            continue

        if _is_worknight(raw):
            cheats = set(_cheat_dates(raw))
            cheat_suns = _cheat_week_sundays(cheats)
            actual, phase1_satisfied = _phase1_stats_worknight(start_iso, marks_list, cheats)
            if not phase1_satisfied:
                req = _phase1_requires_date_worknight(
                    title=title,
                    start_iso=start_iso,
                    target_iso=target_date_iso,
                    marks=marks,
                    actual=actual,
                    duration_minutes=duration_minutes,
                    cheats=cheats,
                )
                if req is not None:
                    out.append(req)
                continue

            boundary = _phase2_boundary_date_worknight(start_iso, marks_list)
            if boundary is None:
                continue
            scan_iso = target_date_iso
            if marks_list:
                scan_iso = max(scan_iso, marks_list[-1])
            if _habit_program_complete_worknight(boundary, marks, scan_iso, cheat_suns):
                continue
            if not _sun_thru_thu(target_date_iso):
                continue
            state = _phase2_state_at_start_worknight(boundary, marks, target_date_iso, cheat_suns)
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
                        f"worknight phase 2 streak night; current run {state.run}/{state.target_len} "
                        "would reset if skipped"
                    ),
                )
            )
            continue

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


def _habit_without_mark_on(habit: dict[str, Any], day_iso: str) -> dict[str, Any]:
    clone = dict(habit)
    raw_days = habit.get("days")
    if isinstance(raw_days, dict):
        clone["days"] = {
            k: v for k, v in raw_days.items() if k != day_iso and v and _parse_date(k) is not None
        }
    else:
        clone["days"] = {}
    return clone


def mandatory_habits_for_planner_date(
    snapshot: dict[str, Any],
    target_date_iso: str,
    *,
    default_minutes: int = DEFAULT_HABIT_MINUTES,
) -> list[dict[str, Any]]:
    """Habits that must be logged on ``target_date_iso``, with calendar completion state."""
    if _parse_date(target_date_iso) is None:
        return []

    habits = snapshot.get("habits")
    if not isinstance(habits, list):
        return []

    pending_by_title = {
        r.title: r
        for r in required_habits_for_date(
            snapshot, target_date_iso, default_minutes=default_minutes
        )
    }
    out: list[dict[str, Any]] = []

    for raw in habits:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "Habit").strip() or "Habit"
        hid = raw.get("id")
        if not isinstance(hid, str) or not hid.strip():
            continue
        marks = set(_marked_dates(raw))
        logged = target_date_iso in marks

        if title in pending_by_title:
            req = pending_by_title[title]
            out.append(
                {
                    "habit_id": hid.strip(),
                    "title": title,
                    "duration_minutes": req.duration_minutes,
                    "target_date": target_date_iso,
                    "logged": False,
                    "reason": req.reason,
                }
            )
            continue

        if not logged:
            continue

        clone = _habit_without_mark_on(raw, target_date_iso)
        reqs = required_habits_for_date(
            {"habits": [clone]},
            target_date_iso,
            default_minutes=default_minutes,
        )
        if not reqs or reqs[0].title != title:
            continue
        req = reqs[0]
        out.append(
            {
                "habit_id": hid.strip(),
                "title": title,
                "duration_minutes": req.duration_minutes,
                "target_date": target_date_iso,
                "logged": True,
                "reason": req.reason,
            }
        )

    out.sort(key=lambda row: (row.get("logged") is True, str(row.get("title") or "").lower()))
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
    if _is_time(habit):
        return _latest_phase1_deadline_iso_time(habit, current_iso)
    if _is_worknight(habit):
        return _latest_phase1_deadline_iso_worknight(habit, current_iso)

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


def _phase2_status_for_target_time(
    habit: dict[str, Any], marks_set: set[str], target_iso: str
) -> dict[str, Any]:
    start_raw = habit.get("start")
    if not isinstance(start_raw, str) or _parse_date(start_raw) is None:
        return {"kind": "phase2-pending"}
    start_iso = start_raw
    n = _time_target_minutes(habit)
    worknight = _is_worknight(habit)
    marks_list = sorted(marks_set)
    _, _, phase1_satisfied = _phase1_stats_time(start_iso, marks_list, n, worknight)
    if not phase1_satisfied:
        return {"kind": "phase2-pending"}
    boundary = _phase2_boundary_date_time(start_iso, marks_list, n, worknight)
    if boundary is None:
        return {"kind": "phase2-pending"}
    if worknight:
        cheat_suns = _cheat_week_sundays(set(_cheat_dates(habit)))
        scan_iso = target_iso
        if marks_list:
            scan_iso = max(scan_iso, marks_list[-1])
        if _habit_program_complete_worknight(boundary, marks_set, scan_iso, cheat_suns):
            return {"kind": "phase2-complete"}
        state = _phase2_state_at_start_worknight(boundary, marks_set, target_iso, cheat_suns)
        if state.before_phase2:
            return {
                "kind": "phase2-before",
                "first_iso": _first_active_on_or_after(_add_days(boundary, 1)),
            }
        if state.violation:
            return {"kind": "phase2-violation"}
        if state.complete:
            return {"kind": "phase2-complete"}
        if state.need_rest:
            nxt = target_iso
            for _ in range(14):
                nxt = _add_days(nxt, 1)
                if _sun_thru_thu(nxt):
                    return {"kind": "phase2-rest", "next_active_iso": nxt}
            return {"kind": "phase2-rest", "next_active_iso": _add_days(target_iso, 1)}
        return {
            "kind": "phase2-active",
            "run": state.run,
            "target_len": state.target_len,
        }
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
        if _is_time(raw):
            n = _time_target_minutes(raw)
            program_days, _, phase1_satisfied = _phase1_stats_time(
                start_iso, marks_list, n, _is_worknight(raw)
            )
            if not phase1_satisfied:
                deadline = latest_phase1_deadline_iso(raw, target_date_iso)
                idx = _time_phase1_day_index(program_days, target_date_iso)
                if idx >= 0:
                    done = sum(1 for d in program_days[: idx + 1] if d in marks_set)
                    need = idx + 1
                    mins = need
                    progress = f"time phase 1 day {need}/{n} is {done}/{need} ({mins} min)"
                else:
                    progress = "time phase 1 in progress"
                if deadline is None or deadline == target_date_iso:
                    lines.append(
                        f"- {title}: {progress}; latest deadline {target_date_iso} (today)."
                    )
                else:
                    lines.append(
                        f"- {title}: {progress}; latest deadline {deadline}; skip on {target_date_iso}."
                    )
                continue
            status = _phase2_status_for_target_time(raw, marks_set, target_date_iso)
        elif _is_worknight(raw):
            cheats = set(_cheat_dates(raw))
            cheat_suns = _cheat_week_sundays(cheats)
            actual, phase1_satisfied = _phase1_stats_worknight(start_iso, marks_list, cheats)
            if not phase1_satisfied:
                deadline = latest_phase1_deadline_iso(raw, target_date_iso)
                week_idx = _week_index_uncapped(start_iso, target_date_iso)
                if 0 <= week_idx < PHASE1_WEEKS_WORKNIGHT:
                    done = actual[week_idx]
                    need = week_idx + 1
                    progress = (
                        f"worknight phase 1 week {week_idx + 1}/{PHASE1_WEEKS_WORKNIGHT} is {done}/{need}"
                    )
                else:
                    progress = "worknight phase 1 in progress"
                if deadline is None or deadline == target_date_iso:
                    lines.append(
                        f"- {title}: {progress}; latest deadline {target_date_iso} (today)."
                    )
                else:
                    lines.append(
                        f"- {title}: {progress}; latest deadline {deadline}; skip on {target_date_iso}."
                    )
                continue
            status = _phase2_status_for_target_worknight(
                start_iso=start_iso,
                marks_set=marks_set,
                target_iso=target_date_iso,
                cheat_suns=cheat_suns,
            )
        else:
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
            nominal_hint = ""
            if not _is_worknight(raw) and not _is_time(raw):
                bd = _phase2_boundary_date(start_iso, marks_list)
                if bd is not None:
                    p2_start = _add_days(bd, 1)
                    for nd in nominal_phase2_rest_dates(p2_start):
                        if nd >= target_date_iso:
                            nominal_hint = f" Nominal next mandatory rest (on-time schedule): {nd}."
                            break
            elif _is_time(raw) and not _is_worknight(raw):
                n = _time_target_minutes(raw)
                bd = _phase2_boundary_date_time(start_iso, marks_list, n, False)
                if bd is not None:
                    p2_start = _add_days(bd, 1)
                    for nd in nominal_phase2_rest_dates(p2_start):
                        if nd >= target_date_iso:
                            nominal_hint = f" Nominal next mandatory rest (on-time schedule): {nd}."
                            break
            lines.append(
                f"- {title}: phase 2 REST day on {target_date_iso}; do NOT schedule. "
                f"Resumes {nxt}.{nominal_hint}"
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
    out["mandatory_for_planner_date"] = mandatory_habits_for_planner_date(
        snapshot, target_date_iso
    )
    return out


__all__ = [
    "DEFAULT_HABIT_MINUTES",
    "HabitRequirement",
    "derive_habit_program_state",
    "habits_snapshot_with_required_rows",
    "mandatory_habits_for_planner_date",
    "latest_phase1_deadline_iso",
    "nominal_phase2_rest_dates",
    "non_required_habits_context_block",
    "required_habits_context_block",
    "required_habits_for_date",
]
