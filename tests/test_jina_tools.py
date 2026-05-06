"""Sprint-19: smoke tests for the Jina search/deepsearch/rerank tools.

Tests are hermetic — every HTTP call site is monkey-patched. Verifies:

* No-key path returns the standard ``{provider, error, results: []}``
  envelope as a JSON string.
* Daily-cap pre-flight short-circuits before HTTP.
* 429 response flips the ledger counter to the cap.
* Successful response normalises to the documented result shape.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def ledger(tmp_path: Path):
    from jeeves.tools.quota import QuotaLedger

    return QuotaLedger(tmp_path / ".quota-state.json")


@pytest.fixture
def cfg_with_key():
    """Lightweight Config stand-in carrying only what the Jina tools touch."""

    class _C:
        jina_api_key = "test-key"

    return _C()


@pytest.fixture
def cfg_no_key():
    class _C:
        jina_api_key = ""

    return _C()


# ---------------------------------------------------------------------------
# Quota foundation
# ---------------------------------------------------------------------------


def test_quota_defaults_include_jina_entries():
    from jeeves.tools.quota import DAILY_HARD_CAPS, DEFAULT_STATE

    for name in ("jina_search", "jina_deepsearch", "jina_rerank"):
        assert name in DEFAULT_STATE
        assert name in DAILY_HARD_CAPS

    assert DAILY_HARD_CAPS["jina_search"] >= 50
    assert DAILY_HARD_CAPS["jina_deepsearch"] <= 50  # token-heavy → tight
    assert DAILY_HARD_CAPS["jina_rerank"] >= 20


# ---------------------------------------------------------------------------
# jina_search
# ---------------------------------------------------------------------------


def test_jina_search_empty_query_returns_error(cfg_with_key, ledger):
    from jeeves.tools.jina import make_jina_search

    out = json.loads(make_jina_search(cfg_with_key, ledger)(""))
    assert out["error"]
    assert out["results"] == []
    assert out["provider"] == "jina_search"


def test_jina_search_no_key_returns_error(cfg_no_key, ledger):
    from jeeves.tools.jina import make_jina_search

    out = json.loads(make_jina_search(cfg_no_key, ledger)("hello"))
    assert "JINA_API_KEY" in out["error"]


def test_jina_search_happy_path(monkeypatch, cfg_with_key, ledger):
    from jeeves.tools import jina as jina_mod

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {
                        "title": "Foo",
                        "url": "https://example.com/foo",
                        "description": "snippet text",
                        "date": "2026-05-01",
                        "source": "example.com",
                    }
                ]
            }

    monkeypatch.setattr(jina_mod._HTTP_CLIENT, "get", lambda *a, **kw: _Resp())

    out = json.loads(jina_mod.make_jina_search(cfg_with_key, ledger)("hello"))
    assert out["provider"] == "jina_search"
    assert out["query"] == "hello"
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["url"] == "https://example.com/foo"
    assert r["title"] == "Foo"
    assert r["snippet"] == "snippet text"
    assert ledger.snapshot_used_counts().get("jina_search", 0) >= 1


def test_jina_search_429_bumps_cap(monkeypatch, cfg_with_key, ledger):
    from jeeves.tools import jina as jina_mod
    from jeeves.tools.quota import DAILY_HARD_CAPS

    class _Resp:
        status_code = 429

    monkeypatch.setattr(jina_mod._HTTP_CLIENT, "get", lambda *a, **kw: _Resp())
    out = json.loads(jina_mod.make_jina_search(cfg_with_key, ledger)("hi"))
    assert "429" in out["error"]
    assert ledger.daily_used("jina_search") >= DAILY_HARD_CAPS["jina_search"]


# ---------------------------------------------------------------------------
# jina_deepsearch
# ---------------------------------------------------------------------------


def test_jina_deepsearch_no_key(cfg_no_key, ledger):
    from jeeves.tools.jina import make_jina_deepsearch

    out = json.loads(make_jina_deepsearch(cfg_no_key, ledger)("what is X"))
    assert out["error"]
    assert out["results"] == []


def test_jina_deepsearch_happy_path(monkeypatch, cfg_with_key, ledger):
    from jeeves.tools import jina as jina_mod

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "the answer"}}],
                "visitedURLs": ["https://a.example", "https://b.example"],
                "annotations": [
                    {"url_citation": {"url": "https://a.example", "title": "A"}},
                ],
            }

    monkeypatch.setattr(jina_mod._DEEPSEARCH_CLIENT, "post", lambda *a, **kw: _Resp())
    out = json.loads(
        jina_mod.make_jina_deepsearch(cfg_with_key, ledger)("triadic ontology 2026")
    )
    assert out["answer"] == "the answer"
    assert out["citations"][0]["url"] == "https://a.example"
    assert "https://b.example" in out["visited_urls"]


# ---------------------------------------------------------------------------
# jina_rerank
# ---------------------------------------------------------------------------


def test_jina_rerank_empty_documents(cfg_with_key, ledger):
    from jeeves.tools.jina import make_jina_rerank

    out = json.loads(make_jina_rerank(cfg_with_key, ledger)("q", []))
    assert out["error"]


def test_jina_rerank_csv_string_coerced(monkeypatch, cfg_with_key, ledger):
    """Kimi sometimes hands a CSV string instead of a list — must coerce."""
    from jeeves.tools import jina as jina_mod

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.4},
                ]
            }

    def _fake_post(url, headers=None, json=None, **kw):
        captured["docs"] = (json or {}).get("documents")
        return _Resp()

    monkeypatch.setattr(jina_mod._HTTP_CLIENT, "post", _fake_post)
    out = json.loads(
        jina_mod.make_jina_rerank(cfg_with_key, ledger)("q", "alpha,beta")
    )
    assert captured["docs"] == ["alpha", "beta"]
    assert out["ranked"][0]["score"] == 0.9
    assert out["ranked"][0]["document"] == "alpha"
