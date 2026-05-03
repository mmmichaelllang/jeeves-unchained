---
name: tool-use-guardian
description: Harden tool call reliability in jeeves-unchained: structured retry wrappers, JSON repair, NIM 429 handling, Groq TPD exhaustion, and mid-chain failure recovery. Use when touching research_sectors.py, llm.py, or any code that calls NIM/Groq APIs.
---

# Tool-Use Guardian — Jeeves

## Core Patterns

### 1. Structured retry with typed errors
Always classify errors before retrying:
- NIM 429 → sector-level backoff (60s/120s), max 2 retries
- Groq TPD exhaustion → fall to NIM silently
- JSON parse failure → _json_repair_retry (LLM reformat)
- Tool call degenerate (id=None, name=None) → skip + warn

### 2. JSON repair order
`_try_normalize_json` runs 4 deterministic passes before LLM escalation:
1. python-repr → json (single→double quotes)
2. trailing-comma removal
3. truncation recovery (find last complete object)
4. bare-obj → array wrap

Escalate to `_json_repair_retry` only when all 4 fail.

### 3. Sentinel pattern
Return `_ParseFailed` (not None, not default) on structural failure so callers can distinguish "parse error" from "empty result".

### 4. Quota guard
Snapshot quota ledger before/after every sector agent run. Reject sector if no search provider was called (hallucination prevention).

### 5. Logging discipline
- DEBUG: partial JSON during streaming (floods WARNING otherwise)
- WARNING: NIM tool call degenerate, refine timeout, parse failure after all repairs
- ERROR: unrecoverable sector failure (no fallback available)
