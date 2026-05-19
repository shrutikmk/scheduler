"""Google Calendar OAuth + minimal REST inserts (used by google_calendar_cli)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import httpx

LOG = logging.getLogger(__name__)
LOG.addHandler(logging.NullHandler())

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# Canonical Desktop OAuth client JSON beside the scheduler checkout (ignored by git).
CALENDAR_OAUTH_CLIENT_SECRETS_DIR = Path("credentials")
CALENDAR_OAUTH_CLIENT_SECRETS_FILENAME = Path("google-calendar-oauth-client.json")


def default_calendar_oauth_client_secrets_path(repo_root: Path | str) -> Path:
    """Absolute path to the Desktop OAuth JSON when saved at ``<repo>/credentials/...``."""
    root = Path(repo_root).expanduser().resolve()
    return root / CALENDAR_OAUTH_CLIENT_SECRETS_DIR / CALENDAR_OAUTH_CLIENT_SECRETS_FILENAME


_CALENDAR_SECRET_RELPATH = (
    f"{CALENDAR_OAUTH_CLIENT_SECRETS_DIR.as_posix()}/"
    f"{CALENDAR_OAUTH_CLIENT_SECRETS_FILENAME.as_posix()}"
)


def google_calendar_setup_instructions(
    *,
    client_secrets_path: Path | None = None,
    token_path: Path | None = None,
    calendar_id_env: str = "GOOGLE_CALENDAR_ID",
) -> tuple[str, ...]:
    """High-level setup steps printed when Calendar is not ready for API calls."""
    secret_hint = ""
    if client_secrets_path is not None:
        secret_hint = f"\n    • Expected file: **{client_secrets_path}**"
        secret_hint += (
            "\n    • Or pass **--client-secrets** / set **GOOGLE_CALENDAR_CLIENT_SECRETS**."
        )
    token_hint = ""
    if token_path is not None:
        token_hint = f"\n    • OAuth tokens cache: **{token_path}** (created after browser login)."
    return (
        "Google Calendar API (OAuth desktop app)",
        "",
        "1. In Google Cloud Console, enable **Google Calendar API** for your project.",
        "2. Credentials → Create **OAuth client ID** → Application type **Desktop app**.",
        "3. Download the JSON "
        "(type `installed`; fields `installed.client_id`, `installed.client_secret`).",
        (
            "4. Save it under your scheduler checkout at "
            + f"`**{_CALENDAR_SECRET_RELPATH}**`, or pass `--client-secrets PATH` "
            "(or `GOOGLE_CALENDAR_CLIENT_SECRETS`)."
            + secret_hint
        ),
        (
            "5. Run **`/auth`** in this CLI (or `/push`) once — a browser opens to grant "
            "Calendar access; tokens are saved for later runs." + token_hint
        ),
        f"6. Optional: set **{calendar_id_env}** or `--calendar-id` if not using `primary`.",
        "",
        "Docs: https://developers.google.com/calendar/api/guides/auth",
    )


@dataclass(frozen=True)
class CalendarReadiness:
    """Result of a local + optional API connectivity check (no interactive OAuth)."""

    ready: bool
    secrets_ok: bool
    token_file_ok: bool
    access_state: Literal["ok", "no_secrets", "no_token", "need_browser", "refresh_failed"]
    api_probe_ok: bool | None  # False = probed but failed; None = skipped
    human_lines: tuple[str, ...]


def oauth_redirect_uri(host: str, port: int) -> str:
    """Loopback redirect URI for browser OAuth on the day-scheduler web server."""
    host_norm = (host or "127.0.0.1").strip()
    if host_norm in {"0.0.0.0", "::"}:
        host_norm = "127.0.0.1"
    return f"http://{host_norm}:{int(port)}/api/calendar/oauth/callback"


def allow_oauth_insecure_transport_for_loopback(*, url: str | None = None) -> None:
    """Allow ``http://127.0.0.1`` OAuth redirects (oauthlib requires HTTPS otherwise).

    Google Desktop OAuth uses loopback HTTP; oauthlib blocks that unless
    ``OAUTHLIB_INSECURE_TRANSPORT=1`` is set for local development hosts only.
    """
    if os.environ.get("OAUTHLIB_INSECURE_TRANSPORT", "").lower() in {"1", "true", "yes"}:
        return
    if url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
            LOG.info("Enabled OAUTHLIB_INSECURE_TRANSPORT for loopback OAuth (%s)", host)
        return
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def secrets_type_ok(client_secrets_path: Path) -> bool:
    """True when the OAuth JSON looks like a Desktop (``installed``) client."""
    if not client_secrets_path.is_file():
        return False
    try:
        data = json.loads(client_secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    installed = data.get("installed")
    if not isinstance(installed, dict):
        return False
    return bool(installed.get("client_id")) and bool(installed.get("client_secret"))


def create_installed_app_flow(
    *,
    client_secrets_path: Path,
    redirect_uri: str,
) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not client_secrets_path.is_file():
        raise FileNotFoundError(f"Missing OAuth client secrets file: {client_secrets_path}")
    allow_oauth_insecure_transport_for_loopback(url=redirect_uri)
    scopes = [CALENDAR_EVENTS_SCOPE]
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), scopes)
    flow.redirect_uri = redirect_uri
    return flow


def oauth_authorization_url(flow: Any) -> tuple[str, str]:
    """Return ``(authorization_url, csrf_state)`` for browser sign-in."""
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return str(auth_url), str(state)


def oauth_exchange_code(flow: Any, authorization_response_url: str) -> Any:
    """Exchange the redirect URL for OAuth credentials."""
    allow_oauth_insecure_transport_for_loopback(url=authorization_response_url)
    flow.fetch_token(authorization_response=authorization_response_url)
    return flow.credentials


def persist_credentials(token_path: Path, creds: Any) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


@dataclass(frozen=True)
class SilentCredentialResult:
    creds: Any | None
    error: Literal["no_secrets", "no_token", "need_browser", "refresh_failed"] | None
    error_detail: str | None = None


def acquire_access_token_silently(
    *,
    client_secrets_path: Path,
    token_path: Path,
) -> SilentCredentialResult:
    """Load or refresh OAuth credentials **without** opening a browser."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    scopes = [CALENDAR_EVENTS_SCOPE]
    if not client_secrets_path.is_file():
        return SilentCredentialResult(None, "no_secrets")

    if not token_path.is_file():
        return SilentCredentialResult(None, "no_token")

    creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    try:
        if creds.valid:
            return SilentCredentialResult(creds, None)

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            persist_credentials(token_path, creds)
            if creds.valid:
                return SilentCredentialResult(creds, None)
        elif creds.expired:
            pass
    except RefreshError as exc:
        detail = str(exc).strip() or "refresh token rejected"
        LOG.warning("Google Calendar token refresh failed: %s", detail)
        return SilentCredentialResult(None, "refresh_failed", detail)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        LOG.exception("Google Calendar token refresh failed: %s", detail)
        return SilentCredentialResult(None, "refresh_failed", detail)

    return SilentCredentialResult(None, "need_browser")


def get_calendar_metadata(
    *,
    access_token: str,
    calendar_id: str,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any] | str]:
    """GET ``/calendars/{calendarId}`` — usually needs ``calendar`` / ``calendar.readonly`` scopes.

    This app OAuth uses only ``calendar.events``; prefer :func:`calendar_events_list_probe`
    for connectivity checks aligned with `/push`).
    """
    enc = quote(calendar_id, safe="")
    url = f"https://www.googleapis.com/calendar/v3/calendars/{enc}"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(url, headers=headers, timeout=timeout)
    try:
        data: dict[str, Any] | str = resp.json()
    except json.JSONDecodeError:
        data = resp.text
    return resp.status_code, data


def calendar_events_list_probe(
    *,
    access_token: str,
    calendar_id: str,
    max_results: int = 1,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any] | str]:
    """GET ``/calendars/{id}/events`` with ``maxResults`` — validates ``calendar.events`` scope.

    Reading **calendar metadata** (``calendars.get``) is a different OAuth scope than
    ``calendar.events``. Probing via ``events.list`` matches ``/push``.
    """
    enc = quote(calendar_id, safe="")
    mr = max(1, min(int(max_results), 250))
    url = (
        f"https://www.googleapis.com/calendar/v3/calendars/{enc}/events"
        f"?maxResults={mr}&singleEvents=true"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(url, headers=headers, timeout=timeout)
    try:
        data_events: dict[str, Any] | str = resp.json()
    except json.JSONDecodeError:
        data_events = resp.text
    return resp.status_code, data_events


def evaluate_calendar_readiness(
    *,
    client_secrets_path: Path,
    token_path: Path,
    calendar_id: str,
    probe_api: bool = True,
    http_timeout: float = 15.0,
) -> CalendarReadiness:
    """Assess whether `/push` can talk to Google Calendar without starting interactive OAuth."""
    human: list[str] = []
    api_ok: bool | None = None if not probe_api else False
    secrets_ok = client_secrets_path.is_file()
    token_ok = token_path.is_file()

    if not secrets_ok:
        human.append(f"OAuth client secrets file not found ({client_secrets_path}).")
        lines = tuple(human + ["", "How to fix:"] + list(google_calendar_setup_instructions()))
        return CalendarReadiness(
            ready=False,
            secrets_ok=False,
            token_file_ok=token_ok,
            access_state="no_secrets",
            api_probe_ok=None,
            human_lines=tuple(lines),
        )

    if not token_ok:
        human.append(
            f"No saved OAuth token yet ({token_path}). "
            + "Authorize once with **`/auth`** (or **`/push`**) — browser opens."
        )
        lines = tuple(
            human + ["", "How to finish setup:"] + list(google_calendar_setup_instructions())
        )
        return CalendarReadiness(
            ready=False,
            secrets_ok=True,
            token_file_ok=False,
            access_state="no_token",
            api_probe_ok=None,
            human_lines=tuple(lines),
        )

    creds_result = acquire_access_token_silently(
        client_secrets_path=client_secrets_path,
        token_path=token_path,
    )
    creds = creds_result.creds
    silent_err = creds_result.error

    if silent_err == "refresh_failed":
        human.append(
            "Saved OAuth token could not be refreshed "
            "(revoked consent, rotated secret, or network error)."
        )
        human.append(f"Try **`/auth`** again, then confirm token path: **{token_path}**.")
        lines = tuple(
            human
            + ["", "If it keeps failing, repeat Cloud setup:"]
            + list(google_calendar_setup_instructions(client_secrets_path=client_secrets_path))
        )
        return CalendarReadiness(
            ready=False,
            secrets_ok=True,
            token_file_ok=True,
            access_state="refresh_failed",
            api_probe_ok=None,
            human_lines=tuple(lines),
        )

    if silent_err == "need_browser" or creds is None:
        human.append(
            "OAuth token missing or inactive; **`/auth`** is required "
            "(or delete the token file if it is corrupted and sign in again)."
        )
        lines = tuple(human + list(google_calendar_setup_instructions(token_path=token_path)))
        return CalendarReadiness(
            ready=False,
            secrets_ok=True,
            token_file_ok=True,
            access_state="need_browser",
            api_probe_ok=None,
            human_lines=tuple(lines),
        )

    if not probe_api:
        human.append(
            "OAuth looks valid (API probe skipped). "
            "Use **`/calendar`** to verify Google responds before `/push`."
        )
        return CalendarReadiness(
            ready=True,
            secrets_ok=True,
            token_file_ok=True,
            access_state="ok",
            api_probe_ok=None,
            human_lines=tuple(human),
        )

    tok = getattr(creds, "token", None)
    if not isinstance(tok, str) or not tok:
        human.append("OAuth credentials did not yield an access token; try `/auth` again.")
        setup = list(google_calendar_setup_instructions(token_path=token_path))
        return CalendarReadiness(
            ready=False,
            secrets_ok=True,
            token_file_ok=True,
            access_state="need_browser",
            api_probe_ok=False,
            human_lines=tuple(human + setup),
        )
    try:
        status, payload = calendar_events_list_probe(
            access_token=tok,
            calendar_id=calendar_id,
            timeout=http_timeout,
        )
    except Exception as exc:
        human.append(
            f"Could not reach Google Calendar API for probe ({exc}). "
            "Check network/firewall/VPN and try `/calendar` again."
        )
        return CalendarReadiness(
            ready=False,
            secrets_ok=True,
            token_file_ok=True,
            access_state="ok",
            api_probe_ok=False,
            human_lines=tuple(human),
        )
    if status == 200 and isinstance(payload, dict) and payload.get("kind") == "calendar#events":
        label = calendar_id if calendar_id != "primary" else "primary"
        human.append(f"Calendar **events** API reachable for **{label}** (scope matches `/push`).")
        api_ok = True
        human.append("`POST /events` (`/push`) should succeed if payloads are valid.")

        return CalendarReadiness(
            ready=True,
            secrets_ok=True,
            token_file_ok=True,
            access_state="ok",
            api_probe_ok=api_ok,
            human_lines=tuple(human),
        )

    hint = ""
    if isinstance(payload, dict):
        err_obj = payload.get("error")
        if isinstance(err_obj, dict):
            msg = err_obj.get("message")
            reasons = err_obj.get("errors")
            if msg:
                hint = f"\n  API message: {msg}"
                if isinstance(reasons, list) and reasons:
                    hint += f"\n  details: {reasons}"
        elif err_obj is not None:
            hint = f"\n  API error field: {err_obj!r}"

    human.append(f"OAuth works, but the **events.list** probe failed (**HTTP {status}**)" + hint)
    if status in (403, 404):
        human.append(
            "Common causes: **Google Calendar API** not enabled on the OAuth project; "
            "OAuth consent missing the **calendar.events** scope; wrong **calendar id** "
            "(try `primary` or `GOOGLE_CALENDAR_ID`)."
        )

    api_ok = False
    lines = tuple(
        human
        + list(
            google_calendar_setup_instructions(
                client_secrets_path=client_secrets_path,
                token_path=token_path,
            )
        ),
    )

    return CalendarReadiness(
        ready=False,
        secrets_ok=True,
        token_file_ok=True,
        access_state="ok",
        api_probe_ok=api_ok,
        human_lines=tuple(lines),
    )


def format_readiness_for_terminal(readiness: CalendarReadiness, *, banner: str) -> str:
    """Join readiness lines into a stderr-friendly block."""
    status = "ready for API push" if readiness.ready else "not fully connected"
    first = (
        "\n────────────────────────────────────────────────────────────\n"
        f"{banner}\n"
        f"Google Calendar connectivity: {status}"
        "\n────────────────────────────────────────────────────────────\n"
    )
    return first + ("\n".join(readiness.human_lines)) + "\n"


def load_or_refresh_credentials(
    *,
    client_secrets_path: Path,
    token_path: Path,
) -> Any:
    """CLI-only blocking OAuth via ephemeral localhost server."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    allow_oauth_insecure_transport_for_loopback()
    if not client_secrets_path.is_file():
        raise FileNotFoundError(
            "Missing OAuth client secrets file.\n"
            f"Expected: {client_secrets_path}\n"
            "Canonical path in this project: scheduler repo root + "
            f"`{_CALENDAR_SECRET_RELPATH}` (Desktop app JSON).\n"
            "Create OAuth credentials of type Desktop app in Google Cloud Console, "
            "download JSON, then save or pass `--client-secrets PATH`.",
        )

    scopes = [CALENDAR_EVENTS_SCOPE]
    creds: Credentials | None = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds is None or not creds.valid:
        if creds is not None and getattr(creds, "expired", False) and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), scopes)
            creds = flow.run_local_server(port=0)
        persist_credentials(token_path, creds)

    return creds


def insert_calendar_event(
    *,
    access_token: str,
    calendar_id: str,
    body: dict[str, Any],
    send_updates: str = "none",
) -> tuple[int, dict[str, Any] | str]:
    """POST ``/calendars/{id}/events``. Returns ``(status_code, json_or_text)``."""
    enc = quote(calendar_id, safe="")
    url = (
        f"https://www.googleapis.com/calendar/v3/calendars/{enc}/events"
        f"?sendUpdates={quote(send_updates, safe='')}"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(url, headers=headers, json=body, timeout=60.0)
    try:
        data: dict[str, Any] | str = resp.json()
    except json.JSONDecodeError:
        data = resp.text
    return resp.status_code, data


def format_api_error(code: int, payload: dict[str, Any] | str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error") or payload
        return f"{code}: {json.dumps(err, indent=2)[:800]}"
    return f"{code}: {str(payload)[:800]}"
