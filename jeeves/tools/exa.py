"""Exa — neural semantic search. Good for intellectual/long-form queries."""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from .quota import QuotaLedger

log = logging.getLogger(__name__)


def make_exa_search(cfg: Config, ledger: QuotaLedger):
    def exa_search(
        query: str = "",
        num_results: int = 10,
        category: str | None = None,
        search_type: str = "auto",
        text_max_chars: int = 20000,
    ) -> dict[str, Any]:
        """Exa neural semantic search with full-text content.

        Auth: `x-api-key` header (handled by the exa-py SDK internally from
        `cfg.exa_api_key`). Endpoint: `https://api.exa.ai/search`.

        Args:
            query: natural-language query (required — must be a non-empty string).
            num_results: result count (default 10).
            category: optional category, e.g. 'news', 'research paper', 'company'.
            search_type: one of 'auto' (default, ~1s balanced), 'fast' (~450ms),
                'instant' (~250ms), 'deep-lite' (~4s), 'deep' (~4-15s),
                'deep-reasoning' (~12-40s, strongest synthesis).
            text_max_chars: cap on per-result full-text (default 20000, ~3000 words
                per article — enough for synthesis without requiring a follow-up extract).

        Returns normalized hits with `snippet` (first 600 chars) AND `text`
        (capped full content), so the agent can skip a follow-up extraction
        call on Exa hits.
        """
        if not (query or "").strip():
            log.warning("exa_search called with empty query — returning error string")
            return (
                "ERROR: exa_search requires a non-empty 'query' argument. "
                "Example: exa_search(query='triadic ontology 2026', search_type='auto', num_results=3)"
            )
        try:
            from exa_py import Exa  # type: ignore

            client = Exa(api_key=cfg.exa_api_key)
            kwargs: dict[str, Any] = {
                "type": search_type,
                "num_results": num_results,
                "contents": {"text": {"max_characters": text_max_chars}},
            }
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
                "text": getattr(r, "text", "") or "",
                "published_at": getattr(r, "published_date", "") or "",
                "source": _host(getattr(r, "url", "")),
                "score": getattr(r, "score", None),
                "provider": "exa",
            }
            for r in (resp.results or [])
        ]
        return {
            "provider": "exa",
            "query": query,
            "type": search_type,
            "results": results,
        }

    return exa_search


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc
    except Exception:
        return ""
