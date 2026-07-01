# MemPalace Light Plugin — Exhaustive Technical Reference

**Version:** 1.0
**File:** `/root/.hermes/plugins/mempalace-light/__init__.py` (539 lines, ~19.5KB)
**Config:** `/root/.hermes/plugins/mempalace-light/plugin.yaml`
**Remote Backend:** `http://192.168.0.118:8765/mcp` (MemPalace on LXC)
**Framework:** Hermes Agent (by Nous Research)

---

## 1. Architecture Overview

The mempalace-light plugin implements the `MemoryProvider` interface from Hermes Agent to persist conversation state to MemPalace — a remote semantic knowledge base running on an LXC container. It uses MCP (Model Context Protocol) over HTTP JSON-RPC 2.0 for all communication. No local ChromaDB, no local Python packages, no local storage. Everything goes through the remote LXC.

### Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Hermes Agent Framework                    │
│                                                              │
│  MemoryManager                                               │
│  ├── add_provider()  → registers MemPalaceLightProvider      │
│  ├── prefetch_all()  → calls provider.prefetch()             │
│  ├── sync_all()      → calls provider.sync_turn() (bg thread)│
│  ├── on_session_end()→ calls provider.on_session_end()       │
│  ├── on_pre_compress()→ calls provider.on_pre_compress()     │
│  ├── on_session_switch() → calls provider.on_session_switch()│
│  ├── on_delegation() → calls provider.on_delegation()        │
│  ├── on_memory_write() → calls provider.on_memory_write()    │
│  ├── on_turn_start() → calls provider.on_turn_start()        │
│  ├── get_all_tool_schemas() → returns provider tools          │
│  └── handle_tool_call() → routes to provider.handle_tool_call│
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  MemPalaceLightProvider                              │    │
│  │  ┌───────────────────────────────────────────────┐    │    │
│  │  │  MCPClient                                     │    │    │
│  │  │  ├── MCPConfig(url, api_key, timeout=10)       │    │    │
│  │  │  ├── requests.Session with auth header          │    │    │
│  │  │  ├── HTTPAdapter (Retry: 429,5xx)              │    │    │
│  │  │  └── call(method, params) → tools/call format  │    │    │
│  │  └───────────────────────────────────────────────┘    │    │
│  │                                                       │    │
│  │  Lifecycle Hooks:                                     │    │
│  │  ├── prefetch()       → diary_read (system prompt)    │    │
│  │  ├── sync_turn()      → mempalace_checkpoint          │    │
│  │  ├── on_session_end() → mempalace_diary_write (bg)    │    │
│  │  ├── on_pre_compress()→ mempalace_search              │    │
│  │  ├── on_memory_write()→ no-op                         │    │
│  │  ├── on_turn_start()  → no-op                         │    │
│  │  ├── on_session_switch() → update _session_id         │    │
│  │  ├── on_delegation()  → mempalace_checkpoint          │    │
│  │  └── on_pre_compress()→ mempalace_search              │    │
│  │                                                       │    │
│  │  Public API (tool schemas):                           │    │
│  │  ├── mempalace_search     → MCP search                │    │
│  │  ├── mempalace_add_drawer → MCP add                   │    │
│  │  └── mempalace_status     → MCP status                │    │
│  └──────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP JSON-RPC 2.0
                              │ POST http://192.168.0.118:8765/mcp
                              │ Authorization: Bearer <key>
                              │ Content-Type: application/json
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  MemPalace MCP Server (LXC)                  │
│                  http://192.168.0.118:8765/mcp               │
│                                                              │
│  Tools exposed via tools/call:                              │
│  ├── mempalace_checkpoint      — file drawers + diary entry │
│  ├── mempalace_diary_read      — read agent diary           │
│  ├── mempalace_diary_write     — write agent diary          │
│  ├── mempalace_search          — semantic search            │
│  ├── mempalace_check_duplicate — dedup check                │
│  ├── mempalace_status          — palace overview            │
│  ├── mempalace_list_wings      — list wings                 │
│  ├── mempalace_list_drawers    — list drawers               │
│  ├── mempalace_list_rooms      — list rooms in wing         │
│  └── mempalace_add_drawer      — add single drawer          │
│                                                              │
│  Storage: ChromaDB (on LXC), HNSW index, SQLite diary       │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow

```
User message → Agent processes → Assistant response
                                         │
                              MemoryManager.sync_all()
                                         │
                              ┌──────────┴──────────┐
                              │  ThreadPoolExecutor  │
                              │  (max_workers=1)     │
                              │  thread_name="mem-sync"│
                              └──────────┬──────────┘
                                         │
                              MemPalaceLightProvider.sync_turn()
                                         │
                    ┌────────────────────┼────────────────────┐
                    │                    │                     │
              Check _available   Coalescing check   _last_sync + 5s
                    │                    │
              _mcp.is_available()  Proceed or skip
                    │
              MCPClient.call("mempalace_status")
                    │
              POST /mcp { tools/call: mempalace_status }
                    │
              Parse response: content[0].text → JSON
                    │
              Return {"error" in result} → bool
                                         │
                              ┌──────────┴──────────┐
                              │   Duplicate Check    │
                              │   _content_hash()    │
                              │   SHA256[:16]        │
                              └──────────┬──────────┘
                                         │
                              MCPClient.call("mempalace_check_duplicate")
                                         │
                              POST /mcp { tools/call: mempalace_check_duplicate }
                                         │
                              result["is_duplicate"] → bool
                                         │
                              ┌──────────┴──────────┐
                              │   File Drawer        │
                              │   mempalace_checkpoint│
                              └──────────┬──────────┘
                                         │
                              POST /mcp { tools/call: mempalace_checkpoint }
                                         │
                              wing="wing_gay-mike"
                              room="compressed-context"
                              content="USER: ...\nASSISTANT: ..."
                                         │
                              → ChromaDB on LXC (HNSW index updated)
```

---

## 2. Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_MEMPALACE_API_KEY` | (none, read from `.env`) | API key for LXC authentication |
| `MCP_MEMPALACE_URL` | `http://192.168.0.118:8765/mcp` | MCP server endpoint |
| `MCP_MEMPALACE_TIMEOUT` | `10` | Request timeout in seconds |

### .env File

Location: `~/.hermes/.env`

```bash
MCP_MEMPALACE_API_KEY=<your-api-key>
```

The plugin reads this file at startup via `get_mcp_config()`. It parses line-by-line, skips comments (`#`) and empty lines, splits on first `=`, and looks for `MCP_MEMPALACE_API_KEY`. Environment variables take precedence over `.env` values.

### config.yaml

The MCP connection details are also stored in `/root/.hermes/config.yaml`:

```yaml
mcp_servers:
  mempalace:
    type: http
    url: http://192.168.0.118:8765/mcp
    headers:
      Authorization: Bearer ${MCP_...KEY}
```

**CRITICAL:** Never use `token:` in config.yaml — it's silently ignored by the runtime. Always use `headers:` with `Authorization: Bearer ${VARIABLE}`.

### plugin.yaml

Location: `/root/.hermes/plugins/mempalace-light/plugin.yaml`

```yaml
name: mempalace-light
description: MemPalace — lean read-only memory provider. Reminder + prefetch only. Uses the same palace as the full mempalace plugin. MCP handles writes.
version: "1.0"
requires:
  - mempalace>=3.0
```

This is the plugin manifest. The `name` field is used by the loader for identification. The `requires` field declares dependencies (currently `mempalace>=3.0` but the plugin doesn't actually import the Python package — it uses HTTP-only).

---

## 3. MCP JSON-RPC Protocol

### Request Format

ALL MCP calls MUST use the `tools/call` method. Direct method calls fail.

```json
{
  "jsonrpc": "2.0",
  "id": <timestamp_in_ms>,
  "method": "tools/call",
  "params": {
    "name": "<tool_name>",
    "arguments": { ... }
  }
}
```

**Example — mempalace_status:**
```json
{
  "jsonrpc": "2.0",
  "id": 1751350000000,
  "method": "tools/call",
  "params": {
    "name": "mempalace_status",
    "arguments": {}
  }
}
```

**Example — mempalace_checkpoint:**
```json
{
  "jsonrpc": "2.0",
  "id": 1751350000001,
  "method": "tools/call",
  "params": {
    "name": "mempalace_checkpoint",
    "arguments": {
      "items": [
        {
          "wing": "wing_gay-mike",
          "room": "compressed-context",
          "content": "USER: hello\nASSISTANT: hi there"
        }
      ]
    }
  }
}
```

### Response Format

The MCP server wraps all tool responses:

```json
{
  "jsonrpc": "2.0",
  "id": 1751350000000,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"total_drawers\": 13, \"wings\": [...], \"error\": null}"
      }
    ]
  }
}
```

The plugin's `MCPClient.call()` method:
1. POSTs the payload to `self.config.url` with `Content-Type: application/json`
2. `requests.post()` with timeout
3. Parses JSON response
4. Extracts text from `result["content"][i]["text"]` where `type == "text"`
5. `json.loads()` the inner text string to get a Python dict
6. Returns the dict, or `{"error": "..."}` on any failure

### Error Handling

The client catches these exception types:
- `requests.exceptions.Timeout` → `{"error": "timeout (<N>s)"}`
- `requests.exceptions.ConnectionError` → `{"error": "connection refused"}`
- Any other `Exception` → `{"error": "<str(e)>"}"`
- JSON-RPC error field → `{"error": "<error string from server>"}`
- HTTP non-2xx → raised by `resp.raise_for_status()`, caught by generic handler

### Retry Logic

`HTTPAdapter` with `Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])`.

Retries only on:
- **429** (Too Many Requests)
- **500** (Internal Server Error)
- **502** (Bad Gateway)
- **503** (Service Unavailable)
- **504** (Gateway Timeout)

Max 2 retries (total of 3 attempts) with exponential backoff starting at 0.5s.

### Session Object

`MCPClient` creates a `requests.Session` in `__init__` that persists across calls. Headers are set once:

```python
self._session.headers.update({
    "Content-Type": "application/json",
    "Authorization": f"Bearer {self.config.api_key}",
})
```

This means the auth header is sent with every request automatically.

---

## 4. MemoryProvider Interface (ABC)

The plugin implements the `MemoryProvider` abstract base class from `agent/memory_provider.py`.

### Mandatory Methods (abstractmethod)

| Method | Signature | Purpose |
|--------|-----------|---------|
| `name` | `@property` → `str` | Short identifier. Returns `"mempalace-light"` |
| `is_available()` | `→ bool` | Check if MCP server is reachable |
| `initialize()` | `(session_id: str, **kwargs) → None` | Initialize for a session. Sets `_session_id`, `_hermes_home`, `_available` |

### Core Methods (overridable, called by framework)

| Method | Signature | Returns | Called By |
|--------|-----------|---------|-----------|
| `system_prompt_block()` | `() → str` | `"[Memory: MemPalace active via MCP]"` | `MemoryManager.build_system_prompt()` |
| `prefetch()` | `(query: str, *, session_id: str = "") → str` | Text for system prompt, or `""` | `MemoryManager.prefetch_all()` |
| `sync_turn()` | `(user_content, assistant_content, *, session_id="", messages=None) → None` | — | `MemoryManager.sync_all()` (background thread) |
| `get_tool_schemas()` | `() → List[Dict]` | OpenAI-format tool schemas | `MemoryManager.get_all_tool_schemas()` |
| `handle_tool_call()` | `(tool_name, args, **kwargs) → str` | JSON string result | `MemoryManager.handle_tool_call()` |

### Optional Hooks (default no-op)

| Method | Signature | Called By |
|--------|-----------|-----------|
| `queue_prefetch()` | `(query: str, *, session_id: str = "") → None` | `MemoryManager.queue_prefetch_all()` |
| `on_turn_start()` | `(turn_number: int, message: str, **kwargs) → None` | `MemoryManager.on_turn_start()` |
| `on_session_end()` | `(messages: List[Dict]) → None` | `MemoryManager.on_session_end()` |
| `on_session_switch()` | `(new_session_id, *, parent_session_id="", reset=False, rewound=False, **kwargs) → None` | `MemoryManager.on_session_switch()` |
| `on_pre_compress()` | `(messages: List[Dict]) → str` | `MemoryManager.on_pre_compress()` |
| `on_memory_write()` | `(action, target, content, metadata=None) → None` | `MemoryManager.on_memory_write()` |
| `on_delegation()` | `(task, result, *, child_session_id="", **kwargs) → None` | `MemoryManager.on_delegation()` |
| `shutdown()` | `() → None` | `MemoryManager.shutdown_all()` |
| `backup_paths()` | `() → List[str]` | `hermes backup` |
| `get_config_schema()` | `() → List[Dict]` | `hermes memory setup` |
| `save_config()` | `(values, hermes_home) → None` | `hermes memory setup` |

### Plugin Loader Heuristic

The Hermes Agent plugin loader at `/usr/local/lib/hermes-agent/plugins/memory/__init__.py` uses a function `_is_memory_provider_dir(path)` to discover memory plugins. It scans `__init__.py` for the presence of either:
- The string `"MemoryProvider"`
- The string `"register_memory_provider"`

If NEITHER string is found, the directory is silently skipped — the plugin never loads.

**Resolution:** Added the comment `# Implements the MemoryProvider ABC interface (register(ctx) pattern)` as the first line of the class docstring in `__init__.py`. This ensures the string `"MemoryProvider"` is present in `__init__.py` content.

### Framework Inspection for Signature Compatibility

The `MemoryManager` uses `inspect.signature()` to check method compatibility:

- **`sync_turn` messages parameter:** `_provider_sync_accepts_messages()` checks if `sync_turn` has a `messages` parameter or `**kwargs`. If yes, passes `messages`; otherwise omits it.
- **`on_memory_write` metadata mode:** `_provider_memory_write_metadata_mode()` checks if `metadata` is a named parameter or if there are `**kwargs` (keyword mode), 4+ positional-accepting params (positional mode), or fewer params (legacy mode).

Our plugin uses keyword arguments throughout (`*, session_id=`, `**kwargs`), so it always uses keyword mode — clean and safe.

---

## 5. Lifecycle Hook Deep Dive

### 5.1 `initialize(session_id, **kwargs) → None`

**When called:** Once at agent startup, via `MemoryManager.initialize_all()`.

**What it does:**
1. Stores `session_id` in `self._session_id`
2. Extracts `hermes_home` from `kwargs` (defaults to `~/.hermes`)
3. Runs `_mcp.is_available()` to check LXC connectivity
4. Stores result in `self._available`

**kwargs passed by framework:**
- `hermes_home` — active HERMES_HOME path
- `platform` — "cli", "telegram", "discord", "cron"
- `agent_context` — "primary", "subagent", "cron", "flush"
- `agent_identity` — profile name (e.g. "coder")
- `agent_workspace` — shared workspace name
- `parent_session_id` — parent session ID for subagents
- `user_id` — platform user identifier
- `user_id_alt` — alternate stable user identifier

**Framework note:** `initialize_all()` auto-injects `hermes_home` if missing.

### 5.2 `system_prompt_block() → str`

**When called:** During system prompt assembly, via `MemoryManager.build_system_prompt()`.

**What it returns:**
- If available: `"[Memory: MemPalace active via MCP]"`
- If not available: `""` (empty string, skipped)

This block is combined with other providers' blocks (e.g., built-in memory) and injected into the system prompt.

### 5.3 `prefetch(query, *, session_id="") → str`

**When called:** Before each API call, via `MemoryManager.prefetch_all()`. Runs synchronously (must be fast).

**What it does:**
1. Checks availability — returns `""` if unavailable
2. Calls `mempalace_diary_read` for agent `"gay-mike"`, `last_n=5`
3. Parses response — `result["entries"]` is a list of diary entries
4. Takes the 3 most recent entries
5. Extracts `entry["content"]` (NOT `entry["entry"]`)
6. Prefixes each with `RECENT_MEMORY: ` and truncates to 500 chars
7. Returns joined lines or `""`

**Critical field:** The diary API returns entries with a `content` key. The old code used `entry.get("entry", "")` which was always empty. Fixed by changing to `entry.get("content", "")`.

**Output format:**
```
RECENT_MEMORY: SESSION:2026-07-01|topic|req|★★★

Session detail...
RECENT_MEMORY: SESSION:2026-06-30|another_topic|req|★★★

More detail...
```

**Framework wrapping:** `MemoryManager.prefetch_all()` wraps the result in a `<memory-context>` fence with a system note:
```
<memory-context>
[System note: The following is recalled memory context, NOT new user input. Treat as authoritative reference data — this is the agent's persistent memory and should inform all responses.]

<provider output>
</memory-context>
```

The framework also has a `StreamingContextScrubber` that handles chunked streaming output to properly strip memory-context spans even across delta boundaries.

### 5.4 `sync_turn(user_content, assistant_content, *, session_id="", messages=None) → None`

**When called:** After every assistant response, via `MemoryManager.sync_all()`. Runs in a background thread (single-worker executor).

**Full flow:**
1. Check `self._available` — return immediately if not available
2. **Coalescing check:** If `now - self._last_sync < 5` seconds, skip. Prevents flooding the LXC with rapid successive writes.
3. Update `_last_sync = now`
4. If `session_id` is provided, update `self._session_id`
5. Build `turn_text = f"USER: {user_content}\nASSISTANT: {assistant_content}"`
6. **Duplicate check:** Call `mempalace_check_duplicate(content=turn_text, threshold=0.9)`
   - Response: `{"is_duplicate": bool, "matches": [...]}`
   - If duplicate, skip (log debug, return)
7. Call `mempalace_checkpoint(items=[{wing, room, content}])`
   - `wing = "wing_gay-mike"`
   - `room = "compressed-context"`
   - `content = turn_text`
8. Log success/error

**Coalescing rationale:** The framework runs `sync_all()` on a single worker thread. But the coalescing check is at the provider level because the framework may call multiple providers and we want to avoid duplicate checkpoint filings for the same turn.

**Duplicate check detail:**
- `_content_hash(content)` generates `SHA256(content)[:16]` — used for local dedup
- `_check_duplicate(client, content, wing, room)` calls the MCP tool `mempalace_check_duplicate`
- The MCP tool returns `{"is_duplicate": bool}` — NOT `{"found": bool}` (common mistake)
- The plugin was previously returning the raw dict instead of a bool — fixed by `return result.get("is_duplicate", False)`

**Background execution:** The framework wraps `sync_all()` in `_submit_background()`, which uses a `ThreadPoolExecutor(max_workers=1)` named `"mem-sync"`. This ensures turn N lands before turn N+1 (serialization). The sync drain timeout is 5 seconds (`_SYNC_DRAIN_TIMEOUT_S`).

### 5.5 `on_session_end(messages) → None`

**When called:** At actual session boundaries — CLI exit, `/reset`, `/new`, gateway session expiry. NOT on every turn.

**Implementation:** Spawns a daemon thread (`_on_session_end_worker`) to avoid blocking the session exit path.

**What the worker does:**
1. Check availability
2. Build session summary via `_build_session_summary(messages)`
3. Call `mempalace_diary_write(agent_name="gay-mike", entry=summary, topic="session_end", wing="wing_gay-mike")`
4. Log success/error

**Summary format:**
```
SESSION:2026-07-01|session_end|ALC.req:session.summary|★★★

Session completed: 7 turns, 14 messages.
Context: <first user message, first 300 chars>
```

The `_build_session_summary` method:
- Extracts user and assistant messages from the `messages` list
- Takes the first user message content (first 300 chars) as context
- Counts `min(len(user_msgs), len(assistant_msgs))` as turn count
- Constructs AAAK-formatted summary

### 5.6 `on_pre_compress(messages) → str`

**When called:** Before context compression discards old messages.

**What it does:**
1. Check availability — return `""` if unavailable
2. Call `mempalace_search(query="session context conversation", wing="wing_gay-mike", room="compressed-context", limit=5)`
3. Check for `result["results"]`
4. If results exist, return `"Existing conversation context is stored in MemPalace."`
5. Return `""` otherwise

**Purpose:** Return text that the framework injects into the compression summary prompt. This tells the context compressor that conversation context has already been preserved in the palace and can be safely compressed out of the active context window.

### 5.7 `on_session_switch(new_session_id, *, parent_session_id="", reset=False, rewound=False, **kwargs) → None`

**When called:** On `/resume`, `/branch`, `/reset`, `/new` (CLI), gateway equivalents, and context compression — any path that reassigns `AIAgent.session_id` without tearing the provider down.

**What it does:**
1. Update `self._session_id = new_session_id`
2. If `reset=True`, clear `self._turn_buffer` and reset `self._last_sync = 0`

**Parameters:**
- `parent_session_id` — previous session ID (set for `/branch` fork lineage, compression continuation lineage, `/resume`)
- `reset` — `True` for genuinely new conversation (`/reset`/`/new`), `False` for continuation (`/resume`/`/branch`/compression)
- `rewound` — `True` if session_id unchanged but transcript truncated (from `/undo` path)

**Why this matters:** Without this hook, after a `/reset`, the provider would continue filing drawers under the old `_session_id`, potentially confusing the palace's semantic indexing with mixed-session content.

### 5.8 `on_delegation(task, result, *, child_session_id="", **kwargs) → None`

**When called:** On the parent agent when a subagent completes. The parent's memory provider gets the task+result pair as an observation.

**What it does:**
1. Check availability — return if unavailable
2. Call `mempalace_checkpoint(items=[{wing, room, content}])`
   - `wing = "wing_gay-mike"`
   - `room = "delegations"`
   - `content = f"TASK: {task}\nRESULT: {result}"`

**Purpose:** Records what was delegated to subagents and what they returned, so the palace retains a record of delegation outcomes.

### 5.9 `on_memory_write(action, target, content, metadata=None) → None`

**When called:** When the built-in `memory` tool writes an entry. Mirrors the write to the palace.

**Implementation:** No-op (`pass`). All writes are handled via MCP tools directly.

**Why no-op:** The built-in memory tool writes to a local SQLite store. Mirroring to the palace would create redundancy and potential inconsistency. The palace is treated as a separate, independent knowledge base.

### 5.10 `on_turn_start(turn_number, message, **kwargs) → None`

**When called:** At the start of each turn with the user message.

**What it does:** No-op (`pass`). Could be used for turn-counting, scope management, or periodic maintenance.

**kwargs may include:** `remaining_tokens`, `model`, `platform`, `tool_count`.

### 5.11 `shutdown() → None`

**When called:** During agent shutdown, in reverse order of registration, via `MemoryManager.shutdown_all()`.

**What it does:** Sets `self._mcp = None` to release the `requests.Session` object.

**Framework coordination:** `shutdown_all()` first calls `_drain_sync_executor()` which:
1. Sets `_sync_executor = None` (stops accepting new work)
2. Cancels queued tasks (`cancel_futures=True`)
3. Waits up to 5 seconds for in-flight tasks on a daemon watcher thread

### 5.12 `backup_paths() → List[str]`

**When called:** By `hermes backup` to discover external storage paths.

**Returns:** `[]` (empty list). The plugin stores all state remotely on the LXC, so no local paths need backing up.

### 5.13 `get_config_schema() → List[Dict]` and `save_config()`

**Not implemented.** Default no-op from the ABC. The plugin reads config from environment variables and `.env` file directly, so it doesn't need the `hermes memory setup` interactive flow.

---

## 6. Public API (Tool Schemas)

The provider exposes 3 tools via `get_tool_schemas()`, each implemented in `handle_tool_call()`:

### 6.1 `mempalace_search`

**Schema:**
```json
{
  "name": "mempalace_search",
  "description": "Search the MemPalace for relevant context",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "Search query"},
      "limit": {"type": "integer", "description": "Max results", "default": 5},
      "wing": {"type": "string", "description": "Filter by wing"},
      "room": {"type": "string", "description": "Filter by room"}
    },
    "required": ["query"]
  }
}
```

**Implementation:** Delegates to `self._mcp.call("mempalace_search", args)` → parses JSON → returns dict as JSON string.

### 6.2 `mempalace_add_drawer`

**Schema:**
```json
{
  "name": "mempalace_add_drawer",
  "description": "Add a drawer to the MemPalace",
  "parameters": {
    "type": "object",
    "properties": {
      "wing": {"type": "string", "description": "Wing (project name)"},
      "room": {"type": "string", "description": "Room (topic)"},
      "content": {"type": "string", "description": "Content to store"}
    },
    "required": ["wing", "room", "content"]
  }
}
```

**Implementation:** Delegates to `self._mcp.call("mempalace_add_drawer", args)`.

### 6.3 `mempalace_status`

**Schema:**
```json
{
  "name": "mempalace_status",
  "description": "Get MemPalace status overview",
  "parameters": {
    "type": "object",
    "properties": {}
  }
}
```

**Implementation:** Delegates to `self._mcp.call("mempalace_status", {})`.

### Tool Routing

`MemoryManager.handle_tool_call()` routes by looking up `self._tool_to_provider[tool_name]`. This mapping is built during `add_provider()` by iterating over `provider.get_tool_schemas()` and extracting the `name` field. Reserved core tool names (from `toolsets._HERMES_CORE_TOOLS` including `clarify`, `delegate_task`, etc.) are filtered out — they can never shadow built-in tools.

---

## 7. Plugin Entry Point

### `register(ctx) → None`

**Location:** Last 3 lines of `__init__.py`

```python
def register(ctx) -> None:
    """Register MemPalace Light as a memory provider."""
    ctx.register_memory_provider(MemPalaceLightProvider())
```

The `ctx` object is the plugin context provided by the Hermes Agent loader. It has a `register_memory_provider(provider)` method that calls `MemoryManager.add_provider(provider)` internally.

### Plugin Discovery Flow

1. Framework scans `~/.hermes/plugins/memory/` for directories
2. For each directory, calls `_is_memory_provider_dir(path)`
3. This function reads `__init__.py` and checks for `"MemoryProvider"` or `"register_memory_provider"` strings
4. If found, it imports the directory's `__init__.py` as a module
5. Looks for a `register(ctx)` function
6. Calls `register(ctx)`
7. `ctx.register_memory_provider()` adds the provider to the MemoryManager

### Plugin State Management

```python
def __init__(self, mcp_client: Optional[MCPClient] = None):
    self._mcp = mcp_client or MCPClient()  # Creates MCPClient with config
    self._palace_url = self._mcp.config.url  # Stores URL for reference
    self._available = False  # Will be set in initialize()
    self._last_sync = 0  # Coalescing timestamp
    self._sync_interval = 5  # Seconds between syncs
    self._turn_buffer: List[Tuple[str, str]] = []  # Unused buffer (planned feature)
    self._turn_buffer_lock = threading.Lock()  # For buffer synchronization
    self._session_id = ""  # Current session
    self._hermes_home = ""  # Profile home directory
```

---

## 8. SHA256 Idempotency

### `_content_hash(content: str) → str`

```python
def _content_hash(content: str) -> str:
    """Generate SHA256 hash for deduplication."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
```

Generates a 16-character hex hash from the content. Used for local dedup tracking (though the current implementation doesn't actively use the hash — it relies on the MCP `mempalace_check_duplicate` tool for the actual dedup check).

### `_check_duplicate(client, content, wing, room, threshold=0.9) → bool`

```python
def _check_duplicate(client: MCPClient, content: str, wing: str, room: str, threshold: float = 0.9) -> bool:
    """Check if content already exists in the palace.
    
    Returns True if duplicate found, False if new.
    """
    result = client.call(
        "mempalace_check_duplicate",
        {"content": content, "threshold": threshold},
    )
    if "error" in result:
        return False  # Don't block on error
    return result.get("is_duplicate", False)
```

Calls `mempalace_check_duplicate` with the content and a similarity threshold of 0.9 (90% match required). Returns `True` if a duplicate exists, `False` otherwise. On error, returns `False` (fail-open: allow the write rather than block it).

---

## 9. Error Handling & Resilience

### Provider-Level Failures

Every lifecycle hook wraps its logic in try/except or checks for `if "error" in result:` patterns. Failures are logged at `logger.error` or `logger.warning` level but never re-raised (except in `handle_tool_call` which wraps errors in a JSON string).

### Framework-Level Resilience

The `MemoryManager` wraps all provider calls in try/except:
- `prefetch_all`: catches exceptions, logs debug, continues to next provider
- `sync_all`: catches exceptions, logs warning, continues to next provider
- `on_session_end`: catches exceptions, logs warning with `exc_info=True`, continues
- `on_pre_compress`: catches exceptions, logs debug, returns empty string
- `on_session_switch`: catches exceptions, logs debug, continues
- `on_delegation`: catches exceptions, logs debug, continues
- `on_memory_write`: catches exceptions, logs debug, continues
- `get_all_tool_schemas`: catches exceptions, logs warning, skips that provider
- `build_system_prompt`: catches exceptions, logs warning, skips that provider

### Background Thread Safety

`on_session_end` spawns a daemon thread. The daemon flag means the thread will be killed when the main process exits — no graceful shutdown for that thread. This is acceptable because the worst case is a lost session-end diary entry.

`sync_all` uses the framework's `ThreadPoolExecutor(max_workers=1)` which runs on a proper worker thread (not daemon). This thread is drained via `_drain_sync_executor()` with a 5-second timeout at shutdown.

### Network Failure Modes

| Failure | Response | Impact |
|---------|----------|--------|
| LXC unreachable | `"error": "connection refused"` | Provider unavailable, all writes skipped |
| LXC timeout (>10s) | `"error": "timeout (10s)"` | Write fails silently (logged), turn continues |
| Auth failure (401) | Caught by generic handler | `"error": "401 Client Error"` |
| LXC returns 5xx | Retried up to 3 times, then logged | Write fails silently |
| LXC returns 429 | Retried up to 3 times with backoff | Write may succeed on retry |
| JSON parse error | Caught by generic handler | `"error": "<str(e)>"` |
| Empty response | Returns `{}` | No data written, no error |

---

## 10. File Structure

```
/root/.hermes/plugins/mempalace-light/
├── __init__.py           # Main plugin (539 lines, ~19.5KB)
│   ├── MCPConfig         # Dataclass: url, api_key, timeout
│   ├── get_mcp_config()  # Load from .env or env vars
│   ├── MCPClient          # JSON-RPC HTTP client (Session, retry, call)
│   │   ├── call()         # POST /mcp, tools/call format, parse response
│   │   └── is_available() # POST mempalace_status, check for error
│   ├── _content_hash()    # SHA256[:16] for idempotency
│   ├── _check_duplicate() # MCP check_duplicate, returns bool
│   ├── MemPalaceLightProvider  # MemoryProvider implementation
│   │   ├── name = "mempalace-light"  # Property
│   │   ├── __init__()    # State initialization
│   │   ├── initialize()  # Session setup, availability check
│   │   ├── is_available()# Cached + fresh check
│   │   ├── system_prompt_block()     # "[Memory: MemPalace active via MCP]"
│   │   ├── prefetch()     # diary_read → RECENT_MEMORY: lines
│   │   ├── queue_prefetch()# No-op
│   │   ├── sync_turn()    # checkpoint filing with coalescing
│   │   ├── on_session_end()# Background diary_write worker
│   │   ├── on_pre_compress()# mempalace_search → preservation hint
│   │   ├── on_session_switch()# Update session_id, reset buffers
│   │   ├── on_delegation()# checkpoint filing for subagent results
│   │   ├── on_memory_write()# No-op
│   │   ├── on_turn_start()# No-op
│   │   ├── get_tool_schemas()# 3 tools: search, add_drawer, status
│   │   ├── handle_tool_call()# Route to appropriate MCP tool
│   │   ├── shutdown()     # Release MCP client
│   │   ├── backup_paths()# Return []
│   │   ├── search()       # Direct MCP search call
│   │   ├── status()       # Direct MCP status call
│   │   ├── list_wings()   # Direct MCP list_wings call
│   │   ├── add_drawer()   # Direct MCP add_drawer call
│   │   ├── _build_session_summary()# AAAK format summary
│   │   └── _on_session_end_worker()# Background thread target
│   └── register()         # Plugin entry point
├── plugin.yaml            # Plugin manifest (5 lines)
├── __pycache__/
│   ├── __init__.cpython-313.pyc  # Python 3.13 bytecode
│   └── __init__.cpython-311.pyc  # Python 3.11 bytecode
```

---

## 11. Historical Debugging Log

### Issue 1: 401 Unauthorized on all MCP calls

**Symptom:** Every `MCPClient.call()` returned `{"error": "401 Client Error"}`.

**Root cause:** No authentication header was being sent. The `MCPConfig` class had `api_key` but `get_mcp_config()` was reading from a non-existent `.env` file, so the key was empty.

**Fix:** Parsed `~/.hermes/.env` line-by-line, extracted `MCP_MEMPALACE_API_KEY`. The key is also available in `config.yaml` under `mcp_servers.mempalace.headers.Authorization`.

### Issue 2: Invalid tool schema / method not found

**Symptom:** MCP server returned errors about unrecognized tool names.

**Root cause:** The client was using direct method calls (`"method": "mempalace_status"`) instead of the `tools/call` format.

**Fix:** Changed `MCPClient.call()` to use:
```json
{"jsonrpc": "2.0", "id": ..., "method": "tools/call", "params": {"name": "<tool>", "arguments": {...}}}
```

### Issue 3: Empty responses from MCP tools

**Symptom:** `MCPClient.call()` returned `{}` for valid tool calls.

**Root cause:** The response parser was looking for `result.get("result", {}).get("content")` but the actual response structure wraps content in `content[0]["text"]` as a JSON string that needs parsing.

**Fix:** Added loop through `content` items to find `type == "text"`, then `json.loads(item["text"])` to get the actual result dict.

### Issue 4: Plugin not loading (silent failure)

**Symptom:** Gateway restart showed no error, but `sync_turn()` was never called. No drawers appeared in the palace.

**Root cause:** The plugin loader's `_is_memory_provider_dir()` heuristic checks for `"MemoryProvider"` or `"register_memory_provider"` in the `__init__.py` file content. Our docstring didn't contain either string.

**Fix:** Added the comment `# Implements the MemoryProvider ABC interface (register(ctx) pattern)` to the class docstring, ensuring `"MemoryProvider"` is present.

### Issue 5: Hook signature mismatches

**Symptom:** Framework logged `sync_turn failed: ...` warnings. Methods were called with wrong argument counts.

**Root cause:** The framework calls hooks with specific signatures that differ from the ABC defaults. `sync_turn` is called as `sync_turn(user_content, assistant_content, session_id=..., messages=...)` — keyword-only `session_id` and `messages`. The plugin's `prefetch` was called as `prefetch(query, session_id=...)` not `prefetch(query, *, session_id=...)`.

**Fix:** Corrected all hook signatures:
- `prefetch(self, query: str, *, session_id: str = "")` — added `*` for keyword-only
- `sync_turn(self, user_content, assistant_content, *, session_id="", messages=None)` — keyword-only params
- `on_session_end(self, messages)` — single positional, no session_id
- `on_pre_compress(self, messages)` — single positional, returns str
- `on_turn_start(self, turn_number: int, message: str, **kwargs)` — matches ABC
- `on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kwargs)` — full signature
- `on_delegation(self, task, result, *, child_session_id="", **kwargs)` — keyword-only child_session_id
- `on_memory_write(self, action, target, content, metadata=None)` — positional args

### Issue 6: `prefetch()` returns empty string

**Symptom:** `prefetch()` always returned `""` even though diary entries existed.

**Root cause:** The diary API returns entries with a `content` key, but the code was reading `entry.get("entry", "")` which is always `None`/default.

**Fix:** Changed to `entry.get("content", "")`.

### Issue 7: Duplicate check returning wrong type

**Symptom:** `sync_turn()` always wrote drawers even for identical content.

**Root cause:** `_check_duplicate()` returned the raw dict from `client.call()` instead of a boolean. The `if _check_duplicate(...)` check would always be truthy (non-empty dict).

**Fix:** Changed to `return result.get("is_duplicate", False)` — note the key is `is_duplicate` not `found`.

### Issue 8: `on_session_end` blocking the exit path

**Symptom:** Agent took several seconds to exit after a long conversation.

**Root cause:** `on_session_end()` called the MCP write synchronously in the main thread.

**Fix:** Spawns a daemon thread (`_on_session_end_worker`) so the write happens in the background. The main thread returns immediately.

### Issue 9: Missing abstract methods

**Symptom:** `MemoryProvider` ABC wouldn't instantiate — `TypeError: Can't instantiate abstract class MemPalaceLightProvider with abstract methods ...`

**Root cause:** The ABC requires `initialize()`, `get_tool_schemas()`, `handle_tool_call()`, `shutdown()`, `backup_paths()` as abstract methods. Our initial implementation was missing `shutdown()` and `backup_paths()`.

**Fix:** Added stub implementations for all missing abstract methods.

---

## 12. Edge Cases & Gotchas

### 12.1 MCP Method Format
- **Wrong:** `"method": "mempalace_status"`
- **Right:** `"method": "tools/call"`, `params: {name: "mempalace_status", arguments: {}}`

### 12.2 Response Parsing
MCP returns `content` as an array of `{"type": "text", "text": "..."}` objects. Must extract the `text` field and `json.loads()` it. The `result` dict itself doesn't contain the parsed data.

### 12.3 Diary API Field Names
- `mempalace_diary_read` returns entries with `content` key
- `mempalace_diary_write` accepts `entry` parameter
- Don't mix these up!

### 12.4 Duplicate Check
- `mempalace_check_duplicate` returns `{"is_duplicate": bool, "matches": [...]}`
- Key is `is_duplicate`, NOT `found`
- On error, returns `False` (fail-open)

### 12.5 Availability Check
- Use `mempalace_status` for availability
- `tools/list` doesn't exist as a tool (it's an internal MCP method)

### 12.6 Coalescing
- `sync_turn` has 5-second coalescing to avoid flooding the LXC
- Rapid consecutive calls within 5 seconds of the last sync are skipped
- Test hooks with `time.sleep(6)` between calls

### 12.7 Background Threads
- `on_session_end` runs in a background daemon thread
- Use `time.sleep()` to wait for completion in tests
- The framework's `flush_pending(timeout)` can wait for all sync work to drain

### 12.8 Session ID Rotation
- `/reset`, `/new`, `/branch`, `/resume`, compression all change `session_id`
- `on_session_switch` must update `_session_id` or writes land in the wrong session
- `reset=True` means flush buffers; `reset=False` means continue under new ID

### 12.9 Plugin Loader Heuristic
- Must contain `"MemoryProvider"` or `"register_memory_provider"` in `__init__.py`
- Adding these strings to docstrings/comments works (the loader does a string search)
- Directory rename doesn't matter — only file content matters

### 12.10 Framework Executor Drain
- `_SYNC_DRAIN_TIMEOUT_S = 5.0` — background syncs have 5 seconds to complete
- Anything still running after 5s is abandoned (daemon thread dies)
- `flush_pending(timeout)` waits for the executor to drain a barrier task

### 12.11 Tool Schema Normalization
- The framework normalizes tool schemas via `normalize_tool_schema()`
- If a schema is already wrapped as `{"type": "function", "function": {...}}`, it unwraps it
- If the normalized schema has no `name` field, it's silently skipped
- A single bad schema could disable the entire toolset — the framework prevents this by skipping

### 12.12 Core Tool Name Reservation
- Core tools (`clarify`, `delegate_task`, `execute_code`, `terminal`, etc.) are reserved
- Memory provider tools can never shadow them
- Conflicts are silently dropped during registration

### 12.13 Skill Scaffolding Stripping
- `MemoryManager._strip_skill_scaffolding()` extracts user instructions from `/skill` and `/bundle` invocations
- Prevents polluting memory stores with prompt scaffolding
- If the stripped query is empty, the entire turn is skipped for prefetch/sync

### 12.14 Context Compression Fencing
- Prefetch output is wrapped in `<memory-context>...</memory-context>` tags
- A `StreamingContextScrubber` handles chunked streaming to strip partial spans
- The system note inside the fence tells the model to treat it as authoritative reference data

---

## 13. Testing Hooks

### Standalone Test Script

```python
import sys, os, time
sys.path.insert(0, "/root/.hermes/plugins")

# Clean slate
for key in list(sys.modules.keys()):
    if 'mempalace' in key:
        del sys.modules[key]

from mempalace_light import MemPalaceLightProvider, MCPClient

# Create provider
provider = MemPalaceLightProvider()

# 1. Test availability
print("1. Availability:", provider.is_available())  # Should be True

# 2. Test initialize
provider.initialize("test-session-001", hermes_home="/root/.hermes", platform="cli")
print("2. Initialized. Session:", provider._session_id)

# 3. Test system_prompt_block
block = provider.system_prompt_block()
print("3. Prompt block:", block)  # "[Memory: MemPalace active via MCP]"

# 4. Test prefetch
prefetch = provider.prefetch("test query", session_id="test-session-001")
print("4. Prefetch length:", len(prefetch))  # Should be > 0
print("   Prefetch content:", prefetch[:200])

# 5. Test sync_turn
provider.sync_turn(
    "Test user message about something",
    "Test assistant response",
    session_id="test-session-001"
)
time.sleep(6)  # Wait for coalescing window

# 6. Verify filing via search
search = provider.search("Test user message", wing="wing_gay-mike", room="compressed-context")
print("6. Search results:", len(search.get("results", [])))
assert len(search.get("results", [])) > 0, "sync_turn didn't file!"

# 7. Test on_session_end
provider.on_session_end([
    {"role": "user", "content": "Test message"},
    {"role": "assistant", "content": "Test reply"},
])
time.sleep(2)  # Wait for background thread

# 8. Verify diary write
diary = provider._mcp.call("mempalace_diary_read", {"agent_name": "gay-mike", "last_n": 1})
print("8. Diary entries:", len(diary.get("entries", [])))
assert len(diary.get("entries", [])) > 0, "on_session_end didn't write!"

# 9. Test tools
schemas = provider.get_tool_schemas()
print("9. Tool schemas:", [s["name"] for s in schemas])

tool_result = provider.handle_tool_call("mempalace_status", {})
print("10. Tool result:", tool_result[:100])

# 10. Test shutdown
provider.shutdown()
print("11. Shutdown complete")
```

### Verification Checklist

| Test | Expected |
|------|----------|
| `is_available()` | `True` |
| `initialize()` | Sets `_session_id`, `_available = True` |
| `system_prompt_block()` | Returns `"[Memory: MemPalace active via MCP]"` |
| `prefetch()` | Returns text with `RECENT_MEMORY:` prefixes, > 0 chars |
| `sync_turn()` | Fils `compressed-context` drawer in `wing_gay-mike` |
| `on_session_end()` | Fils diary entry in `wing_gay-mike` (background) |
| `on_pre_compress()` | Returns text if existing context found, else `""` |
| `on_session_switch()` | Updates `_session_id`, clears buffers on `reset=True` |
| `on_delegation()` | Fils `delegations` drawer |
| `get_tool_schemas()` | Returns 3 schemas: search, add_drawer, status |
| `handle_tool_call()` | Returns JSON result for each tool |
| `shutdown()` | Sets `_mcp = None` |

---

## 14. Framework Integration Points

### Where MemoryManager is Wired

`MemoryManager` is instantiated in `run_agent.py`:
```python
self._memory_manager = MemoryManager()
# Only ONE of these:
self._memory_manager.add_provider(plugin_provider)
```

### Full Request Lifecycle

1. **Agent init:**
   - `MemoryManager()` created
   - `add_provider(provider)` registers plugin
   - `build_system_prompt()` assembles system prompt
   - `inject_memory_provider_tools(agent)` adds tool schemas to agent's tool surface
   - `initialize_all(session_id, **kwargs)` initializes all providers

2. **Per-turn (user message received):**
   - `prefetch_all(user_message)` → injects context into system prompt
   - Agent generates response
   - `on_turn_start(turn_number, message, **kwargs)` → per-turn hook

3. **Post-turn (response complete):**
   - `sync_all(user_msg, assistant_response)` → background write
   - `queue_prefetch_all(user_message)` → queue recall for next turn

4. **Session boundary:**
   - `on_session_end(messages)` → session summary
   - `shutdown_all()` → cleanup

5. **Mid-process session changes:**
   - `on_session_switch(new_session_id, ...)` → session ID rotation

6. **Context compression:**
   - `on_pre_compress(messages)` → extract insights before discarding

7. **Built-in memory tool writes:**
   - `notify_memory_tool_write(tool_result, tool_args, ...)` → mirror to external providers

8. **Subagent delegation:**
   - `on_delegation(task, result, ...)` → parent observation

### Tool Injection Flow

```
MemoryManager.get_all_tool_schemas()
    → provider.get_tool_schemas()  # Returns 3 tool schemas
    → normalize_tool_schema()  # Ensures bare function format
    → filter _core_tool_names  # Skip reserved names
    → inject into agent.tools
    → add to agent.valid_tool_names
```

When the model calls a tool:
```
ToolRegistry dispatches tool_name
    → MemoryManager.handle_tool_call(tool_name, args)
    → _tool_to_provider[tool_name]  # Lookup registered provider
    → provider.handle_tool_call(tool_name, args)
    → JSON string result
```

---

## 15. Network Topology

```
AI Container (this host)                     LXC Container
┌──────────────────────────┐                 ┌──────────────────────────┐
│ /root/.hermes/plugins/   │                 │ http://192.168.0.118:    │
│   mempalace-light/       │                 │   8765/mcp                │
│     __init__.py          │                 │                            │
│     plugin.yaml          │                 │ ┌──────────────────────┐   │
│                          │                 │ │ MemPalace MCP Server │   │
│ ~/.hermes/.env           │                 │ │                      │   │
│   MCP_MEMPALACE_API_KEY  │── POST ──────→  │ │ tools/call           │   │
│                          │                 │ │   → mempalace_*      │   │
│ config.yaml              │                 │ └──────────┬───────────┘
│   mcp_servers:           │                 │            │            │
│     mempalace:           │                 │            │            │
│       url: 192.168.0.118 │                 │            │            │
│       headers:           │                 │            │            │
│         Authorization:   │                 │            │            │
│           Bearer ${KEY}  │                 │            │            │
└──────────────────────────┘                 │   ChromaDB    │ SQLite   │
                                             │   (HNSW index) │ diary    │
                                             └──────────────────────────┘
```

**Network:** LAN-only. `192.168.0.118` is the LXC container on the local network. No internet dependency for MemPalace operations.

**Latency:** Typically <50ms on LAN. Timeout is set to 10s as a safety margin.

**Reliability:** The LXC runs independently. If it's offline, the plugin gracefully degrades — `is_available()` returns `False`, all writes are skipped, and the system prompt block is empty. The agent continues functioning normally with just the built-in memory provider.

---

## 16. Dependencies

**Python packages used:**
- `requests` — HTTP client (standard in Hermes Agent environment)
- `requests.adapters.HTTPAdapter` — connection pooling + retry
- `urllib3.util.retry.Retry` — retry strategy
- `hashlib` — SHA256 for dedup
- `threading` — background threads
- `json` — JSON parsing
- `time` — timestamps + coalescing
- `logging` — structured logging
- `os` — path manipulation
- `sys` — module management
- `typing` — type hints
- `dataclasses` — `MCPConfig` dataclass

**No external dependencies.** Everything is stdlib + requests. No pip installs needed.

---

## 17. Memory Usage

| Component | Size | Notes |
|-----------|------|-------|
| `__init__.py` | 539 lines, ~19.5KB | Full implementation |
| `plugin.yaml` | 5 lines, 222 bytes | Manifest |
| `__pycache__/__init__.cpython-313.pyc` | 15KB | Python 3.13 bytecode |
| `__pycache__/__init__.cpychon-311.pyc` | 24KB | Python 3.11 bytecode |
| **Total** | **~59KB** | On disk |

---

## 18. Deployment

### Installation
1. Place `__init__.py` and `plugin.yaml` in `~/.hermes/plugins/mempalace-light/`
2. Set `MCP_MEMPALACE_API_KEY` in `~/.hermes/.env`
3. Ensure `MCP_MEMPALACE_URL` points to the correct LXC
4. Restart Hermes Agent gateway
5. Verify in config.yaml: `memory.provider: mempalace-light`

### Loading
The plugin is loaded automatically on gateway restart. The loader scans `~/.hermes/plugins/memory/` for directories containing a valid `__init__.py`. The `register(ctx)` function is called, which registers the provider.

### Verification
```python
from hermes_agent import get_hermes_home
import sys

# Check if plugin loaded
plugins_dir = get_hermes_home() / "plugins" / "memory"
for plugin_dir in plugins_dir.iterdir():
    if (plugin_dir / "__init__.py").exists():
        print(f"Found plugin: {plugin_dir.name}")
        content = (plugin_dir / "__init__.py").read_text()
        has_mem = "MemoryProvider" in content or "register_memory_provider" in content
        print(f"  Has MemoryProvider string: {has_mem}")
```

---

## 19. Troubleshooting

### Plugin not loading
1. Check `__init__.py` contains `"MemoryProvider"` string
2. Check `plugin.yaml` has `name:` field
3. Verify directory is under `~/.hermes/plugins/memory/`
4. Restart gateway
5. Check logs for `Memory provider 'mempalace-light' registered`

### Provider unavailable
1. Check LXC is reachable: `curl http://192.168.0.118:8765/mcp`
2. Verify API key in `~/.hermes/.env`
3. Check `config.yaml` headers format (must use `headers:`, not `token:`)
4. Check logs for `MCP connection error` or `MCP error`

### No drawers appearing
1. Verify `is_available()` returns `True`
2. Check coalescing window — wait 5+ seconds between sync calls
3. Check for duplicate detection — identical content is skipped
4. Check logs for `sync_turn failed:` or `MCP error:`

### Prefetch returns empty
1. Check diary has entries: `mempalace_diary_read(agent_name="gay-mike", last_n=5)`
2. Verify `content` key is used (not `entry`)
3. Check `entries` list is populated in the response

### Session end not writing
1. Wait 2+ seconds — the worker runs in a background thread
2. Check logs for `on_session_end worker failed:`
3. Verify daemon thread isn't being killed prematurely

### Hook signature errors
1. Compare with the ABC signatures in `agent/memory_provider.py`
2. Use `inspect.signature()` to check method signatures
3. Ensure keyword-only params have `*` separator
4. Ensure `**kwargs` are present where the ABC specifies them

### Empty MCP responses
1. Verify `tools/call` format is used (not direct method)
2. Verify response parsing extracts `content[0]["text"]` then `json.loads()`
3. Check HTTP status code is 200
4. Check for JSON-RPC `"error"` field in response

---

## 20. Future Extensibility

### Potential Enhancements
- **Batch checkpoint:** Combine multiple turns into a single `mempalace_checkpoint` call
- **Incremental sync:** Track `_last_sync_id` to only sync new content
- **Compression-aware sync:** Use `on_pre_compress` output as input to `sync_turn`
- **Delegation tracking:** File subagent task+result with timestamps
- **Cross-wing tunnels:** Auto-create tunnels between related wings on checkpoint
- **Config schema:** Implement `get_config_schema()` and `save_config()` for `hermes memory setup`

### Not Implemented (By Design)
- **Local caching:** No local ChromaDB or vector store. All state is remote.
- **Two-way sync:** Only writes to MemPalace; reads are on-demand via prefetch/search.
- **Real-time notifications:** No push notifications from LXC to AI container.
- **File attachments:** No binary/multimedia support through the plugin.
- **Cross-agent sharing:** Each agent writes to its own wing (e.g., `wing_gay-mike`).

---

*Last updated: 2026-07-01*
