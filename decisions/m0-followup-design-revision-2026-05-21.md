# M0 Design Revision — Content-Type-Aware Crawl4AI Cascade
# 2026-05-21 — follows probe score 0.71 < 0.8 gate

## M0 Probe Summary

Probe run: `scripts/diagnostics/probe_crawl4ai.py --quick`
Evidence: `decisions/crawl4ai-probe-2026-05-20.md`

| Strategy | Score | Gate |
|----------|-------|------|
| strict_fit BM25=1.0 | 0.4 | FAIL |
| strict_fit BM25=0.2 | 0.4 | FAIL |
| combined (fit or raw) | 0.71 | FAIL (<0.8) |

### Per-URL breakdown

| URL | combined | verdict |
|-----|---------|---------|
| NYRB (paywall) | 0.85 | raw fallback works — 8452c |
| LRB (paywall) | 0.85 | raw fallback works — 7655c |
| NYT | 0.0 | DataDome captcha — total block, all extractors zero |
| Guardian | 0.80 | raw sparse (330c) — barely passes |
| GitHub/OpenGlass | 1.0 | raw=24268c, excellent |
| HN thread | 0.70 | nav_heavy density=0.37 |
| Wikipedia | 0.70 | BM25 discards 98.8% of content |
| BBC | 0.80 | sparse but passes |

### Root causes

1. **NYT DataDome captcha** — single URL pulling combined from ~0.81 → 0.71.
   Not a Crawl4AI bug. No headless browser beats DataDome without paid proxy rotation.

2. **BM25 overfilters complex pages** — HN (comment tree) and Wikipedia (reference article)
   both show BM25 fit <<< trafilatura. BM25 is optimized for short news articles, not
   thread discussions or encyclopedic content.

3. **Guardian soft paywall** — raw=330c. Cookie consent + metered access leaves skeleton only.
   Same structural issue as NYT but less severe.

## Design Revision Decision

**Rejected:** wholesale Crawl4AI replacement of extraction cascade.
**Adopted:** content-type-aware routing based on host classification.

## Host Classification Scheme

```python
# jeeves/tools/crawl4ai_extract.py

HOSTS_LONG_FORM = {
    "nybooks.com", "lrb.co.uk", "aeon.co", "harpers.org",
    "themarginalian.org", "newyorker.com", "nplusonemag.com",
    "dissentmagazine.org", "thebaffler.com", "bostonreview.net",
}
# Long-form literary/cultural journals: Crawl4AI raw is useful but BM25 overfilters.
# Keep existing trafilatura → Jina cascade. Crawl4AI NOT inserted.

HOSTS_PAYWALLED = {
    "nytimes.com", "ft.com", "wsj.com", "economist.com",
    "bloomberg.com", "washingtonpost.com",
}
# Hard paywalls + anti-bot: DataDome, Piano, etc. Crawl4AI returns 0c or skeleton.
# Route to Jina (r.jina.ai reader mode) directly. Crawl4AI NOT attempted.

HOSTS_NAV_HEAVY = {
    "news.ycombinator.com", "reddit.com",
}
# Comment/thread aggregators: raw_markdown nav_heavy (density > 0.3).
# BM25 captures minimal signal. Trafilatura + manual extraction preferred.
# Crawl4AI NOT inserted in fetch chain for these hosts.

# Everything else → "news_short". Standard news articles, blogs, GitHub, AP, BBC.
# Crawl4AI raw fallback works well (probe: 0.8–1.0 on Guardian, BBC, GitHub).
```

## Which sectors gain from Crawl4AI

| Sector | content_type | Crawl4AI eligible? | Rationale |
|--------|-------------|-------------------|-----------|
| local_news | news_short | YES | Guardian, AP, Gothamist — open-web news |
| global_news | news_short | YES | BBC, AP, Reuters — open-web news |
| weather | news_short | YES | Mostly government / NOAA pages |
| career | news_short | YES | Blog posts, LinkedIn, job boards |
| family | news_short | YES | General web articles |
| wearable_ai | news_short | YES | GitHub, tech blogs, HN (filtered) |
| intellectual_journals | long_form | NO | NYRB, LRB, Aeon — keep Jina |
| triadic_ontology | deep | NO | Keep FunctionAgent 3-call loop |
| ai_systems | deep | NO | Keep FunctionAgent 3-call loop |
| uap | deep | NO | Keep FunctionAgent 3-call loop |
| newyorker | direct_fetch | NO | Playwright direct, unchanged |
| enriched_articles | mixed | TBD | Depends on URL set; decide in M2 |
| vault_insight | mixed | TBD | Depends on URL set; decide in M2 |

## Revised LLM-call math

Original target: ~20 calls/run.

With content-type routing:
- 6 light news_short sectors × 1 Cerebras call (Crawl4AI + synthesis) = 6 calls
- 4 other light sectors (literary_pick, enriched_articles, vault_insight, newyorker) × ~1 call = 4 calls
- 3 deep sectors × 3 FunctionAgent calls = 9 calls
- Total: ~19-23 Cerebras calls/run

Fits comfortably within Cerebras free tier (~60 RPM, llama-3.3-70b has no waitlist).

## Revised M1-M3 scope

See `ROADMAP.md` for full milestone specs. Summary of changes from original design:

**M1 (narrowed):** `crawl4ai_extract(url) → (text, mode_used)`. No BM25 default.
Caller decides. Host classifier (`classify_host`) co-located in same file.
Host sets defined. Tool returns mode so callers know what they got.

**M1.5 (new):** Host classifier populated + import-verified.

**M2 (narrowed):** Crawl4AI research synthesis ONLY for `_CRAWL4AI_ELIGIBLE_SECTORS`
(6 light news sectors). Deep sectors keep FunctionAgent unchanged.
Flag: `JEEVES_USE_CRAWL4AI_RESEARCH=1`.

**M3 (narrowed):** Crawl4AI as TIER 2 in `fetch_article_text` ONLY for news_short hosts.
Inserted between trafilatura (TIER 1) and Jina (TIER 3). Other hosts: existing cascade unchanged.
Flag: `JEEVES_USE_CRAWL4AI_FETCH=1`.

## Kill switches (unchanged)

- `JEEVES_REFACTOR_KILL_SWITCH=1` → forces old paths everywhere
- 3 consecutive days <6/13 sectors → revert + investigate
- Crawl4AI raises >5 sector-blocking exceptions/week → flag off
