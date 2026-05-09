from __future__ import annotations

from datetime import date, timedelta

from habit_schedule import required_habits_context_block, required_habits_for_date
from mlx_day_scheduler_ui import augment_chat_payload
from scheduler_store import SchedulerStore


def habit(title: str, start: str, days: dict[str, bool] | None = None) -> dict:
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "start": start,
        "days": days or {},
    }


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
