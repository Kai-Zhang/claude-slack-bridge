# Usage Guide

## The session model

Every Claude Code process has a unique session. The bridge maps each session to exactly one Slack thread. All activity — tool calls, approval requests, status notices — flows into that thread.

```
Claude session ──────────────────────────────────── Slack thread
     │                                                    │
     ├─ tool call          →   🔧 Bash: kubectl apply …  ─┤
     ├─ risky command      →   ⚠️ Approval required       ─┤  ← you click ✅/❌ here
     ├─ another tool call  →   🔧 Edit: deployment.yaml  ─┤
     └─ session ends       →   🏁 Task ended              ─┘
```

**Resume behaviour**: `claude --resume` continues the same session — messages append to the existing thread.

**New session**: a fresh `claude` invocation gets a new session ID and opens a new thread, even in the same channel.

**Concurrent sessions**: each running Claude process has its own thread. They can coexist in the same channel or DM.

---

## Session modes

| Mode | `SLACK_CHANNEL_ID` | Who can interact |
|---|---|---|
| **DM** | DM channel ID (`D…`) | The DM owner only |
| **Channel** | Channel ID (`C…`) | Any channel member |

In channel mode the Claude Code instance is associated with the channel, not a specific user. Any member can approve, deny, inject, or stop.

---

## Commands

All commands are scoped to a session thread. There are no workspace-wide commands.

| Command | How to send | Effect |
|---|---|---|
| `@bot inject: <message>` | Reply inside the session thread | Delivers the message to Claude before its next tool call |
| `@bot stop` | Reply inside the session thread | Stops Claude before its next tool call |
| **✅ Approve** | Click button on approval card | Allows the blocked operation to proceed |
| **❌ Deny** | Click button on approval card | Blocks the operation; Claude receives a denial |

In DM mode the `@bot` prefix is optional — `inject: <message>` and `stop` work as plain replies.

**Setup helper** (DM only, no thread required):

| Message | Effect |
|---|---|
| `config` | Bot replies with your DM channel ID for use in `bridge.conf` |

---

## Where commands work

| Location | inject / stop | Notes |
|---|---|---|
| Reply inside a session thread | ✅ | Targets that session only |
| Top-level channel message | ❌ | Bot replies: "use within a session thread" |
| DM reply inside a session thread | ✅ | Targets that session |
| Top-level DM message | ❌ | Bot replies: "use within a session thread" |

An unrecognised `@bot` mention (no matching command) always receives a short help reply listing available commands.

---

## Interaction flows

### Normal session

```
you: claude (new session, ID=abc)
  → bridge creates thread  →  🚀 myrepo [main] — task started

Claude: reads a file          (no Slack notification — read-only tools are skipped)
Claude: runs git status   →   🔧 Bash: git status
Claude: edits a file      →   ✏️  Edit: src/main.py
Claude: finishes          →   🏁 Task ended
```

### Approval flow

```
Claude: attempts kubectl delete namespace staging
  → bridge posts approval card to thread

⚠️ Approval required — Bash
  kubectl delete namespace staging
  [✅ Approve]  [❌ Deny]

you: click ✅ Approve
  → card updates to "✅ Approved"
  → Claude proceeds

--- or ---

you: reply "@bot stop" in the thread
  → approval resolves as denied immediately
  → Claude halts
```

### Inject mid-task

```
Claude: is working on a deployment

you: reply in the session thread
  "@bot inject: use the canary namespace, not staging"

  → before Claude's next tool call, it receives your message
  → Claude adjusts its plan accordingly
```

### Concurrent sessions in the same channel

```
#ops-channel

  Thread 1  ──────────────────────────
  🚀 myrepo [feat/auth] — task started   ← session abc (machine A)
  🔧 Bash: pytest tests/auth/
  ⚠️ Approval required — Bash: rm -rf …
    [✅ Approve]  [❌ Deny]

  Thread 2  ──────────────────────────
  🚀 myrepo [main] — task started        ← session def (machine B)
  🔧 Bash: kubectl apply -f deploy.yaml
  ✏️  Edit: values.yaml
```

Replying `@bot stop` in Thread 1 stops session `abc` only. Thread 2 is unaffected.

---

## Customising the thread title

By default the opening message includes the git repository name and current branch:

```
🚀 myrepo [feat/auth] — task started
```

To add a task description, set `CLAUDE_TASK` before starting Claude:

```bash
CLAUDE_TASK="fix the canary rollout config" claude
```

Result:

```
🚀 myrepo [feat/auth] — fix the canary rollout config
```

In a non-git directory, the working directory name is used instead.
