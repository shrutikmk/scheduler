from __future__ import annotations

from datetime import date, timedelta

import habit_schedule as habit_schedule
from habit_schedule import (
    habits_snapshot_with_required_rows,
    _phase2_status_for_target,
    latest_phase1_deadline_iso,
    non_required_habits_context_block,
    required_habits_context_block,
    required_habits_for_date,
)
from mlx_day_scheduler_ui import augment_chat_payload
from scheduler_store import SchedulerStore


def habit(title: str, start: str, days: dict[str, bool] | None = None) -> dict:
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "start": start,
        "days": days or {},
    }


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
    assert "phase 1 week 1/7" in reqs[0].reason


def test_already_logged_and_before_start_are_not_required() -> None:
    logged = {"habits": [habit("10k steps", "2026-05-03", {"2026-05-09": True})]}
    before_start = {"habits": [habit("Sleep at 10:30pm", "2026-05-10")]}

    assert required_habits_for_date(logged, "2026-05-09") == []
    assert required_habits_for_date(before_start, "2026-05-09") == []


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
