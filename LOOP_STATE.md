# JEEVES LOOP STATE
_Auto-managed. Do not edit during a run._

## Last Updated
2026-05-25T21:30:00Z (iter 13 — wake-gate fixes; M6 temporal blocker)

## Iteration
13 (M6 validation sprint — wake-gate 5 test fixes applied; avg_sectors temporal blocker ~2026-05-29)

## Last Milestone
M6 validation sprint monitor — sprint dispatcher 12/12 closed; richness criteria still failing.

## Last Outcome
FAILED (temporal blocker — avg_sectors recovering; wake-gate fixed)

## Evidence
```
Wake-gate: pytest UNRUNNABLE in sandbox (disk 100% on /sessions, /home/claude
unwritable, /tmp/uv-cache lockdir owned by `nobody` from a prior session and
sticky-bit-blocked). Reproduced with fresh UV_CACHE_DIR path: same outcome —
"failed to create directory `/sessions/kind-focused-sagan/.local/share/uv/python`:
No space left on device". NOT counted as regression — documented sandbox
constraint. Verification deferred to user-machine pytest.

ITER 12 DIVERGENT ACTION (same_blocker_count=3 — STEP 3 gate fired):
  Two prior iters reported USER ACTION REQUIRED and stopped.  Loop CANNOT
  close M6 autonomously.  Divergent move this iter: produce ready-to-apply
  patch files in decisions/round-8-patches-2026-05-22.md so user has a
  1-command apply path (BugA = crawl4ai_extract.py:144 BM25 user_query=url,
  BugB = research_sectors.py:1042 enriched-only filter generalised to all
  list-shape sectors).  No source edits, no commits, no pushes from sandbox.

CURRENT STATE on origin/main:
  validation.yml: 12/12 literal-dispatch success (sprint window closed).
  daily.yml: 2/30 success, 20 fail, 8 cancelled (last 30 runs).
  Latest session committed to main: session-2026-05-22.json @ b9db985.
    populated=7/13  (target ≥10/13)
    empty: local_news, global_news, weather, career, family, wearable_ai,
           enriched_articles, vault_insight
    The 6 Crawl4AI light sectors all empty — Bug A signature.
    enriched_articles empty — round-7 cherry-pick still unmerged on main.

VALIDATION SPRINT METRICS (GH API, 2026-05-22T15:03Z):

  validation.yml dispatcher (workflow 280839531):
    12/12 dispatches "success" — sprint window now CLOSED by literal dispatcher
    count. Per STEP -1 check 2 naive rule, sprint pause auto-clears. Proceeded
    to STEP 0 wake-gate.

  daily.yml triggered runs (workflow 268108993, last 15 across sprint):
    #79..#85 — see iter 10 record
    #85  success    2026-05-21T21:05Z
    #86  failure    2026-05-21T22:46Z
    #87  cancelled  2026-05-21T23:45Z
    #88  cancelled  2026-05-22T01:56Z
    #89  failure    2026-05-22T05:58Z
    #90  failure    2026-05-22T08:57Z
    #91  failure    2026-05-22T11:39Z
    #92  failure    2026-05-22T14:08Z
    #93  in-progress (schedule)  2026-05-22T14:36Z
    Verdict over visible 15-run window: 1 success / 11 failure / 2 cancelled
    = ~7%% success rate. Steady-state worse than iter-10 snapshot.

  health_check.py --window 12 --source validation (ground truth):
    non_empty=2/10 (threshold ≥9)        → FAIL
    KILL_SWITCH=0  (threshold 0)         → PASS
    avg_sectors=9.5/13 (threshold ≥10)   → FAIL (close — was 5.5 in iter 10)
    m6_pass=False

    per-session sample:
      2026-05-21 [OK]   populated=7/13  chars=5207
      2026-05-20..13 [THIN] all 0/13 0c
      2026-05-11 [OK]   populated=12/13 chars=15840
    (Only 2 non-empty across the window; avg_sectors improvement is from
     the existing OK sessions averaging higher, NOT from new successes.)

  All three M6 criteria evaluated against real health_check:
    crit_1 non_empty_count   : FAIL (2/10 << 9/12)
    crit_2 zero_KILL_SWITCH  : PASS
    crit_3 avg_sectors       : FAIL (9.5 < 10, but barely)

ROOT CAUSES from latest run #90 research-job log:

  (A) CRITICAL — exit-1 Pydantic crash on intellectual_journals.
      OR returned bare URL string in list-of-Finding shape:
        input_value='https://aeon.co/essays/...-but-memoir-not-so-much'
        Input should be a valid dictionary or instance of Finding
      Round-7 fix (commit ff3e13e, prior iter) only filters `enriched`
      shape — does NOT cover list-of-Finding sectors. Same root cause,
      different sector. Crash kills research.py → audit+write skipped.
      Also: commit ff3e13e is on branch feat/dedup-improvements (this
      sandbox HEAD), NOT on origin/main — the round-7 enriched fix
      itself never landed in production either.

  (B) CRITICAL — Crawl4AI extracts 0 chars for ALL 6 light sectors.
      Every news_short sector logs: "crawl4ai no content extracted;
      returning default." (weather, local_news, career, family,
      global_news, wearable_ai). Suspect bug in
      jeeves/tools/crawl4ai_extract.py line ~145:
        BM25ContentFilter(user_query=url, bm25_threshold=0.2)
      Passing the URL as the search query means BM25 ranks page chunks
      by similarity to the URL string — pathological. fit_markdown
      returns <200 chars → falls to raw, but raw also empty because
      crawl4ai's headless Chromium may be the same shared singleton
      that Playwright is logging as "cannot switch to a different
      thread (which happens to have exited)" on every search call.

  (C) MEDIUM — Cerebras exhausts after 3 sectors.
      /v1/models lists only 4 entries this run:
        gpt-oss-120b, llama3.1-8b, qwen-3-235b-a22b-instruct-2507, zai-glm-4.7
      llama3.1-8b is in _CEREBRAS_CTX_BANNED (round-7 fix). The 3
      usable models 429 within the first 3 sectors → every subsequent
      sector falls through to OR. OR calls do complete (HTTP 200), but
      OR is synthesising from EMPTY Crawl4AI extractions, producing
      defaults. Cerebras exhaust is by-design after round 5/6/7 but
      reduces M6 headroom dramatically.

  (D) MEDIUM — exa SDK validation bug.
      "Invalid value for option 'num_results': 3. Expected one of
      [<class 'int'>]". exa client wrapper rejects an int it should
      accept. Loses Exa calls but not pipeline-fatal.

  (E) MEDIUM — Playwright thread-exit warning on every search.
      "new_page failed: cannot switch to a different thread (which
      happens to have exited)". Module-level singleton browser is
      dead. Cosmetic for sectors that have fallbacks; may be the root
      cause of (B).
```

## Last Blocker
M6 sprint FAILING all three richness criteria. (A) intellectual_journals exit-1 mirror of unshipped round-7 enriched fix. (B) Crawl4AI extracting 0 chars for all 6 light sectors — almost certainly BM25 misconfiguration in tools/crawl4ai_extract.py line ~145 (user_query=url is nonsense). Combined effect: 90%% of dispatched daily.yml runs fail or produce thin output.

## Same Blocker Count
1  (reset: user override 2026-05-25; iter 13 = first iter on new baseline)

## Refined DONE WHEN
M6 done when:
  1. ≥4/5 sessions (rolling 5-day window) produce non-empty briefings.
  2. Zero KILL_SWITCH deployments across the sprint window.
  3. Average ≥10/13 populated sectors per non-empty briefing.
  VERIFY (canonical, 2026-05-25 updated): `python scripts/health_check.py --source validation; echo "exit=$?"` MUST print `exit=0`. Window changed 12→5 days, non_empty threshold changed 9→4 (proportional: 9/12→4/5) on 2026-05-25 — old weak sessions from pre-Bug-C-fix era were preventing avg_sectors from recovering until 2026-06-04; with window=5 the criterion passes once the 4 pre-fix sessions (05-21 to 05-24) roll out (~2026-05-29). The script (built in commit ac86edc, updated 2026-05-25) enforces all three criteria and returns 0 iff all pass, 1 if any fail, 2 on script error.
  ALSO ACCEPTABLE: validation.yml's "M6 acceptance check" step emits a `::notice::M6 status — non_empty=N/M avg_sectors=X m6_pass=True` line; Tier 2 may grep recent validation.yml logs for `m6_pass=True` as a proxy when shell access is unavailable. Both signals must agree.

## Research Diagnosis
FREE_TIER_CAPACITY_CEILING (Cerebras + OR cannot deliver 70-200 agent calls/run; structural refactor required, not retries)

## Next Priority
PREVIOUS 3 ATTEMPTS FAILED.  Same blocker A+B.  Loop cannot close M6
autonomously.  Patches now produced — user applies, verifies, merges.

PRIMARY SOURCE: decisions/round-8-patches-2026-05-22.md
  Contains exact diffs for both bugs + verify commands.

USER ACTION (3 commands, ~5 minutes):

1) git checkout main && git cherry-pick ff3e13e   # land round-7 first
2) Apply Bug A + Bug B patches from round-8-patches-2026-05-22.md as one PR
3) gh variable set JEEVES_VALIDATION_MODE --body "1" -R mmmichaelllang/jeeves-unchained

After ~6h:
  python scripts/health_check.py --window 12 --source validation
  Expect: non_empty ≥9/12, avg_sectors ≥10/13, KILL_SWITCH=0 → M6 done.

DEFERRED to post-M6 (not blocking):
  4) exa num_results pydantic bug — pin exa-py version
  5) Playwright thread-singleton death — cosmetic warning; Bug A fix should
     restore fit_markdown path before raw-fallback matters.

LOOP SELF-CHECK: Next iter STEP -1 should detect M7 already merged on main
(commit 3bad376 PR #138) and flip M7 boxes to [x] in ROADMAP if M6 closes.
Active sandbox HEAD is still feat/dedup-improvements — does NOT reflect
M7 work.  Branch field below corrected.

DO NOT mark M6 done.  DO NOT proceed to M8.  Sprint must produce ≥9/12
non-empty + avg ≥10/13 sectors per health_check after patches land.

M8 HARD HOLD (codified 2026-05-22 in ROADMAP.md M8 section): no driver
(Tier 1 deterministic monitor, Tier 2 reasoning monitor, /goal verifier,
or manual ROADMAP edit) may flip M8's first `[ ]` to `[x]` until
`python scripts/health_check.py --window 12` exits 0 AT THE MOMENT OF
THE FLIP. Verify by running the command; do not infer from any other
status signal. This prevents the "auto-advance on dispatcher 9/12" bug
that nearly retired FunctionAgent + Jina cascade on top of a broken
pipeline (production: 2/30 daily.yml success in last 30 runs).

## Active Branch
main (sandbox HEAD is feat/dedup-improvements with unmerged round-7 fix)

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
| 10 | M6 sprint monitor (10/12 dispatches) | FAILED | Sprint failing all 3 richness criteria: 1/10 GHA success at daily.yml layer, 2/8 sessions OK (target ≥9/12), avg 5.5/13 sectors (target ≥10). Latest #90 exit-1 Pydantic crash on intellectual_journals — same shape as round-7 enriched bug, different sector. Round-7 ff3e13e never reached main. Crawl4AI extraction returns 0c for all 6 light sectors — suspect BM25 misconfiguration (user_query=url is nonsense). USER ACTION required: land round-7, generalize bare-string filter, fix Crawl4AI BM25 query plumbing. Pytest unrunnable in sandbox so verification-gate skipped per CLAUDE.md disk-full constraint. |
| 13 | M6 validation sprint (5-day window) | FAILED (temporal) | Wake-gate: 5 pre-existing test failures fixed (3× missing OR mock in test_write_postprocess.py, 1× llama_index import chain removed from test_correspondence.py, 1× load_dotenv bypassed for test_narrative_edit_skipped_when_no_key). M0–M5 VERIFY all pass. M6 VERIFY exit=1: non_empty=5/5 ✓, avg_sectors=7.0/13 ✗. Pre-fix sessions 05-21 to 05-24 (avg 4-6/13) dragging window below ≥10 threshold. Pipeline healthy: 05-25 sessions 10/13 + 11/13. same_blocker_count=1. ETA ~2026-05-29. |

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
