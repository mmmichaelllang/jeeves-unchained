# jeeves-unchained — Repo Variables and Activation Guide

This file lists every repo Variable + Secret the project respects, plus
which ones to flip to activate the 2026-05-21 cascade rebalance.

Set Variables at:
`https://github.com/mmmichaelllang/jeeves-unchained/settings/variables/actions`

Set Secrets at:
`https://github.com/mmmichaelllang/jeeves-unchained/settings/secrets/actions`

---

## Activation order — 2026-05-21 cascade rebalance

These flips drain the over-cap providers (tavily, exa) and shift load to
the cheap-and-empty providers (jina_search, gemini_grounded). Recommended
to flip in order, one per day, watching the next day's daily run.

### Day 1 — Telemetry first, so you can see the effect

| Variable | Value | Why |
|---|---|---|
| `JEEVES_TELEMETRY` | `1` | Per-call cost accounting. Required to measure whether the cascade rebalance is working. Cheap (one JSONL write per call). |

### Day 2 — Activate jina_search

| Variable | Value | Why |
|---|---|---|
| `JEEVES_USE_JINA_SEARCH` | `1` | Promotes the 6000/mo unused jina capacity. **40× cheaper than tavily** ($0.20/1k vs $8/1k). Requires `JINA_API_KEY` secret (you should already have this). |

Verify the next day: `sessions/telemetry-<date>.jsonl` should show `provider=jina_search` calls > 0.

### Day 3 — Activate per-sector tool subsets

| Variable | Value | Why |
|---|---|---|
| `JEEVES_PER_SECTOR_TOOLS` | `1` | Sectors with a declared tool allowlist (currently the default = all tools, opt-in per sector) get a trimmer toolbox. Saves ~1k tokens per sector that opts in. Safe — sectors without an allowlist still see the full toolbox. |

### Day 4 — Activate quota-aware exclusion

| Variable | Value | Why |
|---|---|---|
| `JEEVES_USE_QUOTA_AWARE_EXCLUSION` | `1` | Drops tools whose provider is >=85% of monthly cap from the agent toolbox. Stops tavily/exa from getting called once they're over budget. |
| `JEEVES_QUOTA_EXCLUSION_THRESHOLD` | `0.85` | Optional override. Lower = exclude sooner. |

### Day 5 — Activate Charlotte audit

| Variable | Value | Why |
|---|---|---|
| `JEEVES_USE_CHARLOTTE_AUDIT` | `1` | M7 — Charlotte MCP headless browser + Cerebras verification catches hallucinated URLs in the briefing. ~few seconds × 20 URLs/day. You built it months ago; turn it on. |

Also needed (probably already set as a Secret):

| Secret | Why |
|---|---|
| `CEREBRAS_API_KEY` | Verifier model. **Currently this is in Variables, not Secrets, which means `daily.yml` reads `secrets.CEREBRAS_API_KEY` and gets empty.** Move it to Secrets to actually enable. |

### Day 6 — Lower GATE-C threshold if degradation persists

| Variable | Value | Why |
|---|---|---|
| `JEEVES_GATE_C_THRESHOLD` | `0.5` | Default 0.5 (>=50% empty sectors → exit 7 degraded). Tighten to `0.3` if you want even early degradation to halt the run. |

---

## All recognized environment variables (reference)

### Feature flags

| Variable | Effect when set to `1` |
|---|---|
| `JEEVES_TELEMETRY` | Per-call JSONL telemetry to `sessions/telemetry-<date>.jsonl` |
| `JEEVES_USE_JINA_SEARCH` | Registers `jina_search` tool (requires `JINA_API_KEY`) |
| `JEEVES_USE_JINA_DEEPSEARCH` | Registers `jina_deepsearch` tool |
| `JEEVES_USE_JINA_RERANK` | Registers `jina_rerank` tool |
| `JEEVES_USE_TINYFISH` | Registers `tinyfish_extract` (requires `TINYFISH_API_KEY`) |
| `JEEVES_USE_TINYFISH_SEARCH` | Registers `tinyfish_search` |
| `JEEVES_USE_PLAYWRIGHT_SEARCH` | Registers `playwright_search` (free, no key needed) |
| `JEEVES_USE_CHARLOTTE_AUDIT` | Activates M7 audit-time URL verification |
| `JEEVES_USE_CRAWL4AI_RESEARCH` | Routes news_short sectors through Crawl4AI synthesis |
| `JEEVES_USE_CRAWL4AI_FETCH` | Inserts Crawl4AI in fetch_article_text cascade |
| `JEEVES_USE_QUOTA_AWARE_EXCLUSION` | Drops tools at >=85% monthly cap from agent toolbox |
| `JEEVES_PER_SECTOR_TOOLS` | Filters agent toolbox per `SectorSpec.tools` allowlist |
| `JEEVES_TINYFISH_SHADOW` | Runs TinyFish in parallel observe-only mode, writes `sessions/shadow-tinyfish-*.jsonl` |
| `JEEVES_JINA_SEARCH_SHADOW` | Same shape, for Jina search |
| `JEEVES_TINYFISH_SEARCH_SHADOW` | Same shape, for TinyFish search |
| `JEEVES_PLAYWRIGHT_SEARCH_SHADOW` | Same shape, for Playwright search |
| `JEEVES_PW_USE_LLM_CRYSTALLIZE` | Playwright extractor uses LLM crystallizer (default off) |

### Tunable thresholds

| Variable | Default | Effect |
|---|---|---|
| `JEEVES_GATE_C_THRESHOLD` | `0.5` | Sector emptiness fraction at which GATE-C exits 7 |
| `JEEVES_QUOTA_EXCLUSION_THRESHOLD` | `0.85` | Quota fraction at which tool is dropped from toolbox |
| `JEEVES_RL_<PROVIDER>` | varies | Per-provider rate-limit override (e.g. `JEEVES_RL_SERPER=16`) |

### Bypass flags (use to force a run through normally-blocking gates)

| Variable | Effect when set to `1` |
|---|---|
| `JEEVES_FORCE_RESEARCH_EMPTY` | Bypass GATE-B (all-empty sectors) |
| `JEEVES_FORCE_DEGRADED` | Bypass GATE-C (majority-empty sectors) |
| `JEEVES_FORCE_WRITE_EMPTY` | Bypass GATE-A (write phase: all sectors empty) |
| `JEEVES_REFACTOR_KILL_SWITCH` | Disable all Crawl4AI paths |

### Secrets (required for full functionality)

| Secret | Purpose |
|---|---|
| `GROQ_API_KEY` | Write phase, classify_with_kimi |
| `NVIDIA_API_KEY` | NIM fallback (write phase refine, used to be research) |
| `CEREBRAS_API_KEY` | Charlotte audit verifier, research fallback. **CHECK THIS — currently misplaced in Variables, daily.yml reads from Secrets** |
| `OPENROUTER_API_KEY` | Narrative editor + correspondence fallback chain |
| `SERPER_API_KEY` | serper_search |
| `TAVILY_API_KEY` | tavily_search + tavily_extract |
| `EXA_API_KEY` | exa_search |
| `GEMINI_API_KEY` | gemini_grounded_synthesize |
| `JINA_API_KEY` | jina_* tools (required when enabling jina canaries) |
| `TINYFISH_API_KEY` | tinyfish_* tools |
| `GMAIL_APP_PASSWORD` | SMTP send |
| `GMAIL_OAUTH_TOKEN_JSON` | Correspondence phase Gmail OAuth |

---

## Quota snapshot for context (as of 2026-05-21)

| Provider | Used / Cap | Status |
|---|---|---|
| serper | 585 / 2500 | 23% — plenty of headroom |
| tavily | 1183 / 1000 | **118% — over free tier, paying $8/1k overage** |
| exa | 639 / 500 | **128% — over free tier, paying $5/1k overage** |
| gemini | 97 / 1500 | 6% — drastically under-used |
| jina_search | 0 / 6000 | **0% — flag not set, never fired** |
| jina_deepsearch | 0 / 300 | 0% |
| jina_rerank | 0 / 3000 | 0% |
| tinyfish | 0 / 100 | 0% |
| tinyfish_search | 0 / 250 | 0% |
| playwright_search | 0 / 9999 | 0% |
| stealth | 0 / 200 | 0% |
| playwright | 3 / fallback only | rarely fires |

The single highest-leverage flip: **`JEEVES_USE_JINA_SEARCH=1`** — drains
the tavily overage at 40× lower per-call cost.
