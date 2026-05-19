#!/usr/bin/env bash
# Start or stop the full local stack (vLLM + LLM gateway + day scheduler web UI) from one shell.
#
# Usage:
#   ./scripts/scheduler-local-stack.sh start   # background; logs under scripts/.scheduler-stack/logs/
#   ./scripts/scheduler-local-stack.sh stop
#   ./scripts/scheduler-local-stack.sh restart  # stop then start (full cycle)
#   ./scripts/scheduler-local-stack.sh status
#   ./scripts/scheduler-local-stack.sh logs    # tail -f all logs (Ctrl+C stops tail only)
#   ./scripts/scheduler-local-stack.sh run     # one terminal: services in background + tail -f; Ctrl+C stops all
#
# Env:
#   SCHEDULER_SKIP_VLLM=1     — do not start vLLM (set VLLM_14B_BASE_URL yourself)
#   SCHEDULER_VLLM_PORT=8000
#   SCHEDULER_VLLM_MODEL      — default ~/models/Qwen3-14B
#   SCHEDULER_VLLM_SERVED_NAME — default Qwen3-14B
#   SCHEDULER_GATEWAY_PORT=8766
#   SCHEDULER_WEB_PORT=8765
#   SCHEDULER_VLLM_WAIT_SEC — max wait for vLLM /v1/models (default 600)
#   SCHEDULER_GATEWAY_WAIT_SEC — max wait for gateway /health (default 60)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATEDIR="$ROOT/scripts/.scheduler-stack"
PIDFILE="$STATEDIR/pids"
LOGDIR="$STATEDIR/logs"

export HF_HOME="${HF_HOME:-$HOME/models/.hf-cache}"

VLLM_PORT="${SCHEDULER_VLLM_PORT:-8000}"
VLLM_MODEL="${SCHEDULER_VLLM_MODEL:-$HOME/models/Qwen3-14B}"
VLLM_SERVED_NAME="${SCHEDULER_VLLM_SERVED_NAME:-Qwen3-14B}"
GATEWAY_PORT="${SCHEDULER_GATEWAY_PORT:-8766}"
WEB_PORT="${SCHEDULER_WEB_PORT:-8765}"
VLLM_WAIT_SEC="${SCHEDULER_VLLM_WAIT_SEC:-600}"
GATEWAY_WAIT_SEC="${SCHEDULER_GATEWAY_WAIT_SEC:-60}"

GATEWAY_HOST="127.0.0.1"

wait_http() {
  local url=$1
  local msg=$2
  local max_sec=$3
  local label=${4:-$url}
  local child_pid=${5:-}
  local waited=0
  local next_progress=30
  echo "Waiting for $label (timeout ${max_sec}s; model load can take several minutes) ..."
  while [[ "$waited" -lt "$max_sec" ]]; do
    if [[ -n "$child_pid" ]] && ! kill -0 "$child_pid" 2>/dev/null; then
      echo "error: process (pid $child_pid) exited before $label responded." >&2
      return 1
    fi
    # No -S: avoids printing "Connection refused" every poll while the server is still binding.
    if curl -sf --connect-timeout 2 --max-time 15 -o /dev/null "$url" 2>/dev/null; then
      echo "$msg"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
    if [[ "$waited" -ge "$next_progress" ]]; then
      echo "  ... still waiting (${waited}s / ${max_sec}s)"
      next_progress=$((next_progress + 30))
    fi
  done
  echo "error: timed out after ${max_sec}s waiting for $label ($url)" >&2
  return 1
}

had_vllm_in_pidfile() {
  [[ -f "$PIDFILE" ]] || return 1
  grep -q '^vllm ' "$PIDFILE"
}

cmd_status() {
  if [[ ! -f "$PIDFILE" ]]; then
    echo "No pidfile at $PIDFILE (stack not started via this script)."
    return 1
  fi
  local ok=0
  while read -r name pid; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "$name pid $pid: running"
    else
      echo "$name pid $pid: not running"
      ok=1
    fi
  done < "$PIDFILE"
  return "$ok"
}

cmd_stop() {
  local had_vllm=0
  local lines
  if had_vllm_in_pidfile; then
    had_vllm=1
  fi

  if [[ -f "$PIDFILE" ]]; then
    echo "Stopping processes from $PIDFILE ..."
    lines=()
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -n "$line" ]] && lines+=("$line")
    done <"$PIDFILE"
    for (( idx=${#lines[@]} - 1; idx >= 0; idx-- )); do
      read -r name pid <<<"${lines[idx]}"
      if kill -0 "$pid" 2>/dev/null; then
        echo "  SIGTERM $name (pid $pid)"
        kill -TERM "$pid" 2>/dev/null || true
      fi
    done
    sleep 1
    for (( idx=${#lines[@]} - 1; idx >= 0; idx-- )); do
      read -r name pid <<<"${lines[idx]}"
      if kill -0 "$pid" 2>/dev/null; then
        echo "  SIGKILL $name (pid $pid)"
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
    rm -f "$PIDFILE"
  fi

  # Catch uv/python children and anything still bound to our ports.
  local ports
  ports=(8765 8766)
  if [[ "$had_vllm" -eq 1 ]]; then
    ports+=(8000)
  fi
  "$ROOT/scripts/free_scheduler_ports.sh" "${ports[@]}"
  echo "Stop complete."
}

cmd_restart() {
  echo "Restarting stack (stop then start) ..."
  cmd_stop || true
  cmd_start
}

cleanup_run() {
  local pids_to_kill
  pids_to_kill=()
  [[ -n "${vllm_pid:-}" ]] && pids_to_kill+=("$vllm_pid")
  [[ -n "${gw_pid:-}" ]] && pids_to_kill+=("$gw_pid")
  [[ -n "${web_pid:-}" ]] && pids_to_kill+=("$web_pid")
  for pid in "${pids_to_kill[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 1
  for pid in "${pids_to_kill[@]}"; do
    kill -KILL "$pid" 2>/dev/null || true
  done
  if [[ -n "${tail_pid:-}" ]]; then
    kill -TERM "$tail_pid" 2>/dev/null || true
  fi
  local ports
  ports=(8765 8766)
  [[ "${SCHEDULER_SKIP_VLLM:-0}" != "1" ]] && ports+=(8000)
  "$ROOT/scripts/free_scheduler_ports.sh" "${ports[@]}" || true
  rm -f "$PIDFILE" 2>/dev/null || true
}

cmd_run() {
  local wait_pids
  local p
  mkdir -p "$LOGDIR"
  trap cleanup_run INT TERM EXIT
  SCHEDULER_STACK_FOREGROUND=1 cmd_start
  echo "Tailing logs (Ctrl+C stops the stack). Logs: $LOGDIR"
  tail -n 0 -f "$LOGDIR/vllm.log" "$LOGDIR/gateway.log" "$LOGDIR/web.log" & tail_pid=$!
  wait_pids=()
  for p in "${vllm_pid:-}" "${gw_pid:-}" "${web_pid:-}"; do
    [[ -n "$p" ]] && wait_pids+=("$p")
  done
  if [[ ${#wait_pids[@]} -gt 0 ]]; then
    wait "${wait_pids[@]}" 2>/dev/null || true
  fi
}

cmd_start() {
  local any_alive=0
  mkdir -p "$LOGDIR" "$STATEDIR"
  touch "$LOGDIR/vllm.log" "$LOGDIR/gateway.log" "$LOGDIR/web.log"

  if [[ -f "$PIDFILE" ]]; then
    any_alive=0
    while read -r name pid; do
      [[ -z "$pid" ]] && continue
      if kill -0 "$pid" 2>/dev/null; then
        any_alive=1
        break
      fi
    done <"$PIDFILE"
    if [[ "$any_alive" -eq 1 ]]; then
      echo "Stack already running (see $PIDFILE). Stop first or run: $0 stop" >&2
      exit 1
    fi
  fi
  rm -f "$PIDFILE"

  if [[ "${SCHEDULER_SKIP_VLLM:-0}" == "1" ]]; then
    if [[ -z "${VLLM_14B_BASE_URL:-}" ]]; then
      echo "error: SCHEDULER_SKIP_VLLM=1 requires VLLM_14B_BASE_URL (e.g. http://127.0.0.1:8000/v1)" >&2
      exit 1
    fi
  else
    if ! command -v vllm >/dev/null 2>&1; then
      echo "error: vllm not on PATH (install vLLM or set SCHEDULER_SKIP_VLLM=1)" >&2
      exit 1
    fi
    export VLLM_14B_BASE_URL="http://127.0.0.1:${VLLM_PORT}/v1"
  fi

  vllm_pid=""
  gw_pid=""
  web_pid=""

  if [[ "${SCHEDULER_SKIP_VLLM:-0}" != "1" ]]; then
    echo "Starting vLLM on port $VLLM_PORT (log: $LOGDIR/vllm.log) ..."
    nohup vllm serve "$VLLM_MODEL" --port "$VLLM_PORT" --served-model-name "$VLLM_SERVED_NAME" \
      >>"$LOGDIR/vllm.log" 2>&1 &
    vllm_pid=$!
    echo "vllm $vllm_pid" >>"$PIDFILE"
    sleep 3
    if ! kill -0 "$vllm_pid" 2>/dev/null; then
      echo "error: vLLM exited right after start. See $LOGDIR/vllm.log" >&2
      tail -n 80 "$LOGDIR/vllm.log" >&2 || true
      cmd_stop || true
      exit 1
    fi
    wait_http \
      "http://127.0.0.1:${VLLM_PORT}/v1/models" \
      "vLLM is up." \
      "$VLLM_WAIT_SEC" \
      "vLLM OpenAI API on port ${VLLM_PORT}" \
      "$vllm_pid" || {
      echo "vLLM log tail ($LOGDIR/vllm.log):" >&2
      tail -n 60 "$LOGDIR/vllm.log" 2>/dev/null || true
      cmd_stop || true
      exit 1
    }
  fi

  echo "Starting LLM gateway on $GATEWAY_HOST:$GATEWAY_PORT (log: $LOGDIR/gateway.log) ..."
  nohup "$ROOT/scripts/run-llm-gateway-local-models.sh" --host "$GATEWAY_HOST" --port "$GATEWAY_PORT" \
    >>"$LOGDIR/gateway.log" 2>&1 &
  gw_pid=$!
  echo "gateway $gw_pid" >>"$PIDFILE"
  sleep 2
  if ! kill -0 "$gw_pid" 2>/dev/null; then
    echo "error: LLM gateway exited right after start. See $LOGDIR/gateway.log" >&2
    tail -n 80 "$LOGDIR/gateway.log" >&2 || true
    cmd_stop || true
    exit 1
  fi
  wait_http \
    "http://${GATEWAY_HOST}:${GATEWAY_PORT}/health" \
    "Gateway is up." \
    "$GATEWAY_WAIT_SEC" \
    "LLM gateway on ${GATEWAY_HOST}:${GATEWAY_PORT}" \
    "$gw_pid" || {
    echo "Gateway log tail ($LOGDIR/gateway.log):" >&2
    tail -n 60 "$LOGDIR/gateway.log" 2>/dev/null || true
    cmd_stop || true
    exit 1
  }

  echo "Starting web UI on port $WEB_PORT (log: $LOGDIR/web.log) ..."
  nohup env MLX_SCHEDULER_LLM_API="http://${GATEWAY_HOST}:${GATEWAY_PORT}" \
    OAUTHLIB_INSECURE_TRANSPORT=1 \
    uv run python "$ROOT/app/day_scheduler_web.py" --port "$WEB_PORT" \
    >>"$LOGDIR/web.log" 2>&1 &
  web_pid=$!
  echo "web $web_pid" >>"$PIDFILE"

  echo ""
  echo "Stack started."
  echo "  Web UI:    http://127.0.0.1:${WEB_PORT}/"
  echo "  Gateway:   http://${GATEWAY_HOST}:${GATEWAY_PORT}/"
  if [[ "${SCHEDULER_SKIP_VLLM:-0}" != "1" ]]; then
    echo "  vLLM OpenAI API: ${VLLM_14B_BASE_URL}"
  fi
  echo "  Logs:      $LOGDIR"
  echo "  Stop:      $0 stop"
  echo "  Restart:   $0 restart"
  if [[ -z "${SCHEDULER_STACK_FOREGROUND:-}" ]]; then
    echo "  Tail logs: $0 logs"
  fi
}

cmd_logs() {
  if [[ ! -d "$LOGDIR" ]]; then
    echo "No log dir yet ($LOGDIR). Run $0 start first." >&2
    exit 1
  fi
  touch "$LOGDIR/vllm.log" "$LOGDIR/gateway.log" "$LOGDIR/web.log"
  tail -n 50 -f "$LOGDIR/vllm.log" "$LOGDIR/gateway.log" "$LOGDIR/web.log"
}

usage() {
  sed -n '2,20p' "$0" | sed 's/^# *//'
}

main() {
  local sub=${1:-}
  case "$sub" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
    run) cmd_run ;;
    restart) cmd_restart ;;
    -h | --help | help) usage ;;
    *)
      echo "usage: $0 {start|stop|status|logs|run|restart}" >&2
      exit 1
      ;;
  esac
}

main "$@"
