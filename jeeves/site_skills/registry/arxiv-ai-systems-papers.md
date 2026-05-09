---
name: arxiv-ai-systems-papers
title: arxiv.org — AI Systems Papers (autonomous research, multi-agent, reasoning)
description: Find recent arxiv papers in the autonomous-research / multi-agent / reasoning-models space without re-shipping the same set every day.
sectors: [ai_systems]
hosts: [arxiv.org]
status: seed-2026-05-09
---

## Purpose

Surface arxiv papers (and a handful of close substitutes — papers-with-code,
research blogs) on autonomous AI research systems for the `ai_systems` deep
sector. The same five papers (DOVA / MARS / Mimosa / InternAgent /
DeepAgent) have shipped on six consecutive days as of 2026-05-09 — that's
a re-discovery tax this skill kills.

## When to use

- The `ai_systems` sector's first action.
- Any time the IMMEDIATE FIRST ACTION search would otherwise return a paper
  in the skip-list below.

## Workflow

1. **Filtered first search** — instead of the generic
   `multi-agent AI research systems autonomous pipeline 2026` query, use a
   NARROWED variant that excludes the over-shipped papers by adding negative
   terms or topic specifics. Examples that have surfaced novel hits:

       exa_search(query="multi-agent ScienceAgentBench result 2026 paper",
                  search_type="auto", num_results=4, text_max_chars=4000)
       exa_search(query="reasoning model inference budget scaling 2026 arxiv",
                  search_type="auto", num_results=4, text_max_chars=4000)
       exa_search(query="agent self-evolution loop 2026 paper",
                  search_type="auto", num_results=4)
       exa_search(query="prompt optimization without labels 2026 paper",
                  search_type="auto", num_results=4)
       exa_search(query="tool-use failure recovery agent 2026 arxiv",
                  search_type="auto", num_results=4)

2. **Filter against prior_urls** — every URL the searches return MUST be
   absent from `prior_urls`. arxiv URL canonical form is
   `arxiv.org/abs/<id>` or `arxiv.org/pdf/<id>` — both refer to the same
   paper. Treat them as the same URL when checking the skip-list.

3. **Read full text** — exa returns capped full text directly; no
   `tavily_extract` needed for arxiv hits. Read the abstract + the
   contributions section before writing findings.

## Site-specific gotchas

- **`abs/` vs `pdf/` URL drift**: a paper may surface today as
  `arxiv.org/pdf/2603.13327` and yesterday as `arxiv.org/abs/2603.13327`.
  Canonicalize by stripping the trailing component before dedup match.
- **Versioned IDs (`v1`, `v2`)**: `2603.13327` and `2603.13327v1` are the
  same paper. Strip the `vN` suffix.
- **Search bias toward stuck papers**: exa's neural ranker keeps surfacing
  the same five papers because they have the highest in-graph weight for
  the generic queries. NARROWING the query (specific benchmark name,
  specific failure mode) reliably breaks that cycle.

## Skip-list — papers ALREADY over-shipped (do not include)

The following arxiv URLs have shipped on 3+ briefings through 2026-05-09.
If a search returns one as a top hit, drop it and run a narrower follow-up.

- DOVA — `arxiv.org/pdf/2603.13327` (deliberation-first orchestration)
- MARS — `arxiv.org/pdf/2602.02660` (budget-aware Monte-Carlo planning)
- Mimosa — `arxiv.org/abs/2603.28986` (auto-synth task workflows)
- InternAgent-1.5 — `arxiv.org/abs/2602.08990` (generation+verify+evolve)
- DeepAgent — `arxiv.org/abs/2603.27008`
- "AI prompting techniques 2026" — `sureprompts.com/blog/...` (covered)

This list grows. The sector's run-time loader will splice in the latest
`prior_urls` slice automatically; the entries above are the human-curated
pinning of items that have proven durably stale.

## Empty-feed protocol

If after four narrowed searches no NEW URL surfaces, return ONE sentence in
findings: *"The autonomous-research front advances, Sir, but nothing fresh
has surfaced since our last review."* Then STOP. Do NOT pad with backstory
recap of the skip-list papers — postprocess will truncate.
