"""Tests for Patch 1 (2026-05-10) — manifest persistence via try/finally.

Before this patch, ``_write_run_manifest`` was only called on the happy
path between draft-generation and SMTP send. Any exception OR non-zero
return in between left the manifest unwritten, hiding all post-run
quality_warnings from forensic review. The fix wraps the relevant block
in try/finally so the manifest writes regardless of how the function
exits.

These tests run ``scripts/write.py:main`` with monkeypatched dependencies
and assert that ``_write_run_manifest`` was invoked in every error path
where a ``BriefingResult`` was successfully produced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.write as write_script  # noqa: E402


@pytest.fixture
def manifest_spy(monkeypatch):
    """Spy on _write_run_manifest so tests can verify call count."""
    spy = MagicMock()
    monkeypatch.setattr(write_script, "_write_run_manifest", spy)
    return spy


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    """Minimal env to let scripts.write.main proceed past Config.from_env."""
    monkeypatch.setenv("NVIDIA_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_JSON", "{}")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "x/y")
    monkeypatch.setenv("JEEVES_REPO_ROOT", str(tmp_path))
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _stub_session(tmp_path):
    """Build a minimal SessionModel fixture file so main() loads it."""
    import json
    from datetime import datetime, timezone
    today = datetime.now(tz=timezone.utc).date().isoformat()
    session_path = tmp_path / "sessions" / f"session-{today}.json"
    session_path.write_text(json.dumps({
        "schema_version": "1",
        "date": today,
        "status": "complete",
        "weather": "Sunny.",
    }), encoding="utf-8")
    return today


def _stub_briefing_result():
    """Build a minimal BriefingResult that postprocess_html would normally return."""
    from jeeves.write import BriefingResult
    return BriefingResult(
        html="<!DOCTYPE html><html><body><p>x</p></body></html>",
        coverage_log=[],
        word_count=10,
        profane_aside_count=5,
        banned_word_hits=[],
        banned_transition_hits=[],
        aside_placement_violations=[],
        link_density=0.0,
        structure_errors=[],
        quality_warnings=[],
    )


# ============================================================================
# Patch 1 — manifest writes on every exit path
# ============================================================================

def test_manifest_written_on_skip_send(monkeypatch, base_env, manifest_spy):
    """--skip-send returns 0 early; manifest still writes."""
    today = _stub_session(base_env)

    # generate_briefing returns a fake raw HTML.
    async def fake_generate(*a, **kw):
        return "<p>x</p>", [], 9, 0
    monkeypatch.setattr(write_script, "generate_briefing", fake_generate)
    monkeypatch.setattr(
        write_script, "postprocess_html",
        lambda *a, **kw: _stub_briefing_result(),
    )

    rc = write_script.main(["--date", today, "--skip-send"])
    assert rc == 0
    assert manifest_spy.called, "manifest write skipped on --skip-send"


def test_manifest_written_when_smtp_fails(monkeypatch, base_env, manifest_spy):
    """SMTPConfigError returns 4; manifest must still write."""
    today = _stub_session(base_env)

    async def fake_generate(*a, **kw):
        return "<p>x</p>", [], 9, 0
    monkeypatch.setattr(write_script, "generate_briefing", fake_generate)
    monkeypatch.setattr(
        write_script, "postprocess_html",
        lambda *a, **kw: _stub_briefing_result(),
    )
    monkeypatch.setattr(
        write_script, "_apply_asides_gate",
        lambda *a, **kw: (_stub_briefing_result(), False),
    )

    def fake_send_html(*a, **kw):
        from jeeves.email import SMTPConfigError
        raise SMTPConfigError("missing creds")
    monkeypatch.setattr(write_script, "send_html", fake_send_html)

    rc = write_script.main(["--date", today])
    assert rc == 4
    assert manifest_spy.called, "manifest write skipped on SMTP failure"


def test_manifest_written_when_asides_gate_blocks(monkeypatch, base_env, manifest_spy):
    """Asides-gate block returns 5; manifest must still write."""
    today = _stub_session(base_env)

    async def fake_generate(*a, **kw):
        return "<p>x</p>", [], 9, 0
    monkeypatch.setattr(write_script, "generate_briefing", fake_generate)
    monkeypatch.setattr(
        write_script, "postprocess_html",
        lambda *a, **kw: _stub_briefing_result(),
    )
    monkeypatch.setattr(
        write_script, "_apply_asides_gate",
        lambda *a, **kw: (_stub_briefing_result(), True),
    )

    rc = write_script.main(["--date", today])
    assert rc == 5
    assert manifest_spy.called, "manifest write skipped when gate C blocked"


def test_manifest_written_on_exception(monkeypatch, base_env, manifest_spy):
    """Unexpected exception bubbles; manifest still writes from finally."""
    today = _stub_session(base_env)

    async def fake_generate(*a, **kw):
        return "<p>x</p>", [], 9, 0
    monkeypatch.setattr(write_script, "generate_briefing", fake_generate)
    monkeypatch.setattr(
        write_script, "postprocess_html",
        lambda *a, **kw: _stub_briefing_result(),
    )
    monkeypatch.setattr(
        write_script, "_apply_asides_gate",
        lambda *a, **kw: (_stub_briefing_result(), False),
    )

    def boom(*a, **kw):
        raise RuntimeError("simulated send-time crash")
    monkeypatch.setattr(write_script, "send_html", boom)

    with pytest.raises(RuntimeError, match="simulated send-time crash"):
        write_script.main(["--date", today])
    assert manifest_spy.called, "manifest write skipped on exception"


def test_manifest_NOT_written_for_dry_run(monkeypatch, base_env, manifest_spy):
    """--dry-run intentionally skips manifest write (no real metrics)."""
    today = _stub_session(base_env)
    monkeypatch.setattr(
        write_script, "render_mock_briefing",
        lambda s: "<p>mock</p>",
    )
    monkeypatch.setattr(
        write_script, "postprocess_html",
        lambda *a, **kw: _stub_briefing_result(),
    )

    rc = write_script.main(["--date", today, "--dry-run"])
    assert rc == 0
    assert not manifest_spy.called, "manifest should NOT write on dry-run"


def test_manifest_failure_does_not_override_exit_code(
    monkeypatch, base_env, manifest_spy,
):
    """If the manifest writer itself crashes, the briefing's exit code wins."""
    today = _stub_session(base_env)
    manifest_spy.side_effect = OSError("disk full")

    async def fake_generate(*a, **kw):
        return "<p>x</p>", [], 9, 0
    monkeypatch.setattr(write_script, "generate_briefing", fake_generate)
    monkeypatch.setattr(
        write_script, "postprocess_html",
        lambda *a, **kw: _stub_briefing_result(),
    )

    rc = write_script.main(["--date", today, "--skip-send"])
    # --skip-send → 0 even though manifest write raised.
    assert rc == 0
    assert manifest_spy.called
