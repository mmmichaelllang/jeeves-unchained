"""Exa — neural semantic search. Good for intellectual/long-form queries."""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .quota import QuotaLedger

log = logging.getLogger(__name__)


def make_exa_search(cfg: Config, ledger: QuotaLedger):
    def exa_search(
        query: str,
        num_results: int = 10,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Exa neural semantic search.

        Args:
            query: natural language query (supports 'find similar to X' phrasing).
            num_results: result count.
            category: optional category, e.g. 'news', 'research paper', 'essay'.
        """
        try:
            from exa_py import Exa  # type: ignore

            client = Exa(api_key=cfg.exa_api_key)
            kwargs: dict[str, Any] = {"num_results": num_results}
            if category:
                kwargs["category"] = category
            resp = client.search(query, **kwargs)
        except Exception as e:
            log.warning("exa search error: %s", e)
            return {"provider": "exa", "error": str(e), "results": []}

        ledger.record("exa", 1)
        results = [
            {
                "title": getattr(r, "title", "") or "",
                "url": getattr(r, "url", "") or "",
                "snippet": (getattr(r, "text", "") or "")[:600],
                "published_at": getattr(r, "published_date", "") or "",
                "source": _host(getattr(r, "url", "")),
                "score": getattr(r, "score", None),
                "provider": "exa",
            }
            for r in (resp.results or [])
        ]
        return {"provider": "exa", "query": query, "results": results}

    return exa_search


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc
    except Exception:
        return ""
