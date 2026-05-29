"""exa_search: start_published_date wiring (2026-05-29 fix).

Same dead-path shape as tavily.py time_range — declared in signature
and tool description, prompted in research_sectors.py:689, but never
added to the kwargs dict passed to the Exa SDK. These tests pin the
wired path so the regression cannot reopen.
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
        tavily_api_key="",
        exa_api_key="key",
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


class _FakeResp:
    def __init__(self):
        self.results = []


class _FakeExa:
    """Captures (query, kwargs) per call so tests can assert on them."""
    last_kwargs: dict = {}
    last_query: str = ""

    def __init__(self, api_key):
        pass

    def search(self, query, **kwargs):
        _FakeExa.last_kwargs = dict(kwargs)
        _FakeExa.last_query = query
        return _FakeResp()


def _install_fake_exa(monkeypatch):
    _FakeExa.last_kwargs = {}
    _FakeExa.last_query = ""
    # exa.py does `from exa_py import Exa` inside the call — install the
    # module-level shim AND the package shim so both reach the FakeExa.
    if "exa_py" not in sys.modules:
        fake_mod = types.ModuleType("exa_py")
        fake_mod.Exa = _FakeExa
        sys.modules["exa_py"] = fake_mod
    else:
        monkeypatch.setattr("exa_py.Exa", _FakeExa, raising=False)


def test_start_published_date_none_omits_from_sdk_call(monkeypatch):
    """Default behavior preserved: when agent does not pass
    start_published_date, the SDK kwargs MUST NOT include the key.
    """
    _install_fake_exa(monkeypatch)
    from jeeves.tools.exa import make_exa_search

    fn = make_exa_search(_make_cfg(), _make_ledger())
    fn(query="triadic ontology relational metaphysics")

    assert _FakeExa.last_query == "triadic ontology relational metaphysics"
    assert "start_published_date" not in _FakeExa.last_kwargs, (
        f"start_published_date must be omitted when None, "
        f"got kwargs={_FakeExa.last_kwargs!r}"
    )


def test_start_published_date_passed_to_sdk(monkeypatch):
    """When agent passes start_published_date='2026-05-22', the SDK call
    MUST include start_published_date='2026-05-22'.
    """
    _install_fake_exa(monkeypatch)
    from jeeves.tools.exa import make_exa_search

    fn = make_exa_search(_make_cfg(), _make_ledger())
    fn(
        query="ai systems research recent papers",
        start_published_date="2026-05-22",
    )

    assert _FakeExa.last_kwargs.get("start_published_date") == "2026-05-22", (
        f"start_published_date must be forwarded, "
        f"got kwargs={_FakeExa.last_kwargs!r}"
    )


def test_start_published_date_emitted_in_telemetry(monkeypatch, tmp_path):
    """Telemetry row records the start_published_date value so a
    daily.yml run can be grep-verified.
    """
    _install_fake_exa(monkeypatch)

    from jeeves.tools import telemetry

    telemetry._close()
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))

    from jeeves.tools.exa import make_exa_search

    fn = make_exa_search(_make_cfg(), _make_ledger())
    fn(
        query="weather edmonds 2026",
        start_published_date="2026-05-22",
    )
    telemetry._close()

    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert files, "no telemetry file written"
    rows = [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").strip().splitlines()
        if json.loads(line).get("provider") == "exa"
    ]
    assert rows, "no exa telemetry rows"
    assert rows[0].get("start_published_date") == "2026-05-22", (
        f"start_published_date must appear in telemetry, got {rows[0]!r}"
    )


def test_start_published_date_empty_string_in_telemetry_when_none(
    monkeypatch, tmp_path
):
    """When agent doesn't pass start_published_date, telemetry row
    should show empty string — not be missing — for grep consistency.
    """
    _install_fake_exa(monkeypatch)

    from jeeves.tools import telemetry

    telemetry._close()
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))

    from jeeves.tools.exa import make_exa_search

    fn = make_exa_search(_make_cfg(), _make_ledger())
    fn(query="no freshness filter")
    telemetry._close()

    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert files
    rows = [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").strip().splitlines()
        if json.loads(line).get("provider") == "exa"
    ]
    assert rows
    assert rows[0].get("start_published_date") == "", (
        f"start_published_date must be '' when not passed, got {rows[0]!r}"
    )
