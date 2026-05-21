# JEEVES LOOP STATE
_Auto-managed. Do not edit during a run._

## Last Updated
2026-05-21T05:30:00Z

## Iteration
3 (M2 build — research synthesis for news_short sectors)

## Last Milestone
M1 + M1.5 DONE (2026-05-21) — crawl4ai_extract.py + host classifier, 11/11 tests passing

## Last Outcome
SUCCESS

## Evidence
```
M0 probe: decisions/crawl4ai-probe-2026-05-20.md — combined=0.71 → REVISE M1-M3
Design revision: decisions/m0-followup-design-revision-2026-05-21.md
ROADMAP.md updated: M1 narrowed (host classifier), M1.5 added, M2/M3 narrowed to news_short sectors
User accepted design 2026-05-21 with content-type-aware cascade.
```

## Last Blocker
None — design revision accepted, advancing to M1.

## Same Blocker Count
0

## Refined DONE WHEN
M2 complete: `JEEVES_USE_CRAWL4AI_RESEARCH=1` plumbed through `scripts/research.py` + `research_sectors.py`. `_CRAWL4AI_ELIGIBLE_SECTORS` defined. `pytest tests/test_research_sectors.py` exits 0. FunctionAgent path preserved (no deletions).

## Research Diagnosis
FREE_TIER_CAPACITY_CEILING (Cerebras + OR cannot deliver 70-200 agent calls/run; structural refactor required, not retries)

## Next Priority
1. Build M2: add `JEEVES_USE_CRAWL4AI_RESEARCH=1` flag in `scripts/research.py` + `jeeves/research_sectors.py`.
2. Define `_CRAWL4AI_ELIGIBLE_SECTORS = {local_news, global_news, weather, career, family, wearable_ai}`.
3. New code path: when flag=1 AND sector in eligible set → `crawl4ai_extract` each URL → Cerebras synthesis.
4. Deep sectors (triadic_ontology, ai_systems, uap) → keep FunctionAgent unconditionally.
5. Write/update `tests/test_research_sectors.py` to cover flag=0 (existing path) and flag=1 (new path).
6. `uv run pytest tests/test_research_sectors.py -q` → 0 failures.
7. VERIFY: `grep -n "JEEVES_USE_CRAWL4AI_RESEARCH" scripts/research.py jeeves/research_sectors.py`

## Active Branch
main (loop creates feat/M{N}-* branches per milestone)

## Open PRs
[none — PR #133, #134, #135 all merged today]

## History
| Iter | Milestone | Outcome | Blocker summary |
|------|-----------|---------|-----------------|
| -1 | M1-A/B/C (NIM era) | SUPERSEDED | NIM removed in PR #133 (2026-05-21) |
| 0 | bootstrap (Crawl4AI refactor) | DESIGN ACCEPTED | refactor design accepted by user 2026-05-21 |
| 1a | M0 probe (attempt 1) | HALTED BY USER | stop rule violated: replaced paywalled URLs, score 0.36 |
| 1b | M0 probe (attempt 2) | STOP → DESIGN REVISION | combined=0.71; user accepted content-type-aware cascade; ROADMAP narrowed |
| 2 | M1 + M1.5 | SUCCESS | crawl4ai_extract.py + classify_host + host sets; 11/11 tests passing |

## Refactor Phase
M0 (Probe Crawl4AI on jeeves targets)

## Hardening Constraints (from /challenge — MUST honor)
- All old code paths preserved behind feature flags for ≥30 days
- `JEEVES_REFACTOR_KILL_SWITCH=1` provides instant reversion
- No production code shipping until M0 probe shows quality ≥0.8
- Each milestone PR-sized; no batching multiple milestones into one PR
- Tests required at every M-level
- Feature flag default: OFF in repo Variables; user enables manually after probe

## Loop Behavior
- Each iteration: read this file → identify next M from ROADMAP → execute → verify → update this file
- Iteration cap: 30 (covers M0 through M9 with retry headroom)
- Turn cap per iteration: 25
- Stop and emit USER ACTION REQUIRED if:
  - Any test file hangs >90s
  - LOOP_STATE branch field doesn't match `git branch --show-current`
  - Any KILL_SWITCH condition from ROADMAP.md is met
  - Verifier requires API key not provisioned (e.g., Charlotte before M7)

## Cadence Hint (added 2026-05-21)
Consider re-firing `/loop 30m` for short milestones (M4 model rotation, M5 kill switch — each ~20min of focused coding). Keep `/loop 60m` for longer milestones (M2 research integration, M3 fetch cascade — each ~1-2h). Goal: align loop wake cadence with iteration duration so cron fires shortly after the prior iteration completes, not while it's still running. M6 validation sprint is its own cadence (30min via validation.yml). User on Claude Max — cost not a constraint, optimize for wall-clock speed.
