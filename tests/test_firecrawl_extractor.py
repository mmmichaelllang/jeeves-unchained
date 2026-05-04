"""Tests for jeeves.tools.firecrawl_extractor (sprint 16 audit fixes)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jeeves.tools import firecrawl_extractor as fc
from jeeves.tools.quota import QuotaLedger


def test_extract_article_returns_skip_when_no_api_key(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    out = fc.extract_article("https://example.com/x")
    assert out["success"] is False
    assert "FIRECRAWL_API_KEY not set" in out["error"]
    assert out["quality_score"] == 0.0


def test_extract_article_quality_score_field_always_present(monkeypatch):
    """quality_score MUST be in every return shape — same contract as playwright_extractor."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    out = fc.extract_article("https://example.com/x")
    assert "quality_score" in out


def test_extract_article_records_quota_via_record_not_increment(monkeypatch, tmp_path):
    """Audit fix: ledger.record() not ledger.increment() (typo would silently swallow)."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "success": True,
        "data": {
            "markdown": "# Heading\n\n" + ("Body paragraph. " * 50),
            "title": "Real Title",
        },
    }
    monkeypatch.setattr(fc.httpx, "post", lambda *a, **kw: fake_response)

    ledger = QuotaLedger(tmp_path / "q.json")
    out = fc.extract_article(
        "https://example.com/article", ledger=ledger
    )
    assert out["success"] is True
    assert out["title"] == "Real Title"
    assert out["quality_score"] == 0.85
    # Quota tracked under "firecrawl" key.
    counts = ledger.snapshot_used_counts()
    assert counts.get("firecrawl") == 1


def test_extract_article_does_not_call_increment(monkeypatch, tmp_path):
    """If ledger.record() doesn't exist (mock with only increment), we no-op
    silently — but we MUST NOT call .increment(). Use a sentinel ledger."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "success": True,
        "data": {"markdown": "x" * 500, "title": "T"},
    }
    monkeypatch.setattr(fc.httpx, "post", lambda *a, **kw: fake_response)

    record_calls = []
    increment_calls = []

    class _SpyLedger:
        def record(self, name):
            record_calls.append(name)

        def increment(self, name):
            increment_calls.append(name)

    fc.extract_article("https://example.com/x", ledger=_SpyLedger())
    assert record_calls == ["firecrawl"]
    assert increment_calls == []  # MUST NOT use the typo'd method


def test_extract_article_returns_failure_on_short_content(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "success": True,
        "data": {"markdown": "tiny", "title": "T"},
    }
    monkeypatch.setattr(fc.httpx, "post", lambda *a, **kw: fake_response)

    out = fc.extract_article("https://example.com/x")
    assert out["success"] is False
    assert "no content" in out["error"]
