"""SQLite persistence for habits snapshot, calendar tasks, and chat turns (day-scheduler app).

Single-user, localhost-first. Controlled by ``SCHEDULER_DB`` (path to SQLite file).

Default DB path under repo ``data/scheduler.sqlite``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "scheduler.sqlite"

_ACTIVITY_STOPWORDS = {
    "a",
    "an",
    "and",
    "block",
    "deep",
    "focused",
    "for",
    "my",
    "of",
    "quick",
    "session",
    "task",
    "the",
    "to",
    "work",
}

_ACTIVITY_ALIASES = {
    "code": "coding",
    "coded": "coding",
    "codes": "coding",
    "coding": "coding",
    "dev": "coding",
    "develop": "coding",
    "developing": "coding",
    "development": "coding",
    "laundries": "laundry",
    "laundry": "laundry",
}


def default_db_path() -> Path:
    raw = os.environ.get("SCHEDULER_DB", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_DB


def _activity_tokens(text: str) -> list[str]:
    out: list[str] = []
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        tok = _ACTIVITY_ALIASES.get(raw, raw)
        if tok in _ACTIVITY_STOPWORDS:
            continue
        out.append(tok)
    return out


def normalize_activity_key(title: str) -> str:
    """Small deterministic activity grouper for learned duration/frequency stats."""
    toks = _activity_tokens(title)
    if "coding" in toks:
        return "coding"
    if not toks:
        return "task"
    return " ".join(toks[:4])


_START_LABEL_RE = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}\s+)?(\d{1,2})\s*:\s*(\d{2})\s*(AM|PM)\s*$",
    re.IGNORECASE,
)


def start_label_sort_minutes(label: str) -> int:
    """Wall-clock sort key for labels like ``8:00 AM`` / ``2026-05-09 7:43 PM`` (minute-of-day)."""
    m = _START_LABEL_RE.match((label or "").strip())
    if not m:
        return 24 * 60 + 1
    hour = int(m.group(1))
    minute = int(m.group(2))
    ap = m.group(3).upper()
    if ap == "PM":
        hour24 = 12 if hour == 12 else hour + 12
    else:
        hour24 = 0 if hour == 12 else hour
    return hour24 * 60 + minute


@dataclass
class ScheduleRow:
    task_id: str
    plan_date: str
    start_label: str
    duration_minutes: int
    title: str
    status: str
    gcal_event_id: str | None = None
    gcal_etag: str | None = None
    gcal_calendar_id: str | None = None


GCAL_DEFAULT_KEY = "default"
"""Single-user singleton in `gcal_sync_state`."""


class SchedulerStore:
    _SCHEMA_VER = 2

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_db_path()
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=30.0)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def init_schema(self) -> None:
        with self._lock:
            cn = self._connection()
            cn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS habits_snapshot (
                  id TEXT PRIMARY KEY,
                  snapshot_json TEXT NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_tasks (
                  task_id TEXT PRIMARY KEY,
                  plan_date TEXT NOT NULL,
                  start_label TEXT NOT NULL,
                  duration_minutes INTEGER NOT NULL,
                  title TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  source_turn INTEGER,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_tasks_date
                  ON schedule_tasks(plan_date);
                CREATE TABLE IF NOT EXISTS activity_events (
                  event_id TEXT PRIMARY KEY,
                  task_id TEXT,
                  activity_key TEXT NOT NULL,
                  title TEXT NOT NULL,
                  planned_minutes INTEGER NOT NULL,
                  actual_minutes INTEGER,
                  plan_date TEXT NOT NULL,
                  planned_start_label TEXT NOT NULL,
                  completed_at INTEGER NOT NULL,
                  created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_activity_events_key
                  ON activity_events(activity_key);
                CREATE INDEX IF NOT EXISTS idx_activity_events_completed_at
                  ON activity_events(completed_at);
                CREATE TABLE IF NOT EXISTS conversation_messages (
                  thread_id TEXT NOT NULL,
                  seq INTEGER NOT NULL,
                  role TEXT NOT NULL,
                  content TEXT NOT NULL,
                  assistant_raw TEXT,
                  PRIMARY KEY (thread_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_conv_thread ON conversation_messages(thread_id);
                CREATE TABLE IF NOT EXISTS gcal_sync_state (
                  state_key TEXT PRIMARY KEY,
                  calendar_id TEXT NOT NULL,
                  sync_token TEXT,
                  enabled INTEGER NOT NULL DEFAULT 0,
                  last_sync_at INTEGER,
                  last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS gcal_deletions (
                  gcal_event_id TEXT PRIMARY KEY,
                  calendar_id TEXT NOT NULL,
                  recorded_at INTEGER NOT NULL
                );
                """
            )
            self._migrate_schedule_tasks_columns(cn)
            self._migrate_gcal_sync_columns(cn)
            row = cn.execute("SELECT value FROM meta WHERE key = 'schema_ver'").fetchone()
            if row is None:
                cn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_ver', ?)",
                    (str(self._SCHEMA_VER),),
                )
            else:
                cn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_ver'",
                    (str(self._SCHEMA_VER),),
                )
            cn.commit()

    def _migrate_schedule_tasks_columns(self, cn: sqlite3.Connection) -> None:
        cur = cn.execute("PRAGMA table_info(schedule_tasks)")
        cols = {row[1] for row in cur.fetchall()}
        for name, ddl in (
            ("gcal_event_id", "ALTER TABLE schedule_tasks ADD COLUMN gcal_event_id TEXT"),
            ("gcal_etag", "ALTER TABLE schedule_tasks ADD COLUMN gcal_etag TEXT"),
            ("gcal_calendar_id", "ALTER TABLE schedule_tasks ADD COLUMN gcal_calendar_id TEXT"),
            (
                "gcal_dirty",
                "ALTER TABLE schedule_tasks ADD COLUMN gcal_dirty INTEGER NOT NULL DEFAULT 1",
            ),
            (
                "gcal_deleted",
                "ALTER TABLE schedule_tasks ADD COLUMN gcal_deleted INTEGER NOT NULL DEFAULT 0",
            ),
        ):
            if name not in cols:
                cn.execute(ddl)
        cn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedule_tasks_gcal_event_id "
            "ON schedule_tasks(gcal_event_id)"
        )
        cn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedule_tasks_gcal_dirty ON schedule_tasks(gcal_dirty)"
        )

    def _migrate_gcal_sync_columns(self, cn: sqlite3.Connection) -> None:
        cur = cn.execute("PRAGMA table_info(gcal_sync_state)")
        cols = {row[1] for row in cur.fetchall()}
        if "client_tz" not in cols:
            cn.execute("ALTER TABLE gcal_sync_state ADD COLUMN client_tz TEXT")

    def get_habits_snapshot(self) -> dict[str, Any]:
        with self._lock:
            cn = self._connection()
            row = cn.execute(
                "SELECT snapshot_json FROM habits_snapshot WHERE id = ?",
                ("default",),
            ).fetchone()
            if row is None:
                return {"id": "default", "habits": [], "selectedId": None, "updatedAt": None}
            return json.loads(row["snapshot_json"])

    def put_habits_snapshot(self, payload: dict[str, Any]) -> None:
        import time

        with self._lock:
            cn = self._connection()
            now = int(time.time() * 1000)
            snap = dict(payload)
            snap.setdefault("id", "default")
            if "updatedAt" not in snap:
                snap["updatedAt"] = now
            cn.execute(
                """
                INSERT INTO habits_snapshot(id, snapshot_json, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  snapshot_json = excluded.snapshot_json,
                  updated_at = excluded.updated_at
                """,
                (json.dumps(snap, ensure_ascii=False), snap["updatedAt"]),
            )
            cn.commit()

    def list_schedule_tasks(self, plan_date: str) -> list[dict[str, Any]]:
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                """
                SELECT task_id, plan_date, start_label, duration_minutes, title, status,
                       source_turn, created_at, updated_at,
                       gcal_event_id, gcal_etag, gcal_calendar_id, gcal_dirty, gcal_deleted
                FROM schedule_tasks
                WHERE plan_date = ? AND COALESCE(gcal_deleted, 0) = 0
                """,
                (plan_date,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            rows.sort(
                key=lambda r: (
                    start_label_sort_minutes(str(r.get("start_label", ""))),
                    str(r.get("title", "")).lower(),
                )
            )
            return rows

    def delete_schedule_tasks_for_date(self, plan_date: str) -> int:
        """Soft-delete schedule rows for one date so the GCal worker can drop their events."""
        import time

        now = int(time.time() * 1000)
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                """
                UPDATE schedule_tasks
                SET gcal_deleted = 1, gcal_dirty = 1, updated_at = ?
                WHERE plan_date = ? AND COALESCE(gcal_deleted, 0) = 0
                """,
                (now, plan_date),
            )
            self._purge_unsynced_deleted_locked(cn)
            cn.commit()
            return int(cur.rowcount or 0)

    def replace_tasks_for_dates(self, dates: list[str], new_rows: list[ScheduleRow]) -> int:
        """Replace tasks for ``dates`` while preserving Google Calendar links by (start, title).

        Rows from previous turns whose start_label+title still appear keep their ``gcal_event_id``.
        Disappearing rows get marked ``gcal_deleted=1, gcal_dirty=1`` so the worker removes them.
        """
        import time

        if not dates:
            return 0
        uniq = sorted(set(dates))
        now = int(time.time() * 1000)
        with self._lock:
            cn = self._connection()
            existing = cn.execute(
                f"""
                SELECT task_id, plan_date, start_label, title, gcal_event_id, gcal_etag,
                       gcal_calendar_id
                FROM schedule_tasks
                WHERE plan_date IN ({",".join(["?"] * len(uniq))})
                  AND COALESCE(gcal_deleted, 0) = 0
                """,
                uniq,
            ).fetchall()
            link_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
            for r in existing:
                key = (
                    str(r["plan_date"]),
                    str(r["start_label"]).strip().lower(),
                    str(r["title"]).strip().lower(),
                )
                link_by_key[key] = dict(r)

            new_keys: set[tuple[str, str, str]] = {
                (r.plan_date, r.start_label.strip().lower(), r.title.strip().lower())
                for r in new_rows
            }

            # Hard-delete prior rows that the new set is replacing under a new task_id.
            # Their gcal_event_id is preserved on the new row by the link map.
            removable_ids = [
                str(prior["task_id"])
                for key, prior in link_by_key.items()
                if key in new_keys
            ]
            if removable_ids:
                cn.execute(
                    f"DELETE FROM schedule_tasks WHERE task_id IN "
                    f"({','.join(['?'] * len(removable_ids))})",
                    removable_ids,
                )

            # Soft-delete prior rows that *disappeared* so the worker can drop their gcal events.
            cn.execute(
                f"""
                UPDATE schedule_tasks
                SET gcal_deleted = 1, gcal_dirty = 1, updated_at = ?
                WHERE plan_date IN ({",".join(["?"] * len(uniq))})
                  AND COALESCE(gcal_deleted, 0) = 0
                """,
                [now, *uniq],
            )

            for r in new_rows:
                key = (
                    r.plan_date,
                    r.start_label.strip().lower(),
                    r.title.strip().lower(),
                )
                prior = link_by_key.get(key)
                gcal_id = prior["gcal_event_id"] if prior else r.gcal_event_id
                gcal_etag = prior["gcal_etag"] if prior else r.gcal_etag
                gcal_cal = prior["gcal_calendar_id"] if prior else r.gcal_calendar_id
                cn.execute(
                    """
                    INSERT INTO schedule_tasks (
                      task_id, plan_date, start_label, duration_minutes, title,
                      status, created_at, updated_at,
                      gcal_event_id, gcal_etag, gcal_calendar_id, gcal_dirty, gcal_deleted
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,0)
                    ON CONFLICT(task_id) DO UPDATE SET
                      plan_date = excluded.plan_date,
                      start_label = excluded.start_label,
                      duration_minutes = excluded.duration_minutes,
                      title = excluded.title,
                      status = excluded.status,
                      updated_at = excluded.updated_at,
                      gcal_event_id = excluded.gcal_event_id,
                      gcal_etag = excluded.gcal_etag,
                      gcal_calendar_id = excluded.gcal_calendar_id,
                      gcal_dirty = 1,
                      gcal_deleted = 0
                    """,
                    (
                        r.task_id,
                        r.plan_date,
                        r.start_label,
                        r.duration_minutes,
                        r.title,
                        r.status,
                        now,
                        now,
                        gcal_id,
                        gcal_etag,
                        gcal_cal,
                    ),
                )

            self._purge_unsynced_deleted_locked(cn)
            cn.commit()
        return len(new_rows)

    def _purge_unsynced_deleted_locked(self, cn: sqlite3.Connection) -> None:
        """Drop soft-deleted rows that were never pushed to GCal (no event id)."""
        cn.execute(
            """
            DELETE FROM schedule_tasks
            WHERE COALESCE(gcal_deleted, 0) = 1
              AND (gcal_event_id IS NULL OR gcal_event_id = '')
            """
        )

    def update_task_status(self, task_id: str, status: str) -> bool:
        import time

        now = int(time.time() * 1000)
        with self._lock:
            cn = self._connection()
            before = cn.execute(
                """
                SELECT task_id, plan_date, start_label, duration_minutes, title, status,
                       created_at, updated_at
                FROM schedule_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if before is None:
                return False
            cur = cn.execute(
                """
                UPDATE schedule_tasks
                SET status = ?, gcal_dirty = 1, updated_at = ?
                WHERE task_id = ?
                """,
                (status, now, task_id),
            )
            if status == "done" and before["status"] != "done":
                self._record_activity_event_locked(cn, before, completed_at=now)
            cn.commit()
            return cur.rowcount > 0

    def _record_activity_event_locked(
        self,
        cn: sqlite3.Connection,
        task: sqlite3.Row,
        *,
        completed_at: int,
    ) -> None:
        planned_minutes = max(0, int(task["duration_minutes"]))
        actual_minutes = planned_minutes
        cn.execute(
            """
            INSERT INTO activity_events (
              event_id, task_id, activity_key, title, planned_minutes, actual_minutes,
              plan_date, planned_start_label, completed_at, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                str(task["task_id"]),
                normalize_activity_key(str(task["title"])),
                str(task["title"]),
                planned_minutes,
                actual_minutes,
                str(task["plan_date"]),
                str(task["start_label"]),
                completed_at,
                completed_at,
            ),
        )

    def list_activity_events(self) -> list[dict[str, Any]]:
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                """
                SELECT event_id, task_id, activity_key, title, planned_minutes, actual_minutes,
                       plan_date, planned_start_label, completed_at, created_at
                FROM activity_events
                ORDER BY completed_at DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]

    def learned_activity_context_for_text(self, user_text: str, *, limit: int = 8) -> str | None:
        query_tokens = set(_activity_tokens(user_text))
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                """
                SELECT activity_key,
                       COUNT(*) AS seen_count,
                       AVG(COALESCE(actual_minutes, planned_minutes)) AS typical_minutes,
                       MAX(completed_at) AS last_completed_at
                FROM activity_events
                GROUP BY activity_key
                ORDER BY seen_count DESC, last_completed_at DESC
                """
            )
            rows = [dict(r) for r in cur.fetchall()]

        relevant: list[dict[str, Any]] = []
        for row in rows:
            key = str(row["activity_key"])
            key_tokens = set(_activity_tokens(key))
            if query_tokens and not (query_tokens & key_tokens):
                continue
            relevant.append(row)
            if len(relevant) >= limit:
                break

        if not relevant:
            return None

        lines = ["[Learned — task timing patterns]"]
        for row in relevant:
            typical = int(round(float(row["typical_minutes"] or 0)))
            lines.append(
                f"- {row['activity_key']}: seen {row['seen_count']} time(s), typical {typical}m."
            )
        lines.append(
            "\nUse these as soft priors when the user does not specify an explicit duration. "
            "Explicit user durations still win."
        )
        return "\n".join(lines)

    def get_conversation(self, thread_id: str = "default") -> list[dict[str, str]]:
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                """
                SELECT role, content FROM conversation_messages
                WHERE thread_id = ?
                ORDER BY seq ASC
                """,
                (thread_id,),
            )
            return [{"role": row["role"], "content": row["content"]} for row in cur.fetchall()]

    def sync_conversation(
        self,
        messages: list[dict[str, Any]],
        *,
        thread_id: str = "default",
    ) -> None:
        with self._lock:
            cn = self._connection()
            cn.execute("DELETE FROM conversation_messages WHERE thread_id = ?", (thread_id,))
            for i, msg in enumerate(messages):
                role = str(msg.get("role", "")).strip()
                content = msg.get("content")
                raw = msg.get("assistant_raw")
                if role not in ("user", "assistant") or not isinstance(content, str):
                    continue
                cn.execute(
                    """
                    INSERT INTO conversation_messages
                      (thread_id, seq, role, content, assistant_raw)
                    VALUES (?,?,?,?,?)
                    """,
                    (thread_id, i, role, content, raw if isinstance(raw, str) else None),
                )
            cn.commit()

    # ---------------- Google Calendar sync state ----------------

    def get_gcal_sync_state(self, key: str = GCAL_DEFAULT_KEY) -> dict[str, Any] | None:
        with self._lock:
            cn = self._connection()
            row = cn.execute(
                """
                SELECT state_key, calendar_id, sync_token, enabled, last_sync_at, last_error,
                       client_tz
                FROM gcal_sync_state WHERE state_key = ?
                """,
                (key,),
            ).fetchone()
            return dict(row) if row else None

    def set_gcal_client_timezone(
        self,
        client_tz: str | None,
        *,
        key: str = GCAL_DEFAULT_KEY,
    ) -> None:
        """Persist the UI/browser IANA zone used to label naive task times on Google Calendar."""
        if not isinstance(client_tz, str) or not client_tz.strip():
            return
        tz = client_tz.strip()
        with self._lock:
            cn = self._connection()
            row = cn.execute(
                "SELECT calendar_id FROM gcal_sync_state WHERE state_key = ?",
                (key,),
            ).fetchone()
            calendar_id = str(row["calendar_id"]) if row else "primary"
            if row is None:
                cn.execute(
                    """
                    INSERT INTO gcal_sync_state (
                      state_key, calendar_id, sync_token, enabled,
                      last_sync_at, last_error, client_tz
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (key, calendar_id, None, 0, None, None, tz),
                )
            else:
                cn.execute(
                    "UPDATE gcal_sync_state SET client_tz = ? WHERE state_key = ?",
                    (tz, key),
                )
            cn.commit()

    def set_gcal_sync_state(
        self,
        *,
        calendar_id: str,
        enabled: bool | None = None,
        sync_token: str | None = None,
        last_error: str | None = None,
        last_sync_at: int | None = None,
        key: str = GCAL_DEFAULT_KEY,
        clear_sync_token: bool = False,
    ) -> dict[str, Any]:
        import time as _t

        with self._lock:
            cn = self._connection()
            row = cn.execute(
                "SELECT state_key, calendar_id, sync_token, enabled, last_sync_at, last_error "
                "FROM gcal_sync_state WHERE state_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                cn.execute(
                    """
                    INSERT INTO gcal_sync_state (
                      state_key, calendar_id, sync_token, enabled,
                      last_sync_at, last_error, client_tz
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        calendar_id,
                        None if clear_sync_token else sync_token,
                        1 if (enabled is None or enabled) else 0,
                        last_sync_at if last_sync_at is not None else int(_t.time() * 1000),
                        last_error,
                        None,
                    ),
                )
            else:
                fields: list[str] = []
                values: list[Any] = []
                if calendar_id != row["calendar_id"]:
                    fields.append("calendar_id = ?")
                    values.append(calendar_id)
                    fields.append("sync_token = NULL")
                if clear_sync_token:
                    fields.append("sync_token = NULL")
                elif sync_token is not None:
                    fields.append("sync_token = ?")
                    values.append(sync_token)
                if enabled is not None:
                    fields.append("enabled = ?")
                    values.append(1 if enabled else 0)
                if last_sync_at is not None:
                    fields.append("last_sync_at = ?")
                    values.append(last_sync_at)
                if last_error is not None or last_error == "":
                    fields.append("last_error = ?")
                    values.append(last_error or None)
                if fields:
                    values.append(key)
                    cn.execute(
                        f"UPDATE gcal_sync_state SET {', '.join(fields)} WHERE state_key = ?",
                        values,
                    )
            cn.commit()
            row = cn.execute(
                "SELECT state_key, calendar_id, sync_token, enabled, last_sync_at, last_error, "
                "client_tz FROM gcal_sync_state WHERE state_key = ?",
                (key,),
            ).fetchone()
            return dict(row) if row else {}

    def disable_gcal_sync(self, key: str = GCAL_DEFAULT_KEY) -> None:
        with self._lock:
            cn = self._connection()
            cn.execute(
                "UPDATE gcal_sync_state SET enabled = 0, sync_token = NULL WHERE state_key = ?",
                (key,),
            )
            cn.commit()

    def list_gcal_dirty_rows(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Rows that need to be created/updated/deleted on Google Calendar."""
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                """
                SELECT task_id, plan_date, start_label, duration_minutes, title, status,
                       gcal_event_id, gcal_etag, gcal_calendar_id, gcal_deleted, updated_at
                FROM schedule_tasks
                WHERE COALESCE(gcal_dirty, 0) = 1
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def attach_gcal_event(
        self,
        *,
        task_id: str,
        gcal_event_id: str,
        gcal_etag: str | None,
        gcal_calendar_id: str,
    ) -> None:
        with self._lock:
            cn = self._connection()
            cn.execute(
                """
                UPDATE schedule_tasks
                SET gcal_event_id = ?, gcal_etag = ?, gcal_calendar_id = ?, gcal_dirty = 0
                WHERE task_id = ?
                """,
                (gcal_event_id, gcal_etag, gcal_calendar_id, task_id),
            )
            cn.commit()

    def mark_gcal_clean(self, task_id: str) -> None:
        with self._lock:
            cn = self._connection()
            cn.execute(
                "UPDATE schedule_tasks SET gcal_dirty = 0 WHERE task_id = ?",
                (task_id,),
            )
            cn.commit()

    def hard_delete_synced_task(self, task_id: str) -> None:
        with self._lock:
            cn = self._connection()
            cn.execute("DELETE FROM schedule_tasks WHERE task_id = ?", (task_id,))
            cn.commit()

    def find_task_by_gcal_event(self, gcal_event_id: str) -> dict[str, Any] | None:
        with self._lock:
            cn = self._connection()
            row = cn.execute(
                """
                SELECT task_id, plan_date, start_label, duration_minutes, title, status,
                       gcal_event_id, gcal_etag, gcal_calendar_id, gcal_deleted
                FROM schedule_tasks
                WHERE gcal_event_id = ?
                """,
                (gcal_event_id,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_task_from_gcal(
        self,
        *,
        task: ScheduleRow,
        gcal_calendar_id: str,
        gcal_etag: str | None,
        existing_task_id: str | None = None,
    ) -> str:
        """Create or update a task pulled from Google Calendar. Does not mark dirty."""
        import time as _t

        now = int(_t.time() * 1000)
        with self._lock:
            cn = self._connection()
            tid = existing_task_id or task.task_id
            cn.execute(
                """
                INSERT INTO schedule_tasks (
                  task_id, plan_date, start_label, duration_minutes, title, status,
                  created_at, updated_at,
                  gcal_event_id, gcal_etag, gcal_calendar_id, gcal_dirty, gcal_deleted
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)
                ON CONFLICT(task_id) DO UPDATE SET
                  plan_date = excluded.plan_date,
                  start_label = excluded.start_label,
                  duration_minutes = excluded.duration_minutes,
                  title = excluded.title,
                  status = excluded.status,
                  updated_at = excluded.updated_at,
                  gcal_event_id = excluded.gcal_event_id,
                  gcal_etag = excluded.gcal_etag,
                  gcal_calendar_id = excluded.gcal_calendar_id,
                  gcal_dirty = 0,
                  gcal_deleted = 0
                """,
                (
                    tid,
                    task.plan_date,
                    task.start_label,
                    task.duration_minutes,
                    task.title,
                    task.status,
                    now,
                    now,
                    task.gcal_event_id,
                    gcal_etag,
                    gcal_calendar_id,
                ),
            )
            cn.commit()
            return tid

    def remove_task_from_gcal_pull(self, gcal_event_id: str) -> int:
        """Calendar told us this event is gone; drop the linked local task."""
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                "DELETE FROM schedule_tasks WHERE gcal_event_id = ?",
                (gcal_event_id,),
            )
            cn.commit()
            return int(cur.rowcount or 0)


def tasks_to_persist_facts_block(
    rows_by_date: dict[str, list[ScheduleRow]],
    *,
    max_dates: int = 14,
) -> str | None:
    """Build host context snippet from stored tasks."""
    parts: list[str] = []
    count = 0
    for d in sorted(rows_by_date.keys()):
        rs = rows_by_date[d]
        if not rs:
            continue
        pending = [r for r in rs if r.status == "pending"]
        pending.sort(
            key=lambda r: (
                start_label_sort_minutes(str(r.start_label).strip()),
                r.title.strip().lower(),
            )
        )
        lines = "; ".join(
            f"{r.title.strip()} ({r.start_label.strip()}, {r.duration_minutes}m)" for r in pending
        )
        if lines:
            parts.append(f"- **{d}** (stored): {lines}")
            count += 1
            if count >= max_dates:
                break
    if not parts:
        return None
    return (
        "[Persisted — tasks already saved in the planner database from earlier turns]\n"
        + "\n".join(parts)
        + "\n\nKeep these in sync: add/update/remove bullets if the user's message implies changes."
    )


def new_task_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "DEFAULT_DB",
    "GCAL_DEFAULT_KEY",
    "ScheduleRow",
    "SchedulerStore",
    "default_db_path",
    "normalize_activity_key",
    "start_label_sort_minutes",
    "new_task_id",
    "tasks_to_persist_facts_block",
]
