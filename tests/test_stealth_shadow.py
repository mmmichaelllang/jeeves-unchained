"""Sprint-20: shadow-flag tests for stealth-browser canary.

Mirrors ``test_search_shadow.py`` shape. Hermetic — every shadow runner
is monkey-patched and the playwright core is stubbed to avoid launching
a real browser. Verifies:

* No flag set → no ``shadow-stealth-*.jsonl`` written and the playwright
  primary result returns unchanged.
* ``JEEVES_STEALTH_SHADOW=1`` → shadow runner fires and a comparison
  record lands in ``sessions/shadow-stealth-<utc-date>.jsonl``.
* Shadow crash does not break the playwright primary return.
* Both shadows (tinyfish + stealth) can fire independently in one call;
  both files are written.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Run from a tmp cwd so shadow JSONL writes are scoped to the test
    and clear shadow env flags."""
    cwd = os.getcwd()
    monkeypatch.chdir(tmp_path)
    for flag in ("JEEVES_STEALTH_SHADOW", "JEEVES_TINYFISH_SHADOW",
                 "TINYFISH_API_KEY"):
        monkeypatch.delenv(flag, raising=False)
    yield
    os.chdir(cwd)


def _stub_pw_core(monkeypatch, *, text="x" * 800, title="t"):
    """Monkeypatch the playwright core extractor with a deterministic stub."""
    from jeeves.tools import playwright_extractor as pe

    def _fake(url, *, timeout_seconds, max_chars, crystallize):
        return {
            "url": url,
            "title": title,
            "text": text,
            "success": True,
            "extracted_via": "playwright",
            "quality_score": 0.85,
        }

    monkeypatch.setattr(pe, "_extract_article_core", _fake, raising=True)


# ---------------------------------------------------------------------------
# 1. No flag → no JSONL
# ---------------------------------------------------------------------------


def test_default_writes_no_shadow_jsonl(monkeypatch, tmp_path):
    _stub_pw_core(monkeypatch)
    from jeeves.tools import playwright_extractor as pe

    res = pe.extract_article("https://example.com/", timeout_seconds=5,
                             max_chars=500, crystallize=False)
    assert res["success"] is True
    assert list(Path(tmp_path).glob("sessions/shadow-stealth-*.jsonl")) == []
    assert list(Path(tmp_path).glob("sessions/shadow-tinyfish-*.jsonl")) == []


# ---------------------------------------------------------------------------
# 2. Stealth shadow fires and writes JSONL
# ---------------------------------------------------------------------------


def test_stealth_shadow_writes_jsonl(monkeypatch, tmp_path):
    _stub_pw_core(monkeypatch)
    monkeypatch.setenv("JEEVES_STEALTH_SHADOW", "1")

    from jeeves.tools import playwright_extractor as pe
    from jeeves.tools import stealth as stealth_mod

    def _fake_shadow(url, *, timeout_seconds, max_chars):
        return {
            "url": url,
            "title": "stealth-title",
            "text": "y" * 1000,
            "success": True,
            "extracted_via": "stealth",
            "quality_score": 0.78,
            "backend": "patchright",
            "auth_used": True,
            "_latency_ms": 9000,
        }

    monkeypatch.setattr(stealth_mod, "shadow_call", _fake_shadow, raising=True)

    res = pe.extract_article("https://www.nytimes.com/article",
                             timeout_seconds=5, max_chars=500, crystallize=False)
    assert res["success"] is True
    assert res["extracted_via"] == "playwright"  # primary unchanged

    files = sorted(Path(tmp_path).glob("sessions/shadow-stealth-*.jsonl"))
    assert len(files) == 1, f"expected 1 stealth shadow file, got {files}"
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["url"] == "https://www.nytimes.com/article"
    assert rec["playwright"]["success"] is True
    assert rec["stealth"]["success"] is True
    assert rec["stealth"]["backend"] == "patchright"
    assert rec["stealth"]["auth_used"] is True
    assert rec["stealth"]["char_count"] == 1000


# ---------------------------------------------------------------------------
# 3. Shadow crash does not affect primary
# ---------------------------------------------------------------------------


def test_stealth_shadow_crash_does_not_break_primary(monkeypatch, tmp_path):
    _stub_pw_core(monkeypatch)
    monkeypatch.setenv("JEEVES_STEALTH_SHADOW", "1")

    from jeeves.tools import playwright_extractor as pe
    from jeeves.tools import stealth as stealth_mod

    def _kaboom(url, *, timeout_seconds, max_chars):
        raise RuntimeError("shadow exploded")

    monkeypatch.setattr(stealth_mod, "shadow_call", _kaboom, raising=True)

    res = pe.extract_article("https://example.com/", timeout_seconds=5,
                             max_chars=500, crystallize=False)
    assert res["success"] is True  # primary survived

    files = sorted(Path(tmp_path).glob("sessions/shadow-stealth-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["stealth"]["success"] is False
    assert rec["stealth"]["error"]


# ---------------------------------------------------------------------------
# 4. Both shadows can fire independently
# ---------------------------------------------------------------------------


def test_both_shadows_write_independent_files(monkeypatch, tmp_path):
    _stub_pw_core(monkeypatch)
    monkeypatch.setenv("JEEVES_STEALTH_SHADOW", "1")
    monkeypatch.setenv("JEEVES_TINYFISH_SHADOW", "1")
    monkeypatch.setenv("TINYFISH_API_KEY", "fake-key-for-flag-gate")

    from jeeves.tools import playwright_extractor as pe
    from jeeves.tools import stealth as stealth_mod
    from jeeves.tools import tinyfish as tinyfish_mod

    monkeypatch.setattr(
        stealth_mod,
        "shadow_call",
        lambda url, *, timeout_seconds, max_chars: {
            "success": True, "text": "z" * 700, "title": "s",
            "backend": "camoufox", "auth_used": False, "_latency_ms": 5000,
            "quality_score": 0.7,
        },
        raising=True,
    )
    monkeypatch.setattr(
        tinyfish_mod,
        "extract_article",
        lambda url, *, timeout_seconds, max_chars, ledger=None: {
            "success": True, "text": "w" * 500, "title": "tf",
            "extracted_via": "tinyfish", "quality_score": 0.8,
        },
        raising=True,
    )

    pe.extract_article("https://example.com/", timeout_seconds=5,
                       max_chars=500, crystallize=False)

    stealth_files = list(Path(tmp_path).glob("sessions/shadow-stealth-*.jsonl"))
    tinyfish_files = list(Path(tmp_path).glob("sessions/shadow-tinyfish-*.jsonl"))
    assert len(stealth_files) == 1
    assert len(tinyfish_files) == 1
