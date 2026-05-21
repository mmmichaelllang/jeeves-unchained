# Crawl4AI Probe — 2026-05-20

Generated: 2026-05-21T04:49:32.147053Z

crawl4ai version: 0.8.6 | BM25 thresholds tested: 1.0 (strict) + 0.2 (permissive)


## Per-URL Results

| URL | strict_1.0 | strict_0.2 | combined | trafilatura_chars | jina_chars |
|-----|-----------|-----------|---------|-----------------|----------|
| www.nybooks.com/articles/2024/01/18/the-man-who-broke-t | 0.3 | 0.3 | 0.85 | 0 | 8604 |
| www.lrb.co.uk/the-paper/v46/n01/jenny-turner/the-joy-of | 0.3 | 0.3 | 0.85 | 0 | 8095 |
| www.nytimes.com/2024/01/15/nyregion/nyc-migrants-shelte | 0.0 | 0.0 | 0.0 | 0 | 281 |
| www.theguardian.com/us-news/2024/jan/15/new-york-city-m | 0.3 | 0.3 | 0.8 | 0 | 490 |
| github.com/BasedHardware/OpenGlass | 0.3 | 0.3 | 1.0 | 2812 | 7357 |
| news.ycombinator.com/item?id=39686046 | 0.5 | 0.5 | 0.7 | 6732 | 3673 |
| en.wikipedia.org/wiki/2024_United_States_presidential_e | 0.7 | 0.7 | 0.7 | 213088 | 654210 |
| www.bbc.com/news/world-us-canada-67945976 | 0.8 | 0.8 | 0.8 | 0 | 5926 |

## intellectual_journals — https://www.nybooks.com/articles/2024/01/18/the-man-who-broke-the-music-business/
- crawl4ai latency: 57.9s | error: none
- fit_1.0: 1c | fit_0.2: 1c | raw: 8452c
- fit_0.2 snippet: ``
- trafilatura: 0c | jina: 8604c
- scores: strict_1.0=0.3 strict_0.2=0.3 combined=0.85
  - strict_1.0: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 0)']
  - strict_0.2: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 0)']
  - combined:   ['content_present (8000c)', 'nav_partial (density=0.27)', 'vs_traf OK (8000 vs 0)']

## intellectual_journals — https://www.lrb.co.uk/the-paper/v46/n01/jenny-turner/the-joy-of-boredom
- crawl4ai latency: 50.7s | error: none
- fit_1.0: 1c | fit_0.2: 1c | raw: 7655c
- fit_0.2 snippet: ``
- trafilatura: 0c | jina: 8095c
- scores: strict_1.0=0.3 strict_0.2=0.3 combined=0.85
  - strict_1.0: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 0)']
  - strict_0.2: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 0)']
  - combined:   ['content_present (7655c)', 'nav_partial (density=0.25)', 'vs_traf OK (7655 vs 0)']

## local_news — https://www.nytimes.com/2024/01/15/nyregion/nyc-migrants-shelter-crisis.html
- crawl4ai latency: 29.9s | error: Blocked by anti-bot protection: DataDome captcha
- fit_1.0: 0c | fit_0.2: 0c | raw: 0c
- fit_0.2 snippet: `(empty)`
- trafilatura: 0c | jina: 281c
- scores: strict_1.0=0.0 strict_0.2=0.0 combined=0.0
  - FAIL: Blocked by anti-bot protection: DataDome captcha

## local_news — https://www.theguardian.com/us-news/2024/jan/15/new-york-city-migrant-crisis-shelter
- crawl4ai latency: 85.5s | error: none
- fit_1.0: 1c | fit_0.2: 1c | raw: 330c
- fit_0.2 snippet: ``
- trafilatura: 0c | jina: 490c
- scores: strict_1.0=0.3 strict_0.2=0.3 combined=0.8
  - strict_1.0: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 0)']
  - strict_0.2: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 0)']
  - combined:   ['content_sparse (330c)', 'nav_stripped (density=0.07)', 'vs_traf OK (330 vs 0)']

## wearable_ai — https://github.com/BasedHardware/OpenGlass
- crawl4ai latency: 86.1s | error: none
- fit_1.0: 1c | fit_0.2: 1c | raw: 24268c
- fit_0.2 snippet: ``
- trafilatura: 2812c | jina: 7357c
- scores: strict_1.0=0.3 strict_0.2=0.3 combined=1.0
  - strict_1.0: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 2812)']
  - strict_0.2: ['content_missing (1c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (1 vs 2812)']
  - combined:   ['content_present (8000c)', 'nav_stripped (density=0.13)', 'vs_traf OK (8000 vs 2812)']

## wearable_ai — https://news.ycombinator.com/item?id=39686046
- crawl4ai latency: 86.9s | error: none
- fit_1.0: 122c | fit_0.2: 122c | raw: 13909c
- fit_0.2 snippet: `And Apple prohibits certain types of content in their store, e.g. adult content or P2P apps, which some users would want.`
- trafilatura: 6732c | jina: 3673c
- scores: strict_1.0=0.5 strict_0.2=0.5 combined=0.7
  - strict_1.0: ['content_sparse (122c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (122 vs 6732)']
  - strict_0.2: ['content_sparse (122c)', 'nav_stripped (density=0.00)', 'vs_traf WORSE (122 vs 6732)']
  - combined:   ['content_present (8000c)', 'nav_heavy (density=0.37)', 'vs_traf OK (8000 vs 6732)']

## global_news — https://en.wikipedia.org/wiki/2024_United_States_presidential_election
- crawl4ai latency: 182.4s | error: none
- fit_1.0: 11811c | fit_0.2: 11811c | raw: 1001251c
- fit_0.2 snippet: `* [Contents](https://en.wikipedia.org/wiki/Wikipedia:Contents "Guides to browsing Wikipedia") * [Random article](https://en.wikipedia.org/wiki/Special:Random "Visit a randomly selected article \[alt-x`
- trafilatura: 213088c | jina: 654210c
- scores: strict_1.0=0.7 strict_0.2=0.7 combined=0.7
  - strict_1.0: ['content_present (11811c)', 'nav_stripped (density=0.07)', 'vs_traf WORSE (11811 vs 213088)']
  - strict_0.2: ['content_present (11811c)', 'nav_stripped (density=0.07)', 'vs_traf WORSE (11811 vs 213088)']
  - combined:   ['content_present (11811c)', 'nav_stripped (density=0.07)', 'vs_traf WORSE (11811 vs 213088)']

## global_news — https://www.bbc.com/news/world-us-canada-67945976
- crawl4ai latency: 72.6s | error: none
- fit_1.0: 387c | fit_0.2: 387c | raw: 10552c
- fit_0.2 snippet: `[Skip to content](https://www.bbc.com/news/world-us-canada-67945976#bbc-main) [News](https://www.bbc.com/news) [Content Index](https://www.bbc.com/pages/content-index) Copyright 2026 BBC. All rights r`
- trafilatura: 0c | jina: 5926c
- scores: strict_1.0=0.8 strict_0.2=0.8 combined=0.8
  - strict_1.0: ['content_sparse (387c)', 'nav_stripped (density=0.14)', 'vs_traf OK (387 vs 0)']
  - strict_0.2: ['content_sparse (387c)', 'nav_stripped (density=0.14)', 'vs_traf OK (387 vs 0)']
  - combined:   ['content_sparse (387c)', 'nav_stripped (density=0.14)', 'vs_traf OK (387 vs 0)']

## Strategy Summary

| Strategy | Overall Score | Pass ≥0.8? |
|----------|--------------|-----------|
| strict_fit (BM25=1.0) | 0.4 | NO |
| strict_fit (BM25=0.2) | 0.4 | NO |
| combined (fit_0.2 or raw fallback) | 0.71 | NO |

## Findings

**Paywalled/inaccessible (1 URLs):**
  - https://www.nytimes.com/2024/01/15/nyregion/nyc-migrants-shelter-crisis.html — traf=0c jina=281c
  These URLs require Jina as fallback regardless of crawl4ai strategy.

**BM25 threshold impact:** threshold=0.2 improved score vs 1.0 on 0/8 URLs.

OVERALL SCORE (strict_1.0): 0.4
OVERALL SCORE (strict_0.2): 0.4
OVERALL SCORE (combined): 0.71
DECISION: REVISE M1-M3