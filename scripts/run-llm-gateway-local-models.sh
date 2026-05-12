#!/usr/bin/env bash
# Scheduler LLM gateway: vLLM Metal (primary) when VLLM_* URLs are set, else in-process MLX.
#
# vLLM (install vllm-metal per https://github.com/vllm-project/vllm-metal ); example:
#   vllm serve "$HOME/models/Qwen3-14B" --port 8000 --served-model-name Qwen3-14B
#   export VLLM_14B_BASE_URL=http://127.0.0.1:8000/v1
#   ./scripts/run-llm-gateway-local-models.sh
#
# MLX-only (no vLLM env): same as before — loads Qwen3-14B from ~/models locally.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HF_HOME="${HF_HOME:-$HOME/models/.hf-cache}"

MODEL="${SCHEDULER_MODEL:-$HOME/models/Qwen3-14B}"

# When VLLM_* URLs are set but processes are not running, fall back to MLX instead of exiting.
# For strict failure: SCHEDULER_LLM_FALLBACK_MLX=0 ./scripts/run-llm-gateway-local-models.sh
export SCHEDULER_LLM_FALLBACK_MLX="${SCHEDULER_LLM_FALLBACK_MLX:-1}"

cd "$ROOT"
exec uv run --group samples-vllm --group samples-mlx python app/scheduler_llm_gateway.py \
  --model "$MODEL" \
  "$@"
