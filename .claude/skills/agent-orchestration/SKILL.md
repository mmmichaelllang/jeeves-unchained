---
name: agent-orchestration
description: Workload distribution for jeeves-unchained sector FunctionAgents. Use when modifying research phase parallelism, adding new sectors, or optimizing research wall-clock time. Covers: tiered semaphore strategy (heavy vs. light sectors), NIM rate limit awareness, and how to safely increase parallelism without triggering 429 cascades.
---

# Agent Orchestration — Jeeves

## Current Architecture
_SECTOR_SEMAPHORE_HEAVY=1 (deep sectors) / _SECTOR_SEMAPHORE_LIGHT=2 (light sectors).
Research wall-clock: ~15 minutes for 14 sectors (sequential for-loop, not asyncio.gather).

## Tiered Parallelism Strategy

### NIM-heavy sectors (keep sequential — max_tokens=4096, stream-drop risk)
- triadic_ontology
- ai_systems  
- uap
These sectors use 4096 max_tokens and have forced NIM retries. Running concurrently risks cascade 429s.

### Lightweight sectors (safe for semaphore=2)
- career
- wearable_ai
- biotech
- space_exploration
- cultural_currents
These sectors are shorter, don't use deep-sector token allocation, and rarely trigger rate limits.

## Semaphore Implementation
```python
# Heavy sectors: _SECTOR_SEMAPHORE_HEAVY = asyncio.Semaphore(1)
# Light sectors: _SECTOR_SEMAPHORE_LIGHT = asyncio.Semaphore(2)

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
Never exceed semaphore=2 for any sector group on NIM free tier.
A 429 on a deep sector costs 60-120s backoff, erasing any parallelism gain.
Historical: semaphore=3 caused all sectors to return defaults in <1 min (NIM 429 cascade).
