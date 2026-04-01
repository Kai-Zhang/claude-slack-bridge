# claude-slack-bridge

Stream Claude Code activity to Slack in real time, gate risky operations behind approval buttons, and send commands back to Claude from any device.

## What it does

When a Claude Code session starts, the bridge opens a Slack thread and keeps it updated as Claude works. Configurable command patterns trigger an Approve / Deny card — Claude blocks until you respond. You can also redirect or stop Claude at any time from Slack.

```
Claude runs: kubectl delete namespace production
  → Slack: ⚠️ Approval required  [✅ Approve] [❌ Deny]
  → you tap Deny on your phone
  → Claude sees a denial and adjusts its plan
```

```
you send: /inject focus on staging, not prod
  → Claude receives the note before its next action
```

## Multi-user design

A single Slack App and a shared central router serve an entire team. Each user runs a lightweight local bridge on the machine where Claude Code runs. Sessions are isolated — activity in one user's channel or DM is invisible to others, and only that user (or members of their chosen channel) can approve, deny, inject, or stop their session.

```
Slack ←── Socket Mode ──→ [Central Router]
                                │  HTTP
             ┌──────────────────┼──────────────────┐
             ↓                  ↓                  ↓
       [Bridge A]          [Bridge B]         [Bridge C]
       localhost:9876      localhost:9876      localhost:9876
             │                  │                  │
       Claude Code A      Claude Code B      Claude Code C
```

## Session modes

**DM mode** — the Claude Code instance is associated with one Slack user. Activity posts to the private DM between that user and the bot. Only that user can interact.

**Channel mode** — the Claude Code instance is associated with a Slack channel. Activity is visible to all channel members, and any member can approve, deny, inject, or stop the session.

## Commands

| Command | Where | Effect |
|---|---|---|
| `/inject <message>` | slash command | Delivers a message to Claude before its next tool call |
| `/stop` | slash command | Stops Claude before its next tool call |
| `@bot inject: <message>` | @mention | Same as `/inject` (fallback) |
| `@bot stop` | @mention | Same as `/stop` (fallback) |
| **✅ Approve** | approval card | Allows the blocked operation to proceed |
| **❌ Deny** | approval card | Blocks the operation; Claude receives a denial |

## Repository layout

```
slack_router.py          central router  — admin deploys once, stays running
router.conf              router configuration template
slack_bridge.py          local bridge    — each user runs on their own machine
bridge.conf              local bridge configuration template
risky_patterns.txt       substrings that trigger approval (one per line)
hooks/
  pre_tool_use.sh        check inbox; block risky commands pending approval
  post_tool_use.sh       post tool call summary to the session thread
  stop.sh                post session-ended notice; reset thread state
docs/
  getting-started.md     full setup guide for admins and users
  architecture.md        component design, Router API, data flows
```

## Requirements

| Component | Requirement |
|---|---|
| Router machine | Python 3.8+, `pip install slack_bolt` |
| Each user machine | Python 3.8+, `jq`, `curl` — no Slack SDK or credentials needed |
| Slack | A workspace where you can install apps |

## Security

All Router HTTP endpoints require `Authorization: Bearer <ROUTER_SECRET>`. The secret is generated once by the admin and shared with users via `bridge.conf`. Slack tokens never leave the router machine.

## Documentation

- **[Getting Started](docs/getting-started.md)** — create the Slack App, deploy the router, connect each user
- **[Usage Guide](docs/usage.md)** — session model, commands, interaction flows, multi-session scenarios
- **[Architecture](docs/architecture.md)** — component design, Router API reference, data flows

## Behaviour notes

- **Bridge not running:** hooks exit 0 immediately — Claude operates normally, no interruption
- **Router not reachable:** bridge behaves as if not running — fail open
- **Approval timeout:** resolves to `APPROVAL_DEFAULT` (deny by default)
- **`/stop` during approval wait:** cancels the pending approval and halts Claude
- **Router restart:** in-memory event queues are cleared; active approval waits time out; bridges reconnect automatically on next poll
- **Bridge restart mid-session:** thread resumes — thread timestamp is persisted to `THREAD_FILE` on disk
