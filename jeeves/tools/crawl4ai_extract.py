"""
Crawl4AI extraction tool with host-aware content-type routing.

M0 probe (2026-05-21) showed Crawl4AI is NOT a wholesale replacement
for the extraction cascade. Results by content type:
  - news_short (AP, BBC, Guardian, GitHub): combined 0.8–1.0  ✓
  - long_form (NYRB, LRB): BM25 overfilters; raw fallback works but not better than Jina
  - paywalled (NYT): DataDome blocks all extractors (0.0)
  - nav_heavy (HN, Reddit): raw nav_heavy density > 0.3; BM25 captures near-nothing

Decision: Crawl4AI inserted ONLY for news_short hosts. Caller gets (text, mode_used)
so it can log/fallback accordingly.

See: decisions/m0-followup-design-revision-2026-05-21.md
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Host classification sets
# ---------------------------------------------------------------------------

HOSTS_LONG_FORM: frozenset[str] = frozenset({
    "nybooks.com",
    "lrb.co.uk",
    "aeon.co",
    "harpers.org",
    "themarginalian.org",
    "newyorker.com",
    "nplusonemag.com",
    "dissentmagazine.org",
    "thebaffler.com",
    "bostonreview.net",
})
"""Literary/cultural long-form journals. BM25 overfilters; Jina preferred."""

HOSTS_PAYWALLED: frozenset[str] = frozenset({
    "nytimes.com",
    "ft.com",
    "wsj.com",
    "economist.com",
    "bloomberg.com",
    "washingtonpost.com",
})
"""Hard paywalls + anti-bot (DataDome, Piano). Crawl4AI returns 0c. Route to Jina."""

HOSTS_NAV_HEAVY: frozenset[str] = frozenset({
    "news.ycombinator.com",
    "reddit.com",
    "old.reddit.com",
})
"""Comment/thread aggregators. Raw markdown nav_heavy density > 0.3. BM25 useless."""

ContentType = Literal["news_short", "long_form", "paywalled", "nav_heavy"]


def classify_host(url: str) -> ContentType:
    """Return content_type for a URL based on its hostname.

    Rules (checked in priority order):
      paywalled  → hard anti-bot, never attempt crawl4ai
      nav_heavy  → thread/comment aggregators, skip crawl4ai
      long_form  → literary journals, keep Jina cascade
      news_short → everything else; try crawl4ai first
    """
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return "news_short"

    # Strip www. prefix for matching
    host = hostname.removeprefix("www.")

    # Check from most restrictive to least
    for h in HOSTS_PAYWALLED:
        if host == h or host.endswith("." + h):
            return "paywalled"
    for h in HOSTS_NAV_HEAVY:
        if host == h or host.endswith("." + h):
            return "nav_heavy"
    for h in HOSTS_LONG_FORM:
        if host == h or host.endswith("." + h):
            return "long_form"
    return "news_short"


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

ModeUsed = Literal["crawl4ai_fit", "crawl4ai_raw", "skip_paywalled", "skip_nav_heavy",
                   "skip_long_form", "error"]


async def crawl4ai_extract(
    url: str,
    query: str | None = None,
    max_chars: int = 8000,
    timeout: int = 30,
) -> tuple[str, ModeUsed]:
    """Extract article text from URL using Crawl4AI.

    Returns (text, mode_used). Caller decides BM25 strategy — this function
    tries fit_markdown first; falls back to raw_markdown[:max_chars] if fit < 200 chars.

    Skips extraction (returns empty string) for paywalled, nav_heavy, long_form hosts —
    caller should route those to their existing cascade instead.

    Args:
        url:       URL to extract.
        max_chars: Character cap on returned text.
        timeout:   Per-URL timeout in seconds.

    Returns:
        (text, mode_used) where mode_used is one of:
          "crawl4ai_fit"    — fit_markdown used and sufficient
          "crawl4ai_raw"    — raw_markdown fallback used (fit was < 200 chars)
          "skip_paywalled"  — host is in HOSTS_PAYWALLED, not attempted
          "skip_nav_heavy"  — host is in HOSTS_NAV_HEAVY, not attempted
          "skip_long_form"  — host is in HOSTS_LONG_FORM, not attempted
          "error"           — crawl4ai raised an exception
    """
    content_type = classify_host(url)
    if content_type == "paywalled":
        return ("", "skip_paywalled")
    if content_type == "nav_heavy":
        return ("", "skip_nav_heavy")
    if content_type == "long_form":
        return ("", "skip_long_form")

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    except ImportError:
        return ("", "error")

    try:
        # BM25 is only useful when caller provides a real query string.
        # Passing the URL was a 2026-05-21 bug: BM25 ranks page chunks by
        # similarity to the URL string → fit_markdown ranks near-nothing.
        markdown_gen_kwargs: dict = {}
        if query and query.strip():
            from crawl4ai.content_filter_strategy import BM25ContentFilter
            markdown_gen_kwargs["content_filter"] = BM25ContentFilter(
                user_query=query, bm25_threshold=0.2
            )
        run_cfg = CrawlerRunConfig(
            markdown_generator=DefaultMarkdownGenerator(**markdown_gen_kwargs),
            page_timeout=timeout * 1000,
            wait_until="domcontentloaded",
        )
        browser_cfg = BrowserConfig(headless=True, verbose=False)

        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            result = await asyncio.wait_for(
                crawler.arun(url=url, config=run_cfg),
                timeout=timeout + 5,
            )

        if not result.success:
            return ("", "error")

        fit = (result.markdown.fit_markdown or "").strip()
        raw = (result.markdown.raw_markdown or "").strip()

        if len(fit) >= 200:
            return (fit[:max_chars], "crawl4ai_fit")
        elif len(raw) >= 1:
            return (raw[:max_chars], "crawl4ai_raw")
        else:
            return ("", "error")

    except Exception:
        return ("", "error")


async def batch_extract(
    urls: list[str],
    query: str | None = None,
    max_chars: int = 8000,
    timeout: int = 30,
    concurrency: int = 3,
) -> list[tuple[str, ModeUsed]]:
    """Extract multiple URLs concurrently with bounded concurrency.

    Returns list of (text, mode_used) in same order as input urls.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(url: str) -> tuple[str, ModeUsed]:
        async with sem:
            return await crawl4ai_extract(url, query=query, max_chars=max_chars, timeout=timeout)

    return await asyncio.gather(*[_bounded(u) for u in urls])
