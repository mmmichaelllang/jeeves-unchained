"""ZenRows / Scrapfly managed-scraper extractors — DORMANT fetch-chain tiers.

Position when ``JEEVES_USE_ZENROWS=1`` / ``JEEVES_USE_SCRAPFLY=1``::

    httpx+trafilatura → Crawl4AI → Scrapling → ZENROWS → SCRAPFLY → playwright

Both are hosted anti-bot scraping APIs: you POST/GET a target URL and they
return the rendered HTML, having solved Cloudflare/PerimeterX/etc. with
residential-proxy rotation. We run their HTML through trafilatura (same as
the httpx tier) to get clean article text, so the public surface matches
``scrapling_extract.extract_article`` / ``playwright_extractor.extract_article``::

    {url, title, text, success, extracted_via, quality_score, error?}

DORMANT BY DEFAULT. 2026-06-16 analysis showed extraction is NOT jeeves's
failure mode (discovery + synthesis were) — crawl4ai/playwright clear the
real sources. These tiers exist as opt-in capability for a future measured
gap (a cluster of bot-walled sources playwright can't pass), mirroring the
TinyFish opt-in precedent. When the flags are unset, these functions are
near-zero-cost no-ops (one env read).

Quota: tracked under ``"zenrows"`` / ``"scrapfly"`` in the ledger.
Fail-soft: never raises; returns ``success=False`` + ``error`` on any failure.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .telemetry import emit as _emit

log = logging.getLogger(__name__)

_MIN_CONTENT_LENGTH = 300

_ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"
_SCRAPFLY_ENDPOINT = "https://api.scrapfly.io/scrape"

# Module-level client reused across calls (cheap when flags unset — never hit).
_HTTP_CLIENT = httpx.Client(timeout=60.0)


def zenrows_enabled() -> bool:
    """Cheap per-call env check — JEEVES_USE_ZENROWS=1 arms the tier."""
    return os.environ.get("JEEVES_USE_ZENROWS", "").strip() == "1"


def scrapfly_enabled() -> bool:
    """Cheap per-call env check — JEEVES_USE_SCRAPFLY=1 arms the tier."""
    return os.environ.get("JEEVES_USE_SCRAPFLY", "").strip() == "1"


def _trafilatura_text(html: str) -> str:
    if not html:
        return ""
    try:
        import trafilatura  # type: ignore

        return trafilatura.extract(
            html, include_comments=False, include_tables=False, favor_recall=True
        ) or ""
    except Exception as e:
        log.debug("managed_scraper: trafilatura failed: %s", e)
        return ""


def _trafilatura_title(html: str) -> str:
    if not html:
        return ""
    try:
        import trafilatura  # type: ignore

        md = trafilatura.metadata.extract_metadata(html)
        return (md.title if md else "") or ""
    except Exception:
        return ""


def _finish(base: dict, html: str, provider: str, t0: float, max_chars: int) -> dict:
    text = _trafilatura_text(html)
    if len(text) < _MIN_CONTENT_LENGTH:
        base["text"] = text
        base["error"] = f"{provider} returned no content ({len(text)} chars)"
        _emit("tool_call", provider=provider, url=base["url"], ok=False,
              chars=len(text), latency_ms=int((time.monotonic() - t0) * 1000),
              error="below_min_content_length")
        return base
    base.update({
        "title": _trafilatura_title(html),
        "text": text[:max_chars],
        "success": True,
        "quality_score": 0.85,
    })
    _emit("tool_call", provider=provider, url=base["url"], ok=True,
          chars=len(text), latency_ms=int((time.monotonic() - t0) * 1000))
    return base


def _extract(provider: str, url: str, max_chars: int, ledger: Any) -> dict:
    base: dict = {
        "url": url, "title": "", "text": "", "success": False,
        "extracted_via": provider, "quality_score": 0.0,
    }
    if not url:
        base["error"] = "empty url"
        return base

    if provider == "zenrows":
        api_key = os.environ.get("ZENROWS_API_KEY", "").strip()
        endpoint = _ZENROWS_ENDPOINT
        params: dict[str, Any] = {"apikey": api_key, "url": url, "js_render": "true"}
    else:  # scrapfly
        api_key = os.environ.get("SCRAPFLY_API_KEY", "").strip()
        endpoint = _SCRAPFLY_ENDPOINT
        params = {"key": api_key, "url": url, "asp": "true", "render_js": "true"}

    if not api_key:
        base["error"] = f"{provider.upper()}_API_KEY not set"
        return base

    t0 = time.monotonic()
    try:
        r = _HTTP_CLIENT.get(endpoint, params=params)
        r.raise_for_status()
        if provider == "scrapfly":
            # Scrapfly wraps the page in JSON: {result: {content: "<html>"}}.
            data = r.json()
            html = (data.get("result") or {}).get("content", "") or ""
        else:
            # ZenRows returns the rendered HTML directly as the body.
            html = r.text or ""
    except Exception as exc:
        log.warning("%s: fetch error for %s: %s", provider, url, exc)
        base["error"] = f"{provider} fetch error: {exc}"
        _emit("tool_call", provider=provider, url=url, ok=False,
              latency_ms=int((time.monotonic() - t0) * 1000), error=str(exc)[:200])
        return base

    if ledger is not None:
        try:
            ledger.record(provider)
            ledger.record_daily(provider)
        except Exception as exc:
            log.debug("%s: ledger.record failed: %s", provider, exc)

    return _finish(base, html, provider, t0, max_chars)


def extract_article_zenrows(url: str, *, timeout_seconds: int = 60,
                            max_chars: int = 3000, ledger: Any = None) -> dict:
    """ZenRows-backed extraction. Fail-soft. Shape matches scrapling/playwright."""
    return _extract("zenrows", url, max_chars, ledger)


def extract_article_scrapfly(url: str, *, timeout_seconds: int = 60,
                             max_chars: int = 3000, ledger: Any = None) -> dict:
    """Scrapfly-backed extraction. Fail-soft. Shape matches scrapling/playwright."""
    return _extract("scrapfly", url, max_chars, ledger)
