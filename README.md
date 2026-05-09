# scheduler

Local scheduling workspace: a **day-planner chat** (MLX on Apple Silicon), a **Habit Builder** panel, and prompts for calendar-style planning.

## Requirements

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) for dependencies
- MLX runs on **Metal** (Apple Silicon). The scheduler defaults to a fast
  `Qwen3-8B` model and can run `Qwen3-14B` in the background as a fallback/reference.

## Install

```bash
cd scheduler
uv sync
uv sync --group samples-mlx
```

Model snapshots should live under `~/models`:

```bash
mkdir -p ~/models
HF_HOME="$HOME/models/.hf-cache" hf download Qwen/Qwen3-8B --local-dir "$HOME/models/Qwen3-8B"
HF_HOME="$HOME/models/.hf-cache" hf download Qwen/Qwen3-Embedding-4B --local-dir "$HOME/models/Qwen3-Embedding-4B"
```

`Qwen3-14B` is used as the expensive reference model when present at
`~/models/Qwen3-14B`.

## Run the browser shell (recommended)

Use **two terminals**: the LLM runs in a separate gateway process; the UI proxies chat to it.

**Terminal 1 — LLM gateway (loads the model on Metal):**

```bash
uv run --group samples-mlx python samples/mlx_llm_gateway.py
```

Useful gateway flags:

```bash
uv run --group samples-mlx python samples/mlx_llm_gateway.py \
  --cheap-model "$HOME/models/Qwen3-8B" \
  --expensive-model "$HOME/models/Qwen3-14B" \
  --embedding-model "$HOME/models/Qwen3-Embedding-4B"
```

The gateway validates the fast answer, falls back to `Qwen3-14B` when needed,
and can later replace the visible plan if the background reference response is
semantically different below the configured similarity threshold. Use
`--no-background-reference` if running both scheduler models creates too much
Metal memory pressure.

**Terminal 2 — static shell + habits (default `http://127.0.0.1:8765/`):**

```bash
uv run python samples/mlx_day_scheduler_ui.py
```

The UI talks to the gateway at `http://127.0.0.1:8766` unless you set `MLX_SCHEDULER_LLM_API`.

## Layout

| Path | Role |
|------|------|
| [`samples/mlx_llm_gateway.py`](samples/mlx_llm_gateway.py) | Internal HTTP API for MLX inference (`/health`, streaming chat) |
| [`samples/mlx_day_scheduler_ui.py`](samples/mlx_day_scheduler_ui.py) | Serves `day_scheduler.html` / `habit_builder.html`, proxies `/chat` to the gateway |
| [`samples/day_scheduler.html`](samples/day_scheduler.html) | Three-column shell: habits iframe, agenda, chat |
| [`prompts/day-scheduler-system.md`](prompts/day-scheduler-system.md) | System prompt for day-scheduler mode |
| [`samples/mlx_chat_cli.py`](samples/mlx_chat_cli.py) | CLI chat (including `--day-scheduler`) |

## Checks

```bash
uv run ruff check samples/
uv run pytest
```

## License

See repository files; add a `LICENSE` if you publish this project publicly.
