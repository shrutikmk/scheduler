# scheduler

Local scheduling workspace: a **day-planner chat** (primary stack: **vLLM Metal** when configured), optional in-process **MLX**, a **Habit Builder** panel, and prompts for calendar-style planning.

## Requirements

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) for dependencies
- **Inference (pick one):**
  - **vLLM Metal** ([vllm-metal](https://github.com/vllm-project/vllm-metal)): one OpenAI-compatible server (**Qwen3-14B**) for scheduler chat, insights, repair, labels, and query parsing. Set `VLLM_14B_BASE_URL` (see below). Legacy `VLLM_8B_*` env vars are ignored.
  - **MLX** (Apple Silicon): single in-process **Qwen3-14B** from `~/models/Qwen3-14B` by default (override with `--model` or `SCHEDULER_MODEL` / `MLX_MODEL`). Use `app/mlx_llm_gateway.py` or `app/scheduler_llm_gateway.py --llm-backend mlx`.

## Install

```bash
cd scheduler
uv sync
uv sync --group samples-mlx
uv sync --group samples-vllm
```

Download the scheduler model (and tokenizer) locally:

```bash
mkdir -p ~/models
export HF_HOME="$HOME/models/.hf-cache"
hf download Qwen/Qwen3-14B --local-dir "$HOME/models/Qwen3-14B"
# Optional — MLX-only setups often use Qwen3-8B locally for faster financial labels:
# hf download Qwen/Qwen3-8B --local-dir "$HOME/models/Qwen3-8B"
```

## Run the browser shell (recommended)

Use **two or more terminals**: the LLM runs in a separate gateway process; the UI proxies chat to it.

**vLLM Metal — start inference first** ([vllm-metal](https://github.com/vllm-project/vllm-metal)). `export HF_HOME="$HOME/models/.hf-cache"` is optional but matches the download step above.

```bash
# Terminal 0 — Qwen3-14B (all gateway traffic)
vllm serve "$HOME/models/Qwen3-14B" --port 8000 \
  --served-model-name Qwen3-14B
```

The OpenAI `model` string must match `--served-model-name` (gateway default when unset: `Qwen3-14B`).

**Terminal 1 — LLM gateway**

- **vLLM Metal (recommended when URL is set):** after Terminal 0 is listening:

  ```bash
  export VLLM_14B_BASE_URL=http://127.0.0.1:8000/v1
  # Optional if your served name differs:
  # export VLLM_14B_MODEL=Qwen3-14B

  ./scripts/run-llm-gateway-local-models.sh
  ```

  **Check the server before the gateway:**  
  `uv run --group samples-vllm python app/scheduler_llm_gateway.py --diagnose-only`  
  Exit code **0** means vLLM answered with HTTP &lt; 500 on at least one probe URL (usually `GET …/v1/models`).

  Or: `uv run --group samples-vllm --group samples-mlx python app/scheduler_llm_gateway.py`
  (`./scripts/run-llm-gateway-local-models.sh` sets `SCHEDULER_LLM_FALLBACK_MLX=1` so a missing vLLM process falls back to MLX with a warning; use `SCHEDULER_LLM_FALLBACK_MLX=0` or `--no-mlx-fallback` for strict vLLM-only.)

- **MLX in-process (no vLLM env):**

  ```bash
  ./scripts/run-llm-gateway-local-models.sh
  ```

  Or explicitly: `uv run --group samples-mlx python app/mlx_llm_gateway.py`

Environment overrides (optional): `SCHEDULER_MODEL`, `MLX_MODEL`, `HF_HOME`, `SCHEDULER_LLM_BACKEND` (`auto` / `vllm` / `mlx`), `SCHEDULER_LLM_FALLBACK_MLX=1`.

The gateway validates timetable format, can run an optional self-grade pass, and optional **format repair** (`SCHEDULER_FORMAT_REPAIR`, default on).

### Latency knobs

The two settings with the largest effect on per-turn wall time on Qwen3-14B:

| Knob | Default | Effect |
|------|---------|--------|
| `MLX_DAY_SCHEDULER_NO_THINKING=1` (or `--no-day-scheduler-thinking`) | thinking **on** | Disables Qwen3's hidden think pass; replies are faster and far less variable (the variability is mostly thinking-token count). Schedules tend to be slightly less self-checked. |
| `MLX_DAY_SCHEDULER_SELF_GRADE=1` (or `--self-grade`) | grader **off** | Re-enables the second LLM grader call after a structurally-valid reply. Adds ~5–15 s/turn on Qwen3-14B because it re-prefills the full system + history + candidate. Off by default since structural validation + format-repair already cover the common breakage classes. |

The UI proxy caches the gateway `/health` probe for ~5 s, so repeated chats don't pay an extra round-trip per turn.

**Terminal 2 — static shell + habits (default `http://127.0.0.1:8765/`) + Finances (`/finances`):**

```bash
uv run python app/mlx_day_scheduler_ui.py
```

Open the **Scheduler** at `/` and **Finances** at `/finances` on the same server. The UI talks to the gateway at `http://127.0.0.1:8766` unless you set `MLX_SCHEDULER_LLM_API`. A standalone finances-only server is also available: `uv run python app/financial_analytics_ui.py` (default port **8770**).

### Google Calendar sync (live)

The day-scheduler app keeps the local agenda **bidirectionally** synced with one Google
Calendar:

- Tasks the LLM creates (today **or** future dates) are pushed as Calendar events.
- Edits and completions update the linked event; soft-deleted rows delete the event.
- Events created on Google are pulled into the SQLite store (incremental `events.list`
  with `syncToken`) and rendered on the day-schedule pane.

Drop the OAuth Desktop client JSON at `credentials/google-calendar-oauth-client.json`,
launch the UI server, then click **Connect Calendar** in the chat header. The first
click opens a browser for consent; subsequent boots refresh silently. Override the
poll interval with `--gcal-poll-sec` (set to `0` to disable) and the secrets path with
`--gcal-client-secrets` / `GOOGLE_CALENDAR_CLIENT_SECRETS`.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/calendar/status` | Connection state, calendar id, last sync timestamp, last error. |
| `POST /api/calendar/auth` | Run the (blocking) OAuth flow and enable polling. Body: `{"calendar_id": "primary"}`. |
| `POST /api/calendar/sync` | Trigger one push + incremental pull cycle on demand. |
| `POST /api/calendar/disable` | Stop syncing (token cache is preserved). |

## Layout

The combined day-scheduler product lives under [`app/`](app/). Earlier exploratory CLIs and reference helpers stay under [`samples/`](samples/) and are reused as baselines (the gateway/API import `samples/mlx_chat_cli.py` for shared MLX helpers).

| Path | Role |
|------|------|
| [`scripts/run-llm-gateway-local-models.sh`](scripts/run-llm-gateway-local-models.sh) | Starts unified gateway: vLLM if env set, else MLX |
| [`app/scheduler_llm_gateway.py`](app/scheduler_llm_gateway.py) | Primary LLM gateway (vLLM OpenAI API + optional MLX fallback) |
| [`app/mlx_llm_gateway.py`](app/mlx_llm_gateway.py) | MLX-only gateway entry (`/health`, streaming chat) |
| [`app/mlx_scheduler_llm_api.py`](app/mlx_scheduler_llm_api.py) | Gateway handler: MLX and vLLM paths, validation, format-repair |
| [`app/vllm_openai_client.py`](app/vllm_openai_client.py) | OpenAI-compatible client for vLLM servers |
| [`app/vllm_gateway_routing.py`](app/vllm_gateway_routing.py) | vLLM URL, diagnose probe, plain-completion route helper |
| [`app/mlx_day_scheduler_ui.py`](app/mlx_day_scheduler_ui.py) | Serves Scheduler `/`, Finances `/finances`, `day_scheduler.html` / `habit_builder.html`, proxies `/chat` |
| [`app/financial_analytics_ui.py`](app/financial_analytics_ui.py) | Financial analytics APIs + ledger background jobs (merged into the shell server) |
| [`app/financial_analytics.html`](app/financial_analytics.html) | Finances page (charts, CSV upload) |
| [`app/day_scheduler.html`](app/day_scheduler.html) | Three-column shell: habits iframe, agenda, chat |
| [`app/habit_builder.html`](app/habit_builder.html) | Habit-builder pane embedded in the shell |
| [`app/scheduler_store.py`](app/scheduler_store.py) | SQLite persistence (habits, tasks, conversation, GCal sync state) |
| [`app/google_calendar_sync.py`](app/google_calendar_sync.py) | Bidirectional Google Calendar sync manager (push dirty rows + incremental pull) |
| [`app/schedule_parse.py`](app/schedule_parse.py) | Strict timetable parsing + validation |
| [`app/mlx_day_scheduler_pipeline.py`](app/mlx_day_scheduler_pipeline.py) | Shared MLX prompt/context/generation pipeline |
| [`app/response_quality.py`](app/response_quality.py) | Self-grade JSON parsing + similarity helpers |
| [`app/habit_schedule.py`](app/habit_schedule.py) | Habit-recurrence helpers |
| [`prompts/day-scheduler-system.md`](prompts/day-scheduler-system.md) | System prompt for day-scheduler mode |
| [`samples/mlx_chat_cli.py`](samples/mlx_chat_cli.py) | CLI chat (including `--day-scheduler`); shared MLX helpers reused by `app/` |
| [`samples/mlx_google_calendar_cli.py`](samples/mlx_google_calendar_cli.py) | Google Calendar CLI exploration |

Place the Google Cloud **Desktop** OAuth JSON at **`credentials/google-calendar-oauth-client.json`** (tracked path for the folder; the JSON itself is gitignored). Override with `--client-secrets` or `GOOGLE_CALENDAR_CLIENT_SECRETS`.

## Checks

```bash
uv run ruff check app/ samples/
uv run pytest
```

## License

See repository files; add a `LICENSE` if you publish this project publicly.
