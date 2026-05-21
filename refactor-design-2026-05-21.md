# Jeeves-Unchained Refactor Design — 2026-05-21

## Status
- Design accepted by user 2026-05-21
- Source: web extraction architectures doc + run #79 failure analysis
- Next: /challenge → /plan → /goal → adaptive-loop FULL-COACH fire

## Understanding Summary

**Building:** Three-layer refactor of jeeves-unchained drawing patterns from the web extraction doc.

**Why:** Run #79 (2026-05-21) proved free-tier diversification across Cerebras + OpenRouter cannot deliver 70-200 agent LLM calls/run within rate limits. GATE-B correctly caught the empty result. Structural shift required.

**For:** Single user (mmichaellang) operating daily newsletter via GHA cron.

**Constraints:**
- Free-tier only (no paid LLM/extraction APIs)
- Quality > token economy (reliability > capability > drift-resistance)
- GHA cron stays
- Correspondence + write + auditor phases not refactored (working)
- GATE-A + GATE-B stay

**Non-goals:**
- No paid Firecrawl, Bright Data, ScrapingBee, Kimi K2 paid host
- No migration off GitHub Actions
- No Cowork-style scheduling (Mac on-at-5am dependency rejected)
- No 9-part write architecture changes
- No new test infrastructure

## Assumptions

1. Crawl4AI runs in GHA runner unchanged. Python + asyncio + Playwright deps already present via patchright.
2. Charlotte MCP runs in GHA as a subprocess of the auditor step; OK because auditor doesn't need user's local machine.
3. Quality acceptance: "good day" = ≥10/13 agent sectors non-empty with real-URL citations.
4. Performance budget: research phase under 90min (current GHA timeout 120min).
5. Cerebras free tier ~60 RPM on available models (gpt-oss-120b, qwen-3-235b, zai-glm-4.7, llama3.1-8b).
6. OR free Llama 3.3 70B ~50 req/day — too tight as primary, fine as backup.
7. Hybrid LLM-call shape (deep 3-call agent + light 1-call synthesis) is the chosen balance.
8. No GHA binary-size regression — Crawl4AI reuses existing patchright.

## Decision Log

| # | Decision | Alternatives | Why |
|---|---|---|---|
| 1 | Success criteria = A+B+D | A only, all five, token-econ included | User explicit ranking; reliability + capability + drift |
| 2 | Refactor scope = research + fetch (C) | research-only, fetch-only, whole pipeline | Both layers are where the doc's leverage lives; user picked C |
| 3 | LLM-call shape = hybrid (deep agent-loop + light single-call) | zero LLM, soft cap, full agent loop | User asked for "highest quality" — hybrid preserves iteration where it helps, simplifies where it doesn't |
| 4 | Crawl4AI runs in-process | separate microservice | User preference; simpler |
| 5 | Charlotte MCP role = audit-time URL verification | research-time browsing, no role | Free-tier rules out Claude API driving Charlotte for research; auditor uses Cerebras to drive Charlotte for cited-URL verification (catches 2026-05-13 fabrication class) |
| 6 | Drift resistance = layout changes primary, model deprecations secondary | layout only, models only | User said "ideally both" — BM25 + LLM extraction handles layout, multi-provider abstraction (deferred) handles models |

## Final Design

### Layer 1 — Research engine: Crawl4AI + Cerebras synthesis

Replace per-sector FunctionAgent loop with Crawl4AI-driven extraction + Cerebras synthesis.

**Per light sector (10 sectors):**
1. Existing search tools (serper/tavily/exa) return URLs
2. `AsyncWebCrawler` fetches top 5-8 URLs with `flatten_shadow_dom=True`
3. BM25 statistical ranking produces "fit markdown" — drops nav, sidebar, footer
4. ONE Cerebras `gpt-oss-120b` call synthesizes findings JSON matching `spec.shape`
5. Total LLM calls: 1 per sector

**Per deep sector (3 sectors: triadic_ontology, ai_systems, uap):**
1. Tight 3-call agent loop preserved (search → read → synthesize)
2. Crawl4AI replaces the agent's article-fetch tool — agent calls Crawl4AI instead of raw HTTP
3. Total LLM calls: 3 per sector

**Newyorker:** unchanged direct fetch.

**Enriched_articles:** Crawl4AI fetches the 25 seed URLs, ONE Cerebras call synthesizes.

**Math:** 10×1 + 3×3 + 0 newyorker + 1×1 enriched = **20 Cerebras calls per run**. Fits comfortably.

### Layer 2 — Fetch chain: Crawl4AI replaces extraction cascade

Current chain: `trafilatura → Jina → tinyfish → Playwright/Patchright`.

New chain: `Crawl4AI as default` with `Jina + tinyfish + patchright` as feature-flagged optional layers.

Why: Crawl4AI's `AsyncWebCrawler` IS a Playwright wrapper plus BM25 noise removal plus Shadow DOM piercing plus optional LLM extraction. It replaces 4 tools with 1.

### Layer 3 — Audit-time URL verification: Charlotte MCP

Extend `scripts/audit.py` with a new step after write phase ships briefing:

1. Auditor parses briefing HTML, extracts all cited URLs
2. For each URL, Cerebras drives Charlotte MCP via subprocess
3. Charlotte navigates to URL via headless Chromium, returns tiered observation
4. Cerebras compares page content to briefing's claim about that URL
5. If mismatch or page-not-found: flag in audit-<date>.json with `defect_type: hallucinated_url`
6. Currently audit only checks structural defects; this catches content-fabrication

## Risks (to be deepened by /challenge)

- Crawl4AI may have edge cases on jeeves-specific targets (NYT, Aeon, Marginalian) — needs probe
- Charlotte MCP integration complexity could exceed estimate
- 20 Cerebras calls/run still subject to Cerebras free-tier RPM variability
- Switching off trafilatura → Jina chain removes fallback diversity if Crawl4AI has issues
- Test coverage on new code paths

## Implementation Plan
See: roadmap-refactor-2026-05-21.md (created by /plan skill next).

## Verification Criteria
See: goal output (DONE WHEN clauses, created by /goal skill next).
