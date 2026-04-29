"""Serper.dev — Google SERP API. Cheapest of the four providers."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import Config
from .quota import QuotaLedger

log = logging.getLogger(__name__)

ENDPOINT = "https://google.serper.dev/search"

# Module-level client reuses the TCP connection across sectors instead of
# opening a new handshake for every serper_search tool call.
_HTTP_CLIENT = httpx.Client(timeout=20.0)


def make_serper_search(cfg: Config, ledger: QuotaLedger):
    def serper_search(query: str = "", num: int = 10, tbs: str | None = None) -> str:
        """Google SERP via Serper.dev.

        Args:
            query: search query (required — must be a non-empty string).
            num: number of organic results to return (max ~100).
            tbs: Google TBS filter, e.g. 'qdr:d' (last day), 'qdr:w' (last week).

        Returns a JSON string so LlamaIndex's _parse_tool_output() produces valid
        JSON in the NIM context rather than Python repr with single quotes.
        """
        if not (query or "").strip():
            log.warning("serper_search called with empty query — returning error string")
            return (
                "ERROR: serper_search requires a non-empty 'query' argument. "
                "Example: serper_search(query='Edmonds WA news today')"
            )
        headers = {"X-API-KEY": cfg.serper_api_key, "Content-Type": "application/json"}
        payload: dict[str, Any] = {"q": query, "num": num}
        if tbs:
            payload["tbs"] = tbs

        try:
            r = _HTTP_CLIENT.post(ENDPOINT, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("serper error: %s", e)
            return json.dumps({"provider": "serper", "error": str(e), "results": []})

        ledger.record("serper", 1)
        organic = data.get("organic") or []
        results = [
            {
                "title": o.get("title", ""),
                "url": o.get("link", ""),
                "snippet": o.get("snippet", ""),
                "published_at": o.get("date", ""),
                "source": o.get("source", ""),
                "provider": "serper",
            }
            for o in organic
        ]
        return json.dumps({"provider": "serper", "query": query, "results": results})

    return serper_search
