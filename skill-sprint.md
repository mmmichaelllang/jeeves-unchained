# Skill Sprint — Jeeves Unchained

## Goal
Install 11 skills from the all-skills-catalog via `/skill-creator`, then apply each to the codebase. Ordered by impact. Each task: install skill → read SKILL.md → make targeted changes → verify.

---

## Tier 1 — Reliability (do first; these protect the daily pipeline)

- [ ] **T1-A: `tool-use-guardian`** → Verify: `_try_normalize_json` + `_json_repair_retry` in `jeeves/write.py` and NIM 429 backoff in `jeeves/llm.py` align with guardian patterns; add structured retry wrapper; run `pytest tests/ -k "json_repair or nim or groq"` — all pass.

- [ ] **T1-B: `browser-automation` + `playwright-pro`** → Verify: `jeeves/tools/playwright_extractor.py` gains stealth headers, cookie-consent dismissal, infinite-scroll guard, and hardened circuit-breaker; New Yorker path confirmed non-hallucinating; run `pytest tests/ -k playwright` green.

- [ ] **T1-C: `async-python-patterns`** → Verify: `threading.Thread + time.sleep` in `generate_briefing` (`jeeves/write.py`) replaced with `asyncio.TaskGroup` / bounded semaphore; `_SECTOR_SEMAPHORE` stays at 1 for NIM; `pytest` still passes ≥249.

---

## Tier 2 — Quality (improve output and CI robustness)

- [ ] **T2-A: `context-optimization`** (already installed — apply only) → Verify: Part 4+ Groq system prompt (`run_used_asides` + `used_topics_this_run` block) shaved ≥800 tokens via masking/compaction; confirmed by logging `input_tokens` before/after in a dry write run.

- [ ] **T2-B: `senior-prompt-engineer` + `ai-engineering-toolkit`** → Verify: `jeeves/prompts/write_system.md` and per-part instructions audited against 8-dimension scoring rubric; at least 3 structural improvements applied (e.g. remove redundant continuation rules, consolidate aside-dedup context); no regressions in NIM refine.

- [ ] **T2-C: `github-actions-templates`** → Verify: `daily.yml` gains per-job `timeout-minutes` (correspondence 10, research 25, write 30); write failure no longer kills correspondence artifact; artifact hand-off between jobs explicit; `ci.yml` unchanged.

---

## Tier 3 — Velocity (faster iteration / signal)

- [ ] **T3-A: `python-testing-patterns`** → Verify: new parametrized integration test covers `run_sector → _parse_sector_output → _json_repair_retry` chain with LLM mock (no NIM hit); total test count ≥260; `pytest --co` shows new tests.

- [ ] **T3-B: `agent-orchestration-multi-agent-optimize`** → Verify: research phase sectors split into two groups — NIM-heavy (`triadic_ontology`, `ai_systems`, `uap`) stay sequential; lightweight sectors (`career`, `wearable_ai`, `biotech`) allowed `_SECTOR_SEMAPHORE=2`; wall-clock estimate documented in code comment.

- [ ] **T3-C: `advanced-evaluation` + `llm-evaluation`** → Verify: `scripts/eval_briefing.py` created; scores Part 1 for "announcing the menu" anti-pattern and banned phrases from `write_system.md`; runs against last 3 `sessions/briefing-*.html`; outputs pass/fail table.

---

## Tier 4 — Experimental (benchmark / explore)

- [ ] **T4-A: `firecrawl-scraper`** → Verify: `jeeves/tools/firecrawl_extractor.py` created as optional fetch-chain step between Jina and Playwright; feature-flagged via `FIRECRAWL_API_KEY` env var; quota tracked under `"firecrawl"` key; soft-fails if key absent; benchmark against Playwright on 5 URLs logged.

- [ ] **T4-B: `daily-news-report`** → Verify: skill applied to improve `jeeves/prompts/write_system.md` report-structure section; any structural improvements to Part 1 hook and section transitions documented in a code comment.

---

## Done When
- [ ] All Tier 1 tasks green (tests pass ≥249)
- [ ] `daily.yml` has per-job timeouts
- [ ] Part 4+ token count reduced (measurable)
- [ ] `scripts/eval_briefing.py` runs end-to-end on local session files

---

## Notes
- context-optimization already installed — skip creation, go straight to application.
- playwright-skill + playwright-pro both exist in catalog; use playwright-pro (55 templates).
- T1-C: keep `_SECTOR_SEMAPHORE=1` for NIM tier even after asyncio refactor.
- T3-B: do NOT parallelize deep sectors (triadic_ontology stream-drops under load).
- All skill installs go to `/var/folders/17/5mwfk5nn3d5dh72p4hnqrw2r0000gn/T/claude-hostloop-plugins/575baabf4c007ecd/skills/` — use skill-creator to pull and set up each one.
