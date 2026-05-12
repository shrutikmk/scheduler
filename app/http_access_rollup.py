"""Rolling up identical stderr HTTP access log lines into ×N bundles (noise reduction)."""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, cast

_ROLLUP_INSTANCES: list[AccessLogRollup] = []


class AccessLogRollup:
    """Buffer consecutive duplicate access lines per process; flush on route change or age."""

    __slots__ = ("_tag", "_env_key", "_default_sec", "_lock", "_streak")

    def __init__(
        self,
        *,
        stderr_tag: str,
        env_seconds_key: str,
        default_seconds: float = 45.0,
    ) -> None:
        self._tag = stderr_tag
        self._env_key = env_seconds_key
        self._default_sec = default_seconds
        self._lock = threading.Lock()
        self._streak: dict[str, Any] | None = None
        _ROLLUP_INSTANCES.append(self)

    @staticmethod
    def _wall_ts() -> str:
        return time.strftime("%d/%b/%Y %H:%M:%S", time.localtime())

    def _emit_streak(self, s: dict[str, Any]) -> None:
        key = cast(str, s["key"])
        n = cast(int, s["n"])
        wall0 = cast(str, s["wall_first"])
        if n <= 1:
            print(f"[{wall0}] [{self._tag}] {key}", file=sys.stderr, flush=True)
            return
        wall_emit = self._wall_ts()
        wall_last = cast(str, s.get("wall_last", wall0))
        print(
            f"[{wall_emit}] [{self._tag}] {key}  ×{n}  (since {wall0}; last {wall_last})",
            file=sys.stderr,
            flush=True,
        )

    def _flush_locked(self) -> None:
        if self._streak is not None:
            self._emit_streak(self._streak)
            self._streak = None

    def shutdown_flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def note(self, handler: BaseHTTPRequestHandler, line: str) -> None:
        raw = (os.environ.get(self._env_key) or "").strip() or str(self._default_sec)
        try:
            stack_sec = max(5.0, float(raw))
        except ValueError:
            stack_sec = float(self._default_sec)
        disable = raw.lower() in ("0", "off", "false", "no")

        now_m = time.monotonic()
        now_wall = handler.log_date_time_string()

        with self._lock:
            if disable:
                print(f"[{now_wall}] [{self._tag}] {line}", file=sys.stderr, flush=True)
                return

            if self._streak is None:
                self._streak = {
                    "key": line,
                    "n": 1,
                    "t0_mono": now_m,
                    "wall_first": now_wall,
                    "wall_last": now_wall,
                }
                return

            st = cast(dict[str, Any], self._streak)
            if st["key"] != line:
                self._flush_locked()
                self._streak = {
                    "key": line,
                    "n": 1,
                    "t0_mono": now_m,
                    "wall_first": now_wall,
                    "wall_last": now_wall,
                }
                return

            st["n"] = int(st["n"]) + 1  # type: ignore[assignment]
            st["wall_last"] = now_wall
            if now_m - float(st["t0_mono"]) >= stack_sec:
                self._flush_locked()
                self._streak = {
                    "key": line,
                    "n": 1,
                    "t0_mono": now_m,
                    "wall_first": now_wall,
                    "wall_last": now_wall,
                }


def _flush_all_rollups() -> None:
    for inst in list(_ROLLUP_INSTANCES):
        inst.shutdown_flush()


atexit.register(_flush_all_rollups)
