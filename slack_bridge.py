#!/usr/bin/env python3
"""
Claude Code Slack Bridge — Local Bridge

Each Claude Code session (identified by session_id from hook payloads) has its
own Slack thread, approval gate, and inbox queue. Multiple sessions can run
concurrently on the same bridge instance.

Endpoints  (localhost:{BRIDGE_PORT}  — called by hook scripts only)
-------------------------------------------------------------------
GET  /health                  liveness check
GET  /inbox?session_id=…      return and clear queued inject/stop messages
POST /send                    post to the session thread
POST /approval                request approval; blocks until response or timeout
POST /thread/reset            force-start a new thread for a session
"""

import atexit
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse


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
THREAD_FILE      = Path(cfg.get('THREAD_FILE', '/tmp/claude_threads.json'))

for _var, _val in [('ROUTER_URL',       ROUTER_URL),
                   ('ROUTER_SECRET',    ROUTER_SECRET),
                   ('SLACK_CHANNEL_ID', SLACK_CHANNEL_ID)]:
    if not _val or _val.startswith('your-') or _val.startswith('http://your'):
        sys.exit(f"Error: {_var} is not configured in bridge.conf")


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------

class _Session:
    def __init__(self, session_id: str, thread_ts: Optional[str] = None):
        self.session_id            = session_id
        self.thread_ts             = thread_ts
        self.task_title: Optional[str] = None   # from CLAUDE_TASK env var
        self.cwd:        Optional[str] = None   # working dir of the Claude process
        self.approval_event        = threading.Event()
        self.approval_result: Optional[str] = None   # 'approved' | 'denied'
        self.waiting_for_approval  = False
        self.inbox:      List[str] = []
        self.inbox_lock            = threading.Lock()
        self._create_lock          = threading.Lock()  # serialises thread creation


_global_lock:   threading.Lock       = threading.Lock()
_sessions:      Dict[str, _Session]  = {}   # session_id → _Session
_ts_to_session: Dict[str, str]       = {}   # thread_ts  → session_id
_saved_threads: Dict[str, str]       = {}   # persisted session_id → thread_ts


def _load_saved_threads() -> None:
    global _saved_threads
    if THREAD_FILE.exists():
        try:
            _saved_threads = json.loads(THREAD_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            _saved_threads = {}


def _save_threads() -> None:
    data: Dict[str, str] = {}
    with _global_lock:
        for sid, sess in _sessions.items():
            if sess.thread_ts:
                data[sid] = sess.thread_ts
    try:
        THREAD_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _get_or_create_session(session_id: str) -> _Session:
    with _global_lock:
        if session_id in _sessions:
            return _sessions[session_id]
        ts = _saved_threads.get(session_id)
        sess = _Session(session_id=session_id, thread_ts=ts)
        if ts:
            _ts_to_session[ts] = session_id
        _sessions[session_id] = sess
        return sess


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

def _build_thread_title(session: _Session) -> str:
    cwd = session.cwd or os.getcwd()
    try:
        repo_root = subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            stderr=subprocess.DEVNULL, cwd=cwd,
        ).decode().strip()
        repo_name = Path(repo_root).name
        branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            stderr=subprocess.DEVNULL, cwd=cwd,
        ).decode().strip()
        context = f"{repo_name} [{branch}]"
    except Exception:
        context = Path(cwd).name

    if session.task_title:
        return f"🚀 *{context}* — {session.task_title}"
    return f"🚀 *{context}* — task started"


def _get_or_create_thread(session: _Session) -> str:
    if session.thread_ts:
        return session.thread_ts

    with session._create_lock:
        if session.thread_ts:   # re-check after acquiring lock
            return session.thread_ts

        ts = _router_post(_build_thread_title(session))
        if not ts:
            raise RuntimeError("Failed to create Slack thread: router returned no timestamp")

        session.thread_ts = ts
        with _global_lock:
            _ts_to_session[ts] = session.session_id
            _saved_threads[session.session_id] = ts
        _save_threads()
        return ts


def _post_to_thread(session: _Session, text: str,
                    blocks: Optional[list] = None) -> None:
    thread_ts = _get_or_create_thread(session)
    _router_post(text, thread_ts=thread_ts, blocks=blocks)


# ---------------------------------------------------------------------------
# Event polling  (background thread)
# ---------------------------------------------------------------------------

def _handle_event(event: dict) -> None:
    thread_ts = event.get('thread_ts')
    if not thread_ts:
        return

    with _global_lock:
        session_id = _ts_to_session.get(thread_ts)
        session    = _sessions.get(session_id) if session_id else None

    if not session:
        return

    etype = event.get('type')

    if etype == 'action':
        action_id = event.get('action_id', '')
        if action_id in ('claude_approve', 'claude_deny'):
            session.approval_result = 'approved' if action_id == 'claude_approve' else 'denied'
            session.approval_event.set()

    elif etype == 'inject':
        text = event.get('text', '').strip()
        if text:
            with session.inbox_lock:
                session.inbox.append(text)

    elif etype == 'stop':
        with session.inbox_lock:
            session.inbox.append('STOP')


def _poll_loop() -> None:
    while True:
        try:
            events = _router_get_events()
            for event in events:
                _handle_event(event)
        except Exception:
            pass
        with _global_lock:
            any_waiting = any(s.waiting_for_approval for s in _sessions.values())
        time.sleep(1.0 if any_waiting else 3.0)


# ---------------------------------------------------------------------------
# HTTP handler  (local API for Claude Code hooks — called from hook scripts)
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

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

    def _session_from_id(self, session_id: str) -> Optional['_Session']:
        if not session_id:
            self._send_json(400, {'error': 'session_id required'})
            return None
        return _get_or_create_session(session_id)

    def _apply_context(self, session: '_Session', data: dict) -> None:
        """Store cwd and task_title on first encounter."""
        if data.get('cwd') and not session.cwd:
            session.cwd = data['cwd']
        if data.get('task_title') and not session.task_title:
            session.task_title = data['task_title']

    # --- GET ---

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == '/health':
            with _global_lock:
                n = len(_sessions)
            self._send_json(200, {'status': 'ok', 'sessions': n})

        elif parsed.path == '/inbox':
            session_id = (params.get('session_id') or [None])[0] or ''
            session = self._session_from_id(session_id)
            if not session:
                return
            with session.inbox_lock:
                messages = list(session.inbox)
                session.inbox.clear()
            self._send_json(200, {'messages': messages})

        else:
            self._send_json(404, {'error': 'not found'})

    # --- POST ---

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()

        if path == '/send':
            session = self._session_from_id(data.get('session_id', ''))
            if not session:
                return
            self._apply_context(session, data)
            try:
                _post_to_thread(session, data.get('text', ''), data.get('blocks'))
                self._send_json(200, {'ok': True})
            except Exception as e:
                self._send_json(500, {'error': str(e)})

        elif path == '/approval':
            session = self._session_from_id(data.get('session_id', ''))
            if not session:
                return
            self._apply_context(session, data)

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
                _post_to_thread(session, "Approval required", blocks)
            except Exception as e:
                self._send_json(500, {'error': f'failed to post approval card: {e}'})
                return

            session.approval_event.clear()
            session.approval_result    = None
            session.waiting_for_approval = True

            deadline = time.monotonic() + APPROVAL_TIMEOUT
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if session.approval_event.wait(timeout=min(2.0, remaining)):
                    break
                # Allow stop to interrupt a pending approval wait
                with session.inbox_lock:
                    if 'STOP' in session.inbox:
                        session.waiting_for_approval = False
                        self._send_json(200, {'decision': 'denied', 'reason': 'stop_requested'})
                        return

            session.waiting_for_approval = False
            decision  = session.approval_result or APPROVAL_DEFAULT
            timed_out = session.approval_result is None

            result: dict = {'decision': decision}
            if timed_out:
                result['reason'] = 'timeout'
            self._send_json(200, result)

        elif path == '/thread/reset':
            session = self._session_from_id(data.get('session_id', ''))
            if not session:
                return
            with _global_lock:
                if session.thread_ts:
                    _ts_to_session.pop(session.thread_ts, None)
                    session.thread_ts = None
                _saved_threads.pop(session.session_id, None)
            _save_threads()
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
    _load_saved_threads()
    _register()
    atexit.register(_unregister)
    threading.Thread(target=_poll_loop, daemon=True).start()
    print(f"[bridge] HTTP API on http://127.0.0.1:{BRIDGE_PORT}", flush=True)
    print("[bridge] Ready.", flush=True)
    try:
        HTTPServer(('127.0.0.1', BRIDGE_PORT), _Handler).serve_forever()
    except KeyboardInterrupt:
        pass
