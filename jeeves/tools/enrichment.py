"""Last-resort full-text fetcher using trafilatura."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def fetch_article_text(url: str) -> dict[str, Any]:
    """Fetch a URL and extract clean article text via trafilatura.

    Returns: {url, title, text, fetch_failed, source}
    """
    base = {
        "url": url,
        "title": "",
        "text": "",
        "fetch_failed": True,
        "source": _host(url),
    }
    if not url:
        return base
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=25.0, follow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        log.info("fetch failed %s: %s", url, e)
        base["text"] = f"fetch_error: {e}"
        return base

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

    if len(text) < 300:
        return base

    title = _extract_title(html)
    base.update({"title": title, "text": text, "fetch_failed": False})
    return base


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
