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

See `cron/yaml-review-prompt.txt` for the full prompt (3330 chars).

**Schedule args:** `0 8 * * 1`, deliver to origin, no skills.