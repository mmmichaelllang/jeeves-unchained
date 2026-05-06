"""TinyFish API article extractor — optional fetch-chain step.

Position in the fetch chain (sprint-18 rollout):

    httpx+trafilatura → Jina(r.jina.ai) → **TinyFish** → playwright_extractor

TinyFish is a managed browser-automation/extraction SaaS. It executes JS,
solves consent dialogs server-side, and returns clean markdown via a single
HTTP call. We treat it as a peer to Firecrawl and Playwright: cheaper-than-
Playwright when minute-billed CI is the alternative; more reliable than Jina
on JS-heavy SPAs and soft-paywalled hosts.

Public surface
--------------
``extract_article(url, *, timeout_seconds, max_chars, ledger) -> dict``
    Fail-soft: never raises. Returns success=False with error key on any
    failure. Shape matches ``firecrawl_extractor.extract_article`` and
    ``playwright_extractor.extract_article`` so call-sites can swap.

Return shape::

    {
        "url":            str,
        "title":          str,
        "text":           str,    # markdown, truncated to max_chars
        "success":        bool,
        "extracted_via":  "tinyfish",
        "quality_score":  float,
        "error":          str,    # only present when success=False
    }

Feature flag
------------
``TINYFISH_API_KEY`` env var. Absent → immediate ``success=False`` with an
"api key not set" error (silent disable mirrors firecrawl).

Two opt-in switches gate active use:

* ``JEEVES_TINYFISH_SHADOW=1`` — playwright_extractor fires TinyFish in
  parallel and writes both results to ``sessions/shadow-tinyfish-<date>.jsonl``
  for offline comparison. Production output unchanged.
* ``JEEVES_USE_TINYFISH=1`` — registers ``tinyfish_extract`` as a real agent
  tool and inserts it into the enrichment fetch chain ahead of Playwright.

Quota
-----
Tracked under ``"tinyfish"`` in the ledger (DEFAULT_STATE entry +
DAILY_HARD_CAPS=30/day to prevent runaway spend during the canary).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .rate_limits import acquire as _rl_acquire
from .telemetry import emit as _emit

log = logging.getLogger(__name__)

_TINYFISH_ENDPOINT = os.environ.get(
    "TINYFISH_ENDPOINT",
    "https://api.tinyfish.io/v1/extract",
)
_TINYFISH_SEARCH_ENDPOINT = os.environ.get(
    "TINYFISH_SEARCH_ENDPOINT",
    "https://api.tinyfish.io/v1/search",
)
_MIN_CONTENT_LENGTH = 300


def extract_article(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12_000,
    ledger: Any = None,
) -> dict:
    """Fetch article content via the TinyFish API.

    Parameters
    ----------
    url:
        Target URL to fetch.
    timeout_seconds:
        HTTP request timeout in seconds (default 30).
    max_chars:
        Truncate returned markdown to this many characters (default 12 000).
    ledger:
        Optional ``QuotaLedger``. When provided we record both a monthly
        ``"tinyfish"`` count and a daily counter (for the
        ``DAILY_HARD_CAPS["tinyfish"]`` guard rail).

    Returns
    -------
    dict
        See module docstring for shape. ``quality_score`` is 0.0 on failure
        and 0.8 on success — slightly below firecrawl's 0.85 because
        TinyFish has not yet earned its calibration in our pipeline.
    """
    base: dict = {
        "url": url,
        "title": "",
        "text": "",
        "success": False,
        "extracted_via": "tinyfish",
        "quality_score": 0.0,
    }

    if not url:
        base["error"] = "empty url"
        return base

    api_key = os.environ.get("TINYFISH_API_KEY", "").strip()
    if not api_key:
        log.debug("tinyfish: TINYFISH_API_KEY not set, skipping %s", url)
        base["error"] = "TINYFISH_API_KEY not set"
        return base

    # Hard daily cap — refuse before making the network call so a runaway
    # loop cannot burn the budget. Mirrors gemini_grounded behaviour.
    if ledger is not None:
        try:
            from .quota import DAILY_HARD_CAPS, QuotaExceeded

            cap = DAILY_HARD_CAPS.get("tinyfish")
            if cap is not None:
                ledger.check_daily_allow("tinyfish", hard_cap=cap)
        except Exception as exc:
            # QuotaExceeded gets caught here and turned into a soft failure
            # so the caller's enrichment loop can fall through to Playwright.
            if exc.__class__.__name__ == "QuotaExceeded":
                log.warning("tinyfish: daily cap reached, skipping %s", url)
                base["error"] = f"tinyfish daily cap: {exc}"
                return base
            log.debug("tinyfish: quota check failed: %s", exc)

    payload = {
        "url": url,
        "format": "markdown",
        "main_content": True,
        "timeout_ms": int(timeout_seconds * 1000),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    log.debug("tinyfish: requesting %s", url)
    t0 = time.monotonic()
    try:
        with _rl_acquire("tinyfish"):
            response = httpx.post(
                _TINYFISH_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=float(timeout_seconds),
            )
    except httpx.TimeoutException as exc:
        log.warning("tinyfish: timeout fetching %s: %s", url, exc)
        base["error"] = f"tinyfish timeout: {exc}"
        _emit(
            "tool_call",
            provider="tinyfish",
            url=url,
            ok=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error="timeout",
        )
        return base
    except Exception as exc:
        log.warning("tinyfish: request error for %s: %s", url, exc)
        base["error"] = f"tinyfish request error: {exc}"
        _emit(
            "tool_call",
            provider="tinyfish",
            url=url,
            ok=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=str(exc)[:200],
        )
        return base

    # Always charge the ledger on a completed HTTP round-trip — even non-200s
    # cost something on most managed-extractor pricing models.
    if ledger is not None:
        try:
            ledger.record("tinyfish")
            ledger.record_daily("tinyfish")
        except Exception as exc:
            log.debug("tinyfish: ledger.record failed: %s", exc)

    if response.status_code == 429:
        # Trip the daily cap immediately so subsequent calls in this run
        # short-circuit. Mirrors gemini_grounded 429 handling.
        if ledger is not None:
            try:
                from .quota import DAILY_HARD_CAPS

                cap = DAILY_HARD_CAPS.get("tinyfish", 30)
                # Bump the daily counter to the cap so check_daily_allow
                # rejects every following call this UTC day.
                cur = ledger.daily_used("tinyfish")
                if cur < cap:
                    ledger.record_daily("tinyfish", cap - cur)
            except Exception:
                pass
        base["error"] = "tinyfish rate limited (429)"
        return base

    if response.status_code != 200:
        log.warning("tinyfish: HTTP %s for %s", response.status_code, url)
        base["error"] = f"tinyfish api error: {response.status_code}"
        return base

    try:
        data = response.json()
    except Exception as exc:
        log.warning("tinyfish: JSON decode error for %s: %s", url, exc)
        base["error"] = f"tinyfish json decode error: {exc}"
        return base

    # TinyFish response shape (per their public docs as of 2026-05):
    #   {"success": true, "data": {"markdown": "...", "title": "...", "metadata": {...}}}
    # Defensive extraction handles vendor-side schema drift.
    if isinstance(data, dict) and data.get("success") is False:
        api_error = data.get("error", "unknown error")
        log.warning("tinyfish: API success=false for %s: %s", url, api_error)
        base["error"] = f"tinyfish api error: {api_error}"
        return base

    inner = data.get("data") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        # Some endpoints return the payload at top-level. Fall through.
        inner = data if isinstance(data, dict) else {}

    markdown = inner.get("markdown") or inner.get("content") or inner.get("text") or ""
    title = (
        inner.get("title")
        or (inner.get("metadata") or {}).get("title")
        or ""
    )

    if not isinstance(markdown, str):
        markdown = str(markdown or "")
    if not isinstance(title, str):
        title = str(title or "")

    if len(markdown) < _MIN_CONTENT_LENGTH:
        log.warning(
            "tinyfish: returned %d chars (< %d) for %s",
            len(markdown),
            _MIN_CONTENT_LENGTH,
            url,
        )
        base["text"] = markdown
        base["error"] = "tinyfish returned no content"
        return base

    base.update(
        {
            "title": title,
            "text": markdown[:max_chars],
            "success": True,
            "quality_score": 0.8,
        }
    )
    log.debug("tinyfish: extracted %d chars from %s", len(markdown), url)
    _emit(
        "tool_call",
        provider="tinyfish",
        url=url,
        ok=True,
        status=response.status_code,
        chars=len(markdown),
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
    return base


# ---------------------------------------------------------------------------
# Sprint-19: TinyFish search — ranked SERP + optional raw-content extraction
# in a single round-trip. Designed as a Serper peer for JS-heavy / paywalled /
# site-scoped queries where Serper returns thin metadata.
# ---------------------------------------------------------------------------

def search(
    query: str,
    *,
    num: int = 10,
    include_raw_content: bool = False,
    site: str | None = None,
    country: str = "us",
    timeout_seconds: int = 30,
    ledger: Any = None,
) -> dict:
    """TinyFish managed search.

    Parameters
    ----------
    query:
        Non-empty search string.
    num:
        Max number of organic results to return (default 10).
    include_raw_content:
        When True, TinyFish renders each top result and returns full
        markdown in the same payload — collapses search + extract into one
        call. Costs more credits per call (vendor docs: 5 vs 2).
    site:
        Optional ``site:`` filter (e.g. ``"arxiv.org"``).
    country:
        Localisation hint passed to the SERP backend.
    timeout_seconds:
        HTTP request timeout (default 30).
    ledger:
        Optional ``QuotaLedger``. Records monthly + daily counters under the
        ``"tinyfish_search"`` key. When the daily cap from
        ``DAILY_HARD_CAPS["tinyfish_search"]`` is exceeded the call returns
        immediately with ``success=False``.

    Returns
    -------
    dict
        ``{provider, query, success, results: [{title, url, snippet,
        published_at, source, content?}], error?}``. Fail-soft: every error
        path returns ``success=False`` with a populated ``error`` and an
        empty ``results`` list.
    """
    base: dict = {
        "provider": "tinyfish_search",
        "query": query,
        "success": False,
        "results": [],
    }

    if not (query or "").strip():
        base["error"] = "empty query"
        return base

    api_key = os.environ.get("TINYFISH_API_KEY", "").strip()
    if not api_key:
        base["error"] = "TINYFISH_API_KEY not set"
        return base

    # Hard-cap guard — refuse before the network call.
    if ledger is not None:
        try:
            from .quota import DAILY_HARD_CAPS, QuotaExceeded  # noqa: F401

            cap = DAILY_HARD_CAPS.get("tinyfish_search")
            if cap is not None:
                ledger.check_daily_allow("tinyfish_search", hard_cap=cap)
        except Exception as exc:
            if exc.__class__.__name__ == "QuotaExceeded":
                base["error"] = f"tinyfish_search daily cap: {exc}"
                return base

    payload: dict[str, Any] = {
        "query": query,
        "num_results": int(num),
        "country": country,
        "include_raw_content": bool(include_raw_content),
    }
    if site:
        payload["site"] = site

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    log.debug("tinyfish_search: q=%r num=%d raw=%s", query, num, include_raw_content)
    t0 = time.monotonic()
    try:
        with _rl_acquire("tinyfish_search"):
            response = httpx.post(
                _TINYFISH_SEARCH_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=float(timeout_seconds),
            )
    except httpx.TimeoutException as exc:
        base["error"] = f"tinyfish_search timeout: {exc}"
        _emit(
            "tool_call",
            provider="tinyfish_search",
            query=query,
            ok=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error="timeout",
        )
        return base
    except Exception as exc:
        base["error"] = f"tinyfish_search request error: {exc}"
        _emit(
            "tool_call",
            provider="tinyfish_search",
            query=query,
            ok=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=str(exc)[:200],
        )
        return base

    # Charge ledger on every completed round-trip.
    if ledger is not None:
        try:
            ledger.record("tinyfish_search")
            ledger.record_daily("tinyfish_search")
        except Exception:
            pass

    if response.status_code == 429:
        if ledger is not None:
            try:
                from .quota import DAILY_HARD_CAPS

                cap = DAILY_HARD_CAPS.get("tinyfish_search", 8)
                cur = ledger.daily_used("tinyfish_search")
                if cur < cap:
                    ledger.record_daily("tinyfish_search", cap - cur)
            except Exception:
                pass
        base["error"] = "tinyfish_search rate limited (429)"
        return base

    if response.status_code != 200:
        base["error"] = f"tinyfish_search api error: {response.status_code}"
        return base

    try:
        data = response.json()
    except Exception as exc:
        base["error"] = f"tinyfish_search json decode error: {exc}"
        return base

    if isinstance(data, dict) and data.get("success") is False:
        base["error"] = f"tinyfish_search api error: {data.get('error', 'unknown')}"
        return base

    inner = data.get("data") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        inner = data if isinstance(data, dict) else {}
    items = inner.get("results") or inner.get("organic") or []
    if not isinstance(items, list):
        items = []

    results: list[dict[str, Any]] = []
    for it in items[:num]:
        if not isinstance(it, dict):
            continue
        result: dict[str, Any] = {
            "title": str(it.get("title") or ""),
            "url": str(it.get("url") or it.get("link") or ""),
            "snippet": str(it.get("snippet") or it.get("description") or "")[:1200],
            "published_at": str(it.get("published_at") or it.get("date") or ""),
            "source": str(it.get("source") or ""),
            "provider": "tinyfish",
        }
        if include_raw_content:
            raw = it.get("content") or it.get("markdown") or ""
            if raw:
                result["content"] = str(raw)[:6000]
        results.append(result)

    base.update({"success": True, "results": results})
    _emit(
        "tool_call",
        provider="tinyfish_search",
        query=query,
        ok=True,
        status=response.status_code,
        results=len(results),
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
    return base

