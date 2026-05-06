"""Sprint-19 slice E: shadow-flag tests for serper_search.

Hermetic — every shadow runner is monkey-patched. Verifies:
* No env flags set → no shadow files written.
* Each shadow flag fires the correct runner and writes
  ``sessions/shadow-search-<provider>-<date>.jsonl``.
* Primary serper return value is unchanged whether shadows succeed or
  crash.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Route shadow JSONL output to a tmp dir + clear shadow env flags."""
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))
    for flag in (
        "JEEVES_JINA_SEARCH_SHADOW",
        "JEEVES_TINYFISH_SEARCH_SHADOW",
        "JEEVES_PLAYWRIGHT_SEARCH_SHADOW",
    ):
        monkeypatch.delenv(flag, raising=False)
    yield


@pytest.fixture
def fake_serper_response():
    return {
        "organic": [
            {"title": "A", "link": "https://example.com/a", "snippet": "sa"},
            {"title": "B", "link": "https://example.com/b", "snippet": "sb"},
        ]
    }


@pytest.fixture
def cfg_stub():
    class _C:
        serper_api_key = "k"
        jina_api_key = "k"

    return _C()


@pytest.fixture
def ledger_stub(tmp_path):
    from jeeves.tools.quota import QuotaLedger

    return QuotaLedger(tmp_path / ".quota-state.json")


def _patch_serper(monkeypatch, response: dict):
    from jeeves.tools import serper as serper_mod

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return response

    monkeypatch.setattr(serper_mod._HTTP_CLIENT, "post", lambda *a, **kw: _Resp())


# ---------------------------------------------------------------------------
# No flags → no JSONL
# ---------------------------------------------------------------------------

def test_serper_default_writes_no_shadow_jsonl(
    monkeypatch, cfg_stub, ledger_stub, fake_serper_response, tmp_path
):
    from jeeves.tools.serper import make_serper_search

    _patch_serper(monkeypatch, fake_serper_response)
    out = json.loads(make_serper_search(cfg_stub, ledger_stub)("test query"))
    assert len(out["results"]) == 2

    files = list(tmp_path.glob("shadow-search-*.jsonl"))
    assert files == []


# ---------------------------------------------------------------------------
# Each shadow flag fires its runner and writes JSONL
# ---------------------------------------------------------------------------

def test_jina_shadow_writes_jsonl(
    monkeypatch, cfg_stub, ledger_stub, fake_serper_response, tmp_path
):
    from jeeves.tools import search_shadow

    _patch_serper(monkeypatch, fake_serper_response)
    monkeypatch.setenv("JEEVES_JINA_SEARCH_SHADOW", "1")

    monkeypatch.setattr(
        search_shadow,
        "_shadow_via_jina",
        lambda q, cfg, ledger: {
            "success": True,
            "results": [{"title": "X", "url": "https://example.com/a"}],
            "latency_ms": 10,
            "error": None,
        },
    )

    from jeeves.tools.serper import make_serper_search

    out = json.loads(make_serper_search(cfg_stub, ledger_stub)("test query"))
    assert len(out["results"]) == 2  # primary unaffected

    files = sorted(tmp_path.glob("shadow-search-jina_search-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["primary"] == "serper"
    assert rec["shadow"] == "jina_search"
    assert rec["primary_n"] == 2
    assert rec["shadow_n"] == 1
    assert rec["jaccard"] == pytest.approx(1 / 2, abs=0.001)
    assert rec["shadow_success"] is True


def test_tinyfish_shadow_writes_jsonl(
    monkeypatch, cfg_stub, ledger_stub, fake_serper_response, tmp_path
):
    from jeeves.tools import search_shadow

    _patch_serper(monkeypatch, fake_serper_response)
    monkeypatch.setenv("JEEVES_TINYFISH_SEARCH_SHADOW", "1")
    monkeypatch.setattr(
        search_shadow,
        "_shadow_via_tinyfish",
        lambda q, cfg, ledger: {
            "success": True,
            "results": [{"title": "Y", "url": "https://example.com/c"}],
            "latency_ms": 5,
            "error": None,
        },
    )

    from jeeves.tools.serper import make_serper_search

    json.loads(make_serper_search(cfg_stub, ledger_stub)("q"))
    files = list(tmp_path.glob("shadow-search-tinyfish_search-*.jsonl"))
    assert len(files) == 1


def test_playwright_shadow_writes_jsonl(
    monkeypatch, cfg_stub, ledger_stub, fake_serper_response, tmp_path
):
    from jeeves.tools import search_shadow

    _patch_serper(monkeypatch, fake_serper_response)
    monkeypatch.setenv("JEEVES_PLAYWRIGHT_SEARCH_SHADOW", "1")
    monkeypatch.setattr(
        search_shadow,
        "_shadow_via_playwright",
        lambda q, cfg, ledger: {
            "success": True,
            "results": [{"title": "Z", "url": "https://example.com/a"}],
            "latency_ms": 1200,
            "error": None,
        },
    )

    from jeeves.tools.serper import make_serper_search

    json.loads(make_serper_search(cfg_stub, ledger_stub)("q"))
    files = list(tmp_path.glob("shadow-search-playwright_search-*.jsonl"))
    assert len(files) == 1


# ---------------------------------------------------------------------------
# Shadow crashes do not break primary
# ---------------------------------------------------------------------------

def test_shadow_crash_does_not_break_primary(
    monkeypatch, cfg_stub, ledger_stub, fake_serper_response, tmp_path
):
    from jeeves.tools import search_shadow

    _patch_serper(monkeypatch, fake_serper_response)
    monkeypatch.setenv("JEEVES_JINA_SEARCH_SHADOW", "1")
    monkeypatch.setattr(
        search_shadow,
        "_shadow_via_jina",
        lambda q, cfg, ledger: (_ for _ in ()).throw(RuntimeError("kaboom")),
    )

    from jeeves.tools.serper import make_serper_search

    out = json.loads(make_serper_search(cfg_stub, ledger_stub)("q"))
    assert len(out["results"]) == 2  # primary still returns
    files = list(tmp_path.glob("shadow-search-jina_search-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["shadow_success"] is False
    assert rec["shadow_error"]


def test_jaccard_zero_for_disjoint_sets(
    monkeypatch, cfg_stub, ledger_stub, fake_serper_response, tmp_path
):
    from jeeves.tools import search_shadow

    _patch_serper(monkeypatch, fake_serper_response)
    monkeypatch.setenv("JEEVES_JINA_SEARCH_SHADOW", "1")
    monkeypatch.setattr(
        search_shadow,
        "_shadow_via_jina",
        lambda q, cfg, ledger: {
            "success": True,
            "results": [{"title": "Q", "url": "https://other.example/x"}],
            "latency_ms": 1,
            "error": None,
        },
    )
    from jeeves.tools.serper import make_serper_search

    json.loads(make_serper_search(cfg_stub, ledger_stub)("q"))
    files = list(tmp_path.glob("shadow-search-jina_search-*.jsonl"))
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["jaccard"] == 0.0
