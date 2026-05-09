#!/usr/bin/env python3
"""Thin **day-scheduler shell** HTTP server — static HTML + proxy to MLX LLM gateway.

The browser talks only to this process (same origin). Chat ``POST /chat`` is forwarded to the
internal LLM API (default ``http://127.0.0.1:8766``).

**Two terminals:**

1. **LLM gateway (loads Qwen / MLX on Metal):**

       uv run --group samples-mlx python samples/mlx_llm_gateway.py

   Stop with ``Ctrl+C``.

2. **This UI (no model in memory):**

       uv run python samples/mlx_day_scheduler_ui.py

   Open ``http://127.0.0.1:8765/``

Override upstream URL::

    MLX_SCHEDULER_LLM_API=http://127.0.0.1:8766 \\
        uv run python samples/mlx_day_scheduler_ui.py

Or::

    uv run python samples/mlx_day_scheduler_ui.py --llm-api http://127.0.0.1:8766

Endpoints here:

- ``GET /`` — shell + habits iframe.
- ``GET /llm-health`` — JSON ``{online, upstream, …}`` (proxies gateway ``GET /health``).

``GET /chat`` returns 405; use ``POST`` from the page as before.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SAMPLES_ROOT = Path(__file__).resolve().parent

DEFAULT_UPSTREAM_LLM_API = (
    os.environ.get("MLX_SCHEDULER_LLM_API", "http://127.0.0.1:8766").strip().rstrip("/")
)


def upstream_chat_url(origin: str) -> str:
    return origin.rstrip("/") + "/v1/day-scheduler/chat"


def upstream_health_url(origin: str) -> str:
    return origin.rstrip("/") + "/health"


def fetch_upstream_health(origin: str, *, timeout_sec: float = 3.0) -> tuple[bool, dict]:
    """Return ``(reachable, merged_json)`` for the JS status strip."""
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

    def _send_upstream_error_json(self, code: int, message: str) -> None:
        self._send_json(
            code,
            {
                "error": message,
                "upstream": getattr(self.server, "llm_origin", DEFAULT_UPSTREAM_LLM_API),
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        upstream = getattr(self.server, "llm_origin", DEFAULT_UPSTREAM_LLM_API)

        if path == "/llm-health":
            _, body = fetch_upstream_health(upstream)
            self._send_json(200, body)
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

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/chat":
            self._send_upstream_error_json(404, "Not found")
            return

        upstream = getattr(self.server, "llm_origin", DEFAULT_UPSTREAM_LLM_API)
        ln = self.headers.get("Content-Length")
        try:
            n = int(ln or "0")
        except ValueError:
            self._send_upstream_error_json(400, "Bad Content-Length")
            return
        body = self.rfile.read(max(0, min(n, 4_000_000)))

        ok, _h = fetch_upstream_health(upstream, timeout_sec=1.5)
        if not ok:
            self._send_upstream_error_json(
                503,
                "LLM gateway offline — start: "
                "uv run --group samples-mlx python samples/mlx_llm_gateway.py",
            )
            return

        url = upstream_chat_url(upstream)
        req = Request(url, data=body, method="POST")
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
    """Holds llm_origin for handler."""

    llm_origin: str

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        llm_origin: str,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.llm_origin = llm_origin.rstrip("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day-scheduler web shell (proxies MLX LLM API).")
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

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])

    llm_origin = ns.llm_api.strip().rstrip("/")

    httpd = ThreadedUiServer((ns.host, ns.port), DaySchedulerUiHandler, llm_origin=llm_origin)

    origin_ui = f"http://{ns.host}:{ns.port}/"
    print(f"Day scheduler shell → {origin_ui}", file=sys.stderr, flush=True)
    print(f"LLM upstream       → {llm_origin}", file=sys.stderr, flush=True)
    print(
        "(start gateway separately: "
        "uv run --group samples-mlx python samples/mlx_llm_gateway.py)\n",
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
