# JEEVES-UNCHAINED — Adaptive Goal Loop Driver
# Place at: /Users/frederickyudin/jeeves-unchained/.claude/loop.md
# Requires: Claude Code v2.1.72+ | /goal skill | gh CLI authenticated
# Start with: /loop 30m  (active diagnosis) or /loop 2h (background progress)

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

---

## STEP 5 — Write LOOP_STATE.md

After /goal completes, write `/Users/frederickyudin/jeeves-unchained/LOOP_STATE.md`:

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
