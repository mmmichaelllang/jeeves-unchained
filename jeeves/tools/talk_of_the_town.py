"""New Yorker 'Talk of the Town' fetcher. Ported from jeeves-memory.

Article-discovery priority (newest → oldest uncovered):
  1. RSS feed  (stdlib XML, JS-free, authoritative)
  2. Raw HTML TOC scrape
  3. Jina AI reader TOC

Per-article text-fetch priority (tried for each novel URL, newest first):
  A. Direct HTTP → ld+json articleBody
  B. Direct HTTP → Jina AI reader
  C. Direct HTTP → raw <p> extraction
  D. Wayback Machine (CDX API, up to _MAX_WAYBACK_SNAPSHOTS newest status:200 captures)
     → ld+json → raw <p> (Wayback captures SSR HTML with full articleBody even for
       soft-paywalled pages because the paywall is client-side JS only)
  E. archive.ph/newest/ lookup → ld+json → raw <p>

  On failure for a URL the loop continues to the next novel URL (newest first).
  Up to _MAX_NOVEL_URLS articles are tried before returning available:false.

  Dedup: base["url"] is always the canonical newyorker.com URL so it feeds
  correctly into the session dedup ledger. base["archived_from"] records
  provenance ("" | "wayback" | "archiveph") for logging/diagnostics.

  Unpaywall (unpaywall.org) was researched and ruled out: it only covers
  academic papers with DOIs. New Yorker articles have neither.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from typing import Any
from urllib.parse import quote, urlparse

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
RSS_URL = "https://www.newyorker.com/feed/tags/department/the-talk-of-the-town"
ARTICLE_PATH_RE = re.compile(r"/magazine/(\d{4})/(\d{2})/(\d{2})/([a-z0-9-]+)")
LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL,
)
# The New Yorker omits the drop-cap lead paragraph from ld+json articleBody.
# It lives in a <p class="... has-dropcap ..."> element in the raw HTML.
DROPCAP_RE = re.compile(
    r'<p[^>]*class="[^"]*has-dropcap[^"]*"[^>]*>(.*?)</p>',
    re.DOTALL | re.IGNORECASE,
)
JINA_BASE = "https://r.jina.ai/"

_WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_WAYBACK_BASE = "https://web.archive.org/web"
# All known mirror domains for archive.ph / archive.today.
_ARCHIVEPH_DOMAINS = frozenset(
    {"archive.ph", "archive.today", "archive.is", "archive.fo", "archive.li", "archive.vn"}
)

# Max novel articles to attempt before giving up.  Prevents runaway latency when
# all recent articles are behind a hard paywall or lack Wayback snapshots.
_MAX_NOVEL_URLS = 5
# Max Wayback snapshots to try per article (newest first).
_MAX_WAYBACK_SNAPSHOTS = 3

# Content stop markers: everything after these lines belongs to the footer.
_CONTENT_STOP_MARKERS = (
    "Published in the print edition",
    "New Yorker Favorites",
    "Sign up for our daily newsletter",
    "Get our Classics newsletter",
    "The New Yorker Classics Newsletter",
    "© 20",  # © 20xx copyright line
)


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _jina_fetch(url: str, timeout: int = 30, api_key: str = "") -> str:
    """Fetch article via Jina AI reader — returns clean markdown.

    Passes Authorization header when api_key is provided (removes rate limit).
    Falls back gracefully to unauthenticated free tier when key is absent.
    """
    headers: dict[str, str] = {
        "User-Agent": UA,
        "Accept": "text/plain, text/markdown, */*",
        "X-Return-Format": "markdown",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(JINA_BASE + url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def _discover_from_rss(timeout: int = 15) -> list[tuple[int, str]]:
    """Discover TOTT article URLs from the RSS feed (stdlib, JS-free).

    Returns list of (date_int, url) sorted newest-first, same shape as
    _extract_paths(). Falls back to empty list on any failure so callers
    can chain to the HTML/Jina TOC path.
    """
    try:
        req = urllib.request.Request(
            RSS_URL,
            headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        root = ET.fromstring(raw)
    except Exception as e:
        log.debug("RSS fetch failed: %s", e)
        return []

    results: dict[str, int] = {}
    for item in root.iter("item"):
        link_el = item.find("link")
        if link_el is None or not link_el.text:
            continue
        url = link_el.text.strip()
        m = ARTICLE_PATH_RE.search(url)
        if not m:
            continue
        y, mo, d, _ = m.groups()
        norm_url = f"https://www.newyorker.com{m.group(0)}"
        if norm_url not in results:
            results[norm_url] = int(y + mo + d)

    return sorted(((dk, u) for u, dk in results.items()), key=lambda kv: kv[0], reverse=True)


def _extract_paths(toc_html: str) -> list[tuple[int, str]]:
    seen: dict[str, int] = {}
    for m in ARTICLE_PATH_RE.finditer(toc_html):
        y, mo, d, slug = m.groups()
        url = f"https://www.newyorker.com/magazine/{y}/{mo}/{d}/{slug}"
        if url not in seen:
            seen[url] = int(y + mo + d)
    return sorted(((dk, u) for u, dk in seen.items()), key=lambda kv: kv[0], reverse=True)


def _novel_urls_sorted(paths: list[tuple[int, str]], covered: set[str]) -> list[str]:
    """Return all uncovered article URLs, preserving newest-first order from paths.

    Replaces the old _pick_novel() single-result function so the main loop can
    work backwards through time when earlier articles fail all fetch strategies.
    """
    covered_norm = {c.rstrip("/") for c in covered}
    return [url for _, url in paths if url.rstrip("/") not in covered_norm]


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _clean_jina_text(text: str) -> str:
    """Strip Jina metadata, nav boilerplate, credits, and markdown noise."""
    text = re.sub(r"^(Title|URL|Published Time|Source URL):.*\n", "", text, flags=re.MULTILINE)

    for marker in _CONTENT_STOP_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]

    text = re.sub(
        r"(?:Photograph|Illustration|Cartoon|Image)s?\s+by\s+[^\n]+\n?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"By signing up,[\s\S]*?privacy policy\.?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-=]{3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


def _extract_dropcap(html: str) -> str:
    """Return the drop-cap lead paragraph text, or '' if not found.

    The New Yorker excludes the drop-cap paragraph from ld+json articleBody.
    It is present in the raw HTML as <p class="... has-dropcap ...">...</p>.
    """
    m = DROPCAP_RE.search(html)
    if not m:
        return ""
    return unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _prepend_dropcap(html: str, body: str) -> str:
    """Prepend the drop-cap paragraph to body if it isn't already there."""
    lead = _extract_dropcap(html)
    if not lead:
        return body
    # Avoid duplication: skip if first 80 chars of lead already appear in body.
    if lead[:80] in body:
        return body
    return lead + "\n" + body


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


def _fill_meta_from_ld(html: str, base: dict) -> None:
    """Populate title/section/dek/byline/date from ld+json into base (in-place).

    Only fills fields that are currently empty so earlier successful fetches
    are not overwritten by later fallback attempts.
    """
    article = _load_ld(html)
    if not article:
        return
    if not base.get("title"):
        base["title"] = article.get("headline", "") or ""
    if not base.get("section"):
        base["section"] = article.get("articleSection", "") or ""
    if not base.get("dek"):
        base["dek"] = article.get("alternativeHeadline", "") or ""
    if not base.get("byline"):
        base["byline"] = _extract_byline(article.get("author"))
    if not base.get("date"):
        base["date"] = article.get("datePublished", "") or ""


# ---------------------------------------------------------------------------
# Archive lookup helpers
# ---------------------------------------------------------------------------

def _wayback_snapshot_urls(article_url: str, n: int = _MAX_WAYBACK_SNAPSHOTS, timeout: int = 10) -> list[str]:
    """Return up to n most-recent Wayback status:200 snapshot URLs for article_url.

    Uses the CDX API (no key required). Returns an empty list on any error so
    callers can chain gracefully.

    The `if_` modifier on the returned URLs instructs Wayback to serve the
    original archived HTML without injecting the Wayback toolbar banner,
    which gives cleaner HTML for ld+json and paragraph extraction.

    Why New Yorker SSR snapshots have full text: the New Yorker uses a
    client-side JS paywall. The server renders the full article HTML
    (including ld+json with articleBody) for SEO/crawlers. Wayback captures
    this SSR output, so snapshots typically contain the full article text
    regardless of the live paywall status.
    """
    cdx = (
        f"{_WAYBACK_CDX_URL}"
        f"?url={quote(article_url, safe='')}"
        f"&output=json&fl=timestamp&filter=statuscode:200"
        f"&limit=-{n}&matchType=exact"
    )
    try:
        req = urllib.request.Request(cdx, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.debug("CDX query failed for %s: %s", article_url, e)
        return []

    # data[0] is always the header row ['timestamp']; data[1..] are capture rows.
    if len(data) < 2:
        return []

    # CDX returns rows oldest-first; limit=-n gives the last n (most recent).
    # Reverse so we try newest first, consistent with the "most recent first"
    # requirement.
    snapshots = []
    for row in reversed(data[1:]):
        ts = row[0]
        snapshots.append(f"{_WAYBACK_BASE}/{ts}if_/{article_url}")
    return snapshots


def _archiveph_snapshot_url(article_url: str, timeout: int = 15) -> str | None:
    """Return the archive.ph snapshot URL for article_url, or None if not found.

    Queries `archive.ph/newest/{url}`, which redirects (HTTP 302) to
    `archive.ph/{hash}` when a snapshot exists.  urllib follows the redirect
    automatically; we check the final URL's path to confirm it's a snapshot
    (short alphanumeric hash) rather than the submission/search form page.

    Mirror domains (archive.today, archive.is, etc.) are all accepted.
    """
    lookup = f"https://archive.ph/newest/{article_url}"
    req = urllib.request.Request(
        lookup,
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final_url = r.url
    except Exception as e:
        log.debug("archive.ph lookup failed for %s: %s", article_url, e)
        return None

    # Snapshot redirect lands on archive.ph/{hash} where hash is a short
    # alphanumeric string (typically 5-7 chars).  Form / search pages have
    # longer or slash-separated paths.
    parsed = urlparse(final_url)
    if parsed.netloc not in _ARCHIVEPH_DOMAINS:
        return None
    path_segment = parsed.path.strip("/")
    if re.match(r"^[A-Za-z0-9]{3,12}$", path_segment):
        return final_url
    return None


# ---------------------------------------------------------------------------
# Per-article fetch strategies
# ---------------------------------------------------------------------------

def _try_direct(url: str, jina_api_key: str, base: dict) -> bool:
    """Attempt direct HTTP fetch of the live article page.

    Tries: ld+json articleBody → Jina AI reader → raw <p> extraction.
    Returns True and populates base on success; False otherwise.
    """
    try:
        html = _http_get(url)
    except Exception as e:
        log.debug("direct HTTP fetch failed for %s: %s", url, e)
        return False

    _fill_meta_from_ld(html, base)
    article = _load_ld(html)
    if article:
        body = article.get("articleBody", "") or ""
        if len(body) > 500:
            base["text"] = _prepend_dropcap(html, body)
            base["available"] = True
            base["archived_from"] = ""
            return True

    # Jina AI reader on the original URL (handles JS-rendered paywall pages).
    try:
        raw_jina = _jina_fetch(url, api_key=jina_api_key)
        jina_text = _clean_jina_text(raw_jina)
        if len(jina_text) > 500:
            if not base.get("title"):
                m = re.search(r"^Title:\s*(.+)$", raw_jina, re.MULTILINE)
                if m:
                    base["title"] = m.group(1).strip()
            base["text"] = jina_text
            base["available"] = True
            base["archived_from"] = ""
            return True
    except Exception as e:
        log.debug("Jina fetch failed for %s: %s", url, e)

    # Raw paragraph extraction from the HTML we already have.
    text = _fallback_paragraphs(html)
    if not base.get("title"):
        m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        if m:
            base["title"] = unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    if len(text) > 500:
        base["text"] = text
        base["available"] = True
        base["archived_from"] = ""
        return True

    return False


def _try_wayback(article_url: str, base: dict) -> bool:
    """Attempt text extraction from Wayback Machine snapshots.

    Iterates up to _MAX_WAYBACK_SNAPSHOTS captures newest-first.  For each:
      ld+json articleBody (usually present in SSR snapshot) → raw <p>.
    Does NOT call Jina on Wayback URLs — Jina cannot meaningfully process
    archive.org proxy URLs, and ld+json is nearly always sufficient here.

    Returns True and populates base on success; False otherwise.
    """
    snapshots = _wayback_snapshot_urls(article_url)
    if not snapshots:
        log.debug("Wayback: no status:200 snapshots found for %s", article_url)
        return False

    for snap_url in snapshots:
        try:
            html = _http_get(snap_url, timeout=25)
        except Exception as e:
            log.debug("Wayback fetch failed for %s: %s", snap_url, e)
            continue

        _fill_meta_from_ld(html, base)
        article = _load_ld(html)
        text = ""
        if article:
            text = article.get("articleBody", "") or ""
        if len(text) < 500:
            text = _fallback_paragraphs(html)
        if len(text) > 500:
            base["text"] = text
            base["available"] = True
            base["archived_from"] = "wayback"
            log.info("TOTT: text retrieved from Wayback snapshot %s", snap_url)
            return True

        log.debug(
            "Wayback snapshot %s: insufficient text (%d chars), trying next",
            snap_url, len(text),
        )

    return False


def _try_archiveph(article_url: str, base: dict) -> bool:
    """Attempt text extraction from an archive.ph snapshot.

    Returns True and populates base on success; False otherwise.
    """
    snap_url = _archiveph_snapshot_url(article_url)
    if not snap_url:
        log.debug("archive.ph: no snapshot found for %s", article_url)
        return False

    try:
        html = _http_get(snap_url, timeout=20)
    except Exception as e:
        log.debug("archive.ph page fetch failed for %s: %s", snap_url, e)
        return False

    _fill_meta_from_ld(html, base)
    article = _load_ld(html)
    text = ""
    if article:
        text = article.get("articleBody", "") or ""
    if len(text) < 500:
        text = _fallback_paragraphs(html)
    if len(text) > 500:
        base["text"] = text
        base["available"] = True
        base["archived_from"] = "archiveph"
        log.info("TOTT: text retrieved from archive.ph snapshot %s", snap_url)
        return True

    return False


# ---------------------------------------------------------------------------
# Public closure
# ---------------------------------------------------------------------------

def fetch_talk_of_the_town(covered_urls: set[str], jina_api_key: str = ""):
    """Closure capturing the covered-URL set so the tool takes no args.

    Tries up to _MAX_NOVEL_URLS uncovered articles, newest first.
    For each article all fetch strategies are tried in order:
      direct (ld+json → Jina → raw HTML) → Wayback (newest snapshot first) → archive.ph

    Dedup contract:
      - base["url"] is ALWAYS the canonical newyorker.com URL (not an archive URL)
        so it feeds correctly into the session dedup ledger.
      - base["archived_from"] records provenance for logging ("" | "wayback" | "archiveph").
      - The returned URL will be collected by collect_urls_from_sector() and added to
        session["dedup"]["covered_urls"], preventing the same article from being
        fetched again in future sessions.
    """

    def _run() -> str:
        """Try to fetch the latest uncovered TOTT article and return JSON."""

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
            "archived_from": "",
            "error": None,
        }

        # ----- 1. Discover article URLs -----
        # Priority: RSS (JS-free, reliable) → raw HTML TOC → Jina TOC.
        paths = _discover_from_rss()
        if paths:
            log.debug("RSS discovery: %d TOTT URLs found", len(paths))
        else:
            log.debug("RSS yielded no paths; falling back to raw TOC HTML")
            try:
                toc_html = _http_get(TOC_URL)
                paths = _extract_paths(toc_html)
            except Exception as e:
                log.debug("raw TOC fetch failed: %s", e)
            if not paths:
                log.debug("raw TOC yielded no paths; trying Jina reader for TOC")
                try:
                    jina_toc = _jina_fetch(TOC_URL, api_key=jina_api_key)
                    paths = _extract_paths(jina_toc)
                except Exception as jina_err:
                    log.debug("Jina TOC fetch failed: %s", jina_err)

        if not paths:
            base["error"] = "toc_no_paths_found"
            return json.dumps(base)

        # ----- 2. Build newest-first list of uncovered articles -----
        novel_urls = _novel_urls_sorted(paths, covered_urls)
        if not novel_urls:
            base["error"] = "all_articles_already_covered"
            return json.dumps(base)

        log.debug(
            "TOTT: %d novel URLs (of %d discovered); will try up to %d",
            len(novel_urls), len(paths), _MAX_NOVEL_URLS,
        )

        # ----- 3. Try each novel article, newest first -----
        for attempt_idx, url in enumerate(novel_urls[:_MAX_NOVEL_URLS]):
            # Reset per-attempt state so a failed earlier article doesn't
            # leak partial metadata into the next attempt.
            for k in ("title", "section", "dek", "byline", "date", "text", "archived_from"):
                base[k] = ""
            base["available"] = False
            base["error"] = None
            base["url"] = url  # canonical newyorker.com URL for dedup

            log.debug(
                "TOTT attempt %d/%d: %s",
                attempt_idx + 1, min(len(novel_urls), _MAX_NOVEL_URLS), url,
            )

            if _try_direct(url, jina_api_key, base):
                return json.dumps(base)

            log.debug("direct fetch failed for %s; trying Wayback", url)
            if _try_wayback(url, base):
                return json.dumps(base)

            log.debug("Wayback failed for %s; trying archive.ph", url)
            if _try_archiveph(url, base):
                return json.dumps(base)

            log.debug("all fetch methods failed for %s; trying next article", url)

        # ----- 4. All strategies failed for all tried articles -----
        # Report the most-recent article as the intended target in the error.
        base["url"] = novel_urls[0]
        tried = min(len(novel_urls), _MAX_NOVEL_URLS)
        base["error"] = (
            f"all_fetch_methods_failed: tried {tried} article(s) "
            f"(direct + wayback + archiveph)"
        )
        log.warning("TOTT: could not retrieve text after %d article attempts", tried)
        return json.dumps(base)

    return _run
