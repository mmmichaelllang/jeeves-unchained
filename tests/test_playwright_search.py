"""Sprint-19: smoke tests for ``playwright_extractor.search``.

Hermetic — Playwright isn't actually installed in the CI test env (and we
never launch a real browser from tests), so the path-of-interest is the
``not _playwright_available()`` fail-soft and the daily-cap guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def ledger(tmp_path: Path):
    from jeeves.tools.quota import QuotaLedger

    return QuotaLedger(tmp_path / ".quota-state.json")


def test_quota_defaults_include_playwright_search():
    from jeeves.tools.quota import DAILY_HARD_CAPS, DEFAULT_STATE

    assert "playwright_search" in DEFAULT_STATE
    assert DAILY_HARD_CAPS["playwright_search"] >= 10


def test_playwright_search_empty_query(ledger):
    from jeeves.tools import playwright_extractor as pe

    out = pe.search("", ledger=ledger)
    assert out["success"] is False
    assert "empty query" in out["error"]


def test_playwright_search_unknown_engine(ledger):
    from jeeves.tools import playwright_extractor as pe

    out = pe.search("q", engine="kagi", ledger=ledger)
    assert out["success"] is False
    assert "unsupported engine" in out["error"]


def test_playwright_search_no_playwright(monkeypatch, ledger):
    from jeeves.tools import playwright_extractor as pe

    monkeypatch.setattr(pe, "_playwright_available", lambda: False)

    out = pe.search("hello", ledger=ledger)
    assert out["success"] is False
    assert "not installed" in out["error"]


def test_playwright_search_daily_cap(monkeypatch, ledger):
    """When the daily counter is at cap, the call short-circuits before
    even checking _playwright_available."""
    from jeeves.tools import playwright_extractor as pe
    from jeeves.tools.quota import DAILY_HARD_CAPS

    cap = DAILY_HARD_CAPS["playwright_search"]
    ledger.record_daily("playwright_search", cap)

    out = pe.search("hello", ledger=ledger)
    assert out["success"] is False
    assert "daily cap" in out["error"]


def test_playwright_search_happy_path(monkeypatch, ledger):
    """Stub the singleton context + parser so the function exercises its
    full happy path without launching chromium."""
    from jeeves.tools import playwright_extractor as pe

    monkeypatch.setattr(pe, "_playwright_available", lambda: True)

    class _Page:
        def goto(self, *a, **kw):
            return None

        def close(self):
            return None

        def set_default_timeout(self, _):
            return None

        def set_default_navigation_timeout(self, _):
            return None

    monkeypatch.setattr(pe, "_get_shared_context", lambda **kw: (_Page(), object()))
    monkeypatch.setattr(pe, "_wait_for_settled", lambda *a, **kw: True)
    # _PARSERS dict captures function references at import time, so patching
    # pe._parse_ddg alone wouldn't take effect — monkey-patch the dict entry.
    monkeypatch.setitem(
        pe._PARSERS,
        "ddg",
        lambda page, max_results: [
            {"title": "T", "url": "https://x.example/a", "snippet": "S"},
        ],
    )

    out = pe.search("hello", engine="ddg", ledger=ledger)
    assert out["success"] is True
    assert out["results"][0]["url"] == "https://x.example/a"
    assert out["results"][0]["provider"] == "playwright_ddg"
    assert ledger.daily_used("playwright_search") == 1
