#!/usr/bin/env python3
"""Thin **day-scheduler shell** HTTP server — static HTML + SQLite + MLX LLM gateway proxy.

**Two terminals**

1. **LLM gateway (Metal):**

       uv run --group samples-mlx python samples/mlx_llm_gateway.py

2. **This UI:**

       uv run python samples/mlx_day_scheduler_ui.py

Open ``http://127.0.0.1:8765/`` — REST under ``/api/*`` persists habits, tasks per calendar day,
and conversation logs (SQLite ``SCHEDULER_DB`` or ``./data/scheduler.sqlite``).
``POST /api/tasks/clear_day`` deletes all saved tasks for a local date and optionally clears chat.

``POST /chat`` augments the JSON payload with planner hints + persisted-task context before
proxying NDJSON streams to ``MLX_SCHEDULER_LLM_API``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

SAMPLES_ROOT = Path(__file__).resolve().parent
if str(SAMPLES_ROOT) not in sys.path:
    sys.path.insert(0, str(SAMPLES_ROOT))

from habit_schedule import required_habits_context_block  # noqa: E402
from schedule_parse import collect_tasks_with_dates, planner_facts_injection  # noqa: E402
from scheduler_store import (  # noqa: E402
    ScheduleRow,
    SchedulerStore,
    default_db_path,
    new_task_id,
    tasks_to_persist_facts_block,
)

DEFAULT_UPSTREAM_LLM_API = (
    os.environ.get("MLX_SCHEDULER_LLM_API", "http://127.0.0.1:8766").strip().rstrip("/")
)


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
                "Start the gateway: uv run --group samples-mlx python samples/mlx_llm_gateway.py"
            ),
        }
    except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as e:
        return False, {
            "online": False,
            "upstream": origin,
            "detail": str(e),
            "hint": (
                "Start the gateway: uv run --group samples-mlx python samples/mlx_llm_gateway.py"
            ),
        }


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
            habits_block = required_habits_context_block(store.get_habits_snapshot(), anchor)
            _append_context_block(out, habits_block)
    learned = store.learned_activity_context_for_text(raw_content)
    _append_context_block(out, learned)
    return out


class DaySchedulerUiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        line = fmt % args if args else fmt
        print(
            f"[{self.log_date_time_string()}] [ui] {line}",
            file=sys.stderr,
            flush=True,
        )

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

        if path == "/api/habits":
            self._send_json(200, self._store().get_habits_snapshot())
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

        files = {
            "/": SAMPLES_ROOT / "day_scheduler.html",
            "/day_scheduler.html": SAMPLES_ROOT / "day_scheduler.html",
            "/habit_builder.html": SAMPLES_ROOT / "habit_builder.html",
        }
        fpath = files.get(path)
        if fpath is None or not fpath.is_file():
            self._send_binary(404, b"Not found\n", "text/plain; charset=utf-8")
            return
        data = fpath.read_bytes()
        self._send_binary(200, data, "text/html; charset=utf-8")

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

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

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
            touched, parsed = collect_tasks_with_dates(
                assistant, default_plan_date=anchor
            )
            if not parsed:
                self._send_json(200, {"ok": True, "inserted": 0, "dates": touched})
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
            self._send_json(200, {"ok": True, "inserted": n, "dates": touched})
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
            clear_conv = data.get("clear_conversation", True)
            if clear_conv is not False:
                self._store().sync_conversation([], thread_id="default")
            n = self._store().delete_schedule_tasks_for_date(day)
            self._send_json(200, {"ok": True, "plan_date": day, "deleted": n})
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
            self._send_json(200, {"ok": True, "task_id": task_id.strip(), "status": status})
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
            self._send_json(200, {"ok": True, "saved": total, "dates": alldates})
            return

        if path != "/chat":
            self._send_json(404, {"error": "Not found"})
            return

        upstream = getattr(self.server, "llm_origin", DEFAULT_UPSTREAM_LLM_API)
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

        ok_h, _h = fetch_upstream_health(upstream, timeout_sec=1.5)
        if not ok_h:
            self._send_upstream_error_json(
                503,
                "LLM gateway offline — start: "
                "uv run --group samples-mlx python samples/mlx_llm_gateway.py",
            )
            return

        url = upstream_chat_url(upstream)
        req = Request(url, data=body_fwd, method="POST")
        ctype = self.headers.get("Content-Type", "application/json")
        req.add_header("Content-Type", ctype)

        try:
            with urlopen(req, timeout=None) as resp:
                self.send_response(resp.status)
                ct = resp.headers.get("Content-Type")
                if ct:
                    self.send_header("Content-Type", ct)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except URLError as e:
            self._send_upstream_error_json(
                503,
                f"Upstream LLM unreachable: {e}",
            )


class ThreadedUiServer(ThreadingHTTPServer):
    llm_origin: str
    sched_store: SchedulerStore

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        llm_origin: str,
        sched_store: SchedulerStore,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.llm_origin = llm_origin.rstrip("/")
        self.sched_store = sched_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day-scheduler web shell (SQLite + MLX LLM API).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--llm-api",
        default=DEFAULT_UPSTREAM_LLM_API,
        help=(
            "Base URL of mlx_llm_gateway.py (env MLX_SCHEDULER_LLM_API; "
            "default http://127.0.0.1:8766)"
        ),
    )
    parser.add_argument(
        "--db",
        default="",
        help="SQLite path (defaults to SCHEDULER_DB env or ./data/scheduler.sqlite).",
    )

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])
    llm_origin = ns.llm_api.strip().rstrip("/")
    db_path = Path(ns.db).expanduser().resolve() if ns.db.strip() else default_db_path()
    store = SchedulerStore(db_path)
    store.init_schema()

    httpd = ThreadedUiServer(
        (ns.host, ns.port),
        DaySchedulerUiHandler,
        llm_origin=llm_origin,
        sched_store=store,
    )

    origin_ui = f"http://{ns.host}:{ns.port}/"
    print(f"Day scheduler shell → {origin_ui}", file=sys.stderr, flush=True)
    print(f"LLM upstream       → {llm_origin}", file=sys.stderr, flush=True)
    print(f"SQLite             → {db_path}", file=sys.stderr, flush=True)
    print(
        "(start gateway: uv run --group samples-mlx python samples/mlx_llm_gateway.py)\n",
        file=sys.stderr,
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShell server stopped.", file=sys.stderr)
    finally:
        httpd.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
