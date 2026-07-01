# MemPalace Light Plugin

A lightweight Hermes Agent memory provider that persists conversation state to MemPalace via MCP over HTTP. Zero local dependencies — everything is remote.

## Quick Start

### Prerequisites

- **Hermes Agent** installed and running
- **MemPalace server** running on a remote LXC (or any host) at `http://<host>:<port>/mcp`
- Python 3.11+ with `requests` package (standard in Hermes environments)

### Installation

1. **Copy the plugin directory** into your Hermes plugins folder:

```bash
mkdir -p ~/.hermes/plugins/mempalace-light
cp -r /path/to/mempalace-light/* ~/.hermes/plugins/mempalace-light/
```

2. **Create the `.env` file** with your MCP credentials:

```bash
echo 'MCP_MEMPALACE_API_KEY=<your-api-key>' > ~/.hermes/.env
```

3. **Update `config.yaml`** to point to your MCP endpoint:

```yaml
memory:
  provider: mempalace-light

mcp_servers:
  mempalace:
    type: http
    url: http://<your-host>:8765/mcp
    headers:
      Authorization: Bearer ${MCP_MEMPALACE_API_KEY}
```

4. **Restart Hermes Agent** — the plugin loads automatically on startup.

## Architecture

The plugin implements the `MemoryProvider` ABC from Hermes Agent:

- **Communication:** HTTP JSON-RPC 2.0 via `tools/call` method
- **Authentication:** Bearer token via `Authorization` header
- **Data flow:** Sync writes conversation turns to MemPalace; prefetch reads diary entries for system prompt context
- **No local state:** All storage is on the remote MemPalace server (ChromaDB + SQLite)

### Lifecycle Hooks

| Hook | Purpose |
|------|---------|
| `prefetch(query)` | Reads recent diary entries → injects into system prompt as `RECENT_MEMORY:` blocks |
| `sync_turn(user, assistant)` | Files conversation turns to MemPalace with dedup and coalescing (5s window) |
| `on_session_end(messages)` | Writes session summary diary entry (background thread) |
| `on_pre_compress(messages)` | Searches palace for existing context → hints to compressor that data is safe to discard |
| `on_session_switch(new_id)` | Updates session tracking on `/reset`, `/branch`, `/resume` |
| `on_delegation(task, result)` | Records subagent delegation outcomes |

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_MEMPALACE_API_KEY` | — | API key for MemPalace auth |
| `MCP_MEMPALACE_URL` | `http://localhost:8765/mcp` | MCP server endpoint |
| `MCP_MEMPALACE_TIMEOUT` | `10` | Request timeout in seconds |

### Plugin Manifest (`plugin.yaml`)

```yaml
name: mempalace-light
description: MemPalace memory provider — MCP-based read/write
version: "1.0"
requires:
  - mempalace>=3.0
```

## Testing

Run the verification script to confirm all hooks are working:

```bash
python3 /path/to/mempalace-light-plugin/test_mempalace_light.py
```

Expected output:
```
1. Availability: True
2. Initialized. Session: test-session-001
3. Prompt block: [Memory: MemPalace active via MCP]
4. Prefetch length: NNNN
5. Search results: > 0 ✓
6. Diary entries: > 0 ✓
7. Tool schemas: ['mempalace_search', 'mempalace_add_drawer', 'mempalace_status']
8. Tool result: {"total_drawers": ...}
9. Shutdown complete
```

## Troubleshooting

### Plugin not loading
- Check `__init__.py` contains the string `MemoryProvider` (loader heuristic)
- Verify directory is at `~/.hermes/plugins/mempalace-light/`
- Restart Hermes Agent gateway

### Provider unavailable
- Check LXC is reachable: `curl http://<host>:8765/mcp`
- Verify API key in `~/.hermes/.env`
- Check `config.yaml` uses `headers:`, not `token:`

### No drawers appearing
- Verify `is_available()` returns `True`
- Wait 5+ seconds between sync calls (coalescing window)
- Check logs for `sync_turn failed:` or `MCP error:`

### Prefetch returns empty
- Verify diary has entries via `mempalace_diary_read`
- Check that `entries` list is populated in the response
- Ensure `content` key is used (not `entry`)

## Directory Structure

```
mempalace-light-plugin/
├── README.md                          # This file
├── plugin.yaml                        # Plugin manifest
├── __init__.py                        # Main plugin implementation (539 lines)
├── config/
│   └── example_config.yaml            # Example config.yaml snippet
├── docs/
│   └── architecture.md                # Exhaustive architecture reference
└── test_mempalace_light.py           # Verification test script
```

## More Documentation

See `docs/architecture.md` for the full technical reference — MCP protocol details, every hook's complete flow, debugging history, edge cases, and framework integration points.

## License

MIT License. Use freely.
