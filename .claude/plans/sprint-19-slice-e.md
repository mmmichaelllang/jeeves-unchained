# Sprint 19 Slice E - Tier choreography + eval harness + telemetry

Date: 2026-05-05
Branch: sprint-19-slice-e (from sprint-18-tinyfish-canary)
Production effect on default vanilla run: zero (every new behaviour gated by env flag).

## Files

### New
- jeeves/tools/telemetry.py - emit(event, **fields) writes JSONL to sessions/telemetry-<utc-date>.jsonl when JEEVES_TELEMETRY=1.
- jeeves/tools/rate_limits.py - acquire(provider) ctx manager with tier-tagged threading.Semaphore. JEEVES_RL_<PROVIDER>=N override.
- scripts/mine_golden_set.py - reads sessions/session-*.json, writes tests/fixtures/search_eval_set.yaml.
- scripts/eval_search.py - per-provider recall@N + latency + cost; CSV.
- tests/fixtures/search_eval_set.yaml - 12 queries x 4 categories.
- tests/test_telemetry.py, test_rate_limits.py, test_search_shadow.py, test_eval_search.py, test_mine_golden_set.py.

### Modified
- serper.py / tavily.py / exa.py / gemini_grounded.py / vertex_search.py / jina.py / tinyfish.py / playwright_extractor.py - wrap with acquire() + emit telemetry.
- serper.py - run shadow tools when env flags set.
- tools/__init__.py - TOOL_TAXONOMY constant.
- prompts/research_system.md - taxonomy block.

## Tiers

- T1 (8): serper, exa, jina_search
- T2 (4): tavily, gemini_grounded, vertex_grounded, jina_rerank
- T3 (1): jina_deepsearch, tinyfish, tinyfish_search, playwright, playwright_search, firecrawl

## Shadow flags (env, default unset)

- JEEVES_JINA_SEARCH_SHADOW=1
- JEEVES_TINYFISH_SEARCH_SHADOW=1
- JEEVES_PLAYWRIGHT_SEARCH_SHADOW=1

Each: ThreadPoolExecutor parallel fire alongside serper_search; writes sessions/shadow-search-<provider>-<date>.jsonl. Production output unchanged.

## Verification

```
PYTHONPATH=. python -m pytest tests/test_telemetry.py tests/test_rate_limits.py tests/test_search_shadow.py tests/test_eval_search.py tests/test_mine_golden_set.py tests/test_jina_tools.py tests/test_tinyfish_search.py tests/test_playwright_search.py tests/test_quota_ledger.py -v
```
