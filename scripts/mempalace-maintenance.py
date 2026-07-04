#!/usr/bin/env python3
"""
MemPalace maintenance — no_agent cron script.
Reads session DB directly, extracts facts via patterns from routing_config.yaml,
files to MemPalace via MCP. No LLM calls.
"""
import json
import os
import re
import sqlite3
import time
import urllib.request
import yaml
from collections import Counter
from datetime import datetime, timezone

# --- Config ---
CONFIG_PATH = os.path.expanduser("~/.hermes/plugins/mempalace-light/routing_config.yaml")
STATE_DB = os.path.expanduser("~/.hermes/state.db")
MCP_URL = "http://192.168.0.118:8765/mcp"
MCP_KEY = os.environ.get("MCP_MEMPALACE_API_KEY", "jQXHLbFzHS4YqsfLOJsATRvzRQ0t6b8Y2wHoq2bki9o=")
AGENT_NAME = "gay-mike"
SKIP_SESSION_PREFIXES = ("cron_",)

# --- Load config ---
def load_config():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("extraction", []), cfg.get("wings", {})


# --- MCP helper ---
def mcp_call(tool_name, arguments=None):
    if arguments is None:
        arguments = {}
    body = json.dumps({
        "jsonrpc": "2.0", "id": int(time.time() * 1000) % 100000,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }).encode()
    req = urllib.request.Request(MCP_URL, data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {MCP_KEY}")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        text = data.get("result", {}).get("content", [{}])[0].get("text", "{}")
        return json.loads(text)
    except Exception as e:
        print(f"MCP error ({tool_name}): {e}")
        return None


# --- Diary checkpoint ---
def read_checkpoint():
    """Read last_processed timestamp from diary."""
    result = mcp_call("mempalace_diary_read", {
        "agent_name": AGENT_NAME, "last_n": 5
    })
    if not result or not result.get("entries"):
        return 0
    for entry in result["entries"]:
        content = entry.get("content", "") or entry.get("entry", "")
        if "last_processed" in content:
            match = re.search(r"last_processed[=:](\d+)", content)
            if match:
                return int(match.group(1))
    return 0


def write_checkpoint(timestamp, sessions_done, facts_filed, kg_added, bucket_count):
    entry = (
        f"MAINTENANCE:processed.{sessions_done}.sessions"
        f"|last_processed:{timestamp}"
        f"|extracted.{facts_filed}.facts"
        f"|KG.{kg_added}.new"
        f"|general.{bucket_count}.drawers"
        f"|★★★"
    )
    mcp_call("mempalace_diary_write", {
        "agent_name": AGENT_NAME, "topic": "scavenger", "entry": entry,
    })
    return entry


# --- Session DB ---
def get_sessions_since(timestamp):
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT id, title, started_at, ended_at, message_count
           FROM sessions
           WHERE started_at > ? AND archived = 0
           ORDER BY started_at ASC""",
        (timestamp,)
    )
    return [dict(r) for r in cur.fetchall()]


def get_session_messages(session_id):
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT role, content, timestamp
           FROM messages
           WHERE session_id = ? AND role IN ('user', 'assistant')
           ORDER BY timestamp ASC""",
        (session_id,)
    )
    return [dict(r) for r in cur.fetchall()]


# --- Fact Extraction (YAML-driven) ---
def extract_facts(messages, extraction_patterns):
    """Extract facts using patterns from routing_config.yaml. Returns list of dicts."""
    facts = []
    seen_values = set()

    text = "\n".join(
        f"[{m['role']}] {m.get('content', '')}" for m in messages if m.get('content')
    )

    for entry in extraction_patterns:
        ftype = entry["type"]
        confidence = entry.get("confidence", "medium")
        route = entry.get("route", {})
        entity = entry.get("entity")

        if "names" in entry:
            # List-based extraction (e.g. model names)
            for name in entry["names"]:
                pattern = re.compile(r'\b' + re.escape(name) + r'[\w.-]*\b', re.IGNORECASE)
                for m in pattern.finditer(text):
                    value = m.group()
                    key = (ftype, value[:60])
                    if key in seen_values:
                        continue
                    seen_values.add(key)
                    facts.append({
                        "type": ftype,
                        "value": value,
                        "confidence": confidence,
                        "context": text[max(0, m.start()-30):m.end()+30],
                        "entity": entity,
                        "route": route,
                        "entity_rules": entry.get("entity_rules", []),
                    })

        elif "pattern" in entry:
            # Regex-based extraction
            rx = re.compile(entry["pattern"], re.IGNORECASE)
            for m in rx.finditer(text):
                # Use the first capture group as the value
                value = m.group(1) if m.lastindex else m.group(0)
                key = (ftype, value[:60])
                if key in seen_values:
                    continue
                seen_values.add(key)
                start = max(0, m.start() - 30)
                ctx = text[start:m.end() + 30]
                facts.append({
                    "type": ftype,
                    "value": value.strip(),
                    "confidence": confidence,
                    "context": ctx,
                    "entity": entity,
                    "route": route,
                    "entity_rules": entry.get("entity_rules", []),
                })

    return facts


# --- Routing ---
def route_fact(fact, wings):
    """Determine wing/room for a fact using YAML extraction rules then wing keywords."""
    # 1. Check if fact type has an explicit route in extraction config
    if fact.get("route"):
        return fact["route"]["wing"], fact["route"]["room"]

    # 2. Check entity_rules context matching
    ctx = fact.get("context", "").lower()
    for rule in fact.get("entity_rules", []):
        if re.search(rule["context"], ctx, re.IGNORECASE):
            return rule["wing"], rule["room"]

    # 3. Fall back to wing keyword matching
    best_wing = "infrastructure"
    best_room = "hermes"
    best_wing_score = 0

    for wname, wdata in wings.items():
        score = sum(1 for kw in wdata.get("keywords", []) if kw.lower() in ctx)
        if score > best_wing_score:
            best_wing_score = score
            best_wing = wname
            best_room = "general"
            # Check rooms
            for rname, rdata in wdata.get("rooms", {}).items():
                if rname == "general":
                    continue
                room_score = sum(1 for kw in rdata.get("keywords", []) if kw.lower() in ctx)
                if room_score > 0:
                    best_room = rname
                    break

    return best_wing, best_room


# --- KG triples ---
def build_kg_triples(fact):
    """Build KG triples from a fact using its entity mappings."""
    triples = []
    ctx = fact.get("context", "").lower()

    # 1. Entity rules (context-dependent)
    for rule in fact.get("entity_rules", []):
        if re.search(rule["context"], ctx, re.IGNORECASE):
            triples.append({
                "subject": rule["subject"],
                "predicate": rule["predicate"],
                "object": fact["value"].replace(" ", "_"),
            })
            break  # first match wins

    # 2. Static entity link
    entity = fact.get("entity")
    if entity and isinstance(entity, dict):
        triples.append({
            "subject": entity["subject"],
            "predicate": entity["predicate"],
            "object": fact["value"].replace(" ", "_"),
        })

    return triples


# --- Filing ---
def file_facts(facts, wings):
    """File facts to palace via MCP checkpoint."""
    items = []
    all_triples = []

    for f in facts:
        wing, room = route_fact(f, wings)
        drawer_content = (
            f"[AUTO {f['confidence']}] {f['type']}: {f['value']}\n"
            f"Context: {f['context'][:200]}"
        )
        items.append({"wing": wing, "room": room, "content": drawer_content})
        all_triples.extend(build_kg_triples(f))

    if not items:
        return 0, 0

    # Dedup against existing drawers
    unique_items = []
    for item in items:
        dup = mcp_call("mempalace_check_duplicate", {
            "content": item["content"], "threshold": 0.95
        })
        if dup and dup.get("is_duplicate"):
            continue
        unique_items.append(item)

    if not unique_items:
        return 0, 0

    # File via checkpoint
    result = mcp_call("mempalace_checkpoint", {"items": unique_items})
    if result is None:
        return 0, 0

    # Add KG triples (deduped)
    kg_added = 0
    for triple in all_triples:
        existing = mcp_call("mempalace_kg_query", {
            "entity": triple["subject"], "direction": "outgoing"
        })
        if existing and existing.get("facts"):
            already = any(
                f["predicate"] == triple["predicate"]
                and f["object"] == triple["object"]
                for f in existing["facts"]
            )
            if already:
                continue
        mcp_call("mempalace_kg_add", {
            "subject": triple["subject"],
            "predicate": triple["predicate"],
            "object": triple["object"],
        })
        kg_added += 1

    return len(unique_items), kg_added


# --- General bucket review ---
def review_general_bucket():
    """Check general/general bucket for un-routed content."""
    result = mcp_call("mempalace_list_drawers", {
        "wing": "general", "room": "general"
    })
    if not result:
        return 0, []

    drawers = result.get("drawers", [])
    if not drawers:
        return 0, []

    all_text = " ".join(d.get("content_preview", "") for d in drawers)
    words = re.findall(r'\b[a-zA-Z]{4,}\b', all_text.lower())
    freq = Counter(words).most_common(10)

    return len(drawers), freq


# --- Main ---
def main():
    print("MemPalace Maintenance — Auto Mode")
    print(f"Run: {datetime.now(timezone.utc).isoformat()}\n")
    start = time.time()

    extraction_patterns, wings = load_config()
    print(f"Loaded {len(extraction_patterns)} extraction patterns, {len(wings)} wings")

    # 1. Read checkpoint
    last_processed = read_checkpoint()
    print(f"Checkpoint: last_processed = {last_processed}",
          f"({datetime.fromtimestamp(last_processed, timezone.utc).isoformat()})"
          if last_processed else "(first run)")

    # 2. Get sessions
    sessions = get_sessions_since(last_processed)
    sessions = [s for s in sessions
                if not s["id"].startswith(SKIP_SESSION_PREFIXES)]
    print(f"New sessions: {len(sessions)}")

    # 3. Process each session
    total_facts = 0
    total_kg = 0
    for s in sessions:
        msgs = get_session_messages(s["id"])
        if not msgs:
            continue
        facts = extract_facts(msgs, extraction_patterns)
        if facts:
            filed, kg_added = file_facts(facts, wings)
            total_facts += filed
            total_kg += kg_added
            if filed:
                print(f"  Session {s['id'][:20]} → {filed} facts, {kg_added} KG triples")
                for f in facts:
                    print(f"    [{f['confidence']}] {f['type']}: {f['value']}")

    # 4. Review general bucket
    bucket_count, freq = review_general_bucket()
    if bucket_count:
        print(f"\nGeneral bucket: {bucket_count} drawers")
        print(f"  Top words: {', '.join(f'{w}({c})' for w, c in freq[:5])}")
    else:
        print("\nGeneral bucket: empty — routing OK")

    # 5. Write checkpoint
    now_ts = int(time.time())
    diary_entry = write_checkpoint(now_ts, len(sessions), total_facts, total_kg, bucket_count)
    print(f"\nDiary: {diary_entry}")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s — {total_facts} facts filed, {total_kg} KG triples added")
    if total_facts == 0 and bucket_count == 0:
        print("\nNothing to report.")


if __name__ == "__main__":
    main()