"""Tests for jeeves/tools/crawl4ai_extract.py (M1 milestone).

6 cases:
  1. classify_host — news_short, long_form, paywalled, nav_heavy variants
  2. crawl4ai_extract skips paywalled host
  3. crawl4ai_extract skips nav_heavy host
  4. crawl4ai_extract skips long_form host
  5. crawl4ai_extract returns fit text when crawl4ai succeeds (fit >= 200c)
  6. crawl4ai_extract falls back to raw when fit < 200c; respects max_chars cap

Note: crawl4ai imports are lazy (inside the function) so we patch via sys.modules
to intercept `from crawl4ai import AsyncWebCrawler, ...`.
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jeeves.tools.crawl4ai_extract import (
    HOSTS_LONG_FORM,
    HOSTS_NAV_HEAVY,
    HOSTS_PAYWALLED,
    batch_extract,
    classify_host,
    crawl4ai_extract,
)


# ---------------------------------------------------------------------------
# 1. classify_host — correctness across all four buckets
# ---------------------------------------------------------------------------

def test_classify_host_variants():
    # paywalled
    assert classify_host("https://www.nytimes.com/2024/01/15/article.html") == "paywalled"
    assert classify_host("https://ft.com/content/abc123") == "paywalled"
    # nav_heavy
    assert classify_host("https://news.ycombinator.com/item?id=123") == "nav_heavy"
    assert classify_host("https://www.reddit.com/r/singularity/") == "nav_heavy"
    # long_form
    assert classify_host("https://www.nybooks.com/articles/2024/01/18/test/") == "long_form"
    assert classify_host("https://aeon.co/essays/some-essay") == "long_form"
    # news_short (default)
    assert classify_host("https://www.theguardian.com/article") == "news_short"
    assert classify_host("https://github.com/BasedHardware/OpenGlass") == "news_short"
    assert classify_host("https://apnews.com/article/abc") == "news_short"
    assert classify_host("https://www.bbc.com/news/world-123") == "news_short"


# ---------------------------------------------------------------------------
# 2. crawl4ai_extract skips paywalled host — no crawl4ai import attempted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_skips_paywalled():
    text, mode = await crawl4ai_extract("https://www.nytimes.com/2024/01/15/test.html")
    assert text == ""
    assert mode == "skip_paywalled"


# ---------------------------------------------------------------------------
# 3. crawl4ai_extract skips nav_heavy host
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_skips_nav_heavy():
    text, mode = await crawl4ai_extract("https://news.ycombinator.com/item?id=39686046")
    assert text == ""
    assert mode == "skip_nav_heavy"


# ---------------------------------------------------------------------------
# 4. crawl4ai_extract skips long_form host
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_skips_long_form():
    text, mode = await crawl4ai_extract("https://www.nybooks.com/articles/2024/01/18/test/")
    assert text == ""
    assert mode == "skip_long_form"


# ---------------------------------------------------------------------------
# 5. crawl4ai returns fit text when fit_markdown >= 200c
# ---------------------------------------------------------------------------

def _make_crawl_result(fit: str, raw: str, success: bool = True):
    """Build a minimal mock mimicking crawl4ai's CrawlResult."""
    md = SimpleNamespace(fit_markdown=fit, raw_markdown=raw)
    return SimpleNamespace(success=success, markdown=md)


def _make_crawl4ai_modules(mock_crawler_instance: AsyncMock) -> dict:
    """Build a fake crawl4ai module tree for sys.modules patching.

    crawl4ai imports are lazy (inside function body), so patching
    jeeves.tools.crawl4ai_extract.AsyncWebCrawler doesn't work.
    Instead we inject fake modules into sys.modules before the function runs.
    """
    fake_crawl4ai = ModuleType("crawl4ai")
    fake_crawl4ai.AsyncWebCrawler = MagicMock(return_value=mock_crawler_instance)
    fake_crawl4ai.BrowserConfig = MagicMock()
    fake_crawl4ai.CrawlerRunConfig = MagicMock()

    fake_filter = ModuleType("crawl4ai.content_filter_strategy")
    fake_filter.BM25ContentFilter = MagicMock()

    fake_md = ModuleType("crawl4ai.markdown_generation_strategy")
    fake_md.DefaultMarkdownGenerator = MagicMock()

    return {
        "crawl4ai": fake_crawl4ai,
        "crawl4ai.content_filter_strategy": fake_filter,
        "crawl4ai.markdown_generation_strategy": fake_md,
    }


@pytest.mark.asyncio
async def test_extract_uses_fit_when_sufficient():
    good_fit = "A" * 300
    mock_result = _make_crawl_result(fit=good_fit, raw="B" * 5000)

    mock_crawler = AsyncMock()
    mock_crawler.__aenter__ = AsyncMock(return_value=mock_crawler)
    mock_crawler.__aexit__ = AsyncMock(return_value=False)
    mock_crawler.arun = AsyncMock(return_value=mock_result)

    fake_modules = _make_crawl4ai_modules(mock_crawler)
    with patch.dict(sys.modules, fake_modules):
        text, mode = await crawl4ai_extract("https://apnews.com/article/test", max_chars=8000)

    assert mode == "crawl4ai_fit"
    assert text == good_fit


# ---------------------------------------------------------------------------
# 6. Falls back to raw when fit < 200c; respects max_chars cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_raw_fallback_and_max_chars():
    thin_fit = "X"  # 1 char — triggers raw fallback
    long_raw = "Y" * 20000
    mock_result = _make_crawl_result(fit=thin_fit, raw=long_raw)

    mock_crawler = AsyncMock()
    mock_crawler.__aenter__ = AsyncMock(return_value=mock_crawler)
    mock_crawler.__aexit__ = AsyncMock(return_value=False)
    mock_crawler.arun = AsyncMock(return_value=mock_result)

    fake_modules = _make_crawl4ai_modules(mock_crawler)
    with patch.dict(sys.modules, fake_modules):
        text, mode = await crawl4ai_extract(
            "https://apnews.com/article/test",
            max_chars=5000,
        )

    assert mode == "crawl4ai_raw"
    assert len(text) == 5000  # capped at max_chars
