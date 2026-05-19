"""Tests for Google Calendar OAuth helper (samples)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from google_calendar_client import (
    SilentCredentialResult,
    acquire_access_token_silently,
    default_calendar_oauth_client_secrets_path,
    evaluate_calendar_readiness,
    google_calendar_setup_instructions,
    oauth_redirect_uri,
    secrets_type_ok,
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
            return_value=SilentCredentialResult(FakeCreds(), None),
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
        return_value=SilentCredentialResult(FakeCreds(), None),
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
            return_value=SilentCredentialResult(FakeCreds(), None),
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


def test_oauth_redirect_uri_normalizes_bind_all() -> None:
    assert oauth_redirect_uri("0.0.0.0", 8765).endswith(":8765/api/calendar/oauth/callback")
    assert oauth_redirect_uri("127.0.0.1", 8765) == (
        "http://127.0.0.1:8765/api/calendar/oauth/callback"
    )


def test_secrets_type_ok_requires_installed_block(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"web":{}}', encoding="utf-8")
    assert secrets_type_ok(bad) is False

    good = tmp_path / "good.json"
    good.write_text(
        '{"installed":{"client_id":"abc","client_secret":"xyz"}}',
        encoding="utf-8",
    )
    assert secrets_type_ok(good) is True


def test_acquire_refresh_failed_returns_detail(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text(
        '{"installed":{"client_id":"abc","client_secret":"xyz"}}',
        encoding="utf-8",
    )
    token = tmp_path / "oauth-token.json"
    token.write_text("{}", encoding="utf-8")

    class FakeCreds:
        valid = False
        expired = True
        refresh_token = "rtok"

        def refresh(self, _request: object) -> None:
            from google.auth.exceptions import RefreshError

            raise RefreshError("invalid_grant: Token has been revoked.")

    with patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        return_value=FakeCreds(),
    ):
        result = acquire_access_token_silently(
            client_secrets_path=secrets,
            token_path=token,
        )

    assert isinstance(result, SilentCredentialResult)
    assert result.error == "refresh_failed"
    assert result.error_detail is not None
    assert "invalid_grant" in result.error_detail


def test_allow_oauth_insecure_transport_for_loopback() -> None:
    import os

    from google_calendar_client import allow_oauth_insecure_transport_for_loopback

    prev = os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
    try:
        allow_oauth_insecure_transport_for_loopback(url=oauth_redirect_uri("127.0.0.1", 8765))
        assert os.environ.get("OAUTHLIB_INSECURE_TRANSPORT") == "1"
    finally:
        if prev is not None:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = prev
        else:
            os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)


def test_allow_oauth_insecure_transport_ignores_https() -> None:
    import os

    from google_calendar_client import allow_oauth_insecure_transport_for_loopback

    prev = os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
    try:
        allow_oauth_insecure_transport_for_loopback(
            url="https://127.0.0.1:8765/api/calendar/oauth/callback",
        )
        assert os.environ.get("OAUTHLIB_INSECURE_TRANSPORT") is None
    finally:
        if prev is not None:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = prev
        else:
            os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
