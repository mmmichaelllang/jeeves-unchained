"""Serper.dev — Google SERP API. Cheapest of the four providers."""

from __future__ import annotations

import atexit
import json
import logging
import time
from typing import Any

import httpx

from ..config import Config
from .quota import QuotaLedger
from .rate_limits import acquire as _rl_acquire
from .search_shadow import maybe_run_shadows as _maybe_run_shadows
from .telemetry import emit as _emit

log = logging.getLogger(__name__)

ENDPOINT = "https://google.serper.dev/search"

# Module-level client reuses the TCP connection across sectors instead of
# opening a new handshake for every serper_search tool call.
_HTTP_CLIENT = httpx.Client(timeout=20.0)
atexit.register(_HTTP_CLIENT.close)


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

        t0 = time.monotonic()
        status_code: int | None = None
        try:
            with _rl_acquire("serper"):
                r = _HTTP_CLIENT.post(ENDPOINT, json=payload, headers=headers)
                status_code = r.status_code
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning("serper error: %s", e)
            _emit(
                "tool_call",
                provider="serper",
                query=query,
                ok=False,
                status=status_code,
                results=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(e)[:200],
            )
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
        latency_ms = int((time.monotonic() - t0) * 1000)
        # 2026-05-09: emit the top-10 result URLs so the body graduator can
        # correlate query → stuck-URL precisely. Cap at 10 to bound JSONL
        # line size; full result set still lands in the function return.
        urls_returned = [r.get("url", "") for r in results if r.get("url")][:10]
        _emit(
            "tool_call",
            provider="serper",
            query=query,
            ok=True,
            status=status_code,
            results=len(results),
            latency_ms=latency_ms,
            urls_returned=urls_returned,
        )

        # Shadow flags (sprint-19 slice E): fire any opt-in shadows in
        # parallel and write per-shadow JSONL. Production output (this
        # function's return value) is unchanged regardless of shadow result.
        try:
            _maybe_run_shadows(
                primary="serper",
                query=query,
                primary_results=results,
                primary_latency_ms=latency_ms,
                cfg=cfg,
                ledger=ledger,
            )
        except Exception as exc:  # belt-and-braces — shadows must never break primary
            log.debug("shadow runner failed: %s", exc)

        return json.dumps({"provider": "serper", "query": query, "results": results})

    return serper_search
