"""Tests for URL-keyed cross-block paragraph dedup (sprint-17 finding F2.a)."""

from __future__ import annotations

from jeeves.write import (
    _canonical_url,
    _dedup_urls_across_blocks,
)


def test_canonical_url_strips_tracking_params():
    raw = "https://www.guardian.com/article-x?utm_source=twitter&utm_medium=social&id=1"
    assert _canonical_url(raw) == "https://www.guardian.com/article-x?id=1"


def test_canonical_url_strips_fragment_and_trailing_slash():
    assert (
        _canonical_url("https://Example.com/path/#section")
        == "https://example.com/path"
    )


def test_canonical_url_lowercases_host_only_not_path():
    assert (
        _canonical_url("HTTPS://Example.COM/Mixed/Case/Path")
        == "https://example.com/Mixed/Case/Path"
    )


def test_canonical_url_returns_empty_for_non_http():
    assert _canonical_url("mailto:a@b.com") == ""
    assert _canonical_url("javascript:void(0)") == ""
    assert _canonical_url("") == ""


def test_canonical_url_treats_utm_only_url_correctly():
    raw = "https://example.com/x?utm_source=foo&utm_medium=bar"
    assert _canonical_url(raw) == "https://example.com/x"


def test_dedup_urls_drops_lesser_paragraph_when_url_shared():
    """Two paragraphs cite the same URL. The richer one (more words +
    same anchor count) is kept; the leaner one is dropped."""
    html = (
        '<div class="container">'
        '<p>Short note. <a href="https://x.com/a">X</a> said something.</p>'
        '<p>'
        'Long detailed paragraph that names specific entities, dates, and '
        'figures. <a href="https://x.com/a">X</a> announced the policy '
        'shift after months of debate, with three named officials present '
        'and two specific dollar figures at stake.'
        '</p>'
        '</div>'
    )
    out = _dedup_urls_across_blocks(html)
    assert "Short note." not in out
    assert "Long detailed paragraph" in out
    assert out.count('href="https://x.com/a"') == 1


def test_dedup_urls_keeps_paragraph_with_unique_url():
    """A paragraph cites two URLs; one is duplicated elsewhere, the other
    is unique. We must NOT drop this paragraph (would lose the unique URL).

    Both URLs must remain present in the output. The lesser citer of the
    shared URL may be dropped; the citer of the unique URL must stay.
    """
    html = (
        '<div class="container">'
        '<p>'
        'Rich keeper paragraph that names <a href="https://x.com/a">X</a> '
        'and <a href="https://z.com/extra">Z</a> with substance and several '
        'named facts about the story.'
        '</p>'
        '<p>'
        'Short paragraph: <a href="https://x.com/a">X</a> and '
        '<a href="https://y.com/unique">Y</a>. Y appears only here.'
        '</p>'
        '</div>'
    )
    out = _dedup_urls_across_blocks(html)
    # Unique URL survives — the paragraph carrying it cannot be dropped.
    assert 'https://y.com/unique' in out
    # Shared URL still present at least once.
    assert 'https://x.com/a' in out
    # Z (also unique to the rich paragraph) still present.
    assert 'https://z.com/extra' in out


def test_dedup_urls_no_op_when_no_duplicates():
    html = (
        '<div class="container">'
        '<p>First. <a href="https://a.com/1">A</a> reports.</p>'
        '<p>Second. <a href="https://b.com/2">B</a> reports.</p>'
        '</div>'
    )
    out = _dedup_urls_across_blocks(html)
    assert out == html


def test_dedup_urls_skips_newyorker_block():
    """Verbatim TOTT block must not be touched even if it cites a URL
    that also appears outside."""
    html = (
        '<div class="container">'
        '<p>Outside. <a href="https://newyorker.com/article">NYR</a> piece.</p>'
        '<!-- NEWYORKER_START -->'
        '<div class="newyorker">'
        '<p>The verbatim text references <a href="https://newyorker.com/article">'
        'the article</a> as well.</p>'
        '</div>'
        '<!-- NEWYORKER_END -->'
        '</div>'
    )
    out = _dedup_urls_across_blocks(html)
    # Both occurrences remain — the NYR block is fenced.
    assert out.count('href="https://newyorker.com/article"') == 2


def test_dedup_urls_treats_tracking_param_variants_as_same_url():
    """Same article with and without utm_* params must canonicalize to
    one URL and be deduplicated."""
    html = (
        '<div class="container">'
        '<p>Short. <a href="https://x.com/a?utm_source=t">X</a>.</p>'
        '<p>'
        'Long rich paragraph that covers the same article in proper depth '
        'with multiple specific facts and figures named clearly. '
        '<a href="https://x.com/a">X</a>.'
        '</p>'
        '</div>'
    )
    out = _dedup_urls_across_blocks(html)
    # Both paragraphs canonicalized to https://x.com/a — short one drops.
    assert "Short." not in out
    assert "Long rich paragraph" in out
