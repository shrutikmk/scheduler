#!/usr/bin/env bash
# Free TCP ports used by the MLX day scheduler UI (8765) and LLM gateway (8766).
# With no arguments, only those defaults are freed. Pass port numbers to override, e.g.:
#   ./scripts/free_scheduler_ports.sh 8765 8766 8000
set -euo pipefail
if [[ "$#" -gt 0 ]]; then
  ports=("$@")
else
  ports=(8765 8766)
fi
for port in "${ports[@]}"; do
  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    echo "Port $port: already free"
    continue
  fi
  for pid in $pids; do
    echo "Port $port: stopping PID $pid ($(ps -p "$pid" -o comm= 2>/dev/null || echo '?'))"
    kill -TERM "$pid" 2>/dev/null || true
  done
  sleep 0.5
  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "Port $port: force killing $pids"
    kill -KILL $pids 2>/dev/null || true
  fi
done
echo "Done. Verify:"
for port in "${ports[@]}"; do
  lsof -nP -iTCP:"$port" -sTCP:LISTEN || echo "  $port: nothing listening"
done
