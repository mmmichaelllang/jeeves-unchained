"""New Yorker 'Talk of the Town' fetcher. Stdlib-only; ported from jeeves-memory."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from html import unescape
from typing import Any

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

TOC_URL = "https://www.newyorker.com/magazine/talk-of-the-town"
ARTICLE_PATH_RE = re.compile(r"/magazine/(\d{4})/(\d{2})/(\d{2})/([a-z0-9-]+)")
LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _extract_paths(toc_html: str) -> list[tuple[int, str]]:
    seen: dict[str, int] = {}
    for m in ARTICLE_PATH_RE.finditer(toc_html):
        y, mo, d, slug = m.groups()
        url = f"https://www.newyorker.com/magazine/{y}/{mo}/{d}/{slug}"
        if url not in seen:
            seen[url] = int(y + mo + d)
    return sorted(((dk, u) for u, dk in seen.items()), key=lambda kv: kv[0], reverse=True)


def _pick_novel(paths: list[tuple[int, str]], covered: set[str]) -> str | None:
    covered_norm = {c.rstrip("/") for c in covered}
    for _, url in paths:
        if url.rstrip("/") not in covered_norm:
            return url
    return None


def _load_ld(html: str) -> dict | None:
    for m in LD_JSON_RE.finditer(html):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") in (
                "NewsArticle",
                "Article",
                "ReportageNewsArticle",
            ):
                return node
    return None


def _fallback_paragraphs(html: str) -> str:
    body = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
    chunk = body.group(1) if body else html
    paras = re.findall(r"<p[^>]*>(.*?)</p>", chunk, re.DOTALL)
    out = []
    for p in paras:
        txt = unescape(re.sub(r"<[^>]+>", "", p)).strip()
        if len(txt) > 40:
            out.append(txt)
    return "\n\n".join(out)


def fetch_talk_of_the_town(covered_urls: set[str]):
    """Closure capturing the covered-URL set so the tool takes no args."""

    def _run() -> str:
        """Fetch the latest 'Talk of the Town' article not already covered.

        Returns a JSON-encoded string so LlamaIndex's _parse_tool_output()
        produces TextBlock(text=<valid-JSON>) rather than TextBlock(text=str(dict))
        which yields Python repr with single quotes that NIM cannot parse.
        """
        import json as _json

        base: dict[str, Any] = {
            "available": False,
            "title": "",
            "section": "",
            "dek": "",
            "text": "",
            "url": "",
            "source": "The New Yorker",
            "error": None,
        }
        try:
            toc_html = _http_get(TOC_URL)
        except Exception as e:
            base["error"] = f"toc_fetch_failed: {e}"
            return _json.dumps(base)

        paths = _extract_paths(toc_html)
        if not paths:
            base["error"] = "toc_no_paths_found"
            return _json.dumps(base)

        url = _pick_novel(paths, covered_urls)
        if not url:
            base["error"] = "all_articles_already_covered"
            return _json.dumps(base)
        base["url"] = url

        try:
            html = _http_get(url)
        except Exception as e:
            base["error"] = f"article_fetch_failed: {e}"
            return _json.dumps(base)

        article = _load_ld(html)
        if article:
            base["title"] = article.get("headline", "") or ""
            base["section"] = article.get("articleSection", "") or ""
            base["dek"] = article.get("alternativeHeadline", "") or ""
            body = article.get("articleBody", "") or ""
            if body and len(body) > 500:
                base["text"] = body
                base["available"] = True
                return _json.dumps(base)

        text = _fallback_paragraphs(html)
        if len(text) > 500:
            base["text"] = text
            base["available"] = True
            if not base["title"]:
                m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
                if m:
                    base["title"] = unescape(re.sub(r"\s+", " ", m.group(1))).strip()
            return _json.dumps(base)

        base["error"] = f"article_text_too_short ({len(text)} chars)"
        return _json.dumps(base)

    return _run
