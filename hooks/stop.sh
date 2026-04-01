#!/usr/bin/env bash
# Claude Code Stop hook
# Posts a task-ended notice to the session's Slack thread.
# Thread state is preserved so `claude --resume` continues in the same thread.

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bridge.conf
source "$BRIDGE_DIR/bridge.conf"

BRIDGE_URL="http://127.0.0.1:${BRIDGE_PORT:-9876}"

if ! curl -sf --max-time 2 "$BRIDGE_URL/health" > /dev/null 2>&1; then
    exit 0
fi

HOOK_INPUT=$(cat)
SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // ""')

curl -sf --max-time 10 \
    -X POST "$BRIDGE_URL/send" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": $(printf '%s' "$SESSION_ID" | jq -Rs .),
         \"text\": \"🏁 *Task ended* — Claude has finished or stopped.\"}" \
    > /dev/null || true

exit 0
