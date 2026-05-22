"""Tests for jeeves/tools/enrichment.py — JEEVES_USE_CRAWL4AI_FETCH (M3)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

# Eagerly import crawl4ai_extract so the module is primed in sys.modules before
# the patch() calls below resolve "jeeves.tools.crawl4ai_extract.*".  Without
# this, tests run in isolation (e.g. alphabetically before test_research_sectors
# which would otherwise prime it) hit AttributeError on the patch target.
import jeeves.tools.crawl4ai_extract  # noqa: F401


class _FakeResponse:
    status_code = 200
    text = "<html><head><title>Test</title></head><body><p>Short.</p></body></html>"

    def raise_for_status(self) -> None:  # noqa: D401
        pass


# ---------------------------------------------------------------------------
# Test 1: flag=0 → Crawl4AI never invoked
# ---------------------------------------------------------------------------


def test_crawl4ai_fetch_disabled_when_flag_off(monkeypatch):
    monkeypatch.delenv("JEEVES_USE_CRAWL4AI_FETCH", raising=False)

    classify_called: list[str] = []

    # 2026-05-21 cascade tightening: trafilatura output must be >=600 chars
    # AND look like prose (terminators, alpha ratio >=0.55, no boilerplate).
    # Use a realistic-looking sentence repeated.
    _PROSE = "This is a sentence about the Federal Reserve raising rates today. " * 12
    with patch("jeeves.tools.enrichment._HTTP_CLIENT") as mock_client:
        mock_client.get.return_value = _FakeResponse()
        with patch("trafilatura.extract", return_value=_PROSE):
            with patch(
                "jeeves.tools.crawl4ai_extract.classify_host",
                side_effect=lambda u: classify_called.append(u) or "news_short",
            ):
                from jeeves.tools.enrichment import fetch_article_text

                result = fetch_article_text("https://theguardian.com/article")

    assert not classify_called, "classify_host must not be called when flag=0"
    data = json.loads(result)
    assert data["fetch_failed"] is False


# ---------------------------------------------------------------------------
# Test 2: flag=1, news_short host → Crawl4AI inserted as TIER 2
# ---------------------------------------------------------------------------


def test_crawl4ai_fetch_inserted_for_news_short(monkeypatch):
    monkeypatch.setenv("JEEVES_USE_CRAWL4AI_FETCH", "1")
    # trafilatura returns short text so we fall through to Crawl4AI
    crawl4ai_text = "B" * 400

    async def _fake_c4ai(url: str, max_chars: int = 8000):
        return crawl4ai_text, "crawl4ai"

    with patch("jeeves.tools.enrichment._HTTP_CLIENT") as mock_client:
        mock_client.get.return_value = _FakeResponse()
        with patch("trafilatura.extract", return_value="short"):
            with patch(
                "jeeves.tools.crawl4ai_extract.classify_host",
                return_value="news_short",
            ):
                with patch(
                    "jeeves.tools.crawl4ai_extract.crawl4ai_extract",
                    side_effect=_fake_c4ai,
                ):
                    from jeeves.tools.enrichment import fetch_article_text

                    result = fetch_article_text("https://theguardian.com/article")

    data = json.loads(result)
    assert data.get("extracted_via") == "crawl4ai", "expected crawl4ai as source"
    assert data["fetch_failed"] is False
    assert data["text"] == crawl4ai_text[:3000]


# ---------------------------------------------------------------------------
# Test 3: flag=1, paywalled host → Crawl4AI NOT invoked
# ---------------------------------------------------------------------------


def test_crawl4ai_fetch_skipped_for_paywalled(monkeypatch):
    monkeypatch.setenv("JEEVES_USE_CRAWL4AI_FETCH", "1")
    monkeypatch.delenv("JEEVES_USE_TINYFISH", raising=False)
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)

    crawl4ai_called: list[str] = []

    async def _fake_c4ai(url: str, max_chars: int = 8000):
        crawl4ai_called.append(url)
        return "", "none"

    with patch("jeeves.tools.enrichment._HTTP_CLIENT") as mock_client:
        mock_client.get.return_value = _FakeResponse()
        with patch("trafilatura.extract", return_value="short"):
            with patch(
                "jeeves.tools.crawl4ai_extract.classify_host",
                return_value="paywalled",
            ):
                with patch(
                    "jeeves.tools.crawl4ai_extract.crawl4ai_extract",
                    side_effect=_fake_c4ai,
                ):
                    with patch(
                        "jeeves.tools.playwright_extractor.extract_article",
                        return_value={"success": False},
                    ):
                        from jeeves.tools.enrichment import fetch_article_text

                        result = fetch_article_text("https://nytimes.com/article")

    assert not crawl4ai_called, "crawl4ai must not be called for paywalled hosts"
    data = json.loads(result)
    assert data.get("extracted_via") != "crawl4ai"


# ---------------------------------------------------------------------------
# Test 4: nested-event-loop regression (asyncio.run inside a running loop)
# ---------------------------------------------------------------------------
#
# Reproduces the iter-6 false-SUCCESS bug. Before _run_crawl4ai_sync's
# get_running_loop() detection + thread dispatch, calling fetch_article_text
# from inside an active event loop raised:
#   RuntimeError: Cannot run the event loop while another loop is running
# This test is async (asyncio_mode = "auto" → runs inside pytest-asyncio's
# event loop), exercises the Crawl4AI path, and confirms no RuntimeError.


@pytest.mark.asyncio
async def test_fetch_article_text_survives_running_event_loop(monkeypatch):
    monkeypatch.setenv("JEEVES_USE_CRAWL4AI_FETCH", "1")
    monkeypatch.delenv("JEEVES_REFACTOR_KILL_SWITCH", raising=False)
    crawl4ai_text = "C" * 400

    async def _fake_c4ai(url: str, max_chars: int = 8000):
        return crawl4ai_text, "crawl4ai"

    with patch("jeeves.tools.enrichment._HTTP_CLIENT") as mock_client:
        mock_client.get.return_value = _FakeResponse()
        with patch("trafilatura.extract", return_value="short"):
            with patch(
                "jeeves.tools.crawl4ai_extract.classify_host",
                return_value="news_short",
            ):
                with patch(
                    "jeeves.tools.crawl4ai_extract.crawl4ai_extract",
                    side_effect=_fake_c4ai,
                ):
                    from jeeves.tools.enrichment import fetch_article_text

                    # Setup sanity: confirm we ARE inside a running loop.
                    asyncio.get_running_loop()

                    # Regression call — must not raise RuntimeError.
                    result = fetch_article_text("https://theguardian.com/article")

    data = json.loads(result)
    assert data.get("extracted_via") == "crawl4ai", (
        "Crawl4AI path must be reached and succeed even under a host loop"
    )
    assert data["text"] == crawl4ai_text[:3000]
    assert data["fetch_failed"] is False
