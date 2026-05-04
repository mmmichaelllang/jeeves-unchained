"""Firecrawl API article extractor — optional fetch-chain step.

Position in enrichment.py / talk_of_the_town.py fetch chain:
  httpx+trafilatura → Jina(r.jina.ai) → **Firecrawl** → playwright_extractor

Insert between Jina and Playwright when FIRECRAWL_API_KEY is set:

    # In enrichment.py fetch_article_text(), after the Jina/trafilatura block:
    # if len(text) < 300:
    #     from .firecrawl_extractor import extract_article as _fc_extract
    #     fc_result = _fc_extract(url, timeout_seconds=30, max_chars=3000)
    #     if fc_result.get("success"):
    #         base.update({...})
    #         return json.dumps(base)
    # # Then fall through to playwright_extractor as before.

Public surface:
  - ``extract_article(url, *, timeout_seconds, max_chars, ledger) -> dict``
    Fail-soft: never raises. Returns success=False with error key on any failure.

Return shape (matches playwright_extractor.extract_article):
    {
        "url": str,
        "title": str,
        "text": str,          # markdown content, truncated to max_chars
        "success": bool,
        "extracted_via": "firecrawl",
        "error": str,         # only present when success=False
    }

Feature flag: FIRECRAWL_API_KEY env var. Absent → immediate success=False.
Quota: tracked under "firecrawl" key in ledger if provided.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"
_MIN_CONTENT_LENGTH = 300


def extract_article(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12_000,
    ledger: Any = None,
) -> dict:
    """Fetch article content via the Firecrawl API.

    Parameters
    ----------
    url:
        Target URL to scrape.
    timeout_seconds:
        HTTP request timeout passed to httpx (default 30).
    max_chars:
        Truncate returned markdown to this many characters (default 12 000).
    ledger:
        Optional quota ledger object. If provided, ``ledger.record("firecrawl")``
        is called once per successful API call (same pattern as other tools).

    Returns
    -------
    dict with keys: url, title, text, success, extracted_via, quality_score,
    and (on failure) error. quality_score matches playwright_extractor's
    contract — 0.0 on failure, 0.85 default on success.
    """
    base: dict = {
        "url": url,
        "title": "",
        "text": "",
        "success": False,
        "extracted_via": "firecrawl",
        "quality_score": 0.0,
    }

    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        log.debug("firecrawl: FIRECRAWL_API_KEY not set, skipping %s", url)
        base["error"] = "FIRECRAWL_API_KEY not set"
        return base

    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": timeout_seconds * 1000,  # API expects milliseconds
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    log.debug("firecrawl: requesting %s", url)
    try:
        response = httpx.post(
            _FIRECRAWL_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=float(timeout_seconds),
        )
    except httpx.TimeoutException as exc:
        log.warning("firecrawl: timeout fetching %s: %s", url, exc)
        base["error"] = f"firecrawl timeout: {exc}"
        return base
    except Exception as exc:
        log.warning("firecrawl: request error for %s: %s", url, exc)
        base["error"] = f"firecrawl request error: {exc}"
        return base

    if response.status_code != 200:
        log.warning("firecrawl: HTTP %s for %s", response.status_code, url)
        base["error"] = f"firecrawl api error: {response.status_code}"
        return base

    # Record on the quota ledger on a successful HTTP call (regardless of content).
    # QuotaLedger exposes record(), not increment() — common typo trap that
    # would silently swallow the AttributeError under the broad except below.
    if ledger is not None:
        try:
            ledger.record("firecrawl")
        except Exception as exc:
            log.debug("firecrawl: ledger.record failed: %s", exc)

    try:
        data = response.json()
    except Exception as exc:
        log.warning("firecrawl: JSON decode error for %s: %s", url, exc)
        base["error"] = f"firecrawl json decode error: {exc}"
        return base

    if not data.get("success"):
        api_error = data.get("error", "unknown error")
        log.warning("firecrawl: API returned success=false for %s: %s", url, api_error)
        base["error"] = f"firecrawl api error: {api_error}"
        return base

    inner = data.get("data", {})
    markdown = inner.get("markdown", "")
    title = inner.get("title", "") or inner.get("metadata", {}).get("title", "")

    if not markdown or len(markdown) < _MIN_CONTENT_LENGTH:
        log.warning(
            "firecrawl: returned %d chars (< %d) for %s",
            len(markdown),
            _MIN_CONTENT_LENGTH,
            url,
        )
        base["error"] = "firecrawl returned no content"
        return base

    base.update(
        {
            "title": title,
            "text": markdown[:max_chars],
            "success": True,
            "quality_score": 0.85,  # default OK score; firecrawl already filters main content
        }
    )
    log.debug("firecrawl: extracted %d chars from %s", len(markdown), url)
    return base
