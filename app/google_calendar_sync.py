"""Google Calendar bidirectional sync for the day-scheduler app.

This module wires :class:`scheduler_store.SchedulerStore` rows to Google Calendar v3
events without coupling the store to OAuth or HTTP. It exposes:

- ``task_event_body``                 — ``ScheduleRow`` -> Calendar API insert body.
- ``parse_event_to_row``              — Calendar API event -> ``ScheduleRow``.
- :class:`CalendarSyncManager`        — push dirty rows + pull incremental changes.

Sync model
==========

- Local task is the primary editable unit. Each row has ``gcal_event_id`` (link),
  ``gcal_etag`` (server version), ``gcal_calendar_id`` (which calendar), ``gcal_dirty``
  (push pending), ``gcal_deleted`` (soft-deleted; worker should delete the gcal event
  then drop the row).
- Push: iterate dirty rows; create / patch / delete on Calendar.
- Pull: ``events.list`` with ``syncToken`` (incremental). On HTTP 410, drop the token
  and do a full backfill within the configured horizon.

The OAuth client + low-level HTTPS calls live in ``samples/google_calendar_client.py``;
the worker re-uses :func:`acquire_access_token_silently` and :func:`load_or_refresh_credentials`.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

_SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
if _SAMPLES_DIR.is_dir() and str(_SAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_SAMPLES_DIR))

if TYPE_CHECKING:
    from scheduler_store import ScheduleRow, SchedulerStore

LOG = logging.getLogger(__name__)
LOG.addHandler(logging.NullHandler())

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

DEFAULT_PULL_HORIZON_DAYS = 60
"""When doing a full backfill (no syncToken), pull events within this many days."""

DEFAULT_POLL_INTERVAL_SEC = 30.0
"""Delay between background pull cycles."""

GCAL_PRIVATE_NAMESPACE = "schedulerApp"
"""Namespace used for ``extendedProperties.private.task_id`` round-trip linking."""


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------


_TIME_LABEL_RE = re.compile(
    r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*(AM|PM)\s*$",
    re.IGNORECASE,
)


def parse_start_label(start_label: str) -> tuple[int, int] | None:
    """``"8:00 AM"`` -> ``(8, 0)``; ``"12:30 PM"`` -> ``(12, 30)``; bad input -> ``None``."""
    m = _TIME_LABEL_RE.match(start_label or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ap == "AM":
        h24 = 0 if h == 12 else h
    else:
        h24 = 12 if h == 12 else h + 12
    return h24, mi


def format_start_label(h24: int, mi: int) -> str:
    ap = "AM" if h24 < 12 else "PM"
    h12 = h24 % 12 or 12
    return f"{h12}:{mi:02d} {ap}"


def task_datetimes(
    plan_date: str,
    start_label: str,
    duration_minutes: int,
) -> tuple[datetime, datetime] | None:
    """Build naive ``(start, end)`` datetimes from a row (no tzinfo attached)."""
    parsed = parse_start_label(start_label)
    if not parsed:
        return None
    try:
        y, m, d = (int(p) for p in plan_date.split("-"))
        base = date(y, m, d)
    except ValueError:
        return None
    start = datetime(base.year, base.month, base.day, parsed[0], parsed[1])
    end = start + timedelta(minutes=max(0, int(duration_minutes)))
    return start, end


# ---------------------------------------------------------------------------
# ScheduleRow <-> Calendar API event body
# ---------------------------------------------------------------------------


def task_event_body(
    row: ScheduleRow,
    *,
    tz_name: str,
    description: str | None = None,
    host_tz_name: str | None = None,
) -> dict[str, Any]:
    """Serialize a ``ScheduleRow`` to a Calendar API ``events.insert`` / ``patch`` body."""
    se = task_datetimes(row.plan_date, row.start_label, row.duration_minutes)
    if se is None:
        raise ValueError(f"task {row.task_id!r} has unparseable start/duration")
    start_dt, end_dt = se
    private_props: dict[str, str] = {
        f"{GCAL_PRIVATE_NAMESPACE}.task_id": row.task_id,
        f"{GCAL_PRIVATE_NAMESPACE}.push_timezone": tz_name,
    }
    if host_tz_name:
        private_props[f"{GCAL_PRIVATE_NAMESPACE}.host_timezone"] = str(host_tz_name)
    body: dict[str, Any] = {
        "summary": row.title,
        "start": {"dateTime": start_dt.isoformat(timespec="seconds"), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(timespec="seconds"), "timeZone": tz_name},
        "extendedProperties": {
            "private": private_props,
        },
    }
    if description:
        body["description"] = description
    return body


def _zoneinfo_best_effort(name: str):
    """Return ZoneInfo(name) when valid, else UTC."""
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo((name or "UTC").strip() or "UTC")
    except Exception:
        return UTC


def event_intersects_local_plan_date(
    event: dict[str, Any],
    *,
    plan_date_iso: str,
    tz_name: str,
) -> bool:
    """True if ``event`` overlaps ``plan_date_iso`` interpreted in IANA ``tz_name``.

    Day boundaries are the local midnight-to-midnight window for ``tz_name``.
    """
    if event.get("status") == "cancelled":
        return False

    zi = _zoneinfo_best_effort(tz_name)
    plan_day = date.fromisoformat(plan_date_iso)
    win_lo = datetime.combine(plan_day, dtime.min, tzinfo=zi)
    win_hi = win_lo + timedelta(days=1)

    start = event.get("start") or {}
    end = event.get("end") or {}
    sd = start.get("date")
    ed = end.get("date")
    if isinstance(sd, str):
        start_d = date.fromisoformat(sd)
        ed_excl = date.fromisoformat(ed) if isinstance(ed, str) else start_d + timedelta(days=1)
        return start_d < plan_day + timedelta(days=1) and ed_excl > plan_day

    s_iso = start.get("dateTime")
    e_iso = end.get("dateTime")
    if not isinstance(s_iso, str) or not isinstance(e_iso, str):
        return False

    evt_tz_start = (
        start.get("timeZone")
        if isinstance(start.get("timeZone"), str) and start.get("timeZone")
        else tz_name
    )
    evt_tz_end = (
        end.get("timeZone")
        if isinstance(end.get("timeZone"), str) and end.get("timeZone")
        else tz_name
    )

    zs = _zoneinfo_best_effort(str(evt_tz_start))
    ze = _zoneinfo_best_effort(str(evt_tz_end))

    def _parse_boundary(iso: str, zb) -> datetime:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zb)
        return dt.astimezone(zi)

    estart = _parse_boundary(s_iso, zs)
    eend = _parse_boundary(e_iso, ze)
    return estart < win_hi and eend > win_lo


def _event_local_minute_of_day(
    event_start_dt: str, default_tz: str
) -> tuple[date, int, str] | None:
    """Return ``(plan_date, minute_of_day, tz_name_used)`` for a Calendar event start.

    When ``timeZone`` is set on the event, interpret the dateTime in **that** zone.
    Otherwise, when ``dateTime`` carries an offset, convert it to ``default_tz``.
    """
    try:
        dt = datetime.fromisoformat(event_start_dt.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.date(), dt.hour * 60 + dt.minute, default_tz
    try:
        from zoneinfo import ZoneInfo

        target = ZoneInfo(default_tz)
    except Exception:
        target = UTC
    local = dt.astimezone(target)
    return local.date(), local.hour * 60 + local.minute, default_tz


def parse_event_to_row(
    event: dict[str, Any],
    *,
    fallback_tz: str,
    new_task_id: str | None = None,
) -> ScheduleRow | None:
    """Convert a Calendar API event resource to a ``ScheduleRow``.

    Returns ``None`` for events the local model can't represent (all-day, missing
    start/end, etc.). Re-uses ``extendedProperties.private.{namespace}.task_id``
    when the event was created by us (so re-creates align to the existing row).
    """
    from scheduler_store import ScheduleRow as _ScheduleRow

    if event.get("status") == "cancelled":
        return None

    start = event.get("start") or {}
    end = event.get("end") or {}
    if isinstance(start.get("date"), str) or isinstance(end.get("date"), str):
        return None

    s_iso = start.get("dateTime")
    e_iso = end.get("dateTime")
    if not isinstance(s_iso, str) or not isinstance(e_iso, str):
        return None
    s_tz = (
        start.get("timeZone")
        if isinstance(start.get("timeZone"), str) and start.get("timeZone")
        else fallback_tz
    )
    parsed_s = _event_local_minute_of_day(s_iso, default_tz=s_tz)
    parsed_e = _event_local_minute_of_day(e_iso, default_tz=s_tz)
    if parsed_s is None or parsed_e is None:
        return None
    plan_date, mod, _ = parsed_s
    end_date, end_mod, _ = parsed_e
    duration_minutes = max(0, int((end_date - plan_date).days * 24 * 60 + (end_mod - mod)))

    summary = event.get("summary") or "(untitled event)"
    h24, mi = divmod(mod, 60)
    start_label = format_start_label(h24, mi)
    private = (event.get("extendedProperties") or {}).get("private") or {}
    embedded_id = private.get(f"{GCAL_PRIVATE_NAMESPACE}.task_id")
    task_id = (
        embedded_id
        if isinstance(embedded_id, str) and embedded_id
        else (new_task_id or str(uuid.uuid4()))
    )
    return _ScheduleRow(
        task_id=task_id,
        plan_date=plan_date.isoformat(),
        start_label=start_label,
        duration_minutes=duration_minutes,
        title=str(summary).strip() or "(untitled event)",
        status="pending",
        gcal_event_id=str(event.get("id") or "") or None,
        gcal_etag=str(event.get("etag") or "") or None,
    )


# ---------------------------------------------------------------------------
# Low-level Calendar HTTP helpers
# ---------------------------------------------------------------------------


def _gcal_url(calendar_id: str, suffix: str = "") -> str:
    return f"{CALENDAR_BASE}/calendars/{quote(calendar_id, safe='')}/events{suffix}"


def insert_event(
    *, access_token: str, calendar_id: str, body: dict[str, Any], timeout: float = 30.0
) -> dict[str, Any]:
    resp = httpx.post(
        _gcal_url(calendar_id),
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def patch_event(
    *,
    access_token: str,
    calendar_id: str,
    event_id: str,
    body: dict[str, Any],
    if_match_etag: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    if if_match_etag:
        headers["If-Match"] = if_match_etag
    resp = httpx.patch(
        _gcal_url(calendar_id, "/" + quote(event_id, safe="")),
        headers=headers,
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def delete_event(
    *, access_token: str, calendar_id: str, event_id: str, timeout: float = 30.0
) -> int:
    resp = httpx.delete(
        _gcal_url(calendar_id, "/" + quote(event_id, safe="")),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    if resp.status_code in (404, 410):
        return resp.status_code
    resp.raise_for_status()
    return resp.status_code


def list_events_page(
    *,
    access_token: str,
    calendar_id: str,
    sync_token: str | None = None,
    page_token: str | None = None,
    time_min_iso: str | None = None,
    time_max_iso: str | None = None,
    max_results: int = 250,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """One page of ``events.list`` (sync or window mode).

    Calendar requires that ``syncToken`` not be combined with ``timeMin`` / ``timeMax`` /
    ``q`` (etc.). When the token is set, only ``pageToken`` and ``maxResults`` accompany it.
    """
    params: dict[str, str] = {"maxResults": str(max_results)}
    if sync_token:
        params["syncToken"] = sync_token
    else:
        params["singleEvents"] = "true"
        if time_min_iso:
            params["timeMin"] = time_min_iso
        if time_max_iso:
            params["timeMax"] = time_max_iso
    if page_token:
        params["pageToken"] = page_token
    resp = httpx.get(
        _gcal_url(calendar_id),
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=timeout,
    )
    if resp.status_code == 410:
        # Token expired; let caller restart.
        return {"items": [], "nextSyncToken": None, "_token_expired": True}
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# CalendarSyncManager — orchestrates push + incremental pull around a store.
# ---------------------------------------------------------------------------


@dataclass
class SyncOutcome:
    pushed_create: int = 0
    pushed_update: int = 0
    pushed_delete: int = 0
    pulled_upsert: int = 0
    pulled_delete: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "pushed_create": self.pushed_create,
            "pushed_update": self.pushed_update,
            "pushed_delete": self.pushed_delete,
            "pulled_upsert": self.pulled_upsert,
            "pulled_delete": self.pulled_delete,
            "errors": list(self.errors),
        }


class CalendarSyncManager:
    """Push dirty rows / pull events for a single user.

    Thread-safety: a single internal lock serializes all Calendar IO so the background
    poller and HTTP-driven mutations don't interleave events.list pages.
    """

    def __init__(
        self,
        *,
        store: SchedulerStore,
        client_secrets_path: Path,
        token_path: Path,
        default_calendar_id: str = "primary",
        local_tz_name: str | None = None,
        pull_horizon_days: int = DEFAULT_PULL_HORIZON_DAYS,
    ) -> None:
        self._store = store
        self._client_secrets_path = Path(client_secrets_path)
        self._token_path = Path(token_path)
        self._default_calendar_id = default_calendar_id
        self._local_tz_name = local_tz_name or _detect_local_iana()
        self._pull_horizon_days = max(1, int(pull_horizon_days))
        self._lock = threading.Lock()

    # ---- credential helpers ----

    def has_secrets(self) -> bool:
        return self._client_secrets_path.is_file()

    def has_token(self) -> bool:
        return self._token_path.is_file()

    def silent_credentials(self) -> tuple[Any | None, str | None]:
        """Refresh the saved token without opening a browser. Returns ``(creds, error)``."""
        from google_calendar_client import acquire_access_token_silently

        creds, err = acquire_access_token_silently(
            client_secrets_path=self._client_secrets_path,
            token_path=self._token_path,
        )
        if err:
            return None, err
        return creds, None

    def interactive_login(self) -> Any:
        """Run the browser OAuth flow (blocking) and persist a fresh token."""
        from google_calendar_client import load_or_refresh_credentials

        return load_or_refresh_credentials(
            client_secrets_path=self._client_secrets_path,
            token_path=self._token_path,
        )

    # ---- enable / disable ----

    def enable(self, *, calendar_id: str | None = None) -> dict[str, Any]:
        cal = (calendar_id or self._default_calendar_id).strip() or "primary"
        return self._store.set_gcal_sync_state(
            calendar_id=cal,
            enabled=True,
            clear_sync_token=True,
            last_error="",
        )

    def disable(self) -> None:
        self._store.disable_gcal_sync()

    def effective_timezone(self) -> str:
        """IANA zone used for task wall times on Calendar (browser first, server fallback)."""
        from zoneinfo import ZoneInfo

        row = self._store.get_gcal_sync_state()
        raw = ""
        if row and isinstance(row.get("client_tz"), str):
            raw = row["client_tz"].strip()
        if raw:
            try:
                ZoneInfo(raw)
                return raw
            except Exception:
                pass
        return self._local_tz_name

    def status(self) -> dict[str, Any]:
        state = self._store.get_gcal_sync_state() or {}
        secrets_ok = self.has_secrets()
        token_ok = self.has_token()
        return {
            "secrets_path": str(self._client_secrets_path),
            "token_path": str(self._token_path),
            "secrets_ok": secrets_ok,
            "token_ok": token_ok,
            "calendar_id": state.get("calendar_id") or self._default_calendar_id,
            "enabled": bool(state.get("enabled")),
            "last_sync_at": state.get("last_sync_at"),
            "last_error": state.get("last_error"),
            "has_sync_token": bool(state.get("sync_token")),
            "local_tz_name": self._local_tz_name,
            "client_tz": state.get("client_tz"),
            "push_timezone": self.effective_timezone(),
        }

    def delete_all_calendar_events_for_plan_date(
        self, plan_date_iso: str
    ) -> tuple[int, list[str]]:
        """Delete every Calendar event overlapping ``plan_date_iso``.

        Day bounds are interpreted in :meth:`effective_timezone`.
        """
        errors: list[str] = []
        state = self._store.get_gcal_sync_state()
        if not state or not state.get("enabled"):
            return 0, errors
        try:
            plan_day = date.fromisoformat(plan_date_iso)
        except ValueError:
            return 0, [f"bad plan_date_iso: {plan_date_iso!r}"]

        tz_name = self.effective_timezone()
        calendar_id = str(state.get("calendar_id") or "primary")

        with self._lock:
            creds, err = self.silent_credentials()
            if err or creds is None:
                return 0, [f"oauth: {err or 'no credentials'}"]
            access_token = getattr(creds, "token", None)
            if not isinstance(access_token, str) or not access_token:
                return 0, ["oauth: no access token"]

            zi = _zoneinfo_best_effort(tz_name)
            mid_local = datetime.combine(plan_day, dtime(12, 0), tzinfo=zi)
            mid_utc = mid_local.astimezone(UTC)
            time_min_iso = (mid_utc - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
            time_max_iso = (mid_utc + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")

            removed = 0
            page_token: str | None = None
            while True:
                page = list_events_page(
                    access_token=access_token,
                    calendar_id=calendar_id,
                    sync_token=None,
                    page_token=page_token,
                    time_min_iso=time_min_iso,
                    time_max_iso=time_max_iso,
                )
                if page.get("_token_expired"):
                    break
                for event in page.get("items") or []:
                    if not event_intersects_local_plan_date(
                        event,
                        plan_date_iso=plan_date_iso,
                        tz_name=tz_name,
                    ):
                        continue
                    eid = str(event.get("id") or "")
                    if not eid:
                        continue
                    try:
                        delete_event(
                            access_token=access_token,
                            calendar_id=calendar_id,
                            event_id=eid,
                        )
                        removed += 1
                    except httpx.HTTPStatusError as exc:
                        errors.append(f"delete {eid}: HTTP {exc.response.status_code}")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"delete {eid}: {exc}")
                page_token = page.get("nextPageToken")
                if not page_token:
                    break

        return removed, errors

    # ---- main entry points ----

    def sync_once(self) -> SyncOutcome:
        """Run one push + pull cycle."""
        outcome = SyncOutcome()
        state = self._store.get_gcal_sync_state()
        if not state or not state.get("enabled"):
            return outcome
        with self._lock:
            creds, err = self.silent_credentials()
            if err or creds is None:
                outcome.errors.append(f"oauth: {err or 'no credentials'}")
                self._store.set_gcal_sync_state(
                    calendar_id=str(state.get("calendar_id") or "primary"),
                    last_error=err or "needs_browser_login",
                )
                return outcome
            access_token = getattr(creds, "token", None)
            if not isinstance(access_token, str) or not access_token:
                outcome.errors.append("oauth: no access token")
                return outcome
            calendar_id = str(state.get("calendar_id") or "primary")
            try:
                self._push_dirty(access_token, calendar_id, outcome)
            except Exception as exc:  # noqa: BLE001
                outcome.errors.append(f"push: {exc}")
                LOG.warning("CalendarSyncManager push failed: %s", exc)
            try:
                self._pull_incremental(access_token, calendar_id, state, outcome)
            except Exception as exc:  # noqa: BLE001
                outcome.errors.append(f"pull: {exc}")
                LOG.warning("CalendarSyncManager pull failed: %s", exc)
            self._store.set_gcal_sync_state(
                calendar_id=calendar_id,
                last_sync_at=int(time.time() * 1000),
                last_error="\n".join(outcome.errors) if outcome.errors else "",
            )
        return outcome

    # ---- push ----

    def _push_dirty(self, access_token: str, calendar_id: str, outcome: SyncOutcome) -> None:
        from scheduler_store import ScheduleRow as _ScheduleRow

        for raw in self._store.list_gcal_dirty_rows(limit=200):
            row = _ScheduleRow(
                task_id=str(raw["task_id"]),
                plan_date=str(raw["plan_date"]),
                start_label=str(raw["start_label"]),
                duration_minutes=int(raw["duration_minutes"]),
                title=str(raw["title"]),
                status=str(raw["status"]),
                gcal_event_id=raw.get("gcal_event_id"),
                gcal_etag=raw.get("gcal_etag"),
                gcal_calendar_id=raw.get("gcal_calendar_id"),
            )
            soft_deleted = bool(raw.get("gcal_deleted"))
            event_id = row.gcal_event_id or ""
            row_calendar = (row.gcal_calendar_id or calendar_id).strip() or calendar_id
            try:
                if soft_deleted:
                    if event_id:
                        delete_event(
                            access_token=access_token,
                            calendar_id=row_calendar,
                            event_id=event_id,
                        )
                        outcome.pushed_delete += 1
                    self._store.hard_delete_synced_task(row.task_id)
                    continue

                if row.status == "cancelled":
                    if event_id:
                        delete_event(
                            access_token=access_token,
                            calendar_id=row_calendar,
                            event_id=event_id,
                        )
                        outcome.pushed_delete += 1
                    self._store.hard_delete_synced_task(row.task_id)
                    continue

                body = task_event_body(
                    row,
                    tz_name=self.effective_timezone(),
                    host_tz_name=self._local_tz_name,
                )
                if event_id:
                    updated = patch_event(
                        access_token=access_token,
                        calendar_id=row_calendar,
                        event_id=event_id,
                        body=body,
                    )
                    self._store.attach_gcal_event(
                        task_id=row.task_id,
                        gcal_event_id=str(updated.get("id") or event_id),
                        gcal_etag=str(updated.get("etag") or ""),
                        gcal_calendar_id=row_calendar,
                    )
                    outcome.pushed_update += 1
                else:
                    created = insert_event(
                        access_token=access_token,
                        calendar_id=row_calendar,
                        body=body,
                    )
                    self._store.attach_gcal_event(
                        task_id=row.task_id,
                        gcal_event_id=str(created.get("id") or ""),
                        gcal_etag=str(created.get("etag") or ""),
                        gcal_calendar_id=row_calendar,
                    )
                    outcome.pushed_create += 1
            except httpx.HTTPStatusError as exc:
                outcome.errors.append(f"push {row.task_id}: HTTP {exc.response.status_code}")
                LOG.warning("Calendar push failed for %s: %s", row.task_id, exc.response.text[:200])
            except Exception as exc:  # noqa: BLE001
                outcome.errors.append(f"push {row.task_id}: {exc}")
                LOG.warning("Calendar push raised for %s: %s", row.task_id, exc)

    # ---- pull ----

    def _pull_incremental(
        self,
        access_token: str,
        calendar_id: str,
        state: dict[str, Any],
        outcome: SyncOutcome,
    ) -> None:
        sync_token = state.get("sync_token") or None
        time_min_iso: str | None = None
        time_max_iso: str | None = None
        if not sync_token:
            now = datetime.now(UTC)
            time_min_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            time_max_iso = (now + timedelta(days=self._pull_horizon_days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        next_sync: str | None = None
        page_token: str | None = None
        while True:
            page = list_events_page(
                access_token=access_token,
                calendar_id=calendar_id,
                sync_token=sync_token,
                page_token=page_token,
                time_min_iso=time_min_iso,
                time_max_iso=time_max_iso,
            )
            if page.get("_token_expired"):
                self._store.set_gcal_sync_state(
                    calendar_id=calendar_id,
                    clear_sync_token=True,
                )
                return
            for event in page.get("items") or []:
                self._ingest_pulled_event(event, calendar_id, outcome)
            page_token = page.get("nextPageToken")
            next_sync = page.get("nextSyncToken") or next_sync
            if not page_token:
                break

        if next_sync:
            self._store.set_gcal_sync_state(
                calendar_id=calendar_id,
                sync_token=next_sync,
            )

    def _ingest_pulled_event(
        self,
        event: dict[str, Any],
        calendar_id: str,
        outcome: SyncOutcome,
    ) -> None:
        gcal_event_id = str(event.get("id") or "")
        if not gcal_event_id:
            return
        if event.get("status") == "cancelled":
            outcome.pulled_delete += int(self._store.remove_task_from_gcal_pull(gcal_event_id) > 0)
            return

        existing = self._store.find_task_by_gcal_event(gcal_event_id)
        existing_id = existing["task_id"] if existing else None
        row = parse_event_to_row(
            event,
            fallback_tz=self.effective_timezone(),
            new_task_id=existing_id,
        )
        if row is None:
            return
        if existing_id:
            row.task_id = existing_id
        self._store.upsert_task_from_gcal(
            task=row,
            gcal_calendar_id=calendar_id,
            gcal_etag=row.gcal_etag,
            existing_task_id=row.task_id,
        )
        outcome.pulled_upsert += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_local_iana() -> str:
    """Best-effort IANA name for ``datetime.now().astimezone()``."""
    tz = datetime.now().astimezone().tzinfo
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    name = tz.tzname(datetime.now()) if tz else None
    if name and "/" in name:
        return name
    return "UTC"


__all__ = [
    "CALENDAR_BASE",
    "CalendarSyncManager",
    "DEFAULT_POLL_INTERVAL_SEC",
    "DEFAULT_PULL_HORIZON_DAYS",
    "GCAL_PRIVATE_NAMESPACE",
    "SyncOutcome",
    "delete_event",
    "event_intersects_local_plan_date",
    "format_start_label",
    "insert_event",
    "list_events_page",
    "parse_event_to_row",
    "parse_start_label",
    "patch_event",
    "task_datetimes",
    "task_event_body",
]
