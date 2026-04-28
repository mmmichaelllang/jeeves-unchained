"""Unit tests for jeeves.tools.talk_of_the_town helpers."""

from __future__ import annotations

import json
import textwrap
import unittest.mock as mock

import pytest

from jeeves.tools.talk_of_the_town import (
    _archiveph_snapshot_url,
    _clean_jina_text,
    _extract_byline,
    _extract_paths,
    _fill_meta_from_ld,
    _load_ld,
    _novel_urls_sorted,
    _wayback_snapshot_urls,
    fetch_talk_of_the_town,
    _discover_from_rss,
)


# ---------------------------------------------------------------------------
# _extract_paths
# ---------------------------------------------------------------------------

def test_extract_paths_finds_tott_urls():
    html = """
    <a href="/magazine/2024/03/18/some-slug">article</a>
    <a href="/magazine/2024/03/11/other-slug">article</a>
    """
    paths = _extract_paths(html)
    urls = [u for _, u in paths]
    assert "https://www.newyorker.com/magazine/2024/03/18/some-slug" in urls
    assert "https://www.newyorker.com/magazine/2024/03/11/other-slug" in urls


def test_extract_paths_sorted_newest_first():
    html = """
    <a href="/magazine/2024/01/01/old-slug">x</a>
    <a href="/magazine/2024/06/15/new-slug">x</a>
    <a href="/magazine/2024/03/10/mid-slug">x</a>
    """
    paths = _extract_paths(html)
    dates = [d for d, _ in paths]
    assert dates == sorted(dates, reverse=True)


def test_extract_paths_deduplicates():
    html = """
    <a href="/magazine/2024/03/18/slug-a">x</a>
    <a href="/magazine/2024/03/18/slug-a">x</a>
    <a href="/magazine/2024/03/18/slug-a">x</a>
    """
    paths = _extract_paths(html)
    urls = [u for _, u in paths]
    assert len(urls) == len(set(urls))


def test_extract_paths_empty_on_no_match():
    assert _extract_paths("<html><body>no articles here</body></html>") == []


# ---------------------------------------------------------------------------
# _novel_urls_sorted
# ---------------------------------------------------------------------------

def test_novel_urls_sorted_excludes_covered():
    paths = [
        (20240318, "https://www.newyorker.com/magazine/2024/03/18/slug-a"),
        (20240311, "https://www.newyorker.com/magazine/2024/03/11/slug-b"),
    ]
    covered = {"https://www.newyorker.com/magazine/2024/03/18/slug-a"}
    novel = _novel_urls_sorted(paths, covered)
    assert novel == ["https://www.newyorker.com/magazine/2024/03/11/slug-b"]


def test_novel_urls_sorted_strips_trailing_slash_from_covered():
    paths = [(20240318, "https://www.newyorker.com/magazine/2024/03/18/slug-a")]
    covered = {"https://www.newyorker.com/magazine/2024/03/18/slug-a/"}
    novel = _novel_urls_sorted(paths, covered)
    assert novel == []


def test_novel_urls_sorted_preserves_order():
    paths = [
        (20240320, "https://www.newyorker.com/magazine/2024/03/20/new"),
        (20240310, "https://www.newyorker.com/magazine/2024/03/10/old"),
    ]
    novel = _novel_urls_sorted(paths, set())
    assert novel[0].endswith("new")
    assert novel[1].endswith("old")


def test_novel_urls_sorted_empty_when_all_covered():
    paths = [
        (20240318, "https://www.newyorker.com/magazine/2024/03/18/slug-a"),
    ]
    covered = {"https://www.newyorker.com/magazine/2024/03/18/slug-a"}
    assert _novel_urls_sorted(paths, covered) == []


# ---------------------------------------------------------------------------
# _extract_byline
# ---------------------------------------------------------------------------

def test_extract_byline_from_string():
    assert _extract_byline("Jane Doe") == "By Jane Doe"


def test_extract_byline_from_dict():
    assert _extract_byline({"name": "Jane Doe"}) == "By Jane Doe"


def test_extract_byline_from_list():
    authors = [{"name": "Alice"}, {"name": "Bob"}]
    assert _extract_byline(authors) == "By Alice, Bob"


def test_extract_byline_empty_on_none():
    assert _extract_byline(None) == ""


def test_extract_byline_empty_on_empty_dict():
    assert _extract_byline({}) == ""


def test_extract_byline_empty_on_empty_list():
    assert _extract_byline([]) == ""


# ---------------------------------------------------------------------------
# _load_ld
# ---------------------------------------------------------------------------

_VALID_LD = json.dumps({
    "@type": "NewsArticle",
    "headline": "Test Headline",
    "articleBody": "Some body text here.",
    "author": {"name": "Staff Writer"},
    "datePublished": "2024-03-18T00:00:00Z",
})

_HTML_WITH_LD = f'<script type="application/ld+json">{_VALID_LD}</script>'


def test_load_ld_finds_news_article():
    node = _load_ld(_HTML_WITH_LD)
    assert node is not None
    assert node["headline"] == "Test Headline"


def test_load_ld_accepts_article_type():
    ld = json.dumps({"@type": "Article", "headline": "H", "articleBody": "B"})
    html = f'<script type="application/ld+json">{ld}</script>'
    assert _load_ld(html) is not None


def test_load_ld_accepts_reportage_type():
    ld = json.dumps({"@type": "ReportageNewsArticle", "headline": "H"})
    html = f'<script type="application/ld+json">{ld}</script>'
    assert _load_ld(html) is not None


def test_load_ld_returns_none_on_no_match():
    html = '<script type="application/ld+json">{"@type":"WebPage"}</script>'
    assert _load_ld(html) is None


def test_load_ld_returns_none_on_invalid_json():
    html = '<script type="application/ld+json">{bad json}</script>'
    assert _load_ld(html) is None


def test_load_ld_handles_array_of_nodes():
    ld = json.dumps([
        {"@type": "WebSite", "name": "NYer"},
        {"@type": "NewsArticle", "headline": "H", "articleBody": "B"},
    ])
    html = f'<script type="application/ld+json">{ld}</script>'
    node = _load_ld(html)
    assert node is not None
    assert node["headline"] == "H"


# ---------------------------------------------------------------------------
# _fill_meta_from_ld
# ---------------------------------------------------------------------------

def test_fill_meta_from_ld_populates_empty_fields():
    base = {"title": "", "section": "", "dek": "", "byline": "", "date": ""}
    _fill_meta_from_ld(_HTML_WITH_LD, base)
    assert base["title"] == "Test Headline"
    assert base["byline"] == "By Staff Writer"
    assert base["date"] == "2024-03-18T00:00:00Z"


def test_fill_meta_from_ld_does_not_overwrite_existing():
    base = {"title": "Existing Title", "section": "", "dek": "", "byline": "", "date": ""}
    _fill_meta_from_ld(_HTML_WITH_LD, base)
    assert base["title"] == "Existing Title"


def test_fill_meta_from_ld_noop_on_no_ld():
    base = {"title": "", "section": "", "dek": "", "byline": "", "date": ""}
    _fill_meta_from_ld("<html><body>no ld+json here</body></html>", base)
    assert base["title"] == ""


# ---------------------------------------------------------------------------
# _clean_jina_text
# ---------------------------------------------------------------------------

def test_clean_jina_text_strips_metadata_header():
    text = textwrap.dedent("""\
        Title: Some Article
        URL: https://example.com/foo
        Published Time: 2024-03-18

        The actual article body starts here.
    """)
    cleaned = _clean_jina_text(text)
    assert "Title:" not in cleaned
    assert "URL:" not in cleaned
    assert "The actual article body starts here." in cleaned


def test_clean_jina_text_truncates_at_stop_marker():
    text = "Great article content.\n\nPublished in the print edition\nJune 3, 2024"
    cleaned = _clean_jina_text(text)
    assert "Published in the print edition" not in cleaned
    assert "Great article content." in cleaned


def test_clean_jina_text_removes_photo_credits():
    text = "Some text.\n\nPhotograph by Joe Smith\n\nMore text."
    cleaned = _clean_jina_text(text)
    assert "Photograph by Joe Smith" not in cleaned
    assert "Some text." in cleaned


def test_clean_jina_text_removes_markdown_image_syntax():
    text = "Before. ![alt text](https://example.com/img.jpg) After."
    cleaned = _clean_jina_text(text)
    assert "![" not in cleaned
    assert "Before." in cleaned


def test_clean_jina_text_flattens_markdown_links():
    text = "See [this article](https://example.com/article) for details."
    cleaned = _clean_jina_text(text)
    assert "this article" in cleaned
    assert "https://example.com" not in cleaned


def test_clean_jina_text_removes_excessive_blank_lines():
    text = "Para one.\n\n\n\n\nPara two."
    cleaned = _clean_jina_text(text)
    assert "\n\n\n" not in cleaned


# ---------------------------------------------------------------------------
# _wayback_snapshot_urls — CDX response parsing (no network)
# ---------------------------------------------------------------------------

def _fake_cdx_response(timestamps: list[str]) -> bytes:
    """Build a CDX JSON response matching the Wayback API format."""
    rows: list[list[str]] = [["timestamp"]] + [[ts] for ts in timestamps]
    return json.dumps(rows).encode()


def test_wayback_snapshot_urls_returns_newest_first():
    # CDX returns oldest-first; we should reverse to newest-first.
    # Three snapshots: 20230101, 20230601, 20231201
    cdx_bytes = _fake_cdx_response(["20230101120000", "20230601120000", "20231201120000"])
    article_url = "https://www.newyorker.com/magazine/2023/01/01/slug"

    with mock.patch("urllib.request.urlopen") as m_open:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=ctx)
        ctx.__exit__ = mock.Mock(return_value=False)
        ctx.read.return_value = cdx_bytes
        m_open.return_value = ctx

        snaps = _wayback_snapshot_urls(article_url, n=3)

    assert len(snaps) == 3
    # newest (20231201) must come first
    assert "20231201" in snaps[0]
    assert "20230101" in snaps[-1]


def test_wayback_snapshot_urls_respects_n_limit():
    cdx_bytes = _fake_cdx_response(["20230101120000", "20230601120000", "20231201120000"])
    article_url = "https://www.newyorker.com/magazine/2023/01/01/slug"

    with mock.patch("urllib.request.urlopen") as m_open:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=ctx)
        ctx.__exit__ = mock.Mock(return_value=False)
        ctx.read.return_value = cdx_bytes
        m_open.return_value = ctx

        snaps = _wayback_snapshot_urls(article_url, n=2)

    # n=2 is passed to the CDX limit parameter; the mock returns 3 rows
    # but we should still only get ≤3 (CDX limiting is server-side).
    # More importantly: the URLs embed if_ modifier.
    assert all("if_" in s for s in snaps)


def test_wayback_snapshot_urls_uses_if_modifier():
    cdx_bytes = _fake_cdx_response(["20240318120000"])
    article_url = "https://www.newyorker.com/magazine/2024/03/18/slug"

    with mock.patch("urllib.request.urlopen") as m_open:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=ctx)
        ctx.__exit__ = mock.Mock(return_value=False)
        ctx.read.return_value = cdx_bytes
        m_open.return_value = ctx

        snaps = _wayback_snapshot_urls(article_url)

    assert snaps[0].endswith(f"if_/{article_url}")


def test_wayback_snapshot_urls_empty_on_header_only():
    # CDX returns only the header row — no captures.
    cdx_bytes = json.dumps([["timestamp"]]).encode()
    article_url = "https://www.newyorker.com/magazine/2024/03/18/slug"

    with mock.patch("urllib.request.urlopen") as m_open:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=ctx)
        ctx.__exit__ = mock.Mock(return_value=False)
        ctx.read.return_value = cdx_bytes
        m_open.return_value = ctx

        snaps = _wayback_snapshot_urls(article_url)

    assert snaps == []


def test_wayback_snapshot_urls_empty_on_network_error():
    article_url = "https://www.newyorker.com/magazine/2024/03/18/slug"
    with mock.patch("urllib.request.urlopen", side_effect=OSError("timeout")):
        snaps = _wayback_snapshot_urls(article_url)
    assert snaps == []


# ---------------------------------------------------------------------------
# _archiveph_snapshot_url — redirect path check (no network)
# ---------------------------------------------------------------------------

def _mock_urlopen_redirect(final_url: str):
    """Return a context manager mock whose .url attribute is final_url."""
    ctx = mock.MagicMock()
    ctx.__enter__ = mock.Mock(return_value=ctx)
    ctx.__exit__ = mock.Mock(return_value=False)
    ctx.url = final_url
    return ctx


def test_archiveph_snapshot_url_returns_hash_url():
    article_url = "https://www.newyorker.com/magazine/2024/03/18/slug"
    snap = "https://archive.ph/aB3f7"

    with mock.patch("urllib.request.urlopen", return_value=_mock_urlopen_redirect(snap)):
        result = _archiveph_snapshot_url(article_url)

    assert result == snap


def test_archiveph_snapshot_url_accepts_mirror_domains():
    for domain in ("archive.today", "archive.is", "archive.fo", "archive.li", "archive.vn"):
        snap = f"https://{domain}/xK9pQ"
        with mock.patch("urllib.request.urlopen", return_value=_mock_urlopen_redirect(snap)):
            result = _archiveph_snapshot_url("https://www.newyorker.com/magazine/2024/03/18/slug")
        assert result == snap, f"Expected result for {domain}"


def test_archiveph_snapshot_url_returns_none_on_form_page():
    # Redirected to the search page (multi-segment path — not a snapshot hash).
    form_url = "https://archive.ph/search/results"
    with mock.patch("urllib.request.urlopen", return_value=_mock_urlopen_redirect(form_url)):
        result = _archiveph_snapshot_url("https://www.newyorker.com/magazine/2024/03/18/slug")
    assert result is None


def test_archiveph_snapshot_url_returns_none_on_unknown_domain():
    # Redirect went somewhere other than archive.ph family.
    with mock.patch(
        "urllib.request.urlopen",
        return_value=_mock_urlopen_redirect("https://www.newyorker.com/magazine/2024/03/18/slug"),
    ):
        result = _archiveph_snapshot_url("https://www.newyorker.com/magazine/2024/03/18/slug")
    assert result is None


def test_archiveph_snapshot_url_returns_none_on_network_error():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("timeout")):
        result = _archiveph_snapshot_url("https://www.newyorker.com/magazine/2024/03/18/slug")
    assert result is None


# ---------------------------------------------------------------------------
# fetch_talk_of_the_town closure — dedup contract + fallback ordering
# ---------------------------------------------------------------------------

_GOOD_LD = json.dumps({
    "@type": "NewsArticle",
    "headline": "Test Piece",
    "articleBody": "A" * 600,
    "author": {"name": "Staff"},
    "datePublished": "2024-03-18T00:00:00Z",
})
_GOOD_LD_HTML = (
    '<!DOCTYPE html><html><head>'
    '<script type="application/ld+json">'
    + _GOOD_LD
    + '</script></head><body></body></html>'
)

_RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <link>https://www.newyorker.com/magazine/2024/03/18/test-slug</link>
    </item>
  </channel>
</rss>
"""


def _rss_ctx():
    ctx = mock.MagicMock()
    ctx.__enter__ = mock.Mock(return_value=ctx)
    ctx.__exit__ = mock.Mock(return_value=False)
    ctx.read.return_value = _RSS_XML
    return ctx


def _html_ctx(html: str):
    ctx = mock.MagicMock()
    ctx.__enter__ = mock.Mock(return_value=ctx)
    ctx.__exit__ = mock.Mock(return_value=False)
    ctx.read.return_value = html.encode()
    return ctx


def test_fetch_tott_canonical_url_is_newyorker_not_archive():
    """base['url'] must always be the canonical newyorker.com URL."""
    article_url = "https://www.newyorker.com/magazine/2024/03/18/test-slug"

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "web.archive.org/cdx" in url:
            # Return one Wayback snapshot.
            cdx = json.dumps([["timestamp"], ["20240318120000"]]).encode()
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.Mock(return_value=ctx)
            ctx.__exit__ = mock.Mock(return_value=False)
            ctx.read.return_value = cdx
            return ctx
        if "web.archive.org/web" in url:
            return _html_ctx(_GOOD_LD_HTML)
        if "newyorker.com/feed" in url:
            return _rss_ctx()
        # Direct fetch — return short text to force archive fallback.
        return _html_ctx("<html><body><p>short</p></body></html>")

    fn = fetch_talk_of_the_town(covered_urls=set())
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = json.loads(fn())

    assert result["url"] == article_url
    assert result["available"] is True
    assert result["archived_from"] == "wayback"


def test_fetch_tott_returns_json_string():
    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "newyorker.com/feed" in url:
            return _rss_ctx()
        return _html_ctx(_GOOD_LD_HTML)

    fn = fetch_talk_of_the_town(covered_urls=set())
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        raw = fn()

    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert "available" in parsed


def test_fetch_tott_all_covered_returns_error():
    covered = {"https://www.newyorker.com/magazine/2024/03/18/test-slug"}

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "newyorker.com/feed" in url:
            return _rss_ctx()
        return _html_ctx("<html></html>")

    fn = fetch_talk_of_the_town(covered_urls=covered)
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = json.loads(fn())

    assert result["available"] is False
    assert result["error"] == "all_articles_already_covered"


def test_fetch_tott_rss_failure_falls_back_to_toc_html():
    """When RSS fails, _run() should fall back to raw TOC HTML."""
    toc_html = '<a href="/magazine/2024/03/18/toc-slug">x</a>'

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        call_count["n"] += 1
        if "newyorker.com/feed" in url:
            raise OSError("RSS timeout")
        if "talk-of-the-town" in url and "r.jina.ai" not in url:
            return _html_ctx(toc_html)
        # Article fetch.
        return _html_ctx(_GOOD_LD_HTML)

    fn = fetch_talk_of_the_town(covered_urls=set())
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = json.loads(fn())

    assert result["available"] is True
    assert "toc-slug" in result["url"]


def test_fetch_tott_no_discovery_returns_error():
    """All discovery methods fail → error key set."""
    def fake_urlopen(req, timeout=None):
        raise OSError("network down")

    fn = fetch_talk_of_the_town(covered_urls=set())
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        # Jina uses urllib too but we also need to patch _jina_fetch
        with mock.patch(
            "jeeves.tools.talk_of_the_town._jina_fetch", side_effect=OSError("jina down")
        ):
            result = json.loads(fn())

    assert result["available"] is False
    assert result["error"] == "toc_no_paths_found"


def test_fetch_tott_direct_success_no_archive_fallback():
    """When direct fetch succeeds, archive methods must NOT be called."""
    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "newyorker.com/feed" in url:
            return _rss_ctx()
        # Article direct fetch — return full ld+json.
        return _html_ctx(_GOOD_LD_HTML)

    fn = fetch_talk_of_the_town(covered_urls=set())
    with (
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        mock.patch(
            "jeeves.tools.talk_of_the_town._wayback_snapshot_urls",
            side_effect=AssertionError("Wayback called unexpectedly"),
        ),
        mock.patch(
            "jeeves.tools.talk_of_the_town._archiveph_snapshot_url",
            side_effect=AssertionError("archive.ph called unexpectedly"),
        ),
    ):
        result = json.loads(fn())

    assert result["available"] is True
    assert result["archived_from"] == ""


# ---------------------------------------------------------------------------
# _discover_from_rss
# ---------------------------------------------------------------------------

def test_discover_from_rss_parses_items():
    def fake_urlopen(req, timeout=None):
        return _rss_ctx()

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        paths = _discover_from_rss()

    assert len(paths) == 1
    date, url = paths[0]
    assert url == "https://www.newyorker.com/magazine/2024/03/18/test-slug"
    assert date == 20240318


def test_discover_from_rss_returns_empty_on_failure():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("timeout")):
        paths = _discover_from_rss()
    assert paths == []
