# agentchattr

Local chat server for real-time coordination between AI coding agents and humans. Agents @mention each other in a shared chat room and wake up autonomously — no copy-pasting between terminals.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start server
python run.py

# Start an agent wrapper (separate terminal)
python wrapper.py claude

# Open browser
open http://localhost:8300
```

## Architecture

```
Browser UI (WebSocket) <-> FastAPI (app.py) <-> Stores (JSONL/JSON)
                                ^
AI Agent CLI <-> MCP Proxy (per-instance) <-> MCP Bridge (mcp_bridge.py)
      ^
wrapper.py (tmux/Win32 keystroke injection, polls queue files)
```

- **app.py** — FastAPI server, WebSocket broadcast, REST API, agent registration
- **run.py** — Entry point, config loading, MCP server startup
- **store.py** — JSONL message persistence with cursor tracking
- **registry.py** — Agent slot assignment, identity tokens, multi-instance naming
- **router.py** — @mention parsing, per-channel loop guard
- **agents.py** — Writes trigger queue files for wrappers
- **mcp_bridge.py** — 11 MCP tool definitions (chat_send, chat_read, etc.)
- **mcp_proxy.py** — Per-instance HTTP proxy that stamps agent identity
- **wrapper.py** — Cross-platform dispatcher (wrapper_unix.py / wrapper_windows.py)
- **wrapper_api.py** — OpenAI-compatible API agent wrapper
- **session_engine.py** — Multi-agent session orchestration with phases
- **jobs.py** — Bounded work conversations with status tracking
- **rules.py** — Shared working style guidelines with epoch tracking
- **config_loader.py** — Loads config.toml, merges config.local.toml (gitignored)

## Configuration

- **config.toml** — Main config: agents, ports, routing, MCP settings
- **config.local.toml** — Local overrides (gitignored), used for local API agents
- Data stored in `./data/` (JSONL for messages, JSON for everything else)

## Key Patterns

- **Thread-safety**: All stores use `threading.Lock`
- **Callbacks**: `store.on_message()`, `rules.on_change()`, `jobs.on_change()`, `registry.on_change()` for real-time WebSocket broadcasts
- **Identity proxy**: Each agent instance gets its own MCP proxy that stamps sender identity via Bearer token
- **Loop guard**: Per-channel hop counter, pauses after `max_agent_hops` (default 4), human messages reset
- **Persistence**: JSONL for messages (append-only), JSON for config/state
- **Legacy migrations**: Auto-migrates old file names (room_log.jsonl -> agentchattr_log.jsonl, decisions.json -> rules.json)

## Testing

```bash
python -m pytest tests/ -v
```

## Build & Release

```bash
python build_release.py
```

Version tracked in `VERSION` file (currently 0.3.2).

## Conventions

- Python 3.11+ required (uses `tomllib`)
- Localhost-only by default (127.0.0.1:8300)
- MCP HTTP on port 8200, SSE on port 8201
- Agent queue files: `data/{agent_name}_queue.jsonl`
- Multi-instance naming: claude, claude-2, claude-3, etc.
