"""Tests for Google Calendar JSON extraction (sample helper)."""

from __future__ import annotations

from google_calendar_payload import extract_calendar_payload


def test_extract_fence_timed_success() -> None:
    txt = """Here you go:

```json
{
  "send_updates": "none",
  "events": [
    {
      "summary": "Coffee",
      "start": {"dateTime": "2026-05-10T14:00:00", "timeZone": "America/Los_Angeles"},
      "end": {"dateTime": "2026-05-10T14:30:00", "timeZone": "America/Los_Angeles"}
    }
  ]
}
```
"""
    out = extract_calendar_payload(txt)
    assert out[0] is not None
    events, su = out
    assert su == "none"
    assert len(events) == 1
    assert events[0]["summary"] == "Coffee"


def test_extract_invalid_mixed_timing() -> None:
    txt = """```json
{"send_updates":"none","events":[{"summary":"BAD","start":{"date":"2026-05-10"},"end":{"dateTime":"2026-05-11T09:00:00","timeZone":"UTC"}}]}
```"""
    out = extract_calendar_payload(txt)
    assert out[0] is None


def test_empty_events_allowed() -> None:
    txt = 'OK.\n\n```json\n{"send_updates":"all","events":[]}\n```\n'
    ev, su = extract_calendar_payload(txt)
    assert ev is not None and ev == [] and su == "all"


def test_no_json_returns_error() -> None:
    got = extract_calendar_payload("Just prose, no fences.")
    assert got[0] is None


def test_bad_send_updates() -> None:
    txt = '```json\n{"send_updates": "oops", "events": []}\n```'
    assert extract_calendar_payload(txt)[0] is None
