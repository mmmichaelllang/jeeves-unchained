"""Last-resort full-text fetcher using trafilatura."""

from __future__ import annotations

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


def fetch_article_text(url: str) -> str:
    """Fetch a URL and extract clean article text via trafilatura.

    Returns a JSON string so LlamaIndex's _parse_tool_output() produces
    TextBlock(text=<valid-JSON>) rather than TextBlock(text=str(dict))
    which yields Python repr with single quotes that NIM cannot parse.

    JSON shape: {url, title, text, fetch_failed, source}

    Fallback chain:
      1. httpx + trafilatura (this function's primary path)
      2. headless Playwright + OpenRouter crystallizer (when both 1 yields
         <300 chars text AND playwright is installed). Soft-fails to the
         empty primary result if Playwright is unavailable.
    """
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

    if len(text) >= 300:
        title = _extract_title(html)
        base.update({"title": title, "text": text[:3000], "fetch_failed": False})
        return json.dumps(base)

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


def _extract_title(html: str) -> str:
    import re

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()
