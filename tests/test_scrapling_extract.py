"""Scrapling stealth extractor — hermetic tests (2026-05-29).

Mocks ``scrapling.fetchers.StealthyFetcher`` so the test suite never
hits a real browser. Verifies the public contract that
``enrichment.fetch_article_text`` depends on:

* fail-soft on every error path (never raises),
* same return shape as tinyfish / playwright extractors,
* daily-cap respected via QuotaLedger.check_daily_allow,
* telemetry emitted on both success and failure paths,
* feature flag (JEEVES_USE_SCRAPLING) gates is_enabled().
"""
from __future__ import annotations

import json
import sys
import threading
import types
from pathlib import Path

import pytest

from jeeves.tools.quota import QuotaLedger


@pytest.fixture(autouse=True)
def _clean_scrapling_modules():
    """Reset sys.modules['scrapling*'] between tests so a fake stub from
    one test cannot poison the next. Required because tests intentionally
    install broken/working stubs at different points."""
    for key in list(sys.modules.keys()):
        if key == "scrapling" or key.startswith("scrapling."):
            sys.modules.pop(key, None)
    yield
    for key in list(sys.modules.keys()):
        if key == "scrapling" or key.startswith("scrapling."):
            sys.modules.pop(key, None)


def _make_ledger() -> QuotaLedger:
    """Build a hermetic QuotaLedger pinned to TODAY'S UTC date.

    _daily() auto-resets the daily dict if its date != today's UTC date.
    Hardcoding the date string would silently skip the daily cap test
    whenever the test runs on a different calendar day from the literal,
    so derive it at fixture build time.
    """
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}, "daily": {"date": today}}
    ledger._lock = threading.Lock()
    return ledger


class _FakePage:
    """Mimics the Scrapling Selector surface that scrapling_extract.py
    consults: ``.get_all_text(strip=True)`` and ``.css('title::text').get()``.
    """

    def __init__(self, text: str = "", title: str = ""):
        self._text = text
        self._title = title

    def get_all_text(self, strip: bool = True) -> str:
        return self._text

    def css(self, selector: str):
        page = self

        class _CssResult:
            def get(self):
                if selector == "title::text":
                    return page._title
                return ""

            def getall(self):
                return []

            def get_all_text(self, strip=True):
                if selector == "body":
                    return page._text
                return ""

        return _CssResult()


def _install_fake_scrapling(monkeypatch, *, page=None, raise_on_fetch=None):
    """Install a fake ``scrapling.fetchers`` module so the import inside
    ``extract_article`` resolves without the real package being present.
    """
    fetched_urls: list[str] = []

    class _FakeStealthy:
        @staticmethod
        def fetch(url, **kwargs):
            fetched_urls.append(url)
            if raise_on_fetch is not None:
                raise raise_on_fetch
            return page if page is not None else _FakePage(text="x" * 500, title="T")

    # Build the fake package + submodule.
    pkg = sys.modules.get("scrapling") or types.ModuleType("scrapling")
    fetchers_mod = sys.modules.get("scrapling.fetchers") or types.ModuleType(
        "scrapling.fetchers"
    )
    monkeypatch.setattr(fetchers_mod, "StealthyFetcher", _FakeStealthy, raising=False)
    sys.modules["scrapling"] = pkg
    sys.modules["scrapling.fetchers"] = fetchers_mod
    return fetched_urls


def _enable_telemetry(monkeypatch, tmp_path):
    from jeeves.tools import telemetry

    telemetry._close()
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))
    return telemetry


def _telemetry_rows(tmp_path: Path) -> list[dict]:
    files = list(tmp_path.glob("telemetry-*.jsonl"))
    if not files:
        return []
    return [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").strip().splitlines()
    ]


# ---------------------------------------------------------------------------
# is_enabled() — feature-flag gate
# ---------------------------------------------------------------------------


def test_is_enabled_false_by_default(monkeypatch):
    monkeypatch.delenv("JEEVES_USE_SCRAPLING", raising=False)
    from jeeves.tools.scrapling_extract import is_enabled

    assert is_enabled() is False


def test_is_enabled_true_when_flag_set(monkeypatch):
    monkeypatch.setenv("JEEVES_USE_SCRAPLING", "1")
    from jeeves.tools.scrapling_extract import is_enabled

    assert is_enabled() is True


def test_is_enabled_false_when_flag_is_zero(monkeypatch):
    """Defensive: only literal '1' enables; any other value (including '0',
    'true', 'yes') is treated as disabled to match the existing flag
    convention used by JEEVES_USE_CRAWL4AI_*.
    """
    monkeypatch.setenv("JEEVES_USE_SCRAPLING", "0")
    from jeeves.tools.scrapling_extract import is_enabled

    assert is_enabled() is False


# ---------------------------------------------------------------------------
# Contract: shape + fail-soft + daily cap + telemetry
# ---------------------------------------------------------------------------


def test_empty_url_returns_error_dict(monkeypatch):
    _install_fake_scrapling(monkeypatch)
    from jeeves.tools.scrapling_extract import extract_article

    result = extract_article("")
    assert result["success"] is False
    assert result["error"] == "empty url"
    assert result["extracted_via"] == "scrapling"
    assert result["text"] == ""


def test_successful_fetch_returns_text_and_title(monkeypatch):
    page = _FakePage(text="article body " * 100, title="A great headline")
    _install_fake_scrapling(monkeypatch, page=page)

    from jeeves.tools.scrapling_extract import extract_article

    result = extract_article("https://example.com/article")
    assert result["success"] is True
    assert result["title"] == "A great headline"
    assert result["text"].startswith("article body ")
    assert result["quality_score"] == 0.85
    assert result["extracted_via"] == "scrapling"


def test_max_chars_truncates_text(monkeypatch):
    page = _FakePage(text="x" * 5000, title="T")
    _install_fake_scrapling(monkeypatch, page=page)

    from jeeves.tools.scrapling_extract import extract_article

    result = extract_article("https://example.com/long", max_chars=500)
    assert len(result["text"]) == 500
    assert result["success"] is True


def test_below_min_content_length_returns_soft_failure(monkeypatch):
    """Content under _MIN_CONTENT_LENGTH (300 chars) means soft-paywall
    chrome or empty render — fail so the cascade falls through to
    Playwright."""
    page = _FakePage(text="too short", title="T")
    _install_fake_scrapling(monkeypatch, page=page)

    from jeeves.tools.scrapling_extract import extract_article

    result = extract_article("https://example.com/short")
    assert result["success"] is False
    assert "no content" in result["error"]
    assert result["extracted_via"] == "scrapling"


def test_stealthy_fetcher_exception_caught(monkeypatch):
    """When StealthyFetcher.fetch raises, we return success=False with the
    error in the dict — never propagate. Cascade must continue."""
    _install_fake_scrapling(
        monkeypatch, raise_on_fetch=RuntimeError("browser crashed")
    )

    from jeeves.tools.scrapling_extract import extract_article

    result = extract_article("https://example.com/bad")
    assert result["success"] is False
    assert "browser crashed" in result["error"]


def test_import_error_caught(monkeypatch):
    """When scrapling is not installed, the lazy import inside the function
    raises ImportError — we catch and soft-fail."""
    # Tear down any module-level stub from previous tests.
    sys.modules.pop("scrapling", None)
    sys.modules.pop("scrapling.fetchers", None)

    # Install a stub that raises ImportError at attribute access on
    # StealthyFetcher.
    class _Broken:
        def __getattr__(self, name):
            raise ImportError(f"scrapling missing: {name}")

    sys.modules["scrapling"] = _Broken()
    sys.modules["scrapling.fetchers"] = _Broken()

    from jeeves.tools.scrapling_extract import extract_article

    result = extract_article("https://example.com/whatever")
    assert result["success"] is False
    assert "fetch error" in result["error"] or "missing" in result["error"]


def test_daily_cap_blocks_before_fetch(monkeypatch):
    """When the daily cap is reached, the guard fires BEFORE the fetch
    happens — saves CI minutes on a runaway."""
    fetched = _install_fake_scrapling(monkeypatch)

    ledger = _make_ledger()
    # Force the daily counter to the cap (200 per quota.py:97).
    ledger._state["daily"]["scrapling"] = 200

    from jeeves.tools.scrapling_extract import extract_article

    result = extract_article("https://example.com/capped", ledger=ledger)
    assert result["success"] is False
    assert "daily cap" in result["error"]
    assert fetched == [], "fetch must be skipped when daily cap is hit"


def test_telemetry_emit_on_success(monkeypatch, tmp_path):
    page = _FakePage(text="body content " * 100, title="T")
    _install_fake_scrapling(monkeypatch, page=page)
    telemetry = _enable_telemetry(monkeypatch, tmp_path)

    from jeeves.tools.scrapling_extract import extract_article

    extract_article("https://example.com/good")
    telemetry._close()

    rows = _telemetry_rows(tmp_path)
    sc_rows = [r for r in rows if r.get("provider") == "scrapling"]
    assert sc_rows, f"no scrapling telemetry rows: {rows!r}"
    assert sc_rows[0]["ok"] is True
    assert sc_rows[0]["url"] == "https://example.com/good"
    assert sc_rows[0]["chars"] > 0


def test_telemetry_emit_on_fetch_failure(monkeypatch, tmp_path):
    _install_fake_scrapling(
        monkeypatch, raise_on_fetch=RuntimeError("dns lookup failed")
    )
    telemetry = _enable_telemetry(monkeypatch, tmp_path)

    from jeeves.tools.scrapling_extract import extract_article

    extract_article("https://example.com/badhost")
    telemetry._close()

    rows = _telemetry_rows(tmp_path)
    sc_rows = [
        r for r in rows
        if r.get("provider") == "scrapling" and r.get("ok") is False
    ]
    assert sc_rows, f"no scrapling failure telemetry rows: {rows!r}"
    assert "dns lookup failed" in sc_rows[0].get("error", "")


def test_ledger_records_on_success(monkeypatch):
    page = _FakePage(text="body content " * 100, title="T")
    _install_fake_scrapling(monkeypatch, page=page)

    ledger = _make_ledger()
    from jeeves.tools.scrapling_extract import extract_article

    extract_article("https://example.com/ledger", ledger=ledger)

    # record() bumps month counter; record_daily() bumps day counter.
    assert ledger._state["providers"].get("scrapling", {}).get("used", 0) == 1
    assert ledger._state["daily"].get("scrapling", 0) == 1
