#!/usr/bin/env bash
# Scheduler LLM gateway: vLLM Metal (OpenAI API) via app/scheduler_llm_gateway.py
#
# Prerequisites: running vLLM server, e.g.:
#   vllm serve "$HOME/models/Qwen3-14B" --port 8000 --served-model-name Qwen3-14B
#   export VLLM_14B_BASE_URL=http://127.0.0.1:8000/v1
#   ./scripts/run-llm-gateway-local-models.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HF_HOME="${HF_HOME:-$HOME/models/.hf-cache}"

MODEL="${SCHEDULER_MODEL:-$HOME/models/Qwen3-14B}"

cd "$ROOT"
exec uv run --group samples-vllm python app/scheduler_llm_gateway.py \
  --model "$MODEL" \
  "$@"
