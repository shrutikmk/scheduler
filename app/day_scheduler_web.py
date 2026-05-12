#!/usr/bin/env python3
"""Thin **day-scheduler shell** HTTP server — static HTML + SQLite + LLM gateway proxy.

Serves the **Heartbeat** product: **Scheduler** at ``/`` and **Finances** (analytics) at
``/finances`` on the same origin (default ``http://127.0.0.1:8765/``). Financial CSV APIs
under ``/api/files``, ``/api/summary``, ``/api/upload``, etc. share this process.

**Two terminals**

1. **LLM gateway (Metal):**

       uv run --group samples-vllm python app/scheduler_llm_gateway.py

2. **This UI:**

       uv run python app/day_scheduler_web.py

Open ``http://127.0.0.1:8765/`` — scheduler REST under ``/api/*`` persists habits, tasks per
calendar day, and conversation logs (SQLite ``SCHEDULER_DB`` or ``./data/scheduler.sqlite``).
``POST /api/tasks/clear_day`` deletes all saved tasks for a local date, removes every
overlapping Google Calendar event on the synced calendar when Calendar sync is enabled,
and optionally clears chat.

Financial ledger data lives under ``financial-data/`` (see ``app/financial_analytics_ui.py``).

``POST /chat`` augments the JSON payload with planner hints + persisted-task context before
proxying NDJSON streams to ``MLX_SCHEDULER_LLM_API``.

**Noise control:** Repeated identical access lines roll up into ``×N``. Set
``MLX_DAY_SCHEDULER_UI_ACCESS_LOG_STACK_SEC`` (seconds, default ``45``; ``0``/``off`` = every line).
Structured steps appear as ``[day_ui_flow] …``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from http_access_rollup import AccessLogRollup

_DAY_SCHED_UI_HTTP_ACCESS = AccessLogRollup(
    stderr_tag="ui",
    env_seconds_key="MLX_DAY_SCHEDULER_UI_ACCESS_LOG_STACK_SEC",
)


def _day_ui_flow_ts() -> str:
    return time.strftime("%d/%b/%Y %H:%M:%S", time.localtime())


def day_ui_flow_log(
    flow_id: str,
    message: str,
    *,
    lane: str = "pipeline",
    role: str | None = None,
    mlx: str | None = None,
    gateway: str | None = None,
) -> None:
    bits: list[str] = []
    if lane.strip():
        bits.append(lane.strip().upper())
    if role:
        bits.append(f"role={role}")
    if mlx:
        bits.append(f"mlx={mlx}")
    if gateway:
        bits.append(f"gateway={gateway}")
    head = (" ".join(bits) + " │ ") if bits else ""
    print(
        f"[{_day_ui_flow_ts()}] [day_ui_flow] [{flow_id}] {head}{message}",
        file=sys.stderr,
        flush=True,
    )


from google_calendar_sync import (  # noqa: E402
    DEFAULT_POLL_INTERVAL_SEC,
    CalendarSyncManager,
)
from habit_schedule import (  # noqa: E402
    habits_snapshot_with_required_rows,
    non_required_habits_context_block,
    required_habits_context_block,
)
from schedule_parse import collect_tasks_with_dates, planner_facts_injection  # noqa: E402
from scheduler_store import (  # noqa: E402
    ScheduleRow,
    SchedulerStore,
    default_db_path,
    new_task_id,
    tasks_to_persist_facts_block,
)

_SAMPLES_DIR = APP_ROOT.parent / "samples"
if _SAMPLES_DIR.is_dir() and str(_SAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(_SAMPLES_DIR))

from financial_analytics_ui import (  # noqa: E402
    _ledger_connection,
    _schedule_ledger_titling,
    financial_dispatch_delete,
    financial_dispatch_get,
    financial_dispatch_post,
)
from google_calendar_client import (  # noqa: E402
    default_calendar_oauth_client_secrets_path,
)

DEFAULT_UPSTREAM_LLM_API = (
    os.environ.get("MLX_SCHEDULER_LLM_API", "http://127.0.0.1:8766").strip().rstrip("/")
)


def persist_client_tz_from_payload(store: SchedulerStore, payload: dict[str, Any] | None) -> None:
    """Store browser IANA zone so Calendar pushes label naive task times correctly."""
    if not payload:
        return
    tz = payload.get("timezone")
    if isinstance(tz, str) and tz.strip():
        store.set_gcal_client_timezone(tz.strip())
        return
    cc = payload.get("client_calendar")
    if isinstance(cc, dict):
        inner = cc.get("timezone")
        if isinstance(inner, str) and inner.strip():
            store.set_gcal_client_timezone(inner.strip())


def upstream_chat_url(origin: str) -> str:
    return origin.rstrip("/") + "/v1/day-scheduler/chat"


def upstream_health_url(origin: str) -> str:
    return origin.rstrip("/") + "/health"


def fetch_upstream_health(origin: str, *, timeout_sec: float = 3.0) -> tuple[bool, dict]:
    try:
        req = Request(upstream_health_url(origin), method="GET")
        with urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
            parsed = cast(dict, json.loads(raw.decode("utf-8")))
            out = dict(parsed)
            out["online"] = True
            out["upstream"] = origin
            return True, out
    except URLError as e:
        err = getattr(e, "reason", None) or e
        return False, {
            "online": False,
            "upstream": origin,
            "detail": str(err),
            "hint": (
                "Start gateway: export VLLM_14B_BASE_URL=… then "
                "uv run --group samples-vllm python app/scheduler_llm_gateway.py"
            ),
        }
    except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as e:
        return False, {
            "online": False,
            "upstream": origin,
            "detail": str(e),
            "hint": (
                "Start gateway: export VLLM_14B_BASE_URL=… then "
                "uv run --group samples-vllm python app/scheduler_llm_gateway.py"
            ),
        }


_HEALTH_CACHE_TTL_SEC = 5.0
_health_cache_lock = threading.Lock()
_health_cache: dict[str, tuple[float, bool, dict]] = {}


def cached_upstream_health(
    origin: str,
    *,
    ttl: float = _HEALTH_CACHE_TTL_SEC,
    timeout_sec: float = 0.5,
) -> tuple[bool, dict]:
    """Cache successful health probes for a few seconds.

    The naive pre-flight cost is small per request (~ms on loopback) but it is
    on the *critical path* of every chat turn and a fresh probe still needs a
    full TCP+HTTP round-trip. Caching successes keeps `/chat` essentially free
    when the gateway is up; failures bypass the cache so recovery is fast.
    """
    now = time.monotonic()
    with _health_cache_lock:
        rec = _health_cache.get(origin)
        if rec is not None:
            ts, ok_prev, body_prev = rec
            if ok_prev and (now - ts) < ttl:
                return True, body_prev
    ok, body = fetch_upstream_health(origin, timeout_sec=timeout_sec)
    with _health_cache_lock:
        _health_cache[origin] = (time.monotonic(), ok, body)
    return ok, body


def invalidate_upstream_health_cache(origin: str | None = None) -> None:
    with _health_cache_lock:
        if origin is None:
            _health_cache.clear()
        else:
            _health_cache.pop(origin, None)


def _persisted_block_for_calendar(
    store: SchedulerStore, *, anchor_iso: str, user_raw: str
) -> str | None:
    from schedule_parse import infer_planner_date_hints

    try:
        y, m, dd = map(int, anchor_iso.split("-"))
        base_d = date(y, m, dd)
    except ValueError:
        return None

    hinted: set[str] = set(infer_planner_date_hints(user_raw, anchor_date_iso=anchor_iso))
    hinted.add(anchor_iso)
    hinted.add((base_d + timedelta(days=1)).isoformat())

    rows_by: dict[str, list[ScheduleRow]] = {}
    for day in sorted(hinted):
        lst = store.list_schedule_tasks(day)
        pending = [
            ScheduleRow(
                str(r["task_id"]),
                str(r["plan_date"]),
                str(r["start_label"]),
                int(r["duration_minutes"]),
                str(r["title"]),
                str(r["status"]),
            )
            for r in lst
            if r.get("status") == "pending"
        ]
        if pending:
            rows_by[day] = pending
    return tasks_to_persist_facts_block(rows_by)


def _append_context_block(out: dict[str, Any], block: str | None) -> None:
    if not block:
        return
    existing = out.get("persisted_tasks_context")
    if isinstance(existing, str) and existing.strip():
        out["persisted_tasks_context"] = existing.strip() + "\n\n" + block.strip()
    else:
        out["persisted_tasks_context"] = block.strip()


def augment_chat_payload(store: SchedulerStore, body: dict[str, Any]) -> dict[str, Any]:
    persist_client_tz_from_payload(store, body)
    out = dict(body)
    raw_content = body.get("content") or ""
    if not isinstance(raw_content, str):
        raw_content = str(raw_content)
    cc = body.get("client_calendar")
    if isinstance(cc, dict):
        anchor = cc.get("date_iso")
        if isinstance(anchor, str):
            pf = planner_facts_injection(raw_content, anchor_date_iso=anchor)
            if pf:
                out["content"] = f"{pf}\n\n{raw_content}".strip() if raw_content.strip() else pf
            pb = _persisted_block_for_calendar(store, anchor_iso=anchor, user_raw=raw_content)
            _append_context_block(out, pb)
            habits_snapshot = store.get_habits_snapshot()
            required_block = required_habits_context_block(habits_snapshot, anchor)
            _append_context_block(out, required_block)
            non_required_block = non_required_habits_context_block(habits_snapshot, anchor)
            _append_context_block(out, non_required_block)
    learned = store.learned_activity_context_for_text(raw_content)
    _append_context_block(out, learned)
    return out


class DaySchedulerUiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        line = fmt % args if args else fmt
        _DAY_SCHED_UI_HTTP_ACCESS.note(self, line)

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_binary(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any] | None:
        ln = self.headers.get("Content-Length")
        try:
            n = int(ln or "0")
        except ValueError:
            return None
        raw = self.rfile.read(max(0, min(n, 6_000_000)))
        try:
            return cast(dict[str, Any], json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

    def _read_body(self, cap: int = 20_000_000) -> bytes:
        ln = self.headers.get("Content-Length")
        try:
            n = int(ln or "0")
        except ValueError:
            return b""
        return self.rfile.read(max(0, min(n, cap)))

    def _store(self) -> SchedulerStore:
        return self.server.sched_store

    def _send_upstream_error_json(self, code: int, message: str) -> None:
        self._send_json(
            code,
            {
                "error": message,
                "upstream": getattr(self.server, "llm_origin", DEFAULT_UPSTREAM_LLM_API),
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        upstream = getattr(self.server, "llm_origin", DEFAULT_UPSTREAM_LLM_API)

        if path == "/llm-health":
            _, body = fetch_upstream_health(upstream)
            self._send_json(200, body)
            return

        if path == "/api/calendar/status":
            self._send_json(200, self._calendar_status_payload())
            return

        if path == "/api/habits":
            snapshot = self._store().get_habits_snapshot()
            rfd = qs.get("required_for_date", [None])[0]
            if isinstance(rfd, str) and rfd.strip():
                iso = rfd.strip()
                try:
                    date.fromisoformat(iso)
                except ValueError:
                    self._send_json(
                        400,
                        {"error": "Invalid required_for_date (expected YYYY-MM-DD)."},
                    )
                    return
                payload = habits_snapshot_with_required_rows(
                    cast(dict[str, Any], snapshot), iso
                )
                self._send_json(200, payload)
                return
            self._send_json(200, snapshot)
            return

        if path == "/api/tasks":
            day = qs.get("date", [None])[0]
            if not day or not isinstance(day, str):
                self._send_json(400, {"error": "Missing or invalid `date` query (YYYY-MM-DD)."})
                return
            rows = self._store().list_schedule_tasks(day)
            self._send_json(200, {"plan_date": day, "tasks": rows})
            return

        if path == "/api/conversation":
            tid = qs.get("thread_id", ["default"])[0] or "default"
            msgs = self._store().get_conversation(str(tid))
            self._send_json(200, {"thread_id": tid, "messages": msgs})
            return

        if financial_dispatch_get(self, path, qs):
            return

        static_pages: dict[str, tuple[str, str]] = {
            "/": ("day_scheduler.html", "text/html; charset=utf-8"),
            "/day_scheduler.html": ("day_scheduler.html", "text/html; charset=utf-8"),
            "/habit_builder.html": ("habit_builder.html", "text/html; charset=utf-8"),
            "/habit_builder.css": ("habit_builder.css", "text/css; charset=utf-8"),
            "/habit_builder.js": ("habit_builder.js", "application/javascript; charset=utf-8"),
            "/heartbeat_shell.css": ("heartbeat_shell.css", "text/css; charset=utf-8"),
            "/heartbeat_theme.css": ("heartbeat_theme.css", "text/css; charset=utf-8"),
        }
        entry = static_pages.get(path)
        if entry is None:
            self._send_binary(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        fname, ctype = entry
        fpath = APP_ROOT / fname
        if not fpath.is_file():
            self._send_binary(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        data = fpath.read_bytes()
        self._send_binary(200, data, ctype)

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/api/habits":
            self._send_json(404, {"error": "Not found"})
            return
        data = self._read_json_body()
        if data is None:
            self._send_json(400, {"error": "Invalid JSON"})
            return
        self._store().put_habits_snapshot(data)
        self._send_json(200, {"ok": True})
        day_ui_flow_log(
            uuid.uuid4().hex[:10],
            "SQLite habits snapshot saved (PUT /api/habits)",
            lane="sqlite",
            role="habits",
            mlx="posted",
        )

    def _calendar(self) -> CalendarSyncManager | None:
        return getattr(self.server, "gcal_manager", None)

    def _calendar_status_payload(self) -> dict[str, Any]:
        mgr = self._calendar()
        if mgr is None:
            return {
                "available": False,
                "reason": "Calendar sync manager not initialized.",
            }
        body = mgr.status()
        body["available"] = True
        return body

    def _trigger_calendar_sync_async(self) -> None:
        mgr = self._calendar()
        if mgr is None:
            return
        threading.Thread(target=_safe_calendar_sync, args=(mgr,), daemon=True).start()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if financial_dispatch_post(self, path):
            return

        if path == "/api/conversation":
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            msgs = data.get("messages")
            tid = str(data.get("thread_id") or "default")
            if not isinstance(msgs, list):
                self._send_json(400, {"error": "`messages` must be an array"})
                return
            self._store().sync_conversation(cast(list[dict[str, Any]], msgs), thread_id=tid)
            self._send_json(200, {"ok": True, "count": len(msgs)})
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                f"SQLite conversation synced messages={len(msgs)} thread_id={tid!r}",
                lane="sqlite",
                role="conversation",
                mlx="posted",
            )
            return

        if path == "/api/calendar/auth":
            mgr = self._calendar()
            if mgr is None:
                self._send_json(503, {"error": "Calendar sync manager unavailable"})
                return
            data = self._read_json_body() or {}
            persist_client_tz_from_payload(self._store(), data)
            cal = data.get("calendar_id") or "primary"
            if not isinstance(cal, str):
                self._send_json(400, {"error": "`calendar_id` must be a string"})
                return
            try:
                mgr.interactive_login()
            except FileNotFoundError as exc:
                self._send_json(424, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": f"OAuth failed: {exc}"})
                return
            mgr.enable(calendar_id=cal)
            self._trigger_calendar_sync_async()
            self._send_json(200, {"ok": True, "status": mgr.status()})
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                f"Google Calendar OAuth enabled calendar_id={cal!r}",
                lane="calendar",
                role="gcal_auth",
                mlx="posted",
            )
            return

        if path == "/api/calendar/disable":
            mgr = self._calendar()
            if mgr is None:
                self._send_json(503, {"error": "Calendar sync manager unavailable"})
                return
            mgr.disable()
            self._send_json(200, {"ok": True, "status": mgr.status()})
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                "Google Calendar sync disabled",
                lane="calendar",
                role="gcal_disable",
                mlx="posted",
            )
            return

        if path == "/api/calendar/sync":
            mgr = self._calendar()
            if mgr is None:
                self._send_json(503, {"error": "Calendar sync manager unavailable"})
                return
            sync_payload = self._read_json_body()
            if isinstance(sync_payload, dict):
                persist_client_tz_from_payload(self._store(), sync_payload)
            outcome = mgr.sync_once()
            self._send_json(
                200,
                {"ok": True, "status": mgr.status(), "outcome": outcome.to_dict()},
            )
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                f"manual GCal sync_once outcome={outcome.to_dict()}",
                lane="calendar",
                role="gcal_sync",
                mlx="posted",
            )
            return

        if path == "/api/tasks/import_from_assistant":
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            assistant = data.get("assistant")
            anchor = data.get("client_local_date")
            if not isinstance(assistant, str) or not isinstance(anchor, str):
                self._send_json(400, {"error": "Need `assistant` (str) and `client_local_date`."})
                return
            touched, parsed = collect_tasks_with_dates(assistant, default_plan_date=anchor)
            if not parsed:
                self._send_json(200, {"ok": True, "inserted": 0, "dates": touched})
                day_ui_flow_log(
                    uuid.uuid4().hex[:10],
                    f"assistant import skipped (parse 0 tasks) anchor={anchor!r}",
                    lane="sqlite",
                    role="task_import",
                    mlx="noop",
                )
                return
            rows = [
                ScheduleRow(
                    new_task_id(),
                    p.plan_date_iso,
                    p.start_label,
                    p.duration_minutes,
                    p.title.strip(),
                    "pending",
                )
                for p in parsed
            ]
            n = self._store().replace_tasks_for_dates(touched, rows)
            self._trigger_calendar_sync_async()
            self._send_json(200, {"ok": True, "inserted": n, "dates": touched})
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                f"SQLite merge from assistant: inserted_rows={n} dates={touched!r}",
                lane="sqlite",
                role="task_import",
                mlx="posted",
            )
            return

        if path == "/api/tasks/clear_day":
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            day = data.get("date")
            if not isinstance(day, str) or len(day) != 10 or day[4] != "-" or day[7] != "-":
                self._send_json(400, {"error": "`date` must be YYYY-MM-DD."})
                return
            persist_client_tz_from_payload(self._store(), data)
            clear_conv = data.get("clear_conversation", True)
            if clear_conv is not False:
                self._store().sync_conversation([], thread_id="default")
            calendar_deleted = 0
            calendar_errors: list[str] = []
            mgr = self._calendar()
            if mgr is not None:
                calendar_deleted, calendar_errors = (
                    mgr.delete_all_calendar_events_for_plan_date(day)
                )
            n = self._store().delete_schedule_tasks_for_date(day)
            self._trigger_calendar_sync_async()
            self._send_json(
                200,
                {
                    "ok": True,
                    "plan_date": day,
                    "deleted": n,
                    "calendar_events_deleted": calendar_deleted,
                    "calendar_clear_errors": calendar_errors,
                },
            )
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                f"clear_day {day!r} sqlite_tasks_removed={n} "
                f"gcal_events_removed={calendar_deleted} "
                f"cleared_chat={clear_conv is not False}",
                lane="sqlite",
                role="clear_day",
                mlx="posted",
            )
            return

        if path == "/api/tasks/complete":
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            task_id = data.get("task_id")
            status = data.get("status") or "done"
            if not isinstance(task_id, str) or not task_id.strip():
                self._send_json(400, {"error": "`task_id` required"})
                return
            if status not in ("done", "pending", "cancelled"):
                self._send_json(400, {"error": "`status` must be done, pending, or cancelled"})
                return
            updated = self._store().update_task_status(task_id.strip(), str(status))
            if not updated:
                self._send_json(404, {"error": "Task not found"})
                return
            self._trigger_calendar_sync_async()
            self._send_json(200, {"ok": True, "task_id": task_id.strip(), "status": status})
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                f"task_status task_id={task_id.strip()!r} → {status!r}",
                lane="sqlite",
                role="task_status",
                mlx="posted",
            )
            return

        if path == "/api/tasks/upsert":
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "Invalid JSON"})
                return
            items = data.get("tasks")
            if not isinstance(items, list):
                self._send_json(400, {"error": "`tasks` required"})
                return
            by_day: defaultdict[str, list[ScheduleRow]] = defaultdict(list)
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                pd = raw.get("plan_date")
                st = raw.get("start_label")
                title = raw.get("title")
                dur = raw.get("duration_minutes")
                status = raw.get("status") or "pending"
                tid = raw.get("task_id") or new_task_id()
                if isinstance(pd, str) and isinstance(st, str) and isinstance(title, str):
                    dm = dur if isinstance(dur, int) else int(dur) if dur is not None else 0
                    by_day[pd].append(
                        ScheduleRow(str(tid), pd, st, dm, title, str(status)),
                    )
            total = 0
            alldates = sorted(by_day.keys())
            for pd, lst in by_day.items():
                self._store().replace_tasks_for_dates([pd], lst)
                total += len(lst)
            self._trigger_calendar_sync_async()
            self._send_json(200, {"ok": True, "saved": total, "dates": alldates})
            day_ui_flow_log(
                uuid.uuid4().hex[:10],
                f"tasks upsert saved={total} plan_dates={alldates}",
                lane="sqlite",
                role="task_upsert",
                mlx="posted",
            )
            return

        if path != "/chat":
            self._send_json(404, {"error": "Not found"})
            return

        upstream = getattr(self.server, "llm_origin", DEFAULT_UPSTREAM_LLM_API)
        chat_fid = uuid.uuid4().hex[:10]
        ln = self.headers.get("Content-Length")
        try:
            n = int(ln or "0")
        except ValueError:
            self._send_upstream_error_json(400, "Bad Content-Length")
            return
        body_raw = self.rfile.read(max(0, min(n, 4_000_000)))

        try:
            parsed_body = cast(dict[str, Any], json.loads(body_raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_upstream_error_json(400, "Invalid JSON body")
            return

        aug = augment_chat_payload(self._store(), parsed_body)
        body_fwd = json.dumps(aug).encode("utf-8")
        u0 = aug.get("content") or aug.get("text") or ""
        u_len = len(u0) if isinstance(u0, str) else 0
        h = aug.get("history") if isinstance(aug.get("history"), list) else aug.get("messages")
        h_len = len(h) if isinstance(h, list) else 0

        ok_h, _h = cached_upstream_health(upstream, timeout_sec=0.5)
        if not ok_h:
            day_ui_flow_log(
                chat_fid,
                "upstream health negative before NDJSON proxy",
                lane="gateway",
                role="chat",
                mlx="from_gateway_error",
                gateway=upstream,
            )
            self._send_upstream_error_json(
                503,
                "LLM gateway offline — start: "
                "uv run --group samples-vllm python app/scheduler_llm_gateway.py",
            )
            return

        url = upstream_chat_url(upstream)
        day_ui_flow_log(
            chat_fid,
            f"POST /chat from browser incoming_bytes={len(body_raw)} augmented_bytes="
            f"{len(body_fwd)} content_chars≈{u_len} hist_turns≈{h_len}",
            lane="http",
            role="chat",
            mlx="from_client",
            gateway=upstream,
        )
        req = Request(url, data=body_fwd, method="POST")
        ctype = self.headers.get("Content-Type", "application/json")
        req.add_header("Content-Type", ctype)

        day_ui_flow_log(
            chat_fid,
            f"opening NDJSON stream proxy → POST {url}",
            lane="gateway",
            role="chat",
            mlx="to_gateway",
            gateway=upstream,
        )

        try:
            with urlopen(req, timeout=None) as resp:
                g_flow = resp.headers.get("X-Scheduler-Flow-ID")
                day_ui_flow_log(
                    chat_fid,
                    f"gateway stream started HTTP {resp.status} "
                    f"X-Scheduler-Flow-ID={g_flow!r} (pairs with gateway terminal)",
                    lane="gateway",
                    role="chat",
                    mlx="from_gateway_stream_open",
                    gateway=upstream,
                )
                self.send_response(resp.status)
                ct = resp.headers.get("Content-Type")
                if ct:
                    self.send_header("Content-Type", ct)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                end_normally = False
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        end_normally = True
                        break
                    self.wfile.write(chunk)
                    try:
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                if end_normally:
                    day_ui_flow_log(
                        chat_fid,
                        "gateway NDJSON stream finished (upstream EOF)",
                        lane="gateway",
                        role="chat",
                        mlx="from_gateway_stream_closed",
                        gateway=upstream,
                    )
        except HTTPError as e:
            err_body = e.read()
            day_ui_flow_log(
                chat_fid,
                f"gateway HTTPError during chat proxy code={getattr(e, 'code', '?')!r}",
                lane="gateway",
                role="chat",
                mlx="from_gateway_error",
                gateway=upstream,
            )
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except URLError as e:
            invalidate_upstream_health_cache(upstream)
            day_ui_flow_log(
                chat_fid,
                f"gateway URLError during chat ({e!s})",
                lane="gateway",
                role="chat",
                mlx="from_gateway_error",
                gateway=upstream,
            )
            self._send_upstream_error_json(
                503,
                f"Upstream LLM unreachable: {e}",
            )

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if financial_dispatch_delete(self, path, qs):
            return
        self._send_binary(404, b"Not found\n", "text/plain; charset=utf-8")


def _safe_calendar_sync(mgr: CalendarSyncManager) -> None:
    try:
        mgr.sync_once()
    except Exception as exc:  # noqa: BLE001
        print(f"[ui] calendar sync error: {exc}", file=sys.stderr, flush=True)


class CalendarPollerThread(threading.Thread):
    def __init__(self, manager: CalendarSyncManager, *, interval_sec: float) -> None:
        super().__init__(name="gcal-poller", daemon=True)
        self._manager = manager
        self._interval = max(5.0, float(interval_sec))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # Initial pause lets the HTTP server settle; then loop.
        self._stop.wait(2.0)
        while not self._stop.is_set():
            _safe_calendar_sync(self._manager)
            self._stop.wait(self._interval)


class ThreadedUiServer(ThreadingHTTPServer):
    llm_origin: str
    sched_store: SchedulerStore
    gcal_manager: CalendarSyncManager | None
    financial_label_model: str
    financial_insights_model: str
    ledger_llm_progress_lock: threading.Lock
    ledger_llm_progress: dict[str, Any]

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        llm_origin: str,
        sched_store: SchedulerStore,
        gcal_manager: CalendarSyncManager | None = None,
        financial_label_model: str = "",
        financial_insights_model: str = "",
    ) -> None:
        from ledger_llm_progress import initial_ledger_llm_progress

        super().__init__(server_address, RequestHandlerClass)
        self.llm_origin = llm_origin.rstrip("/")
        self.sched_store = sched_store
        self.gcal_manager = gcal_manager
        self.financial_label_model = financial_label_model
        self.financial_insights_model = financial_insights_model
        self.ledger_llm_progress_lock = threading.Lock()
        self.ledger_llm_progress = initial_ledger_llm_progress()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Day-scheduler web shell (SQLite + scheduler LLM gateway).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--llm-api",
        default=DEFAULT_UPSTREAM_LLM_API,
        help=(
            "Base URL of scheduler_llm_gateway.py (env MLX_SCHEDULER_LLM_API; "
            "default http://127.0.0.1:8766)"
        ),
    )
    parser.add_argument(
        "--db",
        default="",
        help="SQLite path (defaults to SCHEDULER_DB env or ./data/scheduler.sqlite).",
    )
    parser.add_argument(
        "--gcal-client-secrets",
        default="",
        help=(
            "OAuth Desktop client secrets JSON (default: "
            "credentials/google-calendar-oauth-client.json under repo root, "
            "or env GOOGLE_CALENDAR_CLIENT_SECRETS)."
        ),
    )
    parser.add_argument(
        "--gcal-token-cache",
        default="",
        help="Directory for OAuth token (default ~/.config/scheduler/calendar/).",
    )
    parser.add_argument(
        "--gcal-poll-sec",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SEC,
        help="Background Calendar sync interval (seconds). 0 disables polling.",
    )
    parser.add_argument(
        "--financial-label-model",
        default=None,
        help="Model id for ledger titles/categories (env MLX_FINANCIAL_LABEL_MODEL).",
    )
    parser.add_argument(
        "--financial-insights-model",
        default=None,
        help="Model id for financial insights (env MLX_FINANCIAL_INSIGHTS_MODEL).",
    )

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])
    llm_origin = ns.llm_api.strip().rstrip("/")
    from financial_llm_models import (  # noqa: E402
        resolve_financial_insights_model,
        resolve_financial_label_model,
    )

    label_m = resolve_financial_label_model(ns.financial_label_model)
    insight_m = resolve_financial_insights_model(ns.financial_insights_model)
    from ledger_llm_progress import short_model_label
    from vllm_gateway_routing import scheduler_inference_log_model

    vllm_fin_log_model = scheduler_inference_log_model(
        gateway_origin=llm_origin,
        client_model=label_m,
    )
    db_path = Path(ns.db).expanduser().resolve() if ns.db.strip() else default_db_path()
    store = SchedulerStore(db_path)
    store.init_schema()

    repo_root = APP_ROOT.parent
    secrets_arg = (
        ns.gcal_client_secrets.strip()
        or os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRETS", "").strip()
    )
    secrets_path = (
        Path(secrets_arg).expanduser()
        if secrets_arg
        else default_calendar_oauth_client_secrets_path(repo_root)
    )
    token_dir = (
        Path(ns.gcal_token_cache).expanduser().resolve()
        if ns.gcal_token_cache.strip()
        else (Path.home() / ".config" / repo_root.name / "calendar")
    )
    token_path = token_dir / "oauth-token.json"
    gcal_manager = CalendarSyncManager(
        store=store,
        client_secrets_path=secrets_path,
        token_path=token_path,
    )

    (repo_root / "financial-data").mkdir(parents=True, exist_ok=True)

    httpd = ThreadedUiServer(
        (ns.host, ns.port),
        DaySchedulerUiHandler,
        llm_origin=llm_origin,
        sched_store=store,
        gcal_manager=gcal_manager,
        financial_label_model=label_m,
        financial_insights_model=insight_m,
    )

    conn_kick = _ledger_connection()
    try:
        from financial_ledger_store import ledger_meta

        meta_kick = ledger_meta(conn_kick)
        if int(meta_kick.get("retitle_pending_count") or 0) > 0:
            with httpd.ledger_llm_progress_lock:
                already = bool(httpd.ledger_llm_progress.get("active"))
            if not already:
                _schedule_ledger_titling(llm_origin, label_m, httpd)
    finally:
        conn_kick.close()

    poller: CalendarPollerThread | None = None
    if ns.gcal_poll_sec > 0:
        poller = CalendarPollerThread(gcal_manager, interval_sec=ns.gcal_poll_sec)
        poller.start()

    origin_ui = f"http://{ns.host}:{ns.port}/"
    print(f"Heartbeat shell     → {origin_ui}", file=sys.stderr, flush=True)
    print(
        f"  Finances UI        → http://{ns.host}:{ns.port}/finances",
        file=sys.stderr,
        flush=True,
    )
    print(f"LLM upstream       → {llm_origin}", file=sys.stderr, flush=True)
    print(f"SQLite             → {db_path}", file=sys.stderr, flush=True)
    print(
        f"Financial labels (inference log tag) → {short_model_label(vllm_fin_log_model)}",
        file=sys.stderr,
        flush=True,
    )
    print(f"Financial insights    → {insight_m}", file=sys.stderr, flush=True)
    print(
        f"GCal secrets       → {secrets_path} (token: {token_path})",
        file=sys.stderr,
        flush=True,
    )
    if poller is None:
        print("GCal poller        → disabled", file=sys.stderr, flush=True)
    else:
        print(f"GCal poller        → every {ns.gcal_poll_sec:.0f}s", file=sys.stderr, flush=True)
    print(
        "(start gateway: uv run --group samples-vllm python app/scheduler_llm_gateway.py)\n",
        file=sys.stderr,
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShell server stopped.", file=sys.stderr)
    finally:
        if poller is not None:
            poller.stop()
        httpd.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
