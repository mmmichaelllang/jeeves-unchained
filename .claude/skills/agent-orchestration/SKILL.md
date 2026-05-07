---
name: agent-orchestration
description: Workload distribution for jeeves-unchained sector FunctionAgents. Use when modifying research phase parallelism, adding new sectors, or optimizing research wall-clock time. Covers: tiered semaphore strategy (heavy vs. light sectors), NIM rate limit awareness, and how to safely increase parallelism without triggering 429 cascades.
---

# Agent Orchestration — Jeeves

## Current Architecture
_SECTOR_SEMAPHORE_HEAVY=1 (deep sectors) / _SECTOR_SEMAPHORE_LIGHT=1 (light sectors).
Research wall-clock: ~33 minutes for ~14 sectors (sequential for-loop, no asyncio.gather).

## Tiered Parallelism Strategy

### NIM-heavy sectors (sequential — max_tokens=4096, stream-drop risk)
- triadic_ontology
- ai_systems
- uap
These sectors use 4096 max_tokens and have forced NIM retries. Running concurrently risks cascade 429s.

### Lightweight sectors (also sequential — see history)
- career, wearable_ai, biotech, space_exploration, cultural_currents, family, etc.
Sprint-19 slice E (PR #85, merged 2026-05-05) raised _SECTOR_SEMAPHORE_LIGHT to 2 and
dispatched light sectors in pairs via asyncio.gather. The next two production runs
(2026-05-06 12:13 UTC and 17:09 UTC) lost 4/8 and 6/8 light sectors respectively: NIM
free tier silently closes one or both streaming agents under concurrent Kimi calls,
and the loser sector exhausts its 60+120s rate-limit retry budget and returns
spec.default. Reverted same day. Rule: do not re-attempt pair-concurrency on NIM free
tier.

## Semaphore Implementation
```python
# Heavy sectors: _SECTOR_SEMAPHORE_HEAVY = asyncio.Semaphore(1)
# Light sectors: _SECTOR_SEMAPHORE_LIGHT = asyncio.Semaphore(1)

_DEEP_SECTOR_NAMES = frozenset({"triadic_ontology", "ai_systems", "uap"})

def _sector_semaphore(sector_name: str) -> asyncio.Semaphore:
    """Return the appropriate asyncio.Semaphore for this sector's weight class."""
    return _SECTOR_SEMAPHORE_HEAVY if sector_name in _DEEP_SECTOR_NAMES else _SECTOR_SEMAPHORE_LIGHT
```

## Key Constraint: prior_sample dependency
The sequential for-loop in `_run_sector_loop` (scripts/research.py) exists because each sector
must see URLs discovered by prior sectors (prior_sample grows after each completion). This
prevents true concurrent execution without batching. Any parallelism refactor must preserve
this property or cross-sector URL dedup breaks.

## Safety constraint: NIM free tier
Do not raise either semaphore above 1 on NIM free tier.
A 429 on a deep sector costs 60-120s backoff; lost streaming agents in light pairs
silently empty the sector. Both have been observed in production.
Historical:
- semaphore=3 (early experiment) → all sectors returned defaults in <1 min.
- _SECTOR_SEMAPHORE_LIGHT=2 + pair-gather (sprint-19 slice E, 2026-05-05) →
  4-6 of 8 light sectors empty per run; reverted 2026-05-06.
