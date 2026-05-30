"""Scrapling stealth extractor — fetch-chain TIER 2.5 (between Crawl4AI and Playwright).

Position when ``JEEVES_USE_SCRAPLING=1``::

    httpx+trafilatura → Crawl4AI(news_short) → SCRAPLING → playwright_extractor

Uses ``scrapling.fetchers.StealthyFetcher``: Patchright/Camoufox under the
hood with Chrome TLS fingerprint, stealth headers, and Cloudflare-Turnstile
solve. Sits between Crawl4AI and Playwright in the cascade because

* Crawl4AI handles ``news_short`` hosts efficiently (~$0 cost, fast HTTP).
* Vanilla Playwright (raw Patchright) is the existing last-resort fallback.
* Scrapling occupies the middle: same Chromium binary as Playwright already
  installed by daily.yml, but with the stealth tier (Cloudflare solve,
  fingerprint rotation) we do NOT have today.

This module replaces the role ``tinyfish`` was meant to play in the original
fetch chain (sprint-18). TinyFish's vendor host ``api.tinyfish.io`` is
unreachable from GitHub-hosted runners (``EAI_NONAME``) — 47 failed
``tinyfish`` extract calls/week in production telemetry, all 100% failure.
Scrapling has the same role (managed-stealth article extraction) but runs
in-process on the Playwright Chromium binary daily.yml already provisions,
so no vendor DNS dependency.

Public surface
--------------
``extract_article(url, *, timeout_seconds, max_chars, ledger) -> dict``

Returns the same shape as ``tinyfish.extract_article`` /
``playwright_extractor.extract_article`` so call-sites can swap freely::

    {
        "url":            str,
        "title":          str,
        "text":           str,    # plain text, truncated to max_chars
        "success":        bool,
        "extracted_via":  "scrapling",
        "quality_score":  float,  # 0.85 on success — peer to firecrawl
        "error":          str,    # only present when success=False
    }

Fail-soft: never raises. Returns ``success=False`` with an ``error`` key on
any failure (import error, daily cap, network error, parse error, content
below ``_MIN_CONTENT_LENGTH``).

Feature flag
------------
``JEEVES_USE_SCRAPLING=1`` env var enables it in the fetch chain
(checked by ``enrichment.fetch_article_text``). When unset, this module
is dormant — no imports, no work.

Quota
-----
Tracked under ``"scrapling"`` in the ledger with daily cap 200
(see ``quota.py::DAILY_HARD_CAPS``). Cost is CI minutes only — no API
credits. Cap is a wall-clock guardrail, not a billing one.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from .rate_limits import acquire as _rl_acquire
from .telemetry import emit as _emit

log = logging.getLogger(__name__)

# Below this, treat as "no content" so the cascade can fall through to
# the next tier (Playwright). Matches tinyfish.py:73.
_MIN_CONTENT_LENGTH = 300


def extract_article(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12_000,
    ledger: Any = None,
) -> dict:
    """Fetch article content via Scrapling's StealthyFetcher.

    Parameters mirror ``tinyfish.extract_article`` and
    ``playwright_extractor.extract_article`` so the chain step is
    swappable.

    Fail-soft contract: this function MUST NOT raise. Every error path
    returns the base dict with ``success=False`` and an ``error`` key
    populated.
    """
    base: dict = {
        "url": url,
        "title": "",
        "text": "",
        "success": False,
        "extracted_via": "scrapling",
        "quality_score": 0.0,
    }

    if not url:
        base["error"] = "empty url"
        return base

    # Hard daily cap — refuse before launching the browser so a runaway
    # loop cannot burn CI minutes. Mirrors tinyfish + gemini_grounded
    # behaviour.
    if ledger is not None:
        try:
            from .quota import DAILY_HARD_CAPS, QuotaExceeded  # noqa: F401

            cap = DAILY_HARD_CAPS.get("scrapling")
            if cap is not None:
                ledger.check_daily_allow("scrapling", hard_cap=cap)
        except Exception as exc:
            if exc.__class__.__name__ == "QuotaExceeded":
                log.warning("scrapling: daily cap reached, skipping %s", url)
                base["error"] = f"scrapling daily cap: {exc}"
                return base
            log.debug("scrapling: quota check failed: %s", exc)

    log.debug("scrapling: fetching %s", url)
    t0 = time.monotonic()
    try:
        with _rl_acquire("scrapling"):
            # Lazy import so the module is import-cheap when
            # JEEVES_USE_SCRAPLING is unset (vast majority of CI runs).
            from scrapling.fetchers import StealthyFetcher  # type: ignore

            page = StealthyFetcher.fetch(
                url,
                headless=True,
                network_idle=True,
                timeout=timeout_seconds * 1000,
                disable_resources=True,
            )
    except Exception as exc:
        log.warning("scrapling: fetch error for %s: %s", url, exc)
        base["error"] = f"scrapling fetch error: {exc}"
        _emit(
            "tool_call",
            provider="scrapling",
            url=url,
            ok=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=str(exc)[:200],
        )
        return base

    # Charge ledger on every completed fetch — even soft-fail extractions
    # cost CI minutes.
    if ledger is not None:
        try:
            ledger.record("scrapling")
            ledger.record_daily("scrapling")
        except Exception as exc:
            log.debug("scrapling: ledger.record failed: %s", exc)

    # Pull text out of the Selector. Scrapling exposes several APIs;
    # try the cleanest first, fall back to css-selector text extraction.
    try:
        text = _extract_text(page)
        title = _extract_title(page)
    except Exception as exc:
        log.warning("scrapling: parse error for %s: %s", url, exc)
        base["error"] = f"scrapling parse error: {exc}"
        _emit(
            "tool_call",
            provider="scrapling",
            url=url,
            ok=False,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=f"parse: {str(exc)[:160]}",
        )
        return base

    if len(text) < _MIN_CONTENT_LENGTH:
        log.warning(
            "scrapling: returned %d chars (< %d) for %s",
            len(text),
            _MIN_CONTENT_LENGTH,
            url,
        )
        base["text"] = text
        base["error"] = "scrapling returned no content"
        _emit(
            "tool_call",
            provider="scrapling",
            url=url,
            ok=False,
            chars=len(text),
            latency_ms=int((time.monotonic() - t0) * 1000),
            error="below_min_content_length",
        )
        return base

    base.update(
        {
            "title": title,
            "text": text[:max_chars],
            "success": True,
            # 0.85: peer to firecrawl_extractor; above tinyfish's 0.80
            # because StealthyFetcher carries Cloudflare-solve which the
            # other extractors lack.
            "quality_score": 0.85,
        }
    )
    log.debug("scrapling: extracted %d chars from %s", len(text), url)
    _emit(
        "tool_call",
        provider="scrapling",
        url=url,
        ok=True,
        chars=len(text),
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
    return base


def _extract_text(page: Any) -> str:
    """Pull plain text out of a Scrapling Selector with defensive fallbacks.

    Scrapling's API has evolved; rather than pin a single accessor we try
    the cleanest first and fall back to CSS selectors. Returns "" if
    nothing works.
    """
    # 1. Modern Scrapling: .get_all_text(strip=True) returns clean prose.
    try:
        text = page.get_all_text(strip=True)
        if isinstance(text, str) and text:
            return text
    except (AttributeError, TypeError):
        pass

    # 2. Some versions expose .text directly on the body Selector.
    try:
        text = page.css("body").get_all_text(strip=True)
        if isinstance(text, str) and text:
            return text
    except (AttributeError, TypeError):
        pass

    # 3. Final fallback: join every text node under body.
    try:
        chunks = page.css("body *::text").getall() or []
        return " ".join(c.strip() for c in chunks if c and c.strip())
    except (AttributeError, TypeError):
        return ""


def _extract_title(page: Any) -> str:
    try:
        title = page.css("title::text").get()
        if isinstance(title, str):
            return title.strip()
    except (AttributeError, TypeError):
        pass
    return ""


def is_enabled() -> bool:
    """Cheap env-var check. Re-evaluated per call so callers don't have to
    cache or thread the flag through.

    Used by ``enrichment.fetch_article_text`` to decide whether to
    invoke the scrapling tier.
    """
    return os.environ.get("JEEVES_USE_SCRAPLING", "").strip() == "1"
