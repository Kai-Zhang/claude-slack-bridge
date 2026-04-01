# Architecture

## System diagram

```
┌───────────────────────────────────────────────────────────────┐
│ Slack                                                         │
│  ┌──────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────┐  │
│  │ Threads  │  │ Approval     │  │  Slash    │  │ @mention │  │
│  │ & DMs    │  │ cards        │  │ commands  │  │ events   │  │
│  └────┬─────┘  └──────┬───────┘  └─────┬─────┘  └────┬─────┘  │
└───────┼───────────────┼────────────────┼─────────────┼────────┘
        │               │  Socket Mode   │             │
        └───────────────┴────────────────┴─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │     Central Router       │
                    │     slack_router.py      │
                    │                          │
                    │  • Single Slack conn.    │
                    │  • Per-channel queues    │
                    │  • HTTP API :8765        │
                    └────────────┬─────────────┘
                                 │  HTTP (polling)
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
┌─────────▼──────────┐ ┌─────────▼──────────┐ ┌─────────▼──────────┐
│   Local Bridge A   │ │   Local Bridge B   │ │   Local Bridge C   │
│   slack_bridge.py  │ │   slack_bridge.py  │ │   slack_bridge.py  │
│   :9876            │ │   :9876            │ │   :9876            │
└─────────┬──────────┘ └─────────┬──────────┘ └────────┬───────────┘
          │                      │                     │
   ┌──────▼──────┐        ┌──────▼──────┐        ┌─────▼───────┐
   │ Claude Code │        │ Claude Code │        │ Claude Code │
   │  + hooks    │        │  + hooks    │        │  + hooks    │
   └─────────────┘        └─────────────┘        └─────────────┘
```

## Components

### Central Router (`slack_router.py`)

Deployed once by the admin on a shared machine that stays running. Owns the single Slack connection for the whole team.

Responsibilities:
- Maintain a Socket Mode WebSocket connection to Slack
- Receive all incoming Slack events: button interactions, slash commands, @mentions, DM messages
- Route each event to the correct per-channel in-memory queue based on `channel_id`
- Accept message-post and message-update requests from local bridges and call the Slack API
- Respond to DM `config` messages with the sender's channel ID (aids user setup)
- Expose the Router HTTP API on a configurable port (default `8765`)

The router holds no per-user configuration. Routing is purely by `channel_id`, which local bridges supply when they register.

### Local Bridge (`slack_bridge.py`)

Run by each user on the machine where Claude Code executes. Requires no Slack credentials — only the Router URL and a channel ID.

Responsibilities:
- On startup: register `channel_id` with the Router
- On shutdown: unregister from the Router
- Background thread: poll `GET /events` from the Router every 3 s (1 s during approval waits)
- Dispatch polled events: inject/stop commands → `INBOX_FILE`; approve/deny actions → internal approval signal
- Expose the Local Bridge HTTP API on `localhost:9876` for hook scripts
- Manage thread state (`THREAD_FILE`) and inbox (`INBOX_FILE`) on local disk

### Claude Code Hooks

Three shell scripts wired into Claude Code's hook system via `~/.claude/settings.json`. They call the local bridge's HTTP API and are unaware of Slack or the Router.

| Hook | Trigger | Action |
|---|---|---|
| `pre_tool_use.sh` | Before every tool call | Read inbox (deliver inject/stop); check command against `risky_patterns.txt`; if risky, call `/approval` and block |
| `post_tool_use.sh` | After every tool call | Post a summary line to the session thread via `/send` |
| `stop.sh` | When Claude finishes | Post a session-ended notice via `/send`; call `/thread/reset` |

---

## Router HTTP API

The Router listens on `ROUTER_PORT` (default `8765`). All request and response bodies are JSON.

### `POST /register`

Register a local bridge. Creates an event queue for `channel_id` if one does not exist.

Request:
```json
{ "channel_id": "C0123456789" }
```
Response `200`:
```json
{ "ok": true }
```

---

### `DELETE /unregister`

Unregister a local bridge and discard its pending event queue.

Request:
```json
{ "channel_id": "C0123456789" }
```
Response `200`:
```json
{ "ok": true }
```

---

### `GET /events?channel_id=C0123456789`

Return all pending events for the given channel and atomically clear them from the queue. Returns an empty list when nothing is pending.

Response `200`:
```json
{
  "ok": true,
  "events": [
    {
      "type": "action",
      "action_id": "claude_approve",
      "user_id": "U0123456789",
      "channel_id": "C0123456789",
      "message_ts": "1712345678.000100",
      "text": "",
      "ts": "1712345680.000200"
    }
  ]
}
```

---

### `POST /post`

Post a message to a Slack channel or DM on behalf of a local bridge.

Request:
```json
{
  "channel_id": "C0123456789",
  "text": "Plain-text fallback",
  "thread_ts": "1712345678.000100",
  "blocks": []
}
```
`thread_ts` and `blocks` are optional.

Response `200`:
```json
{ "ok": true, "ts": "1712345690.000300" }
```

---

### `POST /update`

Update an existing Slack message (used to replace approval cards after a decision).

Request:
```json
{
  "channel_id": "C0123456789",
  "ts": "1712345690.000300",
  "text": "✅ Approved",
  "blocks": []
}
```

Response `200`:
```json
{ "ok": true }
```

---

### `GET /health`

Liveness check.

Response `200`:
```json
{ "ok": true, "registered_channels": 4 }
```

---

## Local Bridge HTTP API

Exposed on `localhost:BRIDGE_PORT` (default `9876`). Called exclusively by hook scripts. This API is unchanged from the single-user design.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/inbox` | Return and clear queued inject/stop messages |
| `POST` | `/send` | Post a message to the current session thread |
| `POST` | `/approval` | Request approval; blocks until response or timeout |
| `POST` | `/thread/reset` | Clear current thread state (called by stop hook) |

---

## Event types

Events delivered via `GET /events`:

| `type` | `action_id` / content | Produced by |
|---|---|---|
| `action` | `claude_approve` | User clicks Approve button |
| `action` | `claude_deny` | User clicks Deny button |
| `inject` | `text` contains the message | `/inject` slash command, @mention, or DM |
| `stop` | — | `/stop` slash command, @mention, or DM |

---

## Data flows

### Normal tool call

```
Claude executes a tool
  → post_tool_use.sh: POST localhost:9876/send {"text": "🔧 Bash: ls -la"}
  → Local Bridge: POST router:8765/post {"channel_id": "C…", "thread_ts": "…", "text": "…"}
  → Router: Slack API chat.postMessage
  → Slack: message appears in thread
```

### Approval flow

```
Claude is about to run a risky command
  → pre_tool_use.sh: POST localhost:9876/approval {"tool": "Bash", "command": "rm -rf /tmp/build"}
  → Local Bridge: POST router:8765/post  (approval card with Approve / Deny buttons)
  → Router: Slack API chat.postMessage → card appears in thread
  → Local Bridge: poll GET router:8765/events every 1 s

User clicks ✅ Approve in Slack
  → Slack: block_actions event → Router via Socket Mode
  → Router: POST router:8765/update (replace card with "✅ Approved")
  → Router: enqueue {type:"action", action_id:"claude_approve"} for channel

Local Bridge poll returns the action event
  → Local Bridge: /approval returns {"decision": "approved"} to hook
  → pre_tool_use.sh: exits 0
  → Claude: proceeds with the command
```

### Inject / stop flow

```
User types /inject fix the import order first in their channel
  → Slack: slash_command event → Router via Socket Mode
  → Router: enqueue {type:"command", text:"fix the import order first"} for channel

Between tool calls, pre_tool_use.sh calls GET localhost:9876/inbox
  → Local Bridge: GET router:8765/events → receives the command event
  → Local Bridge: appends message to INBOX_FILE
  → /inbox response: {"messages": ["fix the import order first"]}
  → pre_tool_use.sh: writes message to Claude's stdin
  → Claude: reads the note and adjusts its plan
```

---

## Configuration reference

### `router.conf`

| Key | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot User OAuth Token (`xoxb-…`) |
| `SLACK_APP_TOKEN` | Yes | App-Level Token for Socket Mode (`xapp-…`) |
| `ROUTER_SECRET` | Yes | Shared secret for API authentication; all bridges must use the same value |
| `ROUTER_PORT` | No (default `8765`) | Port local bridges connect to |
| `EVENT_TTL` | No (default `300`) | Seconds to retain undelivered events |

### `bridge.conf`

| Key | Required | Description |
|---|---|---|
| `ROUTER_URL` | Yes | Base URL of the Router, e.g. `http://192.168.1.10:8765` |
| `ROUTER_SECRET` | Yes | Shared secret; must match `ROUTER_SECRET` in `router.conf` |
| `SLACK_CHANNEL_ID` | Yes | Channel (`C…`) or DM (`D…`) this bridge is associated with |
| `BRIDGE_PORT` | No (default `9876`) | Local port for hook ↔ bridge communication |
| `APPROVAL_TIMEOUT` | No (default `1800`) | Seconds to wait for an approval response |
| `APPROVAL_DEFAULT` | No (default `deny`) | Decision on timeout: `deny` or `approve` |
| `THREAD_FILE` | No (default `/tmp/claude_threads.json`) | JSON file persisting `session_id → thread_ts` mappings |

---

## Session model

### Session identity

Claude Code assigns a `session_id` to every session. This ID is stable across `claude --resume` — resuming a session reuses the same ID. The bridge uses `session_id` as the key for all per-session state.

Hook scripts extract `session_id` from the JSON payload on stdin and pass it with every request to the local bridge HTTP API.

### Thread mapping

The bridge maintains a persistent JSON file mapping each known session to its Slack thread:

```json
{
  "abc123": "1712345678.000100",
  "def456": "1712345699.000200"
}
```

On first activity for a session, the bridge creates a new thread and records the mapping. On `claude --resume`, the same `session_id` resolves to the same `thread_ts` — messages continue in the existing thread. A new session (new `session_id`) always creates a new thread.

### Per-session state

Each active session in the bridge holds independent state:

| State | Description |
|---|---|
| `thread_ts` | Slack thread timestamp; identifies the session's thread |
| `approval_event` | Threading event signalled when an approve/deny arrives |
| `approval_result` | `'approved'` or `'denied'`, set before signalling |
| `inbox` | In-memory queue of inject/stop messages |

### Event routing within the bridge

The Router continues to route events by `channel_id`. The bridge maintains a reverse mapping `{thread_ts → session_id}` and dispatches each incoming event to the correct session based on the `thread_ts` field in the event payload.

- `block_actions` (button click): Router extracts `container.thread_ts` → bridge matches session
- `app_mention` in thread: Router extracts `event.thread_ts` → bridge matches session
- `message.im` in thread: Router extracts `event.thread_ts` → bridge matches session
- Events with no `thread_ts`: rejected at the router level; inject/stop are not executed

---

## Design notes

**Why a central router instead of one Slack app per user?**
Socket Mode distributes events across all active WebSocket connections from the same app via load balancing. With multiple bridge instances sharing the same app token, an event from user A's channel would randomly land in user B's bridge. A single router connection eliminates this: one connection receives all events, routes by `channel_id`.

**Why polling from local bridge to router instead of router pushing to bridges?**
Local machines are typically behind NAT and have no stable inbound address. Polling (bridges reach out to router) requires only outbound HTTP from user machines. The router never needs to initiate connections to bridges.

**Why no Slack credentials on user machines?**
Keeping the bot token on the router only limits the blast radius of a misconfigured or compromised user machine. Users need only the router URL and their channel ID.

**Why does the router hold no per-user config?**
Routing solely by `channel_id` means adding or removing a user requires zero router changes. A bridge registers when it starts and unregisters when it stops. The router is stateless with respect to user identity.
