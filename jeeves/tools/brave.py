"""Brave Search API — independent web index for search-cascade fallback.

Added 2026-06-16 after serper exhausted its credit balance and starved the
crawl4ai URL-discovery path. Brave runs its own crawler/index (not derived
from Google), so it adds genuine result diversity, and the free tier allows
~2k queries/month. Returns the same wrapper shape as serper/tavily/exa
({provider, query, results: [{title, url, snippet, ...}]}) so it drops into
the discovery cascade with no special handling.
"""

from __future__ import annotations

import atexit
import json
import logging
import time
from typing import Any

import httpx

from ..config import Config
from .quota import QuotaLedger
from .telemetry import emit as _emit

log = logging.getLogger(__name__)

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# Reuse the TCP connection across sectors, mirroring serper.py.
_HTTP_CLIENT = httpx.Client(timeout=20.0)
atexit.register(_HTTP_CLIENT.close)


def make_brave_search(cfg: Config, ledger: QuotaLedger):
    def brave_search(query: str = "", num: int = 10, freshness: str | None = None) -> str:
        """Brave web search.

        Args:
            query: search query (required — must be a non-empty string).
            num: number of results to return (Brave caps `count` at 20).
            freshness: optional recency filter, e.g. 'pd' (past day),
                'pw' (past week), 'pm' (past month).

        Returns a JSON string {provider, query, results: [{title, url,
        snippet, published_at, source, provider}]}. Matches the serper/tavily/
        exa wrapper shape so the discovery cascade can consume it uniformly.
        """
        if not (query or "").strip():
            log.warning("brave_search called with empty query — returning error string")
            return json.dumps(
                {"provider": "brave", "error": "empty query", "results": []}
            )
        if not cfg.brave_api_key:
            return json.dumps(
                {"provider": "brave", "error": "BRAVE_API_KEY not set", "results": []}
            )

        headers = {
            "X-Subscription-Token": cfg.brave_api_key,
            "Accept": "application/json",
        }
        params: dict[str, Any] = {"q": query, "count": min(int(num), 20)}
        if freshness:
            params["freshness"] = freshness

        t0 = time.monotonic()
        status_code: int | None = None
        try:
            r = _HTTP_CLIENT.get(ENDPOINT, params=params, headers=headers)
            status_code = r.status_code
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("brave error: %s", e)
            _emit(
                "tool_call",
                provider="brave",
                query=query,
                ok=False,
                status=status_code,
                results=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(e)[:200],
            )
            return json.dumps({"provider": "brave", "error": str(e), "results": []})

        ledger.record("brave", 1)
        web_results = (data.get("web") or {}).get("results") or []
        results = [
            {
                "title": o.get("title", ""),
                "url": o.get("url", ""),
                "snippet": o.get("description", ""),
                "published_at": o.get("age", ""),
                "source": (o.get("profile") or {}).get("name", ""),
                "provider": "brave",
            }
            for o in web_results
        ]
        latency_ms = int((time.monotonic() - t0) * 1000)
        urls_returned = [r.get("url", "") for r in results if r.get("url")][:10]
        _emit(
            "tool_call",
            provider="brave",
            query=query,
            ok=True,
            status=status_code,
            results=len(results),
            latency_ms=latency_ms,
            urls_returned=urls_returned,
        )
        return json.dumps({"provider": "brave", "query": query, "results": results})

    return brave_search
