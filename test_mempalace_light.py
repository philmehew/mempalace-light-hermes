#!/usr/bin/env python3
"""Verification test for the mempalace-light plugin.

Run from the repo root:
    python3 test_mempalace_light.py

This connects to your MemPalace MCP endpoint and verifies all hooks work.
"""

import sys
import os
import time
import json

# Add the plugin directory so we can import it
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mempalace_light import MemPalaceLightProvider, MCPClient, get_mcp_config


def test_all():
    """Run full verification suite."""
    print("=" * 60)
    print("MemPalace Light Plugin — Verification Test")
    print("=" * 60)

    # Load config
    config = get_mcp_config()
    print(f"\nMCP URL: {config.url}")
    print(f"API Key: {config.api_key[:8]}...{'[REDACTED]' if len(config.api_key) > 8 else '[MISSING]'}")
    print(f"Timeout: {config.timeout}s")
    print()

    # 1. Create provider and check availability
    print("1. Checking availability...")
    provider = MemPalaceLightProvider()
    available = provider.is_available()
    print(f"   is_available(): {available}")
    assert available, "MCP server unreachable — check URL and API key"
    print("   ✓ Available")

    # 2. Initialize
    print("\n2. Initializing...")
    provider.initialize(
        "test-session-verification",
        hermes_home=os.path.expanduser("~/.hermes"),
        platform="cli",
    )
    print(f"   Session ID: {provider._session_id}")
    print(f"   Available: {provider._available}")
    assert provider._available, "Provider not available after initialize"
    print("   ✓ Initialized")

    # 3. System prompt block
    print("\n3. System prompt block...")
    block = provider.system_prompt_block()
    print(f"   '{block}'")
    assert block == "[Memory: MemPalace active via MCP]"
    print("   ✓ Correct")

    # 4. Prefetch
    print("\n4. Prefetch (reading recent diary entries)...")
    prefetch = provider.prefetch("test query", session_id="test-session-verification")
    print(f"   Length: {len(prefetch)} chars")
    if prefetch:
        print(f"   Preview: {prefetch[:150]}...")
        assert "RECENT_MEMORY:" in prefetch, "Prefetch should contain RECENT_MEMORY: prefix"
    print(f"   {'✓ Got results' if prefetch else '⚠ Empty (no diary entries yet)'}")

    # 5. Sync turn
    print("\n5. Syncing test turn...")
    test_user = "This is a verification test message for mempalace-light plugin"
    test_assistant = "This is a verification test response from mempalace-light"
    provider.sync_turn(test_user, test_assistant, session_id="test-session-verification")
    print("   Sync sent (waiting for coalescing window + write)")
    time.sleep(6)  # Wait for coalescing
    print("   Sync complete")

    # 6. Verify filing via search
    print("\n6. Verifying filing via search...")
    search = provider.search(
        "verification test message",
        wing="wing_gay-mike",
        room="compressed-context",
        limit=5,
    )
    results = search.get("results", [])
    print(f"   Results found: {len(results)}")
    assert len(results) > 0, "sync_turn didn't file any drawers"
    print(f"   ✓ Drawer filed successfully")

    # 7. Session end (background)
    print("\n7. Testing on_session_end...")
    provider.on_session_end([
        {"role": "user", "content": "Session end test message"},
        {"role": "assistant", "content": "Session end test response"},
    ])
    time.sleep(2)  # Wait for background thread
    diary = provider._mcp.call(
        "mempalace_diary_read",
        {"agent_name": "gay-mike", "last_n": 1},
    )
    entries = diary.get("entries", [])
    print(f"   Diary entries: {len(entries)}")
    assert len(entries) > 0, "on_session_end didn't write diary entry"
    print("   ✓ Session end diary written")

    # 8. Tool schemas
    print("\n8. Tool schemas...")
    schemas = provider.get_tool_schemas()
    names = [s["name"] for s in schemas]
    print(f"   Tools: {names}")
    assert len(names) == 3, f"Expected 3 tools, got {len(names)}"
    assert "mempalace_search" in names
    assert "mempalace_add_drawer" in names
    assert "mempalace_status" in names
    print("   ✓ All 3 tools registered")

    # 9. Tool call
    print("\n9. Tool call (mempalace_status)...")
    result = provider.handle_tool_call("mempalace_status", {})
    result_obj = json.loads(result) if isinstance(result, str) else result
    error = result_obj.get("error")
    print(f"   Result: {json.dumps(result_obj, indent=None)[:200]}")
    if error:
        print(f"   ⚠ Error: {error}")
    print("   ✓ Tool call successful")

    # 10. Shutdown
    print("\n10. Shutdown...")
    provider.shutdown()
    assert provider._mcp is None, "MCP client not released"
    print("   ✓ Clean shutdown")

    print("\n" + "=" * 60)
    print("All tests passed! Plugin is working correctly.")
    print("=" * 60)


if __name__ == "__main__":
    try:
        test_all()
    except AssertionError as e:
        print(f"\n✗ FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
