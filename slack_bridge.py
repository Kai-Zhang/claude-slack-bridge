#!/usr/bin/env python3
"""
Claude Code Slack Bridge — Local Bridge

Connects to the central router and exposes a local HTTP API for Claude Code
hook scripts. Requires no direct Slack credentials.

Endpoints  (localhost:{BRIDGE_PORT}  — for hook scripts only)
-------------------------------------------------------------
GET  /health          liveness check
GET  /inbox           return and clear queued inject/stop messages
POST /send            post a message to the current task thread
POST /approval        request approval (blocks until response or timeout)
POST /thread/reset    clear the current thread state
"""

import atexit
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse


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


CONFIG_FILE = Path(__file__).parent / "bridge.conf"
if not CONFIG_FILE.exists():
    sys.exit(f"Error: {CONFIG_FILE} not found.")

cfg = _load_config(CONFIG_FILE)

ROUTER_URL       = cfg.get('ROUTER_URL', '').rstrip('/')
ROUTER_SECRET    = cfg.get('ROUTER_SECRET', '')
SLACK_CHANNEL_ID = cfg.get('SLACK_CHANNEL_ID', '')
BRIDGE_PORT      = int(cfg.get('BRIDGE_PORT', 9876))
APPROVAL_TIMEOUT = int(cfg.get('APPROVAL_TIMEOUT', 1800))
APPROVAL_DEFAULT = cfg.get('APPROVAL_DEFAULT', 'deny')
THREAD_FILE      = Path(cfg.get('THREAD_FILE', '/tmp/claude_thread_ts'))
INBOX_FILE       = Path(cfg.get('INBOX_FILE', '/tmp/claude_inbox'))

for _var, _val in [('ROUTER_URL',       ROUTER_URL),
                   ('ROUTER_SECRET',    ROUTER_SECRET),
                   ('SLACK_CHANNEL_ID', SLACK_CHANNEL_ID)]:
    if not _val or _val.startswith('your-') or _val.startswith('http://your'):
        sys.exit(f"Error: {_var} is not configured in bridge.conf")


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_lock             = threading.Lock()
_thread_ts:       Optional[str] = None
_approval_event   = threading.Event()
_approval_result: Optional[str] = None   # 'approved' | 'denied'
_waiting_for_approval = False


# ---------------------------------------------------------------------------
# Router HTTP client
# ---------------------------------------------------------------------------

def _router_request(method: str, path: str, body: Optional[dict] = None,
                    params: str = '') -> dict:
    url = f"{ROUTER_URL}{path}"
    if params:
        url = f"{url}?{params}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            'Authorization': f'Bearer {ROUTER_SECRET}',
            'Content-Type':  'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {'ok': False, 'error': f'HTTP {e.code}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _router_get_events() -> List[dict]:
    result = _router_request('GET', '/events', params=f'channel_id={SLACK_CHANNEL_ID}')
    return result.get('events', [])


def _router_post(text: str, thread_ts: Optional[str] = None,
                 blocks: Optional[list] = None) -> Optional[str]:
    """Post a message via the router. Returns the Slack message timestamp."""
    body: dict = {'channel_id': SLACK_CHANNEL_ID, 'text': text}
    if thread_ts:
        body['thread_ts'] = thread_ts
    if blocks:
        body['blocks'] = blocks
    result = _router_request('POST', '/post', body)
    return result.get('ts')


# ---------------------------------------------------------------------------
# Thread management
# ---------------------------------------------------------------------------

def _get_or_create_thread() -> str:
    global _thread_ts
    with _lock:
        if _thread_ts:
            return _thread_ts
    if THREAD_FILE.exists():
        ts = THREAD_FILE.read_text().strip()
        if ts:
            with _lock:
                _thread_ts = ts
            return ts
    ts = _router_post("🚀 *Claude task started*")
    if not ts:
        raise RuntimeError("Failed to create Slack thread: router returned no timestamp")
    THREAD_FILE.write_text(ts)
    with _lock:
        _thread_ts = ts
    return ts


def _post_to_thread(text: str, blocks: Optional[list] = None) -> None:
    thread_ts = _get_or_create_thread()
    _router_post(text, thread_ts=thread_ts, blocks=blocks)


# ---------------------------------------------------------------------------
# Inbox helpers
# ---------------------------------------------------------------------------

def _append_inbox(message: str) -> None:
    with _lock:
        with open(INBOX_FILE, 'a') as f:
            f.write(message + '\n')


# ---------------------------------------------------------------------------
# Event polling (background thread)
# ---------------------------------------------------------------------------

def _handle_event(event: dict) -> None:
    global _approval_result
    etype = event.get('type')

    if etype == 'action':
        action_id = event.get('action_id', '')
        if action_id in ('claude_approve', 'claude_deny'):
            with _lock:
                _approval_result = 'approved' if action_id == 'claude_approve' else 'denied'
            _approval_event.set()

    elif etype == 'inject':
        text = event.get('text', '').strip()
        if text:
            _append_inbox(text)

    elif etype == 'stop':
        _append_inbox('STOP')


def _poll_loop() -> None:
    while True:
        try:
            events = _router_get_events()
            for event in events:
                _handle_event(event)
        except Exception:
            pass
        time.sleep(1.0 if _waiting_for_approval else 3.0)


# ---------------------------------------------------------------------------
# HTTP handler  (local API for Claude Code hooks — interface unchanged)
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access logs

    def _read_json(self) -> dict:
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/health':
            self._send_json(200, {'status': 'ok'})

        elif path == '/inbox':
            messages: List[str] = []
            with _lock:
                if INBOX_FILE.exists() and INBOX_FILE.stat().st_size > 0:
                    content = INBOX_FILE.read_text()
                    INBOX_FILE.write_text('')
                    messages = [m for m in content.splitlines() if m.strip()]
            self._send_json(200, {'messages': messages})

        else:
            self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        global _thread_ts, _approval_result, _waiting_for_approval
        path = urlparse(self.path).path
        data = self._read_json()

        if path == '/send':
            try:
                _post_to_thread(data.get('text', ''), data.get('blocks'))
                self._send_json(200, {'ok': True})
            except Exception as e:
                self._send_json(500, {'error': str(e)})

        elif path == '/approval':
            tool    = data.get('tool', 'Bash')
            command = data.get('command', '')
            display = command[:300].replace('`', "'")

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"⚠️ *Approval required* — `{tool}`\n```{display}```",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type":      "button",
                            "text":      {"type": "plain_text", "text": "✅ Approve"},
                            "action_id": "claude_approve",
                            "style":     "primary",
                            "value":     "approve",
                        },
                        {
                            "type":      "button",
                            "text":      {"type": "plain_text", "text": "❌ Deny"},
                            "action_id": "claude_deny",
                            "style":     "danger",
                            "value":     "deny",
                        },
                    ],
                },
            ]

            try:
                _post_to_thread("Approval required", blocks)
            except Exception as e:
                self._send_json(500, {'error': f'failed to post approval card: {e}'})
                return

            with _lock:
                _approval_event.clear()
                _approval_result = None
                _waiting_for_approval = True

            deadline = time.monotonic() + APPROVAL_TIMEOUT
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if _approval_event.wait(timeout=min(2.0, remaining)):
                    break
                # Allow /stop to interrupt an in-progress approval wait
                with _lock:
                    if INBOX_FILE.exists() and INBOX_FILE.stat().st_size > 0:
                        if 'STOP' in INBOX_FILE.read_text().splitlines():
                            _waiting_for_approval = False
                            self._send_json(200, {'decision': 'denied', 'reason': 'stop_requested'})
                            return

            with _lock:
                _waiting_for_approval = False
                decision   = _approval_result or APPROVAL_DEFAULT
                timed_out  = _approval_result is None

            result: dict = {'decision': decision}
            if timed_out:
                result['reason'] = 'timeout'
            self._send_json(200, result)

        elif path == '/thread/reset':
            with _lock:
                _thread_ts = None
            if THREAD_FILE.exists():
                THREAD_FILE.unlink()
            self._send_json(200, {'ok': True})

        else:
            self._send_json(404, {'error': 'not found'})


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

def _register() -> None:
    result = _router_request('POST', '/register', {'channel_id': SLACK_CHANNEL_ID})
    if result.get('ok'):
        print(f"[bridge] Registered with router (channel: {SLACK_CHANNEL_ID})", flush=True)
    else:
        print(f"[bridge] Warning: registration failed — {result.get('error')}", flush=True)


def _unregister() -> None:
    _router_request('DELETE', '/unregister', {'channel_id': SLACK_CHANNEL_ID})
    print("[bridge] Unregistered from router.", flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    _register()
    atexit.register(_unregister)
    threading.Thread(target=_poll_loop, daemon=True).start()
    print(f"[bridge] HTTP API on http://127.0.0.1:{BRIDGE_PORT}", flush=True)
    print("[bridge] Ready.", flush=True)
    try:
        HTTPServer(('127.0.0.1', BRIDGE_PORT), _Handler).serve_forever()
    except KeyboardInterrupt:
        pass
