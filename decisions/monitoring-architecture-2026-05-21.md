# Adaptive-Loop Monitoring Architecture (2026-05-21)

## Why

User running Sonnet 4.6 Claude Code in terminal for adaptive-loop execution.
Cowork session (Opus 4.7) needs to monitor without burning Opus tokens on
routine polling. Three-tier escalation pattern: cheap deterministic checks
catch most issues; Opus reasoning reserved for fuzzy/narrative drift; user
paste reserved for deep design conversation.

## Architecture

### Tier 1 — GHA cheap monitor (zero LLM tokens, fully autonomous)

**Frequency:** every 30 minutes via `.github/workflows/loop_monitor.yml`
**Script:** `scripts/loop_monitor.py`
**Triggers on ALERT:**
1. Opens GitHub issue (existing GITHUB_TOKEN)
2. Sends email via `GMAIL_APP_PASSWORD`
3. **Writes intervention to LOOP_STATE.md `## Next Priority` field** — loop honors it on next iteration per loop.md STEP 3. No human paste needed.

**Intervention triggers (subset of all alerts):**
- `same_blocker_count ≥ 3` (stuck pattern)
- `last_outcome == STOP` (loop halted but not acked)
- `iteration > 25` (soft cap)
- Same milestone attempted ≥3 times

**Skip-stacking:** if next_priority already starts with `INTERVENTION [`, no new intervention written until loop honors prior one.

Deterministic checks:
1. `same_blocker_count ≥ 3` → stuck pattern
2. `iteration > 25` → cap exceeded
3. `last_outcome == STOP` and not acknowledged
4. `feat/M*` branch declared in LOOP_STATE but stalled (no commit > 6h)
5. `DECISION: REVISE` in latest decisions doc but no commits in 12h
6. Branch field mismatch between LOOP_STATE and `git branch --show-current`
7. Same milestone (M0, M1, etc.) attempted ≥3 times in history
8. No commits across any branch in last 4h while loop iteration > 0

Acknowledgment file: `.loop-watch-ack.json` (gitignored or committed; either works)

### Tier 2 — Cowork scheduled task (Opus reasoning, fully autonomous, early-exit cadence)

**Frequency:** every 30 minutes (matches Tier 1) with mandatory early-exit
**Task ID:** `jeeves-loop-watch` (`mcp__scheduled-tasks__create_scheduled_task`)

**Early-exit (STEP 0):** before any reasoning, compare most-recent git commit timestamp vs prior Tier 2 run timestamp (from loop-watch.md). If no new commits since last run → exit in ~500 tokens, log SKIP. Only does full Opus reasoning when new evidence exists.

**Cost:** ~$30-100/month depending on loop activity. Steady-state cheap; bursts during active development. Drift detection lag: 30min instead of 6h.
**Behavior:** wakes Opus, reads LOOP_STATE + last 10 commits + last 2 decisions docs, runs reasoning checks Tier 1 can't catch:
- design rationalization patterns ("the probe is buggy, let me fix it" after a STOP rule)
- scope drift (milestones added without commit message explaining why)
- decision docs that contradict ROADMAP
- silent constraint violations (e.g. modifying old code path while flag was supposed to preserve it)
- production trigger violations (unauthorized `gh workflow run`)

**On anomaly: AUTONOMOUS INTERVENTION**
1. Writes intervention block to LOOP_STATE.md `## Next Priority` (replaces prior content).
2. Emails user via Gmail tool as FYI notification (not action request).
3. Logs to `~/Documents/Claude/Projects/Jeeves On Time/loop-watch.md`.

**Skip-stacking:** if Tier 1 or prior Tier 2 already wrote an intervention, Tier 2 logs SKIP and exits.

**On no anomaly:** silent log only. No email.

### Tier 3 — On-demand (user pastes logs — RARELY NEEDED NOW)

After Tier 1 + Tier 2 went fully autonomous, this tier is for:
- Complex design pivots requiring deep human-Opus dialogue
- Cases where Tier 2 flagged an anomaly but the intervention itself needs refinement
- Strategic decisions outside the loop's scope (e.g. abandon refactor, change provider stack)

User can paste logs into Cowork session when they choose. Not required for routine operation.

## How autonomous interventions work end-to-end

1. Loop runs M-th iteration. Writes outcome to LOOP_STATE.md.
2. Tier 1 fires within 30 min. If anomaly: writes `INTERVENTION [...]` to next_priority + emails user.
3. Tier 2 fires within 6h. If fuzzy anomaly: writes `INTERVENTION [...]` to next_priority + emails user. Skips if Tier 1 already wrote one.
4. Loop fires its next iteration. STEP 1 reads LOOP_STATE.md. STEP 3 sees non-empty next_priority starting with `INTERVENTION [` → uses verbatim as adjusted goal. Halts or pivots per intervention instructions.
5. Loop's STEP 5 writes new LOOP_STATE.md. Per loop.md STEP 3 contract, next_priority is "cleared on use." Loop appends `HONORED {timestamp}` per intervention spec.
6. Cycle repeats.

User receives email at each intervention but does not need to take action. Email is a notification of "the loop self-corrected because X."

## When user action IS required

- Three consecutive Tier 2 interventions on same issue → loop is grinding. User pastes logs to Cowork for redesign.
- Stop rule trip that loop self-halts on but design needs revision → user reads email, decides path forward.
- Production trigger needed → user explicitly authorizes via reply or direct ROADMAP edit.

## Token budget

| Tier | Cadence | Tokens/day | Catches |
|------|---------|------------|---------|
| 1 | 48× | 0 | structural anomalies, stalls, cap violations |
| 2 | 4× | ~32k | narrative drift, design rationalization, scope creep |
| 3 | on demand | varies | deep design dialogue |

Total Opus budget for monitoring: ~32k/day baseline + occasional escalation.

## Setup steps

1. Commit `scripts/loop_monitor.py` and `.github/workflows/loop_monitor.yml` to main.
2. Verify `GMAIL_APP_PASSWORD` secret already present (it is — used by daily.yml).
3. Optional: add `JEEVES_RECIPIENT_EMAIL` repo Variable if recipient differs from default.
4. Optional: `gh workflow run loop_monitor.yml -f dry_run=true` to smoke-test.
5. Register the Tier 2 Cowork scheduled task (done programmatically from Cowork session).
6. Watch first 1-2 cycles for false positives. Tune check thresholds if needed.

## Failure modes named in advance

- **False positives flood:** if Tier 1 alerts too often, acks pile up. Mitigation: tune thresholds; ack via JSON file is cheap.
- **Loop intentionally pauses mid-iteration:** stalled-branch check would alert on legitimate pauses. Mitigation: ack the alert; consider extending pause-window flag.
- **Cowork session not open when Tier 2 fires:** task runs on next launch; user receives backlog email. Acceptable.
- **Tier 2 generates noisy emails:** Opus prompt explicitly says "silent if no anomaly." User feedback tunes the prompt.

## Removal

If monitoring becomes noise:
- Disable workflow via GHA UI (Actions tab → Loop Monitor → Disable)
- Disable Tier 2 via Cowork: `mcp__scheduled-tasks__update_scheduled_task` with `enabled: false`
- Delete `.loop-watch-ack.json` to reset state
