"""Test fixtures shared across the suite.

Sprint-18 (TinyFish) and sprint-20 (stealth-browser) rollouts both use the
same hermetic-stub pattern: when the relevant secret / config is absent
(the default in CI), monkey-patch the extractor's public function to
return a deterministic failure dict so any code path that imports the
client stays free of real HTTP egress.
"""

from __future__ import annotations

import asyncio
import os

import pytest


@pytest.fixture(autouse=True)
def _reset_seen_url_cache():
    """Per-test reset of the enrichment seen_url_cache.

    The cache is intentionally module-level + per-RUN in production (the
    research script calls reset_seen_url_cache() once at startup). In tests
    we get many "runs" in one process, so test N would see test N-1's
    cached fetches and serve stale data. Reset before AND after so neither
    leaks in either direction.
    """
    try:
        from jeeves.tools.enrichment import reset_seen_url_cache as _r
        _r()
        yield
        _r()
    except Exception:
        yield


@pytest.fixture(autouse=True)
def _no_leaked_running_loop():
    """Defense-in-depth: any test that leaks a running loop (via Playwright
    sync API or otherwise) fails ON THAT TEST instead of polluting downstream
    tests. Added 2026-05-21 after the test_fetch_tott playwright leak that
    caused 3 false-positive failures in test_research_sectors.py."""
    yield
    leaked = asyncio._get_running_loop()
    if leaked is not None:
        # Clean up so we don't cascade-fail the rest of the run
        asyncio._set_running_loop(None)
        pytest.fail(
            f"Test leaked an asyncio running loop: {leaked!r}. "
            f"Likely caused by an unpatched sync API of an async "
            f"library (Playwright, Crawl4AI, etc)."
        )


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


@pytest.fixture(autouse=True)
def _stub_stealth_when_unconfigured(monkeypatch):
    """Replace ``stealth._extract_with_backend`` with a stub when no
    ``STEALTH_STORAGE_STATE_PATH`` is set.

    Sprint-20 sibling of ``_stub_tinyfish_when_unconfigured``. The stealth
    module is import-safe without patchright/camoufox installed (it
    chooses the backend lazily), but the backend launcher would either
    raise ImportError or attempt to spawn a real browser — neither is
    appropriate for a hermetic test. Tests that exercise the
    ``_extract_with_backend`` happy-path should monkeypatch it themselves.
    """
    if os.environ.get("STEALTH_STORAGE_STATE_PATH", "").strip():
        # Real config in env — let the test exercise the real code path.
        return

    def _stub(url, *, backend, storage_state_path, timeout_seconds, max_chars):
        return {
            "success": False,
            "title": "",
            "text": "",
            "backend": backend,
            "auth_used": bool(storage_state_path),
            "quality_score": 0.0,
            "error": "stealth backend stubbed (no STEALTH_STORAGE_STATE_PATH)",
        }

    try:
        from jeeves.tools import stealth

        monkeypatch.setattr(stealth, "_extract_with_backend", _stub, raising=True)
    except Exception:
        # If stealth isn't importable (older branch / partial sync) just no-op.
        pass
