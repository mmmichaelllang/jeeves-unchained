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
