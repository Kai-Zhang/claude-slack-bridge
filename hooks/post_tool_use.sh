#!/usr/bin/env bash
# Claude Code PostToolUse hook
# Posts a brief summary of the completed tool call to the Slack task thread.
# Read-only tools (Read, Glob, Grep) are skipped to keep the thread readable.

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../bridge.conf
source "$BRIDGE_DIR/bridge.conf"

BRIDGE_URL="http://127.0.0.1:${BRIDGE_PORT:-9876}"

if ! curl -sf --max-time 2 "$BRIDGE_URL/health" > /dev/null 2>&1; then
    exit 0
fi

HOOK_INPUT=$(cat)
TOOL_NAME=$(echo "$HOOK_INPUT" | jq -r '.tool_name // ""')
TOOL_INPUT_JSON=$(echo "$HOOK_INPUT" | jq -c '.tool_input // {}')

case "$TOOL_NAME" in
    Bash)
        COMMAND=$(echo "$TOOL_INPUT_JSON" | jq -r '.command // ""')
        DISPLAY="${COMMAND:0:120}"
        TEXT="🔧 \`Bash\` — \`${DISPLAY}\`"
        ;;
    Edit)
        FILE=$(echo "$TOOL_INPUT_JSON" | jq -r '.file_path // ""')
        TEXT="✏️ \`Edit\` — \`${FILE}\`"
        ;;
    Write)
        FILE=$(echo "$TOOL_INPUT_JSON" | jq -r '.file_path // ""')
        TEXT="📄 \`Write\` — \`${FILE}\`"
        ;;
    Agent)
        TEXT="🤖 Spawned sub-agent"
        ;;
    WebFetch|WebSearch)
        TEXT="🌐 \`${TOOL_NAME}\`"
        ;;
    *)
        # Skip noisy read-only tools: Read, Glob, Grep, etc.
        exit 0
        ;;
esac

curl -sf --max-time 10 \
    -X POST "$BRIDGE_URL/send" \
    -H "Content-Type: application/json" \
    -d "{\"text\": $(printf '%s' "$TEXT" | jq -Rs .)}" \
    > /dev/null

exit 0
