# Jeeves-Unchained Refactor — Crawl4AI + Cerebras + Charlotte (2026-05-21)
# Place at: /Users/frederickyudin/jeeves-unchained/ROADMAP.md
# Driven by: .claude/loop.md (adaptive goal loop)
# Each milestone: verifiable via command or file check
# Previous ROADMAP (NIM circuit-breaker era) archived → ROADMAP.superseded-2026-05-21.md

## Goal
Replace per-sector FunctionAgent loop with content-type-aware Crawl4AI extraction + Cerebras synthesis for news_short sectors. Long-form, paywalled, and deep sectors keep existing paths. Reduce LLM calls from 70-200 → ~20-25/run. Add audit-time URL verification via Charlotte MCP. All changes feature-flagged for 30-day validation. Robust, reliable, resilient.

**M0 outcome (2026-05-21):** Probe score 0.71 combined → REVISE. Crawl4AI NOT a wholesale replacement. Content-type routing required: news_short sectors benefit; long-form paywalled + nav-heavy + deep sectors do not. See `decisions/m0-followup-design-revision-2026-05-21.md`.

## Status
- Refactor design: `/Users/frederickyudin/jeeves-unchained/refactor-design-2026-05-21.md`
- Brainstorm + /challenge applied 2026-05-21. All hardening recommendations adopted.
- NIM fully removed from research (PR #133). Cerebras+OR rebuild landed but failed (PR #134, #135).
- Run #79: GATE-B caught 13/14 empty sectors. Diagnosed: free-tier RPM ceiling on Cerebras + OR cannot serve 70-200 agent calls.

---

## Milestones

### M0 — Probe Crawl4AI on jeeves-target URLs (DONE — design revision)
- [x] Build `scripts/diagnostics/probe_crawl4ai.py`. Ran on 8 URLs across 4 sector types.
  Result: strict_fit=0.4, combined=0.71. DECISION: REVISE M1-M3.
  Evidence: `decisions/crawl4ai-probe-2026-05-20.md`
- [x] Design revision documented in `decisions/m0-followup-design-revision-2026-05-21.md`.
  Outcome: content-type-aware cascade adopted. M1-M3 narrowed as below.
  VERIFY: `grep -E "OVERALL SCORE|DECISION:" decisions/crawl4ai-probe-*.md | tail -2`

### M1 — Crawl4AI extract tool with host classifier
- [x] Build `jeeves/tools/crawl4ai_extract.py` with:
  - `crawl4ai_extract(url, max_chars=8000) → (text, mode_used)` — no BM25 default; caller decides strategy
  - `classify_host(url) → Literal["news_short", "long_form", "paywalled", "nav_heavy"]`
  - `HOSTS_LONG_FORM`, `HOSTS_PAYWALLED`, `HOSTS_NAV_HEAVY` sets (everything else → news_short)
  - `batch_extract(urls, ...) → list[(text, mode_used)]`
  DONE WHEN: File exists with 4 public symbols (`crawl4ai_extract`, `batch_extract`, `classify_host`, host sets importable). Import succeeds.
  VERIFY: `python -c "from jeeves.tools.crawl4ai_extract import crawl4ai_extract, classify_host; print('ok')"`
- [x] Tests: `tests/test_crawl4ai_extract.py` — 6 cases (host classification, news_short extracts, long_form skips crawl4ai, paywalled skips crawl4ai, exception handling, max_chars cap).
  Result: 6/6 passed.
  VERIFY: `uv run pytest tests/test_crawl4ai_extract.py -v 2>&1 | tail -10`

### M1.5 — Host classifier populated + verified
- [x] Populate `HOSTS_LONG_FORM`, `HOSTS_PAYWALLED`, `HOSTS_NAV_HEAVY` with full jeeves target lists (see design doc).
  Result: all sets populated. classify_host verified: nytimes→paywalled, guardian→news_short, github→news_short.
  VERIFY: `python -c "from jeeves.tools.crawl4ai_extract import classify_host; print(classify_host('https://nytimes.com/foo'), classify_host('https://theguardian.com/article'), classify_host('https://github.com/foo'))"`

### M2 — Research synthesis: Crawl4AI for news_short sectors only (feature-flagged)
- [x] Add `JEEVES_USE_CRAWL4AI_RESEARCH=1` flag. New code path in `jeeves/research_sectors.py`:
  - Sectors where `classify_host` returns `news_short` for majority of fetched URLs → Crawl4AI extract top 5-8 URLs → ONE Cerebras synthesis call → findings JSON.
  - Sectors with `content_type` NOT news_short → keep FunctionAgent path unchanged (no deletion, flag guards new path only).
  - Deep sectors (triadic_ontology, ai_systems, uap) → ALWAYS keep FunctionAgent path regardless of flag.
  - `newyorker` → unchanged direct fetch.
  DONE WHEN: Flag plumbed through `scripts/research.py` + `research_sectors.py`. Both paths runnable. `pytest tests/test_research_sectors.py` exits 0.
  VERIFY: `grep -n "JEEVES_USE_CRAWL4AI_RESEARCH" scripts/research.py jeeves/research_sectors.py && uv run pytest tests/test_research_sectors.py -q | tail -5`
- [x] Sector routing table: `_CRAWL4AI_ELIGIBLE_SECTORS` = {local_news, global_news, weather, career, family, wearable_ai} (6 light sectors). literary_pick, enriched_articles, vault_insight → TBD per M2 testing.
  DONE WHEN: Routing table present in `research_sectors.py` + referenced in run_sector decision tree.
  VERIFY: `grep -n "_CRAWL4AI_ELIGIBLE_SECTORS" jeeves/research_sectors.py`
- [ ] Production verification: one workflow_dispatch with flag=1 → ≥10/13 non-empty sectors.
  DONE WHEN: `sessions/session-$(date -u +%Y-%m-%d).json` shows ≥10 non-empty sectors after manual trigger.
  VERIFY: `python3 -c "import json; s=json.load(open('sessions/session-$(date -u +%Y-%m-%d).json')); print(sum(1 for k,v in s.items() if isinstance(v,(list,dict,str)) and (len(v) if isinstance(v,list) else any(v.values()) if isinstance(v,dict) else v)))"`

### M3 — Fetch-chain: Crawl4AI as TIER 2 for news_short hosts only (feature-flagged)
- [x] Add `JEEVES_USE_CRAWL4AI_FETCH=1` flag in `jeeves/enrichment.py` `fetch_article_text`.
  New cascade for news_short hosts: trafilatura → Crawl4AI (new TIER 2) → Jina → tinyfish → Playwright.
  Hosts NOT in news_short (long_form, paywalled, nav_heavy) → trafilatura → Jina → tinyfish → Playwright unchanged.
  `classify_host` from `crawl4ai_extract.py` drives the routing decision.
  Shipped via PR #137 (commit 502f1be) with _run_crawl4ai_sync thread-dispatch helper to survive pytest-asyncio host loops.
  DONE WHEN: Flag plumbed. Both paths runnable. `pytest tests/test_enrichment.py` exits 0.
  VERIFY: `grep -n "JEEVES_USE_CRAWL4AI_FETCH" jeeves/tools/enrichment.py && uv run pytest tests/test_enrichment.py -q | tail -5`
- [ ] Production verification: workflow_dispatch with both flags=1 → non-empty session.
  DONE WHEN: One manual run with both flags=1 produces ≥10/13 non-empty sectors.
  VERIFY: same as M2 production verify on next day's session JSON.

### M4 — Cerebras runtime model rotation
- [x] `_build_cerebras_llm` resolves model from live `/v1/models` response (not hardcoded chain). Per-call rotation on 429.
  DONE WHEN: Model resolution dynamic. Rotation logic catches 429 and tries next available model in chain.
  VERIFY: `grep -nE "_resolve_cerebras_model|_rotate_on_429" jeeves/research_sectors.py`
- [x] Tests: `tests/test_cerebras_rotation.py` — 4 cases (resolution from live, 429 rotation, exhaustion → OR fallback, model cache invalidation).
  DONE WHEN: `pytest tests/test_cerebras_rotation.py -v` 4/4 passed.
  VERIFY: `uv run pytest tests/test_cerebras_rotation.py -v 2>&1 | tail -10`

### M5 — Refactor kill switch
- [x] `JEEVES_REFACTOR_KILL_SWITCH=1` env var forces old paths regardless of feature flags. One-line emergency reversion.
  DONE WHEN: Both feature flags check kill switch first; if set, route to old code path.
  VERIFY: `grep -nE "JEEVES_REFACTOR_KILL_SWITCH" scripts/research.py jeeves/research_sectors.py jeeves/tools/enrichment.py`
- [x] Tests: `tests/test_kill_switch.py` — 3 cases (kill overrides research flag, fetch flag, both).
  DONE WHEN: `pytest tests/test_kill_switch.py -v` 3/3.
  VERIFY: `uv run pytest tests/test_kill_switch.py -v 2>&1 | tail -10`

### M6 — Validation sprint (6-12 hour high-cadence validation, NOT 30 days)
**Revised 2026-05-21:** original 30-day wait compressed to a high-cadence sprint. User on Claude Max, willing to burn GHA minutes for fast validation. Decision: `decisions/m6-acceleration-2026-05-21.md`.

- [ ] Set `JEEVES_USE_CRAWL4AI_RESEARCH=1` AND `JEEVES_USE_CRAWL4AI_FETCH=1` in repo Variables. Enable `.github/workflows/validation.yml` (fires every 30 min during validation window).
  DONE WHEN: Both Variables set AND validation.yml enabled AND first validation run completes.
  VERIFY: `gh workflow list -R mmmichaelllang/jeeves-unchained | grep -i validation`

- [ ] Run validation sprint: 12+ consecutive validation.yml runs (≈ 6 hours at 30min cadence).
  DONE WHEN: `scripts/health_check.py --window 12 --source validation` reports ≥9/12 non-empty briefings AND zero KILL_SWITCH deployments AND average ≥10/13 populated sectors per non-empty briefing.
  VERIFY: `python scripts/health_check.py --window 12 --source validation 2>&1 | grep -E "non_empty|KILL_SWITCH|avg_sectors"`

- [ ] After 12 successful runs: disable validation.yml cron (set workflow inactive). daily.yml at 12:00 UTC resumes as steady-state cadence.
  DONE WHEN: validation.yml disabled AND next daily.yml scheduled run also produces ≥10/13 non-empty sectors.
  VERIFY: `gh workflow disable validation.yml -R mmmichaelllang/jeeves-unchained && gh run list --workflow daily.yml -R mmmichaelllang/jeeves-unchained --limit 1`

### M7 — Charlotte MCP audit-time URL verification (after M6 success)
- [ ] Add `JEEVES_USE_CHARLOTTE_AUDIT=1` flag in `scripts/audit.py`. Charlotte MCP subprocess + Cerebras drives URL verification on cited URLs in briefing HTML.
  DONE WHEN: Charlotte subprocess wired, Cerebras prompts page-content vs briefing-claim comparison, new defect type `hallucinated_url` in audit-{date}.json schema.
  VERIFY: `grep -nE "JEEVES_USE_CHARLOTTE_AUDIT|hallucinated_url" scripts/audit.py jeeves/schema.py`
- [ ] Manual smoke test on known-bad briefing (2026-05-13 fabricated URLs).
  DONE WHEN: `python scripts/audit.py --date 2026-05-13 --force-charlotte` flags ≥1 URL as hallucinated.
  VERIFY: `python scripts/audit.py --date 2026-05-13 --force-charlotte 2>&1 | grep -E "hallucinated_url|defect_type"`

### M8 — Old-code retirement (after M6 + M7 validated)
- [ ] Remove FunctionAgent loop and trafilatura→Jina→tinyfish cascade. Keep Playwright as Crawl4AI's only fallback.
  DONE WHEN: `git diff` shows ≥-500 lines net AND `pytest tests/` exits 0 AND one workflow_dispatch produces non-empty briefing.
  VERIFY: `git diff --stat origin/main | tail -1 && uv run pytest tests/ -q | tail -3`

### M9 — FINAL VERIFICATION (always last per plan skill)
- [ ] 90-day stability check.
  DONE WHEN: `scripts/health_check.py --window 90` reports ≥85/90 non-empty briefings AND no GATE-A/GATE-B regression AND audit log shows zero `hallucinated_url` defects in last 30 days.
  VERIFY: `python scripts/health_check.py --window 90 2>&1 | tail -10`

---

## Project Complete When
All milestones show `- [x]` AND daily.yml scheduled run at 12:00 UTC produces a non-empty briefing emailed to lang.mc@gmail.com on 60+ consecutive days with average ≥10/13 populated sectors AND zero hallucinated URLs caught by auditor.

## Kill Switches (from /challenge hardening)
- `JEEVES_REFACTOR_KILL_SWITCH=1` — instant reversion to old paths.
- 3 consecutive days <6/13 sectors → revert PR + investigate.
- Cerebras free tier removes gpt-oss-120b → fallback to runtime model resolution.
- Crawl4AI introduces >5 sector-blocking exceptions/week → flag off.
- Charlotte MCP audit produces >20% false positives → disable audit flag.

## Critical Path (REVISED 2026-05-21 — accelerated)
M0 → M1 → M1.5 → M2 → M3 → M4 → M5 → M6 (6-12h sprint) → M7 → M8 → M9.
All milestones complete within 24-48 hours if loop runs continuously.
M3/M4/M5 can interleave with M2.

## Notes (REVISED 2026-05-21)
- Total timeline: 24-72 hours end-to-end (was 90-120 days).
- M6 validation compressed from 30 days → 6-12 hour high-cadence sprint via `validation.yml` workflow firing every 30min.
- User on Claude Max + accepts GHA minute burn for fast validation.
- ALL old code paths still preserved behind feature flags. Feature flag default OFF.
- M8 (deletion) requires 30 consecutive non-empty briefings after M6 success — NOT 30 days. Could complete within 15 days at daily cadence.
- Tests required at every M-level (no untested code into main).
- Each milestone is PR-sized.
- Production verification: M2/M3 single workflow_dispatch each. M6 = 12+ validation.yml runs.
