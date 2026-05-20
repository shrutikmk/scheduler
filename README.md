# scheduler (Heartbeat)

Local-first workspace for **day planning**, **habit tracking**, and **personal finances**. The browser shell (**Heartbeat**) runs on your machine; the planner chat uses a **vLLM Metal** server (OpenAI-compatible API) on **Qwen3-14B**. Habits, saved tasks, chat history, and optional Google Calendar sync live in **SQLite** under `data/` (gitignored).

## What you get in the UI

Open **`http://127.0.0.1:8765/`** after starting the web server (see below).

| Area | Route / location | Purpose |
|------|------------------|---------|
| **Habits** | Scheduler, left column | Habit Builder (programs, calendar, progress bars). Data syncs to SQLite when the UI server runs. |
| **Planner** | Scheduler, center column | Per-day **saved plan** (tasks in SQLite), **habits due today** (mandatory log days from habit rules), date navigation, **Clear day**. |
| **Chat** | Scheduler, right column | Day-planner assistant; streams timetable replies validated by the LLM gateway. |
| **Settings** | ☰ menu, top-right | Theme (auto / morning / afternoon / night), LLM API status, Google Calendar connect + **Sync now**. |
| **Finances** | `/finances` | CSV upload, ledger, charts, optional LLM labels/insights (same server process). |

**Themes:** Shared **morning / afternoon / night** palettes via `app/heartbeat_theme.css` and `app/heartbeat_theme.js` (auto follows local time unless you pick a fixed theme in Settings).

**Habits ↔ planner:** `GET /api/habits?required_for_date=YYYY-MM-DD` returns `mandatory_for_planner_date` (habit id, title, logged flag). The planner shows due habits as cards; **×** logs the day on the habit calendar (same as clicking the cell in Habit Builder).

## Requirements

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) for dependencies
- **vLLM Metal** ([vllm-metal](https://github.com/vllm-project/vllm-metal)): OpenAI-compatible server for **Qwen3-14B** (scheduler chat, summarization, repair, query parsing, financial labels). Set `VLLM_14B_BASE_URL`. Legacy `VLLM_8B_*` env vars are ignored.

## Install

```bash
cd scheduler
uv sync
uv sync --group samples-vllm
```

Google Calendar sample CLI (optional):

```bash
uv sync --group samples-vllm --group samples-google-calendar
```

Download the scheduler model locally (recommended path `~/models`):

```bash
mkdir -p ~/models
export HF_HOME="$HOME/models/.hf-cache"
hf download Qwen/Qwen3-14B --local-dir "$HOME/models/Qwen3-14B"
```

## Run locally

You need **vLLM inference**, the **LLM gateway**, and the **web UI**. Ports default to **8000** (vLLM), **8766** (gateway), **8765** (UI).

### Option A — one script (recommended)

```bash
./scripts/scheduler-local-stack.sh start    # background
./scripts/scheduler-local-stack.sh status
./scripts/scheduler-local-stack.sh logs     # tail logs
# open http://127.0.0.1:8765/
./scripts/scheduler-local-stack.sh stop
```

Set `SCHEDULER_SKIP_VLLM=1` if vLLM is already running elsewhere. See script header for `SCHEDULER_VLLM_PORT`, `SCHEDULER_GATEWAY_PORT`, `SCHEDULER_WEB_PORT`, and wait timeouts.

### Option B — manual terminals

**Terminal 0 — Qwen3-14B (vLLM)**

```bash
vllm serve "$HOME/models/Qwen3-14B" --port 8000 --served-model-name Qwen3-14B
```

The OpenAI `model` name must match `--served-model-name` (gateway default: `Qwen3-14B`).

**Terminal 1 — LLM gateway**

```bash
export VLLM_14B_BASE_URL=http://127.0.0.1:8000/v1
./scripts/run-llm-gateway-local-models.sh
```

Probe vLLM before the gateway:

```bash
uv run --group samples-vllm python app/scheduler_llm_gateway.py --diagnose-only
```

Exit code **0** means vLLM responded on at least one probe URL (usually `GET …/v1/models`).

Optional env: `SCHEDULER_MODEL`, `MLX_MODEL` (tokenizer id), `HF_HOME`, `VLLM_14B_MODEL`.

The gateway validates timetable bullets, optional **format repair** (`SCHEDULER_FORMAT_REPAIR`, default on), and optional self-grade (`MLX_DAY_SCHEDULER_SELF_GRADE=1` or `--self-grade`, off by default).

| Knob | Default | Effect |
|------|---------|--------|
| `MLX_DAY_SCHEDULER_NO_THINKING=1` | thinking **on** | Faster, less variable replies (skips Qwen3 hidden think pass). |
| `MLX_DAY_SCHEDULER_SELF_GRADE=1` | grader **off** | Second LLM pass after a valid timetable (slower). |

The UI caches gateway `/health` for ~5 s per chat turn.

**Terminal 2 — web shell**

```bash
uv run python app/day_scheduler_web.py
```

- **Scheduler:** `http://127.0.0.1:8765/`
- **Finances:** `http://127.0.0.1:8765/finances`

Chat is proxied to `http://127.0.0.1:8766` unless you set `MLX_SCHEDULER_LLM_API`.

Standalone finances-only server (optional): `uv run python app/financial_analytics_ui.py` (default port **8770**).

## Google Calendar sync

Bidirectional sync with **one** Google calendar when enabled from **Settings → Connect Calendar**:

- LLM-created tasks (today or future) become calendar events.
- After assistant import or task upsert, each affected **plan date** is **rebuilt** on the calendar: overlapping events that day are removed, then current SQLite tasks for that day are pushed (same overlap logic as **Clear day**).
- Single-task status changes (done / pending) patch the linked event without a full-day wipe.
- Events created in Google are pulled incrementally (`syncToken`) into SQLite and appear on the saved plan.

**Setup**

1. Create a Google Cloud **Desktop** OAuth client.
2. Save JSON as `credentials/google-calendar-oauth-client.json` (gitignored; only `credentials/.gitkeep` is tracked).
3. Start the UI server, open **☰ Settings**, click **Connect Calendar** (browser consent on first connect).
4. Use **Sync now** or rely on background polling (`--gcal-poll-sec`, default on; `0` disables).

Overrides: `--gcal-client-secrets`, `GOOGLE_CALENDAR_CLIENT_SECRETS`, `--gcal-token-dir`, `--gcal-poll-sec`.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/calendar/status` | Connection state, calendar id, last sync, errors. |
| `POST /api/calendar/auth` | OAuth flow + enable polling. Body: `{"calendar_id": "primary"}`. |
| `POST /api/calendar/sync` | One push + incremental pull. |
| `POST /api/calendar/disable` | Stop syncing (token cache kept). |

## Data & secrets (local only)

| Path | Notes |
|------|--------|
| `data/scheduler.sqlite` | Habits, per-day tasks, conversation, GCal sync metadata (gitignored). |
| `financial-data/ledger.sqlite` | Finances ledger (gitignored). |
| `credentials/*.json` | OAuth client secrets (gitignored). |
| `~/.config/scheduler/calendar/` | Default OAuth token cache. |
| `prompts/financial-spend-knowledge.md` | Optional private spend hints for LLM titling (gitignored; copy from `prompts/financial-spend-knowledge.example.md`). |

## Project layout

Product code lives under [`app/`](app/). Exploratory CLIs stay under [`samples/`](samples/).

| Path | Role |
|------|------|
| [`scripts/scheduler-local-stack.sh`](scripts/scheduler-local-stack.sh) | Start/stop vLLM + gateway + UI together |
| [`scripts/run-llm-gateway-local-models.sh`](scripts/run-llm-gateway-local-models.sh) | Start LLM gateway only |
| [`app/day_scheduler_web.py`](app/day_scheduler_web.py) | Heartbeat HTTP server: Scheduler, Finances, `/api/*`, `/chat` proxy |
| [`app/day_scheduler.html`](app/day_scheduler.html) | Three-column Scheduler UI |
| [`app/habit_builder.html`](app/habit_builder.html) + [`habit_builder.js`](app/habit_builder.js) | Habit Builder (embedded iframe) |
| [`app/habit_schedule.py`](app/habit_schedule.py) | Habit deadlines + `mandatory_for_planner_date` for the planner |
| [`app/heartbeat_theme.js`](app/heartbeat_theme.js) | Shared theme paint + iframe `postMessage` |
| [`app/heartbeat_shell.css`](app/heartbeat_shell.css) | Shell nav, date controls, settings drawer |
| [`app/scheduler_llm_gateway.py`](app/scheduler_llm_gateway.py) | LLM gateway (vLLM) |
| [`app/scheduler_llm_http_handler.py`](app/scheduler_llm_http_handler.py) | Validation, format-repair, streaming chat |
| [`app/scheduler_store.py`](app/scheduler_store.py) | SQLite persistence |
| [`app/google_calendar_sync.py`](app/google_calendar_sync.py) | Calendar push/pull |
| [`app/financial_analytics_ui.py`](app/financial_analytics_ui.py) | Finances APIs (also mounted on shell server) |
| [`prompts/day-scheduler-system.md`](prompts/day-scheduler-system.md) | Day-planner system prompt |

## Checks

```bash
uv run ruff check app/ samples/
uv run pytest
```

## License

No license file in the repo yet; add one if you publish publicly.
