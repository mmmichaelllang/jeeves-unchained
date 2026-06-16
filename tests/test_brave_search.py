"""brave_search: wrapper-shape + error handling (2026-06-16).

Brave is a search-cascade fallback added after serper credit exhaustion.
These tests pin the {provider, query, results:[{url,...}]} contract that
_run_crawl4ai_sector's cascade depends on, plus graceful failure when the
key is missing or the API errors.
"""
from __future__ import annotations

import json
import threading
from datetime import date

from jeeves.config import Config
from jeeves.tools.quota import QuotaLedger


def _make_cfg(brave_key: str = "key") -> Config:
    return Config(
        nvidia_api_key="",
        serper_api_key="",
        tavily_api_key="",
        exa_api_key="",
        google_api_key="",
        groq_api_key="",
        gmail_app_password="",
        gmail_oauth_token_json="",
        github_token="",
        github_repository="test/repo",
        run_date=date(2026, 6, 16),
        brave_api_key=brave_key,
    )


def _make_ledger() -> QuotaLedger:
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()
    return ledger


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_brave_search_returns_wrapper_shape(monkeypatch):
    import jeeves.tools.brave as brave

    payload = {
        "web": {
            "results": [
                {
                    "title": "World News",
                    "url": "https://example.com/a",
                    "description": "snippet a",
                    "age": "2026-06-16",
                    "profile": {"name": "Example"},
                },
                {
                    "title": "More News",
                    "url": "https://example.com/b",
                    "description": "snippet b",
                },
            ]
        }
    }
    monkeypatch.setattr(brave._HTTP_CLIENT, "get", lambda *a, **k: _FakeResp(payload))

    fn = brave.make_brave_search(_make_cfg(), _make_ledger())
    out = json.loads(fn(query="world news today"))

    assert out["provider"] == "brave"
    assert [r["url"] for r in out["results"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert out["results"][0]["snippet"] == "snippet a"


def test_brave_search_missing_key_returns_empty(monkeypatch):
    import jeeves.tools.brave as brave

    fn = brave.make_brave_search(_make_cfg(brave_key=""), _make_ledger())
    out = json.loads(fn(query="anything"))
    assert out["results"] == []
    assert "not set" in out["error"].lower()


def test_brave_search_api_error_returns_empty(monkeypatch):
    import jeeves.tools.brave as brave

    def _boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(brave._HTTP_CLIENT, "get", _boom)
    fn = brave.make_brave_search(_make_cfg(), _make_ledger())
    out = json.loads(fn(query="world news"))
    assert out["results"] == []
    assert "429" in out["error"]
