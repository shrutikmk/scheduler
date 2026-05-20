from __future__ import annotations

from datetime import date, timedelta

import habit_schedule as habit_schedule
from day_scheduler_web import augment_chat_payload
from habit_schedule import (
    _phase2_status_for_target,
    derive_habit_program_state,
    habits_snapshot_with_required_rows,
    latest_phase1_deadline_iso,
    mandatory_habits_for_planner_date,
    nominal_phase2_rest_dates,
    non_required_habits_context_block,
    required_habits_context_block,
    required_habits_for_date,
)
from scheduler_store import SchedulerStore


def habit(
    title: str,
    start: str,
    days: dict[str, bool] | None = None,
    *,
    duration_minutes: int | None = None,
    habit_type: str | None = None,
    worknight_mode: bool | None = None,
    time_target_minutes: int | None = None,
    cheat_days: dict[str, bool] | None = None,
) -> dict:
    h = {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "start": start,
        "days": days or {},
    }
    if duration_minutes is not None:
        h["duration_minutes"] = duration_minutes
    if habit_type is not None:
        h["habit_type"] = habit_type
    if worknight_mode is not None:
        h["worknight_mode"] = worknight_mode
    if time_target_minutes is not None:
        h["time_target_minutes"] = time_target_minutes
    if cheat_days is not None:
        h["cheat_days"] = cheat_days
    return h


def _worknight_phase1_minimal_days(start_iso: str) -> dict[str, bool]:
    """Minimal Sun–Thu-heavy marks so Worknight Phase 1 completes (5 weeks)."""
    out: dict[str, bool] = {}
    start_d = date.fromisoformat(start_iso)
    sun0 = start_d - timedelta(days=(start_d.weekday() + 1) % 7)
    for w in range(5):
        need = w + 1
        added = 0
        for delta in range(7):
            d = sun0 + timedelta(days=w * 7 + delta)
            if d < start_d:
                continue
            out[d.isoformat()] = True
            added += 1
            if added >= need:
                break
    return out


def test_worknight_worknight_mode_equivalent_to_legacy_type() -> None:
    snap_legacy = {"habits": [habit("Sleep", "2026-05-04", {}, habit_type="worknight")]}
    snap_toggle = {
        "habits": [habit("Sleep", "2026-05-04", {}, habit_type="default", worknight_mode=True)]
    }
    assert required_habits_for_date(snap_legacy, "2026-05-07") == required_habits_for_date(
        snap_toggle, "2026-05-07"
    )


def test_time_phase1_day_k_requires_k_minutes() -> None:
    snap = {
        "habits": [
            habit("Meditate", "2026-05-19", {}, habit_type="time", time_target_minutes=5)
        ]
    }
    reqs = required_habits_for_date(snap, "2026-05-19")
    assert len(reqs) == 1
    assert reqs[0].duration_minutes == 1
    assert "time phase 1 day 1/5" in reqs[0].reason
    assert "1 min" in reqs[0].reason


def test_time_phase1_worknight_skips_saturday() -> None:
    snap = {
        "habits": [
            habit(
                "Focus",
                "2026-05-15",
                {},
                habit_type="time",
                time_target_minutes=5,
                worknight_mode=True,
            )
        ]
    }
    assert required_habits_for_date(snap, "2026-05-16") == []


def test_time_phase2_rest_day_not_required() -> None:
    start = "2026-01-04"
    n = 5
    days = {d: True for d in habit_schedule._time_program_days(start, n, False)}
    boundary = habit_schedule._phase2_boundary_date_time(start, sorted(days.keys()), n, False)
    assert boundary is not None
    first_p2 = date.fromisoformat(boundary) + timedelta(days=1)
    for offset in range(8):
        days[(first_p2 + timedelta(days=offset)).isoformat()] = True
    snap = {
        "habits": [
            habit("Read", start, days, habit_type="time", time_target_minutes=n)
        ]
    }
    rest_day = (first_p2 + timedelta(days=8)).isoformat()
    assert required_habits_for_date(snap, rest_day) == []


def test_time_phase2_streak_requires_n_minutes() -> None:
    start = "2026-01-04"
    n = 20
    days = {d: True for d in habit_schedule._time_program_days(start, n, False)}
    snap = {
        "habits": [
            habit("Read", start, days, habit_type="time", time_target_minutes=n)
        ]
    }
    boundary = habit_schedule._phase2_boundary_date_time(start, sorted(days.keys()), n, False)
    assert boundary is not None
    first_p2 = (date.fromisoformat(boundary) + timedelta(days=1)).isoformat()
    reqs = required_habits_for_date(snap, first_p2)
    assert len(reqs) == 1
    assert reqs[0].duration_minutes == n
    assert "time phase 2 streak day" in reqs[0].reason


def test_derive_habit_program_state_time_phase1() -> None:
    h = habit("Timer", "2026-05-19", {}, habit_type="time", time_target_minutes=10)
    st = derive_habit_program_state(h, anchor_iso="2026-05-20")
    assert st["phase"] == "phase1"
    assert st["habit_type"] == "time"
    assert st["time_target_minutes"] == 10
    assert st["phase1"]["points_cap"] == 55
    assert st["phase1"]["current_day_ui_index"] == 0


def test_worknight_phase1_requires_sun_thru_thu_but_not_sat() -> None:
    snap = {"habits": [habit("Sleep", "2026-05-04", {}, habit_type="worknight")]}
    thu = required_habits_for_date(snap, "2026-05-07")
    assert len(thu) == 1
    assert required_habits_for_date(snap, "2026-05-09") == []


def test_worknight_phase2_adjacent_thu_sun_run() -> None:
    from habit_schedule import _max_phase2_run_length_worknight, _phase2_boundary_date_worknight

    start = "2026-05-03"
    days = _worknight_phase1_minimal_days(start)
    marks_list = sorted(days.keys())
    boundary = _phase2_boundary_date_worknight(start, marks_list)
    assert boundary is not None
    marks = set(marks_list) | {"2026-06-11", "2026-06-14"}
    assert _max_phase2_run_length_worknight(boundary, marks) >= 2


def test_worknight_phase2_not_required_on_saturday() -> None:
    from habit_schedule import _phase2_boundary_date_worknight

    start = "2026-05-03"
    days = _worknight_phase1_minimal_days(start)
    boundary = _phase2_boundary_date_worknight(start, sorted(days.keys()))
    assert boundary is not None
    snap = {
        "habits": [
            {
                "id": "wn",
                "title": "WN",
                "start": start,
                "days": days,
                "habit_type": "worknight",
            }
        ]
    }
    assert required_habits_for_date(snap, "2026-06-13") == []


def test_habits_snapshot_with_required_rows_matches_required_list() -> None:
    snapshot = {"habits": [habit("10k steps", "2026-05-03")]}
    out = habits_snapshot_with_required_rows(snapshot, "2026-05-09")
    reqs = required_habits_for_date(snapshot, "2026-05-09")
    assert isinstance(out["required_for_planner_date"], list)
    assert len(out["required_for_planner_date"]) == len(reqs) == 1
    row = out["required_for_planner_date"][0]
    assert row["title"] == reqs[0].title
    assert row["target_date"] == reqs[0].target_date


def phase1_completed_days(start_iso: str) -> dict[str, bool]:
    start = date.fromisoformat(start_iso)
    out: dict[str, bool] = {}
    for week in range(7):
        week_start = start + timedelta(days=week * 7)
        for offset in range(week + 1):
            out[(week_start + timedelta(days=offset)).isoformat()] = True
    return out


def test_phase1_noncritical_day_not_required() -> None:
    snapshot = {"habits": [habit("10k steps", "2026-05-03")]}
    assert required_habits_for_date(snapshot, "2026-05-04") == []


def test_phase1_last_eligible_day_required() -> None:
    snapshot = {"habits": [habit("10k steps", "2026-05-03")]}
    reqs = required_habits_for_date(snapshot, "2026-05-09")
    assert len(reqs) == 1
    assert reqs[0].title == "10k steps"
    assert reqs[0].duration_minutes == 60
    assert "phase 1 week 1/7" in reqs[0].reason


def test_already_logged_and_before_start_are_not_required() -> None:
    logged = {"habits": [habit("10k steps", "2026-05-03", {"2026-05-09": True})]}
    before_start = {"habits": [habit("Sleep at 10:30pm", "2026-05-10")]}

    assert required_habits_for_date(logged, "2026-05-09") == []
    assert required_habits_for_date(before_start, "2026-05-09") == []


def test_mandatory_for_planner_includes_pending_and_logged() -> None:
    due = "2026-05-09"
    snapshot = {"habits": [habit("10k steps", "2026-05-03")]}
    pending = mandatory_habits_for_planner_date(snapshot, due)
    assert len(pending) == 1
    assert pending[0]["logged"] is False
    assert pending[0]["habit_id"] == "10k-steps"

    logged_snap = {"habits": [habit("10k steps", "2026-05-03", {due: True})]}
    assert required_habits_for_date(logged_snap, due) == []
    logged_rows = mandatory_habits_for_planner_date(logged_snap, due)
    assert len(logged_rows) == 1
    assert logged_rows[0]["logged"] is True


def test_habits_snapshot_includes_mandatory_for_planner_date() -> None:
    snapshot = {"habits": [habit("10k steps", "2026-05-03")]}
    out = habits_snapshot_with_required_rows(snapshot, "2026-05-09")
    assert isinstance(out["mandatory_for_planner_date"], list)
    assert len(out["mandatory_for_planner_date"]) == 1


def test_phase2_rest_day_not_required() -> None:
    days = phase1_completed_days("2026-01-04")
    first_phase2 = date.fromisoformat("2026-02-22")
    for offset in range(8):
        days[(first_phase2 + timedelta(days=offset)).isoformat()] = True

    snapshot = {"habits": [habit("Practice piano", "2026-01-04", days)]}
    assert required_habits_for_date(snapshot, "2026-03-02") == []


def test_phase2_active_streak_day_required() -> None:
    days = phase1_completed_days("2026-01-04")
    snapshot = {"habits": [habit("Practice piano", "2026-01-04", days)]}

    reqs = required_habits_for_date(snapshot, "2026-02-22")
    assert len(reqs) == 1
    assert reqs[0].title == "Practice piano"
    assert "phase 2 streak day" in reqs[0].reason


def test_required_habits_context_block() -> None:
    snapshot = {"habits": [habit("10k steps", "2026-05-03")]}
    block = required_habits_context_block(snapshot, "2026-05-09")

    assert block is not None
    assert "[Required habits" in block
    assert "10k steps" in block
    assert "must schedule" in block
    assert "1h00m" in block


def test_steps_goal_infers_about_one_hour_when_duration_omitted() -> None:
    snapshot = {"habits": [habit("12 k steps", "2026-05-03")]}
    reqs = required_habits_for_date(snapshot, "2026-05-09")
    assert reqs[0].duration_minutes == 72


def test_explicit_duration_overrides_steps_inference() -> None:
    snapshot = {"habits": [habit("10k steps", "2026-05-03", duration_minutes=45)]}
    reqs = required_habits_for_date(snapshot, "2026-05-09")
    assert reqs[0].duration_minutes == 45


def test_non_steps_habit_uses_generic_default_when_duration_omitted() -> None:
    days = phase1_completed_days("2026-01-04")
    snapshot = {"habits": [habit("Practice piano", "2026-01-04", days)]}
    reqs = required_habits_for_date(snapshot, "2026-02-22")
    assert len(reqs) == 1
    assert reqs[0].duration_minutes == habit_schedule.DEFAULT_HABIT_MINUTES


def test_required_habits_injected_into_chat_payload(tmp_path) -> None:
    store = SchedulerStore(tmp_path / "scheduler.sqlite")
    store.init_schema()
    store.put_habits_snapshot({"id": "default", "habits": [habit("10k steps", "2026-05-03")]})

    out = augment_chat_payload(
        store,
        {
            "content": "Build my plan for today",
            "client_calendar": {"date_iso": "2026-05-09", "minute_of_day": 600},
        },
    )

    ctx = out.get("persisted_tasks_context")
    assert isinstance(ctx, str)
    assert "[Required habits" in ctx
    assert "10k steps" in ctx


def _ten_k_steps_through_week6() -> dict[str, bool]:
    days: dict[str, bool] = {}
    start = date.fromisoformat("2026-04-01")
    week_starts = [
        date.fromisoformat("2026-03-29"),
        date.fromisoformat("2026-04-05"),
        date.fromisoformat("2026-04-12"),
        date.fromisoformat("2026-04-19"),
        date.fromisoformat("2026-04-26"),
        date.fromisoformat("2026-05-03"),
    ]
    for week_idx, week_start in enumerate(week_starts):
        for offset in range(week_idx + 1):
            day = week_start + timedelta(days=offset)
            if day < start:
                continue
            days[day.isoformat()] = True
    return days


def test_latest_deadline_when_current_week_satisfied_points_to_next_week_start() -> None:
    days = _ten_k_steps_through_week6()
    h = habit("10k steps", "2026-04-01", days)

    deadline = latest_phase1_deadline_iso(h, "2026-05-09")

    assert deadline == "2026-05-10"


def test_latest_deadline_for_partial_week_lands_on_correct_thursday() -> None:
    h = habit(
        "Sleep at 10:30pm",
        "2026-05-01",
        {"2026-05-01": True, "2026-05-04": True, "2026-05-08": True},
    )

    deadline = latest_phase1_deadline_iso(h, "2026-05-09")

    assert deadline == "2026-05-14"


def test_latest_deadline_returns_today_when_week_already_at_risk() -> None:
    h = habit("10k steps", "2026-04-01", {"2026-05-15": True})

    deadline = latest_phase1_deadline_iso(h, "2026-05-15")

    assert deadline == "2026-05-15"


def test_latest_deadline_none_when_phase1_satisfied() -> None:
    h = habit("Practice piano", "2026-01-04", phase1_completed_days("2026-01-04"))

    assert latest_phase1_deadline_iso(h, "2026-02-22") is None


def test_non_required_block_lists_only_off_quota_habits() -> None:
    snapshot = {
        "habits": [
            habit("10k steps", "2026-04-01", _ten_k_steps_through_week6()),
            habit(
                "Sleep at 10:30pm",
                "2026-05-01",
                {"2026-05-01": True, "2026-05-04": True, "2026-05-08": True},
            ),
        ]
    }

    block = non_required_habits_context_block(snapshot, "2026-05-09")

    assert block is not None
    assert "[Habit Builder — not required on 2026-05-09]" in block
    assert "10k steps" in block
    assert "latest deadline 2026-05-10" in block
    assert "Sleep at 10:30pm" in block
    assert "latest deadline 2026-05-14" in block
    assert "Do NOT add a timetable bullet" in block


def test_non_required_block_omits_required_habits() -> None:
    snapshot = {"habits": [habit("10k steps", "2026-05-03")]}

    block = non_required_habits_context_block(snapshot, "2026-05-09")

    assert block is None


def test_non_required_block_handles_already_logged_and_pre_start() -> None:
    snapshot = {
        "habits": [
            habit("10k steps", "2026-05-03", {"2026-05-09": True}),
            habit("Sleep at 10:30pm", "2026-05-15"),
        ]
    }

    block = non_required_habits_context_block(snapshot, "2026-05-09")

    assert block is not None
    assert "already logged on 2026-05-09" in block
    assert "not yet started" in block


def test_non_required_block_handles_phase2_rest_day() -> None:
    days = phase1_completed_days("2026-01-04")
    first_phase2 = date.fromisoformat("2026-02-22")
    for offset in range(8):
        days[(first_phase2 + timedelta(days=offset)).isoformat()] = True

    snapshot = {"habits": [habit("Practice piano", "2026-01-04", days)]}

    block = non_required_habits_context_block(snapshot, "2026-03-02")

    assert block is not None
    assert "REST day on 2026-03-02" in block
    assert "Resumes 2026-03-03" in block
    assert "Nominal next mandatory rest (on-time schedule): 2026-03-02" in block


def test_non_required_block_injected_alongside_required(tmp_path) -> None:
    store = SchedulerStore(tmp_path / "scheduler.sqlite")
    store.init_schema()
    store.put_habits_snapshot(
        {
            "id": "default",
            "habits": [
                habit("10k steps", "2026-04-01", _ten_k_steps_through_week6()),
                habit(
                    "Sleep at 10:30pm",
                    "2026-05-01",
                    {
                        "2026-05-01": True,
                        "2026-05-04": True,
                        "2026-05-08": True,
                    },
                ),
            ],
        }
    )

    out = augment_chat_payload(
        store,
        {
            "content": "Build my plan for today",
            "client_calendar": {"date_iso": "2026-05-09", "minute_of_day": 600},
        },
    )

    ctx = out.get("persisted_tasks_context")
    assert isinstance(ctx, str)
    assert "[Habit Builder — not required on 2026-05-09]" in ctx
    assert "10k steps" in ctx
    assert "Sleep at 10:30pm" in ctx
    assert "latest deadline 2026-05-10" in ctx
    assert "latest deadline 2026-05-14" in ctx


def test_forgiving_phase2_terminal_run_nine_points() -> None:
    boundary = "2026-06-01"
    d0 = date.fromisoformat("2026-06-02")
    marks = {(d0 + timedelta(days=i)).isoformat() for i in range(9)}
    pts, done = habit_schedule._forgiving_phase2_points_and_complete(boundary, marks)
    assert pts == 9
    assert not done


def test_forgiving_phase2_priority_terminal_run_after_long_prior_run() -> None:
    """89 consecutive Phase 2 days, gap, then 9 — credit uses terminal run 9 only (8+1 partial)."""
    boundary = "2026-06-01"
    d0 = date.fromisoformat("2026-06-02")
    marks: set[str] = set()
    for i in range(89):
        marks.add((d0 + timedelta(days=i)).isoformat())
    # gap day d0+89 unmarked
    for j in range(9):
        marks.add((d0 + timedelta(days=90 + j)).isoformat())
    pts, done = habit_schedule._forgiving_phase2_points_and_complete(boundary, marks)
    assert habit_schedule._max_phase2_run_length(boundary, marks) == 89
    assert habit_schedule._terminal_phase2_run_length(boundary, marks) == 9
    assert pts == 9
    assert not done


def test_forgiving_phase2_ninety_day_shortcut() -> None:
    boundary = "2026-06-01"
    d0 = date.fromisoformat("2026-06-02")
    marks = {(d0 + timedelta(days=i)).isoformat() for i in range(90)}
    pts, done = habit_schedule._forgiving_phase2_points_and_complete(boundary, marks)
    assert done
    assert pts == habit_schedule.PHASE2_MAX_POINTS


def test_required_habits_empty_when_forgiving_ninety_complete() -> None:
    days = phase1_completed_days("2026-01-04")
    first_p2 = date(2026, 2, 22)
    for i in range(90):
        days[(first_p2 + timedelta(days=i)).isoformat()] = True
    snap = {"habits": [habit("Run90", "2026-01-04", days)]}
    assert required_habits_for_date(snap, "2026-12-01") == []


def test_nominal_phase2_rest_dates_count_first_second() -> None:
    start = "2026-06-01"
    rests = nominal_phase2_rest_dates(start)
    assert len(rests) == habit_schedule.PHASE2_NOMINAL_REST_DAY_COUNT == 83
    assert rests[0] == "2026-06-09"
    assert rests[1] == "2026-06-19"


def test_nominal_phase2_rest_dates_invalid_returns_empty() -> None:
    assert nominal_phase2_rest_dates("") == []
    assert nominal_phase2_rest_dates("not-a-date") == []


def test_derive_habit_program_state_calendar_phase2_has_nominals() -> None:
    days = phase1_completed_days("2026-01-04")
    h = habit("Piano", "2026-01-04", days)
    st = derive_habit_program_state(h, anchor_iso="2026-03-01")
    assert st["phase"] == "phase2"
    assert st["phase2"] is not None
    assert st["phase2"]["phase2_start_iso"] == "2026-02-22"
    nom = st["phase2"]["nominal_rest_dates"]
    assert len(nom) == habit_schedule.PHASE2_NOMINAL_REST_DAY_COUNT
    assert nom[0] == "2026-03-02"


def test_derive_habit_program_state_phase2_leg_run_open_today_not_reset() -> None:
    """Nominal-rest window: first Phase 2 day logged; anchor next calendar day → leg row 1/8."""
    days = phase1_completed_days("2026-01-04")
    days["2026-02-22"] = True
    h = habit("LegUi", "2026-01-04", days)
    st = derive_habit_program_state(h, anchor_iso="2026-02-23")
    assert st["phase"] == "phase2"
    p2 = st["phase2"]
    assert p2 is not None
    assert p2["current_leg_target_len"] == 8
    assert p2["current_leg_run_start_of_tomorrow"] == 1


def test_phase2_leg_progress_nominal_may_first_day_logged() -> None:
    p2 = "2026-05-17"
    marks = {"2026-05-17"}
    assert habit_schedule._phase2_leg_progress_from_nominal_rests(p2, marks, "2026-05-18") == (1, 8)


def test_phase2_leg_progress_nominal_full_leg_on_rest_day() -> None:
    p2 = "2026-05-17"
    marks = {(date(2026, 5, 17) + timedelta(days=i)).isoformat() for i in range(8)}
    assert habit_schedule._phase2_leg_progress_from_nominal_rests(p2, marks, "2026-05-25") == (8, 8)


def test_phase2_leg_progress_nominal_second_leg_empty() -> None:
    p2 = "2026-05-17"
    marks = {(date(2026, 5, 17) + timedelta(days=i)).isoformat() for i in range(8)}
    assert habit_schedule._phase2_leg_progress_from_nominal_rests(p2, marks, "2026-05-26") == (0, 9)


def test_phase2_leg_progress_nominal_prelogged_next_leg_advances_while_rest_today() -> None:
    """Today on rest day but next-leg day already marked → show L9 progress."""
    p2 = "2026-05-17"
    marks = {(date(2026, 5, 17) + timedelta(days=i)).isoformat() for i in range(8)} | {"2026-05-26"}
    assert habit_schedule._phase2_leg_progress_from_nominal_rests(p2, marks, "2026-05-25") == (1, 9)


def test_forgiving_phase2_nineteen_consecutive_days_from_phase2_start() -> None:
    boundary = "2026-06-01"
    d0 = date.fromisoformat("2026-06-02")
    marks = {(d0 + timedelta(days=i)).isoformat() for i in range(19)}
    pts, done = habit_schedule._forgiving_phase2_points_and_complete(boundary, marks)
    assert pts == 19
    assert not done


def test_phase2_status_complete_when_forgiving_ninety_done() -> None:
    days = phase1_completed_days("2026-01-04")
    first_p2 = date(2026, 2, 22)
    for i in range(90):
        days[(first_p2 + timedelta(days=i)).isoformat()] = True
    h = habit("Run90b", "2026-01-04", days)
    marks_set = {k for k, v in (h.get("days") or {}).items() if v}
    st = _phase2_status_for_target(
        start_iso="2026-01-04", marks_set=marks_set, target_iso="2026-12-01"
    )
    assert st["kind"] == "phase2-complete"
