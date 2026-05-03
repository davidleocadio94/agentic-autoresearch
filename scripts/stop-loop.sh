#!/usr/bin/env bash
# stop-loop.sh — clean shutdown of an autoresearch tmux session
#
# Sends SIGTERM via `autoresearch stop` (lets the orchestrator persist final
# state to the DB), then kills the tmux session.

set -euo pipefail

SESSION="autoresearch"
FRAMEWORK="$(cd "$(dirname "$0")/.." && pwd)"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "no tmux session '$SESSION' running."
else
  echo "stopping orchestrator (SIGTERM via autoresearch stop)..."
  (cd "$FRAMEWORK" && uv run autoresearch stop) || true
  sleep 2
  tmux kill-session -t "$SESSION" || true
  echo "tmux session killed."
fi

# Belt and suspenders: kill any stray claude or autoresearch processes
pkill -TERM -f "autoresearch run" 2>/dev/null || true
pkill -TERM -f "claude -p" 2>/dev/null || true

echo "done."
