# JEEVES-UNCHAINED — Adaptive Goal Loop Driver
# Place at: /Users/frederickyudin/jeeves-unchained/.claude/loop.md
# Requires: Claude Code v2.1.72+ | /goal skill | gh CLI authenticated
# Start with: /loop 30m  (active diagnosis) or /loop 2h (background progress)

---

## STEP -1 — Pre-wake checks (mandatory; added 2026-05-21 after Anthropic usage-expiration incident + M6 validation sprint launch)

Two cheap checks that exit the wake before any expensive work. Both run BEFORE STEP 0. Cost: <2s.

### Check 1: pause sentinel (5h auto-recover from usage expiration)

Read `/Users/frederickyudin/jeeves-unchained/.claude/loop-pause-until`. If file exists AND contains an ISO timestamp greater than now (UTC):
1. Read the file's second line (reason, optional).
2. Print one line: `pre_wake: PAUSED until <iso> (reason: <reason or "manual">). Skipping iteration.`
3. Append the same line to `/Users/frederickyudin/jeeves-unchained/decisions/loop-audit-log.md` (create if missing).
4. Exit cleanly. Do not proceed to STEP 0.

If file timestamp ≤ now, delete the file (`rm -f .claude/loop-pause-until`) and proceed.

How the sentinel gets written:
- Automatically by THIS step if a previous iteration caught an Anthropic usage error (see STEP 4 hook below — uses two-window rule: overnight→next 6:30 AM PT, daytime→+5h rolling).
- Manually by the user (same two-window rule):
  ```bash
  python3 - <<'PYEOF' > .claude/loop-pause-until
  from datetime import datetime, timedelta
  from zoneinfo import ZoneInfo
  la = ZoneInfo('America/Los_Angeles')
  now = datetime.now(la)
  today_630 = now.replace(hour=6, minute=30, second=0, microsecond=0)
  if now.hour >= 21:
      target = today_630 + timedelta(days=1)
  elif now < today_630:
      target = today_630
  else:
      target = now + timedelta(hours=5)
  print(target.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ'))
  PYEOF
  echo "manual-pause" >> .claude/loop-pause-until
  ```
- Automatically by Tier 2 monitor (jeeves-loop-watch) if it detects a >2h gap with no Tier 1 progress.

Sentinel format (2 lines):
```
2026-05-21T15:00:00Z
anthropic-usage-expiration
```

### Check 2: sprint mode self-pause (validation sprint = Cerebras quota dedicated to daily.yml)

Run `gh variable get JEEVES_VALIDATION_MODE -R mmmichaelllang/jeeves-unchained 2>/dev/null` (1s timeout). If output equals `1`:
1. Print: `pre_wake: VALIDATION SPRINT ACTIVE (JEEVES_VALIDATION_MODE=1). Tier 1 self-pausing. Validation.yml dispatching daily.yml every 30min — do not compete for Cerebras quota.`
2. Append same line to `decisions/loop-audit-log.md`.
3. Exit cleanly.

If output equals `0` or empty: proceed normally (sprint not active, or sprint already concluded).

If `gh` CLI fails or auth broken: proceed normally (treat as sprint inactive — don't block Tier 1 on diagnostic-CLI failures).

### Hook: post-wake error catch (added to STEP 4 too — duplicated here for visibility)

If during this iteration's STEP 4 /goal invocation the model surfaces an Anthropic API error matching any of: `429 from Anthropic`, `rate_limit`, `usage limit`, `out of extra usage`, `resets at`, `daily message quota` — then BEFORE writing LOOP_STATE.md in STEP 5, write the pause sentinel using the two-window rule (Claude Max daily reset + 5h rolling windows):

```bash
python3 - <<'PYEOF' > .claude/loop-pause-until
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

la = ZoneInfo('America/Los_Angeles')
now_la = datetime.now(la)
today_630 = now_la.replace(hour=6, minute=30, second=0, microsecond=0)

# Two-window rule (user confirmed 2026-05-21):
#   overnight (9 PM - 6:30 AM PT) → anchor on next 6:30 AM PT (full daily reset)
#   daytime  (6:30 AM - 9 PM PT) → rolling +5h (Claude Max rolling window)
# Result: first exhaustion (typically overnight when user is asleep) anchors on the
# next daily reset. Subsequent same-day exhaustions roll forward by 5h, matching
# Claude Max's rolling-window cadence. "Continuous loop of tests and improvements
# until green" — cover every recovery window with minimal waste.

if now_la.hour >= 21:
    # 9 PM or later — anchor on tomorrow's 6:30 AM (overnight window crosses midnight)
    target_la = today_630 + timedelta(days=1)
elif now_la < today_630:
    # Past midnight, before today's 6:30 AM — anchor on today's 6:30 AM
    target_la = today_630
else:
    # Daytime (6:30 AM - 9 PM) — rolling 5h window
    target_la = now_la + timedelta(hours=5)

print(target_la.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ'))
PYEOF
echo "anthropic-usage-expiration" >> .claude/loop-pause-until
```

Behavioral examples (PT clock):
- Exhaustion at 4 AM (overnight) → wake at 6:30 AM today (2.5h pause, catches daily reset)
- Exhaustion at 11 PM (overnight) → wake at 6:30 AM tomorrow (7.5h pause, anchored on reset)
- Exhaustion at 9 AM (daytime) → wake at 2 PM (5h rolling)
- Exhaustion at 3 PM (daytime) → wake at 8 PM (5h rolling)
- Exhaustion at 7 PM (daytime, still <9 PM cutoff) → wake at midnight (5h rolling)

`zoneinfo` handles DST automatically (PDT May-Nov, PST Nov-Mar). The 9 PM cutoff is the practical boundary where +5h rolling would land between 2 AM and 6:30 AM — close enough to the daily reset that anchoring on 6:30 AM is strictly better.

---

## STEP 0 — Wake-gate (mandatory; added 2026-05-21 after M5 false-SUCCESS incident)

Defense against driver bypass. STEP 4.5 (post-goal verification gate) catches false SUCCESS within a single iteration, but only fires when /goal runs from /loop. If a milestone is completed manually (terminal session, /goal invoked outside /loop, ad-hoc edits) OR if a prior iteration's verification was skipped, LOOP_STATE.md can encode a false SUCCESS that the next wake would otherwise propagate. STEP 0 re-checks at every wake.

Run `timeout 300 uv run pytest tests/ --tb=short` from project root (`/Users/frederickyudin/jeeves-unchained`). Capture exit code and the last 25 lines of output.

**Hard-cap rationale (added 2026-05-21):** `timeout 300` is a bash-level safety net because the suite has a history of wedging indefinitely (one >10min hang during M5 regression bisect, root cause: a single test in test_write_* that never returns). pyproject.toml now installs `pytest-timeout` with `timeout=60` per-test, which is the proper fix — but until `uv sync` re-installs dev deps, the bash timeout is the last line of defense. 300s is generous: full suite ran in <90s when healthy, and 953 tests × 60s/test caps individual tests well before bash steps in.

**Exit code 124 (timeout fired) → treat as exit non-zero with Last Blocker = "Wake-gate suite-level timeout (>300s). Likely a single hanging test. Run with `--timeout=10 -x` to surface the first non-terminating test."**

**Wake-gate outcome rules:**

1. pytest exit 0 → state is consistent. Proceed to STEP 1 unchanged.
2. pytest exit non-zero → state-vs-reality drift. Before STEP 1 reads, REWRITE LOOP_STATE.md fields:
   - Read current LOOP_STATE.md.
   - If `Last Outcome == SUCCESS`: this is a false-SUCCESS to reverse. Set `Last Outcome = FAILED`. Set `Last Blocker = "Wake-gate detected stale-state regression. Failing tests: " + first 3 failing test names parsed from pytest output`. Increment `Same Blocker Count` by 1 (or set to 1 if this is the first occurrence). Override `Next Priority` verbatim: "Bisect last commit(s). Identify which milestone's commit introduced the failing tests. Fix on new branch `feat/m{N}-regression-fix` off main. Run `uv run pytest tests/ --tb=short` and confirm exit 0 BEFORE re-declaring SUCCESS." Append History row: "| <next iter> | wake-gate reversal | OVERRIDE: stale SUCCESS reverted | <failing tests summary> |". If the prior iteration's milestone was checked `- [x]` in ROADMAP.md, **UNCHECK** it back to `- [ ]`.
   - If `Last Outcome != SUCCESS`: state already encodes failure. Don't double-revert. Append failing test names to `Last Blocker` if not already present.
3. pytest collection error (exit 2) → treat as exit non-zero. `Last Blocker = "Wake-gate collection error: " + first error line`.

**Skip condition: NONE.** Runs every wake. Cost: ~30-60s per iteration (negligible at 30min cadence — ~2% overhead). The cost buys back trust in LOOP_STATE.md as a monitoring source.

**Print one line before STEP 1:**
```
wake_gate: pytest_exit=<code> last_outcome_before=<X> last_outcome_after=<Y> failures=<comma-sep test names or "none">
```

If wake-gate rewrote LOOP_STATE.md, STEP 1's read will see the corrected state and the rest of the iteration proceeds from the FAILED outcome (retry-with-alternative-approach logic in STEP 3).

---

## STEP 1 — Read prior state

Read `LOOP_STATE.md` in `/Users/frederickyudin/jeeves-unchained/`.

If the file does not exist → iteration 1, all defaults apply.

Extract:
- `iteration` — current count
- `last_milestone` — ID of last attempted (e.g. M0-A)
- `last_outcome` — SUCCESS / PARTIAL / FAILED
- `last_blocker` — specific error, test failure, or obstacle text
- `same_blocker_count` — consecutive identical blocker count
- `refined_done_when` — tightened criterion from last run
- `research_diagnosis` — if M0 complete: one of `NIM_429_CASCADE` | `TOOL_DISPATCH_FAIL` | `GHA_IP_THROTTLE` | `UNKNOWN`
- `next_priority` — explicit override (cleared after reading)

---

## STEP 2 — Read project state

Read `ROADMAP.md` in the project root.

Find the first unchecked milestone (`- [ ]`).

Skip any milestone whose PREREQUISITE milestone is not yet `- [x]`.
- M1-A and M1-B are mutually exclusive. Choose based on `research_diagnosis` in LOOP_STATE.md:
  - `NIM_429_CASCADE` → M1-A
  - `TOOL_DISPATCH_FAIL` or `GHA_IP_THROTTLE` → M1-B
  - `UNKNOWN` → block on M0 completion, do not attempt M1 yet

If all milestones `- [x]`:
> Print: "✅ JEEVES PROJECT COMPLETE — all milestones verified."
> Call CronList to find this loop task's ID (look for the task whose prompt matches loop.md or has no explicit prompt). Then call CronDelete with that ID to self-cancel this loop.
> Stop.

---

## STEP 3 — Adjust goal definition based on feedback

**Standard outcome rules (apply first):**

If last_outcome = NONE (iteration 1):
- Use milestone text + VERIFY command as the goal.

If last_outcome = SUCCESS:
- Mark the completed milestone `- [x]` in ROADMAP.md.
- Move to next eligible milestone. Reset same_blocker_count = 0.

If last_outcome = PARTIAL:
- Same milestone. Prepend: "Continue from where last iteration stopped.
  Specifically address: [last_blocker]. Previous progress: [evidence summary]."
- Tighten DONE WHEN to include: "AND [previously incomplete piece] is now resolved."

If last_outcome = FAILED, same_blocker_count = 1:
- Same milestone. Add pre-step: "Before attempting milestone, resolve: [last_blocker].
  Try an alternative approach from the one that failed."

If last_outcome = FAILED, same_blocker_count ≥ 2:
- Approach change mandatory. Prepend: "PREVIOUS 2+ ATTEMPTS FAILED on: [last_blocker].
  DO NOT repeat the same approach. Try: [alternative from milestone notes].
  If the blocker is environmental (missing dep, wrong key, CI-only issue),
  document it in LOOP_STATE.md next_priority and skip to next milestone."

**Jeeves-specific overrides (apply after standard rules):**

OVERRIDE-1 (diagnostic milestones M0-A / M0-B):
- These require `gh` CLI. Prepend: "Use `gh run view` and `gh api` commands.
  You have a workflow PAT. `gh auth token` works in non-TTY. Don't use git credential fill."
- If `gh` CLI not available in this session: document in LOOP_STATE.md as blocker,
  set next_priority to "User must run M0 commands manually from terminal. Log results in LOOP_STATE.md."

OVERRIDE-2 (any code change milestone):
- ALWAYS prepend: "Work on branch feat/[milestone-id]-[short-slug]. Never commit to main directly."
- ALWAYS append to DONE WHEN: "AND `uv run pytest tests/ --tb=short` exits 0 in project root."
- Run pytest BEFORE declaring SUCCESS. No exceptions.
- Use `uv run pytest` NOT `python -m pytest` or bare `pytest` — those fail in this env.

OVERRIDE-3 (write.py changes):
- ALWAYS read write.py line numbers from CLAUDE.md <nav> section before editing.
- NEVER raise max_tokens above 4096 for Groq parts (daily TPD budget constraint).
- NEVER change DEDUP_PROMPT_HEADLINES_CAP below 150 (sprint-17 calibration).
- DO NOT modify _clamp_groq_max_tokens() logic — it's carefully calibrated.
- After any write.py change: run `uv run pytest tests/test_write_postprocess.py tests/test_write_or_fallback.py tests/test_write_empty_guard.py -v`.

OVERRIDE-4 (research_sectors.py changes):
- NEVER parallelize triadic_ontology / ai_systems / uap sectors — stream-drop under load.
- NEVER raise max_tokens for deep sectors beyond 4096.
- NEVER add new sectors to the `no-quota-check` frozenset unless they genuinely use no quota tools.
- After any research_sectors.py change: run `uv run pytest tests/test_research_circuit_breakers.py tests/test_research_sectors.py -v`.

OVERRIDE-5 (NIM/API gotchas — always observe):
- tool_kwargs={} → NIM "Extra data" 400. Must json.dumps().
- All search tools return json.dumps(), never raw dict.
- tool_call.function.arguments=None → normalize to "{}", never leave None.
- is_function_calling_model=True must be set in constructor. Don't touch the NVIDIA method.

OVERRIDE-6 (schema.py changes):
- Adding fields to SessionModel or DeduplicateModel: always provide default_factory.
- Never rename existing schema fields — breaks the correspondence→research→write pipeline.
- After schema changes: run `pytest tests/test_schema.py`.

OVERRIDE-7 (daily.yml changes):
- Needs `workflow` PAT scope — confirmed available.
- Don't modify cron schedule (0 12 * * *) without explicit user instruction.
- Research job timeout must be ≥120 (bumped in PR #121 — don't revert).

**If next_priority is set (non-empty):**
- Override all above. Use next_priority verbatim as the goal.
- Clear next_priority in LOOP_STATE.md after reading.

**Print adjusted goal before running:**
```
ADJUSTED GOAL [iteration N]:
  Milestone: [ID + short description]
  Goal: [full text]
  DONE WHEN: [refined criterion]
  Pre-steps: [any pre-steps]
  Constraints active: [list OVERRIDE numbers that apply]
  Turn cap: 15
```

---

## STEP 4 — Run /goal

```
/goal [adjusted goal text]. DONE WHEN: [refined criterion]. Turn cap: 15.
```

Turn cap fixed at 15. Diagnostic milestones (M0) typically finish in 3-5 turns.
Code milestones (M1-M5) may use the full 15.

The DONE WHEN passed to /goal MUST include "AND `uv run pytest tests/ --tb=short` exits 0 in project root" per OVERRIDE-2 for any code-change milestone. Even so, /goal's self-reported SUCCESS is NOT final — STEP 4.5 (post-goal verification gate) is the source of truth for outcome. /goal may return SUCCESS after running target-file pytest only; the gate will reverse it if the full suite fails.

---

## STEP 4.5 — Post-Goal Verification Gate (mandatory; added 2026-05-21 after M5 false-SUCCESS incident)

History: M3 (iter 4), M4 (iter 5), M5 (iter 6) all declared SUCCESS via /goal self-report after running target-file pytest only (e.g. `tests/test_kill_switch.py -v` → 3/3). M5 commit `bb5520d` shipped 3 `test_research_sectors.py` regressions because the full suite was never run. OVERRIDE-2 mandated full-suite pytest but /goal ignored it under turn-cap pressure. This step removes the choice — verification happens here, outside /goal's control.

Run `timeout 300 uv run pytest tests/ --tb=short` from project root (`/Users/frederickyudin/jeeves-unchained`). Capture exit code and the last 25 lines of output. Bash-level `timeout 300` is the safety net (same rationale as STEP 0); per-test `--timeout=60` from pyproject.toml is the proper bound. Exit code 124 = suite-level timeout fired → treat as FAILED with Last Blocker = "Post-goal verification suite-level timeout (>300s). Single hanging test likely. Diagnose with `uv run pytest tests/ --timeout=10 -x --tb=short`."

**Outcome derivation rules (override /goal's self-report):**

- pytest exit 0 → outcome stands as /goal reported (SUCCESS / PARTIAL / FAILED).
- pytest exit non-zero AND /goal reported SUCCESS → **FORCE outcome = FAILED**. Set Last Blocker = "Full-suite regression. Failing tests: " + first 3 failing test names parsed from output. Note in LOOP_STATE History row: "M{N} false SUCCESS reverted by verification gate (commit <hash>)".
- pytest exit non-zero AND /goal reported PARTIAL/FAILED → outcome stays whatever /goal said, but APPEND "+ full-suite failures: " + failing test names to Last Blocker.
- pytest collection error (exit 2) → outcome = FAILED. Last Blocker = "pytest collection error: " + first error line.

**If outcome was overridden from SUCCESS → FAILED, additional cleanup before STEP 5:**

1. Override Next Priority verbatim: "Bisect M{N} commit <hash>. If M{N} is the cause, fix on new branch `feat/m{N}-regression-fix` off main (NOT on the batched branch). Run `uv run pytest tests/ --tb=short` and confirm exit 0 BEFORE re-declaring SUCCESS for M{N}."
2. If /goal already checked the ROADMAP milestone `- [x]`, **UNCHECK** it back to `- [ ]`. The milestone is not done.
3. If /goal already pushed a commit, DO NOT revert here — record the commit hash in Last Blocker. Bisect happens in the next iteration's M{N} retry.
4. If /goal already updated an open PR body to claim M{N} done, the next iteration's retry is responsible for correcting the PR body — note in Next Priority: "Also correct PR #<num> body to remove false SUCCESS claim for M{N}."

**Skip condition: NONE.** This gate runs every iteration. Diagnostic milestones (M0-A, M0-B) without code changes pass instantly (~30-60s) — that is the cost of invariant enforcement and it is non-negotiable. The loop has lied about SUCCESS three iterations running; one wasted minute per iteration buys back trust in LOOP_STATE.md as a monitoring source.

**Print one line before STEP 4.6:**
```
verification: pytest_exit=<code> outcome_from_goal=<X> outcome_after_gate=<Y> failures=<comma-sep test names or "none">
```

---

## STEP 4.6 — Apply Cadence Hint (enforced, added 2026-05-21; revised 2026-05-21 → uniform 30min)

Read `## Cadence Hint` section from LOOP_STATE.md. If section absent → skip this step (legacy compat).

**Target cron: `*/30 * * * *` (every 30 minutes) for ALL milestones M0-M9.**

Rationale (user decision 2026-05-21): no-op wake-ups during long milestones are nearly free. Faster wake-after-completion on short milestones is worth more than the cosmetic cost of mid-iteration wakes during long ones. Uniform cadence simplifies reasoning.

Skip cadence change if:
- LOOP_STATE.md `next_priority` is non-empty (intervention overrides cadence)
- Last outcome was FAILED with same_blocker_count > 0 (don't churn cadence during retry)
- All ROADMAP milestones are `- [x]` (project complete, no further iterations needed)

Apply target cadence:

1. Call `CronList` to find the loop's task ID. The loop task is the one whose prompt mentions `LOOP_STATE.md` or `.claude/loop.md`. There should be exactly one match.
2. Inspect that task's current `cronExpression`. If it already equals `*/30 * * * *`, no action — proceed to STEP 5.
3. If mismatch, call `CronUpdate` (preferred) OR `CronDelete` followed by `CronCreate` with the same prompt and the new cron `*/30 * * * *`. Preserve the task's prompt verbatim.
4. Print one line: `cadence_changed: prior=<old> target=*/30 * * * * milestone_next=<id>`.

If `CronUpdate` is unavailable in your tool set:
- `CronDelete` the existing task (capture its prompt + ID first).
- `CronCreate` a new task with the same prompt, cron `*/30 * * * *`.
- Print: `cadence_changed_via_recreate: prior=<old> target=*/30 * * * * new_id=<id>`.

Hard safety: if CronList returns more than one matching task, do NOT change cadence — print warning and skip. Manual cleanup required.

---

## STEP 5 — Write LOOP_STATE.md (and flip ROADMAP on gate-derived SUCCESS)

After STEP 4.5 verification gate completes, write `/Users/frederickyudin/jeeves-unchained/LOOP_STATE.md` using the **gate-derived outcome**, NOT /goal's self-report. If the gate forced SUCCESS → FAILED, every downstream field (Last Outcome, Last Blocker, Same Blocker Count, Refined DONE WHEN, Next Priority, History row) must reflect the FAILED state. Do not paper over the reversal — the History row should explicitly say "false SUCCESS reverted by verification gate" so the trail is auditable.

**STEP 5a — ROADMAP checkbox flip (mandatory on gate-derived SUCCESS):**

Do NOT trust /goal to have edited ROADMAP.md. STEP 3 says "If last_outcome = SUCCESS: Mark the completed milestone `- [x]` in ROADMAP.md" but observation shows /goal has skipped this consistently (every milestone from M2 onward stayed `- [ ]` in ROADMAP.md despite LOOP_STATE.md History claiming SUCCESS through M5). STEP 5a closes that gap.

On gate-derived SUCCESS (pytest exit 0 AND /goal returned SUCCESS), perform the ROADMAP flip explicitly:

1. Read `ROADMAP.md` from project root.
2. Locate the `### M<N>` header matching the milestone just completed (e.g. `### M5 — Refactor kill switch`).
3. Within that section (between the matched header and the next `### M` header), find the first line matching `^- \[ \]` and flip the `[ ]` to `[x]`. Do NOT flip every unchecked line — only the first one. The remaining `- [ ]` lines under that milestone represent sub-tasks that are part of the same milestone work and were verified together by the gate; flip them too IF AND ONLY IF the milestone's full DONE WHEN list was demonstrably satisfied by the gate run. Default behavior: flip ALL `- [ ]` lines within the milestone's section.
4. Edit `ROADMAP.md` with the flipped state.
5. If the milestone's section has no `- [ ]` lines (already fully checked), do nothing — the work was a no-op completion verification.
6. Print one line: `roadmap_flipped: M<N> section, <count> checkboxes set to [x]`.

On gate-derived FAILED, perform the reverse on any erroneous prior checkmarks:

1. If LOOP_STATE.md History shows the prior iteration as the SAME milestone with outcome SUCCESS, and the ROADMAP section for that milestone has any `- [x]` lines, the gate has just reversed a false SUCCESS — those checkmarks must come back to `- [ ]`. Edit ROADMAP.md accordingly.
2. Print one line: `roadmap_reverted: M<N> section, <count> checkboxes reset to [ ]`.

**STEP 5b — Audit log line:**

After STEP 5a, append one line to `/Users/frederickyudin/jeeves-unchained/decisions/loop-audit-log.md` (create if missing):
```
<ISO timestamp> | iter <N> | <milestone> | <outcome_after_gate> | roadmap_action=<flipped|reverted|none> | pytest_exit=<code>
```
This file is append-only — never edit past entries. It is the auditable trail of every milestone outcome the loop has produced, independent of LOOP_STATE.md (which is mutable).

```markdown
# JEEVES LOOP STATE
_Auto-managed. Do not edit during a run._

## Last Updated
[ISO timestamp]

## Iteration
[N]

## Last Milestone
[ID: e.g. M0-A — Pull run #70 research log]

## Last Outcome
[SUCCESS | PARTIAL | FAILED]

## Evidence
[Paste: pytest output tail, grep output, or VERIFY command result]

## Last Blocker
[Error text, missing dep, or obstacle — empty if SUCCESS]

## Same Blocker Count
[0 if outcome changed; increment if same blocker text as last iteration]

## Refined DONE WHEN
[What was actually verified — may be tighter than original]

## Research Diagnosis
[NIM_429_CASCADE | TOOL_DISPATCH_FAIL | GHA_IP_THROTTLE | UNKNOWN | PENDING]
[Set after M0-B completes. Leave PENDING until then.]

## Next Priority
[blank = follow ROADMAP order; set to override next goal; cleared on use]

## Active Branch
[feat/[milestone-id]-[slug] or "none" if diagnostic-only]

## Open PRs
[PR numbers and titles for anything awaiting merge]

## History
| Iter | Milestone | Outcome | Blocker summary |
|------|-----------|---------|-----------------|
[append one row per iteration — never delete rows]
```

---

## STEP 6 — Report

Print one summary line:
```
[timestamp] | Iter [N] | [Milestone ID] | [SUCCESS/PARTIAL/FAILED] | Next: [next milestone ID or DONE] | Branch: [name or —]
```

Then stop. Loop scheduler fires again at next interval.

---

## NEVER DO (project-wide hard stops)

- Never run any `git push` targeting main, and never merge a branch into main directly. Always open a PR and let CI gate it.
- Never declare SUCCESS without running `uv run pytest tests/ --tb=short` and confirming it exits 0.
- Never modify `sessions/*.json` committed files — those are pipeline artifacts.
- Never raise Groq max_tokens above 4096 or Groq TPM safety below 1200.
- Never remove quota guards or GATE-A from scripts/write.py.
- Never add redirect domains back to `_REDIRECT_ARTIFACT_HOSTS`.
- Never re-introduce the "K2.6 emits tool calls as text" misdiagnosis. Tool IDs
  like `functions.tavily_extract:5` are native K2.6 tool_call IDs, not text-form calls.
- Never build jeeves/model_router.py — it solves a phantom problem.
- Never run `scripts/write.py` without `JEEVES_DRY_RUN=1` unless the milestone explicitly requires a live email send. Before any write.py execution, confirm the command includes the dry-run flag.
- Never trigger a live GHA workflow run (`gh workflow run`) unless the milestone explicitly calls for it.

---

## Emergency recovery

If LOOP_STATE.md is corrupted or inconsistent:
- Delete it. Loop restarts at iteration 1.
- ROADMAP.md milestone checkboxes are the ground truth — preserved.
- Active branches survive independently.
- Resume from most recent handoff file in `.claude/handoffs/`.

If loop fires during a GHA run (pipeline is active):
- Take no action on research.py, write.py, or schema.py.
- Diagnostic reads only. Write LOOP_STATE.md with next_priority set for post-run action.
