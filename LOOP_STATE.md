# JEEVES LOOP STATE
_Auto-managed. Do not edit during a run._

## Last Updated
2026-05-21T21:15:00Z (iter 9 — cowork session: round 7 fixes, commit ff3e13e, awaiting user push)

## Iteration
9 (M6 validation sprint — in progress)

## Last Milestone
M6 IN-PROGRESS — round 7 cowork fixes (commit `ff3e13e` on main, awaiting user push + research.yml trigger).

## Last Outcome
IN_PROGRESS

## Evidence
```
PR #136 merged (squash commit 6c73150):
  M0-M5 code + validation.yml + loop_monitor.py on main
  802/805 tests passed (3 pre-existing subprocess timeout failures, unrelated to M0-M5)

validation.yml enabled:
  gh workflow list → "Validation Sprint  active  280839531"
  GH Variables: JEEVES_USE_CRAWL4AI_RESEARCH=1, JEEVES_USE_CRAWL4AI_FETCH=1 (both set)
  First run fires within 30min of enable

round 7 cowork fixes (commit ff3e13e, 2026-05-21 ~21:00 UTC):
  Diagnosed from research.py run log (2026-05-21 20:38-20:49 UTC):

  BUG 1 (EXIT-1 BLOCKER): enriched_articles Pydantic crash.
    OR :floor returned flat URL strings list instead of EnrichedArticle dicts.
    _parse_sector_output enriched shape didn't filter non-dict entries.
    save_session → SessionModel.model_validate() → 5 validation errors → exit 1.
    FIX: filter bare strings in _parse_sector_output before text-cap loop.

  BUG 2: triadic_ontology → spec.default (llama3.1-8b ctx crash, no OR fallback).
    After gpt-oss-120b+qwen-3+zai-glm all 429d, _resolve_cerebras_model fallback
    picked llama3.1-8b from remaining=sorted(available-TRIED) alphabetically.
    llama3.1-8b: 400 ctx exceeded (9536 > 8192) → else branch → returning default.
    Round 5 removed llama3.1-8b from _CEREBRAS_MODEL_CHAIN but not from remaining fallback.
    FIX: added _CEREBRAS_CTX_BANNED=frozenset({"llama3.1-8b"}); filter in remaining fallback.

  BUG 3: global_news → spec.default (Connection error not rotatable in crawl4ai OR phase).
    _is_retryable_network_error didn't match "Connection error." (httpx.ConnectError).
    rotatable=False → returned default instead of rotating to mistral-small:floor.
    FIX: added "connection error" phrase; wired _is_retryable_network_error into
         crawl4ai OR rotation condition.

  4 new hermetic tests.
  NOTE: tests NOT run locally (sandbox disk full).
  VERIFY: uv run pytest tests/test_research_sectors.py -x -q
  User action required: git pull --rebase && git push && gh workflow run research.yml
```

## Last Blocker
Exit-1 Pydantic crash on enriched_articles (fixed in ff3e13e, awaiting push + verification).

## Same Blocker Count
0

## Refined DONE WHEN
M6 done when:
  1. ≥9/12 validation.yml runs produce non-empty briefings.
  2. Zero KILL_SWITCH deployments across the sprint window.
  3. Average ≥10/13 populated sectors per non-empty briefing.
  VERIFY: `python scripts/health_check.py --window 12 --source validation 2>&1 | grep -E "non_empty|KILL_SWITCH|avg_sectors"`

## Research Diagnosis
FREE_TIER_CAPACITY_CEILING (Cerebras + OR cannot deliver 70-200 agent calls/run; structural refactor required, not retries)

## Next Priority
Monitor validation.yml sprint. After 12 runs check health_check.py output.
If ≥9/12 pass → mark M6 done, disable validation.yml cron, resume daily.yml as steady-state.
If <9/12 → diagnose per-run failures, check KILL_SWITCH triggers.

## Active Branch
main

## Open PRs
None.

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
| 5 | M4 | SUCCESS | _resolve_cerebras_model + _rotate_on_429 in research_sectors.py; 4/4 tests passing |
| 6 | M5 | FALSE SUCCESS → FAILED | self-reported SUCCESS on tests/test_kill_switch.py 3/3; reverted by Tier 2 monitor 2026-05-21 — full suite has 3 test_research_sectors.py regressions; commit bb5520d shipped to feat/m6-acceleration-and-monitors before detection |
| 7 | M5 retry | FAILED→BLOCKED | root cause was Playwright sync-API loop leak in TOTT test contaminating test_research_sectors.py (false positives) |
| 8 | M3 asyncio fix | SUCCESS | PR #137 merged (502f1be): _run_crawl4ai_sync + canary fixture + TOTT playwright mock; M5 confirmed non-regressing; feat/m6 rebased on main |
| 9 | M6 validation sprint | IN_PROGRESS | PR #136 merged (6c73150); validation.yml enabled; GH Variables set; sprint running |
| 9 | M6 round 7 cowork fix | IN_PROGRESS | 3 bugs fixed in commit ff3e13e: enriched_articles exit-1 Pydantic crash, llama3.1-8b ctx-banned from Cerebras fallback, Connection error now rotatable in crawl4ai OR phase. Tests unverified locally (disk full). Awaiting user push + next research.yml run. |

## Refactor Phase
M6 (Validation sprint)

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
