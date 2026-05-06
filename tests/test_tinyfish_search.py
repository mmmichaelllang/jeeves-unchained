"""Sprint-19: smoke tests for ``tinyfish.search`` (managed-browser SERP).

Hermetic — no real HTTP. Verifies the no-key fail-soft, daily-cap guard,
and successful-shape normalisation.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def ledger(tmp_path: Path):
    from jeeves.tools.quota import QuotaLedger

    return QuotaLedger(tmp_path / ".quota-state.json")


def test_quota_defaults_include_tinyfish_search():
    from jeeves.tools.quota import DAILY_HARD_CAPS, DEFAULT_STATE

    assert "tinyfish_search" in DEFAULT_STATE
    assert DAILY_HARD_CAPS["tinyfish_search"] >= 1


def test_tinyfish_search_no_key(monkeypatch, ledger):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    from jeeves.tools import tinyfish

    out = tinyfish.search("hello", ledger=ledger)
    assert out["success"] is False
    assert "TINYFISH_API_KEY" in out["error"]
    assert out["results"] == []


def test_tinyfish_search_empty_query(ledger):
    from jeeves.tools import tinyfish

    out = tinyfish.search("", ledger=ledger)
    assert out["success"] is False
    assert "empty query" in out["error"]


def test_tinyfish_search_happy_path(monkeypatch, ledger):
    monkeypatch.setenv("TINYFISH_API_KEY", "test-key")
    from jeeves.tools import tinyfish

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "results": [
                        {
                            "title": "Edmonds school news",
                            "url": "https://myedmondsnews.com/x",
                            "snippet": "fresh story",
                            "published_at": "2026-05-04",
                        }
                    ]
                }
            }

    monkeypatch.setattr(tinyfish.httpx, "post", lambda *a, **kw: _Resp())

    out = tinyfish.search("edmonds news", ledger=ledger)
    assert out["success"] is True
    assert out["provider"] == "tinyfish_search"
    assert len(out["results"]) == 1
    assert out["results"][0]["url"] == "https://myedmondsnews.com/x"
    assert ledger.daily_used("tinyfish_search") == 1


def test_tinyfish_search_429_bumps_to_cap(monkeypatch, ledger):
    monkeypatch.setenv("TINYFISH_API_KEY", "test-key")
    from jeeves.tools import tinyfish
    from jeeves.tools.quota import DAILY_HARD_CAPS

    class _Resp:
        status_code = 429

    monkeypatch.setattr(tinyfish.httpx, "post", lambda *a, **kw: _Resp())

    out = tinyfish.search("q", ledger=ledger)
    assert out["success"] is False
    assert "429" in out["error"]
    assert ledger.daily_used("tinyfish_search") >= DAILY_HARD_CAPS["tinyfish_search"]
