#!/usr/bin/env python3
"""MLX chat CLI that drafts **Google Calendar** events via a local LLM, then pushes with OAuth.

Model loading follows ``samples/mlx_chat_cli.py``. **Defaults for this CLI** prefer **Qwen3-14B**
(``~/models/Qwen3-14B``, else Hugging Face ``Qwen/Qwen3-14B``) unless you pass ``--model`` or use
the env vars below. The model must emit a fenced ``json`` block with ``events[]``.

OAuth (one-time browser flow):

1. Enable **Google Calendar API** for your Cloud project.
2. Create OAuth **Desktop app** credentials; download JSON.
3. Save as ``credentials/google-calendar-oauth-client.json`` under the checkout root
   (that path is gitignored). Or pass ``--client-secrets`` / set ``GOOGLE_CALENDAR_CLIENT_SECRETS``.
4. Run this CLI once; tokens are cached at ``--token-cache`` (default ``~/.config/scheduler/`` …).

Examples::

    uv sync --group samples-mlx --group samples-google-calendar
    uv run --group samples-mlx --group samples-google-calendar \\
        python samples/mlx_google_calendar_cli.py \\
        --client-secrets /path/to/desktop-oauth-secret.json

    # Terminal 1: optionally use MLX LLM elsewhere; here we load Metal in-process::

    uv run --group samples-mlx --group samples-google-calendar \\
        python samples/mlx_google_calendar_cli.py

Commands in chat::

    /push     Insert events from the **last assistant** reply (parses fenced JSON).
    /paste    Multi-line paste mode; end with a line containing only /end
    /calendar Probe OAuth via **events.list** (same scope as `/push`; not calendars metadata GET).
    /auth     Run browser OAuth flow (stores token for `/push`; needed first time).
    /clear    Clear transcript.
    /quit     Exit.

Environment:

    GOOGLE_CALENDAR_ID   Calendar id (default ``primary``).
    MLX_MODEL             If set, overrides the Calendar CLI default weights (unless ``--model``).
    MLX_CALENDAR_MODEL   Calendar-only MLX model dir or HF id when ``MLX_MODEL`` is unset.

    MLX_CALENDAR_NO_AIRPORT_TZ=1   Disable IATA timezone injection from the CSV.
    MLX_CALENDAR_NO_PROBE=1   Skip HTTPS Calendar probe at startup; ``/calendar`` still probes.

Flags ``--no-airport-tz-hints`` disables the same (flight-related ``timeZone`` context).

Startup ``--no-calendar-probe`` skips only the automated pre-model HTTPS probe (offline-friendly).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SAMPLES_ROOT = Path(__file__).resolve().parent
APP_ROOT = SAMPLES_ROOT.parent / "app"
for _p in (SAMPLES_ROOT, APP_ROOT):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# noqa: E402 — preserve chat CLI import ordering like mlx_chat_cli.py
from mlx_chat_cli import (  # noqa: E402
    _encoded_length,
    resolve_context_token_limit,
    resolve_model_arg,
)
from mlx_chat_cli import diagnose_local_snapshot as _diagnose_local  # noqa: E402
from mlx_chat_cli import is_local_dir as _is_local_dir  # noqa: E402
from mlx_chat_cli import run_diagnose as _run_diagnose  # noqa: E402
from mlx_day_scheduler_pipeline import REPO_ROOT, compress_history_for_budget, trim_history_pairs  # noqa: E402
from mlx_day_scheduler_pipeline import (  # noqa: E402
    _minutes_to_ampm,
)
from mlx_day_scheduler_pipeline import build_prompt as _build_prompt  # noqa: E402
from mlx_day_scheduler_pipeline import strip_reasoning_blocks as _strip_reasoning_blocks  # noqa: E402
from airport_timezones import AirportTzTurn, airport_tz_turn  # noqa: E402
from google_calendar_client import default_calendar_oauth_client_secrets_path  # noqa: E402
from google_calendar_payload import extract_calendar_payload  # noqa: E402

Role = Literal["user", "assistant"]

_CAL_DEFAULT_HUB = "Qwen/Qwen3-14B"
_LOCAL_QWEN14 = Path("models") / "Qwen3-14B"


def resolve_calendar_cli_model(cli_model: str | None) -> str:
    """Resolve MLX weights for Calendar chat (not generic ``mlx_chat_cli`` defaults).

    Precedence: ``--model``, ``MLX_MODEL``, ``MLX_CALENDAR_MODEL``, local ``~/models/Qwen3-14B`` if
    present, else Hugging Face ``Qwen/Qwen3-14B``.
    """
    if cli_model and cli_model.strip():
        return resolve_model_arg(cli_model)
    if (os.environ.get("MLX_MODEL") or "").strip():
        return resolve_model_arg(None)
    cal = (os.environ.get("MLX_CALENDAR_MODEL") or "").strip()
    if cal:
        return resolve_model_arg(cal)
    local14 = (Path.home() / _LOCAL_QWEN14).expanduser().resolve()
    if local14.is_dir():
        return str(local14)
    return _CAL_DEFAULT_HUB


def _calendar_tz_label(now: datetime) -> str:
    tz = now.tzinfo
    if tz is None:
        return "unknown"
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    nm = tz.tzname(now)
    if nm:
        return nm
    off = tz.utcoffset(now)
    return f"fixed offset {off}" if off is not None else str(tz)


def _calendar_clock_core_lines(now: datetime | None = None) -> tuple[str, ...]:
    """Shared facts injected into **system** (each inference) + **user** (message appendix)."""
    cur = datetime.now().astimezone() if now is None else now
    weekday = cur.strftime("%A")
    date_iso = cur.date().isoformat()
    mins = cur.hour * 60 + cur.minute
    ampm = _minutes_to_ampm(mins)
    local_iso = cur.isoformat(timespec="seconds")
    utc_z = cur.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tz_lab = _calendar_tz_label(cur)
    return (
        f"Interpret relative dates/times vs **{ampm}** on **{weekday}, {date_iso}**.",
        f"Local wall time ISO-8601: `{local_iso}`.",
        f"Equivalent UTC (Zulu): `{utc_z}`.",
        f"IANA / zone label when known: **`{tz_lab}`** (device-local clock).",
    )


def calendar_clock_system_suffix() -> str:
    """Refreshed on **every** model call so summarized history never loses authoritative now."""
    body = "\n".join(_calendar_clock_core_lines())
    return (
        "\n---\n[Clock — this request]\n"
        "Authoritative snapshot for interpreting **today / tomorrow / this week** "
        "**on this inference** (matches the appendix on the user's message).\n"
        f"{body}\n"
    )


def _clock_block_for_calendar() -> str:
    lines = "\n".join(_calendar_clock_core_lines())
    return f"\n\n---\n[Clock — local machine]\n{lines}\n"


def _paste_block_closer(line: str) -> bool:
    return line.strip() in {"/end", "/paste-end"}


def read_calendar_paste_block() -> str | None:
    """Read lines until `/end` or `/paste-end` alone. Returns ``None`` if cancelled."""
    print(
        "(paste mode — paste lines, then type /end alone on its own line; Ctrl+C cancels)",
        file=sys.stderr,
        flush=True,
    )
    lines: list[str] = []
    try:
        while True:
            line = input()
            if _paste_block_closer(line):
                break
            lines.append(line)
    except KeyboardInterrupt:
        print("\n(paste cancelled)", flush=True)
        return None
    body = "\n".join(lines).strip()
    return body if body else None


def _pasted_multi_line_hint(user_text: str) -> str:
    """Bias the model when the input looks like a forwarded itinerary."""
    if user_text.count("\n") < 3:
        return ""
    lowered = user_text.lower()
    keys = (
        "departs",
        "arrives",
        "airlines",
        "confirmation",
        "itinerary",
        "economy",
        "terminal",
        "layover",
        "southwest",
        "flight",
        "duration",
        "expedia",
    )
    if sum(1 for k in keys if k in lowered) < 2 and "(" not in user_text:
        return ""
    return (
        "\n\n---\n[Host — pasted / multi-line block]\n"
        "- Assume implicit **calendar-write** intent unless the text is unrelated to schedules.\n"
        "- Mine the block for airlines, confirmations, gates, totals, printed dates, and legs.\n"
        "- IATA fragments like `(AUS-…)`, `(SAN-…)`, `(SFO-…)` are **authoritative** — "
        "never claim airports are missing when they appear.\n"
        "- When printed dates (e.g. **May 14, 2026**) disagree with relative words like "
        "“Thursday”, **trust the explicit printed date.**\n"
    )


def _host_calendar_sources_block(
    *,
    tz_hints_requested: bool,
    airport_csv_exists: bool,
    matched_airport_codes: tuple[str, ...],
) -> str:
    lines = ["\n\n---\n[Host — available sources this turn]\n"]
    db_line = ""
    if not tz_hints_requested or not airport_csv_exists:
        db_line = (
            "- Airport IATA→IANA (`prompts/airport-timezones.csv`): **not in use** "
            "(hints off or CSV missing). Do **not** assume airport rows for this message.\n"
        )
    elif matched_airport_codes:
        codes = ", ".join(matched_airport_codes)
        db_line = (
            "- Airport DB: **matched IATA**: "
            + f"**{codes}**. Detailed IANA zones follow in "
            + "`[Airport timezones — from prompts/airport-timezones.csv]` "
            + "when that block is appended.\n"
        )
    else:
        db_line = (
            "- Airport DB: **loaded** but **no catalog IATA** appeared in the user message. "
            + "If intent is aviation-related without codes: ask for **IATA** "
            + "or explicit **IANA** `timeZone`.\n"
        )
    lines.append(db_line)
    lines.append(
        "- Anchoring workflow: use `[Clock — local machine]` (earlier in this user message), "
        "then use airport-appendix **IANA** zones for each flight segment when rows are present.\n"
    )
    return "".join(lines)


def load_calendar_system_text() -> str:
    stem = Path(__file__).stem
    system_path = REPO_ROOT / "prompts" / "google-calendar-llm-system.md"
    patterns_path = REPO_ROOT / "prompts" / "google-calendar-api-patterns.md"
    chunks: list[str] = []

    def _must_read(p: Path) -> str:
        if not p.is_file():
            raise FileNotFoundError(f"{stem}: missing required prompt file:\n  {p}")
        return p.read_text(encoding="utf-8")

    chunks.append(_must_read(system_path))
    chunks.append("\n---\n## Canonical API patterns (verbatim)\n\n")
    chunks.append(_must_read(patterns_path))
    return "".join(chunks)


def push_events_to_calendar(
    *,
    access_token: str,
    calendar_id: str,
    events: list[dict[str, Any]],
    send_updates: str,
) -> tuple[bool, list[str]]:
    from google_calendar_client import format_api_error, insert_calendar_event

    errs: list[str] = []
    for i, ev in enumerate(events):
        code, data = insert_calendar_event(
            access_token=access_token,
            calendar_id=calendar_id,
            body=ev,
            send_updates=send_updates,
        )
        if isinstance(data, dict) and "kind" in data and code == 200:
            eid = str(data.get("id", "?"))
            print(f"  created [{i + 1}] id={eid}", file=sys.stderr)
        elif 200 <= code < 300:
            print(f"  created [{i + 1}] (status {code})", file=sys.stderr)
        else:
            errs.append(f"event {i + 1} ({ev.get('summary')!r}): {format_api_error(code, data)}")
    return not errs, errs


def main(argv: list[str] | None = None) -> int:
    _default_secrets = default_calendar_oauth_client_secrets_path(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description="MLX-backed chat that drafts Calendar events (JSON), then pushes with /push.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "mlx-lm model dir or HF id. Precedence: this flag, MLX_MODEL, MLX_CALENDAR_MODEL, "
            f"then ~/models/Qwen3-14B if present, else Hub {_CAL_DEFAULT_HUB}."
        ),
    )
    parser.add_argument("--max-tokens", type=int, default=2048, help="Completion cap.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    parser.add_argument("--top-p", dest="top_p", type=float, default=1.0, help="Nucleus sampling.")
    parser.add_argument(
        "--top-k", dest="top_k", type=int, default=20, help="Top-k sampling (0 disables)."
    )
    parser.add_argument("--min-p", dest="min_p", type=float, default=0.0, help="Min-p sampling.")
    parser.add_argument("--prefill-step-size", type=int, default=4096, help="Prefill chunk size.")
    parser.add_argument("--kv-bits", type=int, default=8, help="KV cache bits (0 = off).")
    parser.add_argument(
        "--calendar-id",
        default=None,
        help="Target calendar id (default env GOOGLE_CALENDAR_ID or ``primary``).",
    )
    parser.add_argument(
        "--client-secrets",
        type=Path,
        default=None,
        help=f"OAuth Desktop client secrets JSON (default: {_default_secrets}).",
    )
    parser.add_argument(
        "--token-cache",
        type=Path,
        default=None,
        help=(
            f"Where to cache OAuth token (directory). Default ~/.config/{REPO_ROOT.name}/calendar/"
        ),
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="MLX environment check (no OAuth, no chat).",
    )
    parser.add_argument(
        "--no-airport-tz-hints",
        action="store_true",
        help="Do not inject IATA→IANA hints from prompts/airport-timezones.csv.",
    )
    parser.add_argument(
        "--no-calendar-probe",
        action="store_true",
        help=("Skip HTTPS GET probe at startup (/calendar inside chat still probes by default)."),
    )
    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])

    model_str = resolve_calendar_cli_model(ns.model)
    if ns.diagnose:
        return _run_diagnose(model_str)

    default_secrets = _default_secrets
    client_path = Path(
        ns.client_secrets
        or os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRETS", "").strip()
        or default_secrets,
    ).expanduser()

    conf_root = Path(
        ns.token_cache.expanduser()
        if ns.token_cache
        else (Path.home() / ".config" / REPO_ROOT.name / "calendar")
    ).resolve()
    token_path = conf_root / "oauth-token.json"

    cal_default = (
        (ns.calendar_id or "").strip()
        or os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
        or "primary"
    )

    env_no_probe = os.environ.get("MLX_CALENDAR_NO_PROBE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    probe_at_startup = not ns.no_calendar_probe and not env_no_probe

    def _calendar_readiness(*, probe_api: bool, banner: str) -> None:
        try:
            from google_calendar_client import (
                evaluate_calendar_readiness,
                format_readiness_for_terminal,
            )
        except ImportError:
            print(
                "Calendar connectivity check unavailable: missing Google OAuth libs.\n"
                "Install: uv sync --group samples-google-calendar",
                file=sys.stderr,
                flush=True,
            )
            return
        r = evaluate_calendar_readiness(
            client_secrets_path=client_path,
            token_path=token_path,
            calendar_id=cal_default,
            probe_api=probe_api,
        )
        print(format_readiness_for_terminal(r, banner=banner), file=sys.stderr, flush=True)

    _calendar_readiness(probe_api=probe_at_startup, banner="Startup check")

    if _is_local_dir(model_str):
        p = Path(model_str).expanduser().resolve()
        if not p.is_dir():
            print(
                f"error: model directory does not exist:\n  {p}\n\nUse Hub:\n"
                f"  --model {_CAL_DEFAULT_HUB}\n",
                file=sys.stderr,
            )
            return 2

    try:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.generate import stream_generate
        from mlx_lm.sample_utils import make_sampler
    except ImportError as e:
        print(f"error: {e}\nInstall: uv sync --group samples-mlx", file=sys.stderr)
        return 2

    if not mx.metal.is_available():
        print(
            "error: MLX Metal unavailable. Run on Apple Silicon desktop session.",
            file=sys.stderr,
        )
        return 2

    if _is_local_dir(model_str):
        issues = _diagnose_local(Path(model_str).expanduser().resolve())
        hard_block = [
            x
            for x in issues
            if x.startswith("Missing") or "No tokenizer" in x or "No *.safetensors" in x
        ]
        if hard_block:
            for line in issues:
                print(f"  - {line}", file=sys.stderr)
            return 3

    try:
        system_base = load_calendar_system_text()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 5

    airport_csv = REPO_ROOT / "prompts" / "airport-timezones.csv"
    tz_hints_ok = not ns.no_airport_tz_hints and os.environ.get(
        "MLX_CALENDAR_NO_AIRPORT_TZ", ""
    ).strip().lower() not in ("1", "true", "yes", "on")
    if tz_hints_ok and not airport_csv.is_file():
        print(
            f"warning: airport timezone CSV missing (expected {airport_csv}); "
            "flight IATA hints disabled.\n",
            file=sys.stderr,
            flush=True,
        )

    print(f"Loading {model_str!r} (Metal) …", file=sys.stderr)
    try:
        model_m, tokenizer = load(
            model_str,
            tokenizer_config={"trust_remote_code": True},
        )
    except Exception as e:
        print(f"\nLoad failed: {e}\n", file=sys.stderr)
        return 4

    mx.clear_cache()
    sampler = make_sampler(
        temp=ns.temperature,
        top_p=ns.top_p,
        min_p=ns.min_p,
        top_k=ns.top_k,
    )
    gen_kw: dict[str, Any] = {
        "sampler": sampler,
        "prefill_step_size": ns.prefill_step_size,
    }
    if ns.kv_bits > 0:
        gen_kw["kv_bits"] = ns.kv_bits
        gen_kw["kv_group_size"] = 64
        gen_kw["quantized_kv_start"] = 0

    ctx_limit = resolve_context_token_limit(model_str=model_str, tokenizer=tokenizer, explicit=None)
    soft_frac = 0.72
    reserve_tokens = ns.max_tokens + 512

    history: list[tuple[Role, str]] = []
    print(
        "Google Calendar MLX chat. Commands: /paste /push /calendar /auth /clear /quit\n"
        f"model={model_str}\n"
        f"calendar_default={cal_default!r}; OAuth secrets={client_path}\n"
        f"(Startup Calendar probe ran with probe={'on' if probe_at_startup else 'off'}; "
        "/calendar always probes HTTPS when possible.)\n",
        file=sys.stderr,
        flush=True,
    )

    oauth_creds: Any | None = None

    while True:
        try:
            raw_user = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw_user:
            continue
        if raw_user in {"/quit", "/exit", "/q"}:
            break
        if raw_user == "/clear":
            history.clear()
            print("(cleared)")
            continue

        if raw_user == "/paste":
            blob = read_calendar_paste_block()
            if not blob:
                continue
            raw_user = blob

        if raw_user in {"/calendar", "/cal", "/calendar-status"}:
            _calendar_readiness(probe_api=True, banner="/calendar connectivity")
            continue

        if raw_user in {"/auth", "/oauth", "/login"}:
            print("Completing OAuth (browser may open for consent) …", flush=True)
            try:
                from google_calendar_client import load_or_refresh_credentials

                oauth_creds = load_or_refresh_credentials(
                    client_secrets_path=client_path,
                    token_path=token_path,
                )
            except FileNotFoundError as e:
                print(str(e))
                continue
            except Exception as e:
                print(f"OAuth failed: {e}")
                continue
            print(f"OAuth OK — token saved to {token_path}")
            _calendar_readiness(probe_api=True, banner="Post-OAuth probe")
            continue

        if raw_user == "/push":
            last_assistant = next((t for r, t in reversed(history) if r == "assistant"), None)
            if not last_assistant:
                print("Nothing to push: no assistant message yet.")
                continue
            parsed = extract_calendar_payload(last_assistant)
            if parsed[0] is None:
                _, err = parsed
                print(f"/push failed: {err}")
                continue
            events, send_updates = parsed
            if not events:
                print("Assistant JSON had empty ``events[]`` — nothing created.")
                continue
            print(
                f"Creating {len(events)} event(s) on calendar {cal_default!r} "
                f"(send_updates={send_updates}) …",
            )
            try:
                from google_calendar_client import load_or_refresh_credentials

                oauth_creds = load_or_refresh_credentials(
                    client_secrets_path=client_path,
                    token_path=token_path,
                )
            except FileNotFoundError as e:
                print(str(e))
                continue
            except Exception as e:
                print(f"OAuth failed: {e}")
                continue
            token_txt = oauth_creds.token
            ok, errs = push_events_to_calendar(
                access_token=token_txt,
                calendar_id=cal_default,
                events=events,
                send_updates=send_updates,
            )
            if ok:
                print("Done.")
            else:
                print("Partial or full failure:")
                for ln in errs:
                    print(ln)
            continue

        use_airports = tz_hints_ok and airport_csv.is_file()
        if use_airports:
            turn = airport_tz_turn(raw_user, csv_path=airport_csv)
        else:
            turn = AirportTzTurn((), "")
        user_for_prompt = (
            raw_user
            + _pasted_multi_line_hint(raw_user)
            + _clock_block_for_calendar()
            + _host_calendar_sources_block(
                tz_hints_requested=tz_hints_ok,
                airport_csv_exists=airport_csv.is_file(),
                matched_airport_codes=turn.matched_codes,
            )
            + (turn.appendix if use_airports else "")
        )

        system_this_turn = system_base + calendar_clock_system_suffix()

        ctx_verbose = False
        did_compress, p_tokens, budget = compress_history_for_budget(
            model_m=model_m,
            tokenizer=tokenizer,
            system=system_this_turn,
            history=history,
            pending_user=user_for_prompt,
            context_limit=ctx_limit,
            soft_fraction=soft_frac,
            reserve_tokens=reserve_tokens,
            keep_recent_messages=12,
            summarize_max_tokens=384,
            max_summarize_input_tokens=6144,
            gen_kw=gen_kw,
            auto_summarize=True,
            enable_thinking=False,
        )
        if did_compress and ctx_verbose:
            print(f"[context] ~{p_tokens} tok budget ~{budget}", file=sys.stderr)

        tok_before = (
            _encoded_length(tokenizer, system_this_turn + user_for_prompt) if tokenizer else None
        )
        if isinstance(tok_before, int) and tok_before > ctx_limit - reserve_tokens - 512:
            print(
                "[warn] Prompt may be oversized; shorten your message or shorten history.\n",
                file=sys.stderr,
            )

        prompt = _build_prompt(
            tokenizer,
            system_this_turn,
            history + [("user", user_for_prompt)],
            enable_thinking=False,
        )
        print("AI> ", end="", flush=True)
        buf_parts: list[str] = []
        last_resp = None
        gen_ok = False
        try:
            for resp in stream_generate(
                model_m,
                tokenizer,
                prompt,
                max_tokens=ns.max_tokens,
                **gen_kw,
            ):
                last_resp = resp
                buf_parts.append(resp.text)
                print(resp.text, end="", flush=True)
            gen_ok = True
        except KeyboardInterrupt:
            print("\n", flush=True)
            reply_partial = "".join(buf_parts).strip()
            reply_partial = _strip_reasoning_blocks(reply_partial)
            history.append(("user", raw_user))
            history.append(
                (
                    "assistant",
                    reply_partial
                    if reply_partial
                    else "(Generation interrupted — no streamed text retained.)",
                )
            )
            trim_history_pairs(history, 0)
            print("(interrupted)", file=sys.stderr, flush=True)
            continue
        except Exception as e:
            print(f"\nerror: {e}")
            continue

        reply = "".join(buf_parts).strip()
        reply = _strip_reasoning_blocks(reply)
        print()
        if gen_ok:
            peek = extract_calendar_payload(reply)
            if peek[0] is not None:
                evs = peek[0]
                print(f"(parsed {len(evs)} event(s); type `/push` to create on Google)")
            else:
                print("(no fenced JSON payload yet; clarify times or ask assistant to summarize)")

            if (
                last_resp is not None
                and getattr(last_resp, "generation_tokens", 0) >= ns.max_tokens - 1
            ):
                print(
                    f"[hint] Hit --max-tokens ({ns.max_tokens}); output may be truncated.\n",
                    file=sys.stderr,
                    flush=True,
                )

        history.append(("user", raw_user))
        history.append(("assistant", reply))
        trim_history_pairs(history, 0)


if __name__ == "__main__":
    sys.exit(main())
