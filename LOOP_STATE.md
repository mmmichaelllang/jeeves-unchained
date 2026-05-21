# JEEVES LOOP STATE
_Auto-managed. Do not edit during a run._

## Last Updated
2026-05-21T08:00:00Z

## Iteration
5 (M4 build — Cerebras model rotation on 429)

## Last Milestone
M3 DONE (2026-05-21) — JEEVES_USE_CRAWL4AI_FETCH=1 plumbed into jeeves/tools/enrichment.py. Crawl4AI inserted as TIER 2 for news_short hosts. 3/3 tests passing in tests/test_enrichment.py.

## Last Outcome
SUCCESS

## Evidence
```
M3 verify: grep -n "JEEVES_USE_CRAWL4AI_FETCH" jeeves/tools/enrichment.py → 2 matches
M3 verify: uv run pytest tests/test_enrichment.py -q → 3 passed
```

## Last Blocker
None.

## Same Blocker Count
0

## Refined DONE WHEN
M4 complete: `_resolve_cerebras_model` + `_rotate_on_429` added to `jeeves/research_sectors.py`. On Cerebras 429, rotate to next model in list rather than bailing. Tests pass.

## Research Diagnosis
FREE_TIER_CAPACITY_CEILING (Cerebras + OR cannot deliver 70-200 agent calls/run; structural refactor required, not retries)

## Next Priority
1. Read ROADMAP.md M4 section.
2. Build M4: Cerebras model rotation — `_resolve_cerebras_model` selects from list; `_rotate_on_429` advances index on 429.
3. Tests in tests/test_research_sectors.py or new file.
4. After M3 PR lands: set `JEEVES_USE_CRAWL4AI_FETCH=1` GH Variable.

## Active Branch
feat/m6-acceleration-and-monitors

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
| 3 | M2 | SUCCESS | JEEVES_USE_CRAWL4AI_RESEARCH=1 + _run_crawl4ai_sector; 82/82 tests passing |
| 4 | M3 | SUCCESS | JEEVES_USE_CRAWL4AI_FETCH=1 + Crawl4AI TIER 2 in enrichment.py; 3/3 tests passing |

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
