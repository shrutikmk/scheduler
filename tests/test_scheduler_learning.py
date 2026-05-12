from __future__ import annotations

from pathlib import Path

from day_scheduler_web import augment_chat_payload
from scheduler_store import (
    ScheduleRow,
    SchedulerStore,
    normalize_activity_key,
    start_label_sort_minutes,
)


def make_store(tmp_path: Path) -> SchedulerStore:
    store = SchedulerStore(tmp_path / "scheduler.sqlite")
    store.init_schema()
    return store


def test_normalize_activity_key_groups_coding_variants() -> None:
    assert normalize_activity_key("Deep coding session") == "coding"
    assert normalize_activity_key("Code for scheduler app") == "coding"
    assert normalize_activity_key("Focused dev block") == "coding"


def test_start_label_sort_minutes_wall_clock_order() -> None:
    labels = ["10:30 AM", "12:00 PM", "8:00 AM", "8:30 AM", "5:30 PM", "12:01 AM"]
    ordered = sorted(labels, key=start_label_sort_minutes)
    assert ordered == ["12:01 AM", "8:00 AM", "8:30 AM", "10:30 AM", "12:00 PM", "5:30 PM"]


def test_start_label_sort_minutes_datetime_prefixed_labels() -> None:
    labels = [
        "2026-05-09 7:43 PM",
        "2026-05-09 2:43 PM",
        "2026-05-09 1:13 PM",
        "2026-05-09 8:13 PM",
        "2026-05-09 5:13 PM",
    ]
    ordered = sorted(labels, key=start_label_sort_minutes)
    assert ordered == [
        "2026-05-09 1:13 PM",
        "2026-05-09 2:43 PM",
        "2026-05-09 5:13 PM",
        "2026-05-09 7:43 PM",
        "2026-05-09 8:13 PM",
    ]


def test_list_schedule_tasks_ordered_by_wall_clock(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    rows = [
        ScheduleRow("a", "2026-05-09", "10:30 AM", 90, "Cook lunch", "pending"),
        ScheduleRow("b", "2026-05-09", "12:00 PM", 180, "Deep coding session", "pending"),
        ScheduleRow("c", "2026-05-09", "8:00 AM", 30, "Make breakfast", "pending"),
    ]
    store.replace_tasks_for_dates(["2026-05-09"], rows)
    out = store.list_schedule_tasks("2026-05-09")
    titles = [r["title"] for r in out]
    assert titles == ["Make breakfast", "Cook lunch", "Deep coding session"]


def test_delete_schedule_tasks_for_date_only_that_day(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.replace_tasks_for_dates(
        ["2026-05-09", "2026-05-10"],
        [
            ScheduleRow("x", "2026-05-09", "9:00 AM", 60, "One", "pending"),
            ScheduleRow("y", "2026-05-10", "9:00 AM", 60, "Two", "pending"),
        ],
    )
    n = store.delete_schedule_tasks_for_date("2026-05-09")
    assert n == 1
    assert store.list_schedule_tasks("2026-05-09") == []
    assert len(store.list_schedule_tasks("2026-05-10")) == 1


def test_marking_task_done_records_activity_event(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.replace_tasks_for_dates(
        ["2026-05-08"],
        [
            ScheduleRow(
                "task-1",
                "2026-05-08",
                "9:00 PM",
                75,
                "Deep coding session",
                "pending",
            ),
        ],
    )

    assert store.update_task_status("task-1", "done") is True
    assert store.update_task_status("task-1", "done") is True

    events = store.list_activity_events()
    assert len(events) == 1
    assert events[0]["activity_key"] == "coding"
    assert events[0]["planned_minutes"] == 75
    assert events[0]["actual_minutes"] == 75


def test_learned_context_injected_into_chat_payload(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.replace_tasks_for_dates(
        ["2026-05-08"],
        [
            ScheduleRow(
                "task-1",
                "2026-05-08",
                "8:00 PM",
                60,
                "Coding sprint",
                "pending",
            ),
        ],
    )
    assert store.update_task_status("task-1", "done") is True

    out = augment_chat_payload(
        store,
        {
            "content": "I want to code more tonight",
            "client_calendar": {"date_iso": "2026-05-08"},
        },
    )

    ctx = out.get("persisted_tasks_context")
    assert isinstance(ctx, str)
    assert "[Learned" in ctx
    assert "coding" in ctx
    assert "typical 60m" in ctx
