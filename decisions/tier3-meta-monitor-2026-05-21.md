# Tier 3 Meta-Monitor — Architecture + Constraints (2026-05-21)

## Problem statement

The 3-tier loop architecture (Tier 1 milestone driver + Tier 2 anomaly monitor + Tier 3 GHA deterministic checks) has a self-correction gap:

**Tier 2 can intervene on Tier 1's milestone work** (writes INTERVENTION to LOOP_STATE.md), but **Tier 2 cannot patch its own driver code (SKILL.md) or Tier 1's driver (loop.md)** when it detects its own bug.

Real example surfaced 2026-05-21 11:36 UTC: Tier 2's wake correctly inferred via LOOP_STATE.md that the M6 validation sprint was active, BUT refused to write the pause sentinel because its spec required `gh` CLI confirmation — and the Cowork sandbox lacks `gh` auth. Tier 2 logged: `GH_CLI_UNAVAILABLE_in_Tier2_sandbox ... spec_requires_gh_confirmation_to_write_sentinel`. Detection worked. Action was gated on a missing dependency. **Self-aware failure mode, no self-repair.**

The meta-correction (rewriting SKILL.md to add a LOOP_STATE.md fallback signal) had to be done manually by the human via the Cowork session.

## Solution: Tier 3 meta-monitor

A FOURTH layer (numbered Tier 3 in this doc since the existing "GHA loop_monitor.yml" deterministic-check layer is reclassified as Tier 1.5) that:

1. Reads Tier 1 + Tier 2 + GHA monitor logs every 2h
2. Detects self-reported gaps (regex pattern matching)
3. Patches the offending driver file with a fix
4. Notifies user via email
5. Writes append-only audit log

## Hard constraints (NON-NEGOTIABLE)

| Constraint | Reason |
|---|---|
| Cadence ≤ once / 2h | Meta-corrections shouldn't happen often. Faster cadence wastes Cowork budget on no-op wakes. |
| Max 1 patch per 24h | Prevents flailing. If a single fix doesn't resolve the issue, human review needed before another patch. |
| Only patches LOAD-BEARING DRIVER files: `.claude/loop.md`, `Scheduled/jeeves-loop-watch/SKILL.md`, `scripts/loop_monitor.py` | Limits blast radius. Tests, source code, ROADMAP, LOOP_STATE all off-limits. |
| Never patches tests | Tests are ground truth. Bug in test = real bug, not driver bug. |
| Never patches `jeeves/*.py` source | Source-level bugs need human review. |
| Never patches LOOP_STATE.md / ROADMAP.md | Those are state files for Tier 1/2 to write. |
| Always emails user on patch action | Transparency. User wakes up to a notification list. |
| Always writes to `decisions/meta-patch-log.md` (append-only) | Audit trail. Independent of file revisions. |
| Self-test before patching: syntax check of the proposed file | Avoid breaking the patched file with malformed edits. |
| Tier 3 NEVER merges PRs | Boundary: Tier 3 patches files, human reviews + commits + pushes. The Cowork sandbox lacks git push auth anyway, so this is enforced architecturally. |
| Pattern match must cite specific log line | No speculation. "I saw line X containing pattern Y → patch Z" must be traceable. |

## Detection patterns (initial set)

Tier 3 scans `loop-watch.md` and `decisions/loop-audit-log.md` for these patterns:

| Pattern | Meaning | Likely patch target |
|---|---|---|
| `_UNAVAILABLE_in_.*sandbox` | Tier 2 lacks a CLI tool in its env | SKILL.md (add fallback) |
| `gh CLI fails\|gh.*unauthenticated` | gh dependency broken | SKILL.md or loop.md (add LOOP_STATE.md fallback) |
| `spec.requires.*but.*lacks` | Self-reported spec/env mismatch | The file that owns that spec |
| `cannot_act_on_detected` | Detection succeeded, action blocked | The action's spec doc |
| `would_have_acted_but` | Same as above | Same |
| `falls.through.*proceed_normally` | Fall-through default when expected action was blocked | The check that fell through |
| `same.milestone.attempted.*5\+.times` | Loop grinding past Tier 2's intervention threshold | loop.md STEP 3 retry logic |
| `wake_gate.timeout.*test_write_postprocess` | The known hanging test wedge keeps tripping STEP 0 | pyproject.toml + add explicit @pytest.mark.skip OR add file to skip list |

## What Tier 3 does NOT do

- Does NOT diagnose source code bugs (M-level work)
- Does NOT execute git commands
- Does NOT modify ROADMAP.md or LOOP_STATE.md
- Does NOT interpret ambiguous patterns — only acts on patterns in the table above
- Does NOT email user for routine "no anomaly" wakes (silent log only)

## Workflow

```
wake → read logs since last Tier 3 wake → pattern detect →
  if no match: append "OK" line to meta-patch-log.md, stop
  if match:
    if rate-limit hit (>= 1 patch in last 24h): append "RATE_LIMITED" line, email user with detected pattern but no patch, stop
    diagnose: which file to patch + what change
    propose patch (edit file in place — Cowork has filesystem write)
    self-test: python3 -c "syntax check" or markdown parse
    if self-test fails: revert, append "PATCH_FAILED" line, email user with attempted-patch + error, stop
    if self-test passes: keep patched file, append "PATCHED" line, email user with summary + file diff
  stop
```

## File targets

- **Tier 2 SKILL.md (`/Users/frederickyudin/Documents/Claude/Scheduled/jeeves-loop-watch/SKILL.md`)** — direct edit on user's Mac. Takes effect on Tier 2's next wake. No git involved.
- **Tier 1 loop.md (`/Users/frederickyudin/jeeves-unchained/.claude/loop.md`)** — edit working tree. Email user to commit + push. Takes effect on next wake AFTER human commits.
- **scripts/loop_monitor.py** — same as loop.md.

## Failure recovery for Tier 3 itself

If Tier 3 makes a bad patch that breaks Tier 1 or Tier 2:
- Tier 1's wake-gate (STEP 0 full-suite pytest) catches code-level breakage on next Tier 1 wake.
- Tier 2's STEP 0 early-exit catches its own runtime errors (it's a single-shot reasoning).
- Worst case: 30min-2h of broken loops, then human notices and reverts via `git revert`.
- Audit log at `decisions/meta-patch-log.md` tracks every patch, easy to find and revert.

## When Tier 3 needs to be turned off

- During a debugging session where you're hand-editing loop.md / SKILL.md
- When you suspect Tier 3 is patching wrong (oscillating patches, flailing pattern)
- During major refactors of the loop architecture
- Mechanism: Cowork scheduled tasks have an "enabled" flag. Flip to false.

## Audit log format (`decisions/meta-patch-log.md`)

Append-only. Format:
```
{ISO-timestamp} {OK|RATE_LIMITED|PATCHED|PATCH_FAILED|NO_NEW_LOGS} | files_scanned={N} | patterns_matched={list} | patched_file={path or none} | reason={1-line summary} | next_action={human or autonomous}
```

## Reviewable

Tier 3 SKILL.md is itself just a markdown file at `/Users/frederickyudin/Documents/Claude/Scheduled/jeeves-meta-monitor/SKILL.md`. Read it. Disagree with anything? Edit it.

This document describes the spec. The SKILL.md is the implementation.

## What Tier 3 CANNOT fix

The fundamental limit: Tier 3 cannot patch ITSELF. If Tier 3's pattern-detection logic is wrong, Tier 3 keeps making bad decisions. Mitigation: keep Tier 3 simple, prefer false negatives (miss bugs) over false positives (bad patches). 24h rate limit + email-on-action gives the user a fighting chance to spot a flailing Tier 3 before damage.

The architectural escape hatch: human. Always.

## 2026-05-21 strengthening pass

After initial spec, two hardening passes were applied:

### Pattern detection fortified

Replaced flat pattern table with 7-signal confidence scoring (PRESENCE + CURRENCY + RECURRENCE + SELF-REPORTED-AS-GAP + ACTION-FRUSTRATED + NO_HUMAN_OVERRIDE + NOT_IN_DOC_OR_COMMENT) and per-pattern CONFIDENCE FLOORS (3-4). A pattern is actionable only if score ≥ floor.

New defenses against false positives:
- Single-match-at-floor demotion (corroboration required)
- Novelty check (don't re-patch same pattern within 7 days)
- TIER3_IGNORE marker override (human escape hatch in logs)
- Patches confined to single STEP, max +20/-5 lines
- Authorship novelty (don't auto-patch a file recently auto-patched)
- Idempotency via signature grep (don't double-patch)
- Pre-patch file-state check (must compile/parse before AND after)
- Sanity diff post-edit (revert if actual diff exceeds plan)

### STEP 7 — sprint completion auto-transition

NEW action class, separate from STEP 4 patches. Only triggers when M6 validation sprint meets ALL three independent verification counts:

1. Count A: ≥12 session JSON files modified during sprint window (filesystem)
2. Count B: ≥9 successful daily.yml workflow runs (GHA via gh, if available)
3. Count C: avg ≥10/13 non-empty sectors per briefing (content quality)

Plus zero KILL_SWITCH deployments. All four gates must pass.

On gate-pass, Tier 3 atomically:
- Backs up LOOP_STATE.md + ROADMAP.md (filesystem copies with timestamps)
- Edits LOOP_STATE.md (iter++, M6 DONE, M7 set as Next Priority)
- Edits ROADMAP.md (flip M6 boxes to [x])
- Sets JEEVES_VALIDATION_MODE=0 via gh CLI (or notes pending if gh unavailable)
- Deletes the pause sentinel (Tier 1 resumes)
- Emails user with full transition summary + revert instructions

This is the ONE exception to the "never modify LOOP_STATE / ROADMAP" ban. The ban applies to STEP 4 (driver patching). STEP 7 has its own scoped exception, gated by triple-count verification.

Constraints:
- Max 1 TRANSITIONED per 24h (separate budget from PATCHED)
- STEP 4 and STEP 7 NEVER fire in the same wake (STEP 7 skips if PATCHED within 30min)
- M6 is the ONLY auto-transitionable milestone (M7-M9 require human judgment)
- Backups are taken BEFORE any edit; restore path is documented in the email

### Why STEP 7 is safe under the constraints

- Triple verification (filesystem + GHA + content quality) makes false positives unlikely
- Atomic backups + email-on-action means humans can revert in seconds
- 24h rate limit prevents flailing
- Scope-limited to M6 (the milestone with a clear quantitative success criterion)
- Even if STEP 7 fires wrongly, the worst outcome is "Tier 1 unpauses and starts M7 work prematurely" — recoverable via revert + re-setting MODE=1

### Run order

Every Tier 3 wake executes:
1. STEP 0 (duplicate-wake guard)
2. STEP 1 (read logs + sessions)
3. STEP 2 → 2.5 → 3 → 3.5 → 4 (patch flow, may short-circuit at any step)
4. STEP 5 (email if STEP 4 acted)
5. STEP 6 (mark wake)
6. **STEP 7 (sprint completion check, ALWAYS runs after STEP 6)**

Patch budget and transition budget are independent — a wake can fire ONE PATCHED and ONE TRANSITIONED, though in practice STEP 7 self-skips if STEP 4 fired within 30min.
