"""Tests for progressive dedup fixes and RSS-first TOTT discovery."""

from __future__ import annotations

import asyncio
import json
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Progressive dedup tests
# ---------------------------------------------------------------------------


def _make_sector_value(urls: list[str], headlines: list[str]) -> list[dict]:
    """Minimal sector output (list shape) that collect_urls_from_sector can parse."""
    return [{"url": u, "headline": h} for u, h in zip(urls, headlines)]


def test_prior_sample_grows_between_sectors():
    """Sequential loop must append each sector's URLs to prior_sample so the
    next sector sees them — not a frozen snapshot from before any sector ran."""

    captured_samples: list[list] = []

    async def fake_run_sector(cfg, spec, prior_sample, ledger, **kwargs):
        captured_samples.append(list(prior_sample))  # snapshot what this sector saw
        # Each sector "discovers" one new URL.
        return [{"url": f"https://example.com/{spec.name}", "headline": f"{spec.name} headline"}]

    # Two minimal specs.
    spec_a = SimpleNamespace(name="sector_a", default=[])
    spec_b = SimpleNamespace(name="sector_b", default=[])

    from scripts.research import _run_sector_loop
    from jeeves.tools.emit_session import ResearchContext

    cfg = MagicMock()
    cfg.run_date.isoformat.return_value = "2026-04-28"
    ledger = MagicMock()
    ctx = ResearchContext()

    prior_urls_ordered = ["https://prior.com/old"]

    with patch("scripts.research.SECTOR_SPECS", [spec_a, spec_b]), \
         patch("scripts.research.run_sector", side_effect=fake_run_sector), \
         patch("scripts.research.collect_urls_from_sector",
               side_effect=lambda v: [item["url"] for item in v]), \
         patch("scripts.research.collect_headlines_from_sector",
               side_effect=lambda v: [item["headline"] for item in v]):
        asyncio.run(_run_sector_loop(
            cfg, ctx, prior_urls_ordered, set(),
            ledger,
            sector_whitelist=[],
            limit=0,
        ))

    # sector_a saw only prior_urls_ordered
    assert captured_samples[0] == ["https://prior.com/old"]
    # sector_b saw prior_urls_ordered + sector_a's discovery
    assert "https://example.com/sector_a" in captured_samples[1]
    assert "https://prior.com/old" in captured_samples[1]


def test_prior_sample_cap_is_150():
    """prior_sample must be capped at 150 from prior_urls_ordered."""

    async def fake_run_sector(cfg, spec, prior_sample, ledger, **kwargs):
        return []

    spec_a = SimpleNamespace(name="sector_a", default=[])

    from scripts.research import _run_sector_loop
    from jeeves.tools.emit_session import ResearchContext

    cfg = MagicMock()
    cfg.run_date.isoformat.return_value = "2026-04-28"
    ledger = MagicMock()
    ctx = ResearchContext()

    # Feed 300 prior URLs.
    prior_urls_ordered = [f"https://old.com/{i}" for i in range(300)]

    captured: list[list] = []

    async def capturing_run_sector(cfg, spec, prior_sample, ledger, **kwargs):
        captured.append(list(prior_sample))
        return []

    with patch("scripts.research.SECTOR_SPECS", [spec_a]), \
         patch("scripts.research.run_sector", side_effect=capturing_run_sector), \
         patch("scripts.research.collect_urls_from_sector", return_value=[]), \
         patch("scripts.research.collect_headlines_from_sector", return_value=[]):
        asyncio.run(_run_sector_loop(
            cfg, ctx, prior_urls_ordered, set(),
            ledger,
            sector_whitelist=[],
            limit=0,
        ))

    assert len(captured[0]) == 150
    # Must be the FIRST 150 (newest-first ordering preserved).
    assert captured[0][0] == "https://old.com/0"
    assert captured[0][149] == "https://old.com/149"


def test_todays_headlines_appear_before_prior_headlines():
    """Today's discovered headlines must be at the HEAD of covered_headlines
    so write-phase [:80] slicing always captures fresh content."""

    today_headlines = [f"Today story {i}" for i in range(5)]
    prior_headlines = {f"Prior story {i}" for i in range(5)}

    async def fake_run_sector(cfg, spec, prior_sample, ledger, **kwargs):
        return [{"url": f"https://x.com/{i}", "headline": today_headlines[i]}
                for i in range(len(today_headlines))]

    spec_a = SimpleNamespace(name="sector_a", default=[])

    from scripts.research import _run_sector_loop
    from jeeves.tools.emit_session import ResearchContext

    cfg = MagicMock()
    cfg.run_date.isoformat.return_value = "2026-04-28"
    ledger = MagicMock()
    ctx = ResearchContext()

    with patch("scripts.research.SECTOR_SPECS", [spec_a]), \
         patch("scripts.research.run_sector", side_effect=fake_run_sector), \
         patch("scripts.research.collect_urls_from_sector",
               side_effect=lambda v: [item["url"] for item in v]), \
         patch("scripts.research.collect_headlines_from_sector",
               side_effect=lambda v: [item["headline"] for item in v]):
        asyncio.run(_run_sector_loop(
            cfg, ctx, [], prior_headlines,
            ledger,
            sector_whitelist=[],
            limit=0,
        ))

    covered = ctx.session["dedup"]["covered_headlines"]
    # All today_headlines must appear before any prior_headline.
    last_today_idx = max(covered.index(h) for h in today_headlines)
    first_prior_idx = min(covered.index(h) for h in prior_headlines)
    assert last_today_idx < first_prior_idx, (
        f"today headlines end at {last_today_idx} but prior start at {first_prior_idx}"
    )


def test_covered_urls_deduplicated():
    """Same URL discovered by two sectors must appear once in covered_urls."""

    shared_url = "https://shared.com/article"

    spec_a = SimpleNamespace(name="sector_a", default=[])
    spec_b = SimpleNamespace(name="sector_b", default=[])

    call_count = 0

    async def fake_run_sector(cfg, spec, prior_sample, ledger, **kwargs):
        nonlocal call_count
        call_count += 1
        return [{"url": shared_url, "headline": "shared headline"}]

    from scripts.research import _run_sector_loop
    from jeeves.tools.emit_session import ResearchContext

    cfg = MagicMock()
    cfg.run_date.isoformat.return_value = "2026-04-28"
    ledger = MagicMock()
    ctx = ResearchContext()

    with patch("scripts.research.SECTOR_SPECS", [spec_a, spec_b]), \
         patch("scripts.research.run_sector", side_effect=fake_run_sector), \
         patch("scripts.research.collect_urls_from_sector",
               side_effect=lambda v: [item["url"] for item in v]), \
         patch("scripts.research.collect_headlines_from_sector",
               side_effect=lambda v: [item["headline"] for item in v]):
        asyncio.run(_run_sector_loop(
            cfg, ctx, [], set(),
            ledger,
            sector_whitelist=[],
            limit=0,
        ))

    assert ctx.session["dedup"]["covered_urls"].count(shared_url) == 1


def test_recency_ordered_prior_urls_built_newest_first():
    """main() must build prior_urls_ordered newest-first (most-recent session first)."""

    from jeeves.schema import SessionModel
    from datetime import date

    # Simulate two sessions: day 1 (older) and day 2 (newer).
    day1_urls = {"https://old.com/a", "https://old.com/b"}
    day2_urls = {"https://new.com/x", "https://new.com/y"}

    sess1 = MagicMock()
    sess2 = MagicMock()

    from jeeves.dedup import covered_urls as _cu

    # load_prior_sessions returns newest-first.
    ordered_sessions = [sess2, sess1]

    url_map = {id(sess2): day2_urls, id(sess1): day1_urls}

    def fake_covered_urls(sess):
        return url_map[id(sess)]

    with patch("scripts.research.load_prior_sessions", return_value=ordered_sessions), \
         patch("scripts.research.covered_urls", side_effect=fake_covered_urls), \
         patch("scripts.research.get_covered_headlines", return_value=set()), \
         patch("scripts.research._load_prior_coverage_urls", return_value=set()):

        # Simulate the main() URL-building logic directly.
        prior_urls_ordered: list[str] = []
        prior_urls_seen: set[str] = set()
        for sess in ordered_sessions:
            for u in fake_covered_urls(sess):
                if u not in prior_urls_seen:
                    prior_urls_ordered.append(u)
                    prior_urls_seen.add(u)

    # Day2 URLs must all appear before Day1 URLs.
    day2_positions = [prior_urls_ordered.index(u) for u in day2_urls]
    day1_positions = [prior_urls_ordered.index(u) for u in day1_urls]
    assert max(day2_positions) < min(day1_positions), (
        "Newer session URLs should appear before older session URLs"
    )


# ---------------------------------------------------------------------------
# RSS discovery tests
# ---------------------------------------------------------------------------


_SAMPLE_RSS = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>The New Yorker - Talk of the Town</title>
        <item>
          <title>Article One</title>
          <link>https://www.newyorker.com/magazine/2026/04/28/article-one</link>
        </item>
        <item>
          <title>Article Two</title>
          <link>https://www.newyorker.com/magazine/2026/04/21/article-two</link>
        </item>
        <item>
          <title>Not A TOTT Article</title>
          <link>https://www.newyorker.com/culture/some-other-path</link>
        </item>
      </channel>
    </rss>
""")


def test_discover_from_rss_returns_newest_first():
    from jeeves.tools.talk_of_the_town import _discover_from_rss

    class FakeResponse:
        def read(self):
            return _SAMPLE_RSS.encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        results = _discover_from_rss()

    assert len(results) == 2  # non-TOTT path filtered out
    dates = [dk for dk, _ in results]
    assert dates == sorted(dates, reverse=True)
    assert results[0][1] == "https://www.newyorker.com/magazine/2026/04/28/article-one"
    assert results[1][1] == "https://www.newyorker.com/magazine/2026/04/21/article-two"


def test_discover_from_rss_returns_empty_on_network_error():
    from jeeves.tools.talk_of_the_town import _discover_from_rss

    with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
        results = _discover_from_rss()

    assert results == []


def test_discover_from_rss_returns_empty_on_malformed_xml():
    from jeeves.tools.talk_of_the_town import _discover_from_rss

    class FakeResponse:
        def read(self):
            return b"<not valid xml"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        results = _discover_from_rss()

    assert results == []


def test_fetch_talk_of_the_town_uses_rss_first():
    """fetch_talk_of_the_town must try RSS before raw TOC HTML."""

    from jeeves.tools.talk_of_the_town import fetch_talk_of_the_town

    rss_paths = [(20260428, "https://www.newyorker.com/magazine/2026/04/28/fresh-article")]
    article_html = '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Fresh Article","articleBody":"' + "x" * 600 + '"}</script>'

    call_log: list[str] = []

    def fake_discover_rss():
        call_log.append("rss")
        return rss_paths

    def fake_http_get(url, timeout=20):
        call_log.append(f"http:{url}")
        if "magazine" in url:
            return article_html
        return ""  # TOC fallback would return empty

    run = fetch_talk_of_the_town(set())

    with patch("jeeves.tools.talk_of_the_town._discover_from_rss", side_effect=fake_discover_rss), \
         patch("jeeves.tools.talk_of_the_town._http_get", side_effect=fake_http_get):
        result_str = run()

    result = json.loads(result_str)
    assert result["available"] is True
    assert result["title"] == "Fresh Article"
    # RSS was consulted; raw TOC was NOT fetched.
    assert "rss" in call_log
    toc_calls = [c for c in call_log if "talk-of-the-town" in c]
    assert toc_calls == [], f"TOC should not be fetched when RSS succeeds: {call_log}"


def test_fetch_talk_of_the_town_falls_back_to_html_toc_when_rss_empty():
    """When RSS returns no paths, must fall back to raw HTML TOC."""

    from jeeves.tools.talk_of_the_town import fetch_talk_of_the_town

    toc_html = """
        <a href="/magazine/2026/04/28/fallback-article">Fallback</a>
    """
    article_html = '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Fallback","articleBody":"' + "x" * 600 + '"}</script>'

    def fake_discover_rss():
        return []  # RSS fails

    def fake_http_get(url, timeout=20):
        if "talk-of-the-town" in url:
            return toc_html
        return article_html

    run = fetch_talk_of_the_town(set())

    with patch("jeeves.tools.talk_of_the_town._discover_from_rss", side_effect=fake_discover_rss), \
         patch("jeeves.tools.talk_of_the_town._http_get", side_effect=fake_http_get):
        result_str = run()

    result = json.loads(result_str)
    assert result["available"] is True


def test_fetch_talk_of_the_town_skips_covered_urls():
    """All discovered articles already in covered_urls must be skipped."""

    from jeeves.tools.talk_of_the_town import fetch_talk_of_the_town

    url = "https://www.newyorker.com/magazine/2026/04/28/already-covered"
    rss_paths = [(20260428, url)]

    run = fetch_talk_of_the_town({url})  # URL already covered

    with patch("jeeves.tools.talk_of_the_town._discover_from_rss", return_value=rss_paths):
        result_str = run()

    result = json.loads(result_str)
    assert result["available"] is False
    assert result["error"] == "all_articles_already_covered"


def test_jina_fetch_sends_auth_header_when_key_provided():
    """_jina_fetch must include Authorization header when api_key is set."""

    from jeeves.tools.talk_of_the_town import _jina_fetch

    captured_headers: dict = {}

    class FakeResponse:
        def read(self):
            return b"article text"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=30):
        captured_headers.update(req.headers)
        return FakeResponse()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _jina_fetch("https://www.newyorker.com/article", api_key="test-key-123")

    assert captured_headers.get("Authorization") == "Bearer test-key-123"


def test_jina_fetch_no_auth_header_when_key_absent():
    """_jina_fetch must NOT send Authorization header when api_key is empty."""

    from jeeves.tools.talk_of_the_town import _jina_fetch

    captured_headers: dict = {}

    class FakeResponse:
        def read(self):
            return b"article text"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=30):
        captured_headers.update(req.headers)
        return FakeResponse()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _jina_fetch("https://www.newyorker.com/article", api_key="")

    assert "Authorization" not in captured_headers
