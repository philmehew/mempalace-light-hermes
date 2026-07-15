# MemPalace Cron Jobs — Tracked Config

## Job 1: scavenger (LLM-driven)

```yaml
name: MemPalace Scavenger (LLM-driven)
schedule: 30 0 * * *       # 00:30 daily
type: LLM agent + script
script: scripts/mempalace-scavenger.py
deliver: origin
toolsets: [session_search, file]
```

The script (`mempalace-scavenger.py`) reads the Hermes session DB and
outputs metadata for sessions from the last 24h. The LLM agent then:

1. Uses `session_search` to read each session's content
2. Extracts durable facts (currently true, about running systems)
3. Files drawers via `mcp_mempalace_checkpoint` for anything worth remembering
4. Adds KG triples via `mcp_mempalace_kg_add` for structured infrastructure facts
5. Deduplicates against existing KG entities before adding
6. Writes a diary checkpoint via `mcp_mempalace_diary_write`

**Key rules:**
- NO passwords, tokens, or secrets are ever extracted
- Model names are normalised (no .gguf suffixes, quant tags, or filenames)
- Only models actually running get `runs_model` triples — not researched/rejected ones
- Entity names are canonical (Strix_Halo, MemPalace_LXC, Hermes, PVE_host, etc.)

The old regex-based `routing_config.yaml` extraction patterns have been removed.
Wing/room keyword routing is retained for reference only.

---

## Removed Jobs

### yaml review (deleted July 2026)

Was a weekly LLM cron (Mon 08:00) that reviewed routing patterns and
patched the YAML extraction config. Obsolete now that extraction is
LLM-driven. Prompt was in `cron/yaml-review-prompt.txt` (also deleted).

### maintenance auto (replaced July 2026)

Was a no_agent script (`mempalace-maintenance.py`) that used regex
patterns to extract facts from session text. Replaced by the
LLM-driven scavenger above because regex extraction produced too many
false positives (usernames as models, filenames as models, wrong IP
attributions, year numbers as ports).