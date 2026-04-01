# CLAUDE.md

## Project

Claude Code Slack Bridge — streams Claude Code session activity to Slack threads, gates risky operations behind approval buttons, and enables remote control (inject/stop) from Slack.

## Architecture

- **Central Router** (`slack_router.py`): single Socket Mode connection to Slack; per-channel event queues; HTTP API on `:8765`
- **Local Bridge** (`slack_bridge.py`): per-machine; manages per-session state; polls Router; exposes hook API on `localhost:9876`
- **Hooks** (`hooks/`): shell scripts invoked by Claude Code; communicate only with the local bridge
- One Slack thread = one Claude session (`session_id` from hook payload, stable across `claude --resume`)

See `docs/architecture.md` for the full system diagram and API reference.

## Development Workflow

1. **Requirements first**: before writing any code, analyse the request and identify ambiguities
2. **Ask proactively**: surface unclear requirements and tradeoffs as explicit questions; wait for answers before proceeding
3. **Design**: produce a written requirements summary and design plan; get confirmation
4. **Docs**: update or create documentation to reflect the design before or alongside code changes
5. **Code**: implement after design is confirmed
6. **Tests**: write and run tests where applicable after implementation

## Documentation Conventions

- All documentation is written in **English**
- Language style: clear and concise; avoid jargon and "big words"; prefer plain phrasing
- Use ASCII diagrams, tables, and lists to explain complex flows or comparisons
- New documentation files go in `docs/`; follow open-source naming conventions:
  - `getting-started.md` for setup guides
  - `architecture.md` for system design
  - `usage.md` for user-facing feature documentation
  - `CONTRIBUTING.md`, `CHANGELOG.md` at repo root when needed

## Key Commands

```bash
# Start the router (admin machine)
python3 slack_router.py

# Start a local bridge (each user machine)
python3 slack_bridge.py

# Generate a shared secret
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## Configuration

- `router.conf`: Slack tokens + `ROUTER_SECRET` — lives on the router machine only; never commit
- `bridge.conf`: `ROUTER_URL` + `ROUTER_SECRET` + `SLACK_CHANNEL_ID` — per user; never commit
- Both files are listed in `.gitignore`

## Code Conventions

- Python: standard library preferred; `slack_bolt` is used only in the router
- No external dependencies on user machines — the bridge uses `urllib` only
- Functions prefixed with `_` are internal; the public surface is the HTTP API
- Thread safety: `_global_lock` guards shared dicts; per-session locks guard session state
