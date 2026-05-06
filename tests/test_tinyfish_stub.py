"""Smoke tests for the sprint-18 TinyFish integration.

* Conftest stub returns a deterministic no-content payload when no
  ``TINYFISH_API_KEY`` is in the environment (the CI default).
* Quota ledger defaults expose the new ``tinyfish`` provider entry and the
  30/day hard cap.
* ``playwright_extractor.extract_article`` remains a thin pass-through to
  ``_extract_article_core`` when shadow capture is disabled.
"""
from jeeves.tools import tinyfish


def test_stub_returns_no_content_without_key(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    res = tinyfish.extract_article(
        "https://example.com/", timeout_seconds=5, max_chars=1000
    )
    assert res["success"] is False
    assert res["extracted_via"] == "tinyfish"
    assert "TINYFISH_API_KEY" in res["error"] or "stub" in res["error"]


def test_quota_ledger_has_tinyfish_default():
    from jeeves.tools.quota import DAILY_HARD_CAPS, DEFAULT_STATE

    assert "tinyfish" in DEFAULT_STATE
    assert DAILY_HARD_CAPS["tinyfish"] == 30


def test_extract_article_wrapper_passes_through(monkeypatch):
    """extract_article must remain a thin wrapper over _extract_article_core."""
    monkeypatch.delenv("JEEVES_TINYFISH_SHADOW", raising=False)
    from jeeves.tools import playwright_extractor as pe

    calls = {}

    def _fake(url, *, timeout_seconds, max_chars, crystallize):
        calls["url"] = url
        return {
            "success": True,
            "text": "x" * 600,
            "title": "t",
            "extracted_via": "playwright",
            "quality_score": 0.9,
        }

    monkeypatch.setattr(pe, "_extract_article_core", _fake)
    out = pe.extract_article(
        "https://example.com/", timeout_seconds=5, max_chars=1000
    )
    assert out["success"] is True
    assert calls["url"] == "https://example.com/"
