"""New Yorker 'Talk of the Town' fetcher. Ported from jeeves-memory."""

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
JINA_BASE = "https://r.jina.ai/"

# Content stop markers: everything after these lines belongs to the footer.
_CONTENT_STOP_MARKERS = (
    "Published in the print edition",
    "New Yorker Favorites",
    "Sign up for our daily newsletter",
    "Get our Classics newsletter",
    "The New Yorker Classics Newsletter",
    "© 20",  # © 20xx copyright line
)


def _http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _jina_fetch(url: str, timeout: int = 30) -> str:
    """Fetch article via Jina AI reader (free tier) — returns clean markdown."""
    req = urllib.request.Request(
        JINA_BASE + url,
        headers={
            "User-Agent": UA,
            "Accept": "text/plain, text/markdown, */*",
            "X-Return-Format": "markdown",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _clean_jina_text(text: str) -> str:
    """Strip Jina metadata, nav boilerplate, credits, and markdown noise."""
    # Strip Jina header lines (Title:, URL:, Published Time:)
    text = re.sub(r"^(Title|URL|Published Time|Source URL):.*\n", "", text, flags=re.MULTILINE)

    # Hard stop at footer markers
    for marker in _CONTENT_STOP_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]

    # Remove photo / illustration / cartoon credits
    text = re.sub(
        r"(?:Photograph|Illustration|Cartoon|Image)s?\s+by\s+[^\n]+\n?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Remove newsletter signup boilerplate
    text = re.sub(
        r"By signing up,[\s\S]*?privacy policy\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Remove inline images: ![alt](url) → ''
    text = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", text)

    # Unlink remaining markdown links: [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)

    # Strip bold/italic markers
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)

    # Strip markdown headers and horizontal rules
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-=]{3,}$", "", text, flags=re.MULTILINE)

    # Normalise whitespace
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


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


def _extract_byline(author: Any) -> str:
    """Normalise the ld+json author field to a 'By …' string."""
    if isinstance(author, list):
        names = [a.get("name", "") for a in author if isinstance(a, dict)]
        name_str = ", ".join(n for n in names if n)
    elif isinstance(author, dict):
        name_str = author.get("name", "")
    elif isinstance(author, str):
        name_str = author
    else:
        return ""
    return f"By {name_str}" if name_str else ""


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

        Fetch priority:
          1. ld+json articleBody (structured data, cleanest)
          2. Jina AI reader (r.jina.ai — clean markdown, no HTML noise)
          3. Raw HTML <p> extraction (last resort)
        """
        import json as _json

        base: dict[str, Any] = {
            "available": False,
            "title": "",
            "section": "",
            "dek": "",
            "byline": "",
            "date": "",
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
            base["byline"] = _extract_byline(article.get("author"))
            base["date"] = article.get("datePublished", "") or ""
            body = article.get("articleBody", "") or ""
            if body and len(body) > 500:
                base["text"] = body
                base["available"] = True
                return _json.dumps(base)

        # ld+json body absent or too short — try Jina for clean markdown.
        try:
            raw_jina = _jina_fetch(url)
            jina_text = _clean_jina_text(raw_jina)
            if len(jina_text) > 500:
                base["text"] = jina_text
                base["available"] = True
                if not base["title"]:
                    m = re.search(r"^Title:\s*(.+)$", raw_jina, re.MULTILINE)
                    if m:
                        base["title"] = m.group(1).strip()
                return _json.dumps(base)
        except Exception as e:
            log.debug("jina fetch failed for %s: %s", url, e)

        # Last resort: raw HTML paragraph extraction.
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
