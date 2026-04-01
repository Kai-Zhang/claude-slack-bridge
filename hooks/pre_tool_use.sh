#!/usr/bin/env bash
# Claude Code PreToolUse hook
#
# Two responsibilities:
#   1. Deliver queued inject/stop messages to Claude before the next tool runs.
#   2. Block risky operations and wait for Slack approval.
#
# Exit codes (Claude Code convention):
#   0  — allow the tool call to proceed
#   2  — block the tool call; stdout is shown to Claude as feedback

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bridge.conf
source "$BRIDGE_DIR/bridge.conf"

BRIDGE_URL="http://127.0.0.1:${BRIDGE_PORT:-9876}"

# If the bridge is not running, fail open so Claude is not blocked.
if ! curl -sf --max-time 2 "$BRIDGE_URL/health" > /dev/null 2>&1; then
    exit 0
fi

# Parse hook input (JSON on stdin)
HOOK_INPUT=$(cat)
SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // ""')
TOOL_NAME=$(echo "$HOOK_INPUT"  | jq -r '.tool_name  // ""')
TOOL_INPUT_JSON=$(echo "$HOOK_INPUT" | jq -c '.tool_input // {}')

# ---------------------------------------------------------------------------
# 1. Check inbox for inject / stop messages
# ---------------------------------------------------------------------------
INBOX_RESPONSE=$(curl -sf --max-time 5 \
    "${BRIDGE_URL}/inbox?session_id=${SESSION_ID}" 2>/dev/null \
    || echo '{"messages":[]}')
MESSAGES=$(echo "$INBOX_RESPONSE" | jq -r '.messages[]' 2>/dev/null || true)

if [[ -n "$MESSAGES" ]]; then
    if echo "$MESSAGES" | grep -qx "STOP"; then
        echo "🛑 Stop requested via Slack. Halting execution."
        exit 2
    else
        echo "💬 Message from user (via Slack):"
        echo "$MESSAGES"
        echo ""
        echo "Please incorporate this feedback before continuing with the next action."
        exit 2
    fi
fi

# ---------------------------------------------------------------------------
# 2. Check if this tool call requires approval
# ---------------------------------------------------------------------------
requires_approval() {
    [[ "$TOOL_NAME" != "Bash" ]] && return 1

    local command
    command=$(echo "$TOOL_INPUT_JSON" | jq -r '.command // ""')
    local patterns_file="$BRIDGE_DIR/risky_patterns.txt"
    [[ ! -f "$patterns_file" ]] && return 1

    while IFS= read -r pattern; do
        [[ -z "$pattern" || "$pattern" == \#* ]] && continue
        if echo "$command" | grep -qi -- "$pattern"; then
            return 0
        fi
    done < "$patterns_file"
    return 1
}

if requires_approval; then
    COMMAND=$(echo "$TOOL_INPUT_JSON" | jq -r '.command // ""')
    TIMEOUT="${APPROVAL_TIMEOUT:-1800}"

    RESPONSE=$(curl -sf \
        --max-time "$((TIMEOUT + 30))" \
        -X POST "$BRIDGE_URL/approval" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": $(printf '%s' "$SESSION_ID"  | jq -Rs .),
             \"tool\":       $(printf '%s' "$TOOL_NAME"   | jq -Rs .),
             \"command\":    $(printf '%s' "$COMMAND"     | jq -Rs .),
             \"cwd\":        $(printf '%s' "$PWD"         | jq -Rs .),
             \"task_title\": $(printf '%s' "${CLAUDE_TASK:-}" | jq -Rs .)}" \
        2>/dev/null) || {
        echo "⚠️ Could not reach bridge during approval request. Operation denied."
        exit 2
    }

    DECISION=$(echo "$RESPONSE" | jq -r '.decision // "deny"')
    REASON=$(echo "$RESPONSE"   | jq -r '.reason   // ""')

    if [[ "$DECISION" == "approved" ]]; then
        exit 0
    fi

    case "$REASON" in
        timeout)        echo "⏰ No approval received within ${TIMEOUT}s. Operation denied." ;;
        stop_requested) echo "🛑 Stop signal received during approval wait. Halting execution." ;;
        *)              echo "❌ Operation denied via Slack." ;;
    esac
    exit 2
fi

exit 0
