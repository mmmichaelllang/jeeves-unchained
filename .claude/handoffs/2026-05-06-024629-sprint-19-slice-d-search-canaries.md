# Handoff: Sprint 19 Slice D — Search-Agent Canaries

**Created:** 2026-05-06 02:46:29 UTC
**Project:** jeeves-unchained
**Branch:** main (uncommitted; sprint-18 work also on disk uncommitted)
**Slug:** sprint-19-slice-d-search-canaries
**Score:** self-rated 85 (no TODOs, no secrets, all referenced files exist)

---

## Current State Summary

Just shipped Sprint 19 Slice D: 5 new search-agent tools, all opt-in behind individual `JEEVES_USE_*` env flags, all default-off. Production effect on a vanilla daily run is zero.

The slice came out of a 6-agent `/plan` synthesis comparing TinyFish/Jina/Playwright as superior research agents to Serper/Tavily. Slice D was picked first because it's the smallest coherent win: tool surface ready, zero production risk, lets eval/shadow infrastructure follow on top.

32 hermetic tests pass (no real HTTP egress). Branch `main` has the changes uncommitted alongside earlier Sprint 17 + 18 work also uncommitted.

## Important Context

- **NIM-safe contract is non-negotiable.** Every new tool returns `json.dumps(...)`, never a bare dict. CSV-string fallbacks for list args (Kimi sometimes synthesises CSV instead of JSON arrays). See CLAUDE.md `<nim-gotchas>`.
- **`cheapest_with_capacity` semantics preserved.** Adding the new providers to `DEFAULT_STATE` with low overage prices broke `test_quota_starts_fresh` (returned `playwright_search` not `serper`). Fix: introduced `_AUX_PROVIDERS` set in `jeeves/tools/quota.py`; the picker filters them out. Don't undo this — the picker is for primary search providers, not canaries.
- **`_PARSERS` dict gotcha.** In `playwright_extractor.search`, the per-engine parser map captures function refs at import time. Tests must use `monkeypatch.setitem(pe._PARSERS, ...)` not `setattr(pe, "_parse_ddg", ...)`. Documented in test file.
- **TinyFish search endpoint guessed.** `/v1/search` shape is best-knowledge per vendor docs; not exercised against the real API yet. Defensive parsing tolerates either `data.results` or `organic` arrays.
- **Jina free-tier limits unverified.** RPM/RPD ceilings used (200 search, 20 deepsearch, 100 rerank) are conservative. Live probe `curl -H "Authorization: Bearer $JINA_API_KEY" -i https://s.jina.ai/?q=test` to confirm before promotion.
- **Playwright sandbox path failed.** `.venv` symlinks point to host macOS paths; can't run from sandbox bash. Tests pass via `/tmp/.jeeves-test/bin/python` with `pytest httpx pydantic python-dotenv` installed. CI Linux runner uses its own venv — should work fine there.

## Files Modified This Session

```
jeeves/tools/jina.py            NEW   330 lines
jeeves/tools/quota.py           MOD   +20  (DEFAULT_STATE, DAILY_HARD_CAPS, _AUX_PROVIDERS, filter)
jeeves/tools/tinyfish.py        MOD   +160 (search() function + _TINYFISH_SEARCH_ENDPOINT)
jeeves/tools/playwright_extractor.py  MOD  +250 (search(), _parse_ddg/_parse_bing/_parse_brave, _PARSERS)
jeeves/tools/__init__.py        MOD   +120 (5 tool registrations + 2 wrapper factories)
jeeves/config.py                MOD   +5   (5 RESEARCH_BUDGET_* constants)
jeeves/prompts/research_system.md MOD  +12  (sprint-19 canary block + 5 budget rows)
tests/test_jina_tools.py        NEW   180 lines (9 tests)
tests/test_tinyfish_search.py   NEW    90 lines (5 tests)
tests/test_playwright_search.py NEW   105 lines (6 tests)
CLAUDE.md                       MOD   +1 line in <state> block (sprint-19 entry)
```

## Decisions Made

1. **Slice D first, not naming-refactor.** Agent 5's plan ordered naming-taxonomy refactor → tool descriptions → Jina suite. Flipped this: ship working Jina/TinyFish/Playwright skeletons behind flags, then refactor names with shadow data informing the description rewrites. Reasoning: refactor without canary data is guesswork; descriptions get a redo anyway when shadow telemetry shows actual pick rates.

2. **`_AUX_PROVIDERS` filter in `cheapest_with_capacity`** instead of bumping overage prices. Filter is semantic (these are canaries, not primaries); price-bumping is a lie that future-readers would have to decode.

3. **DDG default for `playwright_search`** over Brave/Bing. DDG HTML SERP is server-rendered, no captcha at GH Actions IP rate, and the `/l/?uddg=` unwrap is the only quirk.

4. **`include_raw_content=False` default for `tinyfish_search`.** Vendor pricing has raw content at 5 credits vs 2 for SERP-only; default off preserves the 8/day cap headroom.

5. **No live API probe of Jina/TinyFish.** Sandbox `WebFetch` would not exercise authenticated endpoints, and shipping defensive parsers + behind-flag gating is safer than blocking on vendor verification. Real shape will surface during shadow rollout.

## Immediate Next Steps

1. **Commit and push.** Branch is dirty with sprint-17/18 work intermixed; suggest splitting commits:
   - Commit A: sprint-18 TinyFish canary (already documented in CLAUDE.md as forthcoming PR)
   - Commit B: sprint-19 slice D (this session's work) — files listed in "Files Modified" above
   - Use `commit-work` or `commit` skill for clean conventional-commit messages

2. **Open PR for sprint-19 slice D.** Title: `sprint-19: search-agent canary tools (jina/tinyfish_search/playwright_search) behind env flags`. Body should reference the 6-agent synthesis. Link to `EVAL_GATE.md` for promotion thresholds (those need extending — see step 4).

3. **Add `JINA_API_KEY` to GitHub Actions secrets if not present.** `cfg.jina_api_key` already exists in `config.py`, registered as optional. The `daily.yml` workflow already plumbs it for `talk_of_the_town` — verify.

4. **Land Sprint 19 Slice E** (eval + shadow harness, ~1.5 days):
   - `scripts/mine_search_golden.py` — walk `sessions/session-*.json` last 14 days, emit `tests/fixtures/search_eval_set.yaml`
   - `scripts/eval_search.py` — adapter table mirroring `eval_extractors.py:151`
   - Three shadow flags writing to `sessions/shadow-search-<date>.jsonl`
   - Hook in `jeeves/tools/serper.py:49` (after successful POST → fire shadow searches in thread pool)
   - Extend `EVAL_GATE.md` with promotion criteria table per provider

## Critical Files

- `jeeves/tools/__init__.py:131-282` — 5 new tool registration blocks; pattern: env-flag gate + key check + FunctionTool.from_defaults with CHOOSE WHEN/PREFER OVER description
- `jeeves/tools/quota.py:21-65` — DEFAULT_STATE additions, _AUX_PROVIDERS set, DAILY_HARD_CAPS additions
- `jeeves/tools/quota.py:155-167` — cheapest_with_capacity filter
- `jeeves/tools/jina.py` — full new module
- `jeeves/tools/tinyfish.py:259-380` — new search() function (extract_article unchanged)
- `jeeves/tools/playwright_extractor.py:1382-1620` — new search() + parsers
- `jeeves/prompts/research_system.md:36-63` — sprint-19 canary tool descriptions
- `CLAUDE.md:9-13` — `<state>` block sprint-19 entry
- `EVAL_GATE.md` — exists; needs extension for search-eval thresholds
- `scripts/eval_extractors.py:151` — pattern to mirror for `eval_search.py`

## Key Patterns Discovered

- **Tool wrapper factory pattern.** `_make_tinyfish_search_tool(ledger)` and `_make_playwright_search_tool(ledger)` close over the ledger so the FunctionTool callable has no required args at call time. Mirrors `_make_tinyfish_extract_tool` from sprint-18.
- **429 → counter-bump pattern.** Mirrors `gemini_grounded.py` and `tinyfish.extract_article`. On 429, push the daily counter to the cap so subsequent same-run calls short-circuit. `_bump_to_cap_on_429(ledger, provider)` helper in `jeeves/tools/jina.py:80-91` factors this for the 3 Jina tools.
- **NIM-safe coercion.** `jina_rerank` accepts `documents` as either `list[str]` or CSV string. See `jeeves/tools/jina.py:233-246`.

## Potential Gotchas

- **`_PARSERS` capture-at-import.** Will bite anyone writing tests for `playwright_search`. Use `monkeypatch.setitem(pe._PARSERS, "ddg", fn)`.
- **TinyFish search shape unverified.** Defensive parser handles 3 envelope shapes (`data.results`, `data.organic`, top-level). When real API responds, may need a 4th case.
- **Jina DeepSearch latency.** Up to 90s/call. The `_DEEPSEARCH_CLIENT` has a 120s timeout. If a sector calls deepsearch and times out at 120s, the sector wall-clock budget is wrecked — keep `RESEARCH_BUDGET_JINA_DEEPSEARCH=3` enforced and monitor.
- **`_HTTP_CLIENT` per-module.** `jina.py`, `serper.py`, `tavily.py` each have their own. No shared pool — fine, just don't assume otherwise.
- **`TINYFISH_API_KEY` env var still gates BOTH extract and search tools.** Real TinyFish search test will require either a real key or extending the autouse `_stub_tinyfish_when_unconfigured` fixture (currently only stubs `extract_article`).

## Pending Work / Not Yet Done

- [ ] Commit + push sprint-19 slice D (and split out sprint-17/18 if not already pushed)
- [ ] Open PR with reference to 6-agent /plan synthesis
- [ ] Add `JINA_API_KEY` to repo Actions secrets (verify)
- [ ] Sprint 19 slice E: eval harness + golden-set miner + shadow flags
- [ ] Sprint 19 slice F: tier choreography (rate-limit semaphores, monthly-cap guard)
- [ ] Sprint 19 slice G: naming taxonomy refactor (search_*/extract_*/synthesize_*/rerank_*/crawl_*)
- [ ] Sprint 19 slice H: telemetry JSONL + `tool_telemetry` schema field

## Resume Instructions

```bash
cd /Users/frederickyudin/jeeves-unchained
git status                    # confirm dirty state matches "Files Modified" above
git diff jeeves/tools/quota.py    # eyeball _AUX_PROVIDERS filter
PYTHONPATH=. python -m pytest tests/test_jina_tools.py tests/test_tinyfish_search.py tests/test_playwright_search.py tests/test_quota_ledger.py -v
```

Then proceed with "Immediate Next Steps" item #1.
