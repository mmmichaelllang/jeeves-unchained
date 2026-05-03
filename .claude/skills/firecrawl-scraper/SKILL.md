---
name: firecrawl-scraper
description: Optional article fetcher using the Firecrawl API as an alternative to local Playwright. Use when adding new fetch-chain fallback steps to enrichment.py or talk_of_the_town.py, or when benchmarking extraction quality. Feature-flagged via FIRECRAWL_API_KEY env var — soft-fails when absent.
---

# Firecrawl Scraper — Jeeves

## Position in fetch chain
httpx+trafilatura → Jina(r.jina.ai) → **Firecrawl** → playwright_extractor (fallback order)

Firecrawl sits between Jina and Playwright:
- Use when Jina returns len<300 OR paywall markers
- Before escalating to local Playwright (slower, uses local Chromium)
- Skip entirely if FIRECRAWL_API_KEY not set

## API Contract
POST https://api.firecrawl.dev/v1/scrape
Headers: Authorization: Bearer {FIRECRAWL_API_KEY}
Body:
```json
{
  "url": "https://...",
  "formats": ["markdown"],
  "onlyMainContent": true,
  "timeout": 30000
}
```
Response: {"success": true, "data": {"markdown": "...", "title": "..."}}

## Quota Tracking
Track under "firecrawl" key in quota ledger (same pattern as other tools).
Hard cap: DAILY_HARD_CAPS["firecrawl"] = 50 (generous for free tier).

## Fail-soft Contract
Same as playwright_extractor:
- Missing API key → {"success": False, "error": "FIRECRAWL_API_KEY not set"}
- HTTP error → {"success": False, "error": "firecrawl api error: <status>"}
- Empty response → {"success": False, "error": "firecrawl returned no content"}
- Always returns the same shape as playwright_extractor.extract_article()
