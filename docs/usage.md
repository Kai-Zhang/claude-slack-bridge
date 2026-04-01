# Usage Guide

## The session model

Every Claude Code process has a unique session. The bridge maps each session to exactly one Slack thread. All activity — tool calls, approval requests, status notices — flows into that thread.

```
Claude session ──────────────────────────────────── Slack thread
     │                                                    │
     ├─ tool call          →   🔧 Bash: kubectl apply …  ─┤
     ├─ risky command      →   ⚠️ High-risk operation    ─┤  ← you click Yes/No here
     ├─ another tool call  →   🔧 Edit: deployment.yaml  ─┤
     └─ session ends       →   🏁 Task ended             ─┘
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

`inject` and `stop` are scoped to a session thread. `away` and `back` are global toggles for remote approval.

| Command | How to send | Effect |
|---|---|---|
| `/remote-on` | Slash command | Enable remote approval: permission prompts forward to Slack instead of appearing in the CLI |
| `/remote-off` | Slash command | Disable remote approval: permission prompts return to the CLI |
| `@bot inject: <message>` | Reply inside the session thread | Delivers the message to Claude before its next tool call |
| `@bot stop` | Reply inside the session thread | Stops Claude before its next tool call |
| `@bot away` | Top-level message in the configured channel or DM | Enable remote approval: permission prompts forward to Slack instead of appearing in the CLI |
| `@bot back` | Top-level message in the configured channel or DM | Disable remote approval: permission prompts return to the CLI |
| **Yes** | Click button on approval card | Allow this one operation |
| **Yes to All** | Click button on approval card | Allow all subsequent operations of the same type for this session |
| **No** | Click button on approval card | Opens a text prompt; optional reason is sent to Claude as feedback |

In DM mode the `@bot` prefix is optional. Use top-level `away` / `back` messages for remote approval, and thread replies for `inject: <message>` / `stop`.

**Setup helper** (DM only, no thread required):

| Message | Effect |
|---|---|
| `config` | Bot replies with your DM channel ID for use in `bridge.conf` |

---

## Where commands work

| Location | inject / stop | away / back | Notes |
|---|---|---|---|
| Reply inside a session thread | ✅ | ❌ | `away` / `back` are global; use `/remote-on`, `/remote-off`, or a top-level message |
| Top-level channel message | ❌ | ✅ | `inject` / `stop` reply with "use within a session thread" |
| DM reply inside a session thread | ✅ | ❌ | `away` / `back` are global; use `/remote-on`, `/remote-off`, or a top-level message |
| Top-level DM message | ❌ | ✅ | `inject` / `stop` reply with "use within a session thread" |
| Slash command | ❌ | ✅ | `/remote-on` and `/remote-off` are global toggles |

An unrecognised `@bot` mention (no matching command) always receives a short help reply listing available commands.

---

## Remote approval

When you step away from your machine and want Claude to keep working, enable remote approval. Instead of blocking at CLI permission prompts, Claude forwards each request to the session thread. You respond in Slack; Claude continues.

### Enabling and disabling

From a slash command or a top-level message in the configured Slack channel or DM:

```
/remote-on    ← enable remote approval (before you leave)
/remote-off   ← disable it (when you return)
@bot away     ← top-level message alternative
@bot back     ← top-level message alternative
```

Or directly on the machine:

```bash
touch ~/.claude/slack-bridge/.remote_approve   # enable
rm    ~/.claude/slack-bridge/.remote_approve   # disable
```

The change takes effect on the next tool call — no restart needed.

### What always appears in Slack

Regardless of mode, as long as the bridge is running:

- 🚀 Session started
- Tool activity summaries (Edit, Write, Bash, …)
- 🏁 Task ended

These always flow so a thread is always available for session-scoped commands like `inject` and `stop`.

### What only appears in remote approval mode

Permission requests — whenever Claude Code would normally pause at the CLI and ask for your input.

### Permission card

```
🔐 Permission Request — Edit
file: src/main.py
  − def handle():
  + def handle(timeout=30):

  [Yes]  [Yes to All]  [No]
```

| Button | Effect |
|---|---|
| **Yes** | Allow this one operation |
| **Yes to All** | Allow all subsequent operations of this type for the rest of the session. Equivalent to "Yes, don't ask again this session" in the CLI. Scoped by tool type — approving all Edit calls does not affect Bash prompts. |
| **No** | Opens a text prompt. Type a reason (or leave empty) and submit. Claude receives the reason as feedback and adjusts its next attempt. |

### High-risk operations

Commands matching `risky_patterns.txt` always require Slack approval — regardless of whether remote approval is active. They use the same card with a different label:

```
⚠️ High-risk operation — Bash
  kubectl delete namespace prod

  [Yes]  [Yes to All]  [No]
```

### Running Claude in the right mode

For remote approval to work correctly, run Claude with:

```bash
claude --dangerously-skip-permissions
```

Without this flag, Claude Code's own CLI prompts appear after the hook returns — producing double prompting. This flag makes the hook the sole permission gate.

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

### High-risk approval flow

```
Claude: attempts kubectl delete namespace staging
  → bridge posts approval card to thread

⚠️ High-risk operation — Bash
  kubectl delete namespace staging
  [Yes]  [Yes to All]  [No]

you: click Yes
  → card updates to "✅ Yes"
  → Claude proceeds

--- or ---

you: click No
  → text prompt opens
  → you type: "wrong namespace, use staging-v2"
  → Claude receives the reason as feedback
  → Claude retries with the correct namespace

--- or ---

you: reply "@bot stop" in the thread
  → pending approval resolves as denied
  → Claude halts
```

### Remote approval flow

```
Remote approval is active (.remote_approve exists)

Claude: about to edit src/main.py
  → bridge posts permission card to thread

🔐 Permission Request — Edit
  file: src/main.py
  − def handle():
  + def handle(timeout=30):
  [Yes]  [Yes to All]  [No]

you: click Yes to All
  → card updates to "✅ Yes to All (Edit)"
  → all subsequent Edit calls approved for this session
  → Claude proceeds without further Edit prompts
  → Bash commands still ask for approval separately
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
  ⚠️ High-risk operation — Bash: rm -rf …
    [Yes]  [Yes to All]  [No]

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
