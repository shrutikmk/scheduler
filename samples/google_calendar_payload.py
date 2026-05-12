"""Parse assistant replies into Calendar API-ready event payloads (no MLX / OAuth)."""

from __future__ import annotations

import json
import re
from typing import Any, cast

from mlx_day_scheduler_pipeline import strip_reasoning_blocks

_CODE_FENCE_JSON = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```",
    re.IGNORECASE,
)


def extract_calendar_payload(
    reply: str,
) -> tuple[list[dict[str, Any]], str] | tuple[None, str]:
    """Return ``(events, send_updates)`` or ``(None, error)``."""

    cleaned = strip_reasoning_blocks(reply.strip())
    if not cleaned:
        return None, "empty assistant message"

    obj: dict[str, Any] | None = None
    for match in _CODE_FENCE_JSON.finditer(cleaned):
        raw = match.group(1).strip()
        if '"events"' not in raw and "'events'" not in raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("events"), list):
            obj = parsed
            break

    if obj is None:
        start = cleaned.rfind("{")
        if start >= 0:
            snippet = cleaned[start:]
            for end in range(len(snippet), 0, -1):
                try:
                    cand = json.loads(snippet[:end])
                except json.JSONDecodeError:
                    continue
                if isinstance(cand, dict) and isinstance(cand.get("events"), list):
                    obj = cand
                    break

    if obj is None:
        return None, "No valid JSON block with top-level ``events`` array found."

    events_raw = obj.get("events")
    if not isinstance(events_raw, list):
        return None, "`events` must be an array."

    events: list[dict[str, Any]] = []
    for i, item in enumerate(events_raw):
        if not isinstance(item, dict):
            return None, f"events[{i}] must be an object."

        summary = item.get("summary")
        if summary is None or str(summary).strip() == "":
            return None, f"events[{i}].summary is required"

        if not isinstance(item.get("start"), dict) or not isinstance(item.get("end"), dict):
            return (
                None,
                f"events[{i}] must include start and end objects per Calendar API rules.",
            )
        start_d = cast(dict[str, Any], item["start"])
        end_d = cast(dict[str, Any], item["end"])
        st_dt, st_date = ("dateTime" in start_d), ("date" in start_d)
        en_dt, en_date = ("dateTime" in end_d), ("date" in end_d)
        if st_dt and st_date:
            return None, f"events[{i}].start must not mix dateTime and date keys"
        if en_dt and en_date:
            return None, f"events[{i}].end must not mix dateTime and date keys"
        if not ((st_dt and not st_date) or (st_date and not st_dt)):
            return None, f"events[{i}].start needs dateTime or date"
        if not ((en_dt and not en_date) or (en_date and not en_dt)):
            return None, f"events[{i}].end needs dateTime or date"
        if st_dt != en_dt or st_date != en_date:
            return None, f"events[{i}]: start/end must agree (both timed or both all-day)"

        events.append(cast(dict[str, Any], item))

    send_updates = obj.get("send_updates", "none")
    if not isinstance(send_updates, str):
        return None, "`send_updates` must be a string."
    if send_updates not in ("none", "all", "externalOnly"):
        return None, "`send_updates` must be one of: none, all, externalOnly."

    return events, send_updates
