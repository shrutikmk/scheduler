"""Unit tests for SchedulerStore task merge/replace helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from scheduler_store import ScheduleRow, SchedulerStore


@pytest.fixture()
def store(tmp_path: Path) -> SchedulerStore:
    s = SchedulerStore(tmp_path / "scheduler.sqlite")
    s.init_schema()
    return s


def _attach(store: SchedulerStore, task_id: str, event_id: str) -> None:
    store.attach_gcal_event(
        task_id=task_id,
        gcal_event_id=event_id,
        gcal_etag='"v1"',
        gcal_calendar_id="primary",
    )


def test_merge_preserves_unrelated_rows(store: SchedulerStore) -> None:
    store.replace_tasks_for_dates(
        ["2026-05-10"],
        [
            ScheduleRow("a", "2026-05-10", "8:00 AM", 60, "Laundry", "pending"),
            ScheduleRow("b", "2026-05-10", "2:00 PM", 60, "Email", "pending"),
        ],
    )
    store.merge_tasks_from_assistant(
        ["2026-05-10"],
        [ScheduleRow("c", "2026-05-10", "10:00 AM", 45, "Workshop", "pending")],
    )
    titles = {r["title"] for r in store.list_schedule_tasks("2026-05-10")}
    assert titles == {"Laundry", "Email", "Workshop"}


def test_merge_preserves_gcal_linked_when_omitted(store: SchedulerStore) -> None:
    store.replace_tasks_for_dates(
        ["2026-05-10"],
        [ScheduleRow("a", "2026-05-10", "8:00 AM", 60, "Workshop", "pending")],
    )
    _attach(store, "a", "evt-workshop")
    store.merge_tasks_from_assistant(
        ["2026-05-10"],
        [ScheduleRow("b", "2026-05-10", "3:00 PM", 60, "Walk", "pending")],
    )
    rows = store.list_schedule_tasks("2026-05-10")
    assert len(rows) == 2
    by_title = {r["title"]: r for r in rows}
    assert by_title["Workshop"]["gcal_event_id"] == "evt-workshop"
    assert by_title["Walk"]["title"] == "Walk"


def test_merge_supersedes_local_slot_with_different_title(store: SchedulerStore) -> None:
    store.replace_tasks_for_dates(
        ["2026-05-10"],
        [ScheduleRow("a", "2026-05-10", "8:00 AM", 60, "Laundry", "pending")],
    )
    store.merge_tasks_from_assistant(
        ["2026-05-10"],
        [ScheduleRow("b", "2026-05-10", "8:00 AM", 60, "Workshop", "pending")],
    )
    titles = {r["title"] for r in store.list_schedule_tasks("2026-05-10")}
    assert titles == {"Workshop"}


def test_merge_preserves_gcal_id_on_same_key(store: SchedulerStore) -> None:
    store.replace_tasks_for_dates(
        ["2026-05-10"],
        [ScheduleRow("a", "2026-05-10", "8:00 AM", 60, "Coding", "pending")],
    )
    _attach(store, "a", "evt-code")
    store.merge_tasks_from_assistant(
        ["2026-05-10"],
        [ScheduleRow("b", "2026-05-10", "8:00 AM", 90, "Coding", "pending")],
    )
    rows = store.list_schedule_tasks("2026-05-10")
    assert len(rows) == 1
    assert rows[0]["duration_minutes"] == 90
    assert rows[0]["gcal_event_id"] == "evt-code"
