# scheduler

Local scheduling workspace: a **day-planner chat** (MLX on Apple Silicon), a **Habit Builder** panel, and prompts for calendar-style planning.

## Requirements

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) for dependencies
- MLX runs on **Metal** (Apple Silicon). Models are resolved via `MLX_MODEL` / `--model` / [`samples/mlx_chat_cli.py`](samples/mlx_chat_cli.py) defaults (see that file for paths).

## Install

```bash
cd scheduler
uv sync
uv sync --group samples-mlx
```

## Run the browser shell (recommended)

Use **two terminals**: the LLM runs in a separate gateway process; the UI proxies chat to it.

**Terminal 1 — LLM gateway (loads the model on Metal):**

```bash
uv run --group samples-mlx python samples/mlx_llm_gateway.py
```

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
