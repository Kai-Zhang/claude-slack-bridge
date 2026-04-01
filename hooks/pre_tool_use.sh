#!/usr/bin/env bash
# Claude Code PreToolUse hook
#
# Responsibilities:
#   1. On first call of a session: ensure the Slack thread exists so the user
#      can interact with it (e.g. send @bot away) before any tool output arrives.
#   2. Deliver queued inject/stop messages to Claude before the next tool runs.
#   3. Block commands matching risky_patterns.txt and wait for Slack approval.
#      (Always active when the bridge is running, regardless of .remote_approve.)
#   4. If .remote_approve exists, forward all permission-requiring tool calls to
#      Slack and wait for approval. Mirrors the CLI "yes/yes to all/no" prompt.
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
SESSION_ID=$(echo "$HOOK_INPUT"  | jq -r '.session_id // ""')
TOOL_NAME=$(echo "$HOOK_INPUT"   | jq -r '.tool_name  // ""')
TOOL_INPUT_JSON=$(echo "$HOOK_INPUT" | jq -c '.tool_input // {}')

# ---------------------------------------------------------------------------
# 1. Ensure the Slack thread exists on the first call of this session.
#    This guarantees a thread is available for @bot away before any tool runs.
# ---------------------------------------------------------------------------
FIRST_CALL_MARKER="/tmp/claude_bridge_started_${SESSION_ID}"
if [[ ! -f "$FIRST_CALL_MARKER" ]]; then
    if curl -sf --max-time 5 \
        -X POST "$BRIDGE_URL/session/start" \
        -H "Content-Type: application/json" \
        -d "{\"session_id\": $(printf '%s' "$SESSION_ID"     | jq -Rs .),
             \"cwd\":        $(printf '%s' "$PWD"             | jq -Rs .),
             \"task_title\": $(printf '%s' "${CLAUDE_TASK:-}" | jq -Rs .)}" \
        > /dev/null 2>&1; then
        touch "$FIRST_CALL_MARKER"
    fi
fi

# ---------------------------------------------------------------------------
# 2. Check inbox for inject / stop messages
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
# 3. Determine whether this tool call requires Slack approval
# ---------------------------------------------------------------------------

# Check risky_patterns.txt (always active; sets IS_RISKY flag for card label)
IS_RISKY="false"
PATTERNS_FILE="$BRIDGE_DIR/risky_patterns.txt"
if [[ "$TOOL_NAME" == "Bash" && -f "$PATTERNS_FILE" ]]; then
    COMMAND=$(echo "$TOOL_INPUT_JSON" | jq -r '.command // ""')
    while IFS= read -r pattern; do
        [[ -z "$pattern" || "$pattern" == \#* ]] && continue
        if echo "$COMMAND" | grep -qi -- "$pattern"; then
            IS_RISKY="true"
            break
        fi
    done < "$PATTERNS_FILE"
fi

# Check remote approval (.remote_approve file gates this path)
NEEDS_APPROVAL="$IS_RISKY"
if [[ "$NEEDS_APPROVAL" == "false" && -f "$BRIDGE_DIR/.remote_approve" ]]; then
    _PTOOLS="${PERMISSION_TOOLS:-Edit MultiEdit Write Bash NotebookEdit}"
    for _pt in $_PTOOLS; do
        if [[ "$TOOL_NAME" == "$_pt" ]]; then
            NEEDS_APPROVAL="true"
            break
        fi
    done
fi

[[ "$NEEDS_APPROVAL" == "false" ]] && exit 0

# ---------------------------------------------------------------------------
# 4. Request Slack approval — blocks until the user responds or sends @bot stop.
#    No timeout: mirrors the CLI behaviour where Claude waits indefinitely.
# ---------------------------------------------------------------------------
RESPONSE=$(curl -sf \
    --max-time 0 \
    -X POST "$BRIDGE_URL/approval" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": $(printf '%s' "$SESSION_ID"     | jq -Rs .),
         \"tool\":       $(printf '%s' "$TOOL_NAME"      | jq -Rs .),
         \"tool_input\": $TOOL_INPUT_JSON,
         \"is_risky\":   $IS_RISKY,
         \"cwd\":        $(printf '%s' "$PWD"            | jq -Rs .),
         \"task_title\": $(printf '%s' "${CLAUDE_TASK:-}" | jq -Rs .)}" \
    2>/dev/null) || {
    echo "⚠️ Could not reach bridge during approval request. Operation denied."
    exit 2
}

DECISION=$(echo "$RESPONSE"    | jq -r '.decision    // "denied"')
DENY_REASON=$(echo "$RESPONSE" | jq -r '.deny_reason // ""')

if [[ "$DECISION" == "approved" ]]; then
    exit 0
fi

case "$(echo "$RESPONSE" | jq -r '.reason // ""')" in
    stop_requested) echo "🛑 Stop signal received during approval wait. Halting execution." ;;
    *)
        if [[ -n "$DENY_REASON" ]]; then
            echo "❌ Denied: $DENY_REASON"
        else
            echo "❌ Operation denied via Slack."
        fi
        ;;
esac
exit 2
