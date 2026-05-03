---
name: github-actions-templates
description: Production-ready GitHub Actions patterns for jeeves-unchained's three-job pipeline (correspondence → research → write). Use when touching daily.yml, research.yml, write.yml, or correspondence.yml. Covers per-job timeouts, artifact passing, conditional steps, failure isolation, and cron scheduling.
---

# GitHub Actions Templates — Jeeves

## Pipeline Architecture
Three chained jobs. Failure isolation is critical — a write timeout must not prevent the correspondence artifact from being committed.

## Per-Job Timeout Rules
- correspondence: 15 min (real Gmail ~5 min, mock ~1 min)
- research: 30 min (15 sectors × ~1 min each + Playwright install)
- write: 35 min (9 parts × 65s sleep = ~10 min + NIM refine)

Total: ~80 min max end-to-end (well under GitHub's 6-hour limit).

## Artifact Isolation Pattern
Always `if: always()` on upload steps so they fire even when the job itself fails:
```yaml
- name: Upload briefing HTML
  if: always()
  uses: actions/upload-artifact@v4
```

## Failure Isolation: `continue-on-error`
For non-critical steps (quota commit, cleanup), use `continue-on-error: true` so a git conflict doesn't abort the job.

## Step-Level Timeout
For steps known to hang (Playwright install, long API calls), add `timeout-minutes` at the step level:
```yaml
- name: Install Playwright Chromium
  timeout-minutes: 5
  run: uv run python -m playwright install --with-deps chromium
```

## Secret Presence Check
Optional but useful: add a check step before the main run to validate required secrets are present. Surfaces config errors early with a clear message.
