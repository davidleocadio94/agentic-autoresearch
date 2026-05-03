#!/usr/bin/env bash
# start-loop.sh — launch autoresearch in tmux
#
# Usage:
#   ./scripts/start-loop.sh <problem-path> [--max-hours N] [--max-cost N]
#
# Spawns a tmux session named 'autoresearch' with two panes:
#   left:  the loop  (uv run autoresearch run ... --no-dashboard)
#   right: dashboard (uv run autoresearch dashboard)
#
# Detach with Ctrl-B then D. Reattach with `tmux attach -t autoresearch`.
# Stop with `./scripts/stop-loop.sh`.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <problem-path> [autoresearch-run-flags...]" >&2
  exit 1
fi

PROBLEM="$1"
shift

if [ ! -d "$PROBLEM" ]; then
  echo "error: $PROBLEM does not exist" >&2
  exit 1
fi
if [ ! -f "$PROBLEM/spec.md" ]; then
  echo "error: $PROBLEM/spec.md missing" >&2
  exit 1
fi

PROBLEM=$(cd "$PROBLEM" && pwd)
SESSION="autoresearch"
FRAMEWORK="$(cd "$(dirname "$0")/.." && pwd)"

# already running?
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session '$SESSION' already running. attach with:"
  echo "  tmux attach -t $SESSION"
  echo "or stop first:"
  echo "  ./scripts/stop-loop.sh"
  exit 1
fi

# check claude CLI is available
if ! command -v claude >/dev/null 2>&1; then
  echo "error: claude CLI not on PATH" >&2
  exit 1
fi

LOOP_CMD="cd $FRAMEWORK && DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib caffeinate -d -i -m -s -u uv run autoresearch run $PROBLEM --no-dashboard $*"
DASH_CMD="cd $FRAMEWORK && uv run autoresearch dashboard"

tmux new-session -d -s "$SESSION" -n loop "$LOOP_CMD"
sleep 1
tmux split-window -h -t "$SESSION:0" "$DASH_CMD"
tmux select-pane -t "$SESSION:0.0"

echo "started tmux session '$SESSION'"
echo "  attach:  tmux attach -t $SESSION"
echo "  detach:  Ctrl-B then D"
echo "  dash:    http://127.0.0.1:8765/"
echo "  stop:    ./scripts/stop-loop.sh"
