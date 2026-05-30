"""Tests for the dedup-hardening fix stack (Flaws 1-8 + 10).

Covers behaviors not exercised by the foundational test_dedup_normalization
(canonical_url / canonical_headline pure functions) or the pre-existing
test_dedup_improvements (proportional cap).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jeeves.dedup import canonical_headline, canonical_url


# ---------------------------------------------------------------------------
# Flaw 4 — _find_cross_sector_dupes uses canonical_url
# ---------------------------------------------------------------------------


class TestCrossSectorDupesCanonical:
    def test_same_article_different_decorations_collapsed(self):
        from jeeves.research_sectors import _find_cross_sector_dupes

        session = {
            "global_news": [
                {"urls": ["https://www.reuters.com/article/x?utm_source=email"]}
            ],
            "intellectual_journals": [
                {"urls": ["https://m.reuters.com/article/x"]}
            ],
            "enriched_articles": [
                {"urls": ["https://reuters.com/article/x#section"]}
            ],
        }
        dupes = _find_cross_sector_dupes(session)
        # Pre-fix: 0 dupes (each URL distinct).  Post-fix: 1 dupe.
        assert len(dupes) == 1
        # Returned in canonical form so write phase can compare cleanly.
        assert dupes[0] == "https://reuters.com/article/x"

    def test_unique_urls_no_false_dupes(self):
        from jeeves.research_sectors import _find_cross_sector_dupes

        session = {
            "global_news": [{"urls": ["https://reuters.com/article/x"]}],
            "intellectual_journals": [{"urls": ["https://reuters.com/article/y"]}],
        }
        assert _find_cross_sector_dupes(session) == []


# ---------------------------------------------------------------------------
# Flaw 5 — _prune_cross_sector_dupes removes dupes from later sectors
# ---------------------------------------------------------------------------


class TestPruneCrossSectorDupes:
    def test_dupes_removed_from_later_sector(self):
        from scripts.research import _prune_cross_sector_dupes

        session = {
            "global_news": [
                {"findings": "first finding", "urls": ["https://foo.com/x"]}
            ],
            "intellectual_journals": [
                {"findings": "second finding", "urls": ["https://foo.com/x"]}
            ],
        }
        _prune_cross_sector_dupes(session, ["https://foo.com/x"])
        # global_news keeps the URL (first sector in _PRUNE_SECTOR_ORDER).
        assert session["global_news"][0]["urls"] == ["https://foo.com/x"]
        # intellectual_journals loses it — but item stays because findings present.
        assert session["intellectual_journals"][0]["urls"] == []
        assert session["intellectual_journals"][0]["findings"] == "second finding"

    def test_item_dropped_when_no_remaining_signal(self):
        from scripts.research import _prune_cross_sector_dupes

        session = {
            "global_news": [{"findings": "first", "urls": ["https://foo.com/x"]}],
            "intellectual_journals": [
                # No findings — would become empty after URL drop.
                {"urls": ["https://foo.com/x"]},
                # Survives — has findings.
                {"findings": "keep me", "urls": ["https://foo.com/x"]},
            ],
        }
        _prune_cross_sector_dupes(session, ["https://foo.com/x"])
        assert len(session["intellectual_journals"]) == 1
        assert session["intellectual_journals"][0]["findings"] == "keep me"

    def test_canonical_form_in_dupes_list_matches_decorated_urls(self):
        """Pruner receives canonical urls but session may carry decorated."""
        from scripts.research import _prune_cross_sector_dupes

        session = {
            "global_news": [{"findings": "A", "urls": ["https://www.foo.com/x"]}],
            "intellectual_journals": [
                {"findings": "B", "urls": ["https://m.foo.com/x?utm_source=mail"]}
            ],
        }
        _prune_cross_sector_dupes(session, ["https://foo.com/x"])
        # Both sectors had decorated forms but both canonicalize to the same key.
        assert session["global_news"][0]["urls"] == ["https://www.foo.com/x"]
        assert session["intellectual_journals"][0]["urls"] == []

    def test_no_dupes_is_noop(self):
        from scripts.research import _prune_cross_sector_dupes

        session = {
            "global_news": [{"urls": ["https://foo.com/x"], "findings": "A"}],
        }
        _prune_cross_sector_dupes(session, [])
        assert session["global_news"][0]["urls"] == ["https://foo.com/x"]


# ---------------------------------------------------------------------------
# Flaw 2 — proper-noun-anchored headline extraction
# ---------------------------------------------------------------------------


class TestDistinguishingLabel:
    def test_two_clusters_joined(self):
        from jeeves.research_sectors import _distinguishing_label

        text = "Trade tensions continue. Trump tariffs sting Asia heavily."
        label = _distinguishing_label(text)
        # Should pick up "Trump" + an Asia/related cluster from sentence 2.
        assert "Trump" in label

    def test_distinguishes_two_stories_starting_same(self):
        """The whole reason this exists: two stories with identical first
        sentences but distinct proper-noun bodies must dedup distinctly."""
        from jeeves.research_sectors import _distinguishing_label

        a = "AI policy update. OpenAI released GPT-X on Tuesday."
        b = "AI policy update. Anthropic shipped Claude 5 on Friday."
        la = _distinguishing_label(a)
        lb = _distinguishing_label(b)
        assert la != lb, f"labels collide: {la!r} vs {lb!r}"

    def test_falls_back_when_no_proper_nouns(self):
        from jeeves.research_sectors import _distinguishing_label

        text = "the weather was unusually warm and the wind picked up later."
        label = _distinguishing_label(text)
        # Falls back to first-two-sentences truncation.
        assert label  # non-empty

    def test_empty_input_returns_empty(self):
        from jeeves.research_sectors import _distinguishing_label
        assert _distinguishing_label("") == ""
        assert _distinguishing_label(None) == ""


# ---------------------------------------------------------------------------
# Flaw 3 — covered_topics sidecar persistence
# ---------------------------------------------------------------------------


class TestCoveredTopicsSidecar:
    def test_save_and_load_roundtrip(self, tmp_path):
        from datetime import date
        from jeeves.write import (
            _covered_topics_path,
            _load_prior_covered_topics,
            _save_covered_topics_sidecar,
        )

        cfg_today = SimpleNamespace(
            run_date=date(2026, 5, 21),
            sessions_dir=tmp_path,
        )
        _save_covered_topics_sidecar(cfg_today, ["Trump", "Federal Reserve", "OpenAI"])

        # Next day's load should pick it up.
        cfg_tomorrow = SimpleNamespace(
            run_date=date(2026, 5, 22),
            sessions_dir=tmp_path,
        )
        loaded = _load_prior_covered_topics(cfg_tomorrow)
        assert "Trump" in loaded
        assert "OpenAI" in loaded

    def test_load_missing_returns_empty(self, tmp_path):
        from datetime import date
        from jeeves.write import _load_prior_covered_topics

        cfg = SimpleNamespace(
            run_date=date(2026, 5, 22),
            sessions_dir=tmp_path,  # empty dir — no sidecars
        )
        assert _load_prior_covered_topics(cfg) == []

    def test_load_handles_corrupt_sidecar(self, tmp_path):
        from datetime import date
        from jeeves.write import _covered_topics_path, _load_prior_covered_topics

        cfg = SimpleNamespace(
            run_date=date(2026, 5, 22),
            sessions_dir=tmp_path,
        )
        prior = SimpleNamespace(run_date=date(2026, 5, 21), sessions_dir=tmp_path)
        path = _covered_topics_path(prior)
        path.write_text("not valid json {{{")
        # Should NOT raise; missing entry treated as absent.
        assert _load_prior_covered_topics(cfg) == []


# ---------------------------------------------------------------------------
# Flaw 8 — audit-driven cross-day overlap rewrite
# ---------------------------------------------------------------------------


class TestCrossDayOverlapRewrite:
    def _make_session(self, covered_urls):
        from jeeves.schema import SessionModel
        return SessionModel(
            date="2026-05-21",
            dedup={"covered_urls": covered_urls, "covered_headlines": []},
        )

    def test_pure_repeat_paragraph_collapsed(self):
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        # Covered URL is canonical form.
        session = self._make_session(["https://reuters.com/article/x"])
        html = (
            "<p>The Reuters story on the matter remains in flux, and the "
            "<a href=\"https://reuters.com/article/x?utm_source=mail\">latest update</a> "
            "shows tensions continuing to rise across all parties involved.</p>"
        )
        warnings: list[str] = []
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, warnings)
        assert "stands as we left it" in rewritten
        # Anchor and link preserved.
        assert "https://reuters.com/article/x?utm_source=mail" in rewritten
        assert any(w.startswith("cross_day_overlap_rewritten:") for w in warnings)

    def test_mixed_paragraph_left_intact(self):
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        # Old + new URL in same block → leave alone (would lose new citation).
        session = self._make_session(["https://reuters.com/old"])
        html = (
            "<p>The <a href=\"https://reuters.com/old\">Reuters piece</a> we covered "
            "previously connects to today's <a href=\"https://nyt.com/new\">NYT scoop</a> "
            "in interesting ways.</p>"
        )
        warnings: list[str] = []
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, warnings)
        assert "NYT scoop" in rewritten
        assert "stands as we left it" not in rewritten
        # Flag for human attention.
        assert any(w.startswith("cross_day_overlap_mixed_paragraph:") for w in warnings)

    def test_paragraph_with_only_new_url_untouched(self):
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session(["https://reuters.com/old"])
        html = "<p>A new <a href=\"https://nyt.com/fresh\">NYT report</a> emerged.</p>"
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, [])
        assert rewritten == html

    def test_no_covered_urls_no_op(self):
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session([])
        html = "<p>Any <a href=\"https://example.com\">link</a> at all.</p>"
        assert rewrite_cross_day_overlap_paragraphs(html, session, []) == html

    def test_paragraph_without_anchors_untouched(self):
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session(["https://reuters.com/x"])
        html = "<p>Just prose, no links here at all.</p>"
        assert rewrite_cross_day_overlap_paragraphs(html, session, []) == html

    # -----------------------------------------------------------------------
    # 2026-05-30: per-URL template spam fix.
    # Headline-guard (URL covered but anchor is new → leave) + post-pass
    # collapse of 2+ consecutive identical templates into one summary.
    # -----------------------------------------------------------------------

    def _make_session_with_headlines(self, covered_urls, covered_headlines):
        from jeeves.schema import SessionModel
        return SessionModel(
            date="2026-05-30",
            dedup={
                "covered_urls": covered_urls,
                "covered_headlines": covered_headlines,
            },
        )

    def test_url_repeat_new_headline_left_intact(self):
        """URL covered, but anchor text is a NEW headline → don't collapse.

        Catches the index-page case: bbc.com/news, parentmap.com/calendar etc.
        repeat the same URL daily but carry new stories. Pre-2026-05-30 the
        rewriter wrongly collapsed these, dropping fresh content.
        """
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session_with_headlines(
            covered_urls=["https://bbc.com/news"],
            covered_headlines=["Tariffs spark market jitters"],
        )
        html = (
            '<p>A <a href="https://bbc.com/news">poison seller sentenced</a> '
            'to fifteen years.</p>'
        )
        warnings: list[str] = []
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, warnings)
        assert "stands as we left it" not in rewritten
        assert "poison seller sentenced" in rewritten
        assert any(
            w.startswith("cross_day_url_repeat_new_headline:") for w in warnings
        )

    def test_url_repeat_matching_headline_collapsed(self):
        """URL covered AND anchor matches a covered headline → collapse."""
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session_with_headlines(
            covered_urls=["https://reuters.com/article/x"],
            covered_headlines=["Tensions continue to rise across all parties"],
        )
        html = (
            '<p>A <a href="https://reuters.com/article/x">Tensions continue '
            'to rise across all parties</a> in the dispute.</p>'
        )
        warnings: list[str] = []
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, warnings)
        assert "stands as we left it" in rewritten
        assert any(w.startswith("cross_day_overlap_rewritten:") for w in warnings)

    def test_empty_covered_headlines_preserves_url_only_behavior(self):
        """Backward compat: empty covered_headlines → URL match alone collapses.

        Existing callers that don't populate covered_headlines see the
        pre-2026-05-30 behavior unchanged.
        """
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session(["https://reuters.com/x"])
        html = '<p>A <a href="https://reuters.com/x">brand new story</a> here.</p>'
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, [])
        assert "stands as we left it" in rewritten

    def test_consecutive_stale_paragraphs_collapsed_to_summary(self):
        """3 back-to-back templates → 1 summary line. Reader-facing UX fix.

        Today's run had ai_systems emit 3 identical "stands as we left it"
        paragraphs because every URL was covered AND every anchor matched a
        prior headline. Post-pass merges them.
        """
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session_with_headlines(
            covered_urls=[
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
            ],
            covered_headlines=["story alpha", "story beta", "story gamma"],
        )
        html = (
            '<p>The <a href="https://example.com/a">story alpha</a> piece.</p>\n'
            '<p>The <a href="https://example.com/b">story beta</a> piece.</p>\n'
            '<p>The <a href="https://example.com/c">story gamma</a> piece.</p>'
        )
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, [])
        assert rewritten.count("stands as we left it") == 0
        assert rewritten.count("Several earlier threads stand as we left them") == 1

    def test_single_stale_paragraph_not_collapsed(self):
        """A lone template stays as-is — collapse only fires for runs of 2+."""
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session_with_headlines(
            covered_urls=["https://example.com/a"],
            covered_headlines=["story alpha"],
        )
        html = '<p>The <a href="https://example.com/a">story alpha</a> piece.</p>'
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, [])
        assert "stands as we left it" in rewritten
        assert "Several earlier threads stand as we left them" not in rewritten

    def test_non_adjacent_stale_paragraphs_each_kept(self):
        """Template + content + template (non-adjacent) → no merge.

        Collapse must only fire on CONSECUTIVE stale paragraphs, not across
        intervening real content.
        """
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session_with_headlines(
            covered_urls=["https://example.com/a", "https://example.com/b"],
            covered_headlines=["story alpha", "story beta"],
        )
        html = (
            '<p>The <a href="https://example.com/a">story alpha</a> piece.</p>\n'
            '<p>Some fresh prose with <a href="https://new.com/x">a new link</a>.</p>\n'
            '<p>The <a href="https://example.com/b">story beta</a> piece.</p>'
        )
        rewritten = rewrite_cross_day_overlap_paragraphs(html, session, [])
        assert rewritten.count("stands as we left it") == 2
        assert "Several earlier threads stand as we left them" not in rewritten

    def test_empty_covered_headlines_logs_observability_warning(self, caplog):
        """When covered_urls populated but covered_headlines empty → log warns.

        Normal pipeline has 606 covered_headlines today. If headlines drop to
        zero while URLs remain, something upstream skipped headline tracking
        and the URL-only fallback may misfire on index-page URLs. Surface it.
        """
        import logging
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session(["https://reuters.com/x"])  # no headlines
        html = '<p>A <a href="https://reuters.com/x">link</a> here.</p>'
        with caplog.at_level(logging.WARNING, logger="jeeves.write"):
            rewrite_cross_day_overlap_paragraphs(html, session, [])
        assert any(
            "covered_headlines_empty" in r.getMessage() for r in caplog.records
        )

    def test_populated_covered_headlines_no_observability_warning(self, caplog):
        """Normal state (headlines populated) → no warning, no spurious noise."""
        import logging
        from jeeves.write import rewrite_cross_day_overlap_paragraphs

        session = self._make_session_with_headlines(
            covered_urls=["https://reuters.com/x"],
            covered_headlines=["some prior story"],
        )
        html = '<p>A <a href="https://reuters.com/x">link</a> here.</p>'
        with caplog.at_level(logging.WARNING, logger="jeeves.write"):
            rewrite_cross_day_overlap_paragraphs(html, session, [])
        assert not any(
            "covered_headlines_empty" in r.getMessage() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Flaw 10 — seen_url_cache
# ---------------------------------------------------------------------------


class TestSeenURLCache:
    def setup_method(self):
        # Always start clean.
        from jeeves.tools.enrichment import reset_seen_url_cache
        reset_seen_url_cache()

    def test_second_call_skips_fetch(self):
        from jeeves.tools import enrichment

        impl_calls = {"n": 0}

        def fake_impl(url):
            impl_calls["n"] += 1
            return json.dumps({
                "url": url, "title": "T", "text": "x" * 500,
                "fetch_failed": False, "source": "foo.com",
            })

        with patch.object(enrichment, "_fetch_article_text_impl", side_effect=fake_impl):
            r1 = enrichment.fetch_article_text("https://foo.com/article")
            r2 = enrichment.fetch_article_text("https://foo.com/article")

        assert impl_calls["n"] == 1, "second call must hit cache, not impl"
        assert r1 == r2

    def test_decorated_variants_share_cache_entry(self):
        from jeeves.tools import enrichment

        impl_calls = {"n": 0}

        def fake_impl(url):
            impl_calls["n"] += 1
            return json.dumps({
                "url": url, "title": "T", "text": "x" * 500,
                "fetch_failed": False, "source": "foo.com",
            })

        with patch.object(enrichment, "_fetch_article_text_impl", side_effect=fake_impl):
            enrichment.fetch_article_text("https://www.foo.com/article?utm_source=a")
            enrichment.fetch_article_text("https://m.foo.com/article")
            enrichment.fetch_article_text("https://foo.com/article#frag")

        # All three canonicalize to https://foo.com/article — one impl call.
        assert impl_calls["n"] == 1

    def test_failed_fetch_not_cached(self):
        from jeeves.tools import enrichment

        impl_calls = {"n": 0}

        def fake_impl(url):
            impl_calls["n"] += 1
            return json.dumps({
                "url": url, "title": "", "text": "fetch_error",
                "fetch_failed": True, "source": "foo.com",
            })

        with patch.object(enrichment, "_fetch_article_text_impl", side_effect=fake_impl):
            enrichment.fetch_article_text("https://foo.com/dead")
            enrichment.fetch_article_text("https://foo.com/dead")

        # Failure NOT cached — retry attempted (different extractor chain may succeed).
        assert impl_calls["n"] == 2

    def test_reset_clears_cache(self):
        from jeeves.tools import enrichment

        impl_calls = {"n": 0}

        def fake_impl(url):
            impl_calls["n"] += 1
            return json.dumps({
                "url": url, "title": "T", "text": "x" * 500,
                "fetch_failed": False, "source": "foo.com",
            })

        with patch.object(enrichment, "_fetch_article_text_impl", side_effect=fake_impl):
            enrichment.fetch_article_text("https://foo.com/article")
            enrichment.reset_seen_url_cache()
            enrichment.fetch_article_text("https://foo.com/article")

        assert impl_calls["n"] == 2


# ---------------------------------------------------------------------------
# Flaw 1 — canonical_headline membership in research-phase dedup
# ---------------------------------------------------------------------------


class TestHeadlineDedupCanonical:
    def test_case_punct_variants_dedup_in_set(self):
        """Simulates the research-phase loop's prior_hl_keys membership check.

        Before fix: raw-string equality treated these as three distinct entries
        consuming three cap slots. Now: one canonical key, one entry.
        """
        variants = [
            "Trump tariffs sting Asia",
            "trump tariffs sting Asia.",
            "TRUMP TARIFFS STING ASIA",
        ]
        seen: set[str] = set()
        uniques = []
        for v in variants:
            key = canonical_headline(v)
            if key and key not in seen:
                seen.add(key)
                uniques.append(v)
        assert len(uniques) == 1
