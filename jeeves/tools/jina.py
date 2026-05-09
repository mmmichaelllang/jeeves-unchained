"""Jina AI search/deepsearch/rerank tools — sprint-19 search-agent expansion.

Jina is already used as a Reader (r.jina.ai) for clean-markdown article
extraction inside ``talk_of_the_town`` and the enrichment fallback chain.
This module extends it into a full research-agent surface:

* ``s.jina.ai`` Search        — direct peer to Serper (returns ranked SERP +
                                  Reader-style markdown snippets in one call)
* ``deepsearch.jina.ai``      — multi-hop "search → read → reason" loop
                                  collapsing 5-7 chained Serper/Tavily calls
                                  into a single call
* ``api.jina.ai/v1/rerank``   — semantic reranker over a candidate list
                                  unioned from any combination of search_*
                                  providers

All three return JSON strings (``json.dumps(...)``) — required by the
LlamaIndex/NIM tool contract documented in CLAUDE.md `<nim-gotchas>`.
None of the tools raise on transport errors; failures degrade to an
``{"provider": ..., "error": ..., "results": []}`` shape so the agent loop
keeps moving.

Public factories (parallel to ``serper.make_serper_search``):

* ``make_jina_search(cfg, ledger)``       → ``jina_search``      callable
* ``make_jina_deepsearch(cfg, ledger)``   → ``jina_deepsearch``  callable
* ``make_jina_rerank(cfg, ledger)``       → ``jina_rerank``      callable

Each closure captures the ``Config`` (for ``cfg.jina_api_key``) and the
``QuotaLedger`` (for ``record`` + ``check_daily_allow``). Hard daily caps
live in :mod:`jeeves.tools.quota` (``DAILY_HARD_CAPS``); on cap the call
returns the standard error-shape JSON instead of raising.
"""

from __future__ import annotations

import atexit
import json
import logging
import time
from typing import Any

import httpx

from ..config import Config
from .quota import DAILY_HARD_CAPS, QuotaExceeded, QuotaLedger
from .rate_limits import acquire as _rl_acquire
from .telemetry import emit as _emit

log = logging.getLogger(__name__)

_SEARCH_ENDPOINT = "https://s.jina.ai/"
_DEEPSEARCH_ENDPOINT = "https://deepsearch.jina.ai/v1/chat/completions"
_RERANK_ENDPOINT = "https://api.jina.ai/v1/rerank"
_RERANKER_MODEL = "jina-reranker-v2-base-multilingual"
_DEEPSEARCH_MODEL = "jina-deepsearch-v1"

# Module-level clients — reuse TCP connection across sectors.
_HTTP_CLIENT = httpx.Client(timeout=20.0)
_DEEPSEARCH_CLIENT = httpx.Client(timeout=120.0)  # multi-hop loops can run long
atexit.register(_HTTP_CLIENT.close)
atexit.register(_DEEPSEARCH_CLIENT.close)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(provider: str, message: str, **extra: Any) -> str:
    """Return a JSON error envelope matching the search-tool contract."""
    payload: dict[str, Any] = {"provider": provider, "error": message, "results": []}
    payload.update(extra)
    return json.dumps(payload)


def _check_cap(ledger: QuotaLedger, provider: str) -> str | None:
    """Pre-flight daily cap guard. Returns an error JSON string when capped,
    None otherwise."""
    cap = DAILY_HARD_CAPS.get(provider)
    if cap is None:
        return None
    try:
        ledger.check_daily_allow(provider, hard_cap=cap)
    except QuotaExceeded as exc:
        log.warning("%s: daily cap hit: %s", provider, exc)
        return _err(provider, f"{provider} daily cap reached: {exc}")
    return None


def _bump_to_cap_on_429(ledger: QuotaLedger, provider: str) -> None:
    """On a 429 response, push the daily counter to the cap so subsequent
    calls in this run short-circuit. Mirrors the gemini_grounded /
    tinyfish behaviour documented in the existing tools."""
    cap = DAILY_HARD_CAPS.get(provider)
    if cap is None:
        return
    try:
        cur = ledger.daily_used(provider)
        if cur < cap:
            ledger.record_daily(provider, cap - cur)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. Jina Search — direct Serper peer
# ---------------------------------------------------------------------------

def make_jina_search(cfg: Config, ledger: QuotaLedger):
    def jina_search(query: str = "", num: int = 8, site: str | None = None) -> str:
        """Jina AI search (s.jina.ai) — semantic SERP with extracted snippets.

        Args:
            query: search query (required, non-empty).
            num: max results to return (default 8).
            site: optional site-scope (e.g. 'arxiv.org'). Maps to the
                  ``X-Site`` header which Jina honours as a domain filter.

        Returns a JSON string {provider, query, results: [{title, url,
        snippet, published_at, source, provider}]}. PREFER OVER serper_search
        when you also need clean text — Jina returns extracted markdown
        snippets, often saving a follow-up extract_* call.
        """
        if not (query or "").strip():
            return _err("jina_search", "empty query — pass a non-empty 'query' arg")

        if not (cfg.jina_api_key or "").strip():
            return _err("jina_search", "JINA_API_KEY not set")

        capped = _check_cap(ledger, "jina_search")
        if capped is not None:
            return capped

        headers: dict[str, str] = {
            "Authorization": f"Bearer {cfg.jina_api_key}",
            "Accept": "application/json",
            "X-Engine": "direct",
            "X-Return-Format": "json",
        }
        if site:
            headers["X-Site"] = site

        url = f"{_SEARCH_ENDPOINT}?q={httpx.QueryParams({'q': query})['q']}"
        t0 = time.monotonic()
        try:
            with _rl_acquire("jina_search"):
                r = _HTTP_CLIENT.get(url, headers=headers)
        except Exception as exc:
            log.warning("jina_search transport error: %s", exc)
            _emit(
                "tool_call",
                provider="jina_search",
                query=query,
                ok=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc)[:200],
            )
            return _err("jina_search", f"transport error: {exc}")

        # Always charge ledger on a completed round-trip.
        try:
            ledger.record("jina_search", 1)
            ledger.record_daily("jina_search", 1)
        except Exception:
            pass

        if r.status_code == 429:
            _bump_to_cap_on_429(ledger, "jina_search")
            return _err("jina_search", "rate limited (429)")
        if r.status_code != 200:
            return _err("jina_search", f"http {r.status_code}")

        try:
            data = r.json()
        except Exception as exc:
            return _err("jina_search", f"json decode error: {exc}")

        # Jina returns either {"data": [...]} or a list directly depending on
        # endpoint version. Normalise to a list of result dicts.
        items: list[dict[str, Any]]
        if isinstance(data, dict):
            payload = data.get("data") or data.get("results") or []
            items = payload if isinstance(payload, list) else []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        results = []
        for it in items[:num]:
            if not isinstance(it, dict):
                continue
            results.append(
                {
                    "title": str(it.get("title") or ""),
                    "url": str(it.get("url") or it.get("link") or ""),
                    "snippet": str(
                        it.get("description")
                        or it.get("snippet")
                        or it.get("content")
                        or ""
                    )[:1200],
                    "published_at": str(it.get("date") or it.get("published_at") or ""),
                    "source": str(it.get("source") or ""),
                    "provider": "jina",
                }
            )
        urls_returned = [r_.get("url", "") for r_ in results if r_.get("url")][:10]
        _emit(
            "tool_call",
            provider="jina_search",
            query=query,
            ok=True,
            status=r.status_code,
            results=len(results),
            latency_ms=int((time.monotonic() - t0) * 1000),
            urls_returned=urls_returned,
        )
        return json.dumps({"provider": "jina_search", "query": query, "results": results})

    return jina_search


# ---------------------------------------------------------------------------
# 2. Jina DeepSearch — multi-hop search-and-read
# ---------------------------------------------------------------------------

def make_jina_deepsearch(cfg: Config, ledger: QuotaLedger):
    def jina_deepsearch(question: str = "", reasoning_effort: str = "low") -> str:
        """Jina DeepSearch — agentic multi-hop search-read-reason loop.

        Args:
            question: research question (required).
            reasoning_effort: one of ``low|medium|high``. Higher values
                              produce more hops + tokens; ``low`` is the
                              right default for routine sectors.

        Returns JSON {provider, question, answer, citations: [{url,title}],
        visited_urls}. Use sparingly — one call typically replaces 5+ chained
        Serper/Tavily/extract operations on deep sectors but is slow (15-90s)
        and counts heavily against the daily cap.
        """
        if not (question or "").strip():
            return _err("jina_deepsearch", "empty question")

        if not (cfg.jina_api_key or "").strip():
            return _err("jina_deepsearch", "JINA_API_KEY not set")

        capped = _check_cap(ledger, "jina_deepsearch")
        if capped is not None:
            return capped

        headers = {
            "Authorization": f"Bearer {cfg.jina_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body: dict[str, Any] = {
            "model": _DEEPSEARCH_MODEL,
            "messages": [{"role": "user", "content": question}],
            "reasoning_effort": reasoning_effort,
            "stream": False,
        }
        t0 = time.monotonic()
        try:
            with _rl_acquire("jina_deepsearch"):
                r = _DEEPSEARCH_CLIENT.post(_DEEPSEARCH_ENDPOINT, headers=headers, json=body)
        except Exception as exc:
            log.warning("jina_deepsearch transport error: %s", exc)
            _emit(
                "tool_call",
                provider="jina_deepsearch",
                question=question,
                ok=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc)[:200],
            )
            return _err("jina_deepsearch", f"transport error: {exc}")

        try:
            ledger.record("jina_deepsearch", 1)
            ledger.record_daily("jina_deepsearch", 1)
        except Exception:
            pass

        if r.status_code == 429:
            _bump_to_cap_on_429(ledger, "jina_deepsearch")
            return _err("jina_deepsearch", "rate limited (429)")
        if r.status_code != 200:
            return _err("jina_deepsearch", f"http {r.status_code}")

        try:
            data = r.json()
        except Exception as exc:
            return _err("jina_deepsearch", f"json decode error: {exc}")

        # OpenAI-compatible response shape.
        try:
            choice = (data.get("choices") or [{}])[0] or {}
            answer = (choice.get("message") or {}).get("content") or ""
        except Exception:
            answer = ""

        # DeepSearch returns visited URLs in either ``visitedURLs`` or
        # ``annotations``. Defensive extraction tolerates either.
        visited = data.get("visitedURLs") or data.get("visited_urls") or []
        annotations = data.get("annotations") or []
        citations: list[dict[str, str]] = []
        if isinstance(annotations, list):
            for ann in annotations:
                if isinstance(ann, dict):
                    cite = ann.get("url_citation") or ann
                    citations.append(
                        {
                            "url": str(cite.get("url") or ""),
                            "title": str(cite.get("title") or ""),
                        }
                    )

        _emit(
            "tool_call",
            provider="jina_deepsearch",
            question=question,
            ok=True,
            status=r.status_code,
            answer_chars=len(str(answer)),
            citations=len(citations),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return json.dumps(
            {
                "provider": "jina_deepsearch",
                "question": question,
                "answer": str(answer)[:8000],
                "citations": citations[:30],
                "visited_urls": [str(u) for u in (visited if isinstance(visited, list) else [])][:30],
            }
        )

    return jina_deepsearch


# ---------------------------------------------------------------------------
# 3. Jina Rerank — semantic reorder over candidate list
# ---------------------------------------------------------------------------

def make_jina_rerank(cfg: Config, ledger: QuotaLedger):
    def jina_rerank(query: str = "", documents: list[str] | None = None, top_n: int = 8) -> str:
        """Jina reranker — reorder a candidate list by semantic relevance.

        Args:
            query: anchor query.
            documents: list of strings (title + ' || ' + url + ' || ' + snippet
                       is a reasonable shape; the reranker treats them as
                       opaque text).
            top_n: how many top results to return (default 8).

        Returns JSON {provider, ranked: [{index, score, document}]}. Use
        AFTER unioning candidates from 2+ search_* calls to pick the best
        subset before extraction.

        NIM-safe: ``documents`` arrives as a JSON array of strings (LlamaIndex
        sometimes hands the agent a single CSV string — both are tolerated).
        """
        if not (query or "").strip():
            return _err("jina_rerank", "empty query")

        if not (cfg.jina_api_key or "").strip():
            return _err("jina_rerank", "JINA_API_KEY not set")

        # Defensive coercion: Kimi sometimes synthesises a CSV string.
        docs: list[str]
        if documents is None:
            docs = []
        elif isinstance(documents, str):
            docs = [s.strip() for s in documents.split(",") if s.strip()]
        elif isinstance(documents, list):
            docs = [str(d) for d in documents if d]
        else:
            docs = []
        if not docs:
            return _err("jina_rerank", "empty documents")

        capped = _check_cap(ledger, "jina_rerank")
        if capped is not None:
            return capped

        headers = {
            "Authorization": f"Bearer {cfg.jina_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "model": _RERANKER_MODEL,
            "query": query,
            "documents": docs,
            "top_n": int(top_n),
        }
        t0 = time.monotonic()
        try:
            with _rl_acquire("jina_rerank"):
                r = _HTTP_CLIENT.post(_RERANK_ENDPOINT, headers=headers, json=body)
        except Exception as exc:
            log.warning("jina_rerank transport error: %s", exc)
            _emit(
                "tool_call",
                provider="jina_rerank",
                query=query,
                docs=len(docs),
                ok=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(exc)[:200],
            )
            return _err("jina_rerank", f"transport error: {exc}")

        try:
            ledger.record("jina_rerank", 1)
            ledger.record_daily("jina_rerank", 1)
        except Exception:
            pass

        if r.status_code == 429:
            _bump_to_cap_on_429(ledger, "jina_rerank")
            return _err("jina_rerank", "rate limited (429)")
        if r.status_code != 200:
            return _err("jina_rerank", f"http {r.status_code}")

        try:
            data = r.json()
        except Exception as exc:
            return _err("jina_rerank", f"json decode error: {exc}")

        items = data.get("results") if isinstance(data, dict) else None
        ranked: list[dict[str, Any]] = []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                idx = int(it.get("index", -1))
                score = float(it.get("relevance_score", 0.0) or 0.0)
                doc_text = ""
                if 0 <= idx < len(docs):
                    doc_text = docs[idx]
                ranked.append({"index": idx, "score": score, "document": doc_text})

        _emit(
            "tool_call",
            provider="jina_rerank",
            query=query,
            docs=len(docs),
            ok=True,
            status=r.status_code,
            ranked=len(ranked),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return json.dumps({"provider": "jina_rerank", "query": query, "ranked": ranked})

    return jina_rerank
