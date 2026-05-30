"""Last-resort full-text fetcher using trafilatura."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Module-level client reuses connections across the many article fetches in the
# enriched_articles sector instead of creating a new TCP handshake per URL.
_HTTP_CLIENT = httpx.Client(
    headers={"User-Agent": UA},
    timeout=25.0,
    follow_redirects=True,
)
atexit.register(_HTTP_CLIENT.close)


# Module-level executor reused across all Crawl4AI fetches.
# max_workers=1 because:
#   (a) Crawl4AI's internal browser context may not be reentrant
#   (b) jeeves runs sectors sequentially (_SECTOR_SEMAPHORE=1) so no concurrency benefit
#   (c) one persistent thread avoids per-call thread spawn cost
# Lazy-initialized on first use to avoid spinning up a thread when crawl4ai
# is never called (e.g. JEEVES_USE_CRAWL4AI_FETCH unset, or kill switch on).
_CRAWL4AI_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _get_crawl4ai_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _CRAWL4AI_EXECUTOR
    if _CRAWL4AI_EXECUTOR is None:
        _CRAWL4AI_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="crawl4ai"
        )
        atexit.register(_CRAWL4AI_EXECUTOR.shutdown, wait=False)
    return _CRAWL4AI_EXECUTOR


def _run_crawl4ai_sync(url: str, max_chars: int = 3000) -> tuple[str, str]:
    """Sync wrapper for crawl4ai_extract that survives nested-loop contexts.

    M3 (commit bb5520d, 2026-05-21) originally used bare ``asyncio.run()`` here.
    That works in production (sync code path, no running loop) but crashes
    with ``RuntimeError: Cannot run the event loop while another loop is
    running`` whenever fetch_article_text is invoked from inside an active
    event loop — which pytest-asyncio (``asyncio_mode = "auto"``) creates for
    every async test in the suite. The crash propagated across test files
    and manifested as 3 test_research_sectors.py regressions in the iter 6
    bisect.

    This wrapper detects an active loop via ``asyncio.get_running_loop()``
    and dispatches the async call to a dedicated module-level thread where a
    fresh ``asyncio.run()`` is safe. When no loop is active (production
    path), it uses ``asyncio.run()`` directly with no thread hop.

    30s per-call timeout matches the existing _HTTP_CLIENT 25s + a small
    buffer for Crawl4AI's own internal startup.
    """
    from .crawl4ai_extract import crawl4ai_extract as _crawl4ai_extract

    async def _coro() -> tuple[str, str]:
        return await _crawl4ai_extract(url, max_chars=max_chars)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — production path, use asyncio.run directly
        return asyncio.run(_coro())

    # Running inside an event loop — dispatch to thread where asyncio.run is safe.
    # Only fires under pytest-asyncio (or any other host-loop scenario).
    future = _get_crawl4ai_executor().submit(lambda: asyncio.run(_coro()))
    return future.result(timeout=30)


# ---------------------------------------------------------------------------
# Per-run seen-URL cache (Flaw 10).
#
# Same article surfaces in multiple sectors during a single research run
# (e.g. a ProPublica feature lands in global_news, intellectual_journals,
# AND enriched_articles). Previously each sector fetched + extracted the
# article independently — 3× the playwright cost, 3× the API budget, 3×
# the rate-limit risk. Now the first sector to fetch caches by
# canonical_url; subsequent sectors get the cached JSON without a network
# call. Per-RUN, not per-day: reset via ``reset_seen_url_cache()`` from
# research.main() before the first sector runs. Module-level (not threaded
# through ResearchContext) because fetch_article_text is invoked deep in
# the FunctionAgent tool dispatch tree where threading new args through
# every call site is invasive.
# ---------------------------------------------------------------------------

_SEEN_URL_CACHE: dict[str, str] = {}
_SEEN_URL_CACHE_LOCK = __import__("threading").Lock()


def reset_seen_url_cache() -> None:
    """Drop the per-run cache. Call once at the start of a research run."""
    with _SEEN_URL_CACHE_LOCK:
        n = len(_SEEN_URL_CACHE)
        _SEEN_URL_CACHE.clear()
    if n:
        log.info("seen_url_cache reset (dropped %d entries)", n)


def seen_url_cache_stats() -> dict[str, int]:
    """Return current cache size — used by daily-run telemetry."""
    with _SEEN_URL_CACHE_LOCK:
        return {"size": len(_SEEN_URL_CACHE)}


def _canonical_cache_key(url: str) -> str:
    """Canonical URL key used by the seen-URL cache.

    Imports `canonical_url` lazily to avoid a circular import at module
    load (jeeves.dedup imports from jeeves.schema which is imported widely).
    """
    if not url:
        return ""
    try:
        from jeeves.dedup import canonical_url as _canon
        return _canon(url)
    except Exception:
        return url


def fetch_article_text(url: str) -> str:
    """Fetch a URL and extract clean article text via trafilatura.

    Returns a JSON string so LlamaIndex's _parse_tool_output() produces
    TextBlock(text=<valid-JSON>) rather than TextBlock(text=str(dict))
    which yields Python repr with single quotes that NIM cannot parse.

    JSON shape: {url, title, text, fetch_failed, source}

    Fallback chain:
      1. **Per-run seen-URL cache check** (Flaw 10) — if a prior sector in
         this run already fetched the canonical URL, return its cached
         JSON unchanged. Saves duplicate playwright/trafilatura/network
         spend on cross-sector duplicates.
      2. httpx + trafilatura (primary path)
      3. headless Playwright + OpenRouter crystallizer (when 2 yields
         <300 chars text AND playwright is installed). Soft-fails to the
         empty primary result if Playwright is unavailable.
    """
    # Cache check — earliest possible return.
    cache_key = _canonical_cache_key(url)
    if cache_key:
        with _SEEN_URL_CACHE_LOCK:
            cached = _SEEN_URL_CACHE.get(cache_key)
        if cached is not None:
            log.info("seen_url_cache HIT for %s", cache_key)
            return cached

    result = _fetch_article_text_impl(url)

    # Cache MISS path — store the result only when the fetch succeeded
    # (fetch_failed == False). Storing failure cases would pollute the
    # cache with errors for the rest of the run, blocking retries via a
    # different sector's extractor chain.
    if cache_key:
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict) and not parsed.get("fetch_failed", True):
                with _SEEN_URL_CACHE_LOCK:
                    _SEEN_URL_CACHE[cache_key] = result
                log.debug("seen_url_cache STORE for %s", cache_key)
        except Exception:
            pass
    return result


def _fetch_article_text_impl(url: str) -> str:
    """The original fetch implementation. Kept as a separate function so the
    public ``fetch_article_text`` can wrap it with the per-run seen-URL
    cache without entangling the cache logic with the trafilatura/playwright
    chain control flow."""
    base = {
        "url": url,
        "title": "",
        "text": "",
        "fetch_failed": True,
        "source": _host(url),
    }
    if not url:
        return json.dumps(base)

    html = ""
    primary_error = ""
    try:
        r = _HTTP_CLIENT.get(url)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        primary_error = str(e)
        log.info("fetch failed %s: %s", url, e)
        # html stays empty — trafilatura step skipped, go straight to playwright.

    text = ""
    if html:
        try:
            import trafilatura  # type: ignore

            text = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                favor_recall=True,
            ) or ""
        except Exception as e:
            log.info("trafilatura failed %s: %s", url, e)
            text = ""

    # Cascade-aggressiveness fix (2026-05-21): the 300-char threshold was
    # too generous — trafilatura would return 350 chars of cookie banner +
    # nav + footer and the function returned fetch_failed=False, denying
    # the next tier (playwright/tinyfish) any chance to fetch the real
    # article. Two extra checks:
    #   1. Raise the byte threshold from 300 to 600 — typical paywall
    #      stub pages return 300-500 chars of "subscribe to read more"
    #      boilerplate that passes the old gate.
    #   2. Reject extractions whose alphabetic-content ratio is too low
    #      (signals a list of menu items or button labels rather than
    #      prose). Caps at 0.55 — real news articles average 0.75+.
    if len(text) >= 600 and _looks_like_prose(text):
        title = _extract_title(html)
        base.update({"title": title, "text": text[:3000], "fetch_failed": False})
        return json.dumps(base)

    # Crawl4AI TIER 2 — for news_short hosts only. Soft-fails so cascade continues.
    try:
        from .crawl4ai_extract import classify_host as _classify_host

        if _classify_host(url) == "news_short":
            try:
                c4ai_text, _mode = _run_crawl4ai_sync(url, max_chars=3000)
                if c4ai_text and len(c4ai_text) >= 300:
                    base.update({
                        "title": _extract_title(html) if html else "",
                        "text": c4ai_text[:3000],
                        "fetch_failed": False,
                        "extracted_via": "crawl4ai",
                    })
                    return json.dumps(base)
            except Exception as e:
                log.debug("crawl4ai fetch failed for %s: %s", url, e)
    except Exception as e:
        log.debug("crawl4ai import/classify failed: %s", e)

    # Scrapling TIER 2.5 — stealth extractor with Cloudflare-solve, gated
    # by JEEVES_USE_SCRAPLING=1. Sits between Crawl4AI (news_short fast
    # path) and Playwright (last-resort raw Patchright) because it
    # specifically beats soft-paywall / Cloudflare-gated hosts where the
    # raw Playwright fallback today silently lands on a challenge page.
    # When the flag is unset this block is a near-zero-cost no-op (one
    # env read, one function call returning False).
    try:
        from .scrapling_extract import (
            extract_article as _sc_extract,
            is_enabled as _sc_enabled,
        )

        if _sc_enabled():
            sc_result = _sc_extract(url, timeout_seconds=30, max_chars=3000)
            if sc_result.get("success"):
                base.update({
                    "title": sc_result.get("title", ""),
                    "text": sc_result.get("text", "")[:3000],
                    "fetch_failed": False,
                    "extracted_via": "scrapling",
                })
                return json.dumps(base)
    except Exception as e:
        log.debug("scrapling tier failed for %s: %s", url, e)

    # Playwright fallback — last resort when httpx returned nothing OR
    # trafilatura couldn't extract enough body text.
    try:
        from .playwright_extractor import extract_article as _pw_extract

        pw_result = _pw_extract(url, timeout_seconds=30, max_chars=3000)
        if pw_result.get("success"):
            base.update({
                "title": pw_result.get("title", ""),
                "text": pw_result.get("text", "")[:3000],
                "fetch_failed": False,
                "extracted_via": "playwright",
            })
            return json.dumps(base)
    except Exception as e:
        log.debug("playwright fallback failed for %s: %s", url, e)

    if primary_error:
        base["text"] = f"fetch_error: {primary_error}"
    return json.dumps(base)


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


# Common nav/cookie-banner phrases that indicate trafilatura captured
# chrome instead of article text. Hits → treat as failed extraction so
# the next tier (playwright/tinyfish) gets a turn.
_BOILERPLATE_PATTERNS = (
    "subscribe to continue",
    "create a free account",
    "we use cookies",
    "this site uses cookies",
    "accept all cookies",
    "you have reached your limit",
    "for more, sign up",
    "log in to continue",
    "javascript is disabled",
    "please enable javascript",
    "you have been blocked",
    "access denied",
    "checking your browser",
)


def _looks_like_prose(text: str) -> bool:
    """Heuristic: does this extracted text look like article prose vs.
    navigation/cookie-banner chrome?

    Returns False (caller treats as fetch_failed) if:
      - the text contains any known boilerplate phrase
      - alphabetic-character ratio is below 0.55 (signals
        list-of-menu-items rather than sentences)
      - sentence terminator density is below 1 per 200 chars

    Returns True (caller treats as success) otherwise.

    Designed to be conservative: false negatives (real prose flagged as
    failed) just trigger the next tier, which is cheap. False positives
    (boilerplate flagged as prose) are what we are TRYING to eliminate.
    """
    if not text:
        return False
    low = text.lower()
    for pat in _BOILERPLATE_PATTERNS:
        if pat in low:
            return False
    alpha = sum(1 for c in text if c.isalpha())
    if len(text) > 0 and alpha / len(text) < 0.55:
        return False
    terminators = text.count(".") + text.count("!") + text.count("?")
    if terminators == 0 or len(text) / terminators > 200:
        return False
    return True


def _extract_title(html: str) -> str:
    import re

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()
