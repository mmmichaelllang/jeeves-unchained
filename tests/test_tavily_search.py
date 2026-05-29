"""tavily_search: time_range wiring (2026-05-29 fix).

The time_range parameter had been declared in the function signature,
documented in the tool description, and prompted in
research_sectors.py:688 — but never actually passed to the Tavily SDK
call. These tests pin the wired path so the regression cannot reopen.
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

        def search(self, **kwargs):
            captured.append(kwargs)
            return {"results": [], "answer": ""}

    # tavily.py does an inline `from tavily import TavilyClient` per call,
    # so the fake MUST live on sys.modules["tavily"].TavilyClient for the
    # second-and-later test to see THIS test's captured list.
    fake_mod = sys.modules.get("tavily")
    if fake_mod is None:
        fake_mod = types.ModuleType("tavily")
        sys.modules["tavily"] = fake_mod
    monkeypatch.setattr(fake_mod, "TavilyClient", FakeTavilyClient, raising=False)
    monkeypatch.setattr(
        "jeeves.tools.tavily.TavilyClient", FakeTavilyClient, raising=False
    )


def test_time_range_none_omits_from_sdk_call(monkeypatch):
    """Default behavior preserved: when agent does not pass time_range,
    the SDK call must NOT include the key — some Tavily SDK versions
    reject time_range=None.
    """
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_search

    fn = make_tavily_search(_make_cfg(), _make_ledger())
    fn(query="Edmonds WA local news today")

    assert captured, "TavilyClient.search was never called"
    kwargs = captured[0]
    assert "time_range" not in kwargs, (
        f"time_range must be omitted when None, got kwargs={kwargs!r}"
    )
    # Sanity: the rest of the documented args are still wired.
    assert kwargs["query"] == "Edmonds WA local news today"
    assert kwargs["search_depth"] == "basic"
    assert kwargs["include_answer"] is True


def test_time_range_week_passed_to_sdk(monkeypatch):
    """When agent passes time_range='week', the SDK call MUST include
    time_range='week' — this is the new wired path.
    """
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)
    from jeeves.tools.tavily import make_tavily_search

    fn = make_tavily_search(_make_cfg(), _make_ledger())
    fn(query="weather Edmonds", time_range="week")

    assert captured, "TavilyClient.search was never called"
    assert captured[0].get("time_range") == "week", (
        f"time_range='week' must be forwarded, got kwargs={captured[0]!r}"
    )


def test_time_range_emitted_in_telemetry(monkeypatch, tmp_path):
    """Telemetry row records the time_range value so a daily.yml run can
    be grep-verified — the operator check is
    `jq -r '.time_range' sessions/telemetry-*.jsonl | sort -u`.
    """
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)

    from jeeves.tools import telemetry

    telemetry._close()
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))

    from jeeves.tools.tavily import make_tavily_search

    fn = make_tavily_search(_make_cfg(), _make_ledger())
    fn(query="global news", time_range="day")
    telemetry._close()

    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert files, "no telemetry file written"
    lines = [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").strip().splitlines()
    ]
    tavily_rows = [r for r in lines if r.get("provider") == "tavily"]
    assert tavily_rows, f"no tavily telemetry rows: {lines!r}"
    assert tavily_rows[0].get("time_range") == "day", (
        f"time_range='day' must appear in telemetry, got {tavily_rows[0]!r}"
    )


def test_time_range_empty_string_in_telemetry_when_none(monkeypatch, tmp_path):
    """When the agent doesn't pass time_range, the telemetry row should
    show time_range='' — not be missing — so the grep-verify is
    consistent across runs.
    """
    captured: list = []
    _install_fake_tavily(monkeypatch, captured)

    from jeeves.tools import telemetry

    telemetry._close()
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))

    from jeeves.tools.tavily import make_tavily_search

    fn = make_tavily_search(_make_cfg(), _make_ledger())
    fn(query="no freshness sector")
    telemetry._close()

    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert files
    rows = [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").strip().splitlines()
        if json.loads(line).get("provider") == "tavily"
    ]
    assert rows
    assert rows[0].get("time_range") == "", (
        f"time_range must be '' when not passed, got {rows[0]!r}"
    )
