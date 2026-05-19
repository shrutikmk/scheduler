"""Calendar <-> SchedulerStore sync helpers + CalendarSyncManager (HTTP mocked)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from google_calendar_sync import (
    CalendarSyncManager,
    event_intersects_local_plan_date,
    format_start_label,
    parse_event_to_row,
    parse_start_label,
    task_datetimes,
    task_event_body,
)
from scheduler_store import ScheduleRow, SchedulerStore


def _attach(store: SchedulerStore, task_id: str, event_id: str) -> None:
    store.attach_gcal_event(
        task_id=task_id,
        gcal_event_id=event_id,
        gcal_etag=None,
        gcal_calendar_id="primary",
    )


# ---------- pure helpers ----------


def test_parse_start_label_round_trips() -> None:
    assert parse_start_label("8:00 AM") == (8, 0)
    assert parse_start_label("12:30 PM") == (12, 30)
    assert parse_start_label("12:00 AM") == (0, 0)
    assert parse_start_label("garbage") is None


def test_format_start_label() -> None:
    assert format_start_label(0, 0) == "12:00 AM"
    assert format_start_label(12, 0) == "12:00 PM"
    assert format_start_label(8, 30) == "8:30 AM"
    assert format_start_label(13, 5) == "1:05 PM"


def test_task_datetimes_basic() -> None:
    se = task_datetimes("2026-05-09", "8:00 AM", 90)
    assert se is not None
    s, e = se
    assert s.isoformat() == "2026-05-09T08:00:00"
    assert e.isoformat() == "2026-05-09T09:30:00"


def test_task_event_body_includes_iana_zone_and_task_id() -> None:
    row = ScheduleRow("t-1", "2026-05-09", "8:00 AM", 60, "Coding", "pending")
    body = task_event_body(row, tz_name="America/Chicago")
    assert body["summary"] == "Coding"
    assert body["start"]["dateTime"].startswith("2026-05-09T08:00:00")
    assert body["start"]["timeZone"] == "America/Chicago"
    assert body["end"]["timeZone"] == "America/Chicago"
    extended = body["extendedProperties"]["private"]
    assert extended["schedulerApp.task_id"] == "t-1"
    assert extended["schedulerApp.push_timezone"] == "America/Chicago"


def test_task_event_body_audit_host_timezone_when_provided() -> None:
    row = ScheduleRow("t-1", "2026-05-09", "8:00 AM", 60, "Coding", "pending")
    body = task_event_body(row, tz_name="Pacific/Honolulu", host_tz_name="Europe/London")
    ext = body["extendedProperties"]["private"]
    assert ext["schedulerApp.push_timezone"] == "Pacific/Honolulu"
    assert ext["schedulerApp.host_timezone"] == "Europe/London"


def test_parse_event_to_row_uses_event_timezone() -> None:
    event = {
        "id": "abc",
        "etag": '"e1"',
        "summary": "Flight",
        "start": {"dateTime": "2026-05-14T18:55:00", "timeZone": "America/Chicago"},
        "end": {"dateTime": "2026-05-14T20:00:00", "timeZone": "America/Chicago"},
        "extendedProperties": {"private": {"schedulerApp.task_id": "linked-1"}},
    }
    row = parse_event_to_row(event, fallback_tz="UTC")
    assert row is not None
    assert row.task_id == "linked-1"
    assert row.title == "Flight"
    assert row.plan_date == "2026-05-14"
    assert row.start_label == "6:55 PM"
    assert row.duration_minutes == 65
    assert row.gcal_event_id == "abc"


def test_parse_event_skips_all_day_and_cancelled() -> None:
    assert (
        parse_event_to_row(
            {"start": {"date": "2026-05-14"}, "end": {"date": "2026-05-15"}},
            fallback_tz="UTC",
        )
        is None
    )
    assert parse_event_to_row({"status": "cancelled"}, fallback_tz="UTC") is None


def test_event_intersects_local_plan_date_all_day() -> None:
    ev = {
        "status": "confirmed",
        "start": {"date": "2026-05-09"},
        "end": {"date": "2026-05-10"},
    }
    assert event_intersects_local_plan_date(
        ev, plan_date_iso="2026-05-09", tz_name="America/Chicago"
    )
    assert not event_intersects_local_plan_date(
        ev, plan_date_iso="2026-05-10", tz_name="America/Chicago"
    )


def test_event_intersects_cancelled_false() -> None:
    ev = {
        "status": "cancelled",
        "start": {"date": "2026-05-09"},
        "end": {"date": "2026-05-10"},
    }
    assert not event_intersects_local_plan_date(ev, plan_date_iso="2026-05-09", tz_name="UTC")


# ---------- store-level dirty bookkeeping ----------


@pytest.fixture()
def store(tmp_path: Path) -> SchedulerStore:
    s = SchedulerStore(tmp_path / "scheduler.sqlite")
    s.init_schema()
    return s


def test_replace_tasks_marks_dirty_and_preserves_event_id(store: SchedulerStore) -> None:
    rows = [ScheduleRow("a", "2026-05-09", "8:00 AM", 60, "Coding", "pending")]
    store.replace_tasks_for_dates(["2026-05-09"], rows)
    store.attach_gcal_event(
        task_id="a",
        gcal_event_id="evt-1",
        gcal_etag='"v1"',
        gcal_calendar_id="primary",
    )
    assert store.list_gcal_dirty_rows() == []
    rows2 = [ScheduleRow("b", "2026-05-09", "8:00 AM", 90, "Coding", "pending")]
    store.replace_tasks_for_dates(["2026-05-09"], rows2)
    dirty = store.list_gcal_dirty_rows()
    assert len(dirty) == 1
    assert dirty[0]["task_id"] == "b"
    assert dirty[0]["gcal_event_id"] == "evt-1"
    assert int(dirty[0]["gcal_deleted"]) == 0


def test_disappearing_task_marked_deleted_and_dirty(store: SchedulerStore) -> None:
    rows = [
        ScheduleRow("a", "2026-05-09", "8:00 AM", 60, "Coding", "pending"),
        ScheduleRow("b", "2026-05-09", "9:00 AM", 30, "Email", "pending"),
    ]
    store.replace_tasks_for_dates(["2026-05-09"], rows)
    _attach(store, "a", "evt-a")
    _attach(store, "b", "evt-b")
    new_rows = [ScheduleRow("a2", "2026-05-09", "8:00 AM", 60, "Coding", "pending")]
    store.replace_tasks_for_dates(["2026-05-09"], new_rows)
    dirty = store.list_gcal_dirty_rows()
    deleted = [d for d in dirty if int(d["gcal_deleted"]) == 1]
    assert len(deleted) == 1
    assert deleted[0]["gcal_event_id"] == "evt-b"


def test_unsynced_task_can_be_dropped_without_calendar_call(store: SchedulerStore) -> None:
    rows = [ScheduleRow("a", "2026-05-09", "8:00 AM", 60, "Coding", "pending")]
    store.replace_tasks_for_dates(["2026-05-09"], rows)
    store.replace_tasks_for_dates(["2026-05-09"], [])
    assert store.list_gcal_dirty_rows() == []
    assert store.list_schedule_tasks("2026-05-09") == []


def test_status_change_marks_dirty(store: SchedulerStore) -> None:
    store.replace_tasks_for_dates(
        ["2026-05-09"], [ScheduleRow("a", "2026-05-09", "8:00 AM", 60, "Coding", "pending")]
    )
    _attach(store, "a", "evt-a")
    assert store.update_task_status("a", "cancelled")
    dirty = store.list_gcal_dirty_rows()
    assert any(d["task_id"] == "a" and d["status"] == "cancelled" for d in dirty)


# ---------- end-to-end CalendarSyncManager (HTTP mocked) ----------


def _mgr(tmp_path: Path, store: SchedulerStore) -> CalendarSyncManager:
    secrets = tmp_path / "client.json"
    secrets.write_text(
        '{"installed":{"client_id":"abc","client_secret":"xyz"}}',
        encoding="utf-8",
    )
    token = tmp_path / "oauth-token.json"
    token.write_text("{}", encoding="utf-8")
    return CalendarSyncManager(
        store=store,
        client_secrets_path=secrets,
        token_path=token,
        local_tz_name="America/Chicago",
        oauth_redirect_uri="http://127.0.0.1:8765/api/calendar/oauth/callback",
    )


class _FakeCreds:
    token = "tok"


def test_sync_once_creates_event_and_marks_clean(
    tmp_path: Path, store: SchedulerStore
) -> None:
    mgr = _mgr(tmp_path, store)
    store.set_gcal_sync_state(calendar_id="primary", enabled=True)
    store.replace_tasks_for_dates(
        ["2026-05-09"], [ScheduleRow("a", "2026-05-09", "8:00 AM", 60, "Coding", "pending")]
    )

    create_calls: list[dict] = []

    def fake_insert(*, access_token: str, calendar_id: str, body: dict, timeout: float = 30.0):
        create_calls.append(body)
        return {"id": "evt-new", "etag": '"v1"'}

    list_page = {"items": [], "nextSyncToken": "sync-1"}
    with (
        patch.object(
            CalendarSyncManager, "silent_credentials", return_value=(_FakeCreds(), None, None)
        ),
        patch("google_calendar_sync.insert_event", side_effect=fake_insert),
        patch("google_calendar_sync.list_events_page", return_value=list_page),
    ):
        out = mgr.sync_once()

    assert out.pushed_create == 1
    assert create_calls and create_calls[0]["summary"] == "Coding"
    assert create_calls[0]["start"]["timeZone"] == "America/Chicago"
    assert store.list_gcal_dirty_rows() == []
    rows = store.list_schedule_tasks("2026-05-09")
    assert rows[0]["gcal_event_id"] == "evt-new"
    state = store.get_gcal_sync_state()
    assert state and state["sync_token"] == "sync-1"


def test_sync_once_push_uses_persisted_client_tz(tmp_path: Path, store: SchedulerStore) -> None:
    mgr = _mgr(tmp_path, store)
    store.set_gcal_sync_state(calendar_id="primary", enabled=True)
    store.set_gcal_client_timezone("America/Los_Angeles")
    store.replace_tasks_for_dates(
        ["2026-05-09"], [ScheduleRow("a", "2026-05-09", "8:00 AM", 60, "Coding", "pending")]
    )

    create_calls: list[dict] = []

    def fake_insert(*, access_token: str, calendar_id: str, body: dict, timeout: float = 30.0):
        create_calls.append(body)
        return {"id": "evt-la", "etag": '"v1"'}

    list_page = {"items": [], "nextSyncToken": "sync-la"}
    with (
        patch.object(
            CalendarSyncManager, "silent_credentials", return_value=(_FakeCreds(), None, None)
        ),
        patch("google_calendar_sync.insert_event", side_effect=fake_insert),
        patch("google_calendar_sync.list_events_page", return_value=list_page),
    ):
        mgr.sync_once()

    assert create_calls and create_calls[0]["start"]["timeZone"] == "America/Los_Angeles"


def test_sync_once_deletes_when_soft_deleted(tmp_path: Path, store: SchedulerStore) -> None:
    mgr = _mgr(tmp_path, store)
    store.set_gcal_sync_state(calendar_id="primary", enabled=True)
    store.replace_tasks_for_dates(
        ["2026-05-09"], [ScheduleRow("a", "2026-05-09", "8:00 AM", 60, "Coding", "pending")]
    )
    _attach(store, "a", "evt-a")
    store.delete_schedule_tasks_for_date("2026-05-09")

    delete_calls: list[str] = []

    def fake_delete(*, access_token: str, calendar_id: str, event_id: str, timeout: float = 30.0):
        delete_calls.append(event_id)
        return 204

    list_page = {"items": [], "nextSyncToken": "tok"}
    with (
        patch.object(
            CalendarSyncManager, "silent_credentials", return_value=(_FakeCreds(), None, None)
        ),
        patch("google_calendar_sync.delete_event", side_effect=fake_delete),
        patch("google_calendar_sync.list_events_page", return_value=list_page),
    ):
        out = mgr.sync_once()

    assert out.pushed_delete == 1
    assert delete_calls == ["evt-a"]
    assert store.list_schedule_tasks("2026-05-09") == []


def test_sync_once_pulls_new_event_from_calendar(
    tmp_path: Path, store: SchedulerStore
) -> None:
    mgr = _mgr(tmp_path, store)
    store.set_gcal_sync_state(calendar_id="primary", enabled=True)
    pulled = {
        "id": "evt-pulled",
        "etag": '"v9"',
        "summary": "Standup",
        "start": {"dateTime": "2026-05-09T09:00:00", "timeZone": "America/Chicago"},
        "end": {"dateTime": "2026-05-09T09:15:00", "timeZone": "America/Chicago"},
    }
    list_page = {"items": [pulled], "nextSyncToken": "tok"}
    with (
        patch.object(
            CalendarSyncManager, "silent_credentials", return_value=(_FakeCreds(), None, None)
        ),
        patch("google_calendar_sync.list_events_page", return_value=list_page),
    ):
        out = mgr.sync_once()

    assert out.pulled_upsert == 1
    rows = store.list_schedule_tasks("2026-05-09")
    assert any(r["title"] == "Standup" and r["gcal_event_id"] == "evt-pulled" for r in rows)


def test_sync_once_records_oauth_failure(tmp_path: Path, store: SchedulerStore) -> None:
    mgr = _mgr(tmp_path, store)
    store.set_gcal_sync_state(calendar_id="primary", enabled=True)
    with patch.object(
        CalendarSyncManager, "silent_credentials", return_value=(None, "need_browser", None)
    ):
        out = mgr.sync_once()
    assert any("oauth" in e for e in out.errors)
    state = store.get_gcal_sync_state() or {}
    assert state.get("last_error")


def test_delete_all_calendar_events_filters_by_local_day(
    tmp_path: Path, store: SchedulerStore
) -> None:
    mgr = _mgr(tmp_path, store)
    store.set_gcal_sync_state(calendar_id="primary", enabled=True)
    store.set_gcal_client_timezone("America/Chicago")
    page = {
        "items": [
            {
                "id": "on-day",
                "start": {"dateTime": "2026-05-09T14:30:00", "timeZone": "America/New_York"},
                "end": {"dateTime": "2026-05-09T17:57:47", "timeZone": "America/New_York"},
            },
            {
                "id": "other-day",
                "start": {"dateTime": "2026-05-10T09:30:00", "timeZone": "America/New_York"},
                "end": {"dateTime": "2026-05-10T10:57:47", "timeZone": "America/New_York"},
            },
        ]
    }

    deletes: list[str] = []

    def fake_delete(*, access_token: str, calendar_id: str, event_id: str, timeout: float = 30.0):
        deletes.append(event_id)
        return 204

    with (
        patch.object(
            CalendarSyncManager, "silent_credentials", return_value=(_FakeCreds(), None, None)
        ),
        patch("google_calendar_sync.list_events_page", return_value=page),
        patch("google_calendar_sync.delete_event", side_effect=fake_delete),
    ):
        removed, errs = mgr.delete_all_calendar_events_for_plan_date("2026-05-09")

    assert errs == []
    assert removed == 1
    assert deletes == ["on-day"]


def test_begin_browser_oauth_returns_authorization_url(
    tmp_path: Path, store: SchedulerStore
) -> None:
    mgr = _mgr(tmp_path, store)
    with patch(
        "google_calendar_client.create_installed_app_flow",
        return_value=object(),
    ), patch(
        "google_calendar_client.oauth_authorization_url",
        return_value=("https://accounts.google.com/o/oauth2/auth?x=1", "state-123"),
    ):
        payload = mgr.begin_browser_oauth()
    assert payload["authorization_url"].startswith("https://accounts.google.com/")
    assert payload["state"] == "state-123"


def test_complete_browser_oauth_rejects_unknown_state(
    tmp_path: Path, store: SchedulerStore
) -> None:
    mgr = _mgr(tmp_path, store)
    with pytest.raises(ValueError, match="expired or unknown"):
        mgr.complete_browser_oauth(
            "http://127.0.0.1:8765/api/calendar/oauth/callback?code=abc&state=missing"
        )


def test_complete_browser_oauth_persists_token(
    tmp_path: Path, store: SchedulerStore
) -> None:
    mgr = _mgr(tmp_path, store)
    fake_flow = object()
    fake_creds = type("C", (), {"to_json": lambda self: '{"token":"saved"}'})()

    with patch(
        "google_calendar_client.create_installed_app_flow",
        return_value=fake_flow,
    ), patch(
        "google_calendar_client.oauth_authorization_url",
        return_value=("https://accounts.google.com/o/oauth2/auth?x=1", "state-abc"),
    ):
        mgr.begin_browser_oauth()

    with patch(
        "google_calendar_client.oauth_exchange_code",
        return_value=fake_creds,
    ), patch("google_calendar_client.persist_credentials") as persist:
        mgr.complete_browser_oauth(
            "http://127.0.0.1:8765/api/calendar/oauth/callback?code=abc&state=state-abc"
        )
        persist.assert_called_once()


def test_delete_all_calendar_events_skips_when_sync_off(
    tmp_path: Path, store: SchedulerStore
) -> None:
    mgr = _mgr(tmp_path, store)
    removed, errs = mgr.delete_all_calendar_events_for_plan_date("2026-05-09")
    assert removed == 0
    assert errs == []
