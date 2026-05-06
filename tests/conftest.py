"""Test fixtures shared across the suite.

Currently scoped to the sprint-18 TinyFish rollout: when ``TINYFISH_API_KEY``
is unset (the default in CI), monkey-patch ``jeeves.tools.tinyfish.extract_article``
to return a deterministic stub. This keeps any code path that imports the
TinyFish client hermetic — no real HTTP egress from pytest.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _stub_tinyfish_when_unconfigured(monkeypatch):
    """Replace ``tinyfish.extract_article`` with a stub when no key is set.

    Tests that explicitly want to exercise the real client should set
    ``TINYFISH_API_KEY`` in their own monkeypatch and re-import the module,
    or pass ``api_key`` directly if the function signature ever grows one.
    """
    if os.environ.get("TINYFISH_API_KEY", "").strip():
        # Real key in env — let the test exercise the real code path.
        return

    def _stub(url, *, timeout_seconds=30, max_chars=12_000, ledger=None):
        return {
            "url": url,
            "title": "stub",
            "text": "",
            "success": False,
            "extracted_via": "tinyfish",
            "quality_score": 0.0,
            "error": "TINYFISH_API_KEY not set (test stub)",
        }

    try:
        from jeeves.tools import tinyfish

        monkeypatch.setattr(tinyfish, "extract_article", _stub, raising=True)
    except Exception:
        # If tinyfish isn't importable (older branch / partial sync) just no-op.
        pass
