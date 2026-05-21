"""Tests for scripts/diagnostics/probe_crawl4ai.py (M0 milestone)."""
from __future__ import annotations

from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.diagnostics.probe_crawl4ai import (
    FetchResult,
    FULL_URLS,
    QUICK_URLS,
    score_result,
    write_report,
)


# ---------------------------------------------------------------------------
# 1. QUICK_URLS is a strict subset of FULL_URLS
# ---------------------------------------------------------------------------

def test_quick_urls_subset_of_full():
    for bucket, urls in QUICK_URLS.items():
        assert bucket in FULL_URLS
        for url in urls:
            assert url in FULL_URLS[bucket]
        assert len(urls) <= len(FULL_URLS[bucket])


# ---------------------------------------------------------------------------
# 2. score_result — combined branch: rich raw_markdown scores high
# ---------------------------------------------------------------------------

def test_score_result_full_content():
    res = FetchResult(
        url="https://example.com",
        bucket="test",
        # fit is thin (triggers raw fallback in combined strategy)
        fit_markdown_10="",
        fit_markdown_02="",
        raw_markdown="This is a great article with lots of real content words and no links at all. " * 30,
        trafilatura_text="",  # no trafilatura → vs_traf OK branch
    )
    score_result(res)
    assert res.score_combined >= 0.7, f"Expected ≥0.7 for rich raw content, got {res.score_combined}"


def test_score_result_crawl4ai_error_gives_zero():
    res = FetchResult(
        url="https://example.com",
        bucket="test",
        crawl4ai_error="Connection refused",
    )
    score_result(res)
    assert res.score_strict_10 == 0.0
    assert res.score_strict_02 == 0.0
    assert res.score_combined == 0.0


# ---------------------------------------------------------------------------
# 3. write_report produces valid file + correct OVERALL SCORE / DECISION lines
# ---------------------------------------------------------------------------

def test_write_report_proceed(tmp_path):
    results = [
        FetchResult(
            url="https://a.com", bucket="b1",
            raw_markdown="good content " * 700,  # ~8400 chars → combined uses raw[:8000]
            score_combined=0.9,
        ),
        FetchResult(
            url="https://b.com", bucket="b1",
            raw_markdown="another article " * 600,
            score_combined=0.85,
        ),
    ]
    out = tmp_path / "probe.md"
    s10, s02, combined = write_report(results, out)
    content = out.read_text()
    assert "OVERALL SCORE (combined):" in content
    assert "DECISION: PROCEED" in content
    assert combined >= 0.8


def test_write_report_revise(tmp_path):
    results = [
        FetchResult(url="https://a.com", bucket="b1", score_combined=0.2),
        FetchResult(url="https://b.com", bucket="b1", score_combined=0.1),
    ]
    out = tmp_path / "probe.md"
    s10, s02, combined = write_report(results, out)
    content = out.read_text()
    assert "DECISION: REVISE M1-M3" in content
    assert combined < 0.8
