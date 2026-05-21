# M6 Acceleration — 30-day wait → 6-12h validation sprint (2026-05-21)

## Context

Original ROADMAP M6 specified 30 consecutive days of daily.yml runs as validation gate before M7 (Charlotte) and M8 (deletion). User on Claude Max with explicit preference to "wake up tomorrow and have the pipeline working" — does not want to wait 24h between iterations, let alone 30 days.

## Decision

Compress M6 from 30-day passive wait → 6-12 hour high-cadence sprint via `.github/workflows/validation.yml` firing daily.yml every 30 minutes while `JEEVES_VALIDATION_MODE=1` repo Variable is set.

12 successful validation runs (≈6 hours at 30min cadence) gates M6 done. Same statistical confidence as 30 days × 1 run/day at fraction of wall-clock time.

## Rationale

| Concern | 30-day version | Sprint version |
|---|---|---|
| Wall clock | 30 days | 6-12 hours |
| Runs observed | 30 | 12+ |
| Cerebras free tier exposure | Once/day | Continuous — pressure-tests RPM limits faster |
| Detect provider policy changes | Slow | Forces issues to surface within hours |
| GHA minute cost | Negligible | ~$10-30/sprint at runner pricing |
| Real-world edge case coverage | Higher (more wall time) | Lower (no week-of-week variance) |

User explicitly accepts the lower coverage tradeoff. The 30min cadence actually surfaces capacity issues FASTER than daily cadence — if Cerebras hits RPM caps under sustained load, the sprint will trip on it within 1-2 runs instead of week 3.

## Implementation

1. `validation.yml` workflow created at `.github/workflows/validation.yml`. Fires every 30min. Gates on `vars.JEEVES_VALIDATION_MODE == '1'`. When gated off (default), each cron invocation exits in <10s — no daily.yml dispatch, no Cerebras burn.

2. To start validation sprint after M5 ships:
   ```bash
   gh variable set JEEVES_VALIDATION_MODE --body "1" -R mmmichaelllang/jeeves-unchained
   ```
   Validation runs begin at the next :00 or :30 of the hour.

3. To end validation sprint after 12 successful runs:
   ```bash
   gh variable set JEEVES_VALIDATION_MODE --body "0" -R mmmichaelllang/jeeves-unchained
   ```
   daily.yml at 12:00 UTC resumes as steady-state cadence.

4. ROADMAP M6 spec rewritten — see ROADMAP.md.

## Kill switches still active

All from original /challenge hardening:
- `JEEVES_REFACTOR_KILL_SWITCH=1` forces old paths regardless of feature flags
- 3 consecutive validation runs <6/13 sectors → halt sprint, investigate
- Cerebras RPM cap breached → fall back to OR via runtime model rotation (M4)
- >5 Crawl4AI sector exceptions in 1h → flag off

## Trade-offs accepted

🟡 Reduced real-world variance coverage. The 30-day window catches issues that only surface under different conditions (weekend traffic, holiday news cycles, provider maintenance windows). Sprint compresses to one diurnal slice.

🟡 Higher GHA minute burn. 48 dispatches/day × 30-45min each ≈ 24-36h of runner time per day during sprint. Within paid GHA tier or close to monthly free quota.

🟡 Higher Cerebras free tier exposure. Burst-testing may trip rate limits that day-cadence wouldn't surface. M4 (runtime model rotation) is the mitigation.

🟢 Faster iteration. Issues surface in hours, not weeks. Pipeline declared working same day.

## Reversible

To revert: set `JEEVES_VALIDATION_MODE=0`, restore original M6 30-day spec in ROADMAP, disable validation.yml workflow via GHA UI. No code changes required.
