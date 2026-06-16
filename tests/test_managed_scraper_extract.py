"""ZenRows/Scrapfly managed-scraper extractors (2026-06-16).

These are DORMANT opt-in fetch-chain tiers. Tests pin: (1) the enable flags
default OFF, (2) missing key fails soft, (3) a mocked successful fetch returns
the shared {success,title,text,extracted_via} shape via trafilatura.
"""
from __future__ import annotations

import jeeves.tools.managed_scraper_extract as ms


def test_flags_default_off(monkeypatch):
    monkeypatch.delenv("JEEVES_USE_ZENROWS", raising=False)
    monkeypatch.delenv("JEEVES_USE_SCRAPFLY", raising=False)
    assert ms.zenrows_enabled() is False
    assert ms.scrapfly_enabled() is False


def test_flags_on_when_set(monkeypatch):
    monkeypatch.setenv("JEEVES_USE_ZENROWS", "1")
    monkeypatch.setenv("JEEVES_USE_SCRAPFLY", "1")
    assert ms.zenrows_enabled() is True
    assert ms.scrapfly_enabled() is True


def test_zenrows_missing_key_fails_soft(monkeypatch):
    monkeypatch.delenv("ZENROWS_API_KEY", raising=False)
    out = ms.extract_article_zenrows("https://example.com/a")
    assert out["success"] is False
    assert "not set" in out["error"].lower()


def test_scrapfly_missing_key_fails_soft(monkeypatch):
    monkeypatch.delenv("SCRAPFLY_API_KEY", raising=False)
    out = ms.extract_article_scrapfly("https://example.com/a")
    assert out["success"] is False
    assert "not set" in out["error"].lower()


_ARTICLE_HTML = (
    "<html><head><title>Big Story</title></head><body><article>"
    + "<p>" + ("This is a substantial news article paragraph with real prose "
             "content that trafilatura will happily extract as the body. ") * 8
    + "</p></article></body></html>"
)


class _FakeResp:
    def __init__(self, *, text="", json_payload=None):
        self._text = text
        self._json = json_payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    @property
    def text(self):
        return self._text

    def json(self):
        return self._json


def test_zenrows_success_returns_text(monkeypatch):
    monkeypatch.setenv("ZENROWS_API_KEY", "k")
    monkeypatch.setattr(
        ms._HTTP_CLIENT, "get", lambda *a, **k: _FakeResp(text=_ARTICLE_HTML)
    )
    out = ms.extract_article_zenrows("https://example.com/a")
    assert out["success"] is True
    assert out["extracted_via"] == "zenrows"
    assert len(out["text"]) >= 300


def test_scrapfly_success_unwraps_json_content(monkeypatch):
    monkeypatch.setenv("SCRAPFLY_API_KEY", "k")
    # Scrapfly wraps page in {result: {content: html}}.
    monkeypatch.setattr(
        ms._HTTP_CLIENT, "get",
        lambda *a, **k: _FakeResp(json_payload={"result": {"content": _ARTICLE_HTML}}),
    )
    out = ms.extract_article_scrapfly("https://example.com/a")
    assert out["success"] is True
    assert out["extracted_via"] == "scrapfly"
    assert len(out["text"]) >= 300
