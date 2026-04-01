#!/usr/bin/env python3
"""
Claude Code Slack Bridge — Central Router

Maintains a single Socket Mode connection to Slack and exposes an HTTP API
for local bridge instances. Routes Slack events to per-channel queues.

All events include a `thread_ts` field when the Slack event occurred inside a
thread. Bridges use this field to route events to the correct session.

All HTTP endpoints require:  Authorization: Bearer <ROUTER_SECRET>

Endpoints
---------
POST   /register              register a bridge for a channel
DELETE /unregister            unregister a bridge
GET    /events?channel_id=…   poll and clear pending events for a channel
POST   /post                  post a message to Slack on behalf of a bridge
POST   /update                update an existing Slack message
GET    /health                liveness check
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config(path: Path) -> Dict[str, str]:
    cfg: Dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


CONFIG_FILE = Path(__file__).parent / "router.conf"
if not CONFIG_FILE.exists():
    sys.exit(f"Error: {CONFIG_FILE} not found.")

cfg = _load_config(CONFIG_FILE)

SLACK_BOT_TOKEN = cfg.get('SLACK_BOT_TOKEN', '')
SLACK_APP_TOKEN = cfg.get('SLACK_APP_TOKEN', '')
ROUTER_SECRET   = cfg.get('ROUTER_SECRET', '')
ROUTER_HOST     = cfg.get('ROUTER_HOST', '0.0.0.0')
ROUTER_PORT     = int(cfg.get('ROUTER_PORT', 8765))
EVENT_TTL       = int(cfg.get('EVENT_TTL', 300))

for _var, _val in [('SLACK_BOT_TOKEN', SLACK_BOT_TOKEN),
                   ('SLACK_APP_TOKEN', SLACK_APP_TOKEN),
                   ('ROUTER_SECRET',   ROUTER_SECRET)]:
    if not _val or _val.startswith('your-') or _val.startswith('xoxb-your'):
        sys.exit(f"Error: {_var} is not configured in router.conf")


# ---------------------------------------------------------------------------
# Event queues  (channel_id → [(enqueue_monotonic, event_dict), ...])
# ---------------------------------------------------------------------------

_lock        = threading.Lock()
_queues:     Dict[str, List[Tuple[float, dict]]] = {}
_registered: set = set()


def _enqueue(channel_id: str, event: dict) -> None:
    with _lock:
        if channel_id not in _queues:
            _queues[channel_id] = []
        _queues[channel_id].append((time.monotonic(), event))


def _dequeue_all(channel_id: str) -> List[dict]:
    with _lock:
        items = _queues.get(channel_id, [])
        _queues[channel_id] = []
        return [e for _, e in items]


def _ttl_cleanup() -> None:
    while True:
        time.sleep(60)
        cutoff = time.monotonic() - EVENT_TTL
        with _lock:
            for cid in list(_queues.keys()):
                _queues[cid] = [(t, e) for t, e in _queues[cid] if t > cutoff]
            for cid in list(_queues.keys()):
                if cid not in _registered and not _queues[cid]:
                    del _queues[cid]


# ---------------------------------------------------------------------------
# Slack app
# ---------------------------------------------------------------------------

slack_app = App(token=SLACK_BOT_TOKEN)


def _parse_inject(raw: str) -> str:
    """Extract payload from 'inject: foo' or 'inject foo'."""
    raw = raw.strip()
    if raw.lower().startswith('inject'):
        return raw[6:].lstrip(': ').strip()
    return ''


def _mode_text(value: str) -> str:
    if value == 'away':
        return "Remote approval enabled. Permission prompts will forward to Slack. 🛫"
    return "Remote approval disabled. Permission prompts return to the CLI. 🛬"


def _enqueue_mode(channel_id: str, value: str, user_id: str) -> str:
    _enqueue(channel_id, {
        "type":    "mode",
        "value":   value,
        "user_id": user_id,
        "ts":      str(time.time()),
    })
    return _mode_text(value)


def _mode_thread_error() -> str:
    return (
        "`away` and `back` are global commands and do not run inside a session thread.\n"
        "Use `/remote-on`, `/remote-off`, or send `@bot away` / `@bot back` as a top-level message."
    )


@slack_app.command("/remote-on")
def on_remote_on_command(ack, body):
    ack(_enqueue_mode(body['channel_id'], 'away', body['user_id']))


@slack_app.command("/remote-off")
def on_remote_off_command(ack, body):
    ack(_enqueue_mode(body['channel_id'], 'back', body['user_id']))


# --- Approval button handlers ---

@slack_app.action("claude_approve")
def on_approve(ack, body, client):
    ack()
    channel_id = body['channel']['id']
    message_ts = body['message']['ts']
    thread_ts  = body.get('container', {}).get('thread_ts', message_ts)
    _enqueue(channel_id, {
        "type":       "action",
        "action_id":  "claude_approve",
        "thread_ts":  thread_ts,
        "user_id":    body['user']['id'],
        "channel_id": channel_id,
        "message_ts": message_ts,
        "ts":         str(time.time()),
    })
    content_blocks = [b for b in body['message'].get('blocks', [])
                      if b.get('type') != 'actions']
    client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text="✅ Yes",
        blocks=content_blocks + [{"type": "section", "text": {"type": "mrkdwn", "text": "✅ *Yes*"}}],
    )


@slack_app.action("claude_approve_all")
def on_approve_all(ack, body, client):
    ack()
    channel_id = body['channel']['id']
    message_ts = body['message']['ts']
    thread_ts  = body.get('container', {}).get('thread_ts', message_ts)
    tool_type  = body['actions'][0].get('value', '')
    _enqueue(channel_id, {
        "type":       "action",
        "action_id":  "claude_approve_all",
        "tool_type":  tool_type,
        "thread_ts":  thread_ts,
        "user_id":    body['user']['id'],
        "channel_id": channel_id,
        "message_ts": message_ts,
        "ts":         str(time.time()),
    })
    label = f"Yes to All (`{tool_type}`)" if tool_type else "Yes to All"
    content_blocks = [b for b in body['message'].get('blocks', [])
                      if b.get('type') != 'actions']
    client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text=f"✅ {label}",
        blocks=content_blocks + [{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *{label}*"}}],
    )


@slack_app.action("claude_deny")
def on_deny(ack, body, client):
    ack()
    channel_id = body['channel']['id']
    message_ts = body['message']['ts']
    thread_ts  = body.get('container', {}).get('thread_ts', message_ts)

    private_metadata = json.dumps({
        "channel_id": channel_id,
        "message_ts": message_ts,
        "thread_ts":  thread_ts,
    })

    try:
        client.views_open(
            trigger_id=body['trigger_id'],
            view={
                "type":             "modal",
                "callback_id":      "claude_deny_modal",
                "private_metadata": private_metadata,
                "title":            {"type": "plain_text", "text": "Deny operation"},
                "submit":           {"type": "plain_text", "text": "Deny"},
                "close":            {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type":     "input",
                        "block_id": "reason_block",
                        "optional": True,
                        "label":    {"type": "plain_text", "text": "Reason for Claude (optional)"},
                        "element":  {
                            "type":        "plain_text_input",
                            "action_id":   "reason_input",
                            "multiline":   True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Explain why, or leave empty for a plain denial",
                            },
                        },
                    },
                ],
            },
        )
    except Exception:
        # Fallback: deny immediately if modal cannot open (e.g. trigger_id expired)
        _enqueue(channel_id, {
            "type":       "action",
            "action_id":  "claude_deny",
            "thread_ts":  thread_ts,
            "user_id":    body['user']['id'],
            "channel_id": channel_id,
            "message_ts": message_ts,
            "ts":         str(time.time()),
        })
        content_blocks = [b for b in body['message'].get('blocks', [])
                          if b.get('type') != 'actions']
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="❌ No",
            blocks=content_blocks + [{"type": "section", "text": {"type": "mrkdwn", "text": "❌ *No*"}}],
        )


@slack_app.view("claude_deny_modal")
def on_deny_modal_submit(ack, body, client):
    ack()
    metadata   = json.loads(body['view']['private_metadata'])
    channel_id = metadata['channel_id']
    message_ts = metadata['message_ts']
    thread_ts  = metadata['thread_ts']

    values = body['view']['state']['values']
    reason = (values.get('reason_block', {})
                    .get('reason_input', {})
                    .get('value') or '').strip()

    _enqueue(channel_id, {
        "type":        "action",
        "action_id":   "claude_deny",
        "deny_reason": reason or None,
        "thread_ts":   thread_ts,
        "user_id":     body['user']['id'],
        "channel_id":  channel_id,
        "message_ts":  message_ts,
        "ts":          str(time.time()),
    })

    deny_text = "❌ *No*"
    if reason:
        deny_text += f"\n> {reason}"

    content_blocks: list = []
    try:
        history = client.conversations_history(
            channel=channel_id, latest=message_ts, limit=1, inclusive=True,
        )
        msgs = history.get('messages') or []
        if msgs:
            content_blocks = [b for b in (msgs[0].get('blocks') or [])
                               if b.get('type') != 'actions']
    except Exception:
        pass

    client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text="❌ No",
        blocks=content_blocks + [{"type": "section", "text": {"type": "mrkdwn", "text": deny_text}}],
    )


# --- @mention handler (channel mode) ---

@slack_app.event("app_mention")
def on_mention(body, say):
    event      = body['event']
    channel_id = event['channel']
    thread_ts  = event.get('thread_ts')     # set only when reply is inside a thread
    reply_ts   = thread_ts or event['ts']

    # Strip the leading @mention token (<@UXXXXX>)
    raw  = event.get('text', '')
    parts = raw.split(None, 1)
    cmd   = parts[1].strip() if len(parts) > 1 else ''
    lower = cmd.lower()

    if lower in ('away', 'back'):
        if thread_ts:
            say(_mode_thread_error(), thread_ts=reply_ts)
        else:
            say(_enqueue_mode(channel_id, lower, event['user']), thread_ts=reply_ts)
        return

    if not thread_ts:
        # Outside a thread — never execute session commands
        if lower in ('stop',) or lower.startswith('inject'):
            say(
                "Commands must be sent as a reply *within a session thread*.",
                thread_ts=reply_ts,
            )
        else:
            say(
                "Reply within a session thread:\n"
                "• `stop`\n"
                "• `inject: <message>`\n\n"
                "Global commands:\n"
                "• `/remote-on` or top-level `@bot away`\n"
                "• `/remote-off` or top-level `@bot back`",
                thread_ts=reply_ts,
            )
        return

    # Inside a thread — execute session commands
    if lower == 'stop':
        _enqueue(channel_id, {
            "type": "stop", "thread_ts": thread_ts,
            "user_id": event['user'], "ts": str(time.time()),
        })
        say("Stop signal queued for Claude.", thread_ts=reply_ts)

    elif lower.startswith('inject'):
        msg = _parse_inject(cmd)
        if msg:
            _enqueue(channel_id, {
                "type": "inject", "text": msg, "thread_ts": thread_ts,
                "user_id": event['user'], "ts": str(time.time()),
            })
            say(f"Message queued for Claude: _{msg}_", thread_ts=reply_ts)
        else:
            say("Usage: `inject: <message>`", thread_ts=reply_ts)

    else:
        say("Commands: `stop`  |  `inject: <message>`", thread_ts=reply_ts)


# --- DM message handler ---

@slack_app.event("message")
def on_dm_message(body, say):
    event = body.get('event', {})
    # Only handle direct messages; skip bot messages and edits
    if event.get('channel_type') != 'im':
        return
    if event.get('bot_id') or event.get('subtype'):
        return

    channel_id = event['channel']
    text       = event.get('text', '').strip()
    # Strip leading @mention if present (user may @mention the bot even in a DM)
    _parts = text.split(None, 1)
    if _parts and _parts[0].startswith('<@') and _parts[0].endswith('>'):
        text = _parts[1].strip() if len(_parts) > 1 else ''
    lower      = text.lower()
    thread_ts  = event.get('thread_ts')     # set only when message is a thread reply
    reply_ts   = thread_ts or event['ts']

    # Setup helper — always available outside threads
    if lower in ('config', 'id') and not thread_ts:
        say(
            f"Your DM channel ID is: `{channel_id}`\n"
            f"Set this as `SLACK_CHANNEL_ID` in your `bridge.conf`."
        )
        return

    if lower in ('away', 'back'):
        if thread_ts:
            say(_mode_thread_error(), thread_ts=reply_ts)
        else:
            say(_enqueue_mode(channel_id, lower, event['user']))
        return

    if not thread_ts:
        # Outside a thread — never execute session commands
        if lower in ('stop',) or lower.startswith('inject'):
            say("Commands must be sent as a reply *within a session thread*.")
        else:
            say(
                "Reply within a session thread:\n"
                "• `stop`\n"
                "• `inject: <message>`\n\n"
                "Global commands:\n"
                "• `/remote-on` or top-level `away`\n"
                "• `/remote-off` or top-level `back`\n\n"
                "Send `config` to get your DM channel ID."
            )
        return

    # Inside a thread — execute session commands
    if lower == 'stop':
        _enqueue(channel_id, {
            "type": "stop", "thread_ts": thread_ts,
            "user_id": event['user'], "ts": str(time.time()),
        })
        say("Stop signal queued for Claude.", thread_ts=reply_ts)

    elif lower.startswith('inject'):
        msg = _parse_inject(text)
        if msg:
            _enqueue(channel_id, {
                "type": "inject", "text": msg, "thread_ts": thread_ts,
                "user_id": event['user'], "ts": str(time.time()),
            })
            say(f"Message queued for Claude: _{msg}_", thread_ts=reply_ts)
        else:
            say("Usage: `inject: <message>`  or  `stop`", thread_ts=reply_ts)

    else:
        say("Commands: `stop`  |  `inject: <message>`", thread_ts=reply_ts)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _auth_ok(self) -> bool:
        return self.headers.get('Authorization', '') == f'Bearer {ROUTER_SECRET}'

    def _require_auth(self) -> bool:
        if not self._auth_ok():
            self._send_json(401, {'error': 'unauthorized'})
            return False
        return True

    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == '/health':
            with _lock:
                n = len(_registered)
            self._send_json(200, {'ok': True, 'registered_channels': n})

        elif parsed.path == '/events':
            channel_id = (params.get('channel_id') or [None])[0]
            if not channel_id:
                self._send_json(400, {'error': 'channel_id query param required'})
                return
            self._send_json(200, {'ok': True, 'events': _dequeue_all(channel_id)})

        else:
            self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        data = self._read_json()

        if path == '/register':
            channel_id = data.get('channel_id', '')
            if not channel_id:
                self._send_json(400, {'error': 'channel_id required'})
                return
            with _lock:
                _registered.add(channel_id)
                if channel_id not in _queues:
                    _queues[channel_id] = []
            print(f"[router] + registered {channel_id}", flush=True)
            self._send_json(200, {'ok': True})

        elif path == '/post':
            channel_id = data.get('channel_id', '')
            if not channel_id:
                self._send_json(400, {'error': 'channel_id required'})
                return
            try:
                kwargs: dict = dict(channel=channel_id, text=data.get('text', ''))
                if data.get('thread_ts'):
                    kwargs['thread_ts'] = data['thread_ts']
                if data.get('blocks'):
                    kwargs['blocks'] = data['blocks']
                result = slack_app.client.chat_postMessage(**kwargs)
                self._send_json(200, {'ok': True, 'ts': result['ts']})
            except Exception as e:
                self._send_json(500, {'error': str(e)})

        elif path == '/update':
            channel_id = data.get('channel_id', '')
            ts         = data.get('ts', '')
            if not channel_id or not ts:
                self._send_json(400, {'error': 'channel_id and ts required'})
                return
            try:
                kwargs = dict(channel=channel_id, ts=ts, text=data.get('text', ''))
                if data.get('blocks'):
                    kwargs['blocks'] = data['blocks']
                slack_app.client.chat_update(**kwargs)
                self._send_json(200, {'ok': True})
            except Exception as e:
                self._send_json(500, {'error': str(e)})

        else:
            self._send_json(404, {'error': 'not found'})

    def do_DELETE(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        data = self._read_json()

        if path == '/unregister':
            channel_id = data.get('channel_id', '')
            with _lock:
                _registered.discard(channel_id)
                _queues.pop(channel_id, None)
            print(f"[router] - unregistered {channel_id}", flush=True)
            self._send_json(200, {'ok': True})

        else:
            self._send_json(404, {'error': 'not found'})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_http() -> None:
    server = HTTPServer((ROUTER_HOST, ROUTER_PORT), _Handler)
    print(f"[router] HTTP API on http://{ROUTER_HOST}:{ROUTER_PORT}", flush=True)
    server.serve_forever()


if __name__ == '__main__':
    threading.Thread(target=_ttl_cleanup, daemon=True).start()
    threading.Thread(target=_run_http,    daemon=True).start()
    print("[router] Connecting to Slack via Socket Mode...", flush=True)
    try:
        SocketModeHandler(slack_app, SLACK_APP_TOKEN).start()
    except KeyboardInterrupt:
        pass
