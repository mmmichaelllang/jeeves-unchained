"""Tavily search + extract. AI-native SERP plus full-text article fetcher."""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .quota import QuotaLedger

log = logging.getLogger(__name__)


def make_tavily_search(cfg: Config, ledger: QuotaLedger):
    def tavily_search(
        query: str,
        max_results: int = 8,
        depth: str = "basic",
    ) -> dict[str, Any]:
        """Tavily AI-native search with an optional synthesized answer.

        Args:
            query: question or keyword string.
            max_results: max results to return.
            depth: 'basic' (1 credit) or 'advanced' (2 credits).
        """
        try:
            from tavily import TavilyClient  # type: ignore

            client = TavilyClient(api_key=cfg.tavily_api_key)
            resp = client.search(
                query=query,
                max_results=max_results,
                search_depth=depth,
                include_answer=True,
            )
        except Exception as e:
            log.warning("tavily search error: %s", e)
            return {"provider": "tavily", "error": str(e), "results": []}

        ledger.record("tavily", 2 if depth == "advanced" else 1)
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "published_at": r.get("published_date", ""),
                "source": _host(r.get("url", "")),
                "score": r.get("score"),
                "provider": "tavily",
            }
            for r in (resp.get("results") or [])
        ]
        return {
            "provider": "tavily",
            "query": query,
            "answer": resp.get("answer", ""),
            "results": results,
        }

    return tavily_search


def make_tavily_extract(cfg: Config, ledger: QuotaLedger):
    def tavily_extract(urls: list[str]) -> dict[str, Any]:
        """Extract clean article text for up to 20 URLs via Tavily."""
        if not urls:
            return {"provider": "tavily", "error": "urls empty", "results": []}
        try:
            from tavily import TavilyClient  # type: ignore

            client = TavilyClient(api_key=cfg.tavily_api_key)
            resp = client.extract(urls=urls[:20])
        except Exception as e:
            log.warning("tavily extract error: %s", e)
            return {"provider": "tavily", "error": str(e), "results": []}

        ledger.record("tavily", len(urls[:20]))
        results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", "") or "",
                "text": r.get("raw_content", "") or r.get("content", "") or "",
                "fetch_failed": not bool(r.get("raw_content") or r.get("content")),
                "source": _host(r.get("url", "")),
            }
            for r in (resp.get("results") or [])
        ]
        return {"provider": "tavily", "results": results}

    return tavily_extract


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc
    except Exception:
        return ""
