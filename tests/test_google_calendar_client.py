"""Tests for Google Calendar OAuth helper (samples)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from google_calendar_client import (
    default_calendar_oauth_client_secrets_path,
    evaluate_calendar_readiness,
    google_calendar_setup_instructions,
)


def test_default_oauth_secret_path_under_repo_tmp(tmp_path: Path) -> None:
    p = default_calendar_oauth_client_secrets_path(tmp_path)
    assert p.parent.name == "credentials"
    assert p.name == "google-calendar-oauth-client.json"


def test_setup_instructions_non_empty() -> None:
    lines = google_calendar_setup_instructions()
    assert any("Google Cloud Console" in ln for ln in lines)


def test_readiness_missing_secrets(tmp_path: Path) -> None:
    secrets = tmp_path / "missing.json"
    token = tmp_path / "oauth-token.json"
    r = evaluate_calendar_readiness(
        client_secrets_path=secrets,
        token_path=token,
        calendar_id="primary",
        probe_api=False,
    )
    assert not r.ready
    assert r.access_state == "no_secrets"


def test_readiness_missing_token(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed":{}}', encoding="utf-8")
    token = tmp_path / "oauth-token.json"
    r = evaluate_calendar_readiness(
        client_secrets_path=secrets,
        token_path=token,
        calendar_id="primary",
        probe_api=False,
    )
    assert not r.ready
    assert r.access_state == "no_token"


def test_readiness_api_success_patched(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed":{}}', encoding="utf-8")
    token = tmp_path / "oauth-token.json"
    token.write_text("{}", encoding="utf-8")

    class FakeCreds:
        token = "fake-access-token"

    fake_payload = {"kind": "calendar#events", "items": []}
    with (
        patch(
            "google_calendar_client.acquire_access_token_silently",
            return_value=(FakeCreds(), None),
        ),
        patch(
            "google_calendar_client.calendar_events_list_probe",
            return_value=(200, fake_payload),
        ),
    ):
        r = evaluate_calendar_readiness(
            client_secrets_path=secrets,
            token_path=token,
            calendar_id="primary",
            probe_api=True,
        )

    assert r.ready
    assert r.api_probe_ok is True
    assert r.access_state == "ok"


def test_readiness_probe_skipped(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed":{}}', encoding="utf-8")
    token = tmp_path / "oauth-token.json"
    token.write_text("{}", encoding="utf-8")

    class FakeCreds:
        token = "x"

    with patch(
        "google_calendar_client.acquire_access_token_silently",
        return_value=(FakeCreds(), None),
    ):
        r = evaluate_calendar_readiness(
            client_secrets_path=secrets,
            token_path=token,
            calendar_id="primary",
            probe_api=False,
        )

    assert r.ready
    assert r.api_probe_ok is None


def test_readiness_api_network_error(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed":{}}', encoding="utf-8")
    token = tmp_path / "oauth-token.json"
    token.write_text("{}", encoding="utf-8")

    class FakeCreds:
        token = "fake-access-token"

    with (
        patch(
            "google_calendar_client.acquire_access_token_silently",
            return_value=(FakeCreds(), None),
        ),
        patch(
            "google_calendar_client.calendar_events_list_probe",
            side_effect=OSError("[Errno 50] offline"),
        ),
    ):
        r = evaluate_calendar_readiness(
            client_secrets_path=secrets,
            token_path=token,
            calendar_id="primary",
            probe_api=True,
        )

    assert not r.ready
    assert r.api_probe_ok is False
    assert any("could not reach" in ln.lower() for ln in r.human_lines)
