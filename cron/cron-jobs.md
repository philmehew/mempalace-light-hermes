# MemPalace Cron Jobs — Tracked Config

## Job 1: maintenance auto

```yaml
name: memcalace: maintenance auto
schedule: 30 0 * * *       # 00:30 daily
type: no_agent (script)
script: scripts/mempalace-maintenance.py
deliver: origin
```

Reads session DB directly, runs extraction patterns from `routing_config.yaml`,
files to palace + KG. Zero LLM cost.

---

## Job 2: yaml review

```yaml
name: memcalace: yaml review
schedule: 0 8 * * 1         # Monday 08:00 weekly
type: LLM agent
deliver: origin
```

Two tasks:
1. **Routing review** — checks `general/general` bucket, files identifiable
   content to correct wing/room, patches routing keywords
2. **Extraction review** — reviews drawers since last checkpoint, spots missed
   patterns, files them immediately as drawers + KG triples, patches YAML

---

## Prompt: yaml review

```
You are Gay Mike running the weekly YAML review for MemPalace. Two tasks:

**Task 1: Routing Review**
1. Call `mempalace_list_drawers(wing="general", room="general", limit=50)`
2. For each drawer in general, read its content_preview. If the content clearly
   belongs to a known wing/room, file it there via `mempalace_checkpoint`.
3. Read routing_config.yaml, patch keywords if recurring topics warrant it.

**Task 2: Extraction Pattern Review**
1. Read checkpoint from diary: `mempalace_diary_read(agent_name="gay-mike",
   last_n=3)`, extract `last_reviewed:` timestamp. Default 0 on first run.
2. List drawers via `mempalace_list_drawers(limit=100)`, filter client-side
   where metadata.filed_at > last_reviewed. Iterate wings/rooms.
3. Look at `[AUTO high]` / `[AUTO medium]` drawers — note what fired.
4. Look for missed patterns (3+ instances of a fact type with no auto drawer).
5. If 3+ instances: file now via checkpoint + kg_add, then patch YAML.
6. If 3+ instances with wrong entity mapping: file corrected versions, update
   YAML.

**Filing rules:**
- Check duplicate before filing: `mempalace_check_duplicate`
- Check KG before adding: `mempalace_kg_query` first
- Use `mempalace_checkpoint` for batch, `mempalace_kg_add` for singles

**Important:**
- Only update YAML with clear evidence of 3+ instances
- Keep patterns simple and specific
- Prefer context-matching entity_rules over static entity links
- Read the full YAML before patching — get exact text

Write diary checkpoint:
`mempalace_diary_write(agent_name="gay-mike", topic="yaml-review",
entry="YAML_REVIEW:general.N.drawers|reviewed.M.drawers|filed.P.items|
KG.Q.added|added.R.patterns|modified.S.routes|last_reviewed:TIMESTAMP|★★★")`

Report concise: general bucket status, files filed, KG added, YAML changes.
```