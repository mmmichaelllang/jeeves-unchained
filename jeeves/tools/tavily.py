"""Tavily search + extract. AI-native SERP plus full-text article fetcher."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import Config
from .quota import QuotaLedger
from .rate_limits import acquire as _rl_acquire
from .telemetry import emit as _emit

log = logging.getLogger(__name__)


def make_tavily_search(cfg: Config, ledger: QuotaLedger):
    def tavily_search(
        query: str = "",
        max_results: int = 8,
        depth: str = "basic",
        time_range: str | None = None,
    ) -> str:
        """Tavily AI-native search with an optional synthesized answer.

        Args:
            query: question or keyword string (required — must be a non-empty string).
            max_results: max results to return.
            depth: 'basic' (1 credit) or 'advanced' (2 credits).
            time_range: bias toward freshness — 'day' / 'week' / 'month' / 'year'.
                None = no freshness filter (default Tavily ranking).

        Returns a JSON string so LlamaIndex's _parse_tool_output() produces valid
        JSON in the NIM context rather than Python repr with single quotes.
        """
        if not (query or "").strip():
            log.warning("tavily_search called with empty query — returning error string")
            return (
                "ERROR: tavily_search requires a non-empty 'query' argument. "
                "Example: tavily_search(query='Edmonds WA local news today')"
            )
        t0 = time.monotonic()
        try:
            from tavily import TavilyClient  # type: ignore

            client = TavilyClient(api_key=cfg.tavily_api_key)
            with _rl_acquire("tavily"):
                resp = client.search(
                    query=query,
                    max_results=max_results,
                    search_depth=depth,
                    include_answer=True,
                )
        except Exception as e:
            log.warning("tavily search error: %s", e)
            _emit(
                "tool_call",
                provider="tavily",
                query=query,
                ok=False,
                results=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(e)[:200],
            )
            return json.dumps({"provider": "tavily", "error": str(e), "results": []})

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
        urls_returned = [r.get("url", "") for r in results if r.get("url")][:10]
        _emit(
            "tool_call",
            provider="tavily",
            query=query,
            depth=depth,
            ok=True,
            results=len(results),
            latency_ms=int((time.monotonic() - t0) * 1000),
            urls_returned=urls_returned,
        )
        return json.dumps({
            "provider": "tavily",
            "query": query,
            "answer": resp.get("answer", ""),
            "results": results,
        })

    return tavily_search


def make_tavily_extract(cfg: Config, ledger: QuotaLedger):
    def tavily_extract(urls: list[str]) -> str:
        """Extract clean article text for up to 10 URLs via Tavily.

        Each result's `text` is capped at 2500 chars so the FunctionAgent's
        context window doesn't fill from a single extraction turn.

        Fallback chain per URL:
          1. Tavily extract (this function's primary path).
          2. Headless Playwright + OpenRouter crystallizer (when Tavily fails
             entirely OR returns ``fetch_failed: true`` for a URL).
        Soft-fails to the empty result if Playwright is unavailable.

        Returns a JSON string so LlamaIndex's _parse_tool_output() produces valid
        JSON in the NIM context rather than Python repr with single quotes.
        """
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            return (
                "ERROR: tavily_extract requires a non-empty 'urls' list. "
                "Example: tavily_extract(urls=['https://example.com/article'])"
            )
        urls = urls[:10]

        results: list[dict[str, Any]] = []
        tavily_failed_completely = False
        t0 = time.monotonic()
        try:
            from tavily import TavilyClient  # type: ignore

            client = TavilyClient(api_key=cfg.tavily_api_key)
            with _rl_acquire("tavily"):
                resp = client.extract(urls=urls)
        except Exception as e:
            log.warning("tavily extract error: %s", e)
            tavily_failed_completely = True
            resp = {"results": []}
        _emit(
            "tool_call",
            provider="tavily_extract",
            urls=len(urls),
            ok=not tavily_failed_completely,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

        if not tavily_failed_completely:
            ledger.record("tavily", len(urls))

        # Build a lookup of Tavily results by URL so we can identify which
        # URLs failed and need a Playwright re-attempt.
        tavily_by_url: dict[str, dict[str, Any]] = {}
        for r in (resp.get("results") or []):
            u = r.get("url", "") or ""
            if not u:
                continue
            tavily_by_url[u] = r

        for url in urls:
            r = tavily_by_url.get(url, {})
            raw = r.get("raw_content", "") or r.get("content", "") or ""
            if raw:
                results.append({
                    "url": url,
                    "title": r.get("title", "") or "",
                    "text": raw[:2500],
                    "fetch_failed": False,
                    "source": _host(url),
                })
                continue

            # Playwright fallback for this URL — Tavily either failed
            # entirely or returned no body for this specific URL.
            pw = _playwright_extract_safe(url)
            if pw and pw.get("success") and pw.get("text"):
                results.append({
                    "url": url,
                    "title": pw.get("title", "") or "",
                    "text": str(pw.get("text", ""))[:2500],
                    "fetch_failed": False,
                    "source": _host(url),
                    "extracted_via": "playwright",
                })
            else:
                results.append({
                    "url": url,
                    "title": r.get("title", "") or "",
                    "text": "",
                    "fetch_failed": True,
                    "source": _host(url),
                })

        return json.dumps({"provider": "tavily", "results": results})

    return tavily_extract


def _playwright_extract_safe(url: str) -> dict[str, Any] | None:
    """Run the Playwright fallback extractor, swallowing every error.

    Returns the extractor's result dict on success-or-soft-fail, or None
    if the import itself blows up. Callers must check ``.get('success')``.
    """
    try:
        from .playwright_extractor import extract_article

        return extract_article(url, timeout_seconds=30, max_chars=2500)
    except Exception as e:
        log.debug("playwright fallback failed for %s: %s", url, e)
        return None


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc
    except Exception:
        return ""
