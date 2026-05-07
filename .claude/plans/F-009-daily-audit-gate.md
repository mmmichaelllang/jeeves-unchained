# F-009 — Gate auditor commit on defect-count improvement

Branch: `fix/F-009-daily-audit-gate` (off `origin/main`)
Origin: forensic audit 2026-05-06 (`reports/00-master-findings.md` §F-009 + §F-011 + §F-012)
Status: planning -> implementation -> commit (push needs `workflow` PAT scope)

## Problem

`.github/workflows/daily.yml` auditor job has three coupled defects:

1. **F-011** — Step "Re-run audit on revised briefing" runs `scripts/audit.py --date "$DATE" --no-llm`, which OVERWRITES `sessions/audit-<date>.json` with post-fix counts. The pre-fix detection record is destroyed. On 2026-05-06 this meant we couldn't see which defects `fix_empty_with_data` had been triggered against.

2. **F-009** — The commit step is unconditional. No comparison of `pre_fix_defect_count` vs `post_fix_defect_count`. If the auditor MAKES THINGS WORSE — as it did on 2026-05-06 by splicing nemotron's chain-of-thought into Talk of the Town — the broken briefing still ships.

3. **F-012** (subsumed) — `JEEVES_AUDITOR_RESEND` only gates email re-send; it does NOT gate the commit. Even with resend=0, the broken briefing lands on main.

## Fix

**Pure workflow-file change.** No Python modification — preserves the `audit.py` CLI surface, which is widely depended on. Use shell file moves to rename outputs.

### Step "Re-run audit" — preserve pre-fix as `audit-<date>.json`, write post-fix to `audit-<date>.post-fix.json`

- Pre-existing audit JSON copied to `audit-<date>.original.json`
- `audit.py --no-llm` runs and overwrites `audit-<date>.json` with post-fix counts
- Renames: post-fix file -> `audit-<date>.post-fix.json`; original file -> back to `audit-<date>.json`

After this step:
- `sessions/audit-<date>.json` -> pre-fix (preserved, forensic record intact)
- `sessions/audit-<date>.post-fix.json` -> post-fix (new artifact)

### NEW step "Gate commit on auditor improvement"

Compares `.defects | length` between the two JSONs via `jq`. Three branches:

- post-fix file missing entirely -> `AUDITOR_REVERT=1` (treat as gate-failed, conservative)
- `post >= pre` -> `AUDITOR_REVERT=1`. `git checkout sessions/briefing-<date>.html` reverts the briefing to its pre-fix state. Audit JSONs still committed for forensic record.
- `post < pre` -> `AUDITOR_REVERT=0`. Keep the auditor's revisions.

### "Re-send" gate extended

`if:` clause now also requires `env.AUDITOR_REVERT != '1'`. If briefing was reverted, do not re-send email.

### "Commit" step changes

- Also stages `audit-fix-*.json` (was missing — fix log was uploaded as artifact but never committed)
- Commit message includes `(reverted — auditor regressed)` suffix when gate fired

## What this fix DOESN'T address

- **`JEEVES_AUDITOR_AUTO_FIX` gate itself** — that's the operator's lever. Setting it to `0` (current state per session start) bypasses all of this. Once F-001 + F-007 + F-009 land, AUTO_FIX can be safely re-enabled.
- **Severity weighting in defect counting** — `jq '.defects | length'` counts all defects equally. A future improvement: weighted comparison (e.g., critical defects worth 10x). Out of scope for F-009.
- **Auditor cycle convergence proof** — if auditor runs on briefing, makes change X, then on next cron runs again on the changed briefing, does it converge? No formal proof here. Empirically: the validators in F-001 + F-007 mean retries are no-ops on bad output, so no infinite-loop risk.

## Tests

`tests/test_audit_cli_signal.py` — two hermetic tests using `audit.run_audit` directly (no LLM):

1. `test_audit_defect_count_drops_when_section_filled` — pre-fix briefing has empty Library Stacks with `literary_pick.available=True` in session -> `empty_with_data` defect fires. Fill section with 50+ words -> defect drops out. Asserts post-fix count strictly less than pre-fix. Proves the gate has signal.

2. `test_audit_defect_count_unchanged_on_no_op_change` — cosmetic-only briefing change (extra newline) MUST NOT drop defect count. Asserts `post >= pre`. Proves the gate distinguishes real fixes from whitespace.

These are CLI-signal tests, not workflow-execution tests. The workflow `if:` chains can be smoke-tested manually via `gh workflow run daily.yml -f date=2026-05-06` after merge.

## Push path (BLOCKED on PAT scope)

This branch modifies `.github/workflows/daily.yml`. Per TK: PAT lacks `workflow` scope on this machine.

Options:
- **A.** Open PR via GitHub UI (web — works without workflow scope). Push the branch first via the patch path below; create PR through the website.
- **B.** Upgrade PAT to include `workflow` scope, then `git push 'https://ghp_<PAT>@github.com/...' fix/F-009-daily-audit-gate`.

A is faster; B is cleaner for future workflow changes.

## Acceptance

- `pytest tests/test_audit_cli_signal.py` -> 2 pass.
- After merge, manual `gh workflow run daily.yml` on a date where the auditor regressed shows the gate fires + revert happens, briefing.html is unchanged from pre-fix state in the resulting commit.
- `audit-<date>.post-fix.json` appears in the commit alongside `audit-<date>.json`.
- Commit message says "(reverted — auditor regressed)" when gate fires.
