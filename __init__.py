"""
MemPalace Light — MemoryProvider plugin for Hermes Agent.

Writes conversation state to MemPalace via its MCP server (HTTP JSON-RPC).
Does NOT require local ChromaDB — all storage goes through the remote LXC.

Lifecycle hooks:
  prefetch()   → injected into system prompt (diary entries)
  sync_turn()  → fires after every assistant response (conversation exchange)
  on_session_end() → fires on session exit (diary summary)
  on_pre_compress() → fires before context compression (context consolidation)
"""

import os
import sys
import time
import json
import hashlib
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from collections import defaultdict

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing Configuration
# ---------------------------------------------------------------------------

_ROUTING_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "routing_config.yaml"
)
_routing_config = None
_routing_config_mtime = 0


def _load_routing_config() -> dict:
    """Load routing_config.yaml, re-reading if the file changed."""
    global _routing_config, _routing_config_mtime
    if yaml is None:
        logger.warning("yaml module not available — routing disabled")
        return None
    try:
        mtime = os.path.getmtime(_ROUTING_CONFIG_PATH)
        if _routing_config is not None and mtime == _routing_config_mtime:
            return _routing_config
        with open(_ROUTING_CONFIG_PATH) as f:
            _routing_config = yaml.safe_load(f)
        _routing_config_mtime = mtime
        logger.info("routing_config.yaml loaded/reloaded")
        return _routing_config
    except FileNotFoundError:
        logger.warning("routing_config.yaml not found — using defaults")
        return None
    except Exception as e:
        logger.error(f"Failed to load routing_config.yaml: {e}")
        return None


def route(text: str) -> Tuple[str, str]:
    """
    Route conversation text to a (wing, room) pair using keyword matching.
    Falls back to ("wing_gay-mike", "general") on no match.
    """
    config = _load_routing_config()
    if config is None:
        return "wing_gay-mike", "general"

    default_wing = config.get("default_wing", "wing_gay-mike")
    default_room = config.get("default_room", "general")
    text_lower = text.lower()[:2000]

    # Score wings — include room keywords as wing-level signal too
    wing_scores = defaultdict(int)
    for wing_name, wing_def in config.get("wings", {}).items():
        for kw in wing_def.get("keywords", []):
            wing_scores[wing_name] += text_lower.count(kw.lower())
        for room_def in wing_def.get("rooms", {}).values():
            for kw in room_def.get("keywords", []):
                wing_scores[wing_name] += text_lower.count(kw.lower())

    if not wing_scores or max(wing_scores.values()) == 0:
        return default_wing, default_room

    best_wing = max(wing_scores, key=wing_scores.get)

    # Score rooms within the best wing
    wing_def = config.get("wings", {}).get(best_wing, {})
    room_scores = defaultdict(int)
    for room_name, room_def in wing_def.get("rooms", {}).items():
        room_kw = room_def.get("keywords", []) + [room_name]
        for kw in room_kw:
            room_scores[room_name] += text_lower.count(kw.lower())

    if not room_scores or max(room_scores.values()) == 0:
        return best_wing, "general"

    best_room = max(room_scores, key=room_scores.get)
    return best_wing, best_room


# ---------------------------------------------------------------------------
# MCP JSON-RPC Client
# ---------------------------------------------------------------------------

@dataclass
class MCPConfig:
    url: str
    api_key: str
    timeout: int = 10


def get_mcp_config() -> MCPConfig:
    """Load MCP config from .env or return defaults."""
    env_path = os.path.join(os.path.expanduser("~/.hermes"), ".env")
    api_key = os.environ.get("MCP_MEMPALACE_API_KEY", "")
    
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key == "MCP_MEMPALACE_API_KEY" and not api_key:
                        api_key = value
    
    return MCPConfig(
        url=os.environ.get("MCP_MEMPALACE_URL", "http://192.168.0.118:8765/mcp"),
        api_key=api_key,
        timeout=int(os.environ.get("MCP_MEMPALACE_TIMEOUT", "10")),
    )


class MCPClient:
    """JSON-RPC 2.0 client for MemPalace MCP server."""
    
    def __init__(self, config: Optional[MCPConfig] = None):
        self.config = config or get_mcp_config()
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        })
        
        # Retry on transient errors
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
    
    def call(self, method: str, params: Optional[dict] = None) -> dict:
        """Make a JSON-RPC call to MCP server.
        
        Uses tools/call format: {"name": "method", "arguments": params}
        """
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {
                "name": method,
                "arguments": params or {},
            },
        }
        
        try:
            resp = self._session.post(
                self.config.url,
                json=payload,
                timeout=self.config.timeout,
            )
            resp.raise_for_status()
            result = resp.json()
            
            if "error" in result:
                logger.error(f"MCP error: {result['error']}")
                return {"error": str(result["error"])}
            
            # Extract text content from MCP response
            content = result.get("result", {}).get("content", [])
            if content:
                # First content item with text
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        return json.loads(item["text"])
            
            return {}
        except requests.exceptions.Timeout:
            logger.error(f"MCP timeout: {method}")
            return {"error": f"timeout ({self.config.timeout}s)"}
        except requests.exceptions.ConnectionError:
            logger.error(f"MCP connection error: {method}")
            return {"error": "connection refused"}
        except Exception as e:
            logger.error(f"MCP call failed: {method}: {e}")
            return {"error": str(e)}
    
    def is_available(self) -> bool:
        """Check if the MCP server is reachable."""
        try:
            result = self.call("mempalace_status")
            return "error" not in result
        except Exception:
            return False


# ---------------------------------------------------------------------------
# SHA256 Idempotency
# ---------------------------------------------------------------------------

def _content_hash(content: str) -> str:
    """Generate SHA256 hash for deduplication."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


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


# ---------------------------------------------------------------------------
# MemPalace Light Provider
# ---------------------------------------------------------------------------

class MemPalaceLightProvider:
    # Implements the MemoryProvider ABC interface (register(ctx) pattern)
    """MemoryProvider that writes to MemPalace via MCP server."""
    
    name = "mempalace-light"
    
    def __init__(self, mcp_client: Optional[MCPClient] = None):
        self._mcp = mcp_client or MCPClient()
        self._palace_url = self._mcp.config.url
        self._available = False
        self._last_sync = 0
        self._sync_interval = 5  # Coalesce sync calls (seconds)
        self._turn_buffer: List[Tuple[str, str]] = []  # (user_msg, assistant_msg)
        self._turn_buffer_lock = threading.Lock()
        self._session_id = ""
        self._hermes_home = ""
    
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session."""
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home", os.path.expanduser("~/.hermes"))
        # Refresh availability check with fresh config
        self._available = self._mcp.is_available()
    
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas for mempalace search/add/list."""
        return [
            {
                "name": "mempalace_search",
                "description": "Search the MemPalace for relevant context",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results", "default": 5},
                        "wing": {"type": "string", "description": "Filter by wing"},
                        "room": {"type": "string", "description": "Filter by room"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mempalace_add_drawer",
                "description": "Add a drawer to the MemPalace",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "wing": {"type": "string", "description": "Wing (project name)"},
                        "room": {"type": "string", "description": "Room (topic)"},
                        "content": {"type": "string", "description": "Content to store"},
                    },
                    "required": ["wing", "room", "content"],
                },
            },
            {
                "name": "mempalace_status",
                "description": "Get MemPalace status overview",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]
    
    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a tool call for this provider's tools."""
        if tool_name == "mempalace_search":
            result = self._mcp.call("mempalace_search", args)
        elif tool_name == "mempalace_add_drawer":
            result = self._mcp.call("mempalace_add_drawer", args)
        elif tool_name == "mempalace_status":
            result = self._mcp.call("mempalace_status", {})
        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        return json.dumps(result)
    
    def is_available(self) -> bool:
        """Check if MemPalace MCP server is reachable."""
        if not self._available:
            self._available = self._mcp.is_available()
        return self._available
    
    def system_prompt_block(self) -> str:
        """Return a block to inject into the system prompt."""
        if not self.is_available():
            return ""
        return "[Memory: MemPalace active via MCP]"
    
    # -----------------------------------------------------------------------
    # Lifecycle Hooks
    # -----------------------------------------------------------------------
    
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Load recent diary entries AND search palace for relevant context."""
        if not self.is_available():
            return ""

        blocks = []

        # --- 1. Diary entries (session summaries) ---
        try:
            result = self._mcp.call(
                "mempalace_diary_read",
                {"agent_name": "gay-mike", "last_n": 5},
            )
            if "error" not in result:
                entries = result.get("entries", [])
                lines = []
                for entry in entries[:3]:
                    entry_text = entry.get("content", "")
                    if entry_text:
                        lines.append(f"RECENT_MEMORY: {entry_text[:500]}")
                if lines:
                    blocks.append("--- Recent Memories ---\n" + "\n".join(lines))
        except Exception as e:
            logger.error(f"prefetch diary_read error: {e}")

        # --- 2. Semantic search (palace context) ---
        if len(query) >= 5:
            try:
                search_result = self._mcp.call(
                    "mempalace_search",
                    {
                        "query": query[:250],
                        "limit": 3,
                        "max_distance": 1.2,
                    },
                )
                if "error" not in search_result:
                    matches = search_result.get("results", [])
                    palace_lines = []
                    for match in matches[:3]:
                        text = match.get("text", "")
                        wing = match.get("wing", "")
                        room = match.get("room", "")
                        created = match.get("created_at", "")
                        if created:
                            ts = created[:19]  # ISO format: 2026-07-03T18:11:51
                        else:
                            ts = "?"
                        if "## Turn" in text:
                            lines = text.split("\n")
                            relevant = []
                            for line in lines:
                                l = line.lower()
                                if any(kw in l for kw in ["model", "port", "qwen", "phi", "gemma", "ministral", "strix", "800"]):
                                    relevant.append(line)
                            if relevant:
                                snippet = "\n".join(relevant[:4])
                                palace_lines.append(f"PALACE_MATCH [{wing}/{room} @ {ts}]: {snippet}")
                    if palace_lines:
                        blocks.append("--- Palace Context ---\n" + "\n".join(palace_lines))
            except Exception as e:
                logger.error(f"prefetch search error: {e}")

        if blocks:
            result = "\n".join(blocks) + "\n"
            total = len(result)
            if total > 2000:
                result = result[:2000] + "\n... (truncated)"
            logger.info(f"prefetch: {len(blocks)} block(s), {len(result)} chars")
            return result
        return ""
    
    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn."""
        pass  # Not needed for our synchronous MCP approach
    
    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        Record conversation exchange to MemPalace.
        Fires after every assistant response.
        """
        if not self.is_available():
            return
        
        # Coalesce rapid sync calls
        now = time.time()
        if now - self._last_sync < self._sync_interval:
            return
        self._last_sync = now
        
        # Use the provided session_id if different from ours
        if session_id:
            self._session_id = session_id
        
        # Build the turn content
        turn_text = f"USER: {user_content}\nASSISTANT: {assistant_content}"
        
        # Route to the right wing/room based on content
        combined = f"{user_content} {assistant_content}"
        wing, room = route(combined)
        
        # Check for duplicates before writing
        if _check_duplicate(self._mcp, turn_text, wing, room):
            logger.info("sync_turn: duplicate detected, skipping")
            return
        
        # File as a drawer checkpoint (no diary this time)
        result = self._mcp.call(
            "mempalace_checkpoint",
            {
                "items": [
                    {
                        "wing": wing,
                        "room": room,
                        "content": turn_text,
                    }
                ],
            },
        )
        
        if "error" in result:
            logger.error(f"sync_turn failed: {result['error']}")
        else:
            logger.info(f"sync_turn: filed via MCP to {wing}/{room}")
    
    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """
        Write session summary to diary.
        Fires on session exit/shutdown.
        Runs in background thread to avoid blocking.
        """
        threading.Thread(
            target=self._on_session_end_worker,
            args=(messages,),
            daemon=True,
        ).start()
    
    def _on_session_end_worker(self, messages: List[Dict[str, Any]]) -> None:
        """Worker that runs on_session_end in background."""
        if not self.is_available():
            return
        
        try:
            # Build a summary from the messages
            summary = self._build_session_summary(messages)
            
            # Write diary entry
            result = self._mcp.call(
                "mempalace_diary_write",
                {
                    "agent_name": "gay-mike",
                    "entry": summary,
                    "topic": "session_end",
                    "wing": "wing_gay-mike",
                },
            )
            
            if "error" in result:
                logger.error(f"on_session_end failed: {result['error']}")
            else:
                logger.debug(f"on_session_end: diary entry written")
                
        except Exception as e:
            logger.error(f"on_session_end worker failed: {e}")
    
    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """
        Consolidate context before compression.
        Fires before context compression.
        """
        if not self.is_available():
            return ""
        
        try:
            # Search for existing compressed context
            result = self._mcp.call(
                "mempalace_search",
                {
                    "query": "session context conversation",
                    "wing": "wing_gay-mike",
                    "room": "compressed-context",
                    "limit": 5,
                },
            )
            
            if "error" in result:
                logger.warning(f"on_pre_compress search failed: {result['error']}")
                return ""
            
            items = result.get("results", [])
            if items:
                # Return existing context as preservation hint for compressor
                return "Existing conversation context is stored in MemPalace."
            
        except Exception as e:
            logger.error(f"on_pre_compress failed: {e}")
        
        return ""
    
    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """No-op — write handled by MCP tools."""
        pass
    
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Track turn count."""
        pass
    
    def on_session_switch(self, new_session_id: str, *,
                          parent_session_id: str = "", reset: bool = False,
                          rewound: bool = False, **kwargs) -> None:
        """Handle session ID rotation."""
        self._session_id = new_session_id
        if reset:
            self._turn_buffer.clear()
            self._last_sync = 0
    
    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """Handle subagent completion."""
        if not self.is_available():
            return
        try:
            # File delegation outcome to palace
            self._mcp.call(
                "mempalace_checkpoint",
                {
                    "items": [
                        {
                            "wing": "wing_gay-mike",
                            "room": "delegations",
                            "content": f"TASK: {task}\nRESULT: {result}",
                        }
                    ],
                },
            )
        except Exception:
            pass
    
    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------
    
    def _build_session_summary(self, messages: List[Dict[str, Any]]) -> str:
        """Build a concise summary from session messages."""
        # Extract key exchanges
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        
        # Get the first user message as context
        context = user_msgs[0].get("content", "")[:300] if user_msgs else "No context"
        
        # Count exchanges
        turns = min(len(user_msgs), len(assistant_msgs))
        
        summary = (
            f"SESSION:2026-07-01|session_end|ALC.req:session.summary|★★★\n"
            f"\nSession completed: {turns} turns, {len(messages)} messages.\n"
            f"Context: {context}"
        )
        return summary
    
    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""
        self._mcp = None
    
    def backup_paths(self) -> List[str]:
        """Return extra on-disk paths this provider stores OUTSIDE HERMES_HOME."""
        return []
    
    # -----------------------------------------------------------------------
    # Public API (for tool usage)
    # -----------------------------------------------------------------------
    
    def search(self, query: str, limit: int = 5, **kwargs) -> dict:
        """Search the palace."""
        return self._mcp.call(
            "mempalace_search",
            {"query": query, "limit": limit, **kwargs},
        )
    
    def status(self) -> dict:
        """Get palace status."""
        return self._mcp.call("mempalace_status")
    
    def list_wings(self) -> dict:
        """List all wings."""
        return self._mcp.call("mempalace_list_wings")
    
    def add_drawer(self, wing: str, room: str, content: str) -> dict:
        """Add a drawer to the palace."""
        return self._mcp.call(
            "mempalace_add_drawer",
            {"wing": wing, "room": room, "content": content},
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register MemPalace Light as a memory provider."""
    ctx.register_memory_provider(MemPalaceLightProvider())
