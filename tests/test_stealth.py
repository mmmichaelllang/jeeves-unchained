"""Sprint-20: stealth-browser canary skeleton tests.

Hermetic — every backend call is monkey-patched. Verifies:

* Conftest autouse stub returns a deterministic failure dict when no
  ``STEALTH_STORAGE_STATE_PATH`` is set in env.
* Quota ledger defaults expose the new ``stealth`` provider entry and a
  daily hard cap.
* Tool registration is gated on ``JEEVES_USE_STEALTH=1``.
* Tool wrapper returns a JSON string (NIM-safe contract).
* Daily-cap guard fires before the backend launches when the ledger
  reports the cap reached.
* Telemetry tool_call event is emitted on completed attempts.
* ``_state_for`` resolves auth host suffixes correctly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Route telemetry to a tmp dir + clear stealth env so tests are
    independent of the developer's own environment."""
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))
    for var in (
        "JEEVES_USE_STEALTH",
        "JEEVES_USE_CAMOUFOX",
        "JEEVES_USE_BROWSERFORGE",
        "JEEVES_STEALTH_SHADOW",
        "JEEVES_STEALTH_ARCHIVE_FALLBACK",
        "STEALTH_STORAGE_STATE_PATH",
        "STEALTH_FALLBACK_API",
        "STEALTH_FALLBACK_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Clear semaphore cache so JEEVES_RL_* overrides re-read each test.
    from jeeves.tools import rate_limits

    rate_limits.reset_for_tests()
    yield


# ---------------------------------------------------------------------------
# 1. Conftest autouse stub returns deterministic failure
# ---------------------------------------------------------------------------


def test_stealth_stub_returns_failure_without_config():
    from jeeves.tools import stealth

    res = stealth.extract_article(
        "https://example.com/", timeout_seconds=5, max_chars=1000
    )
    assert res["success"] is False
    assert res["extracted_via"] == "stealth"
    # Failure mode depends on env: stub fires when a backend is importable
    # ("not implemented" / "stubbed"), otherwise _backend_choice returns
    # "none" first and we get the no-backend message. Both are valid
    # default-off failure paths.
    assert res["error"], "error key must be populated on failure"
    # Shape contract — every key the call-site relies on is present.
    for k in ("url", "title", "text", "success", "extracted_via",
              "quality_score", "backend", "auth_used"):
        assert k in res, f"missing key {k}"


# ---------------------------------------------------------------------------
# 2. Quota ledger defaults expose stealth provider
# ---------------------------------------------------------------------------


def test_quota_ledger_has_stealth_default():
    from jeeves.tools.quota import (
        DAILY_HARD_CAPS,
        DEFAULT_STATE,
        _AUX_PROVIDERS,
    )

    assert "stealth" in DEFAULT_STATE
    assert DAILY_HARD_CAPS["stealth"] == 40
    assert "stealth" in _AUX_PROVIDERS


# ---------------------------------------------------------------------------
# 3. Tool registration gated on JEEVES_USE_STEALTH=1
# ---------------------------------------------------------------------------


def test_stealth_tool_not_registered_by_default(monkeypatch, tmp_path):
    pytest.importorskip("llama_index.core")

    from jeeves.tools import all_search_tools
    from jeeves.tools.quota import QuotaLedger

    class _Cfg:
        serper_api_key = ""
        tavily_api_key = ""
        exa_api_key = ""
        gemini_api_key = ""
        jina_api_key = ""

    monkeypatch.delenv("JEEVES_USE_STEALTH", raising=False)
    ledger = QuotaLedger(tmp_path / ".q.json")
    tools = all_search_tools(_Cfg(), ledger, prior_urls=set())
    names = {t.metadata.name for t in tools}
    assert "stealth_extract" not in names


def test_stealth_tool_registered_when_flag_set(monkeypatch, tmp_path):
    pytest.importorskip("llama_index.core")

    from jeeves.tools import all_search_tools
    from jeeves.tools.quota import QuotaLedger

    class _Cfg:
        serper_api_key = ""
        tavily_api_key = ""
        exa_api_key = ""
        gemini_api_key = ""
        jina_api_key = ""

    monkeypatch.setenv("JEEVES_USE_STEALTH", "1")
    ledger = QuotaLedger(tmp_path / ".q.json")
    tools = all_search_tools(_Cfg(), ledger, prior_urls=set())
    names = {t.metadata.name for t in tools}
    assert "stealth_extract" in names


# ---------------------------------------------------------------------------
# 4. Tool wrapper returns JSON string (NIM-safe)
# ---------------------------------------------------------------------------


def test_stealth_extract_tool_returns_json_string(monkeypatch, tmp_path):
    from jeeves.tools import _make_stealth_extract_tool
    from jeeves.tools.quota import QuotaLedger

    ledger = QuotaLedger(tmp_path / ".q.json")
    tool_fn = _make_stealth_extract_tool(ledger)
    out = tool_fn("https://example.com/")
    assert isinstance(out, str)
    parsed = json.loads(out)  # must be valid JSON
    assert parsed["url"] == "https://example.com/"
    assert parsed["success"] is False  # conftest stub
    assert parsed["extracted_via"] == "stealth"


def test_stealth_extract_tool_handles_crash(monkeypatch, tmp_path):
    """If extract_article itself raises, wrapper still returns a JSON string."""
    from jeeves.tools import _make_stealth_extract_tool, stealth as stealth_mod
    from jeeves.tools.quota import QuotaLedger

    def _kaboom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(stealth_mod, "extract_article", _kaboom, raising=True)
    ledger = QuotaLedger(tmp_path / ".q.json")
    tool_fn = _make_stealth_extract_tool(ledger)
    out = tool_fn("https://example.com/")
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert "boom" in parsed["error"]


# ---------------------------------------------------------------------------
# 5. Daily cap guard short-circuits before backend
# ---------------------------------------------------------------------------


def test_stealth_daily_cap_short_circuits(monkeypatch, tmp_path):
    from jeeves.tools import stealth
    from jeeves.tools.quota import DAILY_HARD_CAPS, QuotaLedger

    backend_called = {"n": 0}

    def _fake_backend(*a, **kw):
        backend_called["n"] += 1
        return {"success": True, "text": "x" * 1000, "title": "t",
                "backend": "patchright", "auth_used": False, "quality_score": 0.8}

    monkeypatch.setattr(stealth, "_extract_with_backend", _fake_backend, raising=True)
    monkeypatch.setattr(stealth, "_backend_choice", lambda: "patchright", raising=True)

    ledger = QuotaLedger(tmp_path / ".q.json")
    cap = DAILY_HARD_CAPS["stealth"]
    # Pretend we already used the entire daily budget.
    ledger.record_daily("stealth", cap)

    res = stealth.extract_article(
        "https://example.com/", timeout_seconds=5, max_chars=500, ledger=ledger
    )
    assert res["success"] is False
    assert "daily cap" in res["error"]
    assert backend_called["n"] == 0  # backend never invoked


# ---------------------------------------------------------------------------
# 6. Telemetry tool_call emitted on success
# ---------------------------------------------------------------------------


def test_stealth_emits_tool_call_telemetry(monkeypatch, tmp_path):
    from jeeves.tools import stealth
    from jeeves.tools.quota import QuotaLedger

    monkeypatch.setenv("JEEVES_TELEMETRY", "1")

    def _fake_backend(*a, **kw):
        return {
            "success": True,
            "text": "y" * 800,
            "title": "the title",
            "backend": "patchright",
            "auth_used": False,
            "quality_score": 0.82,
        }

    monkeypatch.setattr(stealth, "_extract_with_backend", _fake_backend, raising=True)
    monkeypatch.setattr(stealth, "_backend_choice", lambda: "patchright", raising=True)

    ledger = QuotaLedger(tmp_path / ".q.json")
    res = stealth.extract_article(
        "https://example.com/", timeout_seconds=5, max_chars=500, ledger=ledger
    )
    assert res["success"] is True

    tel_files = sorted(Path(tmp_path).glob("telemetry-*.jsonl"))
    assert tel_files, "telemetry file not written"
    lines = [
        json.loads(l)
        for l in tel_files[0].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    tool_calls = [r for r in lines if r["event"] == "tool_call"
                  and r.get("provider") == "stealth"]
    assert tool_calls, "no tool_call event for stealth"
    assert tool_calls[-1]["ok"] is True
    assert tool_calls[-1]["backend"] == "patchright"


# ---------------------------------------------------------------------------
# 7. Auth host resolver
# ---------------------------------------------------------------------------


def test_state_for_resolves_known_host(monkeypatch, tmp_path):
    """_state_for returns a path only if the file exists on disk."""
    from jeeves.tools import stealth

    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "nyt_state.json").write_text("{}")

    monkeypatch.setenv("STEALTH_STORAGE_STATE_PATH", str(auth_dir))

    # Subdomain match
    assert stealth._state_for("https://www.nytimes.com/2026/05/05/foo.html") \
        == str(auth_dir / "nyt_state.json")
    # Apex match
    assert stealth._state_for("https://nytimes.com/section/world") \
        == str(auth_dir / "nyt_state.json")
    # Known-host but state file missing
    assert stealth._state_for("https://www.ft.com/content/abc") is None
    # Unknown host
    assert stealth._state_for("https://example.com/") is None


def test_state_for_returns_none_without_env(monkeypatch):
    from jeeves.tools import stealth

    monkeypatch.delenv("STEALTH_STORAGE_STATE_PATH", raising=False)
    assert stealth._state_for("https://www.nytimes.com/foo") is None


# ---------------------------------------------------------------------------
# 8. Backend selection prefers camoufox when flag set
# ---------------------------------------------------------------------------


def test_backend_choice_falls_back_to_none(monkeypatch):
    """When neither browser is importable, _backend_choice returns 'none'."""
    from jeeves.tools import stealth

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def _fake_import(name, *a, **kw):
        if name in ("patchright", "camoufox", "playwright"):
            raise ImportError(f"stub: no {name}")
        return real_import(name, *a, **kw)

    monkeypatch.setitem(__builtins__ if isinstance(__builtins__, dict)
                        else __builtins__.__dict__,
                        "__import__", _fake_import)
    assert stealth._backend_choice() == "none"
