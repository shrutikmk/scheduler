"""SQLite persistence for habits snapshot, calendar tasks, and chat turns (samples UI server).

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

REPO_SAMPLES = Path(__file__).resolve().parent
PROJECT_ROOT = REPO_SAMPLES.parent
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
    r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*(AM|PM)\s*$",
    re.IGNORECASE,
)


def start_label_sort_minutes(label: str) -> int:
    """Wall-clock sort key for labels like ``8:00 AM`` / ``12:30 PM`` (minute-of-day)."""
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


class SchedulerStore:
    _SCHEMA_VER = 1

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
                """
            )
            row = cn.execute("SELECT value FROM meta WHERE key = 'schema_ver'").fetchone()
            if row is None:
                cn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_ver', ?)",
                    (str(self._SCHEMA_VER),),
                )
            cn.commit()

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
                       source_turn, created_at, updated_at
                FROM schedule_tasks
                WHERE plan_date = ?
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
        """Remove all schedule rows for one calendar day. Returns rows deleted."""
        with self._lock:
            cn = self._connection()
            cur = cn.execute(
                "DELETE FROM schedule_tasks WHERE plan_date = ?",
                (plan_date,),
            )
            cn.commit()
            return int(cur.rowcount or 0)

    def replace_tasks_for_dates(self, dates: list[str], new_rows: list[ScheduleRow]) -> int:
        import time

        if not dates:
            return 0
        uniq = sorted(set(dates))
        now = int(time.time() * 1000)
        with self._lock:
            cn = self._connection()
            cn.execute(
                f"DELETE FROM schedule_tasks WHERE plan_date IN ({','.join(['?'] * len(uniq))})",
                uniq,
            )
            for r in new_rows:
                cn.execute(
                    """
                    INSERT INTO schedule_tasks (
                      task_id, plan_date, start_label, duration_minutes, title,
                      status, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?)
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
                    ),
                )
            cn.commit()
        return len(new_rows)

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
                SET status = ?, updated_at = ?
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
                f"- {row['activity_key']}: seen {row['seen_count']} time(s), "
                f"typical {typical}m."
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
    "ScheduleRow",
    "SchedulerStore",
    "default_db_path",
    "normalize_activity_key",
    "start_label_sort_minutes",
    "new_task_id",
    "tasks_to_persist_facts_block",
]
