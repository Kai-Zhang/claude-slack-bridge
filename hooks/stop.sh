#!/usr/bin/env bash
# Claude Code Stop hook
# Sends a task-ended notice to the Slack thread, then resets the thread so
# the next Claude session opens a fresh thread.

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bridge.conf
source "$BRIDGE_DIR/bridge.conf"

BRIDGE_URL="http://127.0.0.1:${BRIDGE_PORT:-9876}"

if ! curl -sf --max-time 2 "$BRIDGE_URL/health" > /dev/null 2>&1; then
    exit 0
fi

curl -sf --max-time 10 \
    -X POST "$BRIDGE_URL/send" \
    -H "Content-Type: application/json" \
    -d '{"text": "🏁 *Task ended* — Claude has finished or stopped."}' \
    > /dev/null || true

# Clear the thread so the next session starts a new Slack thread
curl -sf --max-time 5 \
    -X POST "$BRIDGE_URL/thread/reset" \
    > /dev/null || true

exit 0
