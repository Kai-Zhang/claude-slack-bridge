# Getting Started

This guide is split into two parts:

- **[Part 1 — Admin Setup](#part-1--admin-setup)**: done once by whoever owns the Slack workspace. Creates the Slack App and deploys the central router.
- **[Part 2 — User Setup](#part-2--user-setup)**: done by each person who wants to use Claude Code with Slack. Requires only the router URL and a channel or DM ID — no Slack credentials.

---

## Part 1 — Admin Setup

### Prerequisites

The machine that will run the router needs:
- Python 3.8+
- `pip install slack_bolt`
- Outbound internet access (to reach Slack's API)
- A stable address reachable by all user machines on the same network or VPN (e.g. `192.168.1.10` or `router.internal`)

---

### Step 1 — Create the Slack App

Open **https://api.slack.com/apps** and sign in to your workspace.

#### 1.1 — Create a new app

1. Click **Create New App** → **From scratch**
2. Name: `Claude Code` (or any name your team will recognise)
3. Select your workspace → **Create App**

#### 1.2 — Enable Socket Mode

1. Left sidebar → **Settings → Socket Mode** → toggle on
2. When prompted to generate an App-Level Token:
   - Name: `router-socket`
   - Scope: `connections:write`
   - Click **Generate**
3. Copy the token — it starts with `xapp-`. This is `SLACK_APP_TOKEN`.

#### 1.3 — Add bot scopes

1. Left sidebar → **OAuth & Permissions**
2. Scroll to **Bot Token Scopes** → **Add an OAuth Scope**
3. Add the following scopes:

| Scope | Purpose |
|---|---|
| `chat:write` | Post and update messages in channels and DMs |
| `app_mentions:read` | Receive @mention events (fallback command method) |
| `im:read` | Receive DM messages (DM mode commands; channel ID auto-reply) |

#### 1.4 — Add slash commands

1. Left sidebar → **Slash Commands** → **Create New Command**

   **Command 1**
   - Command: `/inject`
   - Request URL: `https://placeholder.example.com` *(Socket Mode ignores this)*
   - Short description: `Send a message to Claude`
   - Click **Save**

   **Command 2**
   - Command: `/stop`
   - Request URL: `https://placeholder.example.com`
   - Short description: `Stop Claude's current task`
   - Click **Save**

#### 1.5 — Enable DM messages

This allows users to send messages to the bot directly in the DM tab (required for DM mode commands and the `config` channel ID helper).

1. Left sidebar → **App Home**
2. Under **Show Tabs**, check **Allow users to send Slash commands and messages from the messaging tab**

#### 1.7 — Subscribe to events

1. Left sidebar → **Event Subscriptions** → toggle **Enable Events** on
2. Request URL: `https://placeholder.example.com` *(Socket Mode ignores this)*
3. Under **Subscribe to bot events** → **Add Bot User Event**, add:
   - `app_mention`
   - `message.im`
4. Click **Save Changes**

#### 1.8 — Install the app to the workspace

1. Left sidebar → **OAuth & Permissions** → **Install to Workspace**
2. Review permissions and click **Allow**
   - If your workspace requires admin approval, submit the request and wait for confirmation.
3. Copy the **Bot User OAuth Token** — it starts with `xoxb-`. This is `SLACK_BOT_TOKEN`.

---

### Step 2 — Deploy the Router

On the machine designated as the router host:

#### 2.1 — Configure `router.conf`

Generate a shared secret (keep this — you will give it to each user):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Edit `~/.claude/slack-bridge/router.conf`:

```bash
SLACK_BOT_TOKEN="xoxb-..."    # from Step 1.6
SLACK_APP_TOKEN="xapp-..."    # from Step 1.2
ROUTER_SECRET="<generated secret>"
ROUTER_PORT=8765
EVENT_TTL=300
```

#### 2.2 — Start the router

```bash
cd ~/.claude/slack-bridge
python3 slack_router.py
```

Expected output:
```
[router] HTTP API on http://0.0.0.0:8765
[router] Connecting to Slack via Socket Mode...
[router] Ready.
```

For persistent operation, run it in a tmux session:
```bash
tmux new -s claude-router
python3 ~/.claude/slack-bridge/slack_router.py
# Ctrl-b d to detach
# tmux attach -t claude-router to reattach
```

#### 2.3 — Share the router URL with your team

Tell users the router URL, e.g.:
```
http://192.168.1.10:8765
```

---

## Part 2 — User Setup

### Prerequisites

- Python 3.8+
- `jq` — `sudo apt install jq` or `brew install jq`
- `curl` — pre-installed on most systems
- The router URL from the admin (e.g. `http://192.168.1.10:8765`)

No Slack account credentials are needed.

---

### Step 3 — Get your channel or DM ID

Choose one of the two session modes:

#### DM mode (private — recommended for individuals)

1. Open Slack and find the bot (search for the app name the admin chose, e.g. `Claude Code`)
2. Send it any message — for example: `config`
3. The bot replies with your DM channel ID:
   ```
   Your DM channel ID is: D0123456789
   Use this as SLACK_CHANNEL_ID in your bridge.conf.
   ```
4. Copy the ID.

#### Channel mode (shared — for team sessions)

Use this when a Claude Code instance should be visible and controllable by a whole team.

1. Create a Slack channel for the session (e.g. `#claude-infra-team`)
2. Invite the bot: type `/invite @Claude Code` in the channel
3. Right-click the channel name → **View channel details**
4. The channel ID is at the bottom of the dialog — it starts with `C`.
5. Copy the ID.

In channel mode, any channel member can approve, deny, inject, and stop the session.

---

### Step 4 — Configure `bridge.conf`

Edit `~/.claude/slack-bridge/bridge.conf` on your machine:

```bash
ROUTER_URL="http://192.168.1.10:8765"   # router URL from admin
ROUTER_SECRET="<shared secret>"          # secret from admin
SLACK_CHANNEL_ID="D0123456789"           # your DM or channel ID from Step 3

# Defaults — adjust if needed:
BRIDGE_PORT=9876
APPROVAL_TIMEOUT=1800
APPROVAL_DEFAULT=deny
```

---

### Step 5 — Customise `risky_patterns.txt`

`risky_patterns.txt` lists substrings that cause Claude to pause and request approval before running a Bash command. Each line is a case-insensitive substring; lines starting with `#` are comments.

Review and edit the defaults to match your workflow. Examples:
```
rm -rf
kubectl delete
DROP TABLE
git push --force
```

---

### Step 6 — Start the local bridge

```bash
python3 ~/.claude/slack-bridge/slack_bridge.py
```

Expected output:
```
[bridge] Registered with router at http://192.168.1.10:8765 (channel: D0123456789)
[bridge] HTTP API on http://127.0.0.1:9876
[bridge] Ready.
```

Keep it running in a terminal or tmux session for as long as you use Claude Code.

---

### Step 7 — Configure Claude Code hooks

Add the following to `~/.claude/settings.json`. If the file already exists, merge the `hooks` key into it.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "/home/ubuntu/.claude/slack-bridge/hooks/pre_tool_use.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": ".*",
        "hooks": [
          {
            "type": "command",
            "command": "/home/ubuntu/.claude/slack-bridge/hooks/post_tool_use.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/ubuntu/.claude/slack-bridge/hooks/stop.sh"
          }
        ]
      }
    ]
  }
}
```

Adjust the paths if your install location differs. Check that the hook scripts are executable:

```bash
ls -l ~/.claude/slack-bridge/hooks/
# should show -rwxr-xr-x for all three scripts
# if not: chmod +x ~/.claude/slack-bridge/hooks/*.sh
```

---

### Step 8 — Verify

1. Confirm the bridge is running (`Step 6`)
2. Start a Claude Code session in any project
3. Check Slack — you should see a `🚀 Claude task started` message in your DM or channel
4. Ask Claude to do something simple (`list files`) — a `🔧 Bash` line should appear in the thread
5. Ask Claude to run a command that matches one of your risky patterns — an approval card with **✅ Approve** and **❌ Deny** buttons should appear; click one and confirm Claude responds accordingly

---

## Troubleshooting

**No message in Slack when Claude starts**
- Confirm the bridge is running and shows no errors
- Confirm the router is running and reachable: `curl http://<router-url>/health`
- Confirm `SLACK_CHANNEL_ID` is correct — it must be the ID (starts with `C` or `D`), not the channel name
- For channel mode: confirm the bot has been `/invite`d to the channel

**Approval card appears but buttons do nothing**
- Check the router terminal for errors
- Confirm `SLACK_APP_TOKEN` (`xapp-…`) is set correctly in `router.conf` — this is the Socket Mode token

**Slash commands return "dispatch_failed"**
- Socket Mode must be enabled in the Slack App settings
- Confirm the router is running and connected

**Hook scripts fail with "jq: command not found"**
- Install jq: `sudo apt install jq` or `brew install jq`

**Hooks are not firing**
- Verify `~/.claude/settings.json` contains the hooks configuration (Step 7)
- Run a hook manually to check for errors:
  ```bash
  echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' \
    | ~/.claude/slack-bridge/hooks/pre_tool_use.sh
  ```

**Bridge cannot reach router**
- Confirm the router machine's firewall allows inbound TCP on `ROUTER_PORT` (default `8765`) from user machines
- Confirm `ROUTER_URL` in `bridge.conf` uses the correct IP or hostname and port
