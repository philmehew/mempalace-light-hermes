#!/usr/bin/env python3
"""
MemPalace scavenger — feeds recent session metadata to the LLM cron prompt.
Outputs session list as stdout, which gets injected into the agent's prompt.
The LLM then uses session_search to read each session and extract facts.
"""
import sqlite3
import time
from datetime import datetime, timezone

STATE_DB = "/root/.hermes/state.db"
SKIP_PREFIXES = ("cron_",)
HOURS_BACK = 24  # Look at sessions from last 24h

def main():
    cutoff = time.time() - (HOURS_BACK * 3600)

    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT id, title, started_at, ended_at, message_count, model
           FROM sessions
           WHERE started_at > ? AND archived = 0
           ORDER BY started_at ASC""",
        (cutoff,)
    )
    sessions = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Filter out cron sessions
    sessions = [s for s in sessions
               if not s["id"].startswith(SKIP_PREFIXES)]

    if not sessions:
        # No output = silent (no_agent-style watchdog behaviour
        # doesn't apply here since this is agent-driven, but keep quiet)
        print("No new sessions in the last 24h.")
        return

    print(f"Sessions to review ({len(sessions)}):")
    print()
    for s in sessions:
        ts = datetime.fromtimestamp(s["started_at"], timezone.utc)
        ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")
        title = s.get("title") or "untitled"
        print(f"- Session ID: {s['id']}")
        print(f"  Title: {title}")
        print(f"  Started: {ts_str}")
        print(f"  Messages: {s['message_count']}")
        print(f"  Model: {s.get('model', 'unknown')}")
        print()

    print("---")
    print(f"Total: {len(sessions)} sessions to review.")


if __name__ == "__main__":
    main()