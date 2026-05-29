"""tavily_extract: empty-URL guard hardening (2026-05-29 fix).

PROBLEM_CHRONICLE.md §4.2: the original guard at tavily.py:118-124 only
caught the fully-empty case (``urls=[]``). ``urls=['']`` and
``urls=['   ']`` slipped through because ``not ['']`` is False — the
Tavily SDK then received a list of garbage URLs and returned nothing,
leaving the agent with empty results and no diagnostic. Implicated in
the 2026-05-13 empty-research incident.

These tests pin the new shape: filter to http(s) URLs only, emit
``empty_url_filtered`` telemetry when the guard fires, return an ERROR
string the agent can learn from.
"""
from __future__ import annotations

import json
import sys
import threading
import types
from datetime import date
from pathlib import Path

import pytest

from jeeves.config import Config
from jeeves.tools.quota import QuotaLedger


def _make_cfg() -> Config:
    return Config(
        nvidia_api_key="",
        serper_api_key="",
        tavily_api_key="key",
        exa_api_key="",
        google_api_key="",
        groq_api_key="",
        gmail_app_password="",
        gmail_oauth_token_json="",
        github_token="",
        github_repository="test/repo",
        run_date=date(2026, 5, 29),
    )


def _make_ledger() -> QuotaLedger:
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()
    return ledger


def _install_fake_tavily(monkeypatch, captured: list):
    class FakeTavilyClient:
        def __init__(self, api_key):
            pass

        def extract(self, urls):
            captured.append(list(urls))
            return {
                "results": [
                    {"url": u, "raw_content": "fake body", "title": "T"}
                    for u in urls
                ]
            }

    fake_mod = sys.modules.get("tavily")
    if fake_mod is None:
        fake_mod = types.ModuleType("tavily")
        sys.modules["tavily"] = fake_mod
    monkeypatch.setattr(fake_mod, "TavilyClient", FakeTavilyClient, raising=False)
    monkeypatch.setattr(
        "jeeves.tools.tavily.TavilyClient", FakeTavilyClient, raising=False
    )


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
# Guard cases: the old guard caught (a); the new guard also catches (b)/(c)/(d).
# ---------------------------------------------------------------------------


def test_empty_list_returns_error_and_skips_sdk(monkeypatch):
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    result = fn(urls=[])

    assert result.startswith("ERROR:"), result
    assert "non-empty list of http(s) URLs" in result
    assert captured == [], (
        f"SDK must not be called when the guard fires, got captured={captured!r}"
    )


def test_single_empty_string_filtered(monkeypatch):
    """urls=[''] slipped through the OLD guard (``not ['']`` is False).
    NEW guard filters it out."""
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    result = fn(urls=[""])

    assert result.startswith("ERROR:"), result
    assert captured == [], (
        f"empty-string URL must not reach the SDK, got captured={captured!r}"
    )


def test_whitespace_only_filtered(monkeypatch):
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    result = fn(urls=["   ", "\t\n"])

    assert result.startswith("ERROR:"), result
    assert captured == []


def test_non_http_scheme_filtered(monkeypatch):
    """ftp:// / javascript: / mailto: / file:// must NOT reach Tavily.
    Defends against the agent inventing a URL with the wrong scheme."""
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    result = fn(urls=["ftp://x.com/article", "javascript:alert(1)", "mailto:x@y"])

    assert result.startswith("ERROR:"), result
    assert captured == []


def test_mixed_valid_and_invalid_keeps_only_valid(monkeypatch):
    """Partial filter: the agent passed one good URL and two garbage ones.
    Tavily should receive only the good one, the others silently dropped."""
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    result = fn(urls=["", "https://example.com/article", "javascript:bad"])

    # Not an ERROR — there was at least one valid URL.
    assert not result.startswith("ERROR:"), result
    assert captured, "SDK must be called with the surviving valid URL"
    assert captured[0] == ["https://example.com/article"], (
        f"SDK should see only the valid URL, got {captured[0]!r}"
    )


def test_valid_urls_pass_through_unchanged(monkeypatch):
    """Happy path: all-valid input survives the guard with identical content."""
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    urls = [
        "https://example.com/a",
        "http://example.org/b",
        "https://example.net/c",
    ]
    result = fn(urls=urls)

    assert not result.startswith("ERROR:")
    assert captured == [urls]


def test_leading_whitespace_stripped(monkeypatch):
    """Defensive: agent passes a URL with leading/trailing whitespace.
    Strip when filtering so the SDK gets a clean string."""
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    fn(urls=["  https://example.com/article  "])

    assert captured, "SDK must be called"
    assert captured[0] == ["https://example.com/article"], (
        f"Stripped URL expected, got {captured[0]!r}"
    )


def test_bare_string_url_still_works(monkeypatch):
    """Regression guard: tavily.py:127-128 wraps bare-string input. That
    behaviour MUST survive the guard tightening (test_research_sectors
    line 658 already pins this for extract; we re-pin it here too)."""
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    result = fn("https://example.com/article")

    assert not result.startswith("ERROR:")
    assert captured == [["https://example.com/article"]]


# ---------------------------------------------------------------------------
# Telemetry: guard fires must surface in the JSONL stream so the silent
# failure mode is observable via `grep empty_url_filtered`.
# ---------------------------------------------------------------------------


def test_telemetry_row_on_guard_fire(monkeypatch, tmp_path):
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    telemetry = _enable_telemetry(monkeypatch, tmp_path)

    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    fn(urls=[""])
    telemetry._close()

    rows = _telemetry_rows(tmp_path)
    extract_rows = [
        r for r in rows if r.get("provider") == "tavily_extract"
    ]
    assert extract_rows, f"no tavily_extract telemetry rows: {rows!r}"
    row = extract_rows[0]
    assert row.get("ok") is False
    assert row.get("error") == "empty_url_filtered", row
    assert row.get("urls") == 0


def test_no_guard_telemetry_on_valid_input(monkeypatch, tmp_path):
    """When the guard does NOT fire, the row should be the success row
    (ok=True, urls=N) — NOT an empty_url_filtered row. Prevents the new
    telemetry from polluting the JSONL on every successful call."""
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    telemetry = _enable_telemetry(monkeypatch, tmp_path)

    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(_make_cfg(), _make_ledger())
    fn(urls=["https://example.com/article"])
    telemetry._close()

    rows = _telemetry_rows(tmp_path)
    error_rows = [
        r for r in rows
        if r.get("provider") == "tavily_extract"
        and r.get("error") == "empty_url_filtered"
    ]
    assert error_rows == [], (
        f"empty_url_filtered must not emit when guard does not fire, "
        f"got rows={error_rows!r}"
    )
